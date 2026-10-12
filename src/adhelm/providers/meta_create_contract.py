"""One exact paused Meta Ads v26.0 single-image hierarchy CREATE, without authority.

This is the P04-MA contract layer.  It turns one *retained* offline compiler
candidate (:mod:`adhelm.providers.meta_compiler`) plus one fresh bounded EDIT-token
read into a closed :class:`MetaAdsSingleImageHierarchyCreatePlannedAction` whose
``plan`` is the ordinary provider-neutral :class:`~adhelm.safety.CompositePlan` the
kernel executes.

It mirrors ``providers/openai_create_contract.py`` deliberately, because the
shape of the problem is the same and the reviewed differences are the point.

* **The approved operation is five ordered requests.**  Meta documents no atomic
  hierarchy mutation: the image, the campaign, the ad set, the ad creative and
  the ad are five separate ``POST`` edges under the ad account, and each later
  one needs an identifier only the earlier one returns.  The subplan therefore
  declares ``expected_child_egress_count = 5`` and is executed by a
  :class:`~adhelm.safety.ChildEgressAdapter`.
* **``before`` and ``after`` are semantic hierarchy fingerprints.**  Meta assigns
  every native identifier at creation time, so the states are locally keyed
  HMACs (:mod:`adhelm.providers._semantic_state`) over request-controllable names
  and configuration only.  ``before`` is the empty hierarchy under one
  ``(ad account, campaign name)`` binding and ``after`` is the exact predicted
  member set.
* **The uploaded image *can* be read back, unlike OpenAI's file.**  Meta
  documents ``GET /act_{id}/adimages?hashes=[...]``, so the account's own
  possession of the uploaded ``hash`` is an observation rather than an
  assumption.  That is still not byte equality: ``hash`` is an opaque provider
  value and no documented route returns the stored bytes or a content digest of
  them, so ``PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ`` is retained
  separately and never collapsed into the association claim.
* **The compiled candidate's own ``image_hash`` is never sent.**  The compiler
  requires a customer-supplied opaque ``image_hash`` with local evidence.  This
  route does not send it: the ad creative must reference the image *this
  attempt's own upload* minted, because that is the only association a readback
  can prove.  The plan records
  ``compiled_image_reference_use: LOCAL_EVIDENCE_NOT_SENT``.

Nothing here reads a credential, contacts Meta, or authorizes anything.  A
matching fingerprint proves neither provider origin, freshness, permission,
reservation, execution authority, nor a mutation outcome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import MAX_CHILD_EGRESS_COUNT, SafeRef, StrictModel
from adhelm.providers._semantic_state import (
    SemanticHierarchySpec,
    collection_state,
    keyed_fingerprint,
    member_fingerprint,
)
from adhelm.providers.meta_compiler import (
    MetaAdsCampaignBrief,
    MetaAdsCompiledCampaignSpec,
)
from adhelm.safety import (
    AccountIdentity,
    CompositePlan,
    EgressTarget,
    FieldChange,
    Money,
    OperationKind,
    StateRef,
    SubPlan,
)

#: The retained ledger record kind this contract's plan is stored under.  It is
#: additive in ``changes`` and mirrors ``META_CAMPAIGN_PAUSE_PLAN``.
META_CREATE_PLAN_RECORD_KIND: Final = "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN"

META_CREATE_PLAN_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
#: The documented Graph host and pinned version, as recorded 2026-09-09; see the
#: wire-fact provenance in ``docs/implementation/meta-create-execution.md`` for
#: what was and was not re-verified.
META_CREATE_HOST: Final[Literal["graph.facebook.com"]] = "graph.facebook.com"
META_CREATE_API_VERSION: Final[Literal["v26.0"]] = "v26.0"
META_CREATE_CAPABILITY_VERSION: Final = (
    "meta-graph-v26-single-image-hierarchy-create-contract-2026-09-09"
)
META_CREATE_EGRESS_PURPOSE: Final = "ordered Meta Ads v26 paused single-image hierarchy create"
#: The one provider permission this route's EDIT profile declares.
META_CREATE_SCOPE: Final[Literal["ads_management"]] = "ads_management"

#: The five ordered child steps.  ``upload-image`` is first because an ad
#: creative cannot be created without the ``hash`` it returns, and the campaign
#: precedes its ad set, which precedes the ad that binds the creative.
META_CREATE_CHILD_LABELS: Final = (
    "upload-image",
    "create-campaign",
    "create-ad-set",
    "create-creative",
    "create-ad",
)

#: The image media types this build will send.  Meta's Ad Image reference
#: documents no media-type allowlist for the multipart upload, so the bound is
#: deliberately local and narrow rather than inferred.
META_CREATE_MEDIA_TYPES: Final = frozenset({"image/png", "image/jpeg"})
#: The one multipart form-field name this build sends for the binary image.
#: Meta's own reference documents a base64 ``bytes`` field; the long-documented
#: binary form is ``-F 'filename=@<path>'``, and that is what the shared
#: ``SafeHttpClient.upload_image`` seam can express without a new transport
#: capability.  The choice is recorded as UNVERIFIED in the execution contract.
META_CREATE_UPLOAD_FIELD: Final = "filename"
#: The local ceiling on the bytes this route will hand to the upload child.  It
#: is the shared transport's own bound; Meta documents no maximum here.
META_CREATE_MAX_UPLOAD_BYTES: Final = 5 * 1024 * 1024

#: One campaign + one ad set + one creative + one ad.  The compiler emits
#: exactly one of each, and the kernel's ``MAX_CHILD_EGRESS_COUNT`` (32) bounds
#: any later shape.
META_CREATE_MAX_HIERARCHY_MEMBERS: Final = MAX_CHILD_EGRESS_COUNT - 1
META_CREATE_HIERARCHY_MEMBER_COUNT: Final = 4
META_CREATE_CHILD_EGRESS_COUNT: Final = 5

#: The five documented account-scoped create edges, in child order.
META_CREATE_EDGES: Final = ("/adimages", "/campaigns", "/adsets", "/adcreatives", "/ads")

_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
#: Meta native object identifiers are decimal strings without a leading zero.
_NATIVE_IDENTIFIER: Final = re.compile(r"^[1-9][0-9]{0,39}$", re.ASCII)
#: ``act_`` prefixed ad-account references, as ``providers/meta_ads.py`` returns.
_ACCOUNT_IDENTIFIER: Final = re.compile(r"^act_[1-9][0-9]{0,39}$", re.ASCII)
#: The ad-image hash the upload acknowledgement and every readback may carry.
#: Meta documents ``hash`` as an opaque string and no grammar.  Every documented
#: example is 32 lowercase hexadecimal characters, so this route accepts exactly
#: that and nothing else (``UNVERIFIED``, a local narrowing).  A closed grammar
#: is what lets planning reserve the creative form's exact length: a longer or
#: escaped hash would otherwise grow ``object_story_spec`` past the form-value
#: bound only after the upload, when the creative's request can no longer be
#: refused cleanly.
META_CREATE_IMAGE_HASH_LENGTH: Final = 32
_IMAGE_HASH: Final = re.compile(r"^[a-f0-9]{32}$", re.ASCII)
#: The stand-in planning sizes the creative form with.  It is a member of the
#: accepted hash grammar, so no acknowledgement can make the form longer.
META_CREATE_PLANNING_IMAGE_HASH: Final = "0" * META_CREATE_IMAGE_HASH_LENGTH
_PRINCIPAL_REF: Final = re.compile(r"^meta_token_principal_v1\.[a-f0-9]{64}$", re.ASCII)
_CREDENTIAL_BINDING_REF: Final = re.compile(r"^credential-binding:sha256:[a-f0-9]{64}$", re.ASCII)
_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 4_194_304
#: Every name this route puts in a fixed Graph form body must survive the shared
#: transport's printable-ASCII form bound, so a name that cannot be expressed
#: there is refused at planning rather than at egress.
_FORM_SAFE: Final = re.compile(r"^[\x20-\x7e]{1,512}$", re.ASCII)
#: The local ceiling on one value in a fixed Graph form body.  A nested object
#: is sent as ``ensure_ascii`` JSON, so non-ASCII copy expands to six characters
#: per code unit and a long enough headline and message can exceed it.
META_CREATE_MAX_FORM_VALUE_LENGTH: Final = 4_096

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

# -- the semantic projection -------------------------------------------------

_FINGERPRINT_CONTEXT: Final = b"adhelm/meta-ads/single-image-hierarchy-create/semantic/v1\x00"
_STATE_KIND: Final = "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_SEMANTIC_HIERARCHY_V1"
_SEMANTIC_PROJECTION: Final = "NAME_HMAC_CONFIGURATION_EXACT_V1"
_SEMANTIC_SPEC: Final = SemanticHierarchySpec(
    context=_FINGERPRINT_CONTEXT,
    state_kind=_STATE_KIND,
    semantic_projection=_SEMANTIC_PROJECTION,
    target_purpose="meta-ads-single-image-hierarchy-create-semantic-target",
    member_purpose="meta-ads-single-image-hierarchy-create-semantic-member",
    collection_purpose="meta-ads-single-image-hierarchy-create-semantic-collection",
)
_IDENTITY_PURPOSE: Final = "meta-ads-single-image-hierarchy-create-semantic-identity"
_KEY_MARKER_PURPOSE: Final = "meta-ads-single-image-hierarchy-create-fingerprint-key-marker"
#: ``name`` is a reserved semantic key, so the projection names the
#: request-controllable identity role instead.
_IDENTITY_FIELD: Final = "semantic_identity"

#: The sentinel an observation uses when the created ad creative does not
#: reference the exact image this attempt uploaded, or the account does not
#: prove it holds that image.  It can never equal a content digest, so a
#: hierarchy whose creative association is unproven cannot match the prediction.
IMAGE_ASSOCIATION_NOT_PROVEN: Final = "IMAGE_ASSOCIATION_NOT_PROVEN"
#: The value a creative member carries for every field this route never reads
#: back.  The creative node read is ``id``, ``name`` and ``image_hash`` only: the
#: call to action, format, headline, primary text, destination and Page live in
#: ``object_story_spec``, which the readback deliberately never requests because
#: it would pull ad copy and a Page identifier into this process.  Predicting a
#: literal for them and "observing" the same literal would assert configuration
#: nobody read, so both sides carry this sentinel and a creative match proves the
#: name, the image association and the creative association, nothing more.
NOT_READ_BACK_COPY_BEARING_FIELD: Final = "NOT_READ_BACK_COPY_BEARING_FIELD"
#: The same idea for the ad -> creative edge.
CREATIVE_ASSOCIATION_NOT_PROVEN: Final = "CREATIVE_ASSOCIATION_NOT_PROVEN"
#: The value an ad member carries when it *does* reference this attempt's own
#: creative.  It is a same-attempt association, not a provider guarantee.
CREATIVE_ASSOCIATION_PROVEN: Final = "PROVEN_SAME_ATTEMPT_CREATIVE"


def _invalid_plan(message: str = "Meta Ads CREATE plan data is invalid.") -> AdhelmError:
    return AdhelmError("INVALID_META_ADS_CREATE_PLAN", message, exit_code=ExitCode.INVALID_INPUT)


class MetaAdsHierarchyMember(StrictModel):
    """One request-controllable member of a paused single-image hierarchy.

    ``identity`` is the ordered path of configured names that uniquely names the
    member to a *request*: the campaign name, then the ad-set name, then the
    creative or ad name.  ``configuration`` is the remaining request-controllable
    values.  Neither may carry a provider-assigned identifier;
    ``_semantic_state`` rejects the common shapes and this projection keeps
    identifiers out by hashing the identity separately.
    """

    member_kind: Literal["CAMPAIGN", "AD_SET", "CREATIVE", "AD"]
    identity: tuple[str, ...] = Field(min_length=1, max_length=4)
    configuration: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded_projection_member(self) -> MetaAdsHierarchyMember:
        keys = tuple(key for key, _value in self.configuration)
        if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
            raise ValueError("member configuration keys must be unique and sorted")
        if any(not key or len(key) > 64 for key in keys):
            raise ValueError("member configuration keys are bounded")
        if any(not value or len(value) > 512 for _key, value in self.configuration):
            raise ValueError("member configuration values are bounded")
        if any(not item or len(item) > 1_000 for item in self.identity):
            raise ValueError("member identity components are bounded")
        expected = {"CAMPAIGN": 2, "AD_SET": 3, "CREATIVE": 4, "AD": 4}[self.member_kind]
        if len(self.identity) != expected:
            raise ValueError("member identity must be its exact configured name path")
        return self


class MetaAdsSingleImageHierarchyProjection(StrictModel):
    """The exact member multiset one paused single-image hierarchy projects to.

    Both the compiled candidate and the post-create readback are reduced to this
    one shape, so a predicted and an observed fingerprint are comparable by
    construction rather than by two hand-written hashers agreeing.
    """

    schema_version: Literal[1] = 1
    projection_kind: Literal["META_ADS_SINGLE_IMAGE_LINK_HIERARCHY"] = (
        "META_ADS_SINGLE_IMAGE_LINK_HIERARCHY"
    )
    campaign_name: str = Field(min_length=1, max_length=1_000)
    members: tuple[MetaAdsHierarchyMember, ...] = Field(
        min_length=META_CREATE_HIERARCHY_MEMBER_COUNT,
        max_length=META_CREATE_HIERARCHY_MEMBER_COUNT,
    )

    @model_validator(mode="after")
    def every_pausable_member_is_paused(self) -> MetaAdsSingleImageHierarchyProjection:
        for member in self.members:
            configuration = dict(member.configuration)
            if member.member_kind == "CREATIVE":
                # A Meta ad creative has no create-time configured status.  The
                # projection says so rather than inventing a paused claim.
                if configuration.get("configured_status") != "NOT_A_PAUSABLE_META_ENTITY":
                    raise ValueError("a Meta ad creative is not a pausable entity")
            elif configuration.get("configured_status") != "PAUSED":
                raise ValueError("every created pausable Meta entity must be paused")
            if member.identity[0] != self.campaign_name:
                raise ValueError("every member belongs to this projection's campaign")
        kinds = sorted(member.member_kind for member in self.members)
        if kinds != ["AD", "AD_SET", "CAMPAIGN", "CREATIVE"]:
            raise ValueError("a single-image hierarchy is one campaign, ad set, creative and ad")
        return self


def _identity_fingerprint(components: tuple[str, ...], *, fingerprint_key: bytes) -> str:
    return keyed_fingerprint(
        fingerprint_key,
        context=_FINGERPRINT_CONTEXT,
        purpose=_IDENTITY_PURPOSE,
        value={"schema_version": 1, "components": list(components)},
    )


def fingerprint_key_marker(fingerprint_key: bytes) -> str:
    """Return the non-reversible marker that binds a state to one local key."""

    return keyed_fingerprint(
        fingerprint_key,
        context=_FINGERPRINT_CONTEXT,
        purpose=_KEY_MARKER_PURPOSE,
        value={"schema_version": 1, "state_kind": _STATE_KIND},
    )


def semantic_target_fingerprint(
    *, account_client_ref: str, campaign_name: str, fingerprint_key: bytes
) -> str:
    """Bind one projection to the exact ad account and campaign name it describes."""

    if (
        type(account_client_ref) is not str
        or _ACCOUNT_IDENTIFIER.fullmatch(account_client_ref) is None
    ):
        raise ValueError("semantic target requires the exact act_-prefixed ad-account ID")
    return keyed_fingerprint(
        fingerprint_key,
        context=_FINGERPRINT_CONTEXT,
        purpose=_SEMANTIC_SPEC.target_purpose,
        value={
            "schema_version": 1,
            "state_kind": _STATE_KIND,
            "provider": "meta_ads",
            "provider_api_version": META_CREATE_API_VERSION,
            "ad_account_client_ref": account_client_ref,
            "campaign_name_fingerprint": _identity_fingerprint(
                ("campaign_name", campaign_name), fingerprint_key=fingerprint_key
            ),
        },
    )


def _member_fingerprint(member: MetaAdsHierarchyMember, *, fingerprint_key: bytes) -> str:
    return member_fingerprint(
        _SEMANTIC_SPEC,
        identity_field=_IDENTITY_FIELD,
        identity_fingerprint=_identity_fingerprint(
            (member.member_kind, *member.identity), fingerprint_key=fingerprint_key
        ),
        configuration={
            "member_kind": member.member_kind,
            **{key: value for key, value in member.configuration},
        },
        fingerprint_key=fingerprint_key,
    )


def semantic_state_from_members(
    members: tuple[MetaAdsHierarchyMember, ...],
    *,
    account_client_ref: str,
    campaign_name: str,
    fingerprint_key: bytes,
) -> StateRef:
    """Project any observed member multiset into one comparable state.

    A *partial* hierarchy is a real outcome of an ordered non-atomic create, so
    the state projection deliberately does not require a complete hierarchy.
    Completeness is the strict :class:`MetaAdsSingleImageHierarchyProjection`'s
    job, and equality with the approved ``after`` is the adapter's.
    """

    if type(members) is not tuple or any(
        type(item) is not MetaAdsHierarchyMember for item in members
    ):
        raise ValueError("semantic members")
    if any(item.identity[0] != campaign_name for item in members):
        raise ValueError("semantic members name a different campaign")
    return collection_state(
        _SEMANTIC_SPEC,
        binding_fingerprint=semantic_target_fingerprint(
            account_client_ref=account_client_ref,
            campaign_name=campaign_name,
            fingerprint_key=fingerprint_key,
        ),
        fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
        member_fingerprints=tuple(
            sorted(_member_fingerprint(item, fingerprint_key=fingerprint_key) for item in members)
        ),
        fingerprint_key=fingerprint_key,
    )


def semantic_hierarchy_state(
    projection: MetaAdsSingleImageHierarchyProjection | None,
    *,
    account_client_ref: str,
    campaign_name: str,
    fingerprint_key: bytes,
) -> StateRef:
    """Project one complete hierarchy -- or its absence -- into a state.

    ``None`` is the deliberate ``before`` of a CREATE: the empty member set under
    this exact ``(ad account, campaign name)`` binding, which is the local
    statement "no campaign with this name in this ad account".
    """

    members: tuple[MetaAdsHierarchyMember, ...] = ()
    if projection is not None:
        if type(projection) is not MetaAdsSingleImageHierarchyProjection:
            raise ValueError("semantic projection type")
        if projection.campaign_name != campaign_name:
            raise ValueError("semantic projection names a different campaign")
        members = projection.members
    return semantic_state_from_members(
        members,
        account_client_ref=account_client_ref,
        campaign_name=campaign_name,
        fingerprint_key=fingerprint_key,
    )


def epoch_seconds(value: datetime) -> str:
    """Render one aware instant as the timezone-independent value a plan compares.

    Graph echoes a schedule back in the *account's* timezone offset, so the only
    stable comparison between a sent and a read-back schedule is the instant
    itself.  The projection therefore carries epoch seconds, never a rendered
    local timestamp.
    """

    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule instant must be timezone-aware")
    return str(int(value.astimezone(UTC).timestamp()))


def creative_member_configuration(
    creative_asset_content_digest: str,
) -> tuple[tuple[str, str], ...]:
    """Return the one creative configuration both a prediction and a readback use.

    Only ``creative_asset_content_digest`` varies: a prediction carries the
    digest this attempt will upload, and an observation carries it only when the
    image association was proven (otherwise :data:`IMAGE_ASSOCIATION_NOT_PROVEN`).
    Every copy-bearing field is the explicit not-read sentinel on both sides.
    """

    return (
        ("call_to_action", NOT_READ_BACK_COPY_BEARING_FIELD),
        ("configured_status", "NOT_A_PAUSABLE_META_ENTITY"),
        ("creative_asset_content_digest", creative_asset_content_digest),
        ("creative_format", NOT_READ_BACK_COPY_BEARING_FIELD),
        ("destination_declared", NOT_READ_BACK_COPY_BEARING_FIELD),
        ("headline_declared", NOT_READ_BACK_COPY_BEARING_FIELD),
        ("page_declared", NOT_READ_BACK_COPY_BEARING_FIELD),
        ("primary_text_declared", NOT_READ_BACK_COPY_BEARING_FIELD),
    )


def projection_from_compiled_spec(
    spec: MetaAdsCompiledCampaignSpec, *, creative_asset_content_digest: str
) -> MetaAdsSingleImageHierarchyProjection:
    """Reduce one compiled candidate to its exact predicted member multiset.

    ``creative_asset_content_digest`` is the local vault asset this attempt will
    upload.  The compiled candidate's own ``image_hash`` is customer-supplied
    local evidence that this route never sends, because the ad creative must
    reference the image this attempt's own upload minted.

    No ad copy, destination URL, Facebook Page identifier, call to action or
    image hash enters the projection.  None of them is read back, so the
    creative member records them as :data:`NOT_READ_BACK_COPY_BEARING_FIELD`
    rather than as a predicted literal an observation could only echo.
    """

    if type(spec) is not MetaAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    if type(creative_asset_content_digest) is not str or (
        _DIGEST.fullmatch(creative_asset_content_digest) is None
    ):
        raise ValueError("creative asset content digest")
    campaign = spec.campaign_request
    adset = spec.adset_request
    creative = spec.creative_request
    name = campaign.name
    members = (
        MetaAdsHierarchyMember(
            member_kind="CAMPAIGN",
            identity=(name, name),
            configuration=(
                ("buying_type", campaign.buying_type),
                ("configured_status", campaign.status),
                ("lifetime_budget_minor_units", str(campaign.lifetime_budget)),
                ("objective", campaign.objective),
                ("special_ad_categories", "NONE_DECLARED_EMPTY"),
                ("start_time_epoch_seconds", epoch_seconds(campaign.start_time)),
                ("stop_time_epoch_seconds", epoch_seconds(campaign.stop_time)),
            ),
        ),
        MetaAdsHierarchyMember(
            member_kind="AD_SET",
            identity=(name, adset.name, adset.name),
            configuration=(
                ("bid_strategy", adset.bid_strategy),
                ("billing_event", adset.billing_event),
                ("configured_status", adset.status),
                ("facebook_positions", ",".join(adset.targeting.facebook_positions)),
                ("geo_countries", ",".join(adset.targeting.geo_locations.countries)),
                ("optimization_goal", adset.optimization_goal),
                ("publisher_platforms", ",".join(adset.targeting.publisher_platforms)),
            ),
        ),
        MetaAdsHierarchyMember(
            member_kind="CREATIVE",
            identity=(name, adset.name, creative.name, creative.name),
            configuration=creative_member_configuration(creative_asset_content_digest),
        ),
        MetaAdsHierarchyMember(
            member_kind="AD",
            identity=(name, adset.name, spec.ad_request.name, spec.ad_request.name),
            configuration=(
                ("configured_status", spec.ad_request.status),
                ("creative_association", CREATIVE_ASSOCIATION_PROVEN),
            ),
        ),
    )
    return MetaAdsSingleImageHierarchyProjection(campaign_name=name, members=members)


# -- the ordered child plan --------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetaAdsCreateChild:
    """One journaled child request of the single approved CREATE operation."""

    index: int
    label: str
    kind: Literal[
        "UPLOAD_IMAGE", "CREATE_CAMPAIGN", "CREATE_AD_SET", "CREATE_CREATIVE", "CREATE_AD"
    ]
    edge: str


_CHILD_PLAN: Final = (
    MetaAdsCreateChild(index=1, label="upload-image", kind="UPLOAD_IMAGE", edge="/adimages"),
    MetaAdsCreateChild(index=2, label="create-campaign", kind="CREATE_CAMPAIGN", edge="/campaigns"),
    MetaAdsCreateChild(index=3, label="create-ad-set", kind="CREATE_AD_SET", edge="/adsets"),
    MetaAdsCreateChild(
        index=4, label="create-creative", kind="CREATE_CREATIVE", edge="/adcreatives"
    ),
    MetaAdsCreateChild(index=5, label="create-ad", kind="CREATE_AD", edge="/ads"),
)


def create_child_plan(spec: MetaAdsCompiledCampaignSpec) -> tuple[MetaAdsCreateChild, ...]:
    """Return the exact ordered child requests one compiled candidate needs.

    The order is a dependency order, not a preference: the creative's
    ``image_hash`` comes from the upload, the ad set's ``campaign_id`` from the
    campaign, and the ad's ``adset_id`` and ``creative`` from its ad set and its
    creative.  The compiler emits exactly one of each entity, so the plan is
    fixed at five; a later shape would still be bounded by the kernel.
    """

    if type(spec) is not MetaAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    return _CHILD_PLAN


def expected_child_egress_count(spec: MetaAdsCompiledCampaignSpec) -> int:
    """Return ``5``, refusing a hierarchy above the kernel's bound."""

    children = create_child_plan(spec)
    if len(children) > MAX_CHILD_EGRESS_COUNT:
        raise AdhelmError(
            "META_ADS_CREATE_HIERARCHY_TOO_LARGE",
            "This compiled hierarchy needs more journaled child requests than one approved "
            "operation may issue.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a candidate that needs at most "
                f"{MAX_CHILD_EGRESS_COUNT} journaled requests, or plan the hierarchy as "
                "separate approved operations; nothing was read from the provider."
            ),
        )
    return len(children)


# -- the fresh bounded read --------------------------------------------------


class MetaAdsCreatePreconditions(StrictModel):
    """The bounded promotion checks this workspace actually performed.

    Several of the compiler contract's promotion prerequisites are recorded as
    explicit statuses rather than values, because the documented Graph surface
    exposes nothing to read them from before the entities exist.  Saying so is
    the point: the plan refuses to imply a check it did not make.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_V26_SINGLE_IMAGE_CREATE_PRECONDITIONS"] = (
        "META_ADS_V26_SINGLE_IMAGE_CREATE_PRECONDITIONS"
    )
    campaign_name_collision: Literal["NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"] = (
        "NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"
    )
    observed_campaign_name_matches: Literal[0] = 0
    #: The bounded near-variant refusal P04-GA introduced.  One local rule
    #: governs both sides of every name comparison this route makes: **no
    #: normalization at all**.  Graph does not document whether it compares
    #: campaign names case-sensitively, so a near match is refused rather than
    #: assumed distinct.  Meta returns the account's campaign names directly, so
    #: unlike Google this build compares locally instead of probing.
    campaign_name_near_match: Literal["NO_NEAR_MATCH_CAMPAIGN_NAME_OBSERVED"] = (
        "NO_NEAR_MATCH_CAMPAIGN_NAME_OBSERVED"
    )
    observed_near_match_names: Literal[0] = 0
    observed_account_campaign_names: int = Field(ge=0, le=5_000)
    name_comparison: Literal["BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE"] = (
        "BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE"
    )
    provider_name_comparison_semantics: Literal["UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER"] = (
        "UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER"
    )
    #: Graph's ``account_status`` is an integer; ``1`` is the documented ACTIVE
    #: value the shipped read contract already records.
    account_status: Literal["ACTIVE_ACCOUNT_STATUS_1"] = "ACTIVE_ACCOUNT_STATUS_1"
    #: ``OUTCOME_TRAFFIC`` with ``LINK_CLICKS`` optimization is the only compiled
    #: option this build promotes.  Nothing readable proves account entitlement
    #: for another objective, so an unreviewed one is refused outright.
    objective_optimization_gate: Literal["OUTCOME_TRAFFIC_LINK_CLICKS_ONLY_NO_ENTITLEMENT_READ"] = (
        "OUTCOME_TRAFFIC_LINK_CLICKS_ONLY_NO_ENTITLEMENT_READ"
    )
    #: Placement, Page and creative-transformation eligibility are explicit
    #: readback requirements in the compiler contract and remain unreadable
    #: before the entities exist.
    placement_eligibility: Literal["NOT_READABLE_BEFORE_CREATE_REQUIRES_POST_CREATE_READBACK"] = (
        "NOT_READABLE_BEFORE_CREATE_REQUIRES_POST_CREATE_READBACK"
    )
    page_eligibility: Literal["NOT_READABLE_NO_REVIEWED_PAGE_SURFACE_IN_THIS_BUILD"] = (
        "NOT_READABLE_NO_REVIEWED_PAGE_SURFACE_IN_THIS_BUILD"
    )
    #: Meta *does* document an image readback, unlike OpenAI's file surface.
    uploaded_image_readback: Literal["AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"] = (
        "AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"
    )
    image_association_proof: Literal["ACCOUNT_IMAGE_HASH_AND_CREATIVE_READBACK"] = (
        "ACCOUNT_IMAGE_HASH_AND_CREATIVE_READBACK"
    )
    provider_image_bytes: Literal["PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ"] = (
        "PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ"
    )
    creative_source: Literal["PRIVATE_LOCAL_VAULT_ONLY"] = "PRIVATE_LOCAL_VAULT_ONLY"
    compiled_image_reference_use: Literal["LOCAL_EVIDENCE_NOT_SENT"] = "LOCAL_EVIDENCE_NOT_SENT"
    #: No idempotency key, request identifier, or settlement resource is
    #: documented for a Graph write.
    idempotency_contract: Literal["NOT_DOCUMENTED_NO_SAFE_WRITE_RETRY"] = (
        "NOT_DOCUMENTED_NO_SAFE_WRITE_RETRY"
    )

    @field_validator("observed_campaign_name_matches", "observed_near_match_names", mode="before")
    @classmethod
    def exact_zero(cls, value: object) -> int:
        if type(value) is not int or type(value) is bool or value != 0:
            raise ValueError("a create plan requires zero observed name collisions")
        return value


class MetaAdsCreativeVaultBinding(StrictModel):
    """The one private-vault image this attempt uploads, bound by content.

    The vault is content-addressed, so its object path is exactly
    ``<workspace>/assets/sha256/<content digest>``.  This binding therefore names
    the digest, not a caller-supplied filesystem path: accepting a path would let
    a request read a file the vault does not hold, which is the boundary
    ``docs/implementation/creative-asset-vault.md`` exists to keep.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_V26_PRIVATE_VAULT_CREATIVE"] = (
        "META_ADS_V26_PRIVATE_VAULT_CREATIVE"
    )
    vault_ref: str = Field(pattern=r"^vault:sha256:[0-9a-f]{64}$")
    content_digest: Digest
    media_type: Literal["image/png", "image/jpeg"]
    byte_length: int = Field(ge=1, le=META_CREATE_MAX_UPLOAD_BYTES)
    width_px: int = Field(ge=1, le=65_535)
    height_px: int = Field(ge=1, le=65_535)
    upload_field_name: Literal["filename"] = META_CREATE_UPLOAD_FIELD
    upload_part_filename: Literal["creative.png", "creative.jpg"]

    @model_validator(mode="after")
    def exact_vault_reference(self) -> MetaAdsCreativeVaultBinding:
        if self.vault_ref != f"vault:sha256:{self.content_digest}":
            raise ValueError("the vault reference does not name its own content digest")
        expected = "creative.png" if self.media_type == "image/png" else "creative.jpg"
        if self.upload_part_filename != expected:
            raise ValueError("the neutral upload part filename must match the declared media type")
        return self


class MetaAdsCreateAccountBinding(StrictModel):
    """The observed ad-account baseline this plan was compiled against.

    ``account_status`` is retained exactly as the canonical tagged Graph integer
    representation the shipped pause contract already uses; it is not mapped to
    an invented eligibility state.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_GRAPH_V26_CREATE_ACCOUNT_BASELINE"] = (
        "META_GRAPH_V26_CREATE_ACCOUNT_BASELINE"
    )
    account_ref: SafeRef
    account_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: Literal["USD"]
    account_status: Literal["1"] = "1"
    provider_api_version: Literal["v26.0"] = META_CREATE_API_VERSION

    @field_validator("account_client_ref")
    @classmethod
    def native_account_identifier(cls, value: str) -> str:
        if _ACCOUNT_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("ad-account reference must be act_<native decimal ID>")
        return value

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("account timezone must be an IANA timezone") from exc
        return value


class MetaAdsCreateExecutionBinding(StrictModel):
    """Closed nonsecret EDIT facts needed to reconstruct ``AccountIdentity``.

    These are the Meta EDIT session's *two credential slots* -- the subject token
    and the inspector token -- reduced to their nonsecret inspection results.  It
    proves only a local, inspected token/profile association.  It does not prove
    current campaign access or provider permission to create anything.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_EDIT_LOCAL_TOKEN_ASSOCIATION"] = (
        "META_ADS_EDIT_LOCAL_TOKEN_ASSOCIATION"
    )
    profile_ref: str = Field(min_length=1, max_length=160)
    client_application_ref: str = Field(min_length=1, max_length=64)
    principal_ref: str = Field(min_length=1, max_length=256)
    required_scope: Literal["ads_management"] = META_CREATE_SCOPE
    credential_binding_ref: str = Field(min_length=1, max_length=128)
    configuration_digest: Digest
    profile_configuration_digest: Digest
    inspected_at: datetime
    expires_at: datetime
    data_access_expires_at: datetime
    local_association_status: Literal["LOCAL_INSPECTED_TOKEN_ASSOCIATION_ONLY"] = (
        "LOCAL_INSPECTED_TOKEN_ASSOCIATION_ONLY"
    )
    provider_write_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"

    @field_validator("client_application_ref")
    @classmethod
    def native_application_identifier(cls, value: str) -> str:
        if _NATIVE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("client application identifier is invalid")
        return value

    @field_validator("principal_ref")
    @classmethod
    def derived_principal_identifier(cls, value: str) -> str:
        if _PRINCIPAL_REF.fullmatch(value) is None:
            raise ValueError("principal reference is invalid")
        return value

    @field_validator("credential_binding_ref")
    @classmethod
    def credential_binding_identifier(cls, value: str) -> str:
        if _CREDENTIAL_BINDING_REF.fullmatch(value) is None:
            raise ValueError("credential binding reference is invalid")
        return value

    @field_validator("inspected_at", "expires_at", "data_access_expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("inspection times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def finite_inspection_window(self) -> MetaAdsCreateExecutionBinding:
        if self.inspected_at >= self.expires_at or (
            self.inspected_at >= self.data_access_expires_at
        ):
            raise ValueError("both inspected token windows must remain in the future")
        return self


class MetaAdsSingleImageHierarchyCreateSelection(StrictModel):
    """The additive ``plan execution create`` envelope member P04-MA introduces.

    The generic request carries a profile, provider, operation, account and one
    resource reference.  A Meta Ads single-image CREATE needs three more things:
    the retained candidate this plan promotes, the *brief that candidate was
    compiled from*, and the private-vault image the upload child sends.  The
    brief is required because a retained ``CANDIDATE_COMPILED`` record
    deliberately keeps only the redacted review, so the planner recompiles the
    supplied brief and refuses unless the recompiled candidate digest equals the
    retained one.
    """

    schema_version: Literal[1] = 1
    selection_kind: Literal["META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE"] = (
        "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE"
    )
    candidate_record_digest: Digest
    fresh: MetaAdsCampaignBrief
    creative_vault_ref: str = Field(pattern=r"^vault:sha256:[0-9a-f]{64}$")
    expires_in_seconds: int = Field(default=300, ge=60, le=900)


class MetaAdsCreateCandidateBinding(StrictModel):
    """The exact retained offline candidate this plan promotes."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_V26_RETAINED_SINGLE_IMAGE_CANDIDATE"] = (
        "META_ADS_V26_RETAINED_SINGLE_IMAGE_CANDIDATE"
    )
    candidate_record_digest: Digest
    candidate_digest: Digest
    spec_digest: Digest
    input_digest: Digest
    specification: MetaAdsCompiledCampaignSpec

    @model_validator(mode="after")
    def exact_recompiled_candidate(self) -> MetaAdsCreateCandidateBinding:
        if self.spec_digest != self.specification.spec_digest:
            raise ValueError("candidate binding does not describe its own specification")
        if self.input_digest != self.specification.input_digest:
            raise ValueError("candidate binding restates a different compiler input")
        if self.specification.planning_boundary.status != "OFFLINE_CANDIDATE_NOT_EXECUTABLE":
            raise ValueError("only an offline candidate can be promoted")
        if (
            self.specification.campaign_request.objective != "OUTCOME_TRAFFIC"
            or self.specification.adset_request.optimization_goal != "LINK_CLICKS"
        ):
            # The compiled payload types already exclude everything else.
            # Restating the enabled set here keeps a constructed or altered
            # candidate from carrying a feature-gated option into a plan.
            raise ValueError("only the reviewed traffic/link-clicks option is enabled")
        return self


class MetaAdsCreateObservationBinding(StrictModel):
    """One bounded sequential account observation interval and its checks."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_V26_SINGLE_IMAGE_CREATE_OBSERVATION"] = (
        "META_ADS_V26_SINGLE_IMAGE_CREATE_OBSERVATION"
    )
    campaign_name: str = Field(min_length=1, max_length=1_000)
    preconditions: MetaAdsCreatePreconditions
    creative: MetaAdsCreativeVaultBinding
    fingerprint_key_marker: Digest
    read_started_at: datetime
    read_finished_at: datetime
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_interval(self) -> MetaAdsCreateObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("the create observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("the create observation interval exceeds the local bound")
        return self


class MetaAdsCreatePostcondition(StrictModel):
    """Closed configuration-only postcondition with no delivery prediction."""

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    campaign_name: str = Field(min_length=1, max_length=1_000)
    configured_status: Literal["PAUSED"] = "PAUSED"
    every_pausable_entity_configured_status: Literal["PAUSED"] = "PAUSED"
    creative_pausable: Literal["NOT_A_PAUSABLE_META_ENTITY"] = "NOT_A_PAUSABLE_META_ENTITY"
    member_count: Literal[4] = META_CREATE_HIERARCHY_MEMBER_COUNT
    child_egress_count: Literal[5] = META_CREATE_CHILD_EGRESS_COUNT
    predicted_hierarchy_digest: Digest
    #: The campaign owns the lifetime cap; the compiled ad set deliberately has
    #: no budget or schedule field, so there is no daily amount to disclose.
    spend_bound_source: Literal["CAMPAIGN_LIFETIME_BUDGET_MINOR_UNITS"] = (
        "CAMPAIGN_LIFETIME_BUDGET_MINOR_UNITS"
    )
    daily_budget_declared: Literal[False] = False
    image_association: Literal["PROVEN_ONLY_BY_ACCOUNT_IMAGE_AND_CREATIVE_READBACK"] = (
        "PROVEN_ONLY_BY_ACCOUNT_IMAGE_AND_CREATIVE_READBACK"
    )
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    activation: Literal["SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"] = (
        "SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"
    )

    @field_validator("daily_budget_declared", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("this route declares no daily budget")
        return value


class MetaAdsCreateRedactedReview(StrictModel):
    """The exact review the approval host renders for this operation.

    Every field is derived: the compiler's own redacted review supplies the
    handles and the spend bound, and this contract supplies the statements a
    reviewer needs and the compiler cannot make -- that the operation is five
    ordered requests, that every pausable entity is created paused, how the
    image association is proven, and that no ambiguous child is ever retried.
    """

    account_ref: SafeRef
    campaign_handle: str = Field(min_length=1, max_length=80)
    adset_handle: str = Field(min_length=1, max_length=80)
    creative_handle: str = Field(min_length=1, max_length=80)
    ad_handle: str = Field(min_length=1, max_length=80)
    initial_state: Literal["PAUSED_AT_EVERY_PAUSABLE_LEVEL"] = "PAUSED_AT_EVERY_PAUSABLE_LEVEL"
    objective: Literal["OUTCOME_TRAFFIC"] = "OUTCOME_TRAFFIC"
    requested_placement: Literal["FACEBOOK_FEED"] = "FACEBOOK_FEED"
    country_count: Literal[1] = 1
    child_request_count: Literal[5] = META_CREATE_CHILD_EGRESS_COUNT
    ordered_child_steps: tuple[str, ...] = Field(min_length=5, max_length=5)
    maximum_spend: Money
    spend_bound_source: Literal["CAMPAIGN_LIFETIME_BUDGET_MINOR_UNITS"] = (
        "CAMPAIGN_LIFETIME_BUDGET_MINOR_UNITS"
    )
    activation_required: Literal[True] = True
    uploaded_image_readback: Literal["AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"] = (
        "AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"
    )
    image_association_proof: Literal["ACCOUNT_IMAGE_HASH_AND_CREATIVE_READBACK"] = (
        "ACCOUNT_IMAGE_HASH_AND_CREATIVE_READBACK"
    )
    provider_image_bytes: Literal["PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ"] = (
        "PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ"
    )
    placement_eligibility_status: Literal["REQUIRES_POST_CREATE_READBACK"] = (
        "REQUIRES_POST_CREATE_READBACK"
    )
    partial_hierarchy_recovery: Literal["RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"] = (
        "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    )
    write_retry: Literal["NEVER_NO_DOCUMENTED_IDEMPOTENCY"] = "NEVER_NO_DOCUMENTED_IDEMPOTENCY"
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = (
        "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    )


def create_redacted_review(
    spec: MetaAdsCompiledCampaignSpec, children: tuple[MetaAdsCreateChild, ...]
) -> MetaAdsCreateRedactedReview:
    """Derive the review lines' source from the compiler's own redacted review."""

    review = spec.redacted_review
    return MetaAdsCreateRedactedReview(
        account_ref=review.account_ref,
        campaign_handle=review.campaign_handle,
        adset_handle=review.adset_handle,
        creative_handle=review.creative_handle,
        ad_handle=review.ad_handle,
        ordered_child_steps=tuple(child.label for child in children),
        maximum_spend=Money(
            minor_units=review.maximum_spend.minor_units,
            currency=review.maximum_spend.currency,
        ),
    )


def form_safe_request_values(spec: MetaAdsCompiledCampaignSpec) -> tuple[str, ...]:
    """Return the compiled scalar values this route must put in a Graph form.

    The shared transport's fixed mutation form accepts printable ASCII only.  A
    compiled name that cannot be expressed there would fail *after* a permit was
    consumed, so the planner checks it before any provider work instead.
    """

    if type(spec) is not MetaAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    return (
        spec.campaign_request.name,
        spec.adset_request.name,
        spec.creative_request.name,
        spec.ad_request.name,
    )


def form_value_expressible(value: object) -> bool:
    """Report whether one value fits this route's fixed Graph form body.

    The single predicate the planner, the EDIT capability and the session's
    private POST all use, so the three can never disagree about what egress
    will accept after a permit has already been consumed.
    """

    return (
        type(value) is str
        and 1 <= len(value) <= META_CREATE_MAX_FORM_VALUE_LENGTH
        and all(32 <= ord(character) <= 126 for character in value)
    )


def unexpressible_form_values(spec: MetaAdsCompiledCampaignSpec) -> tuple[str, ...]:
    """Return the *names of the fields* whose compiled value is not form-safe."""

    labels = ("campaign name", "ad set name", "creative name", "ad name")
    return tuple(
        label
        for label, value in zip(labels, form_safe_request_values(spec), strict=True)
        if _FORM_SAFE.fullmatch(value) is None
    )


def _postcondition(
    account: MetaAdsCreateAccountBinding,
    candidate: MetaAdsCreateCandidateBinding,
    creative: MetaAdsCreativeVaultBinding,
) -> MetaAdsCreatePostcondition:
    projection = projection_from_compiled_spec(
        candidate.specification, creative_asset_content_digest=creative.content_digest
    )
    return MetaAdsCreatePostcondition(
        account_ref=account.account_ref,
        campaign_name=projection.campaign_name,
        predicted_hierarchy_digest=digest(projection.model_dump(mode="json", warnings="error")),
    )


def _action_id(
    *,
    workspace_profile_ref: str,
    account: MetaAdsCreateAccountBinding,
    execution: MetaAdsCreateExecutionBinding,
    candidate: MetaAdsCreateCandidateBinding,
    observation: MetaAdsCreateObservationBinding,
    postcondition: MetaAdsCreatePostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> str:
    return digest(
        {
            "kind": "meta-ads-single-image-hierarchy-create-action-v1",
            "workspace_profile_ref": workspace_profile_ref,
            "account": account.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "candidate": candidate.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "before": before.model_dump(mode="json", warnings="error"),
            "after": after.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": META_CREATE_PLAN_STATUS,
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_EVERY_CHILD",
            "recovery_semantics": "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE",
            "execution_authority": False,
            "live_validated": False,
            "conditional_write_proof": False,
            "idempotency_proof": False,
        }
    )


class MetaAdsCreateWorkspaceBinding(StrictModel):
    """The exact local workspace profile this plan belongs to."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_CREATE_LOCAL_WORKSPACE"] = "META_ADS_CREATE_LOCAL_WORKSPACE"
    workspace_profile_ref: SafeRef


def _build_plan(
    *,
    action_id: str,
    workspace: MetaAdsCreateWorkspaceBinding,
    account: MetaAdsCreateAccountBinding,
    execution: MetaAdsCreateExecutionBinding,
    candidate: MetaAdsCreateCandidateBinding,
    postcondition: MetaAdsCreatePostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> CompositePlan:
    identity = AccountIdentity(
        account_ref=account.account_ref,
        stable_account_id=account.account_client_ref,
        auth_profile_ref=execution.profile_ref,
        principal_ref=execution.principal_ref,
        client_application_ref=execution.client_application_ref,
        scopes=(META_CREATE_SCOPE,),
    )
    subplan = SubPlan(
        subplan_id=f"meta-ads-single-image-hierarchy-create-{action_id}",
        provider="meta_ads",
        operation=OperationKind.CREATE,
        account=identity,
        provider_api_version=META_CREATE_API_VERSION,
        capability_version=META_CREATE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path="campaign/hierarchy", before=before, after=after),),
        # The compiled candidate's campaign lifetime cap, in minor units.  The
        # compiler contract records that the campaign request alone owns the
        # lifetime cap and that the ad set deliberately carries no budget or
        # schedule, so there is no daily amount and no daily x declared-days
        # disclosure to make here.
        max_spend=Money(
            minor_units=candidate.specification.maximum_spend.minor_units,
            currency=candidate.specification.maximum_spend.currency,
        ),
        egress=(EgressTarget(host=META_CREATE_HOST, purpose=META_CREATE_EGRESS_PURPOSE),),
        # Everything pausable this route creates is PAUSED, and rule 11 forbids
        # deleting a resource that is not proven unreferenced.  The declared
        # disposition is ``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``; ``SubPlan``
        # reserves its explicit ``recovery_disposition`` for GA4 CREATE and
        # requires at least one provider-request operation, so the bounded
        # compensating action named here is ``PAUSE`` -- the only non-destructive
        # one, and a no-op against a hierarchy this route already created paused.
        recovery_actions=(OperationKind.PAUSE,),
        expected_child_egress_count=postcondition.child_egress_count,
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"meta-ads-single-image-hierarchy-create-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: MetaAdsCreateAccountBinding,
    execution: MetaAdsCreateExecutionBinding,
    candidate: MetaAdsCreateCandidateBinding,
    observation: MetaAdsCreateObservationBinding,
    expires_at: datetime,
) -> None:
    specification = candidate.specification
    if specification.account_ref != account.account_ref:
        raise ValueError("the candidate was compiled for a different Meta ad account")
    if specification.account_currency != account.currency:
        raise ValueError("the candidate currency differs from the observed account currency")
    if specification.account_timezone != account.timezone:
        raise ValueError("the candidate timezone differs from the observed account timezone")
    if observation.campaign_name != specification.campaign_request.name:
        raise ValueError("the observation checked a different campaign name")
    if specification.asset_content_digest != observation.creative.content_digest:
        raise ValueError("the compiled ad must reference the one uploaded vault asset")
    if unexpressible_form_values(specification):
        raise ValueError("a compiled request value cannot be expressed in the fixed form body")
    if observation.read_finished_at >= expires_at:
        raise ValueError("plan expiry must follow the completed create read")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")
    if expires_at > execution.expires_at or expires_at > execution.data_access_expires_at:
        raise ValueError("plan expiry exceeds an inspected Meta token window")


class MetaAdsSingleImageHierarchyCreatePlannedAction(StrictModel):
    """One exact paused single-image hierarchy CREATE, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = META_CREATE_PLAN_STATUS
    action_id: Digest
    workspace_binding: MetaAdsCreateWorkspaceBinding
    account_binding: MetaAdsCreateAccountBinding
    execution_binding: MetaAdsCreateExecutionBinding
    candidate_binding: MetaAdsCreateCandidateBinding
    observation_binding: MetaAdsCreateObservationBinding
    postcondition: MetaAdsCreatePostcondition
    plan: CompositePlan
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    configuration_readback: Literal["REQUIRED_AFTER_EVERY_CHILD"] = "REQUIRED_AFTER_EVERY_CHILD"
    recovery_semantics: Literal["RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"] = (
        "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    )
    execution_authority: Literal[False] = False
    live_validated: Literal[False] = False
    conditional_write_proof: Literal[False] = False
    idempotency_proof: Literal[False] = False

    @field_validator(
        "execution_authority",
        "live_validated",
        "conditional_write_proof",
        "idempotency_proof",
        mode="before",
    )
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("planned action control flags must be exactly false")
        return value

    @model_validator(mode="after")
    def exact_reconstructed_action(self) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only complete, closed, validated serializable action data."""

        checked = revalidate_meta_ads_create_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def build_meta_ads_single_image_hierarchy_create_plan(
    *,
    workspace_profile_ref: str,
    account_binding: MetaAdsCreateAccountBinding,
    execution_binding: MetaAdsCreateExecutionBinding,
    candidate_binding: MetaAdsCreateCandidateBinding,
    observation_binding: MetaAdsCreateObservationBinding,
    expires_at: datetime,
    fingerprint_key: bytes,
) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
    """Build the one non-authorizing paused single-image hierarchy CREATE plan."""

    try:
        workspace = MetaAdsCreateWorkspaceBinding(workspace_profile_ref=workspace_profile_ref)
        account = _checked_member(account_binding, MetaAdsCreateAccountBinding)
        execution = _checked_member(execution_binding, MetaAdsCreateExecutionBinding)
        candidate = _checked_member(candidate_binding, MetaAdsCreateCandidateBinding)
        observation = _checked_member(observation_binding, MetaAdsCreateObservationBinding)
        if observation.fingerprint_key_marker != fingerprint_key_marker(fingerprint_key):
            raise ValueError("the observation was projected under a different local key")
        expiry = _checked_expiry(expires_at)
        _assert_outer_bindings(
            account=account,
            execution=execution,
            candidate=candidate,
            observation=observation,
            expires_at=expiry,
        )
        expected_child_egress_count(candidate.specification)
        postcondition = _postcondition(account, candidate, observation.creative)
        before, after = _states(
            account=account,
            candidate=candidate,
            creative=observation.creative,
            fingerprint_key=fingerprint_key,
        )
        action_id = _action_id(
            workspace_profile_ref=workspace.workspace_profile_ref,
            account=account,
            execution=execution,
            candidate=candidate,
            observation=observation,
            postcondition=postcondition,
            before=before,
            after=after,
            expires_at=expiry,
        )
        plan = _build_plan(
            action_id=action_id,
            workspace=workspace,
            account=account,
            execution=execution,
            candidate=candidate,
            postcondition=postcondition,
            before=before,
            after=after,
            expires_at=expiry,
        )
        return MetaAdsSingleImageHierarchyCreatePlannedAction(
            action_id=action_id,
            workspace_binding=workspace,
            account_binding=account,
            execution_binding=execution,
            candidate_binding=candidate,
            observation_binding=observation,
            postcondition=postcondition,
            plan=plan,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


def _states(
    *,
    account: MetaAdsCreateAccountBinding,
    candidate: MetaAdsCreateCandidateBinding,
    creative: MetaAdsCreativeVaultBinding,
    fingerprint_key: bytes,
) -> tuple[StateRef, StateRef]:
    projection = projection_from_compiled_spec(
        candidate.specification, creative_asset_content_digest=creative.content_digest
    )
    before = semantic_hierarchy_state(
        None,
        account_client_ref=account.account_client_ref,
        campaign_name=projection.campaign_name,
        fingerprint_key=fingerprint_key,
    )
    after = semantic_hierarchy_state(
        projection,
        account_client_ref=account.account_client_ref,
        campaign_name=projection.campaign_name,
        fingerprint_key=fingerprint_key,
    )
    if before == after:
        raise ValueError("a create plan must change the observed hierarchy state")
    return before, after


def predicted_states(
    action: MetaAdsSingleImageHierarchyCreatePlannedAction, *, fingerprint_key: bytes
) -> tuple[StateRef, StateRef]:
    """Recompute the plan's two semantic states under the current local key."""

    return _states(
        account=action.account_binding,
        candidate=action.candidate_binding,
        creative=action.observation_binding.creative,
        fingerprint_key=fingerprint_key,
    )


# -- strict reconstruction ---------------------------------------------------


def _strict_json_bytes(value: object, *, label: str) -> bytes:
    try:
        encoded = json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise ValueError(f"{label} JSON") from None
    if len(encoded) > _MAX_ACTION_BYTES:
        raise ValueError(f"{label} exceeds bound")
    return encoded


def _checked_member[T: BaseModel](value: object, expected: type[T]) -> T:
    if type(value) is not expected:
        raise ValueError("member type")
    try:
        raw = value.model_dump(mode="json", warnings="error")
        parsed = expected.model_validate_json(_strict_json_bytes(raw, label="member"))
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("member value") from None
    if type(parsed) is not expected or canonical_bytes(raw) != canonical_bytes(
        parsed.model_dump(mode="json", warnings="error")
    ):
        raise ValueError("member canonical shape")
    return parsed


def _checked_expiry(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("plan expiry")
    return value.astimezone(UTC)


_ACTION_FIELDS: Final = frozenset(
    {
        "schema_version",
        "status",
        "action_id",
        "workspace_binding",
        "account_binding",
        "execution_binding",
        "candidate_binding",
        "observation_binding",
        "postcondition",
        "plan",
        "current_state_status",
        "configuration_readback",
        "recovery_semantics",
        "execution_authority",
        "live_validated",
        "conditional_write_proof",
        "idempotency_proof",
    }
)


def _raw_mapping(value: object) -> dict[str, object]:
    if type(value) is MetaAdsSingleImageHierarchyCreatePlannedAction:
        raw = value.model_dump(mode="json", warnings="error")
    elif type(value) is dict:
        raw = value
    else:
        raise ValueError("action input type")
    encoded = _strict_json_bytes(raw, label="action")
    try:
        parsed = parse_json(encoded, maximum_bytes=_MAX_ACTION_BYTES)
    except (AdhelmError, UnicodeError, ValueError):
        raise ValueError("action JSON") from None
    if type(parsed) is not dict:
        raise ValueError("action object")
    return cast(dict[str, object], parsed)


def _raw_member(data: dict[str, object], name: str, expected: type[BaseModel]) -> BaseModel:
    value = data[name]
    if type(value) is not dict:
        raise ValueError("nested action member")
    encoded = _strict_json_bytes(value, label="nested action member")
    try:
        parsed = expected.model_validate_json(encoded)
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("nested action member") from None
    if type(parsed) is not expected or canonical_bytes(value) != canonical_bytes(
        parsed.model_dump(mode="json", warnings="error")
    ):
        raise ValueError("nested action canonical shape")
    return parsed


def _action_from_raw(
    data: dict[str, object],
) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
    if set(data) != _ACTION_FIELDS or any(type(key) is not str for key in data):
        raise ValueError("action keys")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("action schema version")
    if data["status"] != META_CREATE_PLAN_STATUS:
        raise ValueError("action status")
    if type(data["action_id"]) is not str or _DIGEST.fullmatch(data["action_id"]) is None:
        raise ValueError("action ID")
    if data["current_state_status"] != "RECHECK_REQUIRED_BEFORE_EGRESS":
        raise ValueError("current-state semantics")
    if data["configuration_readback"] != "REQUIRED_AFTER_EVERY_CHILD":
        raise ValueError("readback semantics")
    if data["recovery_semantics"] != "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE":
        raise ValueError("recovery semantics")
    for name in (
        "execution_authority",
        "live_validated",
        "conditional_write_proof",
        "idempotency_proof",
    ):
        if type(data[name]) is not bool or data[name] is not False:
            raise ValueError("action control flag")
    try:
        return MetaAdsSingleImageHierarchyCreatePlannedAction(
            action_id=data["action_id"],
            workspace_binding=cast(
                MetaAdsCreateWorkspaceBinding,
                _raw_member(data, "workspace_binding", MetaAdsCreateWorkspaceBinding),
            ),
            account_binding=cast(
                MetaAdsCreateAccountBinding,
                _raw_member(data, "account_binding", MetaAdsCreateAccountBinding),
            ),
            execution_binding=cast(
                MetaAdsCreateExecutionBinding,
                _raw_member(data, "execution_binding", MetaAdsCreateExecutionBinding),
            ),
            candidate_binding=cast(
                MetaAdsCreateCandidateBinding,
                _raw_member(data, "candidate_binding", MetaAdsCreateCandidateBinding),
            ),
            observation_binding=cast(
                MetaAdsCreateObservationBinding,
                _raw_member(data, "observation_binding", MetaAdsCreateObservationBinding),
            ),
            postcondition=cast(
                MetaAdsCreatePostcondition,
                _raw_member(data, "postcondition", MetaAdsCreatePostcondition),
            ),
            plan=cast(CompositePlan, _raw_member(data, "plan", CompositePlan)),
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("planned action") from None


def _assert_action_instance(action: MetaAdsSingleImageHierarchyCreatePlannedAction) -> None:
    """Reconstruct every derived fact, using the action's own retained states.

    The semantic states are keyed by a local key this pure contract must not
    read, so revalidation checks their *structural* binding here -- the plan's
    ``before``/``after``, the postcondition's predicted hierarchy digest, and the
    action ID -- while the configured planner and the guarded adapter each
    recompute the fingerprints under the current key.
    """

    if type(action) is not MetaAdsSingleImageHierarchyCreatePlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, MetaAdsCreateWorkspaceBinding)
    account = _checked_member(action.account_binding, MetaAdsCreateAccountBinding)
    execution = _checked_member(action.execution_binding, MetaAdsCreateExecutionBinding)
    candidate = _checked_member(action.candidate_binding, MetaAdsCreateCandidateBinding)
    observation = _checked_member(action.observation_binding, MetaAdsCreateObservationBinding)
    postcondition = _checked_member(action.postcondition, MetaAdsCreatePostcondition)
    plan = _checked_member(action.plan, CompositePlan)
    expiry = _checked_expiry(plan.expires_at)
    _assert_outer_bindings(
        account=account,
        execution=execution,
        candidate=candidate,
        observation=observation,
        expires_at=expiry,
    )
    expected_child_egress_count(candidate.specification)
    if postcondition != _postcondition(account, candidate, observation.creative):
        raise ValueError("postcondition differs from the exact candidate")
    if len(plan.subplans) != 1:
        raise ValueError("a create action carries exactly one subplan")
    subplan = plan.subplans[0]
    expected_action_id = _action_id(
        workspace_profile_ref=workspace.workspace_profile_ref,
        account=account,
        execution=execution,
        candidate=candidate,
        observation=observation,
        postcondition=postcondition,
        before=subplan.before,
        after=subplan.after,
        expires_at=expiry,
    )
    if action.action_id != expected_action_id:
        raise ValueError("action ID differs from complete action facts")
    expected_plan = _build_plan(
        action_id=expected_action_id,
        workspace=workspace,
        account=account,
        execution=execution,
        candidate=candidate,
        postcondition=postcondition,
        before=subplan.before,
        after=subplan.after,
        expires_at=expiry,
    )
    if plan != expected_plan:
        raise ValueError("plan differs from the reconstructed Meta Ads create action")
    if subplan.before == subplan.after:
        raise ValueError("a create plan must change the observed hierarchy state")
    if subplan.before.revision != _STATE_KIND or subplan.after.revision != _STATE_KIND:
        raise ValueError("create states must be semantic hierarchy projections")


def revalidate_meta_ads_create_planned_action(
    value: object,
) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
    """Strictly parse and reconstruct one closed locally retained CREATE action."""

    try:
        raw = _raw_mapping(value)
        action = _action_from_raw(raw)
        _assert_action_instance(action)
        if canonical_bytes(raw) != canonical_bytes(
            action.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("action canonical shape")
        return action
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


class MetaAdsSingleImageHierarchyCreatePlanRecord(StrictModel):
    """The additive ``changes`` record that retains one closed CREATE action."""

    schema_version: Literal[1] = 1
    event_type: Literal["META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN"] = (
        "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN"
    )
    provider: Literal["meta_ads"] = "meta_ads"
    action: MetaAdsSingleImageHierarchyCreatePlannedAction
    action_digest: Digest
    plan_digest: Digest
    candidate_digest: Digest
    expected_child_egress_count: Literal[5] = META_CREATE_CHILD_EGRESS_COUNT
    expires_at: datetime
    maximum_incremental_spend: Money
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"] = (
        "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    )
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("execution_authority", "requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("plan record controls require exact booleans")
        return value

    @field_validator("action", mode="before")
    @classmethod
    def closed_action(cls, value: object) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
        return revalidate_meta_ads_create_planned_action(value)

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_action_binding(self) -> MetaAdsSingleImageHierarchyCreatePlanRecord:
        action = self.action
        subplan = action.plan.subplans[0]
        if (
            self.action_digest != digest(action.model_dump(mode="json", warnings="error"))
            or self.plan_digest != action.plan.plan_digest
            or self.candidate_digest != action.candidate_binding.candidate_digest
            or self.expected_child_egress_count != subplan.expected_child_egress_count
            or self.expires_at != action.plan.expires_at
            or subplan.provider != self.provider
            or self.maximum_incremental_spend != subplan.max_spend
        ):
            raise ValueError("retained Meta Ads CREATE plan facts do not match the action")
        return self


def meta_ads_create_plan_record(
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
) -> MetaAdsSingleImageHierarchyCreatePlanRecord:
    """Build the retained record for one already-closed CREATE action."""

    checked = revalidate_meta_ads_create_planned_action(action)
    return MetaAdsSingleImageHierarchyCreatePlanRecord(
        action=checked,
        action_digest=digest(checked.model_dump(mode="json", warnings="error")),
        plan_digest=checked.plan.plan_digest,
        candidate_digest=checked.candidate_binding.candidate_digest,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_meta_ads_create_plan_record(
    value: object,
) -> MetaAdsSingleImageHierarchyCreatePlanRecord:
    """Reject incomplete, aliased, or incoherent retained CREATE plan facts."""

    try:
        if type(value) is MetaAdsSingleImageHierarchyCreatePlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            MetaAdsSingleImageHierarchyCreatePlanRecord.model_fields
        ):
            raise ValueError("a complete record is required")
        encoded = _strict_json_bytes(value, label="plan record")
        return MetaAdsSingleImageHierarchyCreatePlanRecord.model_validate_json(encoded)
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_plan("A complete closed Meta Ads CREATE plan record is required.") from None


def meta_ads_create_plan_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive plan kind."""

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(META_CREATE_PLAN_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is MetaAdsSingleImageHierarchyCreatePlanRecord


def checked_native_identifier(value: object) -> str:
    """Accept only a bounded native decimal Graph object identifier."""

    if type(value) is not str or _NATIVE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("native Graph object identifier")
    return value


def checked_image_hash(value: object) -> str:
    """Accept only a 32-character lowercase hexadecimal ad-image hash.

    Meta documents ``hash`` as an opaque string that uniquely identifies the
    image within the ad account.  No grammar is documented; 32 lowercase
    hexadecimal characters is what every documented example shows, and this
    closed grammar is a local ``UNVERIFIED`` narrowing, not a provider contract.
    Because it is fixed-length and escape-free, planning reserves the creative
    form's exact length (:data:`META_CREATE_PLANNING_IMAGE_HASH`).  A response
    carrying any other hash fails the acknowledgement as ambiguous; the safe
    failure, since nothing after it is sent.
    """

    if type(value) is not str or _IMAGE_HASH.fullmatch(value) is None:
        raise ValueError("ad image hash")
    return value


__all__ = [
    "CREATIVE_ASSOCIATION_NOT_PROVEN",
    "CREATIVE_ASSOCIATION_PROVEN",
    "IMAGE_ASSOCIATION_NOT_PROVEN",
    "META_CREATE_API_VERSION",
    "META_CREATE_CAPABILITY_VERSION",
    "META_CREATE_CHILD_EGRESS_COUNT",
    "META_CREATE_CHILD_LABELS",
    "META_CREATE_EDGES",
    "META_CREATE_EGRESS_PURPOSE",
    "META_CREATE_HIERARCHY_MEMBER_COUNT",
    "META_CREATE_HOST",
    "META_CREATE_IMAGE_HASH_LENGTH",
    "META_CREATE_MAX_FORM_VALUE_LENGTH",
    "META_CREATE_MAX_HIERARCHY_MEMBERS",
    "META_CREATE_MAX_UPLOAD_BYTES",
    "META_CREATE_MEDIA_TYPES",
    "META_CREATE_PLAN_RECORD_KIND",
    "META_CREATE_PLAN_STATUS",
    "META_CREATE_PLANNING_IMAGE_HASH",
    "META_CREATE_SCOPE",
    "META_CREATE_UPLOAD_FIELD",
    "NOT_READ_BACK_COPY_BEARING_FIELD",
    "MetaAdsCreateAccountBinding",
    "MetaAdsCreateCandidateBinding",
    "MetaAdsCreateChild",
    "MetaAdsCreateExecutionBinding",
    "MetaAdsCreateObservationBinding",
    "MetaAdsCreatePostcondition",
    "MetaAdsCreatePreconditions",
    "MetaAdsCreateRedactedReview",
    "MetaAdsCreateWorkspaceBinding",
    "MetaAdsCreativeVaultBinding",
    "MetaAdsHierarchyMember",
    "MetaAdsSingleImageHierarchyCreatePlanRecord",
    "MetaAdsSingleImageHierarchyCreatePlannedAction",
    "MetaAdsSingleImageHierarchyCreateSelection",
    "MetaAdsSingleImageHierarchyProjection",
    "build_meta_ads_single_image_hierarchy_create_plan",
    "checked_image_hash",
    "checked_native_identifier",
    "create_child_plan",
    "create_redacted_review",
    "creative_member_configuration",
    "epoch_seconds",
    "expected_child_egress_count",
    "fingerprint_key_marker",
    "form_value_expressible",
    "meta_ads_create_plan_record",
    "meta_ads_create_plan_record_kind_installed",
    "predicted_states",
    "projection_from_compiled_spec",
    "revalidate_meta_ads_create_plan_record",
    "revalidate_meta_ads_create_planned_action",
    "semantic_hierarchy_state",
    "semantic_state_from_members",
    "semantic_target_fingerprint",
    "unexpressible_form_values",
]

"""One exact paused OpenAI Ads v1 chat-card hierarchy CREATE, without authority.

This is the P04-OA contract layer.  It turns one *retained* offline compiler
candidate (:mod:`adhelm.providers.openai_compiler`) plus one fresh bounded
account-key read into a closed
:class:`OpenAIAdsChatCardHierarchyCreatePlannedAction` whose ``plan`` is the
ordinary provider-neutral :class:`~adhelm.safety.CompositePlan` the kernel
executes.

Three things are deliberately different from the M55/P03 single-request
contracts.

* **The approved operation is several ordered requests.**  OpenAI Ads has no
  atomic hierarchy mutation: a paused campaign, its ad groups and its ads are
  separate ``POST`` routes, and the creative image is a separate upload that
  mints the ``file_id`` an ad must reference.  The subplan therefore declares
  ``expected_child_egress_count = 1 + 1 + N + M`` and is executed by a
  :class:`~adhelm.safety.ChildEgressAdapter`.
* **``before`` and ``after`` are semantic hierarchy fingerprints.**  OpenAI
  assigns every native identifier at creation time, so the states are locally
  keyed HMACs (:mod:`adhelm.providers._semantic_state`) over request-controllable
  names and configuration only.  ``before`` is the empty hierarchy under one
  ``(ad account, campaign name)`` binding -- "no campaign with this name in this
  account" -- and ``after`` is the exact predicted member set.
* **The uploaded file cannot be read back.**  ``docs/research/openai-file-read-contract.md``
  establishes that OpenAI Ads documents no file ``GET`` at all.  The predicted
  ad member therefore binds the **local asset content digest**, and the observed
  ad member may carry that digest only when the paused ad's own readback
  references the exact ``file_id`` this attempt's upload acknowledged.  That is
  ``PROVEN_UPLOAD_ASSOCIATION``; it is never provider-storage or delivered-
  rendition byte equality, which stay
  ``PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ``.

Nothing here reads a credential, contacts OpenAI, or authorizes anything.  A
matching fingerprint proves neither provider origin, freshness, permission,
reservation, execution authority, nor a mutation outcome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal, cast

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
from adhelm.providers.openai_compiler import (
    OpenAIAdsCampaignBrief,
    OpenAIAdsCompiledCampaignSpec,
)
from adhelm.providers.openai_pause_contract import OpenAIPauseWorkspaceBinding
from adhelm.safety import (
    CompositePlan,
    EgressTarget,
    FieldChange,
    Money,
    OpenAIAccountKeyIdentity,
    OperationKind,
    StateRef,
    SubPlan,
)

#: The retained ledger record kind this contract's plan is stored under.  It is
#: additive in ``changes`` and mirrors ``GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN``.
OPENAI_CREATE_PLAN_RECORD_KIND: Final = "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN"

OPENAI_CREATE_PLAN_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
#: The documented Ads base host and version prefix, re-verified 2026-09-09 from
#: https://developers.openai.com/ads/api-reference/files (the upload curl uses
#: ``https://api.ads.openai.com/v1/upload``).
OPENAI_CREATE_HOST: Final[Literal["api.ads.openai.com"]] = "api.ads.openai.com"
OPENAI_CREATE_API_VERSION: Final[Literal["v1"]] = "v1"
OPENAI_CREATE_CAPABILITY_VERSION: Final = (
    "openai-ads-v1-chat-card-hierarchy-create-contract-2026-09-09"
)
OPENAI_CREATE_EGRESS_PURPOSE: Final = "ordered OpenAI Ads v1 paused chat-card hierarchy create"

#: The four ordered child steps.  ``upload-creative`` is first because an ad
#: cannot be created without the ``file_id`` it returns, and the campaign
#: precedes its ad groups, which precede their ads.
OPENAI_CREATE_CHILD_LABELS: Final = (
    "upload-creative",
    "create-campaign",
    "create-ad-group",
    "create-ad",
)

#: The image media types this build will send.  The Ads Files reference
#: documents no allowlist and no maximum byte count (UNVERIFIED, 2026-09-09), so
#: the bound is deliberately local and narrow rather than inferred.
OPENAI_CREATE_MEDIA_TYPES: Final = frozenset({"image/png", "image/jpeg"})
#: The one documented multipart form field name for a binary upload.
OPENAI_CREATE_UPLOAD_FIELD: Final = "file"
#: The local ceiling on the bytes this route will hand to the upload child.
OPENAI_CREATE_MAX_UPLOAD_BYTES: Final = 5 * 1024 * 1024

#: 1 campaign + every ad group + every ad.  One fewer than the kernel's child
#: bound, because the upload child creates no hierarchy member.
OPENAI_CREATE_MAX_HIERARCHY_MEMBERS: Final = MAX_CHILD_EGRESS_COUNT - 1

_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_NATIVE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9_-]{1,120}$", re.ASCII)
_FILE_IDENTIFIER: Final = re.compile(r"^file_[A-Za-z0-9_-]{1,114}$", re.ASCII)
_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 4_194_304

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

# -- the semantic projection -------------------------------------------------

_FINGERPRINT_CONTEXT: Final = b"adhelm/openai-ads/chat-card-hierarchy-create/semantic/v1\x00"
_STATE_KIND: Final = "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_SEMANTIC_HIERARCHY_V1"
_SEMANTIC_PROJECTION: Final = "NAME_HMAC_CONFIGURATION_EXACT_V1"
_SEMANTIC_SPEC: Final = SemanticHierarchySpec(
    context=_FINGERPRINT_CONTEXT,
    state_kind=_STATE_KIND,
    semantic_projection=_SEMANTIC_PROJECTION,
    target_purpose="openai-ads-chat-card-hierarchy-create-semantic-target",
    member_purpose="openai-ads-chat-card-hierarchy-create-semantic-member",
    collection_purpose="openai-ads-chat-card-hierarchy-create-semantic-collection",
)
_IDENTITY_PURPOSE: Final = "openai-ads-chat-card-hierarchy-create-semantic-identity"
_KEY_MARKER_PURPOSE: Final = "openai-ads-chat-card-hierarchy-create-fingerprint-key-marker"
#: ``name`` is a reserved semantic key, so the projection names the
#: request-controllable identity role instead.
_IDENTITY_FIELD: Final = "semantic_identity"

#: The sentinel an observation uses when the readback ad does not reference the
#: exact file this attempt uploaded.  It can never equal a content digest, so a
#: hierarchy whose creative association is unproven cannot match the prediction.
FILE_ASSOCIATION_NOT_PROVEN: Final = "FILE_ASSOCIATION_NOT_PROVEN"


def _invalid_plan(message: str = "OpenAI Ads CREATE plan data is invalid.") -> AdhelmError:
    return AdhelmError("INVALID_OPENAI_ADS_CREATE_PLAN", message, exit_code=ExitCode.INVALID_INPUT)


class OpenAIAdsHierarchyMember(StrictModel):
    """One request-controllable member of a paused chat-card hierarchy.

    ``identity`` is the ordered path of configured names that uniquely names the
    member to a *request*: the campaign name, then the ad-group name, then the
    ad name.  ``configuration`` is the remaining request-controllable values.
    Neither may carry a provider-assigned identifier; ``_semantic_state``
    rejects the common shapes and this projection keeps identifiers out by
    hashing the identity separately.
    """

    member_kind: Literal["CAMPAIGN", "AD_GROUP", "AD"]
    identity: tuple[str, ...] = Field(min_length=1, max_length=4)
    configuration: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded_projection_member(self) -> OpenAIAdsHierarchyMember:
        keys = tuple(key for key, _value in self.configuration)
        if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
            raise ValueError("member configuration keys must be unique and sorted")
        if any(not key or len(key) > 64 for key in keys):
            raise ValueError("member configuration keys are bounded")
        if any(not value or len(value) > 256 for _key, value in self.configuration):
            raise ValueError("member configuration values are bounded")
        if any(not item or len(item) > 1_000 for item in self.identity):
            raise ValueError("member identity components are bounded")
        expected = {"CAMPAIGN": 2, "AD_GROUP": 3, "AD": 4}[self.member_kind]
        if len(self.identity) != expected:
            raise ValueError("member identity must be its exact configured name path")
        return self


class OpenAIAdsChatCardHierarchyProjection(StrictModel):
    """The exact member multiset one paused chat-card hierarchy is projected to.

    Both the compiled candidate and the post-create readback are reduced to this
    one shape, so a predicted and an observed fingerprint are comparable by
    construction rather than by two hand-written hashers agreeing.
    """

    schema_version: Literal[1] = 1
    projection_kind: Literal["OPENAI_ADS_CHAT_CARD_HIERARCHY"] = "OPENAI_ADS_CHAT_CARD_HIERARCHY"
    campaign_name: str = Field(min_length=3, max_length=1_000)
    members: tuple[OpenAIAdsHierarchyMember, ...] = Field(
        min_length=3, max_length=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS
    )

    @model_validator(mode="after")
    def every_member_is_paused(self) -> OpenAIAdsChatCardHierarchyProjection:
        for member in self.members:
            if dict(member.configuration).get("status") != "paused":
                raise ValueError("every created OpenAI Ads entity must be paused")
            if member.identity[0] != self.campaign_name:
                raise ValueError("every member belongs to this projection's campaign")
        kinds = [member.member_kind for member in self.members]
        if kinds.count("CAMPAIGN") != 1 or "AD_GROUP" not in kinds or "AD" not in kinds:
            raise ValueError("a chat-card hierarchy projection needs one campaign, groups and ads")
        return self

    @property
    def ad_group_count(self) -> int:
        return sum(member.member_kind == "AD_GROUP" for member in self.members)

    @property
    def ad_count(self) -> int:
        return sum(member.member_kind == "AD" for member in self.members)


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
        or _NATIVE_IDENTIFIER.fullmatch(account_client_ref) is None
    ):
        raise ValueError("semantic target requires the exact ad-account ID")
    return keyed_fingerprint(
        fingerprint_key,
        context=_FINGERPRINT_CONTEXT,
        purpose=_SEMANTIC_SPEC.target_purpose,
        value={
            "schema_version": 1,
            "state_kind": _STATE_KIND,
            "provider": "openai_ads",
            "provider_api_version": OPENAI_CREATE_API_VERSION,
            "ad_account_client_ref": account_client_ref,
            "campaign_name_fingerprint": _identity_fingerprint(
                ("campaign_name", campaign_name), fingerprint_key=fingerprint_key
            ),
        },
    )


def _member_fingerprint(member: OpenAIAdsHierarchyMember, *, fingerprint_key: bytes) -> str:
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
    members: tuple[OpenAIAdsHierarchyMember, ...],
    *,
    account_client_ref: str,
    campaign_name: str,
    fingerprint_key: bytes,
) -> StateRef:
    """Project any observed member multiset into one comparable state.

    A *partial* hierarchy is a real outcome of an ordered non-atomic create, so
    the state projection deliberately does not require a complete hierarchy.
    Completeness is the strict :class:`OpenAIAdsChatCardHierarchyProjection`'s
    job, and equality with the approved ``after`` is the adapter's.
    """

    if type(members) is not tuple or any(
        type(item) is not OpenAIAdsHierarchyMember for item in members
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
    projection: OpenAIAdsChatCardHierarchyProjection | None,
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

    members: tuple[OpenAIAdsHierarchyMember, ...] = ()
    if projection is not None:
        if type(projection) is not OpenAIAdsChatCardHierarchyProjection:
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


def targeted_location_digest(location_ids: tuple[str, ...]) -> str:
    """Digest the exact declared location targeting without hashing an ID as a key.

    ``_semantic_state`` refuses a configuration key ending in ``_ids``, and it is
    right to: a provider-assigned identifier in a projection would make the
    predicted ``after`` unmatchable.  These are *customer-declared* documented
    location IDs the request controls, so they belong in the projection -- as one
    digest under a key that is not identifier-shaped.
    """

    if type(location_ids) is not tuple or not location_ids:
        raise ValueError("declared location targeting is required")
    return digest(
        {"kind": "openai-ads-v1-declared-location-targeting-v1", "include": list(location_ids)}
    )


def projection_from_compiled_spec(
    spec: OpenAIAdsCompiledCampaignSpec, *, creative_asset_content_digest: str
) -> OpenAIAdsChatCardHierarchyProjection:
    """Reduce one compiled candidate to its exact predicted member multiset.

    ``creative_asset_content_digest`` is the local vault asset this attempt will
    upload.  The compiled candidate's own ``file_id`` is customer-supplied local
    evidence that this route never sends, because an ad must reference the file
    this attempt's own upload minted.
    """

    if type(spec) is not OpenAIAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    if type(creative_asset_content_digest) is not str or (
        _DIGEST.fullmatch(creative_asset_content_digest) is None
    ):
        raise ValueError("creative asset content digest")
    campaign = spec.campaign_request
    name = campaign.name
    members: list[OpenAIAdsHierarchyMember] = [
        OpenAIAdsHierarchyMember(
            member_kind="CAMPAIGN",
            identity=(name, name),
            configuration=(
                ("bidding_type", campaign.bidding_type),
                ("end_time", str(campaign.end_time)),
                ("lifetime_spend_limit_micros", str(campaign.budget.lifetime_spend_limit_micros)),
                (
                    "location_targeting_digest",
                    targeted_location_digest(
                        tuple(item.id for item in campaign.targeting.locations.include)
                    ),
                ),
                ("start_time", str(campaign.start_time)),
                ("status", campaign.status),
                ("targeting_declared", "true"),
            ),
        )
    ]
    for group in spec.ad_groups:
        bidding = group.request.bidding_config
        members.append(
            OpenAIAdsHierarchyMember(
                member_kind="AD_GROUP",
                identity=(name, group.request.name, group.request.name),
                configuration=(
                    ("billing_event_type", bidding.billing_event_type),
                    ("max_bid_micros", str(bidding.max_bid_micros)),
                    ("status", group.request.status),
                ),
            )
        )
        for ad in group.ads:
            members.append(
                OpenAIAdsHierarchyMember(
                    member_kind="AD",
                    identity=(name, group.request.name, ad.request.name, ad.request.name),
                    configuration=(
                        ("creative_asset_content_digest", creative_asset_content_digest),
                        ("creative_body_present", "true"),
                        ("creative_kind", ad.request.creative.type),
                        ("creative_target_url_present", "true"),
                        ("creative_title_present", "true"),
                        ("status", ad.request.status),
                    ),
                )
            )
    return OpenAIAdsChatCardHierarchyProjection(campaign_name=name, members=tuple(members))


# -- the ordered child plan --------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenAIAdsCreateChild:
    """One journaled child request of the single approved CREATE operation."""

    index: int
    label: str
    kind: Literal["UPLOAD_CREATIVE", "CREATE_CAMPAIGN", "CREATE_AD_GROUP", "CREATE_AD"]
    path: str
    ad_group_handle: str | None = None
    ad_handle: str | None = None


def create_child_plan(spec: OpenAIAdsCompiledCampaignSpec) -> tuple[OpenAIAdsCreateChild, ...]:
    """Return the exact ordered child requests one compiled candidate needs.

    The order is a dependency order, not a preference: the ad's ``file_id`` comes
    from the upload, the ad group's ``campaign_id`` from the campaign, and the
    ad's ``ad_group_id`` from its ad group.
    """

    if type(spec) is not OpenAIAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    children: list[OpenAIAdsCreateChild] = [
        OpenAIAdsCreateChild(
            index=1, label="upload-creative", kind="UPLOAD_CREATIVE", path="/upload"
        ),
        OpenAIAdsCreateChild(
            index=2, label="create-campaign", kind="CREATE_CAMPAIGN", path="/campaigns"
        ),
    ]
    index = 3
    for group in spec.ad_groups:
        children.append(
            OpenAIAdsCreateChild(
                index=index,
                label="create-ad-group",
                kind="CREATE_AD_GROUP",
                path="/ad_groups",
                ad_group_handle=group.ad_group_handle,
            )
        )
        index += 1
    for group in spec.ad_groups:
        for ad in group.ads:
            children.append(
                OpenAIAdsCreateChild(
                    index=index,
                    label="create-ad",
                    kind="CREATE_AD",
                    path="/ads",
                    ad_group_handle=group.ad_group_handle,
                    ad_handle=ad.ad_handle,
                )
            )
            index += 1
    return tuple(children)


def expected_child_egress_count(spec: OpenAIAdsCompiledCampaignSpec) -> int:
    """Return ``1 + 1 + N + M``, refusing a hierarchy above the kernel's bound."""

    children = create_child_plan(spec)
    if len(children) > MAX_CHILD_EGRESS_COUNT:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_HIERARCHY_TOO_LARGE",
            "This compiled hierarchy needs more journaled child requests than one approved "
            "operation may issue.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a candidate with at most "
                f"{MAX_CHILD_EGRESS_COUNT - 2} ad groups and ads combined, or plan the hierarchy "
                "as separate approved operations; nothing was read from the provider."
            ),
        )
    return len(children)


# -- the fresh bounded read --------------------------------------------------


class OpenAIAdsCreatePreconditions(StrictModel):
    """The bounded promotion checks this workspace actually performed.

    Several of the compiler contract's promotion prerequisites are recorded as
    explicit statuses rather than values, because the documented Ads surface
    exposes nothing to read them from.  Saying so is the point: the plan refuses
    to imply a check it did not make.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["OPENAI_ADS_V1_CHAT_CARD_CREATE_PRECONDITIONS"] = (
        "OPENAI_ADS_V1_CHAT_CARD_CREATE_PRECONDITIONS"
    )
    campaign_name_collision: Literal["NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"] = (
        "NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"
    )
    observed_campaign_name_matches: Literal[0] = 0
    account_status: Literal["ACTIVE"] = "ACTIVE"
    #: The compiler already refuses ``bidding_type: "conversions"``; the planner
    #: refuses it again, before any read, as a typed provider gate.
    conversion_bidding: Literal["NOT_ENABLED_NO_VERIFIED_ACTIVE_CONVERSION_SETTING"] = (
        "NOT_ENABLED_NO_VERIFIED_ACTIVE_CONVERSION_SETTING"
    )
    #: There is no documented Ads file GET, list, metadata or content route, so
    #: an uploaded file's provider bytes are never readable from this build.
    uploaded_file_readback: Literal["NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"] = (
        "NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"
    )
    file_association_proof: Literal["PAUSED_AD_READBACK_ONLY"] = "PAUSED_AD_READBACK_ONLY"
    provider_file_bytes: Literal["PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ"] = (
        "PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ"
    )
    creative_source: Literal["PRIVATE_LOCAL_VAULT_ONLY"] = "PRIVATE_LOCAL_VAULT_ONLY"
    compiled_file_reference_use: Literal["LOCAL_EVIDENCE_NOT_SENT"] = "LOCAL_EVIDENCE_NOT_SENT"
    #: No idempotency key, request ID, or settlement resource is documented for
    #: an Ads write (re-verified 2026-09-09 on the API overview).
    idempotency_contract: Literal["NOT_DOCUMENTED_NO_SAFE_WRITE_RETRY"] = (
        "NOT_DOCUMENTED_NO_SAFE_WRITE_RETRY"
    )

    @field_validator("observed_campaign_name_matches", mode="before")
    @classmethod
    def exact_zero(cls, value: object) -> int:
        if type(value) is not int or value != 0:
            raise ValueError("a create plan requires zero observed name collisions")
        return value


class OpenAIAdsCreativeVaultBinding(StrictModel):
    """The one private-vault image this attempt uploads, bound by content.

    The vault is content-addressed, so its object path is exactly
    ``<workspace>/assets/sha256/<content digest>``.  This binding therefore names
    the digest, not a caller-supplied filesystem path: accepting a path would let
    a request read a file the vault does not hold, which is the boundary
    ``docs/implementation/creative-asset-vault.md`` exists to keep.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["OPENAI_ADS_V1_PRIVATE_VAULT_CREATIVE"] = (
        "OPENAI_ADS_V1_PRIVATE_VAULT_CREATIVE"
    )
    #: The vault reference the request named and the planner resolved.
    vault_ref: str = Field(pattern=r"^vault:sha256:[0-9a-f]{64}$")
    content_digest: Digest
    media_type: Literal["image/png", "image/jpeg"]
    byte_length: int = Field(ge=1, le=OPENAI_CREATE_MAX_UPLOAD_BYTES)
    width_px: int = Field(ge=1, le=65_535)
    height_px: int = Field(ge=1, le=65_535)
    upload_field_name: Literal["file"] = OPENAI_CREATE_UPLOAD_FIELD

    @model_validator(mode="after")
    def exact_vault_reference(self) -> OpenAIAdsCreativeVaultBinding:
        if self.vault_ref != f"vault:sha256:{self.content_digest}":
            raise ValueError("the vault reference does not name its own content digest")
        return self


class OpenAIAdsCreateAccountBinding(StrictModel):
    """The observed ad account and the account-key material bound to it."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["OPENAI_ADS_V1_ACCOUNT_KEY_ACCOUNT"] = "OPENAI_ADS_V1_ACCOUNT_KEY_ACCOUNT"
    account_ref: SafeRef
    account_client_ref: SafeRef
    profile_ref: str = Field(min_length=1, max_length=160)
    credential_binding_ref: str = Field(pattern=r"^credential-binding:sha256:[0-9a-f]{64}$")
    account_currency: Literal["USD"]
    account_timezone: str = Field(min_length=1, max_length=80)
    account_status: Literal["active"] = "active"
    provider_api_version: Literal["v1"] = OPENAI_CREATE_API_VERSION


class OpenAIAdsChatCardHierarchyCreateSelection(StrictModel):
    """The additive ``plan execution create`` envelope member P04-OA introduces.

    The generic request carries a profile, provider, operation, account and one
    resource reference.  An OpenAI Ads chat-card CREATE needs three more things:
    the retained candidate this plan promotes, the *brief that candidate was
    compiled from*, and the private-vault image the upload child sends.  The
    brief is required because a retained ``CANDIDATE_COMPILED`` record
    deliberately keeps only the redacted review -- it declares
    ``requires_new_compilation_for_planning`` -- so the planner recompiles the
    supplied brief and refuses unless the recompiled candidate digest equals the
    retained one.
    """

    schema_version: Literal[1] = 1
    selection_kind: Literal["OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE"] = (
        "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE"
    )
    candidate_record_digest: Digest
    fresh: OpenAIAdsCampaignBrief
    creative_vault_ref: str = Field(pattern=r"^vault:sha256:[0-9a-f]{64}$")
    expires_in_seconds: int = Field(default=300, ge=60, le=900)


class OpenAIAdsCreateCandidateBinding(StrictModel):
    """The exact retained offline candidate this plan promotes."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["OPENAI_ADS_V1_RETAINED_CHAT_CARD_CANDIDATE"] = (
        "OPENAI_ADS_V1_RETAINED_CHAT_CARD_CANDIDATE"
    )
    candidate_record_digest: Digest
    candidate_digest: Digest
    spec_digest: Digest
    input_digest: Digest
    specification: OpenAIAdsCompiledCampaignSpec

    @model_validator(mode="after")
    def exact_recompiled_candidate(self) -> OpenAIAdsCreateCandidateBinding:
        if self.spec_digest != self.specification.spec_digest:
            raise ValueError("candidate binding does not describe its own specification")
        if self.input_digest != self.specification.input_digest:
            raise ValueError("candidate binding restates a different compiler input")
        if self.specification.planning_boundary.status != "OFFLINE_CANDIDATE_NOT_EXECUTABLE":
            raise ValueError("only an offline candidate can be promoted")
        if self.specification.campaign_request.bidding_type not in {"impressions", "clicks"}:
            # The compiled payload type already excludes ``conversions``.
            # Restating the enabled set here keeps a constructed or altered
            # candidate from carrying a feature-gated option into a plan.
            raise ValueError("conversion bidding is not an enabled option in this build")
        return self


class OpenAIAdsCreateObservationBinding(StrictModel):
    """One bounded sequential account observation interval and its checks."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["OPENAI_ADS_V1_CHAT_CARD_CREATE_OBSERVATION"] = (
        "OPENAI_ADS_V1_CHAT_CARD_CREATE_OBSERVATION"
    )
    campaign_name: str = Field(min_length=3, max_length=1_000)
    preconditions: OpenAIAdsCreatePreconditions
    creative: OpenAIAdsCreativeVaultBinding
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
    def bounded_interval(self) -> OpenAIAdsCreateObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("the create observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("the create observation interval exceeds the local bound")
        return self


class OpenAIAdsCreatePostcondition(StrictModel):
    """Closed configuration-only postcondition with no delivery prediction."""

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    campaign_name: str = Field(min_length=3, max_length=1_000)
    configured_status: Literal["paused"] = "paused"
    every_entity_configured_status: Literal["paused"] = "paused"
    ad_group_count: int = Field(ge=1, le=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS)
    ad_count: int = Field(ge=1, le=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS)
    member_count: int = Field(ge=3, le=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS)
    child_egress_count: int = Field(ge=4, le=MAX_CHILD_EGRESS_COUNT)
    predicted_hierarchy_digest: Digest
    creative_association: Literal["PROVEN_ONLY_BY_PAUSED_AD_READBACK"] = (
        "PROVEN_ONLY_BY_PAUSED_AD_READBACK"
    )
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    activation: Literal["SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"] = (
        "SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"
    )

    @model_validator(mode="after")
    def exact_counts(self) -> OpenAIAdsCreatePostcondition:
        if self.member_count != 1 + self.ad_group_count + self.ad_count:
            raise ValueError("the postcondition member count does not describe its own hierarchy")
        if self.child_egress_count != 1 + self.member_count:
            raise ValueError("the child egress count is one upload plus every created member")
        return self


class OpenAIAdsCreateRedactedReview(StrictModel):
    """The exact review the approval host renders for this operation.

    Every field is derived: the compiler's own redacted review supplies the
    counts and the spend bound, and this contract supplies the four statements a
    reviewer needs and the compiler cannot make -- that the operation is several
    ordered requests, that everything is created paused, that an uploaded file
    cannot be read back, and that the creative association is proven only by the
    paused ad's readback.
    """

    account_ref: SafeRef
    campaign_handle: str = Field(min_length=1, max_length=80)
    initial_state: Literal["PAUSED_AT_EVERY_LEVEL"] = "PAUSED_AT_EVERY_LEVEL"
    location_count: int = Field(ge=1)
    ad_group_count: int = Field(ge=1)
    ad_count: int = Field(ge=1)
    child_request_count: int = Field(ge=4, le=MAX_CHILD_EGRESS_COUNT)
    ordered_child_steps: tuple[str, ...] = Field(min_length=4, max_length=MAX_CHILD_EGRESS_COUNT)
    maximum_spend: Money
    activation_required: Literal[True] = True
    uploaded_file_readback: Literal["NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"] = (
        "NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"
    )
    file_association_proof: Literal["PAUSED_AD_READBACK_ONLY"] = "PAUSED_AD_READBACK_ONLY"
    partial_hierarchy_recovery: Literal["RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"] = (
        "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    )
    write_retry: Literal["NEVER_NO_DOCUMENTED_IDEMPOTENCY"] = "NEVER_NO_DOCUMENTED_IDEMPOTENCY"
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = (
        "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    )


def create_redacted_review(
    spec: OpenAIAdsCompiledCampaignSpec, children: tuple[OpenAIAdsCreateChild, ...]
) -> OpenAIAdsCreateRedactedReview:
    """Derive the review lines' source from the compiler's own redacted review."""

    review = spec.redacted_review
    return OpenAIAdsCreateRedactedReview(
        account_ref=review.account_ref,
        campaign_handle=review.campaign_handle,
        location_count=review.location_count,
        ad_group_count=review.ad_group_count,
        ad_count=review.ad_count,
        child_request_count=len(children),
        ordered_child_steps=tuple(child.label for child in children),
        maximum_spend=Money(
            minor_units=review.maximum_spend.minor_units,
            currency=review.maximum_spend.currency,
        ),
    )


def _postcondition(
    account: OpenAIAdsCreateAccountBinding,
    candidate: OpenAIAdsCreateCandidateBinding,
    creative: OpenAIAdsCreativeVaultBinding,
) -> OpenAIAdsCreatePostcondition:
    projection = projection_from_compiled_spec(
        candidate.specification, creative_asset_content_digest=creative.content_digest
    )
    return OpenAIAdsCreatePostcondition(
        account_ref=account.account_ref,
        campaign_name=projection.campaign_name,
        ad_group_count=projection.ad_group_count,
        ad_count=projection.ad_count,
        member_count=len(projection.members),
        child_egress_count=len(create_child_plan(candidate.specification)),
        predicted_hierarchy_digest=digest(projection.model_dump(mode="json", warnings="error")),
    )


def _action_id(
    *,
    workspace: OpenAIPauseWorkspaceBinding,
    account: OpenAIAdsCreateAccountBinding,
    candidate: OpenAIAdsCreateCandidateBinding,
    observation: OpenAIAdsCreateObservationBinding,
    postcondition: OpenAIAdsCreatePostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> str:
    return digest(
        {
            "kind": "openai-ads-chat-card-hierarchy-create-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "account": account.model_dump(mode="json", warnings="error"),
            "candidate": candidate.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "before": before.model_dump(mode="json", warnings="error"),
            "after": after.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": OPENAI_CREATE_PLAN_STATUS,
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_EVERY_CHILD",
            "recovery_semantics": "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE",
            "execution_authority": False,
            "live_validated": False,
            "conditional_write_proof": False,
            "idempotency_proof": False,
        }
    )


def _build_plan(
    *,
    action_id: str,
    workspace: OpenAIPauseWorkspaceBinding,
    account: OpenAIAdsCreateAccountBinding,
    candidate: OpenAIAdsCreateCandidateBinding,
    postcondition: OpenAIAdsCreatePostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> CompositePlan:
    identity = OpenAIAccountKeyIdentity(
        account_ref=account.account_ref,
        stable_account_id=account.account_client_ref,
        auth_profile_ref=account.profile_ref,
        credential_binding_ref=account.credential_binding_ref,
    )
    subplan = SubPlan(
        subplan_id=f"openai-ads-chat-card-hierarchy-create-{action_id}",
        provider="openai_ads",
        operation=OperationKind.CREATE,
        account=identity,
        provider_api_version=OPENAI_CREATE_API_VERSION,
        capability_version=OPENAI_CREATE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path="campaign/hierarchy", before=before, after=after),),
        # The candidate's own campaign lifetime cap, in minor units.  OpenAI's
        # documented budget object is a lifetime spend limit only, so there is no
        # daily amount and no overdelivery multiple to declare.
        max_spend=Money(
            minor_units=candidate.specification.activation_boundary.maximum_spend.minor_units,
            currency=candidate.specification.activation_boundary.maximum_spend.currency,
        ),
        egress=(EgressTarget(host=OPENAI_CREATE_HOST, purpose=OPENAI_CREATE_EGRESS_PURPOSE),),
        # Everything this route creates is paused, and rule 11 forbids deleting
        # a resource that is not proven unreferenced.  The declared disposition
        # is ``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``; ``SubPlan`` reserves its
        # explicit ``recovery_disposition`` for GA4 and requires at least one
        # provider-request operation, so the bounded compensating action named
        # here is ``PAUSE`` -- the only non-destructive one, and a no-op against
        # a hierarchy this route already created paused.
        recovery_actions=(OperationKind.PAUSE,),
        expected_child_egress_count=postcondition.child_egress_count,
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"openai-ads-chat-card-hierarchy-create-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: OpenAIAdsCreateAccountBinding,
    candidate: OpenAIAdsCreateCandidateBinding,
    observation: OpenAIAdsCreateObservationBinding,
    expires_at: datetime,
) -> None:
    specification = candidate.specification
    if specification.account_ref != account.account_ref:
        raise ValueError("the candidate was compiled for a different OpenAI ad account")
    if specification.account_currency != account.account_currency:
        raise ValueError("the candidate currency differs from the observed account currency")
    if specification.account_timezone != account.account_timezone:
        raise ValueError("the candidate timezone differs from the observed account timezone")
    if observation.campaign_name != specification.campaign_request.name:
        raise ValueError("the observation checked a different campaign name")
    digests = {ad.asset_content_digest for group in specification.ad_groups for ad in group.ads}
    if digests != {observation.creative.content_digest}:
        raise ValueError("every compiled ad must reference the one uploaded vault asset")
    if observation.read_finished_at >= expires_at:
        raise ValueError("plan expiry must follow the completed create read")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")


class OpenAIAdsChatCardHierarchyCreatePlannedAction(StrictModel):
    """One exact paused chat-card hierarchy CREATE, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = OPENAI_CREATE_PLAN_STATUS
    action_id: Digest
    workspace_binding: OpenAIPauseWorkspaceBinding
    account_binding: OpenAIAdsCreateAccountBinding
    candidate_binding: OpenAIAdsCreateCandidateBinding
    observation_binding: OpenAIAdsCreateObservationBinding
    postcondition: OpenAIAdsCreatePostcondition
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
    def exact_reconstructed_action(self) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only complete, closed, validated serializable action data."""

        checked = revalidate_openai_ads_create_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def build_openai_ads_chat_card_hierarchy_create_plan(
    *,
    workspace_profile_ref: str,
    account_binding: OpenAIAdsCreateAccountBinding,
    candidate_binding: OpenAIAdsCreateCandidateBinding,
    observation_binding: OpenAIAdsCreateObservationBinding,
    expires_at: datetime,
    fingerprint_key: bytes,
) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
    """Build the one non-authorizing paused chat-card hierarchy CREATE plan."""

    try:
        workspace = OpenAIPauseWorkspaceBinding(workspace_profile_ref=workspace_profile_ref)
        account = _checked_member(account_binding, OpenAIAdsCreateAccountBinding)
        candidate = _checked_member(candidate_binding, OpenAIAdsCreateCandidateBinding)
        observation = _checked_member(observation_binding, OpenAIAdsCreateObservationBinding)
        if observation.fingerprint_key_marker != fingerprint_key_marker(fingerprint_key):
            raise ValueError("the observation was projected under a different local key")
        expiry = _checked_expiry(expires_at)
        _assert_outer_bindings(
            account=account, candidate=candidate, observation=observation, expires_at=expiry
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
            workspace=workspace,
            account=account,
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
            candidate=candidate,
            postcondition=postcondition,
            before=before,
            after=after,
            expires_at=expiry,
        )
        return OpenAIAdsChatCardHierarchyCreatePlannedAction(
            action_id=action_id,
            workspace_binding=workspace,
            account_binding=account,
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
    account: OpenAIAdsCreateAccountBinding,
    candidate: OpenAIAdsCreateCandidateBinding,
    creative: OpenAIAdsCreativeVaultBinding,
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
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction, *, fingerprint_key: bytes
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
    if type(value) is OpenAIAdsChatCardHierarchyCreatePlannedAction:
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
) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
    if set(data) != _ACTION_FIELDS or any(type(key) is not str for key in data):
        raise ValueError("action keys")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("action schema version")
    if data["status"] != OPENAI_CREATE_PLAN_STATUS:
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
        return OpenAIAdsChatCardHierarchyCreatePlannedAction(
            action_id=data["action_id"],
            workspace_binding=cast(
                OpenAIPauseWorkspaceBinding,
                _raw_member(data, "workspace_binding", OpenAIPauseWorkspaceBinding),
            ),
            account_binding=cast(
                OpenAIAdsCreateAccountBinding,
                _raw_member(data, "account_binding", OpenAIAdsCreateAccountBinding),
            ),
            candidate_binding=cast(
                OpenAIAdsCreateCandidateBinding,
                _raw_member(data, "candidate_binding", OpenAIAdsCreateCandidateBinding),
            ),
            observation_binding=cast(
                OpenAIAdsCreateObservationBinding,
                _raw_member(data, "observation_binding", OpenAIAdsCreateObservationBinding),
            ),
            postcondition=cast(
                OpenAIAdsCreatePostcondition,
                _raw_member(data, "postcondition", OpenAIAdsCreatePostcondition),
            ),
            plan=cast(CompositePlan, _raw_member(data, "plan", CompositePlan)),
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("planned action") from None


def _assert_action_instance(action: OpenAIAdsChatCardHierarchyCreatePlannedAction) -> None:
    """Reconstruct every derived fact, using the action's own retained states.

    The semantic states are keyed by a local key this pure contract must not
    read, so revalidation checks their *structural* binding here -- the plan's
    ``before``/``after``, the postcondition's predicted hierarchy digest, and the
    action ID -- while the configured planner and the guarded adapter each
    recompute the fingerprints under the current key.
    """

    if type(action) is not OpenAIAdsChatCardHierarchyCreatePlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, OpenAIPauseWorkspaceBinding)
    account = _checked_member(action.account_binding, OpenAIAdsCreateAccountBinding)
    candidate = _checked_member(action.candidate_binding, OpenAIAdsCreateCandidateBinding)
    observation = _checked_member(action.observation_binding, OpenAIAdsCreateObservationBinding)
    postcondition = _checked_member(action.postcondition, OpenAIAdsCreatePostcondition)
    plan = _checked_member(action.plan, CompositePlan)
    expiry = _checked_expiry(plan.expires_at)
    _assert_outer_bindings(
        account=account, candidate=candidate, observation=observation, expires_at=expiry
    )
    expected_child_egress_count(candidate.specification)
    if postcondition != _postcondition(account, candidate, observation.creative):
        raise ValueError("postcondition differs from the exact candidate")
    if len(plan.subplans) != 1:
        raise ValueError("a create action carries exactly one subplan")
    subplan = plan.subplans[0]
    expected_action_id = _action_id(
        workspace=workspace,
        account=account,
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
        candidate=candidate,
        postcondition=postcondition,
        before=subplan.before,
        after=subplan.after,
        expires_at=expiry,
    )
    if plan != expected_plan:
        raise ValueError("plan differs from the reconstructed OpenAI Ads create action")
    if subplan.before == subplan.after:
        raise ValueError("a create plan must change the observed hierarchy state")
    if subplan.before.revision != _STATE_KIND or subplan.after.revision != _STATE_KIND:
        raise ValueError("create states must be semantic hierarchy projections")


def revalidate_openai_ads_create_planned_action(
    value: object,
) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
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


class OpenAIAdsChatCardHierarchyCreatePlanRecord(StrictModel):
    """The additive ``changes`` record that retains one closed CREATE action."""

    schema_version: Literal[1] = 1
    event_type: Literal["OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN"] = (
        "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN"
    )
    provider: Literal["openai_ads"] = "openai_ads"
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction
    action_digest: Digest
    plan_digest: Digest
    candidate_digest: Digest
    expected_child_egress_count: int = Field(ge=4, le=MAX_CHILD_EGRESS_COUNT)
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
    def closed_action(cls, value: object) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
        return revalidate_openai_ads_create_planned_action(value)

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_action_binding(self) -> OpenAIAdsChatCardHierarchyCreatePlanRecord:
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
            raise ValueError("retained OpenAI Ads CREATE plan facts do not match the action")
        return self


def openai_ads_create_plan_record(
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
) -> OpenAIAdsChatCardHierarchyCreatePlanRecord:
    """Build the retained record for one already-closed CREATE action."""

    checked = revalidate_openai_ads_create_planned_action(action)
    return OpenAIAdsChatCardHierarchyCreatePlanRecord(
        action=checked,
        action_digest=digest(checked.model_dump(mode="json", warnings="error")),
        plan_digest=checked.plan.plan_digest,
        candidate_digest=checked.candidate_binding.candidate_digest,
        expected_child_egress_count=checked.plan.subplans[0].expected_child_egress_count,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_openai_ads_create_plan_record(
    value: object,
) -> OpenAIAdsChatCardHierarchyCreatePlanRecord:
    """Reject incomplete, aliased, or incoherent retained CREATE plan facts."""

    try:
        if type(value) is OpenAIAdsChatCardHierarchyCreatePlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            OpenAIAdsChatCardHierarchyCreatePlanRecord.model_fields
        ):
            raise ValueError("a complete record is required")
        encoded = _strict_json_bytes(value, label="plan record")
        return OpenAIAdsChatCardHierarchyCreatePlanRecord.model_validate_json(encoded)
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_plan(
            "A complete closed OpenAI Ads CREATE plan record is required."
        ) from None


def openai_ads_create_plan_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive plan kind."""

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(OPENAI_CREATE_PLAN_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is OpenAIAdsChatCardHierarchyCreatePlanRecord


def checked_file_identifier(value: object) -> str:
    """Accept only the documented opaque Ads file identifier shape.

    The Files reference's response example is ``{"file_id": "file_901"}``
    (re-verified 2026-09-09).  The ``file_`` prefix is taken from that one
    example and is a local narrowing, not a documented grammar.
    """

    if type(value) is not str or _FILE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("upload acknowledgement file identifier")
    return value


def checked_native_identifier(value: object) -> str:
    """Accept only a bounded opaque native resource identifier."""

    if type(value) is not str or _NATIVE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("native resource identifier")
    return value


__all__ = [
    "FILE_ASSOCIATION_NOT_PROVEN",
    "OPENAI_CREATE_API_VERSION",
    "OPENAI_CREATE_CAPABILITY_VERSION",
    "OPENAI_CREATE_CHILD_LABELS",
    "OPENAI_CREATE_EGRESS_PURPOSE",
    "OPENAI_CREATE_HOST",
    "OPENAI_CREATE_MAX_HIERARCHY_MEMBERS",
    "OPENAI_CREATE_MAX_UPLOAD_BYTES",
    "OPENAI_CREATE_MEDIA_TYPES",
    "OPENAI_CREATE_PLAN_RECORD_KIND",
    "OPENAI_CREATE_PLAN_STATUS",
    "OPENAI_CREATE_UPLOAD_FIELD",
    "OpenAIAdsChatCardHierarchyCreatePlanRecord",
    "OpenAIAdsChatCardHierarchyCreatePlannedAction",
    "OpenAIAdsChatCardHierarchyCreateSelection",
    "OpenAIAdsChatCardHierarchyProjection",
    "OpenAIAdsCreateAccountBinding",
    "OpenAIAdsCreateCandidateBinding",
    "OpenAIAdsCreateChild",
    "OpenAIAdsCreateObservationBinding",
    "OpenAIAdsCreatePostcondition",
    "OpenAIAdsCreatePreconditions",
    "OpenAIAdsCreateRedactedReview",
    "OpenAIAdsCreativeVaultBinding",
    "OpenAIAdsHierarchyMember",
    "build_openai_ads_chat_card_hierarchy_create_plan",
    "checked_file_identifier",
    "checked_native_identifier",
    "create_child_plan",
    "create_redacted_review",
    "expected_child_egress_count",
    "fingerprint_key_marker",
    "openai_ads_create_plan_record",
    "openai_ads_create_plan_record_kind_installed",
    "predicted_states",
    "projection_from_compiled_spec",
    "revalidate_openai_ads_create_plan_record",
    "revalidate_openai_ads_create_planned_action",
    "semantic_hierarchy_state",
    "semantic_state_from_members",
    "semantic_target_fingerprint",
    "targeted_location_digest",
]

"""One exact paused Google Ads v25 Search/RSA hierarchy CREATE, without authority.

This is the P04 contract layer.  It turns one *retained* offline compiler
candidate (:mod:`adhelm.providers.google_ads_compiler`) plus one fresh bounded
account read into a closed :class:`GoogleAdsSearchCampaignCreatePlannedAction`
whose ``plan`` is the ordinary provider-neutral
:class:`~adhelm.safety.CompositePlan` the kernel executes.

Two things are deliberately different from the M55 PAUSE contract:

* Google assigns every native identifier at creation time, so ``before`` and
  ``after`` are **semantic** hierarchy fingerprints from
  :mod:`adhelm.providers._semantic_state`: locally keyed HMACs of the
  request-controllable names and configuration only.  ``before`` is the empty
  hierarchy under one ``(customer, campaign name)`` binding -- "no campaign
  with this name in this customer" -- and ``after`` is the exact predicted
  member set.  No native ID enters either side, so the post-create readback can
  be compared against the prediction.
* The candidate is retained history the planner must *re-derive*.  A retained
  ``CANDIDATE_COMPILED`` record deliberately keeps only the redacted review, so
  the planner recompiles the supplied brief and refuses unless the recompiled
  candidate digest equals the retained one.

Nothing here reads a credential, contacts Google, or authorizes anything.  A
matching fingerprint proves neither provider origin, freshness, permission,
reservation, execution authority, nor a mutation outcome.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import SafeRef, StrictModel
from adhelm.providers._semantic_state import (
    SemanticHierarchySpec,
    collection_state,
    keyed_fingerprint,
    member_fingerprint,
)
from adhelm.providers.google_ads_compiler import (
    GoogleAdsCompiledCampaignSpec,
    GoogleAdsNativeLanguageCriterionPayload,
    GoogleAdsNativeLocationCriterionPayload,
    GoogleAdsNativeScheduleCriterionPayload,
    GoogleAdsSearchCampaignBrief,
)
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseExecutionBinding,
    GoogleAdsCampaignPauseWorkspaceBinding,
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
#: additive in ``changes`` and mirrors ``GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN``.
GOOGLE_ADS_CREATE_PLAN_RECORD_KIND: Final = "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN"

GOOGLE_ADS_CREATE_PLAN_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
GOOGLE_ADS_CREATE_HOST: Final[Literal["googleads.googleapis.com"]] = "googleads.googleapis.com"
GOOGLE_ADS_CREATE_API_VERSION: Final[Literal["v25"]] = "v25"
GOOGLE_ADS_CREATE_CAPABILITY_VERSION: Final = "google-ads-v25-search-rsa-create-contract-2026-09-09"
GOOGLE_ADS_CREATE_EGRESS_PURPOSE: Final = "atomic Google Ads v25 paused Search hierarchy create"
GOOGLE_ADS_CREATE_SCOPE: Final[Literal["https://www.googleapis.com/auth/adwords"]] = (
    "https://www.googleapis.com/auth/adwords"
)

_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_CUSTOMER_ID: Final = re.compile(r"^[0-9]{10}$", re.ASCII)
_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 1_048_576

#: The GAQL string-literal characters this build refuses to construct.  A
#: campaign name carrying one is refused *before* any egress rather than
#: escaped, because this repository builds no escaped GAQL literal.
GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS: Final = frozenset("'\"\\`")

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

# -- the semantic projection -------------------------------------------------

_FINGERPRINT_CONTEXT: Final = b"adhelm/google-ads/search-campaign-create/semantic/v1\x00"
_STATE_KIND: Final = "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_SEMANTIC_HIERARCHY_V1"
_SEMANTIC_PROJECTION: Final = "NAME_HMAC_CONFIGURATION_EXACT_V1"
_SEMANTIC_SPEC: Final = SemanticHierarchySpec(
    context=_FINGERPRINT_CONTEXT,
    state_kind=_STATE_KIND,
    semantic_projection=_SEMANTIC_PROJECTION,
    target_purpose="google-ads-search-campaign-create-semantic-target",
    member_purpose="google-ads-search-campaign-create-semantic-member",
    collection_purpose="google-ads-search-campaign-create-semantic-collection",
)
_IDENTITY_PURPOSE: Final = "google-ads-search-campaign-create-semantic-identity"
_KEY_MARKER_PURPOSE: Final = "google-ads-search-campaign-create-fingerprint-key-marker"
#: The identity slot name every member uses.  ``name`` is a reserved semantic
#: key, so the projection names the request-controllable identity role instead.
_IDENTITY_FIELD: Final = "semantic_identity"


def _invalid_plan(message: str = "Google Ads Search CREATE plan data is invalid.") -> AdhelmError:
    return AdhelmError("INVALID_GOOGLE_ADS_CREATE_PLAN", message, exit_code=ExitCode.INVALID_INPUT)


class GoogleAdsSearchHierarchyMember(StrictModel):
    """One request-controllable member of a paused Search hierarchy.

    ``identity`` is whatever uniquely names the member to a *request*: a
    configured name, a target constant, a keyword pair, or the creative content
    digest.  ``configuration`` is the remaining request-controllable values.
    Neither may carry a provider-assigned identifier; ``_semantic_state``
    rejects the common shapes and this projection keeps identifiers out by
    hashing the identity separately.
    """

    member_kind: Literal[
        "CAMPAIGN_BUDGET",
        "CAMPAIGN",
        "CAMPAIGN_CRITERION_LOCATION",
        "CAMPAIGN_CRITERION_LANGUAGE",
        "CAMPAIGN_CRITERION_AD_SCHEDULE",
        "AD_GROUP",
        "AD_GROUP_CRITERION_KEYWORD",
        "AD_GROUP_AD",
    ]
    identity: tuple[str, ...] = Field(min_length=1, max_length=6)
    configuration: tuple[tuple[str, str], ...] = Field(max_length=16)

    @model_validator(mode="after")
    def bounded_projection_member(self) -> GoogleAdsSearchHierarchyMember:
        keys = tuple(key for key, _value in self.configuration)
        if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
            raise ValueError("member configuration keys must be unique and sorted")
        if any(not key or len(key) > 64 for key in keys):
            raise ValueError("member configuration keys are bounded")
        if any(len(value) > 256 for _key, value in self.configuration):
            raise ValueError("member configuration values are bounded")
        if any(len(item) > 512 for item in self.identity):
            raise ValueError("member identity components are bounded")
        return self


class GoogleAdsSearchHierarchyProjection(StrictModel):
    """The exact member multiset one paused Search hierarchy is projected to.

    Both the compiled candidate and the post-create readback are reduced to
    this one shape, so a predicted and an observed fingerprint are comparable
    by construction rather than by two hand-written hashers agreeing.
    """

    schema_version: Literal[1] = 1
    projection_kind: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_HIERARCHY"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_HIERARCHY"
    )
    campaign_name: str = Field(min_length=1, max_length=255)
    members: tuple[GoogleAdsSearchHierarchyMember, ...] = Field(min_length=6, max_length=140)

    @model_validator(mode="after")
    def every_member_is_paused(self) -> GoogleAdsSearchHierarchyProjection:
        for member in self.members:
            configuration = dict(member.configuration)
            if member.member_kind == "CAMPAIGN_BUDGET":
                continue
            if configuration.get("status") != "PAUSED":
                raise ValueError("every created Google Ads entity must be PAUSED")
        kinds = {member.member_kind for member in self.members}
        required = {"CAMPAIGN_BUDGET", "CAMPAIGN", "AD_GROUP", "AD_GROUP_AD"}
        if not required <= kinds:
            raise ValueError("a Search hierarchy projection is missing a required member")
        for kind in required:
            if sum(member.member_kind == kind for member in self.members) != 1:
                raise ValueError("a Search hierarchy projection has exactly one of each root")
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
    """Bind one projection to the exact customer and campaign name it describes."""

    if _CUSTOMER_ID.fullmatch(account_client_ref) is None:
        raise ValueError("semantic target requires the exact customer ID")
    return keyed_fingerprint(
        fingerprint_key,
        context=_FINGERPRINT_CONTEXT,
        purpose=_SEMANTIC_SPEC.target_purpose,
        value={
            "schema_version": 1,
            "state_kind": _STATE_KIND,
            "provider": "google_ads",
            "provider_api_version": GOOGLE_ADS_CREATE_API_VERSION,
            "customer_client_ref": account_client_ref,
            "campaign_name_fingerprint": _identity_fingerprint(
                ("campaign_name", campaign_name), fingerprint_key=fingerprint_key
            ),
        },
    )


def _member_fingerprints(
    projection: GoogleAdsSearchHierarchyProjection, *, fingerprint_key: bytes
) -> tuple[str, ...]:
    fingerprints = [
        member_fingerprint(
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
        for member in projection.members
    ]
    return tuple(sorted(fingerprints))


def semantic_hierarchy_state(
    projection: GoogleAdsSearchHierarchyProjection | None,
    *,
    account_client_ref: str,
    campaign_name: str,
    fingerprint_key: bytes,
) -> StateRef:
    """Project one hierarchy -- or its absence -- into a comparable state.

    ``None`` is the deliberate ``before`` of a CREATE: the empty member set
    under this exact ``(customer, campaign name)`` binding, which is the local
    statement "no campaign with this name in this customer".
    """

    target = semantic_target_fingerprint(
        account_client_ref=account_client_ref,
        campaign_name=campaign_name,
        fingerprint_key=fingerprint_key,
    )
    members: tuple[str, ...] = ()
    if projection is not None:
        if type(projection) is not GoogleAdsSearchHierarchyProjection:
            raise ValueError("semantic projection type")
        if projection.campaign_name != campaign_name:
            raise ValueError("semantic projection names a different campaign")
        members = _member_fingerprints(projection, fingerprint_key=fingerprint_key)
    return collection_state(
        _SEMANTIC_SPEC,
        binding_fingerprint=target,
        fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
        member_fingerprints=members,
        fingerprint_key=fingerprint_key,
    )


def _schedule_identity(schedule: Any) -> tuple[str, ...]:
    return (
        schedule.day_of_week,
        str(schedule.start_hour),
        schedule.start_minute,
        str(schedule.end_hour),
        schedule.end_minute,
    )


def projection_from_compiled_spec(
    spec: GoogleAdsCompiledCampaignSpec,
) -> GoogleAdsSearchHierarchyProjection:
    """Reduce one compiled candidate to its exact predicted member multiset."""

    if type(spec) is not GoogleAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    campaign = spec.campaign_request
    network = campaign.network_settings
    members: list[GoogleAdsSearchHierarchyMember] = [
        GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN_BUDGET",
            # A non-shared budget inherits the campaign's name, so the campaign
            # name is the only request-controllable identity it has.
            identity=(campaign.name,),
            configuration=(
                ("delivery_method", spec.budget.request.delivery_method),
                ("explicitly_shared", "false"),
                ("period", spec.budget.request.period),
                ("total_amount_micros", str(spec.budget.request.total_amount_micros)),
            ),
        ),
        GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN",
            identity=(campaign.name,),
            configuration=(
                ("advertising_channel_type", campaign.advertising_channel_type),
                ("bidding_strategy", "TARGET_SPEND"),
                ("end_date_time", campaign.end_date_time),
                ("start_date_time", campaign.start_date_time),
                ("status", campaign.status),
                ("target_content_network", _flag(network.target_content_network)),
                ("target_google_search", _flag(network.target_google_search)),
                ("target_google_tv_network", _flag(network.target_google_tv_network)),
                ("target_partner_search_network", _flag(network.target_partner_search_network)),
                ("target_search_network", _flag(network.target_search_network)),
                ("target_youtube", _flag(network.target_youtube)),
            ),
        ),
    ]
    for criterion in spec.campaign_criteria:
        request = criterion.request
        if isinstance(request, GoogleAdsNativeLocationCriterionPayload):
            members.append(
                GoogleAdsSearchHierarchyMember(
                    member_kind="CAMPAIGN_CRITERION_LOCATION",
                    identity=(request.location.geo_target_constant,),
                    configuration=(("negative", "false"), ("status", request.status)),
                )
            )
        elif isinstance(request, GoogleAdsNativeLanguageCriterionPayload):
            members.append(
                GoogleAdsSearchHierarchyMember(
                    member_kind="CAMPAIGN_CRITERION_LANGUAGE",
                    identity=(request.language.language_constant,),
                    configuration=(("negative", "false"), ("status", request.status)),
                )
            )
        elif isinstance(request, GoogleAdsNativeScheduleCriterionPayload):
            members.append(
                GoogleAdsSearchHierarchyMember(
                    member_kind="CAMPAIGN_CRITERION_AD_SCHEDULE",
                    identity=_schedule_identity(request.ad_schedule),
                    configuration=(("negative", "false"), ("status", request.status)),
                )
            )
        else:  # pragma: no cover - the compiled union has exactly three members
            raise ValueError("unknown compiled campaign criterion")
    members.append(
        GoogleAdsSearchHierarchyMember(
            member_kind="AD_GROUP",
            identity=(spec.ad_group.request.name,),
            configuration=(
                ("status", spec.ad_group.request.status),
                ("type", spec.ad_group.request.type),
            ),
        )
    )
    for keyword in spec.keywords:
        members.append(
            GoogleAdsSearchHierarchyMember(
                member_kind="AD_GROUP_CRITERION_KEYWORD",
                identity=(keyword.request.keyword.text, keyword.request.keyword.match_type),
                configuration=(("negative", "false"), ("status", keyword.request.status)),
            )
        )
    ad = spec.responsive_search_ad.request.ad
    members.append(
        GoogleAdsSearchHierarchyMember(
            member_kind="AD_GROUP_AD",
            identity=(
                rsa_text_digest(
                    headlines=tuple(item.text for item in ad.responsive_search_ad.headlines),
                    descriptions=tuple(item.text for item in ad.responsive_search_ad.descriptions),
                    final_urls=ad.final_urls,
                ),
            ),
            configuration=(
                ("description_count", str(len(ad.responsive_search_ad.descriptions))),
                ("headline_count", str(len(ad.responsive_search_ad.headlines))),
                ("status", spec.responsive_search_ad.request.status),
            ),
        )
    )
    return GoogleAdsSearchHierarchyProjection(campaign_name=campaign.name, members=tuple(members))


def _flag(value: bool) -> str:
    return "true" if value else "false"


def rsa_text_digest(
    *, headlines: tuple[str, ...], descriptions: tuple[str, ...], final_urls: tuple[str, ...]
) -> str:
    """Digest the exact provider-observable Responsive Search Ad text surface.

    The compiler's own ``creative_content_digest`` additionally commits the
    declared CTA headline and the local creative-version identity, neither of
    which Google returns.  A semantic projection must bind the surface both
    sides can actually observe, in the order they observe it, so a predicted
    ``after`` stays comparable with a post-create readback.
    """

    return digest(
        {
            "kind": "google-ads-v25-responsive-search-ad-text-v1",
            "headlines": list(headlines),
            "descriptions": list(descriptions),
            "final_urls": list(final_urls),
        }
    )


# -- the fresh bounded read --------------------------------------------------


class GoogleAdsAssetAutomationObservation(StrictModel):
    """One observed account-level asset-automation setting, verbatim."""

    asset_automation_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    asset_automation_status: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")


class GoogleAdsSearchCampaignCreatePreconditions(StrictModel):
    """The bounded promotion checks this workspace actually performed.

    Two of the compiler contract's required promotion checks are recorded as
    explicit statuses rather than values, because v25 exposes no account-scoped
    surface for them before the campaign exists.  Saying so is the point: the
    plan refuses to imply a check it did not make, and the readback records the
    provider-chosen geo mode after the create.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_SEARCH_CREATE_PRECONDITIONS"] = (
        "GOOGLE_ADS_V25_SEARCH_CREATE_PRECONDITIONS"
    )
    campaign_name_collision: Literal["NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"] = (
        "NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"
    )
    observed_campaign_name_matches: Literal[0] = 0
    #: The bounded near-variant probe.  One local rule governs both sides of
    #: every name comparison this route makes: **no normalization at all**.  The
    #: ``before`` projection hashes the campaign name byte for byte and the GAQL
    #: probe asks for byte-exact literals, so a plan can never be executed
    #: against a differently normalized name.  The near-variant probe is a
    #: *separate, additional* refusal on top of that, because Google's own
    #: comparison semantics for ``campaign.name`` are undocumented and some
    #: Google Ads surfaces treat names case-insensitively.
    campaign_name_near_match: Literal["NO_NEAR_MATCH_CAMPAIGN_NAME_OBSERVED"] = (
        "NO_NEAR_MATCH_CAMPAIGN_NAME_OBSERVED"
    )
    observed_near_match_names: Literal[0] = 0
    probed_near_match_names: int = Field(ge=0, le=64)
    name_comparison: Literal["BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE"] = (
        "BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE"
    )
    provider_name_comparison_semantics: Literal["UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER"] = (
        "UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER"
    )
    #: v25 has no account-scoped campaign-budget quota resource this build can
    #: read.  ``UNVERIFIED`` in the research contract; refused as a claim here.
    campaign_budget_quota: Literal["NOT_READABLE_NO_PROVIDER_SURFACE_IN_THIS_BUILD"] = (
        "NOT_READABLE_NO_PROVIDER_SURFACE_IN_THIS_BUILD"
    )
    #: ``geo_target_type_setting`` is a Campaign field only.  The candidate does
    #: not send it, so the provider default applies and is read back after the
    #: create rather than predicted or authorized here.
    geo_target_type_setting: Literal["NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE"] = (
        "NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE"
    )
    #: Google documents the *account* asset-automation control as configurable
    #: only in the Google Ads web interface, and v25 exposes no account-scoped
    #: resource for it.  Saying so is the point: this plan makes no claim about
    #: it.  ``Campaign.asset_automation_settings`` is a campaign field the
    #: candidate never requests, so it is read back after the create instead.
    asset_automation_account_setting: Literal["NOT_READABLE_WEB_INTERFACE_ONLY_IN_V25"] = (
        "NOT_READABLE_WEB_INTERFACE_ONLY_IN_V25"
    )
    asset_automation_campaign_setting: Literal[
        "NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE"
    ] = "NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE"
    asset_automation_effect_on_text: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"

    @field_validator("observed_campaign_name_matches", "observed_near_match_names", mode="before")
    @classmethod
    def exact_zero(cls, value: object) -> int:
        if type(value) is not int or value != 0:
            raise ValueError("a create plan requires zero observed name collisions")
        return value

    @field_validator("probed_near_match_names", mode="before")
    @classmethod
    def exact_probe_count(cls, value: object) -> int:
        if type(value) is not int or not 0 <= value <= 64:
            raise ValueError("the near-variant probe count is bounded")
        return value


class GoogleAdsSearchCampaignCreateSelection(StrictModel):
    """The additive ``plan execution create`` envelope member P04 introduces.

    The generic request carries a profile, a provider, an operation, an account
    and one resource reference.  A Google Ads Search CREATE needs two more
    things and the envelope's own docstring anticipates exactly this: the
    retained candidate this plan promotes, and the *brief that candidate was
    compiled from*.  The brief is required because a retained
    ``CANDIDATE_COMPILED`` record deliberately keeps only the redacted review --
    it declares ``requires_new_compilation_for_planning`` -- so the planner
    recompiles the supplied brief and refuses unless the recompiled candidate
    digest equals the retained one.
    """

    schema_version: Literal[1] = 1
    selection_kind: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE"
    )
    candidate_record_digest: Digest
    fresh: GoogleAdsSearchCampaignBrief
    expires_in_seconds: int = Field(default=300, ge=60, le=900)


class GoogleAdsSearchCampaignCreateCandidateBinding(StrictModel):
    """The exact retained offline candidate this plan promotes."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_RETAINED_SEARCH_CANDIDATE"] = (
        "GOOGLE_ADS_V25_RETAINED_SEARCH_CANDIDATE"
    )
    candidate_record_digest: Digest
    candidate_digest: Digest
    spec_digest: Digest
    input_digest: Digest
    specification: GoogleAdsCompiledCampaignSpec

    @model_validator(mode="after")
    def exact_recompiled_candidate(self) -> GoogleAdsSearchCampaignCreateCandidateBinding:
        if self.spec_digest != self.specification.spec_digest:
            raise ValueError("candidate binding does not describe its own specification")
        if self.input_digest != self.specification.input_digest:
            raise ValueError("candidate binding restates a different compiler input")
        if self.specification.planning_boundary.status != "OFFLINE_CANDIDATE_NOT_EXECUTABLE":
            raise ValueError("only an offline candidate can be promoted")
        return self


class GoogleAdsSearchCampaignCreateObservationBinding(StrictModel):
    """One bounded sequential account observation interval and its checks."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_SEARCH_CREATE_OBSERVATION"] = (
        "GOOGLE_ADS_V25_SEARCH_CREATE_OBSERVATION"
    )
    campaign_name: str = Field(min_length=1, max_length=255)
    preconditions: GoogleAdsSearchCampaignCreatePreconditions
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
    def bounded_interval(self) -> GoogleAdsSearchCampaignCreateObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("the create observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("the create observation interval exceeds the local bound")
        if GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(self.campaign_name):
            raise ValueError("this build refuses to construct a quoted GAQL campaign name")
        return self


class GoogleAdsSearchCampaignCreatePostcondition(StrictModel):
    """Closed configuration-only postcondition with no delivery prediction."""

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    campaign_name: str = Field(min_length=1, max_length=255)
    configured_status: Literal["PAUSED"] = "PAUSED"
    every_entity_configured_status: Literal["PAUSED"] = "PAUSED"
    member_count: int = Field(ge=6, le=140)
    predicted_hierarchy_digest: Digest
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    activation: Literal["SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"] = (
        "SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"
    )

    @model_validator(mode="after")
    def exact_account_reference(self) -> GoogleAdsSearchCampaignCreatePostcondition:
        if self.account_ref != f"google_ads.customer.{self.account_client_ref}":
            raise ValueError("postcondition references do not bind the Google Ads customer")
        return self


def _postcondition(
    account: GoogleAdsCampaignPauseAccountBinding,
    candidate: GoogleAdsSearchCampaignCreateCandidateBinding,
) -> GoogleAdsSearchCampaignCreatePostcondition:
    projection = projection_from_compiled_spec(candidate.specification)
    return GoogleAdsSearchCampaignCreatePostcondition(
        account_ref=account.account_ref,
        account_client_ref=account.account_client_ref,
        campaign_name=projection.campaign_name,
        member_count=len(projection.members),
        predicted_hierarchy_digest=digest(projection.model_dump(mode="json", warnings="error")),
    )


def _action_id(
    *,
    workspace: GoogleAdsCampaignPauseWorkspaceBinding,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    candidate: GoogleAdsSearchCampaignCreateCandidateBinding,
    observation: GoogleAdsSearchCampaignCreateObservationBinding,
    postcondition: GoogleAdsSearchCampaignCreatePostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> str:
    return digest(
        {
            "kind": "google-ads-search-campaign-create-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "account": account.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "candidate": candidate.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "before": before.model_dump(mode="json", warnings="error"),
            "after": after.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": GOOGLE_ADS_CREATE_PLAN_STATUS,
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_CREATE",
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
    workspace: GoogleAdsCampaignPauseWorkspaceBinding,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    candidate: GoogleAdsSearchCampaignCreateCandidateBinding,
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
        scopes=(GOOGLE_ADS_CREATE_SCOPE,),
    )
    subplan = SubPlan(
        subplan_id=f"google-ads-search-campaign-create-{action_id}",
        provider="google_ads",
        operation=OperationKind.CREATE,
        account=identity,
        provider_api_version=GOOGLE_ADS_CREATE_API_VERSION,
        capability_version=GOOGLE_ADS_CREATE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path="campaign/hierarchy", before=before, after=after),),
        # The candidate's own campaign-lifetime cap.  The v25 budget is
        # ``CUSTOM_PERIOD`` with ``total_amount_micros`` only, so there is no
        # average-daily amount and no up-to-2x daily overdelivery to declare.
        max_spend=Money(
            minor_units=candidate.specification.maximum_spend.minor_units,
            currency=candidate.specification.maximum_spend.currency,
        ),
        egress=(
            EgressTarget(host=GOOGLE_ADS_CREATE_HOST, purpose=GOOGLE_ADS_CREATE_EGRESS_PURPOSE),
        ),
        # Google creates the whole hierarchy in one atomic mutate, so a partial
        # child hierarchy is not a reachable outcome.  The disposition this
        # plan actually declares is ``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``:
        # everything created is PAUSED and rule 11 forbids deleting it.
        # ``SubPlan`` requires at least one provider-request operation and
        # refuses ``RECOVER``, so the bounded compensating action named here is
        # ``PAUSE`` -- the only non-destructive one, and a no-op against a
        # hierarchy this route already created paused.
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"google-ads-search-campaign-create-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    candidate: GoogleAdsSearchCampaignCreateCandidateBinding,
    observation: GoogleAdsSearchCampaignCreateObservationBinding,
    expires_at: datetime,
) -> None:
    specification = candidate.specification
    if specification.account_ref != account.account_ref:
        raise ValueError("the candidate was compiled for a different Google Ads customer")
    if specification.account_currency != account.currency:
        raise ValueError("the candidate currency differs from the observed customer currency")
    if specification.account_timezone != account.timezone:
        raise ValueError("the candidate timezone differs from the observed customer timezone")
    if account.account_status != "ENABLED":
        raise ValueError("a create plan requires an ENABLED Google Ads customer")
    if observation.campaign_name != specification.campaign_request.name:
        raise ValueError("the observation checked a different campaign name")
    if execution.resolved_at > observation.read_started_at:
        raise ValueError("EDIT association occurs after the create read began")
    if observation.read_finished_at >= expires_at:
        raise ValueError("plan expiry must follow the completed create read")
    if expires_at > execution.expires_at:
        raise ValueError("plan expiry exceeds the declared bearer expiry")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")


class GoogleAdsSearchCampaignCreatePlannedAction(StrictModel):
    """One exact paused Search hierarchy CREATE, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = GOOGLE_ADS_CREATE_PLAN_STATUS
    action_id: Digest
    workspace_binding: GoogleAdsCampaignPauseWorkspaceBinding
    account_binding: GoogleAdsCampaignPauseAccountBinding
    execution_binding: GoogleAdsCampaignPauseExecutionBinding
    candidate_binding: GoogleAdsSearchCampaignCreateCandidateBinding
    observation_binding: GoogleAdsSearchCampaignCreateObservationBinding
    postcondition: GoogleAdsSearchCampaignCreatePostcondition
    plan: CompositePlan
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    configuration_readback: Literal["REQUIRED_AFTER_CREATE"] = "REQUIRED_AFTER_CREATE"
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
    def exact_reconstructed_action(self) -> GoogleAdsSearchCampaignCreatePlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only complete, closed, validated serializable action data."""

        checked = revalidate_google_ads_search_campaign_create_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def build_google_ads_search_campaign_create_plan(
    *,
    workspace_profile_ref: str,
    account_binding: GoogleAdsCampaignPauseAccountBinding,
    execution_binding: GoogleAdsCampaignPauseExecutionBinding,
    candidate_binding: GoogleAdsSearchCampaignCreateCandidateBinding,
    observation_binding: GoogleAdsSearchCampaignCreateObservationBinding,
    expires_at: datetime,
    fingerprint_key: bytes,
) -> GoogleAdsSearchCampaignCreatePlannedAction:
    """Build the one non-authorizing paused Search hierarchy CREATE plan."""

    try:
        workspace = GoogleAdsCampaignPauseWorkspaceBinding(
            workspace_profile_ref=workspace_profile_ref
        )
        account = _checked_member(account_binding, GoogleAdsCampaignPauseAccountBinding)
        execution = _checked_member(execution_binding, GoogleAdsCampaignPauseExecutionBinding)
        candidate = _checked_member(
            candidate_binding, GoogleAdsSearchCampaignCreateCandidateBinding
        )
        observation = _checked_member(
            observation_binding, GoogleAdsSearchCampaignCreateObservationBinding
        )
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
        postcondition = _postcondition(account, candidate)
        before, after = _states(
            account=account, candidate=candidate, fingerprint_key=fingerprint_key
        )
        action_id = _action_id(
            workspace=workspace,
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
            before=before,
            after=after,
            expires_at=expiry,
        )
        return GoogleAdsSearchCampaignCreatePlannedAction(
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
    account: GoogleAdsCampaignPauseAccountBinding,
    candidate: GoogleAdsSearchCampaignCreateCandidateBinding,
    fingerprint_key: bytes,
) -> tuple[StateRef, StateRef]:
    projection = projection_from_compiled_spec(candidate.specification)
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
    action: GoogleAdsSearchCampaignCreatePlannedAction, *, fingerprint_key: bytes
) -> tuple[StateRef, StateRef]:
    """Recompute the plan's two semantic states under the current local key."""

    return _states(
        account=action.account_binding,
        candidate=action.candidate_binding,
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
    if type(value) is GoogleAdsSearchCampaignCreatePlannedAction:
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


def _action_from_raw(data: dict[str, object]) -> GoogleAdsSearchCampaignCreatePlannedAction:
    if set(data) != _ACTION_FIELDS or any(type(key) is not str for key in data):
        raise ValueError("action keys")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("action schema version")
    if data["status"] != GOOGLE_ADS_CREATE_PLAN_STATUS:
        raise ValueError("action status")
    if type(data["action_id"]) is not str or _DIGEST.fullmatch(data["action_id"]) is None:
        raise ValueError("action ID")
    if data["current_state_status"] != "RECHECK_REQUIRED_BEFORE_EGRESS":
        raise ValueError("current-state semantics")
    if data["configuration_readback"] != "REQUIRED_AFTER_CREATE":
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
        return GoogleAdsSearchCampaignCreatePlannedAction(
            action_id=data["action_id"],
            workspace_binding=cast(
                GoogleAdsCampaignPauseWorkspaceBinding,
                _raw_member(data, "workspace_binding", GoogleAdsCampaignPauseWorkspaceBinding),
            ),
            account_binding=cast(
                GoogleAdsCampaignPauseAccountBinding,
                _raw_member(data, "account_binding", GoogleAdsCampaignPauseAccountBinding),
            ),
            execution_binding=cast(
                GoogleAdsCampaignPauseExecutionBinding,
                _raw_member(data, "execution_binding", GoogleAdsCampaignPauseExecutionBinding),
            ),
            candidate_binding=cast(
                GoogleAdsSearchCampaignCreateCandidateBinding,
                _raw_member(
                    data, "candidate_binding", GoogleAdsSearchCampaignCreateCandidateBinding
                ),
            ),
            observation_binding=cast(
                GoogleAdsSearchCampaignCreateObservationBinding,
                _raw_member(
                    data, "observation_binding", GoogleAdsSearchCampaignCreateObservationBinding
                ),
            ),
            postcondition=cast(
                GoogleAdsSearchCampaignCreatePostcondition,
                _raw_member(data, "postcondition", GoogleAdsSearchCampaignCreatePostcondition),
            ),
            plan=cast(CompositePlan, _raw_member(data, "plan", CompositePlan)),
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("planned action") from None


def _assert_action_instance(action: GoogleAdsSearchCampaignCreatePlannedAction) -> None:
    """Reconstruct every derived fact, using the action's own retained states.

    The semantic states are keyed by a local key this pure contract must not
    read, so revalidation checks their *structural* binding here -- the plan's
    ``before``/``after``, the postcondition's predicted hierarchy digest, and
    the action ID -- while the configured planner and the guarded adapter each
    recompute the fingerprints under the current key.
    """

    if type(action) is not GoogleAdsSearchCampaignCreatePlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, GoogleAdsCampaignPauseWorkspaceBinding)
    account = _checked_member(action.account_binding, GoogleAdsCampaignPauseAccountBinding)
    execution = _checked_member(action.execution_binding, GoogleAdsCampaignPauseExecutionBinding)
    candidate = _checked_member(
        action.candidate_binding, GoogleAdsSearchCampaignCreateCandidateBinding
    )
    observation = _checked_member(
        action.observation_binding, GoogleAdsSearchCampaignCreateObservationBinding
    )
    postcondition = _checked_member(
        action.postcondition, GoogleAdsSearchCampaignCreatePostcondition
    )
    plan = _checked_member(action.plan, CompositePlan)
    expiry = _checked_expiry(plan.expires_at)
    _assert_outer_bindings(
        account=account,
        execution=execution,
        candidate=candidate,
        observation=observation,
        expires_at=expiry,
    )
    if postcondition != _postcondition(account, candidate):
        raise ValueError("postcondition differs from the exact candidate")
    if len(plan.subplans) != 1:
        raise ValueError("a create action carries exactly one subplan")
    subplan = plan.subplans[0]
    expected_action_id = _action_id(
        workspace=workspace,
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
        before=subplan.before,
        after=subplan.after,
        expires_at=expiry,
    )
    if plan != expected_plan:
        raise ValueError("plan differs from the reconstructed Google Ads create action")
    if subplan.before == subplan.after:
        raise ValueError("a create plan must change the observed hierarchy state")
    if subplan.before.revision != _STATE_KIND or subplan.after.revision != _STATE_KIND:
        raise ValueError("create states must be semantic hierarchy projections")


def revalidate_google_ads_search_campaign_create_planned_action(
    value: object,
) -> GoogleAdsSearchCampaignCreatePlannedAction:
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


class GoogleAdsSearchCampaignCreatePlanRecord(StrictModel):
    """The additive ``changes`` record that retains one closed CREATE action."""

    schema_version: Literal[1] = 1
    event_type: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN"
    )
    provider: Literal["google_ads"] = "google_ads"
    action: GoogleAdsSearchCampaignCreatePlannedAction
    action_digest: Digest
    plan_digest: Digest
    candidate_digest: Digest
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
    def closed_action(cls, value: object) -> GoogleAdsSearchCampaignCreatePlannedAction:
        return revalidate_google_ads_search_campaign_create_planned_action(value)

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_action_binding(self) -> GoogleAdsSearchCampaignCreatePlanRecord:
        action = self.action
        subplan = action.plan.subplans[0]
        if (
            self.action_digest != digest(action.model_dump(mode="json", warnings="error"))
            or self.plan_digest != action.plan.plan_digest
            or self.candidate_digest != action.candidate_binding.candidate_digest
            or self.expires_at != action.plan.expires_at
            or subplan.provider != self.provider
            or self.maximum_incremental_spend != subplan.max_spend
        ):
            raise ValueError("retained Google Ads CREATE plan facts do not match the action")
        return self


def google_ads_search_campaign_create_plan_record(
    action: GoogleAdsSearchCampaignCreatePlannedAction,
) -> GoogleAdsSearchCampaignCreatePlanRecord:
    """Build the retained record for one already-closed CREATE action."""

    checked = revalidate_google_ads_search_campaign_create_planned_action(action)
    return GoogleAdsSearchCampaignCreatePlanRecord(
        action=checked,
        action_digest=digest(checked.model_dump(mode="json", warnings="error")),
        plan_digest=checked.plan.plan_digest,
        candidate_digest=checked.candidate_binding.candidate_digest,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_google_ads_search_campaign_create_plan_record(
    value: object,
) -> GoogleAdsSearchCampaignCreatePlanRecord:
    """Reject incomplete, aliased, or incoherent retained CREATE plan facts."""

    try:
        if type(value) is GoogleAdsSearchCampaignCreatePlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            GoogleAdsSearchCampaignCreatePlanRecord.model_fields
        ):
            raise ValueError("a complete record is required")
        encoded = _strict_json_bytes(value, label="plan record")
        return GoogleAdsSearchCampaignCreatePlanRecord.model_validate_json(encoded)
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_plan(
            "A complete closed Google Ads CREATE plan record is required."
        ) from None


def google_ads_create_plan_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive plan kind."""

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(GOOGLE_ADS_CREATE_PLAN_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is GoogleAdsSearchCampaignCreatePlanRecord


__all__ = [
    "GOOGLE_ADS_CREATE_API_VERSION",
    "GOOGLE_ADS_CREATE_CAPABILITY_VERSION",
    "GOOGLE_ADS_CREATE_EGRESS_PURPOSE",
    "GOOGLE_ADS_CREATE_HOST",
    "GOOGLE_ADS_CREATE_PLAN_RECORD_KIND",
    "GOOGLE_ADS_CREATE_PLAN_STATUS",
    "GOOGLE_ADS_CREATE_SCOPE",
    "GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS",
    "GoogleAdsAssetAutomationObservation",
    "GoogleAdsSearchCampaignCreateCandidateBinding",
    "GoogleAdsSearchCampaignCreateObservationBinding",
    "GoogleAdsSearchCampaignCreatePlanRecord",
    "GoogleAdsSearchCampaignCreatePlannedAction",
    "GoogleAdsSearchCampaignCreatePostcondition",
    "GoogleAdsSearchCampaignCreatePreconditions",
    "GoogleAdsSearchCampaignCreateSelection",
    "GoogleAdsSearchHierarchyMember",
    "GoogleAdsSearchHierarchyProjection",
    "build_google_ads_search_campaign_create_plan",
    "fingerprint_key_marker",
    "google_ads_create_plan_record_kind_installed",
    "google_ads_search_campaign_create_plan_record",
    "predicted_states",
    "projection_from_compiled_spec",
    "revalidate_google_ads_search_campaign_create_plan_record",
    "revalidate_google_ads_search_campaign_create_planned_action",
    "rsa_text_digest",
    "semantic_hierarchy_state",
    "semantic_target_fingerprint",
]

"""One exact spend-explicit Google Ads v25 Search hierarchy ACTIVATE, no authority.

This is the P05 activation contract.  It turns one *retained* P04
``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN`` action plus one fresh bounded read of
the paused hierarchy that create produced into a closed
:class:`GoogleAdsSearchCampaignActivatePlannedAction` whose ``plan`` is the
ordinary provider-neutral :class:`~adhelm.safety.CompositePlan` the kernel
executes.

Three things are deliberately different from the P04 CREATE contract:

* Activation is the **spend-explicit** authorization rule 8 and
  ``docs/authorization-policy.md`` require.  The subplan's ``max_spend`` is the
  observed ``CampaignBudget.total_amount_micros`` converted to account minor
  units, so the ``COMPOSITE_EXECUTION_PLAN`` subject's per-currency bound *is*
  the campaign-lifetime exposure a reviewer sees.  ``ACTIVATE`` is not one of
  the ``NON_SPEND_OPERATIONS``, so the bound can never summarize as
  ``NOT_SPEND_CAPABLE``.
* ``before`` and ``after`` are both **populated** semantic hierarchy states over
  the same ``(customer, campaign name)`` binding: the observed all-``PAUSED``
  member multiset, and the same multiset with every non-budget member
  ``ENABLED``.  ``CampaignBudget.status`` is output-only and tracks the campaign
  for a non-shared budget, so the budget member carries no status at all.
* The recovery disposition is a **precomputed pause proposal**.  The planner
  reuses the M55 pause contract to build the exact plan that would return this
  hierarchy to ``PAUSED`` and records only its digest.  That is a proposal, not
  an authorization: a pause still needs its own exact grant.

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
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS,
    GoogleAdsSearchHierarchyMember,
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
#: additive in ``changes`` and mirrors ``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN``.
GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND: Final = "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN"

GOOGLE_ADS_ACTIVATE_PLAN_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
GOOGLE_ADS_ACTIVATE_HOST: Final[Literal["googleads.googleapis.com"]] = "googleads.googleapis.com"
GOOGLE_ADS_ACTIVATE_API_VERSION: Final[Literal["v25"]] = "v25"
GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION: Final = "google-ads-v25-search-activate-contract-2026-09-09"
GOOGLE_ADS_ACTIVATE_EGRESS_PURPOSE: Final = "atomic Google Ads v25 Search hierarchy activation"
GOOGLE_ADS_ACTIVATE_SCOPE: Final[Literal["https://www.googleapis.com/auth/adwords"]] = (
    "https://www.googleapis.com/auth/adwords"
)

_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_CUSTOMER_ID: Final = re.compile(r"^[0-9]{10}$", re.ASCII)
_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 1_048_576
_USD_MINOR_TO_MICROS: Final = 10_000
_MAX_MINOR_UNITS: Final = 900_719_925_474

#: The v25 date-time shape the compiler emits and the readback observes.
DATE_TIME: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$", re.ASCII)
_DATE_TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

#: The one status every entity carries before activation and after it.
ACTIVATED_STATUS: Final = "ENABLED"
PAUSED_STATUS: Final = "PAUSED"
#: ``CampaignStatus``/``AdGroupStatus``/``AdGroupAdStatus``/``CampaignCriterionStatus``
#: /``AdGroupCriterionStatus`` all carry this value; a hierarchy holding one is
#: refused rather than activated.
REMOVED_STATUS: Final = "REMOVED"

#: The member kind whose status is output-only in v25 and therefore absent from
#: this projection: a non-shared ``CampaignBudget`` tracks its campaign.
_BUDGET_MEMBER_KIND: Final = "CAMPAIGN_BUDGET"

# -- the semantic projection -------------------------------------------------

_FINGERPRINT_CONTEXT: Final = b"adhelm/google-ads/search-campaign-activate/semantic/v1\x00"
_STATE_KIND: Final = "GOOGLE_ADS_SEARCH_CAMPAIGN_LIVE_HIERARCHY_V1"
_SEMANTIC_PROJECTION: Final = "NAME_HMAC_CONFIGURATION_EXACT_V1"
_SEMANTIC_SPEC: Final = SemanticHierarchySpec(
    context=_FINGERPRINT_CONTEXT,
    state_kind=_STATE_KIND,
    semantic_projection=_SEMANTIC_PROJECTION,
    target_purpose="google-ads-search-campaign-live-semantic-target",
    member_purpose="google-ads-search-campaign-live-semantic-member",
    collection_purpose="google-ads-search-campaign-live-semantic-collection",
)
_IDENTITY_PURPOSE: Final = "google-ads-search-campaign-live-semantic-identity"
_KEY_MARKER_PURPOSE: Final = "google-ads-search-campaign-live-fingerprint-key-marker"
_IDENTITY_FIELD: Final = "semantic_identity"


def _invalid_plan(message: str = "Google Ads Search ACTIVATE plan data is invalid.") -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_ACTIVATE_PLAN", message, exit_code=ExitCode.INVALID_INPUT
    )


class GoogleAdsLiveHierarchyProjection(StrictModel):
    """The exact member multiset one *existing* Search hierarchy projects to.

    Unlike the P04 create projection this permits ``PAUSED``, ``ENABLED``, or
    ``REMOVED`` on every non-budget member, because it describes a hierarchy
    that already exists and a reader must be able to *describe* reality before
    a planner refuses it.  Refusing a removed entity is the planner's job, with
    its own named code, not this projection's.
    """

    schema_version: Literal[1] = 1
    projection_kind: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_LIVE_HIERARCHY"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_LIVE_HIERARCHY"
    )
    campaign_name: str = Field(min_length=1, max_length=255)
    members: tuple[GoogleAdsSearchHierarchyMember, ...] = Field(min_length=6, max_length=140)

    @model_validator(mode="after")
    def bounded_live_hierarchy(self) -> GoogleAdsLiveHierarchyProjection:
        for member in self.members:
            configuration = dict(member.configuration)
            if member.member_kind == _BUDGET_MEMBER_KIND:
                if "status" in configuration:
                    raise ValueError("a non-shared campaign budget status is output only")
                continue
            if configuration.get("status") not in {
                PAUSED_STATUS,
                ACTIVATED_STATUS,
                REMOVED_STATUS,
            }:
                raise ValueError("every live Google Ads entity carries a closed v25 status")
        kinds = {member.member_kind for member in self.members}
        required = {_BUDGET_MEMBER_KIND, "CAMPAIGN", "AD_GROUP", "AD_GROUP_AD"}
        if not required <= kinds:
            raise ValueError("a live Search hierarchy projection is missing a required member")
        for kind in required:
            if sum(member.member_kind == kind for member in self.members) != 1:
                raise ValueError("a live Search hierarchy projection has exactly one of each root")
        return self

    @property
    def statuses(self) -> tuple[str, ...]:
        """Return every non-budget member status, in member order."""

        return tuple(
            dict(member.configuration)["status"]
            for member in self.members
            if member.member_kind != _BUDGET_MEMBER_KIND
        )

    def configured(self, name: str) -> dict[str, str]:
        """Return the configuration of the single member of one required kind."""

        for member in self.members:
            if member.member_kind == name:
                return dict(member.configuration)
        raise ValueError("the projection holds no member of this kind")


def activated_projection(
    projection: GoogleAdsLiveHierarchyProjection,
) -> GoogleAdsLiveHierarchyProjection:
    """Return the same hierarchy with every non-budget member ``ENABLED``.

    This is the whole of the activation postcondition: no name, criterion,
    keyword, budget amount, date, or creative surface changes, so a readback
    that differs anywhere else fails verification.
    """

    if type(projection) is not GoogleAdsLiveHierarchyProjection:
        raise ValueError("live hierarchy projection type")
    members: list[GoogleAdsSearchHierarchyMember] = []
    for member in projection.members:
        if member.member_kind == _BUDGET_MEMBER_KIND:
            members.append(member)
            continue
        configuration = tuple(
            (key, ACTIVATED_STATUS if key == "status" else value)
            for key, value in member.configuration
        )
        members.append(
            GoogleAdsSearchHierarchyMember(
                member_kind=member.member_kind,
                identity=member.identity,
                configuration=configuration,
            )
        )
    return GoogleAdsLiveHierarchyProjection(
        campaign_name=projection.campaign_name, members=tuple(members)
    )


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
            "provider_api_version": GOOGLE_ADS_ACTIVATE_API_VERSION,
            "customer_client_ref": account_client_ref,
            "campaign_name_fingerprint": _identity_fingerprint(
                ("campaign_name", campaign_name), fingerprint_key=fingerprint_key
            ),
        },
    )


def semantic_hierarchy_state(
    projection: GoogleAdsLiveHierarchyProjection | None,
    *,
    account_client_ref: str,
    campaign_name: str,
    fingerprint_key: bytes,
) -> StateRef:
    """Project one existing hierarchy -- or its absence -- into an opaque state.

    ``None`` is the empty member multiset under this exact ``(customer,
    campaign name)`` binding.  It is the local statement "no live hierarchy with
    this name in this customer", which is the ``after`` of an archive and the
    ``before`` a P04 CREATE uses under its own separate keyed namespace.
    """

    target = semantic_target_fingerprint(
        account_client_ref=account_client_ref,
        campaign_name=campaign_name,
        fingerprint_key=fingerprint_key,
    )
    members: tuple[str, ...] = ()
    if projection is not None:
        if type(projection) is not GoogleAdsLiveHierarchyProjection:
            raise ValueError("semantic projection type")
        if projection.campaign_name != campaign_name:
            raise ValueError("semantic projection names a different campaign")
        members = tuple(
            sorted(
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
            )
        )
    return collection_state(
        _SEMANTIC_SPEC,
        binding_fingerprint=target,
        fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
        member_fingerprints=members,
        fingerprint_key=fingerprint_key,
    )


# -- the plan request envelope member ----------------------------------------


class GoogleAdsSearchCampaignActivateSelection(StrictModel):
    """The additive ``plan execution create`` envelope member P05 introduces.

    Activation promotes exactly one retained P04 CREATE plan.  The retained
    action carries the compiled candidate, so the planner can compare the
    observed budget amount and campaign dates against the ones the create was
    authorized under and refuse when either changed.

    Only this source is installed.  There is no "activate whatever campaign has
    this name" path: a hierarchy this workspace did not plan and create has no
    retained budget/date baseline to compare against, and inventing one would be
    a claim rather than a check.  ``plan execution create`` refuses any other
    ``resource_ref`` shape with ``INVALID_GOOGLE_ADS_ACTIVATE_EXECUTION_SELECTION``
    before any configuration, credential, or provider work.
    """

    schema_version: Literal[1] = 1
    selection_kind: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE"
    )
    #: The retained ``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN`` record digest.
    create_plan_record_digest: Digest
    #: The campaign name that plan configured, restated so the request names the
    #: exact resource without the planner having to trust one retained field.
    campaign_name: str = Field(min_length=1, max_length=255)
    expires_in_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("campaign_name")
    @classmethod
    def quotable_name(cls, value: str) -> str:
        if GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(value):
            raise ValueError("this build refuses to construct a quoted GAQL campaign name")
        return value


# -- the closed bindings -----------------------------------------------------


class GoogleAdsActivateSourceBinding(StrictModel):
    """The exact retained CREATE plan this activation promotes."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_RETAINED_SEARCH_CREATE_PLAN"] = (
        "GOOGLE_ADS_V25_RETAINED_SEARCH_CREATE_PLAN"
    )
    create_plan_record_digest: Digest
    create_action_id: Digest
    create_plan_digest: Digest
    campaign_name: str = Field(min_length=1, max_length=255)
    #: The authorized create baseline, restated so drift is checkable locally.
    authorized_budget_total_amount_micros: int = Field(
        ge=_USD_MINOR_TO_MICROS, le=_MAX_MINOR_UNITS * _USD_MINOR_TO_MICROS
    )
    authorized_start_date_time: str = Field(pattern=DATE_TIME.pattern)
    authorized_end_date_time: str = Field(pattern=DATE_TIME.pattern)
    authorized_member_count: int = Field(ge=6, le=140)


class GoogleAdsActivatePreconditions(StrictModel):
    """The bounded activation checks this workspace actually performed."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_SEARCH_ACTIVATE_PRECONDITIONS"] = (
        "GOOGLE_ADS_V25_SEARCH_ACTIVATE_PRECONDITIONS"
    )
    observed_hierarchy_status: Literal["EVERY_ENTITY_PAUSED"] = "EVERY_ENTITY_PAUSED"
    observed_removed_members: Literal[0] = 0
    observed_campaign_type: Literal["SEARCH"] = "SEARCH"
    destination: Literal["FINAL_URLS_PRESENT_ON_EVERY_AD"] = "FINAL_URLS_PRESENT_ON_EVERY_AD"
    #: ``ad_group_ad.policy_summary.approval_status`` when v25 returned it for
    #: every ad, and the explicit ``NOT_READ`` disclosure when it did not.  A
    #: ``DISAPPROVED`` ad is refused before planning, never recorded here.
    policy_approval_status: Literal[
        "APPROVED",
        "APPROVED_LIMITED",
        "AREA_OF_INTEREST_ONLY",
        "NOT_READ_NO_POLICY_SUMMARY_RETURNED",
    ]
    budget_and_dates: Literal["EXACT_MATCH_WITH_RETAINED_CREATE_PLAN"] = (
        "EXACT_MATCH_WITH_RETAINED_CREATE_PLAN"
    )
    budget_period: Literal["CUSTOM_PERIOD"] = "CUSTOM_PERIOD"
    #: A ``CUSTOM_PERIOD`` budget has no ``amount_micros``, so Google's
    #: up-to-2x average-daily overdelivery behaviour has nothing to apply to.
    #: A future ``DAILY`` candidate must declare it here before promotion.
    daily_overdelivery: Literal["NOT_APPLICABLE_NO_AVERAGE_DAILY_BUDGET"] = (
        "NOT_APPLICABLE_NO_AVERAGE_DAILY_BUDGET"
    )
    #: The compiled campaign uses the standard ``target_spend`` click strategy,
    #: which is a non-conversion objective.  Conversion-dependent activation is
    #: a separate C02 dependency and is refused, not assumed, by this route.
    conversion_dependency: Literal["NOT_APPLICABLE_TARGET_SPEND_NON_CONVERSION_OBJECTIVE"] = (
        "NOT_APPLICABLE_TARGET_SPEND_NON_CONVERSION_OBJECTIVE"
    )
    campaign_end_date_time_in_future: Literal[True] = True

    @field_validator("observed_removed_members", mode="before")
    @classmethod
    def exact_zero(cls, value: object) -> int:
        if type(value) is not int or value != 0:
            raise ValueError("an activation plan requires zero removed members")
        return value

    @field_validator("campaign_end_date_time_in_future", mode="before")
    @classmethod
    def exact_true(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("an activation plan requires an unexpired campaign end date")
        return value


class GoogleAdsActivateObservationBinding(StrictModel):
    """One bounded sequential hierarchy observation and its activation checks."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_SEARCH_ACTIVATE_OBSERVATION"] = (
        "GOOGLE_ADS_V25_SEARCH_ACTIVATE_OBSERVATION"
    )
    campaign_name: str = Field(min_length=1, max_length=255)
    projection: GoogleAdsLiveHierarchyProjection
    observed_budget_total_amount_micros: int = Field(
        ge=_USD_MINOR_TO_MICROS, le=_MAX_MINOR_UNITS * _USD_MINOR_TO_MICROS
    )
    observed_start_date_time: str = Field(pattern=DATE_TIME.pattern)
    observed_end_date_time: str = Field(pattern=DATE_TIME.pattern)
    observed_ad_final_url_count: int = Field(ge=1, le=16)
    preconditions: GoogleAdsActivatePreconditions
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
    def bounded_paused_observation(self) -> GoogleAdsActivateObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("the activate observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("the activate observation interval exceeds the local bound")
        if GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(self.campaign_name):
            raise ValueError("this build refuses to construct a quoted GAQL campaign name")
        if self.projection.campaign_name != self.campaign_name:
            raise ValueError("the observation projects a different campaign")
        if set(self.projection.statuses) != {PAUSED_STATUS}:
            raise ValueError("an activation plan requires an entirely paused hierarchy")
        campaign = self.projection.configured("CAMPAIGN")
        budget = self.projection.configured(_BUDGET_MEMBER_KIND)
        if (
            campaign.get("start_date_time") != self.observed_start_date_time
            or campaign.get("end_date_time") != self.observed_end_date_time
            or campaign.get("advertising_channel_type") != "SEARCH"
            or budget.get("period") != "CUSTOM_PERIOD"
            or budget.get("total_amount_micros") != str(self.observed_budget_total_amount_micros)
        ):
            raise ValueError("the observation restates facts its own projection does not hold")
        return self


class GoogleAdsActivateRecoveryDisposition(StrictModel):
    """The bounded recovery this plan declares: retained refs, pause proposal.

    The digest names a complete M55 ``CompositePlan`` this workspace compiled
    for the *post-activation* state.  It is a proposal only: pausing is its own
    exact operation and needs its own authorization.  Nothing in this route
    reverts automatically.
    """

    schema_version: Literal[1] = 1
    disposition_kind: Literal["RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"] = (
        "RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"
    )
    recovery_pause_plan_digest: Digest
    recovery_pause_plan_authorization: Literal["NOT_AUTHORIZED_PROPOSAL_ONLY"] = (
        "NOT_AUTHORIZED_PROPOSAL_ONLY"
    )
    automatic_revert: Literal[False] = False

    @field_validator("automatic_revert", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("an activation never reverts automatically")
        return value


class GoogleAdsActivatePostcondition(StrictModel):
    """Closed configuration-only postcondition with an explicit spend statement."""

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    campaign_name: str = Field(min_length=1, max_length=255)
    configured_status: Literal["ENABLED"] = "ENABLED"
    every_entity_configured_status: Literal["ENABLED"] = "ENABLED"
    member_count: int = Field(ge=6, le=140)
    predicted_hierarchy_digest: Digest
    #: Activation is exactly the point at which delivery and spend can begin.
    #: The plan says so; it does not predict impressions, clicks, or pacing.
    effective_delivery: Literal["BEGINS_ON_ACTIVATION_VOLUME_NOT_PREDICTED"] = (
        "BEGINS_ON_ACTIVATION_VOLUME_NOT_PREDICTED"
    )
    spend_exposure: Literal["CAMPAIGN_LIFETIME_CUSTOM_PERIOD_TOTAL"] = (
        "CAMPAIGN_LIFETIME_CUSTOM_PERIOD_TOTAL"
    )
    maximum_spend: Money

    @model_validator(mode="after")
    def exact_account_reference(self) -> GoogleAdsActivatePostcondition:
        if self.account_ref != f"google_ads.customer.{self.account_client_ref}":
            raise ValueError("postcondition references do not bind the Google Ads customer")
        return self


class GoogleAdsActivateRedactedReview(StrictModel):
    """The closed spend-explicit review this plan's ``review_lines`` render.

    :func:`adhelm.approval_protocol.compiler_review_lines` renders every field as
    one ``label: value`` line, in sorted key order, after the derived
    per-currency spend summary.  The first activation line therefore states, in
    the reviewer's own list, that activation begins delivery and spend.
    """

    activation_effect: Literal["ACTIVATION_BEGINS_DELIVERY_AND_SPEND"] = (
        "ACTIVATION_BEGINS_DELIVERY_AND_SPEND"
    )
    activation_scope: Literal["ONE_SEARCH_CAMPAIGN_AND_EVERY_CHILD_ENTITY"] = (
        "ONE_SEARCH_CAMPAIGN_AND_EVERY_CHILD_ENTITY"
    )
    atomicity: Literal["ONE_MUTATE_UPDATE_MASK_STATUS_PARTIAL_FAILURE_DISABLED"] = (
        "ONE_MUTATE_UPDATE_MASK_STATUS_PARTIAL_FAILURE_DISABLED"
    )
    bidding_strategy: Literal["TARGET_SPEND_NON_CONVERSION_OBJECTIVE"] = (
        "TARGET_SPEND_NON_CONVERSION_OBJECTIVE"
    )
    budget_period: Literal["CUSTOM_PERIOD_CAMPAIGN_LIFETIME_TOTAL"] = (
        "CUSTOM_PERIOD_CAMPAIGN_LIFETIME_TOTAL"
    )
    campaign_end_date_time: str = Field(pattern=DATE_TIME.pattern)
    campaign_start_date_time: str = Field(pattern=DATE_TIME.pattern)
    daily_overdelivery: Literal["NOT_APPLICABLE_NO_AVERAGE_DAILY_BUDGET"] = (
        "NOT_APPLICABLE_NO_AVERAGE_DAILY_BUDGET"
    )
    entities_enabled: int = Field(ge=5, le=139)
    live_validated: Literal[False] = False
    policy_approval_status: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    recovery: Literal["RETAIN_REFS_PAUSE_PROPOSAL_NEEDS_ITS_OWN_AUTHORIZATION"] = (
        "RETAIN_REFS_PAUSE_PROPOSAL_NEEDS_ITS_OWN_AUTHORIZATION"
    )

    @field_validator("live_validated", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("an activation review cannot claim live validation")
        return value


# -- derivation --------------------------------------------------------------


def budget_minor_units(total_amount_micros: int, currency: str) -> Money:
    """Convert an observed v25 budget total to exact account minor units."""

    if type(total_amount_micros) is not int or total_amount_micros % _USD_MINOR_TO_MICROS:
        raise ValueError("a budget total must be an exact number of account minor units")
    minor_units = total_amount_micros // _USD_MINOR_TO_MICROS
    if not 1 <= minor_units <= _MAX_MINOR_UNITS:
        raise ValueError("the observed budget total is outside the local representation bound")
    return Money(minor_units=minor_units, currency=currency)


def _postcondition(
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsActivateObservationBinding,
) -> GoogleAdsActivatePostcondition:
    predicted = activated_projection(observation.projection)
    return GoogleAdsActivatePostcondition(
        account_ref=account.account_ref,
        account_client_ref=account.account_client_ref,
        campaign_name=observation.campaign_name,
        member_count=len(predicted.members),
        predicted_hierarchy_digest=digest(predicted.model_dump(mode="json", warnings="error")),
        maximum_spend=budget_minor_units(
            observation.observed_budget_total_amount_micros, account.currency
        ),
    )


def redacted_review(
    observation: GoogleAdsActivateObservationBinding,
) -> GoogleAdsActivateRedactedReview:
    """Build the closed review model the retained plan's lines are rendered from."""

    status = observation.preconditions.policy_approval_status
    return GoogleAdsActivateRedactedReview(
        campaign_end_date_time=observation.observed_end_date_time,
        campaign_start_date_time=observation.observed_start_date_time,
        entities_enabled=len(observation.projection.statuses),
        policy_approval_status=status,
    )


def _states(
    *,
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsActivateObservationBinding,
    fingerprint_key: bytes,
) -> tuple[StateRef, StateRef]:
    before = semantic_hierarchy_state(
        observation.projection,
        account_client_ref=account.account_client_ref,
        campaign_name=observation.campaign_name,
        fingerprint_key=fingerprint_key,
    )
    after = semantic_hierarchy_state(
        activated_projection(observation.projection),
        account_client_ref=account.account_client_ref,
        campaign_name=observation.campaign_name,
        fingerprint_key=fingerprint_key,
    )
    if before == after:
        raise ValueError("an activation plan must change the observed hierarchy state")
    return before, after


def _action_id(
    *,
    workspace: GoogleAdsCampaignPauseWorkspaceBinding,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    source: GoogleAdsActivateSourceBinding,
    observation: GoogleAdsActivateObservationBinding,
    postcondition: GoogleAdsActivatePostcondition,
    recovery: GoogleAdsActivateRecoveryDisposition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> str:
    return digest(
        {
            "kind": "google-ads-search-campaign-activate-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "account": account.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "source": source.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "recovery_disposition": recovery.model_dump(mode="json", warnings="error"),
            "before": before.model_dump(mode="json", warnings="error"),
            "after": after.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": GOOGLE_ADS_ACTIVATE_PLAN_STATUS,
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_ACTIVATE",
            "recovery_semantics": "RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL",
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
    postcondition: GoogleAdsActivatePostcondition,
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
        scopes=(GOOGLE_ADS_ACTIVATE_SCOPE,),
    )
    subplan = SubPlan(
        subplan_id=f"google-ads-search-campaign-activate-{action_id}",
        provider="google_ads",
        operation=OperationKind.ACTIVATE,
        account=identity,
        provider_api_version=GOOGLE_ADS_ACTIVATE_API_VERSION,
        capability_version=GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path="campaign/hierarchy/status", before=before, after=after),),
        # The spend-explicit fact.  ``ACTIVATE`` is not a ``NON_SPEND_OPERATION``,
        # so this integer always reaches the COMPOSITE_EXECUTION_PLAN subject's
        # per-currency bound and the review line a person reads.
        max_spend=postcondition.maximum_spend,
        egress=(
            EgressTarget(host=GOOGLE_ADS_ACTIVATE_HOST, purpose=GOOGLE_ADS_ACTIVATE_EGRESS_PURPOSE),
        ),
        # The one bounded compensating action: pause the hierarchy this
        # activation enabled.  The exact pause plan is precomputed and its
        # digest recorded, but executing it still needs its own authorization.
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"google-ads-search-campaign-activate-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    source: GoogleAdsActivateSourceBinding,
    observation: GoogleAdsActivateObservationBinding,
    expires_at: datetime,
) -> None:
    if account.account_status != "ENABLED":
        raise ValueError("an activation plan requires an ENABLED Google Ads customer")
    if source.campaign_name != observation.campaign_name:
        raise ValueError("the retained create plan configured a different campaign name")
    if source.authorized_budget_total_amount_micros != (
        observation.observed_budget_total_amount_micros
    ):
        raise ValueError("the observed budget differs from the authorized create baseline")
    if (
        source.authorized_start_date_time != observation.observed_start_date_time
        or source.authorized_end_date_time != observation.observed_end_date_time
    ):
        raise ValueError("the observed campaign dates differ from the authorized create baseline")
    if source.authorized_member_count != len(observation.projection.members):
        raise ValueError("the observed hierarchy size differs from the authorized create baseline")
    if execution.resolved_at > observation.read_started_at:
        raise ValueError("EDIT association occurs after the activation read began")
    if observation.read_finished_at >= expires_at:
        raise ValueError("plan expiry must follow the completed activation read")
    if expires_at > execution.expires_at:
        raise ValueError("plan expiry exceeds the declared bearer expiry")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")


class GoogleAdsSearchCampaignActivatePlannedAction(StrictModel):
    """One exact spend-explicit Search ACTIVATE, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = GOOGLE_ADS_ACTIVATE_PLAN_STATUS
    action_id: Digest
    workspace_binding: GoogleAdsCampaignPauseWorkspaceBinding
    account_binding: GoogleAdsCampaignPauseAccountBinding
    execution_binding: GoogleAdsCampaignPauseExecutionBinding
    source_binding: GoogleAdsActivateSourceBinding
    observation_binding: GoogleAdsActivateObservationBinding
    postcondition: GoogleAdsActivatePostcondition
    recovery_disposition: GoogleAdsActivateRecoveryDisposition
    plan: CompositePlan
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    configuration_readback: Literal["REQUIRED_AFTER_ACTIVATE"] = "REQUIRED_AFTER_ACTIVATE"
    recovery_semantics: Literal["RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"] = (
        "RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"
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
    def exact_reconstructed_action(self) -> GoogleAdsSearchCampaignActivatePlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only complete, closed, validated serializable action data."""

        checked = revalidate_google_ads_search_campaign_activate_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def build_google_ads_search_campaign_activate_plan(
    *,
    workspace_profile_ref: str,
    account_binding: GoogleAdsCampaignPauseAccountBinding,
    execution_binding: GoogleAdsCampaignPauseExecutionBinding,
    source_binding: GoogleAdsActivateSourceBinding,
    observation_binding: GoogleAdsActivateObservationBinding,
    recovery_disposition: GoogleAdsActivateRecoveryDisposition,
    expires_at: datetime,
    fingerprint_key: bytes,
) -> GoogleAdsSearchCampaignActivatePlannedAction:
    """Build the one non-authorizing spend-explicit Search ACTIVATE plan."""

    try:
        workspace = GoogleAdsCampaignPauseWorkspaceBinding(
            workspace_profile_ref=workspace_profile_ref
        )
        account = _checked_member(account_binding, GoogleAdsCampaignPauseAccountBinding)
        execution = _checked_member(execution_binding, GoogleAdsCampaignPauseExecutionBinding)
        source = _checked_member(source_binding, GoogleAdsActivateSourceBinding)
        observation = _checked_member(observation_binding, GoogleAdsActivateObservationBinding)
        recovery = _checked_member(recovery_disposition, GoogleAdsActivateRecoveryDisposition)
        if observation.fingerprint_key_marker != fingerprint_key_marker(fingerprint_key):
            raise ValueError("the observation was projected under a different local key")
        expiry = _checked_expiry(expires_at)
        _assert_outer_bindings(
            account=account,
            execution=execution,
            source=source,
            observation=observation,
            expires_at=expiry,
        )
        postcondition = _postcondition(account, observation)
        before, after = _states(
            account=account, observation=observation, fingerprint_key=fingerprint_key
        )
        action_id = _action_id(
            workspace=workspace,
            account=account,
            execution=execution,
            source=source,
            observation=observation,
            postcondition=postcondition,
            recovery=recovery,
            before=before,
            after=after,
            expires_at=expiry,
        )
        plan = _build_plan(
            action_id=action_id,
            workspace=workspace,
            account=account,
            execution=execution,
            postcondition=postcondition,
            before=before,
            after=after,
            expires_at=expiry,
        )
        return GoogleAdsSearchCampaignActivatePlannedAction(
            action_id=action_id,
            workspace_binding=workspace,
            account_binding=account,
            execution_binding=execution,
            source_binding=source,
            observation_binding=observation,
            postcondition=postcondition,
            recovery_disposition=recovery,
            plan=plan,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


def predicted_states(
    action: GoogleAdsSearchCampaignActivatePlannedAction, *, fingerprint_key: bytes
) -> tuple[StateRef, StateRef]:
    """Recompute the plan's two semantic states under the current local key."""

    return _states(
        account=action.account_binding,
        observation=action.observation_binding,
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
        raise ValueError("plan expiry must be an aware datetime")
    return value.astimezone(UTC)


def _assert_action_instance(action: GoogleAdsSearchCampaignActivatePlannedAction) -> None:
    if type(action) is not GoogleAdsSearchCampaignActivatePlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, GoogleAdsCampaignPauseWorkspaceBinding)
    account = _checked_member(action.account_binding, GoogleAdsCampaignPauseAccountBinding)
    execution = _checked_member(action.execution_binding, GoogleAdsCampaignPauseExecutionBinding)
    source = _checked_member(action.source_binding, GoogleAdsActivateSourceBinding)
    observation = _checked_member(action.observation_binding, GoogleAdsActivateObservationBinding)
    postcondition = _checked_member(action.postcondition, GoogleAdsActivatePostcondition)
    recovery = _checked_member(action.recovery_disposition, GoogleAdsActivateRecoveryDisposition)
    plan = _checked_member(action.plan, CompositePlan)
    expiry = _checked_expiry(plan.expires_at)
    _assert_outer_bindings(
        account=account,
        execution=execution,
        source=source,
        observation=observation,
        expires_at=expiry,
    )
    if postcondition != _postcondition(account, observation):
        raise ValueError("postcondition differs from the observed baseline")
    subplan = plan.subplans[0]
    expected_action_id = _action_id(
        workspace=workspace,
        account=account,
        execution=execution,
        source=source,
        observation=observation,
        postcondition=postcondition,
        recovery=recovery,
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
        postcondition=postcondition,
        before=subplan.before,
        after=subplan.after,
        expires_at=expiry,
    )
    if plan != expected_plan:
        raise ValueError("plan differs from the reconstructed Google Ads activate action")


def revalidate_google_ads_search_campaign_activate_planned_action(
    value: object,
) -> GoogleAdsSearchCampaignActivatePlannedAction:
    """Strictly parse and reconstruct one closed locally retained ACTIVATE action.

    This verifies only self-consistency.  It cannot prove current customer or
    hierarchy state, dual-slot continuity, provider access, authorization, or a
    mutation outcome.  A later guarded workflow must freshly re-read and
    reauthorize before any provider write.
    """

    try:
        if type(value) is GoogleAdsSearchCampaignActivatePlannedAction:
            raw: Any = value.model_dump(mode="json", warnings="error")
        elif type(value) is dict:
            raw = value
        else:
            raise ValueError("action input type")
        encoded = _strict_json_bytes(raw, label="action")
        parsed = parse_json(encoded, maximum_bytes=_MAX_ACTION_BYTES)
        if type(parsed) is not dict or set(parsed) != set(
            GoogleAdsSearchCampaignActivatePlannedAction.model_fields
        ):
            raise ValueError("action keys")
        action = GoogleAdsSearchCampaignActivatePlannedAction.model_validate_json(encoded)
        _assert_action_instance(action)
        if canonical_bytes(parsed) != canonical_bytes(
            action.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("action canonical shape")
        return action
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


class GoogleAdsSearchCampaignActivatePlanRecord(StrictModel):
    """The additive ``changes`` record that retains one closed ACTIVATE action."""

    schema_version: Literal[1] = 1
    event_type: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN"
    )
    provider: Literal["google_ads"] = "google_ads"
    action: GoogleAdsSearchCampaignActivatePlannedAction
    action_digest: Digest
    plan_digest: Digest
    create_plan_digest: Digest
    recovery_pause_plan_digest: Digest
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
    def closed_action(cls, value: object) -> GoogleAdsSearchCampaignActivatePlannedAction:
        return revalidate_google_ads_search_campaign_activate_planned_action(value)

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_action_binding(self) -> GoogleAdsSearchCampaignActivatePlanRecord:
        action = self.action
        subplan = action.plan.subplans[0]
        if (
            self.action_digest != digest(action.model_dump(mode="json", warnings="error"))
            or self.plan_digest != action.plan.plan_digest
            or self.create_plan_digest != action.source_binding.create_plan_digest
            or self.recovery_pause_plan_digest
            != action.recovery_disposition.recovery_pause_plan_digest
            or self.expires_at != action.plan.expires_at
            or subplan.provider != self.provider
            or self.maximum_incremental_spend != subplan.max_spend
        ):
            raise ValueError("retained Google Ads ACTIVATE plan facts do not match the action")
        return self


def google_ads_search_campaign_activate_plan_record(
    action: GoogleAdsSearchCampaignActivatePlannedAction,
) -> GoogleAdsSearchCampaignActivatePlanRecord:
    """Build the retained record for one already-closed ACTIVATE action."""

    checked = revalidate_google_ads_search_campaign_activate_planned_action(action)
    return GoogleAdsSearchCampaignActivatePlanRecord(
        action=checked,
        action_digest=digest(checked.model_dump(mode="json", warnings="error")),
        plan_digest=checked.plan.plan_digest,
        create_plan_digest=checked.source_binding.create_plan_digest,
        recovery_pause_plan_digest=checked.recovery_disposition.recovery_pause_plan_digest,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_google_ads_search_campaign_activate_plan_record(
    value: object,
) -> GoogleAdsSearchCampaignActivatePlanRecord:
    """Reject incomplete, aliased, or incoherent retained ACTIVATE plan facts."""

    try:
        if type(value) is GoogleAdsSearchCampaignActivatePlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            GoogleAdsSearchCampaignActivatePlanRecord.model_fields
        ):
            raise ValueError("a complete record is required")
        encoded = _strict_json_bytes(value, label="plan record")
        return GoogleAdsSearchCampaignActivatePlanRecord.model_validate_json(encoded)
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_plan(
            "A complete closed Google Ads ACTIVATE plan record is required."
        ) from None


def google_ads_activate_plan_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive plan kind."""

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is GoogleAdsSearchCampaignActivatePlanRecord


def campaign_end_date_time_in_future(value: str, *, timezone: str, now: datetime) -> bool:
    """Report whether one v25 campaign end date-time is still in the future.

    ``Campaign.end_date_time`` is expressed in the *customer's* time zone, so
    the comparison localizes it there rather than reading it as UTC.
    """

    from zoneinfo import ZoneInfo

    if type(value) is not str or DATE_TIME.fullmatch(value) is None:
        raise ValueError("campaign end date time is invalid")
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("an aware instant is required")
    zone = ZoneInfo(timezone)
    end = datetime.strptime(value, _DATE_TIME_FORMAT).replace(tzinfo=zone)
    return end > now.astimezone(UTC)


__all__ = [
    "ACTIVATED_STATUS",
    "DATE_TIME",
    "GOOGLE_ADS_ACTIVATE_API_VERSION",
    "GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION",
    "GOOGLE_ADS_ACTIVATE_EGRESS_PURPOSE",
    "GOOGLE_ADS_ACTIVATE_HOST",
    "GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND",
    "GOOGLE_ADS_ACTIVATE_PLAN_STATUS",
    "GOOGLE_ADS_ACTIVATE_SCOPE",
    "PAUSED_STATUS",
    "REMOVED_STATUS",
    "GoogleAdsActivateObservationBinding",
    "GoogleAdsActivatePostcondition",
    "GoogleAdsActivatePreconditions",
    "GoogleAdsActivateRecoveryDisposition",
    "GoogleAdsActivateRedactedReview",
    "GoogleAdsActivateSourceBinding",
    "GoogleAdsLiveHierarchyProjection",
    "GoogleAdsSearchCampaignActivatePlanRecord",
    "GoogleAdsSearchCampaignActivatePlannedAction",
    "GoogleAdsSearchCampaignActivateSelection",
    "activated_projection",
    "budget_minor_units",
    "build_google_ads_search_campaign_activate_plan",
    "campaign_end_date_time_in_future",
    "fingerprint_key_marker",
    "google_ads_activate_plan_record_kind_installed",
    "google_ads_search_campaign_activate_plan_record",
    "predicted_states",
    "redacted_review",
    "revalidate_google_ads_search_campaign_activate_plan_record",
    "revalidate_google_ads_search_campaign_activate_planned_action",
    "semantic_hierarchy_state",
    "semantic_target_fingerprint",
]

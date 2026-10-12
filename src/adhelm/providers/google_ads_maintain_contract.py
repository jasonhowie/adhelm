"""One exact guarded Google Ads v25 Search maintenance operation, without authority.

This is the P05 maintenance contract.  One family covers the four documented
maintenance and replacement operations on an existing Search hierarchy, each
compiled as its own single-request executable plan on the generic route:

===============================  ==========  ==========================================
``maintenance_kind``             operation   spend bound
===============================  ==========  ==========================================
``CAMPAIGN_BUDGET_AMOUNT``       ``UPDATE``  the **new** campaign-lifetime total
``AD_GROUP_CPC_BID``             ``UPDATE``  the campaign budget total
``AD_SCHEDULE_REPLACEMENT``      ``UPDATE``  the campaign budget total
``CAMPAIGN_ARCHIVE``             ``ARCHIVE`` ``NOT_SPEND_CAPABLE`` (explicit zero cap)
===============================  ==========  ==========================================

A bid change is deliberately **not** ``NOT_SPEND_CAPABLE``.  Raising a max CPC
changes what the account can spend per click, so the plan must state a spend
bound; the honest, provider-enforceable one is the campaign budget that caps the
whole flight.  The same reasoning applies to a schedule replacement, which
changes when the same budget is allowed to be spent.

An archive is the only operation here that removes delivery capability rather
than changing it, so it is the only one that summarizes as
``NOT_SPEND_CAPABLE`` -- and the only one whose review lines say the word
irreversible.

An archive is therefore **delete-class**, and AGENTS rule 11 governs it: "A
resource may be deleted only when the dependency engine proves it is
``PROVEN_UNREFERENCED``.  Age, naming, paused status, or apparent inactivity is
never enough."  The gate is :class:`GoogleAdsCampaignDependencyProof`, built
only from a read.  The plan's ``IRREVERSIBLE_ARCHIVE`` disposition, the
explicit acknowledgement, and the paused-first rule all remain required, and
none of them is the proof.

Nothing here reads a credential, contacts Google, or authorizes anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal, cast, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import SafeRef, StrictModel
from adhelm.providers._semantic_state import keyed_fingerprint
from adhelm.providers.google_ads_activate_contract import (
    ACTIVATED_STATUS,
    DATE_TIME,
    GoogleAdsLiveHierarchyProjection,
    budget_minor_units,
    fingerprint_key_marker,
    semantic_hierarchy_state,
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

#: The retained ledger record kind this contract's plan is stored under.
GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND: Final = "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN"

GOOGLE_ADS_MAINTAIN_PLAN_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
GOOGLE_ADS_MAINTAIN_HOST: Final[Literal["googleads.googleapis.com"]] = "googleads.googleapis.com"
GOOGLE_ADS_MAINTAIN_API_VERSION: Final[Literal["v25"]] = "v25"
GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION: Final = "google-ads-v25-search-maintain-contract-2026-09-09"
GOOGLE_ADS_MAINTAIN_SCOPE: Final[Literal["https://www.googleapis.com/auth/adwords"]] = (
    "https://www.googleapis.com/auth/adwords"
)

_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 1_048_576
_USD_MINOR_TO_MICROS: Final = 10_000
_MAX_MINOR_UNITS: Final = 900_719_925_474
_MAX_MICROS: Final = _MAX_MINOR_UNITS * _USD_MINOR_TO_MICROS

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

MaintenanceKind = Literal[
    "CAMPAIGN_BUDGET_AMOUNT",
    "AD_GROUP_CPC_BID",
    "AD_SCHEDULE_REPLACEMENT",
    "CAMPAIGN_ARCHIVE",
]

#: The one operation each maintenance kind compiles to.  ``ARCHIVE`` is the
#: only member of ``NON_SPEND_OPERATIONS`` this family uses.
MAINTENANCE_OPERATIONS: Final[dict[str, OperationKind]] = {
    "CAMPAIGN_BUDGET_AMOUNT": OperationKind.UPDATE,
    "AD_GROUP_CPC_BID": OperationKind.UPDATE,
    "AD_SCHEDULE_REPLACEMENT": OperationKind.UPDATE,
    "CAMPAIGN_ARCHIVE": OperationKind.ARCHIVE,
}

#: The exact egress purpose each kind declares.
MAINTENANCE_EGRESS_PURPOSES: Final[dict[str, str]] = {
    "CAMPAIGN_BUDGET_AMOUNT": "fixed Google Ads v25 campaign budget amount update",
    "AD_GROUP_CPC_BID": "fixed Google Ads v25 ad group cpc bid update",
    "AD_SCHEDULE_REPLACEMENT": "atomic Google Ads v25 ad schedule criterion replacement",
    "CAMPAIGN_ARCHIVE": "fixed Google Ads v25 campaign archive",
}

#: The v25 ``MinuteOfHour`` enum symbols the compiler already uses.
MINUTE_OF_HOUR: Final = ("ZERO", "FIFTEEN", "THIRTY", "FORTY_FIVE")
DAY_OF_WEEK: Final = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)

_ARCHIVED_STATUS: Final = "REMOVED"

#: The typed refusal an archive raises when rule 11's proof is not established.
GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN: Final = "GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN"

#: Every reference surface this build reviews before a campaign archive, in the
#: exact order a proof must carry them.  Within this build the list is
#: **closed**: the proof model refuses a reading that adds, drops, duplicates,
#: or reorders a member, so a surface cannot be silently omitted from an
#: existing proof.
#:
#: **The enumeration itself is provisional and uncited.**  No page in
#: ``docs/research/`` cites a v25 resource for any of these surfaces, so there
#: is no source that shows this five-member list is *complete*.  Other places a
#: campaign can be referenced from -- campaign assets and asset sets,
#: customizers, extension settings -- are not on it, and nothing here argues
#: they are absent.  This list is a working model for the refusal, not evidence
#: for the ``measurement-control.md`` "every applicable resource surface"
#: requirement.  The ticket that adds a cited reference read must re-derive and
#: cite the complete surface list first, extending it (with a ``schema_version``
#: bump on the proof) where needed, *before* any proof can be ``READ_EMPTY``.
#:
#: These are the places outside the campaign that can hold a reference to it.
#: The campaign's own contents -- ad groups, ads, keywords, and campaign
#: criteria -- are deliberately not on the list: the archive plan's ``before``
#: projection enumerates them and its ``after`` is the empty multiset, so they
#: are the reviewed scope of the removal rather than something that references
#: it from outside.  Counting them as references would make a proof impossible
#: beside any hierarchy this route can observe, which always has an ad group.
#:
#: **No read this build can cite reaches any of them.**  ``docs/research/``
#: cites no v25 resource for them, so none is queried and each is recorded
#: ``NOT_READ`` rather than assumed empty.
CampaignReferenceSurface = Literal[
    "CAMPAIGN_EXPERIMENTS_AND_DRAFTS",
    "CAMPAIGN_SHARED_SET_LINKS",
    "CAMPAIGN_LABELS",
    "CAMPAIGN_CONVERSION_GOAL_LINKS",
    "PORTFOLIO_BIDDING_STRATEGY_REFERENCES",
]
CAMPAIGN_REFERENCE_SURFACES: Final[tuple[CampaignReferenceSurface, ...]] = get_args(
    CampaignReferenceSurface
)

#: What one bounded read established about one surface.  ``NOT_READ`` covers
#: every reason a surface was not established: no citable GAQL resource, a
#: bound exceeded, or an incomplete pagination.  It is never a synonym for
#: "empty"; ``measurement-control.md`` requires the opposite reading.
CampaignReferenceDisposition = Literal["READ_EMPTY", "READ_NONEMPTY", "NOT_READ"]


def _invalid_plan(message: str = "Google Ads Search MAINTAIN plan data is invalid.") -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_MAINTAIN_PLAN", message, exit_code=ExitCode.INVALID_INPUT
    )


# -- the rule 11 dependency proof --------------------------------------------


class GoogleAdsCampaignReferenceSurfaceReading(StrictModel):
    """What one bounded read established about one campaign reference surface."""

    schema_version: Literal[1] = 1
    surface: CampaignReferenceSurface
    disposition: CampaignReferenceDisposition
    #: How many rows referencing this campaign the read returned.  ``None`` is
    #: required for ``NOT_READ``: an unread surface has no row count, and a
    #: zero here would be exactly the fabricated emptiness rule 11 forbids.
    referencing_row_count: int | None = Field(default=None, ge=0, le=10_000)
    #: A bounded reason code.  It never carries a customer value.
    basis: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")

    @model_validator(mode="after")
    def counted_only_when_read(self) -> GoogleAdsCampaignReferenceSurfaceReading:
        if self.disposition == "NOT_READ":
            if self.referencing_row_count is not None:
                raise ValueError("an unread reference surface cannot carry a row count")
            return self
        if self.referencing_row_count is None:
            raise ValueError("a read reference surface must carry its row count")
        if (self.referencing_row_count == 0) != (self.disposition == "READ_EMPTY"):
            raise ValueError("the reference surface row count contradicts its disposition")
        return self


class GoogleAdsCampaignDependencyProof(StrictModel):
    """One read-derived rule 11 proof that nothing references a campaign.

    This model is deliberately unable to hold anything but a proof.  ``status``
    is the single literal ``PROVEN_UNREFERENCED`` and the validator refuses
    unless *every* surface in :data:`CAMPAIGN_REFERENCE_SURFACES` is present, in
    order, with disposition ``READ_EMPTY``.  A surface that is ``READ_NONEMPTY``
    or ``NOT_READ`` therefore cannot be carried by a proof at all: the producer
    refuses with :data:`GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN` and no archive
    plan is ever built.

    It restates nothing its container already holds.  The campaign name, the
    hierarchy projection, the local key marker, and the read interval belong to
    the :class:`GoogleAdsMaintainObservationBinding` that carries the proof and
    are digested with it, so a copy here could only ever be checked against
    itself.  The proof also records **no read interval of its own**: this build
    performs no reference read, so there is no interval to measure, and copying
    the hierarchy read's would fabricate one.  The ticket that adds a cited
    reference read adds fields that read measures (and bumps ``schema_version``).

    Nothing here is an operator assertion.  There is no field a request can set:
    ``adhelm.google_ads_maintain_planning_session.campaign_dependency_proof``
    builds it from the completed fixed read or raises.
    """

    schema_version: Literal[1] = 1
    proof_kind: Literal["GOOGLE_ADS_V25_CAMPAIGN_DEPENDENCY_PROOF"] = (
        "GOOGLE_ADS_V25_CAMPAIGN_DEPENDENCY_PROOF"
    )
    status: Literal["PROVEN_UNREFERENCED"] = "PROVEN_UNREFERENCED"
    surfaces: tuple[GoogleAdsCampaignReferenceSurfaceReading, ...] = Field(
        min_length=len(CAMPAIGN_REFERENCE_SURFACES), max_length=len(CAMPAIGN_REFERENCE_SURFACES)
    )
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    #: No proof this build produces is a live-validated one.
    live_validated: Literal[False] = False

    @field_validator("live_validated", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("a dependency proof cannot claim live validation")
        return value

    @model_validator(mode="after")
    def every_surface_read_empty(self) -> GoogleAdsCampaignDependencyProof:
        if tuple(item.surface for item in self.surfaces) != CAMPAIGN_REFERENCE_SURFACES:
            raise ValueError("a dependency proof carries the closed surface list exactly once")
        blocking = [item.surface for item in self.surfaces if item.disposition != "READ_EMPTY"]
        if blocking:
            raise ValueError(f"reference surface not proven empty: {blocking[0]}")
        return self


# -- the four intents --------------------------------------------------------


class GoogleAdsMaintenanceSchedule(StrictModel):
    """One replacement ad-schedule criterion, in the compiler's native shape."""

    day_of_week: Literal[
        "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"
    ]
    start_hour: int = Field(ge=0, le=23)
    start_minute: Literal["ZERO", "FIFTEEN", "THIRTY", "FORTY_FIVE"]
    end_hour: int = Field(ge=0, le=24)
    end_minute: Literal["ZERO", "FIFTEEN", "THIRTY", "FORTY_FIVE"]

    @model_validator(mode="after")
    def ordered_interval(self) -> GoogleAdsMaintenanceSchedule:
        start = self.start_hour * 60 + MINUTE_OF_HOUR.index(self.start_minute) * 15
        end = self.end_hour * 60 + MINUTE_OF_HOUR.index(self.end_minute) * 15
        if self.end_hour == 24 and self.end_minute != "ZERO":
            raise ValueError("an ad schedule ending at hour 24 ends at minute ZERO")
        if end <= start:
            raise ValueError("an ad schedule must end after it starts")
        return self

    @property
    def identity(self) -> tuple[str, ...]:
        """Return the projection identity a readback of this criterion produces."""

        return (
            self.day_of_week,
            str(self.start_hour),
            self.start_minute,
            str(self.end_hour),
            self.end_minute,
        )


class GoogleAdsBudgetAmountIntent(StrictModel):
    """Change one campaign budget's ``CUSTOM_PERIOD`` total."""

    maintenance_kind: Literal["CAMPAIGN_BUDGET_AMOUNT"] = "CAMPAIGN_BUDGET_AMOUNT"
    new_total_amount_micros: int = Field(ge=_USD_MINOR_TO_MICROS, le=_MAX_MICROS)

    @field_validator("new_total_amount_micros")
    @classmethod
    def exact_minor_units(cls, value: int) -> int:
        if value % _USD_MINOR_TO_MICROS:
            raise ValueError("a budget total must be an exact number of account minor units")
        return value


class GoogleAdsAdGroupBidIntent(StrictModel):
    """Change one ad group's manual max-CPC bid."""

    maintenance_kind: Literal["AD_GROUP_CPC_BID"] = "AD_GROUP_CPC_BID"
    new_cpc_bid_micros: int = Field(ge=_USD_MINOR_TO_MICROS, le=_MAX_MICROS)

    @field_validator("new_cpc_bid_micros")
    @classmethod
    def exact_minor_units(cls, value: int) -> int:
        if value % _USD_MINOR_TO_MICROS:
            raise ValueError("a bid must be an exact number of account minor units")
        return value


class GoogleAdsAdScheduleReplacementIntent(StrictModel):
    """Replace every ad-schedule campaign criterion in one atomic mutate.

    ``CampaignCriterion.ad_schedule`` is ``Immutable`` in v25, so a schedule is
    never edited: the exact existing schedule criteria are removed and the new
    ones created in the same request.  No temporary resource ID is needed
    because no created criterion is referenced by a later operation.
    """

    maintenance_kind: Literal["AD_SCHEDULE_REPLACEMENT"] = "AD_SCHEDULE_REPLACEMENT"
    schedules: tuple[GoogleAdsMaintenanceSchedule, ...] = Field(min_length=1, max_length=21)

    @model_validator(mode="after")
    def unique_sorted_schedules(self) -> GoogleAdsAdScheduleReplacementIntent:
        identities = tuple(item.identity for item in self.schedules)
        if len(set(identities)) != len(identities):
            raise ValueError("replacement ad schedules must be unique")
        if tuple(sorted(identities)) != identities:
            raise ValueError("replacement ad schedules must be supplied in sorted order")
        counts = {
            day: sum(item.day_of_week == day for item in self.schedules) for day in DAY_OF_WEEK
        }
        if any(count > 6 for count in counts.values()):
            raise ValueError("Google Ads permits at most six ad schedules per day")
        return self


class GoogleAdsCampaignArchiveIntent(StrictModel):
    """Set one campaign's status to ``REMOVED``.  This cannot be undone.

    ``disposition`` is not decoration.  This build refuses to compile a
    ``REMOVED`` transition unless the request itself names the outcome as
    irreversible, so an archive can never be reached by a caller that thought
    it was pausing.
    """

    maintenance_kind: Literal["CAMPAIGN_ARCHIVE"] = "CAMPAIGN_ARCHIVE"
    disposition: Literal["IRREVERSIBLE_ARCHIVE"]
    acknowledged_irreversible: Literal[True]

    @field_validator("acknowledged_irreversible", mode="before")
    @classmethod
    def exact_true(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("an archive requires an explicit irreversibility acknowledgement")
        return value


MaintenanceIntent = (
    GoogleAdsBudgetAmountIntent
    | GoogleAdsAdGroupBidIntent
    | GoogleAdsAdScheduleReplacementIntent
    | GoogleAdsCampaignArchiveIntent
)


class GoogleAdsSearchCampaignMaintainSelection(StrictModel):
    """The additive ``plan execution create`` envelope member P05 introduces."""

    schema_version: Literal[1] = 1
    selection_kind: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN"
    )
    campaign_name: str = Field(min_length=1, max_length=255)
    intent: MaintenanceIntent = Field(discriminator="maintenance_kind")
    expires_in_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("campaign_name")
    @classmethod
    def quotable_name(cls, value: str) -> str:
        if GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(value):
            raise ValueError("this build refuses to construct a quoted GAQL campaign name")
        return value

    @property
    def operation(self) -> OperationKind:
        return MAINTENANCE_OPERATIONS[self.intent.maintenance_kind]


# -- the closed bindings -----------------------------------------------------


class GoogleAdsMaintainPreconditions(StrictModel):
    """The bounded maintenance checks this workspace actually performed."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_SEARCH_MAINTAIN_PRECONDITIONS"] = (
        "GOOGLE_ADS_V25_SEARCH_MAINTAIN_PRECONDITIONS"
    )
    observed_campaign_type: Literal["SEARCH"] = "SEARCH"
    observed_removed_members: Literal[0] = 0
    observed_campaign_status: Literal["ENABLED", "PAUSED"]
    budget_period: Literal["CUSTOM_PERIOD"] = "CUSTOM_PERIOD"
    #: An archive requires a campaign that is already paused, so activation and
    #: archival can never be combined into one unreviewed step.  Paused status
    #: is a *sequencing* control, never the deletion gate: AGENTS rule 11 names
    #: paused status as never sufficient, and the gate is ``dependency_proof``.
    archive_requires_paused_campaign: Literal[True] = True
    #: AGENTS rule 11's gate for the one delete-class operation in this family.
    #: ``None`` for every other kind; an archive whose preconditions carry
    #: ``None`` is refused before a plan exists.  This is read-derived: see
    #: :class:`GoogleAdsCampaignDependencyProof`.
    dependency_proof: GoogleAdsCampaignDependencyProof | None = None
    #: A maintenance operation records the previous values and reverts nothing.
    recovery: Literal["RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"] = (
        "RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"
    )

    @field_validator("observed_removed_members", mode="before")
    @classmethod
    def exact_zero(cls, value: object) -> int:
        if type(value) is not int or value != 0:
            raise ValueError("a maintenance plan requires zero removed members")
        return value

    @field_validator("archive_requires_paused_campaign", mode="before")
    @classmethod
    def exact_true(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("this control is always true")
        return value


class GoogleAdsMaintainObservationBinding(StrictModel):
    """One bounded sequential hierarchy observation and its maintenance checks."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_SEARCH_MAINTAIN_OBSERVATION"] = (
        "GOOGLE_ADS_V25_SEARCH_MAINTAIN_OBSERVATION"
    )
    campaign_name: str = Field(min_length=1, max_length=255)
    projection: GoogleAdsLiveHierarchyProjection
    observed_budget_total_amount_micros: int = Field(ge=_USD_MINOR_TO_MICROS, le=_MAX_MICROS)
    observed_cpc_bid_micros: int | None = Field(default=None, ge=1, le=_MAX_MICROS)
    observed_start_date_time: str = Field(pattern=DATE_TIME.pattern)
    observed_end_date_time: str = Field(pattern=DATE_TIME.pattern)
    observed_ad_schedule_count: int = Field(ge=0, le=21)
    preconditions: GoogleAdsMaintainPreconditions
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
    def bounded_observation(self) -> GoogleAdsMaintainObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("the maintain observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("the maintain observation interval exceeds the local bound")
        if GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(self.campaign_name):
            raise ValueError("this build refuses to construct a quoted GAQL campaign name")
        if self.projection.campaign_name != self.campaign_name:
            raise ValueError("the observation projects a different campaign")
        campaign = self.projection.configured("CAMPAIGN")
        budget = self.projection.configured("CAMPAIGN_BUDGET")
        ad_group = self.projection.configured("AD_GROUP")
        bid = (
            "NOT_SET" if self.observed_cpc_bid_micros is None else str(self.observed_cpc_bid_micros)
        )
        schedules = sum(
            member.member_kind == "CAMPAIGN_CRITERION_AD_SCHEDULE"
            for member in self.projection.members
        )
        if (
            campaign.get("start_date_time") != self.observed_start_date_time
            or campaign.get("end_date_time") != self.observed_end_date_time
            or campaign.get("advertising_channel_type") != "SEARCH"
            or campaign.get("status") != self.preconditions.observed_campaign_status
            or budget.get("period") != "CUSTOM_PERIOD"
            or budget.get("total_amount_micros") != str(self.observed_budget_total_amount_micros)
            or ad_group.get("cpc_bid_micros") != bid
            or schedules != self.observed_ad_schedule_count
        ):
            raise ValueError("the observation restates facts its own projection does not hold")
        return self


class GoogleAdsMaintainPostcondition(StrictModel):
    """Closed configuration-only postcondition for one maintenance operation."""

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    campaign_name: str = Field(min_length=1, max_length=255)
    maintenance_kind: MaintenanceKind
    #: For every kind except the archive this is the predicted member multiset;
    #: an archive predicts no live hierarchy at all under this binding.
    predicted_hierarchy_digest: Digest | None = None
    member_count: int | None = Field(default=None, ge=6, le=140)
    configured_status: Literal["UNCHANGED", "REMOVED"]
    reversibility: Literal["REVERSIBLE_BY_A_NEW_PLAN", "IRREVERSIBLE_ARCHIVE"]
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    maximum_spend: Money

    @model_validator(mode="after")
    def exact_shape_for_kind(self) -> GoogleAdsMaintainPostcondition:
        if self.account_ref != f"google_ads.customer.{self.account_client_ref}":
            raise ValueError("postcondition references do not bind the Google Ads customer")
        archive = self.maintenance_kind == "CAMPAIGN_ARCHIVE"
        if archive:
            if (
                self.predicted_hierarchy_digest is not None
                or self.member_count is not None
                or self.configured_status != "REMOVED"
                or self.reversibility != "IRREVERSIBLE_ARCHIVE"
                or self.maximum_spend.minor_units != 0
            ):
                raise ValueError("an archive postcondition is removed, irreversible, and zero cap")
        elif (
            self.predicted_hierarchy_digest is None
            or self.member_count is None
            or self.configured_status != "UNCHANGED"
            or self.reversibility != "REVERSIBLE_BY_A_NEW_PLAN"
            or self.maximum_spend.minor_units < 1
        ):
            raise ValueError("a maintenance postcondition keeps a live hierarchy and a spend cap")
        return self


class GoogleAdsMaintainRedactedReview(StrictModel):
    """The closed review this plan's ``review_lines`` are rendered from."""

    maintenance_kind: MaintenanceKind
    maintenance_effect: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,95}$")
    atomicity: Literal["ONE_MUTATE_PARTIAL_FAILURE_DISABLED"] = (
        "ONE_MUTATE_PARTIAL_FAILURE_DISABLED"
    )
    campaign_status_before: Literal["ENABLED", "PAUSED"]
    campaign_status_after: Literal["UNCHANGED", "REMOVED"]
    #: The AGENTS rule 11 basis a reviewer sees.  The delete-class archive must
    #: carry ``PROVEN_UNREFERENCED`` and the other three kinds must carry
    #: ``NOT_APPLICABLE_NOT_DELETE_CLASS``; no archive plan exists to say less.
    dependency_proof: Literal["PROVEN_UNREFERENCED", "NOT_APPLICABLE_NOT_DELETE_CLASS"]
    live_validated: Literal[False] = False
    new_value: str = Field(pattern=r"^[A-Z0-9_]{1,64}$|^[0-9]{1,20}$")
    previous_value: str = Field(pattern=r"^[A-Z0-9_]{1,64}$|^[0-9]{1,20}$")
    recovery: Literal["RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"] = (
        "RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"
    )
    reversibility: Literal["REVERSIBLE_BY_A_NEW_PLAN", "IRREVERSIBLE_ARCHIVE"]

    @field_validator("live_validated", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("a maintenance review cannot claim live validation")
        return value

    @model_validator(mode="after")
    def dependency_proof_matches_the_kind(self) -> GoogleAdsMaintainRedactedReview:
        expected = (
            "PROVEN_UNREFERENCED"
            if self.maintenance_kind == "CAMPAIGN_ARCHIVE"
            else "NOT_APPLICABLE_NOT_DELETE_CLASS"
        )
        if self.dependency_proof != expected:
            raise ValueError("only the delete-class archive carries a rule 11 dependency proof")
        return self


# -- derivation --------------------------------------------------------------


def _replace_configuration(
    member: GoogleAdsSearchHierarchyMember, key: str, value: str
) -> GoogleAdsSearchHierarchyMember:
    return GoogleAdsSearchHierarchyMember(
        member_kind=member.member_kind,
        identity=member.identity,
        configuration=tuple(
            (name, value if name == key else current) for name, current in member.configuration
        ),
    )


def maintained_projection(
    projection: GoogleAdsLiveHierarchyProjection, intent: MaintenanceIntent
) -> GoogleAdsLiveHierarchyProjection | None:
    """Return the exact predicted hierarchy, or ``None`` for an archive."""

    kind = intent.maintenance_kind
    if kind == "CAMPAIGN_ARCHIVE":
        return None
    members: list[GoogleAdsSearchHierarchyMember] = []
    if kind == "AD_SCHEDULE_REPLACEMENT":
        assert isinstance(intent, GoogleAdsAdScheduleReplacementIntent)
        status = projection.configured("CAMPAIGN")["status"]
        for member in projection.members:
            if member.member_kind != "CAMPAIGN_CRITERION_AD_SCHEDULE":
                members.append(member)
        for schedule in intent.schedules:
            members.append(
                GoogleAdsSearchHierarchyMember(
                    member_kind="CAMPAIGN_CRITERION_AD_SCHEDULE",
                    identity=schedule.identity,
                    configuration=(("negative", "false"), ("status", status)),
                )
            )
        return GoogleAdsLiveHierarchyProjection(
            campaign_name=projection.campaign_name, members=tuple(members)
        )
    for member in projection.members:
        if kind == "CAMPAIGN_BUDGET_AMOUNT" and member.member_kind == "CAMPAIGN_BUDGET":
            assert isinstance(intent, GoogleAdsBudgetAmountIntent)
            members.append(
                _replace_configuration(
                    member, "total_amount_micros", str(intent.new_total_amount_micros)
                )
            )
        elif kind == "AD_GROUP_CPC_BID" and member.member_kind == "AD_GROUP":
            assert isinstance(intent, GoogleAdsAdGroupBidIntent)
            members.append(
                _replace_configuration(member, "cpc_bid_micros", str(intent.new_cpc_bid_micros))
            )
        else:
            members.append(member)
    return GoogleAdsLiveHierarchyProjection(
        campaign_name=projection.campaign_name, members=tuple(members)
    )


def maintenance_values(
    observation: GoogleAdsMaintainObservationBinding, intent: MaintenanceIntent
) -> tuple[str, str]:
    """Return the ``(previous, new)`` review values for one maintenance kind."""

    kind = intent.maintenance_kind
    if kind == "CAMPAIGN_BUDGET_AMOUNT":
        assert isinstance(intent, GoogleAdsBudgetAmountIntent)
        return (
            str(observation.observed_budget_total_amount_micros),
            str(intent.new_total_amount_micros),
        )
    if kind == "AD_GROUP_CPC_BID":
        assert isinstance(intent, GoogleAdsAdGroupBidIntent)
        previous = (
            "NOT_SET"
            if observation.observed_cpc_bid_micros is None
            else str(observation.observed_cpc_bid_micros)
        )
        return previous, str(intent.new_cpc_bid_micros)
    if kind == "AD_SCHEDULE_REPLACEMENT":
        assert isinstance(intent, GoogleAdsAdScheduleReplacementIntent)
        return str(observation.observed_ad_schedule_count), str(len(intent.schedules))
    return observation.preconditions.observed_campaign_status, _ARCHIVED_STATUS


_MAINTENANCE_EFFECTS: Final[dict[str, str]] = {
    "CAMPAIGN_BUDGET_AMOUNT": "CAMPAIGN_LIFETIME_TOTAL_BUDGET_MICROS_CHANGED",
    "AD_GROUP_CPC_BID": "AD_GROUP_MAX_CPC_BID_MICROS_CHANGED_SPEND_AFFECTING",
    "AD_SCHEDULE_REPLACEMENT": "AD_SCHEDULE_CRITERIA_REMOVED_AND_RECREATED_ATOMICALLY",
    "CAMPAIGN_ARCHIVE": "CAMPAIGN_STATUS_SET_TO_REMOVED_IRREVERSIBLE",
}


def redacted_review(
    observation: GoogleAdsMaintainObservationBinding, intent: MaintenanceIntent
) -> GoogleAdsMaintainRedactedReview:
    """Build the closed review model the retained plan's lines are rendered from."""

    kind = intent.maintenance_kind
    archive = kind == "CAMPAIGN_ARCHIVE"
    previous, new = maintenance_values(observation, intent)
    proof = observation.preconditions.dependency_proof
    return GoogleAdsMaintainRedactedReview(
        maintenance_kind=kind,
        maintenance_effect=_MAINTENANCE_EFFECTS[kind],
        campaign_status_before=observation.preconditions.observed_campaign_status,
        campaign_status_after=_ARCHIVED_STATUS if archive else "UNCHANGED",
        dependency_proof=("NOT_APPLICABLE_NOT_DELETE_CLASS" if proof is None else proof.status),
        new_value=new,
        previous_value=previous,
        reversibility="IRREVERSIBLE_ARCHIVE" if archive else "REVERSIBLE_BY_A_NEW_PLAN",
    )


def maintenance_maximum_spend(
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsMaintainObservationBinding,
    intent: MaintenanceIntent,
) -> Money:
    """Return the declared spend bound for one maintenance kind.

    An archive removes delivery capability, so it declares an explicit zero cap
    and summarizes as ``NOT_SPEND_CAPABLE``.  A budget change declares the new
    amount.  A bid or schedule change declares the campaign budget that bounds
    the whole flight: neither is spend-neutral, and neither has a smaller
    provider-enforceable bound than the budget itself.
    """

    kind = intent.maintenance_kind
    if kind == "CAMPAIGN_ARCHIVE":
        return Money(minor_units=0, currency=account.currency)
    if kind == "CAMPAIGN_BUDGET_AMOUNT":
        assert isinstance(intent, GoogleAdsBudgetAmountIntent)
        return budget_minor_units(intent.new_total_amount_micros, account.currency)
    return budget_minor_units(observation.observed_budget_total_amount_micros, account.currency)


def _postcondition(
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsMaintainObservationBinding,
    intent: MaintenanceIntent,
) -> GoogleAdsMaintainPostcondition:
    predicted = maintained_projection(observation.projection, intent)
    archive = intent.maintenance_kind == "CAMPAIGN_ARCHIVE"
    return GoogleAdsMaintainPostcondition(
        account_ref=account.account_ref,
        account_client_ref=account.account_client_ref,
        campaign_name=observation.campaign_name,
        maintenance_kind=intent.maintenance_kind,
        predicted_hierarchy_digest=(
            None
            if predicted is None
            else digest(predicted.model_dump(mode="json", warnings="error"))
        ),
        member_count=None if predicted is None else len(predicted.members),
        configured_status=_ARCHIVED_STATUS if archive else "UNCHANGED",
        reversibility="IRREVERSIBLE_ARCHIVE" if archive else "REVERSIBLE_BY_A_NEW_PLAN",
        maximum_spend=maintenance_maximum_spend(account, observation, intent),
    )


def _states(
    *,
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsMaintainObservationBinding,
    intent: MaintenanceIntent,
    fingerprint_key: bytes,
) -> tuple[StateRef, StateRef]:
    before = semantic_hierarchy_state(
        observation.projection,
        account_client_ref=account.account_client_ref,
        campaign_name=observation.campaign_name,
        fingerprint_key=fingerprint_key,
    )
    after = semantic_hierarchy_state(
        maintained_projection(observation.projection, intent),
        account_client_ref=account.account_client_ref,
        campaign_name=observation.campaign_name,
        fingerprint_key=fingerprint_key,
    )
    if before == after:
        raise ValueError("a maintenance plan must change the observed hierarchy state")
    return before, after


def _action_id(
    *,
    workspace: GoogleAdsCampaignPauseWorkspaceBinding,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    intent: MaintenanceIntent,
    observation: GoogleAdsMaintainObservationBinding,
    postcondition: GoogleAdsMaintainPostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> str:
    return digest(
        {
            "kind": "google-ads-search-campaign-maintain-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "account": account.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "intent": intent.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "before": before.model_dump(mode="json", warnings="error"),
            "after": after.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": GOOGLE_ADS_MAINTAIN_PLAN_STATUS,
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_MAINTAIN",
            "recovery_semantics": "RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT",
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
    intent: MaintenanceIntent,
    postcondition: GoogleAdsMaintainPostcondition,
    before: StateRef,
    after: StateRef,
    expires_at: datetime,
) -> CompositePlan:
    kind = intent.maintenance_kind
    operation = MAINTENANCE_OPERATIONS[kind]
    identity = AccountIdentity(
        account_ref=account.account_ref,
        stable_account_id=account.account_client_ref,
        auth_profile_ref=execution.profile_ref,
        principal_ref=execution.principal_ref,
        client_application_ref=execution.client_application_ref,
        scopes=(GOOGLE_ADS_MAINTAIN_SCOPE,),
    )
    paths = {
        "CAMPAIGN_BUDGET_AMOUNT": "campaign_budget/total_amount_micros",
        "AD_GROUP_CPC_BID": "ad_group/cpc_bid_micros",
        "AD_SCHEDULE_REPLACEMENT": "campaign_criterion/ad_schedule",
        "CAMPAIGN_ARCHIVE": "campaign/status",
    }
    subplan = SubPlan(
        subplan_id=f"google-ads-search-campaign-maintain-{action_id}",
        provider="google_ads",
        operation=operation,
        account=identity,
        provider_api_version=GOOGLE_ADS_MAINTAIN_API_VERSION,
        capability_version=GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path=paths[kind], before=before, after=after),),
        max_spend=postcondition.maximum_spend,
        egress=(
            EgressTarget(host=GOOGLE_ADS_MAINTAIN_HOST, purpose=MAINTENANCE_EGRESS_PURPOSES[kind]),
        ),
        # The one bounded compensating action.  An archive is irreversible, so
        # the only honest recovery is to observe the current state; ``RECOVER``
        # is not a compiled subplan operation, and ``ARCHIVE`` names the
        # operation whose attempt a recovery would reconcile.
        recovery_actions=(
            (OperationKind.ARCHIVE,) if kind == "CAMPAIGN_ARCHIVE" else (OperationKind.UPDATE,)
        ),
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"google-ads-search-campaign-maintain-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    intent: MaintenanceIntent,
    observation: GoogleAdsMaintainObservationBinding,
    expires_at: datetime,
) -> None:
    if account.account_status != "ENABLED":
        raise ValueError("a maintenance plan requires an ENABLED Google Ads customer")
    archive = intent.maintenance_kind == "CAMPAIGN_ARCHIVE"
    if archive and observation.preconditions.observed_campaign_status == ACTIVATED_STATUS:
        raise ValueError("an ENABLED Google Ads campaign must be paused before it is archived")
    # AGENTS rule 11.  An archive is delete-class, so the substantive gate is
    # the read-derived dependency proof and never the paused status or the
    # request's own irreversibility acknowledgement, which stays necessary but
    # is not sufficient.  Every other kind must carry no proof at all, so the
    # closed action shape is exact for each maintenance kind.
    if archive and observation.preconditions.dependency_proof is None:
        raise ValueError("an archive requires a PROVEN_UNREFERENCED dependency proof")
    if not archive and observation.preconditions.dependency_proof is not None:
        raise ValueError("only an archive carries a campaign dependency proof")
    if execution.resolved_at > observation.read_started_at:
        raise ValueError("EDIT association occurs after the maintenance read began")
    if observation.read_finished_at >= expires_at:
        raise ValueError("plan expiry must follow the completed maintenance read")
    if expires_at > execution.expires_at:
        raise ValueError("plan expiry exceeds the declared bearer expiry")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")


class GoogleAdsSearchCampaignMaintainPlannedAction(StrictModel):
    """One exact guarded Search maintenance operation, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = GOOGLE_ADS_MAINTAIN_PLAN_STATUS
    action_id: Digest
    workspace_binding: GoogleAdsCampaignPauseWorkspaceBinding
    account_binding: GoogleAdsCampaignPauseAccountBinding
    execution_binding: GoogleAdsCampaignPauseExecutionBinding
    intent: MaintenanceIntent = Field(discriminator="maintenance_kind")
    observation_binding: GoogleAdsMaintainObservationBinding
    postcondition: GoogleAdsMaintainPostcondition
    plan: CompositePlan
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    configuration_readback: Literal["REQUIRED_AFTER_MAINTAIN"] = "REQUIRED_AFTER_MAINTAIN"
    recovery_semantics: Literal["RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"] = (
        "RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"
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
    def exact_reconstructed_action(self) -> GoogleAdsSearchCampaignMaintainPlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only complete, closed, validated serializable action data."""

        checked = revalidate_google_ads_search_campaign_maintain_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def build_google_ads_search_campaign_maintain_plan(
    *,
    workspace_profile_ref: str,
    account_binding: GoogleAdsCampaignPauseAccountBinding,
    execution_binding: GoogleAdsCampaignPauseExecutionBinding,
    intent: MaintenanceIntent,
    observation_binding: GoogleAdsMaintainObservationBinding,
    expires_at: datetime,
    fingerprint_key: bytes,
) -> GoogleAdsSearchCampaignMaintainPlannedAction:
    """Build the one non-authorizing guarded Search maintenance plan."""

    try:
        workspace = GoogleAdsCampaignPauseWorkspaceBinding(
            workspace_profile_ref=workspace_profile_ref
        )
        account = _checked_member(account_binding, GoogleAdsCampaignPauseAccountBinding)
        execution = _checked_member(execution_binding, GoogleAdsCampaignPauseExecutionBinding)
        checked_intent = _checked_intent(intent)
        observation = _checked_member(observation_binding, GoogleAdsMaintainObservationBinding)
        if observation.fingerprint_key_marker != fingerprint_key_marker(fingerprint_key):
            raise ValueError("the observation was projected under a different local key")
        expiry = _checked_expiry(expires_at)
        _assert_outer_bindings(
            account=account,
            execution=execution,
            intent=checked_intent,
            observation=observation,
            expires_at=expiry,
        )
        postcondition = _postcondition(account, observation, checked_intent)
        before, after = _states(
            account=account,
            observation=observation,
            intent=checked_intent,
            fingerprint_key=fingerprint_key,
        )
        action_id = _action_id(
            workspace=workspace,
            account=account,
            execution=execution,
            intent=checked_intent,
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
            intent=checked_intent,
            postcondition=postcondition,
            before=before,
            after=after,
            expires_at=expiry,
        )
        return GoogleAdsSearchCampaignMaintainPlannedAction(
            action_id=action_id,
            workspace_binding=workspace,
            account_binding=account,
            execution_binding=execution,
            intent=checked_intent,
            observation_binding=observation,
            postcondition=postcondition,
            plan=plan,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


def predicted_states(
    action: GoogleAdsSearchCampaignMaintainPlannedAction, *, fingerprint_key: bytes
) -> tuple[StateRef, StateRef]:
    """Recompute the plan's two semantic states under the current local key."""

    return _states(
        account=action.account_binding,
        observation=action.observation_binding,
        intent=action.intent,
        fingerprint_key=fingerprint_key,
    )


#: Domain separation for the state an archive reads as when its reference
#: surfaces are no longer proven empty.
_UNPROVEN_ARCHIVE_CONTEXT: Final = (
    b"adhelm/google-ads/search-campaign-maintain/unproven-archive/v1\x00"
)
_UNPROVEN_ARCHIVE_PURPOSE: Final = "google-ads-search-campaign-maintain-unproven-archive"
_UNPROVEN_ARCHIVE_REVISION: Final = "GOOGLE_ADS_SEARCH_CAMPAIGN_ARCHIVE_DEPENDENCY_UNPROVEN_V1"


def unproven_archive_state(observed: StateRef, *, fingerprint_key: bytes) -> StateRef:
    """Return the distinct state an archive observes when it is no longer proven.

    Rule 11 gates the *precondition* of an archive, not the observation of the
    campaign.  A read that finds a reference surface that is not ``READ_EMPTY``
    therefore reports state rather than raising: the semantic fingerprint is
    re-keyed with an explicit UNPROVEN marker, so it can never equal the plan's
    ``before`` (the kernel's pre-start comparison refuses it as drift, before
    the durable start) and never equal the archived ``after``.  A recovery read
    of an interrupted attempt records the same honest observation instead of
    failing to record anything.
    """

    if type(observed) is not StateRef:
        raise ValueError("unproven archive state input")
    return StateRef(
        fingerprint=keyed_fingerprint(
            fingerprint_key,
            context=_UNPROVEN_ARCHIVE_CONTEXT,
            purpose=_UNPROVEN_ARCHIVE_PURPOSE,
            value={
                "schema_version": 1,
                "marker": "DEPENDENCY_UNPROVEN",
                "semantic_fingerprint": observed.fingerprint,
                "semantic_revision": observed.revision,
            },
        ),
        revision=_UNPROVEN_ARCHIVE_REVISION,
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


_INTENTS: Final[dict[str, type[BaseModel]]] = {
    "CAMPAIGN_BUDGET_AMOUNT": GoogleAdsBudgetAmountIntent,
    "AD_GROUP_CPC_BID": GoogleAdsAdGroupBidIntent,
    "AD_SCHEDULE_REPLACEMENT": GoogleAdsAdScheduleReplacementIntent,
    "CAMPAIGN_ARCHIVE": GoogleAdsCampaignArchiveIntent,
}


def _checked_intent(value: object) -> MaintenanceIntent:
    expected = _INTENTS.get(getattr(value, "maintenance_kind", ""))
    if expected is None or type(value) is not expected:
        raise ValueError("maintenance intent type")
    return cast(MaintenanceIntent, _checked_member(value, expected))


def _checked_expiry(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("plan expiry must be an aware datetime")
    return value.astimezone(UTC)


def _assert_action_instance(action: GoogleAdsSearchCampaignMaintainPlannedAction) -> None:
    if type(action) is not GoogleAdsSearchCampaignMaintainPlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, GoogleAdsCampaignPauseWorkspaceBinding)
    account = _checked_member(action.account_binding, GoogleAdsCampaignPauseAccountBinding)
    execution = _checked_member(action.execution_binding, GoogleAdsCampaignPauseExecutionBinding)
    intent = _checked_intent(action.intent)
    observation = _checked_member(action.observation_binding, GoogleAdsMaintainObservationBinding)
    postcondition = _checked_member(action.postcondition, GoogleAdsMaintainPostcondition)
    plan = _checked_member(action.plan, CompositePlan)
    expiry = _checked_expiry(plan.expires_at)
    _assert_outer_bindings(
        account=account,
        execution=execution,
        intent=intent,
        observation=observation,
        expires_at=expiry,
    )
    if postcondition != _postcondition(account, observation, intent):
        raise ValueError("postcondition differs from the observed baseline")
    subplan = plan.subplans[0]
    expected_action_id = _action_id(
        workspace=workspace,
        account=account,
        execution=execution,
        intent=intent,
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
        intent=intent,
        postcondition=postcondition,
        before=subplan.before,
        after=subplan.after,
        expires_at=expiry,
    )
    if plan != expected_plan:
        raise ValueError("plan differs from the reconstructed Google Ads maintain action")


def revalidate_google_ads_search_campaign_maintain_planned_action(
    value: object,
) -> GoogleAdsSearchCampaignMaintainPlannedAction:
    """Strictly parse and reconstruct one closed locally retained MAINTAIN action."""

    try:
        if type(value) is GoogleAdsSearchCampaignMaintainPlannedAction:
            raw: Any = value.model_dump(mode="json", warnings="error")
        elif type(value) is dict:
            raw = value
        else:
            raise ValueError("action input type")
        encoded = _strict_json_bytes(raw, label="action")
        parsed = parse_json(encoded, maximum_bytes=_MAX_ACTION_BYTES)
        if type(parsed) is not dict or set(parsed) != set(
            GoogleAdsSearchCampaignMaintainPlannedAction.model_fields
        ):
            raise ValueError("action keys")
        action = GoogleAdsSearchCampaignMaintainPlannedAction.model_validate_json(encoded)
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


class GoogleAdsSearchCampaignMaintainPlanRecord(StrictModel):
    """The additive ``changes`` record that retains one closed MAINTAIN action."""

    schema_version: Literal[1] = 1
    event_type: Literal["GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN"] = (
        "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN"
    )
    provider: Literal["google_ads"] = "google_ads"
    action: GoogleAdsSearchCampaignMaintainPlannedAction
    action_digest: Digest
    plan_digest: Digest
    maintenance_kind: MaintenanceKind
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
    def closed_action(cls, value: object) -> GoogleAdsSearchCampaignMaintainPlannedAction:
        return revalidate_google_ads_search_campaign_maintain_planned_action(value)

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_action_binding(self) -> GoogleAdsSearchCampaignMaintainPlanRecord:
        action = self.action
        subplan = action.plan.subplans[0]
        if (
            self.action_digest != digest(action.model_dump(mode="json", warnings="error"))
            or self.plan_digest != action.plan.plan_digest
            or self.maintenance_kind != action.intent.maintenance_kind
            or self.expires_at != action.plan.expires_at
            or subplan.provider != self.provider
            or self.maximum_incremental_spend != subplan.max_spend
        ):
            raise ValueError("retained Google Ads MAINTAIN plan facts do not match the action")
        return self


def google_ads_search_campaign_maintain_plan_record(
    action: GoogleAdsSearchCampaignMaintainPlannedAction,
) -> GoogleAdsSearchCampaignMaintainPlanRecord:
    """Build the retained record for one already-closed MAINTAIN action."""

    checked = revalidate_google_ads_search_campaign_maintain_planned_action(action)
    return GoogleAdsSearchCampaignMaintainPlanRecord(
        action=checked,
        action_digest=digest(checked.model_dump(mode="json", warnings="error")),
        plan_digest=checked.plan.plan_digest,
        maintenance_kind=checked.intent.maintenance_kind,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_google_ads_search_campaign_maintain_plan_record(
    value: object,
) -> GoogleAdsSearchCampaignMaintainPlanRecord:
    """Reject incomplete, aliased, or incoherent retained MAINTAIN plan facts."""

    try:
        if type(value) is GoogleAdsSearchCampaignMaintainPlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            GoogleAdsSearchCampaignMaintainPlanRecord.model_fields
        ):
            raise ValueError("a complete record is required")
        encoded = _strict_json_bytes(value, label="plan record")
        return GoogleAdsSearchCampaignMaintainPlanRecord.model_validate_json(encoded)
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_plan(
            "A complete closed Google Ads MAINTAIN plan record is required."
        ) from None


def google_ads_maintain_plan_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive plan kind."""

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is GoogleAdsSearchCampaignMaintainPlanRecord


__all__ = [
    "CAMPAIGN_REFERENCE_SURFACES",
    "DAY_OF_WEEK",
    "GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN",
    "GOOGLE_ADS_MAINTAIN_API_VERSION",
    "GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION",
    "GOOGLE_ADS_MAINTAIN_HOST",
    "GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND",
    "GOOGLE_ADS_MAINTAIN_PLAN_STATUS",
    "GOOGLE_ADS_MAINTAIN_SCOPE",
    "MAINTENANCE_EGRESS_PURPOSES",
    "MAINTENANCE_OPERATIONS",
    "MINUTE_OF_HOUR",
    "CampaignReferenceDisposition",
    "CampaignReferenceSurface",
    "GoogleAdsAdGroupBidIntent",
    "GoogleAdsAdScheduleReplacementIntent",
    "GoogleAdsBudgetAmountIntent",
    "GoogleAdsCampaignArchiveIntent",
    "GoogleAdsCampaignDependencyProof",
    "GoogleAdsCampaignReferenceSurfaceReading",
    "GoogleAdsMaintainObservationBinding",
    "GoogleAdsMaintainPostcondition",
    "GoogleAdsMaintainPreconditions",
    "GoogleAdsMaintainRedactedReview",
    "GoogleAdsMaintenanceSchedule",
    "GoogleAdsSearchCampaignMaintainPlanRecord",
    "GoogleAdsSearchCampaignMaintainPlannedAction",
    "GoogleAdsSearchCampaignMaintainSelection",
    "MaintenanceIntent",
    "MaintenanceKind",
    "build_google_ads_search_campaign_maintain_plan",
    "google_ads_maintain_plan_record_kind_installed",
    "google_ads_search_campaign_maintain_plan_record",
    "maintained_projection",
    "maintenance_maximum_spend",
    "maintenance_values",
    "predicted_states",
    "redacted_review",
    "revalidate_google_ads_search_campaign_maintain_plan_record",
    "revalidate_google_ads_search_campaign_maintain_planned_action",
    "unproven_archive_state",
]

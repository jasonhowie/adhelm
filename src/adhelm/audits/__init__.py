# ruff: noqa: E501
"""Provider-specific audit rule seam over retained local records.

`adhelm.audit` evaluates one normalized report against one customer-declared
policy.  That evaluation is provider-neutral: it can only check values a report
declares.  This package adds the provider-specific half.  A rule module reads
**retained local records** -- the same normalized report, the retained resource
snapshots a completed inventory appended, an optional earlier snapshot set, and
an optional local creative catalog -- and returns typed findings, an explicit
coverage status for every audit dimension, and non-writing recommendations.

Three invariants hold for every rule module registered here:

1. No rule module makes a provider request, reads a credential, appends a
   ledger record, or compiles a plan.  Every input is already-retained local
   evidence, so audit-time quota, 401/403, and 429 handling stays with the
   read adapters that produced those records.
2. Every audit dimension gets an explicit coverage status.  A dimension the
   provider's reviewed read does not return is `NOT_READ`; it is never quietly
   omitted and never reported as healthy.
3. A recommendation names an operation kind this product can actually compile
   for that provider, states that it is neither executed nor a plan, and
   carries no value that could be mistaken for an authorization digest.  When
   the evidence supports no such operation, the module returns an explicit
   "no recommendation" outcome with its reason instead.

## Adding a provider (the seam the remaining P01 provider tickets use)

1. Create `adhelm/audits/<provider>.py` with one
   `evaluate(context: ProviderAuditContext) -> ProviderAuditSections` entry
   point, and call `register_provider_rule("<provider>", evaluate)` at module
   scope.
2. Add `"<provider>": "adhelm.audits.<provider>"` to `_RULE_MODULES` below.
3. Add the provider's compilable operation kinds to
   `COMPILABLE_OPERATION_KINDS`.  An empty set means every proposed mutation is
   reported as `OPERATION_NOT_COMPILABLE_FOR_PROVIDER`; local re-reads
   (`OBSERVE`) are always available.

`adhelm.audit.audit_provider` performs the dispatch.  A provider with no
registered module returns the unchanged provider-neutral audit result and an
empty provider section, so registering LinkedIn changes nothing for the other
five providers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import import_module
from typing import Final, Literal, get_args

from pydantic import Field, field_validator, model_validator

from adhelm.audit import AuditDimension, AuditFinding, AuditPolicy, AuditResult, Recommendation
from adhelm.models import Provider, SafeRef, StrictModel
from adhelm.providers.ga4_admin import Ga4AdminInventory
from adhelm.reporting import NormalizedReport

ProviderCoverageStatus = Literal["COVERED", "PARTIAL", "NOT_READ", "NOT_APPLICABLE"]
# A retained analytics report is `BOUNDED_NON_EXHAUSTIVE` whenever the reviewed
# provider read cannot prove that it returned every row.  A finding derived
# from such a report may never be phrased as a total.  No reviewed provider
# analytics read is proven exhaustive today, so this vocabulary deliberately
# has no `COMPLETE` member: no model in this package can publish an exhaustive
# claim.  Promoting one provider requires changing this type, its rule module,
# and its read contract together, under review.
AnalyticsCompleteness = Literal["NOT_APPLICABLE", "BOUNDED_NON_EXHAUSTIVE"]
# How much a retained record set proves about what it does *not* contain.
#
# * `COMPLETE_BOUNDED_READ` -- the record set is one whole bounded read whose
#   completion this product recorded, so an absence is real evidence.
# * `BOUNDED_NON_EXHAUSTIVE` -- the caller deliberately selected a subset with
#   a known boundary, so an absence is weak evidence and every absence rule
#   drops to `LOW` confidence.
# * `READ_BOUNDARY_UNVERIFIED` -- the record set has no proven read boundary,
#   such as history retained before this product recorded read completions.
#   An absence carries no information at all: absence-based drift is suppressed
#   rather than reported at low confidence, because the same record set can
#   fabricate both deletions and creations.
InventoryCompleteness = Literal[
    "COMPLETE_BOUNDED_READ", "BOUNDED_NON_EXHAUSTIVE", "READ_BOUNDARY_UNVERIFIED"
]
ResourceType = Literal["ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "AD_GROUP", "AD"]
EvidenceSource = Literal[
    "PERFORMANCE_OBSERVATION",
    "RETAINED_INVENTORY",
    "EARLIER_RETAINED_INVENTORY",
    "LOCAL_CREATIVE_CATALOG",
    "RETAINED_MEASUREMENT_OBSERVATION",
]
NoRecommendationReason = Literal[
    "COVERAGE_NOT_READ",
    "EVIDENCE_INSUFFICIENT",
    "OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
]
# `OBSERVE` is the ledger's read/observation operation; the mutation kinds are
# the product's `adhelm.safety.OperationKind` values.  Naming a kind here is a
# vocabulary statement, never an authorization or a compiled plan.
RecommendedOperationKind = Literal[
    "OBSERVE",
    "CREATE",
    "UPDATE",
    "ASSOCIATE",
    "REPLACE",
    "ACTIVATE",
    "PAUSE",
    "ENABLE",
    "ARCHIVE",
    "DELETE",
    "PUBLISH",
    "RESTORE",
]
_ALWAYS_AVAILABLE_OPERATION: Final = "OBSERVE"
# A recommendation or a declined outcome states how many findings it rests on
# and names the first few inline.  The inline list is deliberately small so that
# one output row stays a constant size and the smallest page an agent can
# request always fits the default output bound, whatever the size of the
# customer's account.  Nothing is dropped: every finding is still an individual
# row in `findings`, each row states its complete `bound_finding_count` and the
# `bound_rule_ids` that define its membership, and a row that could not inline
# every identifier records an explicit `BoundExceeded` entry saying so.
MAXIMUM_INLINE_FINDING_IDS: Final = 32

# What this build can compile today for each provider.  A LinkedIn brief
# compiles only to an offline, non-executable paused campaign-group/campaign/
# creative CREATE candidate; there is no LinkedIn update, pause, association,
# or activation compiler.  Recommending one would promise an operation the
# product cannot produce.
COMPILABLE_OPERATION_KINDS: Final[Mapping[Provider, frozenset[str]]] = {
    "google_ads": frozenset({"CREATE", "PAUSE"}),
    "meta_ads": frozenset({"CREATE", "PAUSE"}),
    "linkedin_ads": frozenset({"CREATE"}),
    # The TikTok compiler produces one offline, explicitly non-executable paused
    # candidate hierarchy.  There is no TikTok pause, status-update, or any
    # other route that changes an existing TikTok resource, so the TikTok rule
    # table emits `OBSERVE` only and returns an explicit declined row for the
    # pause a serving contradiction might otherwise suggest.
    "tiktok_ads": frozenset({"CREATE"}),
    "reddit_ads": frozenset({"CREATE"}),
    "openai_ads": frozenset({"CREATE", "PAUSE"}),
    # No GTM mutation is compilable in this build.  The GTM rule table in
    # `adhelm/audits/gtm_web.py` is read-only over a retained container inventory:
    # every candidate action it could name -- repairing a reference, attaching a
    # trigger, pausing a tag, removing an unreferenced resource -- would be a
    # staged workspace change this product cannot yet produce, so each one is
    # returned as a `NoRecommendation` with
    # `OPERATION_NOT_COMPILABLE_FOR_PROVIDER`.  Deletion in particular stays out:
    # it additionally requires a `PROVEN_UNREFERENCED` classification and its own
    # separately authorized staged cleanup.
    #
    # `gtm` is deliberately absent from `_RULE_MODULES` below.  That map drives
    # `adhelm.audit.audit_provider`, which dispatches on a `NormalizedReport`; GTM
    # produces no such performance report, so the GTM rule table has its own
    # entry point over the retained inventory instead of a fabricated report.
    "gtm": frozenset(),
    # This build compiles one GA4 Admin operation: the fixed key-event CREATE
    # (`providers/ga4_admin_compiler.py`, `ga4_key_event_candidates.py`,
    # `saved_ga4_create_plans.py`, `ga4_create_execution_workflow.py`).  That is
    # what is compilable, and it is separate from what the audit recommends: the
    # GA4 rule table in `adhelm/audits/ga4.py` emits only `OBSERVE`, and returns an
    # explicit `EVIDENCE_INSUFFICIENT` decline that considers `CREATE`, because
    # the retained evidence never identifies which event the customer counts.
    # This map records what the product can compile, not what the audit emits;
    # the rule table, not this map, keeps it to `OBSERVE`.
    "ga4": frozenset({"CREATE"}),
}

_RULE_MODULES: Final[Mapping[Provider, str]] = {
    "ga4": "adhelm.audits.ga4",
    "google_ads": "adhelm.audits.google_ads",
    "linkedin_ads": "adhelm.audits.linkedin_ads",
    "meta_ads": "adhelm.audits.meta_ads",
    "openai_ads": "adhelm.audits.openai_ads",
    "tiktok_ads": "adhelm.audits.tiktok_ads",
    "reddit_ads": "adhelm.audits.reddit_ads",
}
_HEX = frozenset("0123456789abcdef")


def _record_digests(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(len(value) != 64 or any(char not in _HEX for char in value) for value in values):
        raise ValueError("Audit evidence references must be record digests")
    return values


def _aware(value: datetime, context: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must include a timezone")
    return value


def _finding_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        prefix, _, suffix = value.partition("finding:sha256:")
        if prefix or len(suffix) != 64 or any(char not in _HEX for char in suffix):
            raise ValueError("A recommendation binds to audit finding identifiers")
    return values


def _has_digest_like_run(text: str) -> bool:
    """Detect a 64-character hexadecimal run that could read as an authorization digest."""
    run = 0
    for char in text.lower():
        run = run + 1 if char in _HEX else 0
        if run >= 64:
            return True
    return False


class ResourceSnapshotState(StrictModel):
    """The normalized snapshot payload a completed inventory read appended."""

    provider_api_version: str = Field(min_length=1, max_length=80)
    resource_type: ResourceType
    resource_ref: SafeRef
    parent_resource_refs: tuple[SafeRef, ...] = Field(default_factory=tuple, max_length=8)
    normalized_state: dict[str, str | int | bool | None] = Field(
        default_factory=dict, max_length=32
    )
    normalized_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_status: str = Field(min_length=1, max_length=80)
    data_quality_statuses: tuple[str, ...] = Field(default_factory=tuple, max_length=32)


class RetainedSnapshotRecord(StrictModel):
    """One retained `RESOURCE_SNAPSHOT` ledger record, cited by its record digest."""

    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    snapshot: ResourceSnapshotState

    @field_validator("observed_at")
    @classmethod
    def observed_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Snapshot observation time")


class RetainedInventory(StrictModel):
    """One retained inventory read for one account.

    `completeness` is the caller's declaration about the record set it selected,
    not a provider claim.  `COMPLETE_BOUNDED_READ` means the retained set is the
    complete result of the reviewed bounded read; a subset is
    `BOUNDED_NON_EXHAUSTIVE` and lowers the confidence of every absence-based
    structure rule.
    """

    provider: Provider
    account_ref: SafeRef
    observed_at: datetime
    completeness: InventoryCompleteness
    records: tuple[RetainedSnapshotRecord, ...] = Field(default_factory=tuple, max_length=5_000)

    @field_validator("observed_at")
    @classmethod
    def observed_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Inventory observation time")

    @model_validator(mode="after")
    def resource_refs_are_unique(self) -> RetainedInventory:
        refs = [record.snapshot.resource_ref for record in self.records]
        if len(set(refs)) != len(refs):
            raise ValueError("A retained inventory holds one snapshot per resource reference")
        return self


class RetainedCreativeCatalogEntry(StrictModel):
    """One locally retained creative version and the provider resource it claims.

    Local creative registration retains `deployment_status=NOT_CREATED` and
    `provider_currentness=NOT_EVALUATED`, so a declared provider reference is a
    local claim awaiting an independently reviewed association, never proof.
    """

    creative_version_id: SafeRef
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Provider
    provider_resource_ref: SafeRef | None = None
    binding_status: Literal["NOT_CREATED", "LOCAL_DECLARED_UNVERIFIED"]

    @model_validator(mode="after")
    def declared_binding_needs_a_reference(self) -> RetainedCreativeCatalogEntry:
        if (self.binding_status == "LOCAL_DECLARED_UNVERIFIED") != (
            self.provider_resource_ref is not None
        ):
            raise ValueError("Only a declared local binding carries a provider resource reference")
        return self


class RetainedCreativeCatalog(StrictModel):
    entries: tuple[RetainedCreativeCatalogEntry, ...] = Field(
        default_factory=tuple, max_length=5_000
    )


class RetainedGa4AdminObservation(StrictModel):
    """One retained GA4 Admin `MEASUREMENT_OBSERVATION`, cited by its record digest.

    GA4 configuration is not a resource-snapshot hierarchy.  The fixed Admin read
    appends one closed inventory of key events and web-stream event rules as a
    measurement observation, and the read that produced it listed every
    collection to its last page (`pagination_verified`), so an absent key event
    is real evidence about the one web stream that was read.  Event names and
    rule content are held only as keyed fingerprints.
    """

    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    inventory: Ga4AdminInventory

    @field_validator("observed_at")
    @classmethod
    def observed_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "GA4 Admin observation time")

    def describes_report_account(self, report_account_ref: str) -> bool:
        """Whether this observation reads the property a GA4 report describes.

        A GA4 report names its property `ga4.property.<id>` and the Admin
        observation names it `ga4_admin.property.<id>`.  Both carry the same
        numeric property identifier, which is the only join between them.
        """
        return report_account_ref == f"ga4.property.{self.inventory.identity.property_client_ref}"


class ProviderAuditEvidence(StrictModel):
    """Retained local evidence supplied alongside the audited report.

    Every field is optional.  Missing evidence lowers a dimension's coverage
    status; it never produces an assumed value.
    """

    inventory: RetainedInventory | None = None
    earlier_inventory: RetainedInventory | None = None
    creative_catalog: RetainedCreativeCatalog | None = None
    ga4_admin: RetainedGa4AdminObservation | None = None
    # The GA4 property the caller asked to audit evidence for, whether or not an
    # Admin observation was retained for it.  Without it an explicit `--account`
    # naming a property with nothing retained would be indistinguishable from "no
    # selector", and the audit would silently proceed on the report's own
    # property.  `audit_provider` compares it with the report's property.
    ga4_requested_property_ref: str | None = Field(
        default=None, pattern=r"^ga4\.property\.[0-9]{1,20}$"
    )

    @model_validator(mode="after")
    def inventory_pair_is_ordered_and_matched(self) -> ProviderAuditEvidence:
        if self.earlier_inventory is None:
            return self
        if self.inventory is None:
            raise ValueError("A drift comparison needs the later inventory as well")
        if self.earlier_inventory.provider != self.inventory.provider:
            raise ValueError("A drift comparison needs one provider")
        if self.earlier_inventory.account_ref != self.inventory.account_ref:
            raise ValueError("A drift comparison needs one account")
        if self.earlier_inventory.observed_at > self.inventory.observed_at:
            raise ValueError("The earlier inventory must not be observed after the later one")
        return self


class ProviderAuditContext(StrictModel):
    """Everything a provider rule module may read.  There is no client here."""

    provider: Provider
    report: NormalizedReport
    source_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: AuditPolicy
    as_of: datetime
    evidence: ProviderAuditEvidence

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Audit as_of")


class ProviderAuditCoverage(StrictModel):
    """One audit dimension's coverage status and the evidence behind it.

    `evidence_refs` carries exact record digests only where the dimension is
    evaluated from a bounded record set, such as the one selected performance
    observation.  A dimension evaluated across a whole retained inventory names
    its sources and the number of records instead of truncating a digest list;
    every individual finding still cites the exact record digests it used.
    """

    dimension: AuditDimension
    status: ProviderCoverageStatus
    evidence_sources: tuple[EvidenceSource, ...] = Field(default_factory=tuple, max_length=4)
    evidence_record_count: int = Field(default=0, ge=0)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    rationale: str = Field(min_length=1, max_length=400)

    @field_validator("evidence_refs")
    @classmethod
    def record_digests_only(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _record_digests(values)

    @field_validator("rationale")
    @classmethod
    def rationale_carries_no_digest(cls, value: str) -> str:
        if _has_digest_like_run(value):
            raise ValueError("A coverage rationale may not contain a digest-like value")
        return value

    @model_validator(mode="after")
    def unread_dimensions_cite_nothing(self) -> ProviderAuditCoverage:
        if self.status in {"NOT_READ", "NOT_APPLICABLE"} and (
            self.evidence_refs or self.evidence_sources or self.evidence_record_count
        ):
            raise ValueError("An unread or inapplicable dimension cites no evidence")
        if self.status in {"COVERED", "PARTIAL"} and (
            not self.evidence_sources or self.evidence_record_count < 1
        ):
            raise ValueError("A covered dimension names the evidence it was evaluated from")
        if len(set(self.evidence_sources)) != len(self.evidence_sources):
            raise ValueError("Each evidence source is named once")
        return self


class ProviderAuditFinding(StrictModel):
    """One canonical audit finding plus its provider-audit qualifications."""

    finding: AuditFinding
    dimension: AuditDimension
    coverage_status: ProviderCoverageStatus
    analytics_completeness: AnalyticsCompleteness
    # `AnalyticsCompleteness` has no exhaustive member, so no finding,
    # recommendation, or section can claim a retained report was complete.
    rule_id: str = Field(min_length=1, max_length=120)


class ProviderRecommendation(StrictModel):
    """A non-writing recommendation bound to the findings that justify it."""

    recommendation: Recommendation
    # The first identifiers by content order.  `bound_finding_count` is the
    # complete number this recommendation rests on and `bound_rule_ids` names
    # the rules that define its membership, so every finding above the inline
    # cap stays reachable by paging `findings` and matching its `rule_id`.
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=MAXIMUM_INLINE_FINDING_IDS)
    bound_finding_count: int = Field(ge=1)
    bound_rule_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    operation_kind: RecommendedOperationKind
    provider: Provider
    execution_status: Literal["NOT_EXECUTED"] = "NOT_EXECUTED"
    plan_status: Literal["NOT_A_PLAN"] = "NOT_A_PLAN"
    authorization_status: Literal["NO_AUTHORIZATION_DIGEST_PRESENT"] = (
        "NO_AUTHORIZATION_DIGEST_PRESENT"
    )
    analytics_completeness: AnalyticsCompleteness

    @field_validator("finding_ids")
    @classmethod
    def finding_ids_are_content_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _finding_ids(values)

    @model_validator(mode="after")
    def inline_identifiers_do_not_exceed_the_bound_count(self) -> ProviderRecommendation:
        if self.bound_finding_count < len(self.finding_ids):
            raise ValueError("A recommendation cannot name more findings than it rests on")
        return self

    @model_validator(mode="after")
    def carries_no_authorization_digest(self) -> ProviderRecommendation:
        if self.recommendation.proposed_plan_ref is not None:
            raise ValueError("A recommendation never references a plan")
        texts = (
            self.recommendation.hypothesis,
            self.recommendation.expected_impact,
            *self.recommendation.observations,
            *self.recommendation.assumptions,
        )
        if any(_has_digest_like_run(text) for text in texts):
            raise ValueError("Recommendation prose may not contain a digest-like value")
        return self

    @model_validator(mode="after")
    def operation_is_compilable_for_this_provider(self) -> ProviderRecommendation:
        allowed = COMPILABLE_OPERATION_KINDS.get(self.provider, frozenset())
        if (
            self.operation_kind != _ALWAYS_AVAILABLE_OPERATION
            and self.operation_kind not in allowed
        ):
            raise ValueError("This product cannot compile the recommended operation")
        return self


class NoRecommendation(StrictModel):
    """An explicit, reasoned absence of a recommendation."""

    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=MAXIMUM_INLINE_FINDING_IDS)
    bound_finding_count: int = Field(ge=1)
    bound_rule_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    reason: NoRecommendationReason
    detail: str = Field(min_length=1, max_length=400)
    considered_operation_kind: RecommendedOperationKind | None = None

    @field_validator("finding_ids")
    @classmethod
    def finding_ids_are_content_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _finding_ids(values)

    @field_validator("detail")
    @classmethod
    def detail_carries_no_digest(cls, value: str) -> str:
        if _has_digest_like_run(value):
            raise ValueError("A declined outcome may not contain a digest-like value")
        return value

    @model_validator(mode="after")
    def inline_identifiers_do_not_exceed_the_bound_count(self) -> NoRecommendation:
        if self.bound_finding_count < len(self.finding_ids):
            raise ValueError("A declined outcome cannot name more findings than it rests on")
        return self


class BoundExceeded(StrictModel):
    """One output row that could not inline every finding identifier it rests on.

    This is the explicit alternative to silent truncation.  It names the scope,
    the inline bound, the complete total, and how the remainder is reached.  No
    finding is dropped: each one remains an individual row in `findings`, and
    `rule_ids` is the exact membership test for the row that could not name it.
    This entry stays a constant size so that paging it, and paging every other
    list, keeps the smallest page inside the output bound.
    """

    scope: Literal["RECOMMENDATION_FINDING_IDS", "DECLINED_FINDING_IDS"]
    subject: str = Field(min_length=1, max_length=200)
    bound: int = Field(ge=1)
    total_finding_count: int = Field(ge=1)
    inlined_finding_count: int = Field(ge=1)
    omitted_finding_count: int = Field(ge=1)
    rule_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    resolution: Literal["PAGE_FINDINGS_AND_MATCH_RULE_ID"] = "PAGE_FINDINGS_AND_MATCH_RULE_ID"

    @field_validator("subject")
    @classmethod
    def subject_carries_no_digest(cls, value: str) -> str:
        if _has_digest_like_run(value):
            raise ValueError("A bound record may not contain a digest-like value")
        return value

    @model_validator(mode="after")
    def totals_are_consistent(self) -> BoundExceeded:
        if self.total_finding_count <= self.bound:
            raise ValueError("A bound is only exceeded above the bound")
        if self.inlined_finding_count != self.bound:
            raise ValueError("An exceeded row inlines exactly its bound")
        if self.inlined_finding_count + self.omitted_finding_count != self.total_finding_count:
            raise ValueError("Every finding above the bound is accounted for")
        return self


class DriftObservation(StrictModel):
    """One out-of-band change between two retained snapshots of one resource.

    The observation carries the interval between the two retained observations
    and an `UNKNOWN` actor.  It never invents an exact external change time or
    attributes the change to a person, and it reports only the changed field
    paths, never their values.
    """

    resource_type: ResourceType
    resource_ref: SafeRef
    change_kind: Literal[
        "STATE_CHANGED",
        "RESOURCE_ABSENT_FROM_LATER_INVENTORY",
        "RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
    ]
    changed_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    before_record_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_record_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observation_interval_start: datetime
    observation_interval_end: datetime
    observed_actor: Literal["UNKNOWN"] = "UNKNOWN"
    finding_id: str = Field(pattern=r"^finding:sha256:[0-9a-f]{64}$")

    @field_validator("observation_interval_start", "observation_interval_end")
    @classmethod
    def interval_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Drift observation interval")

    @model_validator(mode="after")
    def interval_is_ordered(self) -> DriftObservation:
        if self.observation_interval_end < self.observation_interval_start:
            raise ValueError("A drift observation interval must not end before it starts")
        if self.change_kind == "STATE_CHANGED" and not self.changed_paths:
            raise ValueError("A state change names the paths that differ")
        # An observation that claims two retained snapshots differ must cite
        # both of them; one that claims a resource is missing from one side
        # cites the side that holds it.
        required = {
            "STATE_CHANGED": ("before_record_digest", "after_record_digest"),
            "RESOURCE_ABSENT_FROM_LATER_INVENTORY": ("before_record_digest",),
            "RESOURCE_ABSENT_FROM_EARLIER_INVENTORY": ("after_record_digest",),
        }[self.change_kind]
        if any(getattr(self, field) is None for field in required):
            raise ValueError("A drift observation cites every snapshot it compared")
        if self.change_kind != "STATE_CHANGED" and self.changed_paths:
            raise ValueError("An absence observation compares no field values")
        return self


class ProviderAuditSections(StrictModel):
    """What one provider rule module returns."""

    provider: Provider
    rule_table_version: str = Field(min_length=1, max_length=40)
    live_provider_reads: Literal["NONE_RETAINED_RECORDS_ONLY"] = "NONE_RETAINED_RECORDS_ONLY"
    analytics_completeness: AnalyticsCompleteness
    coverage: tuple[ProviderAuditCoverage, ...] = Field(default_factory=tuple, max_length=32)
    findings: tuple[ProviderAuditFinding, ...] = Field(default_factory=tuple, max_length=5_000)
    recommendations: tuple[ProviderRecommendation, ...] = Field(
        default_factory=tuple, max_length=1_000
    )
    no_recommendations: tuple[NoRecommendation, ...] = Field(
        default_factory=tuple, max_length=1_000
    )
    drift_observations: tuple[DriftObservation, ...] = Field(
        default_factory=tuple, max_length=5_000
    )
    bound_exceeded: tuple[BoundExceeded, ...] = Field(default_factory=tuple, max_length=64)

    @model_validator(mode="after")
    def every_dimension_has_exactly_one_coverage_status(self) -> ProviderAuditSections:
        dimensions = [item.dimension for item in self.coverage]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("Each audit dimension has exactly one coverage status")
        # Invariant 2 of this package: a dimension is never quietly omitted.
        if set(dimensions) != set(get_args(AuditDimension)):
            raise ValueError("Every audit dimension carries an explicit coverage status")
        by_dimension = {item.dimension: item.status for item in self.coverage}
        for finding in self.findings:
            if by_dimension.get(finding.dimension) != finding.coverage_status:
                raise ValueError("A finding restates its dimension's coverage status")
        return self


class ProviderAuditResult(StrictModel):
    """The provider-neutral audit result plus the provider rule module's output."""

    provider: Provider
    audit: AuditResult
    rules_status: Literal["EVALUATED", "NO_PROVIDER_RULE_MODULE"]
    sections: ProviderAuditSections | None = None

    @model_validator(mode="after")
    def evaluated_results_carry_sections(self) -> ProviderAuditResult:
        if (self.rules_status == "EVALUATED") != (self.sections is not None):
            raise ValueError("An evaluated provider audit carries its rule sections")
        if self.sections is not None and self.sections.provider != self.provider:
            raise ValueError("Provider rule sections belong to the audited provider")
        return self


ProviderRule = Callable[[ProviderAuditContext], ProviderAuditSections]
_REGISTERED: Final[dict[str, ProviderRule]] = {}


def register_provider_rule(provider: Provider, rule: ProviderRule) -> None:
    """Register one provider's rule entry point.  Re-registration is rejected."""
    if provider in _REGISTERED and _REGISTERED[provider] is not rule:
        raise ValueError("A provider has exactly one registered audit rule module")
    _REGISTERED[provider] = rule


def provider_rule(provider: Provider) -> ProviderRule | None:
    """Return the registered rule entry point for a provider, or None.

    The rule module is imported on demand so that `adhelm.audit` keeps no import
    dependency on any provider rule table.
    """
    if provider not in _REGISTERED:
        module = _RULE_MODULES.get(provider)
        if module is None:
            return None
        import_module(module)
    return _REGISTERED.get(provider)


def snapshot_state_deltas(
    before: ResourceSnapshotState, after: ResourceSnapshotState
) -> tuple[str, ...]:
    """Changed paths between two retained snapshots, without their values.

    This mirrors the local history snapshot diff exactly: one
    `normalized_state.<field>` path per differing field, in sorted field order,
    followed by `parent_resource_refs` when the parent chain differs.
    `tests/test_linkedin_audit.py` pins that equivalence against the inventory
    implementation so the two never drift apart.
    """
    paths = [
        f"normalized_state.{field}"
        for field in sorted(set(before.normalized_state) | set(after.normalized_state))
        if before.normalized_state.get(field) != after.normalized_state.get(field)
    ]
    if before.parent_resource_refs != after.parent_resource_refs:
        paths.append("parent_resource_refs")
    return tuple(paths)


__all__ = [
    "COMPILABLE_OPERATION_KINDS",
    "MAXIMUM_INLINE_FINDING_IDS",
    "AnalyticsCompleteness",
    "BoundExceeded",
    "DriftObservation",
    "EvidenceSource",
    "InventoryCompleteness",
    "NoRecommendation",
    "NoRecommendationReason",
    "ProviderAuditContext",
    "ProviderAuditCoverage",
    "ProviderAuditEvidence",
    "ProviderAuditFinding",
    "ProviderAuditResult",
    "ProviderAuditSections",
    "ProviderCoverageStatus",
    "ProviderRecommendation",
    "ProviderRule",
    "RecommendedOperationKind",
    "ResourceSnapshotState",
    "ResourceType",
    "RetainedCreativeCatalog",
    "RetainedCreativeCatalogEntry",
    "RetainedGa4AdminObservation",
    "RetainedInventory",
    "RetainedSnapshotRecord",
    "provider_rule",
    "register_provider_rule",
    "snapshot_state_deltas",
]

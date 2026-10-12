"""Compare only two verified, retained measurement observations from one checkpoint.

This module has no provider, credential, mutation, or ledger-append surface.  It
reads one verified local checkpoint and turns only the three closed normalized
measurement schemas into bounded facts.  It deliberately does not perform a
generic JSON diff.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import Field, ValidationError, field_validator, model_validator

from adhelm.campaign_joins import (
    SIDE_BY_SIDE_DISCLOSURE,
    CampaignJoinMap,
    CampaignJoinResolution,
    JoinBoundaryStatus,
    refuse_cross_source_derivation,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_chunks import CHUNKED_KIND, OBSERVATION_KINDS
from adhelm.measurement_records import (
    Ga4MeasurementSnapshot,
    GtmFullInventoryMeasurementSnapshot,
    GtmMeasurementSnapshot,
    MeasurementObservation,
    MeasurementSnapshot,
    OpenAIMeasurementSnapshot,
)
from adhelm.models import AD_PROVIDERS, Money, Provider, SafeRef, StrictModel
from adhelm.providers.gtm_read import GtmVersionSnapshot
from adhelm.reporting import (
    JoinSource,
    JoinStatus,
    NormalizedReport,
    PerformanceRow,
    QualityStatus,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_COMPARISON_FACTS = 1_000
MAX_CHANGED_FIELDS = 32

type ComparisonStatus = Literal["CHANGED", "ADDED", "OBSERVED_ABSENT_FROM_LIST"]
type MeasurementComparisonProvider = Literal["ga4_admin", "openai_ads", "gtm"]
type FingerprintKeyContinuity = Literal[
    "NOT_EVIDENCED", "NOT_APPLICABLE", "MATCHING_RECORDED_KEY_MARKERS"
]
type MeasurementComparisonCapability = Literal[
    "ga4-admin-fixed-inventory-1",
    "openai-standard-conversion-inventory-1",
    "gtm-versions-workspace-metadata-1",
]
type ComparisonResourceType = Literal[
    "GA4_KEY_EVENT",
    "GA4_EVENT_CREATE_RULE",
    "GA4_EVENT_EDIT_RULE",
    "OPENAI_ACCOUNT_SETTINGS",
    "OPENAI_CONVERSION_SETTING",
    "GTM_ACCOUNT_FEATURES",
    "GTM_CONTAINER_FEATURES",
    "GTM_LIVE_VERSION",
    "GTM_LATEST_VERSION",
    "GTM_LATEST_HEADER_COUNTS",
    "GTM_WORKSPACE",
    "GTM_OPAQUE_CODE_MARKER",
    "GTM_INVENTORY_COVERAGE",
]
type ComparisonField = Literal[
    "configuration_fingerprint",
    "event_name_fingerprint",
    "fingerprint_scheme",
    "counting_method",
    "default_value",
    "custom",
    "deletable",
    "condition_count",
    "parameter_mutation_count",
    "source_copy_parameters",
    "processing_order",
    "inspection_status",
    "timezone",
    "currency",
    "status",
    "event_type",
    "attribution_window_days",
    "source_count",
    "campaign_count",
    "version",
    "provider_api_version",
    "support_user_permissions",
    "support_multiple_containers",
    "support_environments",
    "support_workspaces",
    "support_gtag_configs",
    "support_built_in_variables",
    "support_clients",
    "support_folders",
    "support_tags",
    "support_templates",
    "support_triggers",
    "support_variables",
    "support_versions",
    "support_zones",
    "support_transformations",
    "version_ref",
    "resource_counts.tags",
    "resource_counts.triggers",
    "resource_counts.variables",
    "resource_counts.folders",
    "resource_counts.built_in_variables",
    "resource_counts.zones",
    "resource_counts.custom_templates",
    "resource_counts.clients",
    "resource_counts.gtag_configs",
    "resource_counts.transformations",
    "compiler_status",
    "browser_status",
    "missing_counter_fields",
    "contents_status",
    "dependency_status",
    "observed_count",
    "inventory_coverage",
    "dependency_completeness",
    "observation_status",
    "staging_eligibility",
]

_ALLOWED_FIELDS: dict[ComparisonResourceType, tuple[ComparisonField, ...]] = {
    "GA4_KEY_EVENT": (
        "configuration_fingerprint",
        "event_name_fingerprint",
        "fingerprint_scheme",
        "counting_method",
        "default_value",
        "custom",
        "deletable",
    ),
    "GA4_EVENT_CREATE_RULE": (
        "configuration_fingerprint",
        "fingerprint_scheme",
        "condition_count",
        "parameter_mutation_count",
        "source_copy_parameters",
        "inspection_status",
    ),
    "GA4_EVENT_EDIT_RULE": (
        "configuration_fingerprint",
        "fingerprint_scheme",
        "condition_count",
        "parameter_mutation_count",
        "processing_order",
        "inspection_status",
    ),
    "OPENAI_ACCOUNT_SETTINGS": ("timezone", "currency", "status"),
    "OPENAI_CONVERSION_SETTING": (
        "event_type",
        "attribution_window_days",
        "status",
        "source_count",
        "campaign_count",
        "version",
        "provider_api_version",
    ),
    "GTM_ACCOUNT_FEATURES": ("support_user_permissions", "support_multiple_containers"),
    "GTM_CONTAINER_FEATURES": (
        "support_user_permissions",
        "support_environments",
        "support_workspaces",
        "support_gtag_configs",
        "support_built_in_variables",
        "support_clients",
        "support_folders",
        "support_tags",
        "support_templates",
        "support_triggers",
        "support_variables",
        "support_versions",
        "support_zones",
        "support_transformations",
    ),
    "GTM_LIVE_VERSION": (
        "version_ref",
        "configuration_fingerprint",
        "resource_counts.tags",
        "resource_counts.triggers",
        "resource_counts.variables",
        "resource_counts.folders",
        "resource_counts.built_in_variables",
        "resource_counts.zones",
        "resource_counts.custom_templates",
        "resource_counts.clients",
        "resource_counts.gtag_configs",
        "resource_counts.transformations",
        "compiler_status",
        "browser_status",
    ),
    "GTM_LATEST_VERSION": (
        "version_ref",
        "configuration_fingerprint",
        "resource_counts.tags",
        "resource_counts.triggers",
        "resource_counts.variables",
        "resource_counts.folders",
        "resource_counts.built_in_variables",
        "resource_counts.zones",
        "resource_counts.custom_templates",
        "resource_counts.clients",
        "resource_counts.gtag_configs",
        "resource_counts.transformations",
        "compiler_status",
        "browser_status",
    ),
    "GTM_LATEST_HEADER_COUNTS": ("status", "missing_counter_fields"),
    "GTM_WORKSPACE": ("configuration_fingerprint", "contents_status", "dependency_status"),
    "GTM_OPAQUE_CODE_MARKER": ("observed_count",),
    "GTM_INVENTORY_COVERAGE": (
        "inventory_coverage",
        "dependency_completeness",
        "observation_status",
        "staging_eligibility",
    ),
}

_PROVIDER_RESOURCE_TYPES: dict[str, frozenset[ComparisonResourceType]] = {
    "ga4_admin": frozenset({"GA4_KEY_EVENT", "GA4_EVENT_CREATE_RULE", "GA4_EVENT_EDIT_RULE"}),
    "openai_ads": frozenset({"OPENAI_ACCOUNT_SETTINGS", "OPENAI_CONVERSION_SETTING"}),
    "gtm": frozenset(
        {
            "GTM_ACCOUNT_FEATURES",
            "GTM_CONTAINER_FEATURES",
            "GTM_LIVE_VERSION",
            "GTM_LATEST_VERSION",
            "GTM_LATEST_HEADER_COUNTS",
            "GTM_WORKSPACE",
            "GTM_OPAQUE_CODE_MARKER",
            "GTM_INVENTORY_COVERAGE",
        }
    ),
}


def _exact_true(value: object) -> bool:
    if type(value) is not bool or value is not True:
        raise ValueError("Comparison completeness requires the JSON boolean true")
    return value


class MeasurementComparisonSource(StrictModel):
    """Exact retained evidence selected from one verified checkpoint."""

    record_digest: Digest
    snapshot_digest: Digest
    observation_digest: Digest
    provider: MeasurementComparisonProvider
    capability_version: MeasurementComparisonCapability
    account_ref: SafeRef
    profile_ref: SafeRef
    recorded_at: datetime
    read_started_at: datetime
    read_finished_at: datetime

    @field_validator("recorded_at", "read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Measurement comparison timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def interval_is_ordered(self) -> MeasurementComparisonSource:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("Measurement comparison source interval is reversed")
        return self


class Ga4MeasurementComparisonScope(StrictModel):
    provider: Literal["ga4_admin"] = "ga4_admin"
    capability_version: Literal["ga4-admin-fixed-inventory-1"]
    account_ref: SafeRef
    property_ref: SafeRef
    web_stream_ref: SafeRef


class OpenAIMeasurementComparisonScope(StrictModel):
    provider: Literal["openai_ads"] = "openai_ads"
    capability_version: Literal["openai-standard-conversion-inventory-1"]
    account_ref: SafeRef


class GtmMeasurementComparisonScope(StrictModel):
    provider: Literal["gtm"] = "gtm"
    capability_version: Literal["gtm-versions-workspace-metadata-1"]
    account_ref: SafeRef
    container_ref: SafeRef


MeasurementComparisonScope = Annotated[
    (
        Ga4MeasurementComparisonScope
        | OpenAIMeasurementComparisonScope
        | GtmMeasurementComparisonScope
    ),
    Field(discriminator="provider"),
]


class Ga4MeasurementComparisonCompleteness(StrictModel):
    provider: Literal["ga4_admin"] = "ga4_admin"
    source_record_digest: Digest
    pagination_verified: Literal[True]
    retained_list_coverage: Literal["FIXED_INVENTORY_PAGINATION_VERIFIED"] = (
        "FIXED_INVENTORY_PAGINATION_VERIFIED"
    )

    @field_validator("pagination_verified", mode="before")
    @classmethod
    def exact_pagination_verified(cls, value: object) -> bool:
        return _exact_true(value)


class OpenAIMeasurementComparisonCompleteness(StrictModel):
    provider: Literal["openai_ads"] = "openai_ads"
    source_record_digest: Digest
    pagination_verified: Literal[True]
    list_response_conformance: Literal["FIXTURE_CONTRACT_LIVE_UNVERIFIED"]
    retained_list_coverage: Literal["COMPLETE_WITHIN_RETAINED_PAGE_BOUND"] = (
        "COMPLETE_WITHIN_RETAINED_PAGE_BOUND"
    )

    @field_validator("pagination_verified", mode="before")
    @classmethod
    def exact_pagination_verified(cls, value: object) -> bool:
        return _exact_true(value)


class GtmMeasurementComparisonCompleteness(StrictModel):
    provider: Literal["gtm"] = "gtm"
    source_record_digest: Digest
    inventory_coverage: Literal["VERSION_COUNTS_AND_WORKSPACE_METADATA"]
    dependency_completeness: Literal["NOT_PROVEN"]
    latest_header_count_status: Literal["ALL_HEADER_COUNTS_MATCH", "HEADER_COUNTS_UNAVAILABLE"]
    observation_status: Literal["SEQUENTIALLY_REREAD_NONATOMIC"]
    staging_eligibility: Literal["NOT_EVALUATED"]
    retained_list_coverage: Literal["WORKSPACE_AND_OPAQUE_METADATA_WITHIN_FIXED_BOUNDS"] = (
        "WORKSPACE_AND_OPAQUE_METADATA_WITHIN_FIXED_BOUNDS"
    )


MeasurementComparisonCompleteness = Annotated[
    (
        Ga4MeasurementComparisonCompleteness
        | OpenAIMeasurementComparisonCompleteness
        | GtmMeasurementComparisonCompleteness
    ),
    Field(discriminator="provider"),
]


class MeasurementComparisonRequest(StrictModel):
    """Closed selector input for one comparison of two retained observations."""

    schema_version: Literal[1] = 1
    before_record_digest: Digest
    after_record_digest: Digest

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def distinct_selectors(self) -> MeasurementComparisonRequest:
        if self.before_record_digest == self.after_record_digest:
            raise ValueError("Comparison requires two distinct retained record digests")
        return self


class MeasurementComparisonFact(StrictModel):
    """One allowlisted normalized configuration fact, never a provider payload."""

    resource_type: ComparisonResourceType
    resource_ref: SafeRef
    status: ComparisonStatus
    before_fact_digest: Digest | None = None
    after_fact_digest: Digest | None = None
    changed_fields: tuple[ComparisonField, ...] = Field(
        default_factory=tuple, max_length=MAX_CHANGED_FIELDS
    )
    before_observed_count: int | None = Field(default=None, ge=1, le=4_000)
    after_observed_count: int | None = Field(default=None, ge=1, le=4_000)

    @model_validator(mode="after")
    def shape_matches_status_and_surface(self) -> MeasurementComparisonFact:
        allowed = _ALLOWED_FIELDS[self.resource_type]
        if len(self.changed_fields) != len(set(self.changed_fields)) or any(
            field not in allowed for field in self.changed_fields
        ):
            raise ValueError("Measurement comparison fact contains an invalid changed field")
        expected_counts = self.resource_type == "GTM_OPAQUE_CODE_MARKER"
        if expected_counts:
            if self.status == "CHANGED":
                counts_valid = (
                    self.before_observed_count is not None and self.after_observed_count is not None
                )
            elif self.status == "ADDED":
                counts_valid = (
                    self.before_observed_count is None and self.after_observed_count is not None
                )
            else:
                counts_valid = (
                    self.before_observed_count is not None and self.after_observed_count is None
                )
        else:
            counts_valid = self.before_observed_count is None and self.after_observed_count is None
        if not counts_valid:
            raise ValueError("Measurement comparison fact has invalid observed count evidence")
        if self.status == "CHANGED":
            valid = (
                self.before_fact_digest is not None
                and self.after_fact_digest is not None
                and bool(self.changed_fields)
            )
        elif self.status == "ADDED":
            valid = (
                self.before_fact_digest is None
                and self.after_fact_digest is not None
                and not self.changed_fields
            )
        else:
            valid = (
                self.before_fact_digest is not None
                and self.after_fact_digest is None
                and not self.changed_fields
            )
        if not valid:
            raise ValueError("Measurement comparison fact does not match its status")
        return self


class MeasurementComparison(StrictModel):
    """Bounded historical configuration comparison with no execution authority."""

    schema_version: Literal[1] = 1
    comparison_kind: Literal["RETAINED_MEASUREMENT_CONFIGURATION_COMPARISON"] = (
        "RETAINED_MEASUREMENT_CONFIGURATION_COMPARISON"
    )
    checkpoint_digest: Digest
    scope: MeasurementComparisonScope
    before: MeasurementComparisonSource
    after: MeasurementComparisonSource
    source_completeness: tuple[MeasurementComparisonCompleteness, ...] = Field(
        min_length=2, max_length=2
    )
    observation_interval_start: datetime
    observation_interval_end: datetime
    observed_actor: Literal["UNKNOWN"] = "UNKNOWN"
    exact_change_time: None = None
    absence_semantics: Literal["OBSERVED_ABSENT_FROM_LIST"] = "OBSERVED_ABSENT_FROM_LIST"
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    implementation_evidence: Literal["FIXTURE_VERIFIED_LIVE_UNVERIFIED"] = (
        "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    )
    fact_basis: Literal["NORMALIZED_OBSERVATION_DIFFERENCE"] = "NORMALIZED_OBSERVATION_DIFFERENCE"
    fingerprint_key_continuity: FingerprintKeyContinuity
    runtime_firing: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    processed_receipt: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    native_optimization_association: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    change_attribution: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    facts: tuple[MeasurementComparisonFact, ...] = Field(max_length=MAX_COMPARISON_FACTS)
    changed_count: int = Field(ge=0, le=MAX_COMPARISON_FACTS)
    added_count: int = Field(ge=0, le=MAX_COMPARISON_FACTS)
    observed_absent_count: int = Field(ge=0, le=MAX_COMPARISON_FACTS)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_boolean(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("Comparison control fields must be false JSON booleans")
        return value

    @field_validator("observation_interval_start", "observation_interval_end")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Measurement comparison interval must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def source_and_fact_invariants(self) -> MeasurementComparison:
        if self.before.record_digest == self.after.record_digest:
            raise ValueError("Measurement comparison requires two distinct retained records")
        if self.before.profile_ref != self.after.profile_ref:
            raise ValueError("Measurement comparison sources must use one profile")
        if (
            self.before.provider != self.scope.provider
            or self.after.provider != self.scope.provider
            or self.before.capability_version != self.scope.capability_version
            or self.after.capability_version != self.scope.capability_version
            or self.before.account_ref != self.scope.account_ref
            or self.after.account_ref != self.scope.account_ref
        ):
            raise ValueError("Measurement comparison source evidence conflicts with scope")
        if self.before.read_finished_at > self.after.read_started_at:
            raise ValueError(
                "Measurement comparison sources must be chronological and nonoverlapping"
            )
        if self.observation_interval_start != self.before.read_started_at or (
            self.observation_interval_end != self.after.read_finished_at
        ):
            raise ValueError(
                "Measurement comparison interval must bind exact source read boundaries"
            )
        if self.observation_interval_end < self.observation_interval_start:
            raise ValueError("Measurement comparison interval is reversed")
        if not fingerprint_continuity_matches_provider(
            self.fingerprint_key_continuity, self.scope.provider
        ):
            raise ValueError("Measurement comparison fingerprint continuity conflicts with scope")
        if (
            self.source_completeness[0].source_record_digest != self.before.record_digest
            or self.source_completeness[1].source_record_digest != self.after.record_digest
            or any(item.provider != self.scope.provider for item in self.source_completeness)
        ):
            raise ValueError("Measurement comparison completeness must bind both exact sources")
        expected = {
            "CHANGED": self.changed_count,
            "ADDED": self.added_count,
            "OBSERVED_ABSENT_FROM_LIST": self.observed_absent_count,
        }
        actual = {status: 0 for status in expected}
        keys: list[tuple[str, str]] = []
        for fact in self.facts:
            if fact.resource_type not in _PROVIDER_RESOURCE_TYPES[self.scope.provider]:
                raise ValueError("Measurement comparison fact conflicts with its provider scope")
            actual[fact.status] += 1
            keys.append((fact.resource_type, fact.resource_ref))
        if actual != expected or len(keys) != len(set(keys)) or keys != sorted(keys):
            raise ValueError("Measurement comparison facts are inconsistent or unordered")
        return self

    @property
    def comparison_digest(self) -> str:
        """Digest of the complete canonical comparison, excluding no hidden fields."""
        return digest(self.model_dump(mode="json"))


@dataclass(frozen=True)
class _Surface:
    resource_type: ComparisonResourceType
    resource_ref: str
    values: dict[ComparisonField, object]
    observed_count: int | None = None


def compare_measurements(
    ledger: Ledger,
    before_record_digest: str,
    after_record_digest: str,
) -> MeasurementComparison:
    """Compare two exact observations from one verified local checkpoint.

    Invalid selectors fail before the checkpoint is read.  The function never
    reads a provider, obtains a key, appends a record, or performs a mutation.
    """

    _validate_selection(before_record_digest, after_record_digest)
    if not isinstance(ledger, Ledger):
        raise AdhelmError(
            "INVALID_MEASUREMENT_COMPARISON_SELECTION",
            "Measurement comparison requires a local verified ledger.",
            exit_code=2,
        )
    checkpoint = _checkpoint(ledger)
    before = _source(checkpoint, before_record_digest)
    after = _source(checkpoint, after_record_digest)
    if before.observation.profile_ref != after.observation.profile_ref:
        raise _scope_error()
    if before.observation.read_finished_at > after.observation.read_started_at:
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_INTERVAL_INVALID",
            "Measurement observations must be chronological and nonoverlapping.",
            exit_code=2,
        )
    scope = _scope(before.observation.snapshot, after.observation.snapshot)
    continuity = _fingerprint_continuity(before.observation.snapshot, after.observation.snapshot)
    facts = _facts(before.observation.snapshot, after.observation.snapshot)
    if len(facts) > MAX_COMPARISON_FACTS:
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_TOO_LARGE",
            "Measurement comparison exceeds the fixed configuration-fact bound.",
            exit_code=2,
            next_action=(
                "Review each retained observation with measurement show; no complete "
                "comparison was produced within the fixed fact bound."
            ),
        )
    counts = {status: sum(fact.status == status for fact in facts) for status in _STATUSES}
    return MeasurementComparison(
        checkpoint_digest=cast(str, checkpoint["checkpoint_digest"]),
        scope=scope,
        before=before.evidence,
        after=after.evidence,
        source_completeness=(
            _completeness(before.observation, before.evidence.record_digest),
            _completeness(after.observation, after.evidence.record_digest),
        ),
        observation_interval_start=before.observation.read_started_at,
        observation_interval_end=after.observation.read_finished_at,
        fingerprint_key_continuity=continuity,
        facts=tuple(facts),
        changed_count=counts["CHANGED"],
        added_count=counts["ADDED"],
        observed_absent_count=counts["OBSERVED_ABSENT_FROM_LIST"],
    )


_STATUSES: tuple[ComparisonStatus, ...] = ("CHANGED", "ADDED", "OBSERVED_ABSENT_FROM_LIST")


def fingerprint_continuity_matches_provider(
    continuity: FingerprintKeyContinuity, provider: MeasurementComparisonProvider
) -> bool:
    """Keep comparison and artifact scope checks on the same closed vocabulary."""
    if provider == "openai_ads":
        return continuity == "NOT_APPLICABLE"
    if provider == "ga4_admin":
        return continuity in {"NOT_EVIDENCED", "MATCHING_RECORDED_KEY_MARKERS"}
    return continuity == "NOT_EVIDENCED"


def _fingerprint_continuity(
    before: MeasurementSnapshot, after: MeasurementSnapshot
) -> FingerprintKeyContinuity:
    if isinstance(before, OpenAIMeasurementSnapshot):
        return "NOT_APPLICABLE"
    if isinstance(before, Ga4MeasurementSnapshot) and isinstance(after, Ga4MeasurementSnapshot):
        left = before.inventory.fingerprint_key_marker
        right = after.inventory.fingerprint_key_marker
        if left is not None and right is not None:
            if left != right:
                raise AdhelmError(
                    "MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH",
                    "The retained GA4 observations record different fingerprint keys.",
                    exit_code=2,
                    next_action=(
                        "Review each observation separately or collect a new pair under one "
                        "configured key; key rotation must not be reported as configuration drift."
                    ),
                )
            return "MATCHING_RECORDED_KEY_MARKERS"
    # Legacy observations and GTM do not record continuity. Never derive it
    # from coincidentally matching resource values or the currently installed key.
    return "NOT_EVIDENCED"


@dataclass(frozen=True)
class _SelectedSource:
    observation: MeasurementObservation
    evidence: MeasurementComparisonSource


def _validate_selection(before_record_digest: str, after_record_digest: str) -> None:
    if (
        type(before_record_digest) is not str
        or type(after_record_digest) is not str
        or len(before_record_digest) != 64
        or len(after_record_digest) != 64
        or any(character not in "0123456789abcdef" for character in before_record_digest)
        or any(character not in "0123456789abcdef" for character in after_record_digest)
        or before_record_digest == after_record_digest
    ):
        raise AdhelmError(
            "INVALID_MEASUREMENT_COMPARISON_SELECTION",
            "Comparison requires two distinct retained record digests.",
            exit_code=2,
        )


def _checkpoint(ledger: Ledger) -> dict[str, object]:
    checkpoint = ledger.checkpoint()
    if not isinstance(checkpoint, dict) or checkpoint.get("status") != "VERIFIED_CHECKPOINT":
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_CHECKPOINT_INVALID",
            "Local history did not return a verified comparison checkpoint.",
        )
    checkpoint_digest = checkpoint.get("checkpoint_digest")
    streams = checkpoint.get("streams")
    records = checkpoint.get("records_by_stream")
    if (
        type(checkpoint_digest) is not str
        or len(checkpoint_digest) != 64
        or any(character not in "0123456789abcdef" for character in checkpoint_digest)
        or not isinstance(streams, Mapping)
        or not isinstance(records, Mapping)
    ):
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_CHECKPOINT_INVALID",
            "Local history did not return a verified comparison checkpoint.",
        )
    stream_names = ("changes", "creative", "performance")
    vector: dict[str, object] = {}
    for stream in stream_names:
        value = streams.get(stream)
        if not isinstance(value, Mapping) or stream not in records:
            raise AdhelmError(
                "MEASUREMENT_COMPARISON_CHECKPOINT_INVALID",
                "Local history did not return a verified comparison checkpoint.",
            )
        vector[stream] = dict(value)
    if digest(vector) != checkpoint_digest:
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_CHECKPOINT_INVALID",
            "Local history did not return a verified comparison checkpoint.",
        )
    return {
        "checkpoint_digest": checkpoint_digest,
        "changes": records["changes"],
    }


def _source(checkpoint: dict[str, object], record_digest: str) -> _SelectedSource:
    changes = checkpoint["changes"]
    if not isinstance(changes, list):
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_CHECKPOINT_INVALID",
            "Local history did not return comparison records.",
        )
    matches = [
        record
        for record in changes
        if isinstance(record, Mapping)
        and record.get("record_kind") in OBSERVATION_KINDS
        and record.get("record_digest") == record_digest
    ]
    if len(matches) != 1:
        raise AdhelmError(
            "MEASUREMENT_NOT_RETAINED",
            "Selected measurement is absent from verified history.",
        )
    record = matches[0]
    _refuse_uncomparable_source(record)
    try:
        raw_record = dict(record)
        if raw_record.get("record_digest") != digest(
            {name: value for name, value in raw_record.items() if name != "record_digest"}
        ):
            raise ValueError("record digest")
        payload = raw_record["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("payload")
        _validate_raw_measurement_payload(payload)
        observation = MeasurementObservation.model_validate_json(canonical_bytes(dict(payload)))
        if isinstance(observation.snapshot, GtmFullInventoryMeasurementSnapshot):
            # Already refused above; this re-proves the precondition for the
            # closed provider and capability literals of the source model.
            raise ValueError("uncomparable snapshot kind")
        recorded_at = _utc_time(raw_record["recorded_at"])
        observed_at = _utc_time(raw_record["observed_at"])
        if (
            raw_record.get("provider") != observation.snapshot.provider
            or raw_record.get("account_ref") != observation.snapshot.account_ref
            or raw_record.get("source") != "UNKNOWN"
            or raw_record.get("run_id") is not None
            or raw_record.get("occurred_at") is not None
            or observed_at != observation.read_finished_at
        ):
            raise ValueError("observation provenance")
        evidence = MeasurementComparisonSource(
            record_digest=record_digest,
            snapshot_digest=observation.snapshot_digest,
            observation_digest=digest(observation),
            provider=observation.snapshot.provider,
            capability_version=observation.snapshot.capability_version,
            account_ref=observation.snapshot.account_ref,
            profile_ref=observation.profile_ref,
            recorded_at=recorded_at,
            read_started_at=observation.read_started_at,
            read_finished_at=observation.read_finished_at,
        )
        return _SelectedSource(observation=observation, evidence=evidence)
    except (AdhelmError, KeyError, TypeError, ValueError, ValidationError):
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_SOURCE_INVALID",
            "Selected retained measurement has invalid comparison provenance.",
            exit_code=2,
        ) from None


def _validate_raw_measurement_payload(payload: Mapping[object, object]) -> None:
    """Reject Literal equality coercions before Pydantic can normalize them.

    A verified ledger can contain historical schema versions whose Pydantic
    models use ``Literal[1]``/``Literal[True]``.  Python equality treats
    ``True == 1`` and Pydantic can otherwise normalize a self-consistent,
    rehashed payload.  The comparison boundary accepts only the actual JSON
    scalar types for the closed schemas it projects.
    """

    _raw_exact_int(payload, "schema_version", 1)
    _raw_exact_bool(payload, "execution_authority", False)
    snapshot = _raw_mapping(payload, "snapshot")
    provider = snapshot.get("provider")
    if type(provider) is not str:
        raise ValueError("snapshot provider")
    if provider == "ga4_admin":
        _raw_ga4(snapshot)
    elif provider == "openai_ads":
        _raw_openai(snapshot)
    elif provider == "gtm":
        _raw_gtm(snapshot)
    else:
        raise ValueError("snapshot provider")


def _raw_ga4(snapshot: Mapping[object, object]) -> None:
    inventory = _raw_mapping(snapshot, "inventory")
    _raw_exact_int(inventory, "schema_version", 1)
    _raw_exact_bool(inventory, "pagination_verified", True)
    identity = _raw_mapping(inventory, "identity")
    _raw_exact_int(identity, "schema_version", 1)
    for name in ("key_events", "event_create_rules", "event_edit_rules"):
        for item in _raw_list(inventory, name):
            item_object = _raw_mapping_value(item)
            if name == "key_events":
                _raw_exact_bool(item_object, "custom")
                _raw_exact_bool(item_object, "deletable")
                default_value = item_object.get("default_value")
                if default_value is not None:
                    default_value_object = _raw_mapping_value(default_value)
                    if (
                        type(default_value_object.get("numeric_value")) is not str
                        or type(default_value_object.get("currency")) is not str
                    ):
                        raise ValueError("GA4 default value must preserve JSON string scalars")
            else:
                value = item_object.get("source_copy_parameters")
                if value is not None and type(value) is not bool:
                    raise ValueError("GA4 rule boolean")
                _raw_exact_int(item_object, "condition_count")
                _raw_exact_int(item_object, "parameter_mutation_count")


def _raw_openai(snapshot: Mapping[object, object]) -> None:
    _raw_exact_bool(snapshot, "pagination_verified", True)
    identity = _raw_mapping(snapshot, "identity")
    _raw_exact_int(identity, "schema_version", 1)
    for item in _raw_list(snapshot, "settings"):
        setting = _raw_mapping_value(item)
        _raw_exact_int(setting, "schema_version", 1)
        _raw_exact_int(setting, "attribution_window_days", 30)
        _raw_exact_int(setting, "source_count", 1)
        _raw_exact_int(setting, "campaign_count")
        _raw_exact_int(setting, "version")


def _raw_gtm(snapshot: Mapping[object, object]) -> None:
    discovery = _raw_mapping(snapshot, "discovery")
    inventory = _raw_mapping(snapshot, "inventory")
    _raw_exact_int(discovery, "schema_version", 1)
    _raw_exact_int(inventory, "schema_version", 1)
    _raw_exact_int(_raw_mapping(discovery, "identity"), "schema_version", 1)
    _raw_exact_int(_raw_mapping(inventory, "identity"), "schema_version", 1)
    for name in ("live", "latest"):
        counts = _raw_mapping(_raw_mapping(inventory, name), "resource_counts")
        for field in (
            "tags",
            "triggers",
            "variables",
            "folders",
            "built_in_variables",
            "zones",
            "custom_templates",
            "clients",
            "gtag_configs",
            "transformations",
        ):
            _raw_exact_int(counts, field)


def _raw_mapping(parent: Mapping[object, object], key: str) -> Mapping[object, object]:
    return _raw_mapping_value(parent.get(key))


def _raw_mapping_value(value: object) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ValueError("object")
    return value


def _raw_list(parent: Mapping[object, object], key: str) -> list[object]:
    value = parent.get(key)
    if type(value) is not list:
        raise ValueError("list")
    return value


def _raw_exact_int(parent: Mapping[object, object], key: str, expected: int | None = None) -> None:
    value = parent.get(key)
    if type(value) is not int or (expected is not None and value != expected):
        raise ValueError("integer")


def _raw_exact_bool(
    parent: Mapping[object, object], key: str, expected: bool | None = None
) -> None:
    value = parent.get(key)
    if type(value) is not bool or (expected is not None and value is not expected):
        raise ValueError("boolean")


def _utc_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp")
    return parsed.astimezone(UTC)


def _completeness(
    observation: MeasurementObservation,
    record_digest: str,
) -> MeasurementComparisonCompleteness:
    snapshot = observation.snapshot
    if isinstance(snapshot, Ga4MeasurementSnapshot):
        return Ga4MeasurementComparisonCompleteness(
            source_record_digest=record_digest,
            pagination_verified=snapshot.inventory.pagination_verified,
        )
    if isinstance(snapshot, OpenAIMeasurementSnapshot):
        return OpenAIMeasurementComparisonCompleteness(
            source_record_digest=record_digest,
            pagination_verified=snapshot.pagination_verified,
            list_response_conformance=snapshot.list_response_conformance,
        )
    if isinstance(snapshot, GtmMeasurementSnapshot):
        return GtmMeasurementComparisonCompleteness(
            source_record_digest=record_digest,
            inventory_coverage=snapshot.inventory_coverage,
            dependency_completeness=snapshot.dependency_completeness,
            latest_header_count_status=snapshot.inventory.latest_header_count_status.status,
            observation_status=snapshot.inventory.observation_status,
            staging_eligibility=snapshot.inventory.staging_eligibility,
        )
    raise AdhelmError(
        "MEASUREMENT_COMPARISON_SOURCE_INVALID",
        "Retained measurement has an unsupported comparison surface.",
        exit_code=2,
    )


def _scope(
    before: MeasurementSnapshot,
    after: MeasurementSnapshot,
) -> MeasurementComparisonScope:
    if type(before) is not type(after) or before.provider != after.provider:
        raise _scope_error()
    if isinstance(before, Ga4MeasurementSnapshot) and isinstance(after, Ga4MeasurementSnapshot):
        before_identity, after_identity = before.inventory.identity, after.inventory.identity
        if (
            before.capability_version != after.capability_version
            or before.account_ref != after.account_ref
            or before_identity.property_ref != after_identity.property_ref
            or before_identity.property_client_ref != after_identity.property_client_ref
            or before_identity.web_stream_ref != after_identity.web_stream_ref
            or before_identity.web_stream_client_ref != after_identity.web_stream_client_ref
        ):
            raise _scope_error()
        return Ga4MeasurementComparisonScope(
            capability_version=before.capability_version,
            account_ref=before.account_ref,
            property_ref=before_identity.property_ref,
            web_stream_ref=before_identity.web_stream_ref,
        )
    if isinstance(before, OpenAIMeasurementSnapshot) and isinstance(
        after, OpenAIMeasurementSnapshot
    ):
        if (
            before.capability_version != after.capability_version
            or before.account_ref != after.account_ref
            or before.identity.account_client_ref != after.identity.account_client_ref
        ):
            raise _scope_error()
        return OpenAIMeasurementComparisonScope(
            capability_version=before.capability_version,
            account_ref=before.account_ref,
        )
    if isinstance(before, GtmMeasurementSnapshot) and isinstance(after, GtmMeasurementSnapshot):
        gtm_before_identity, gtm_after_identity = before.identity, after.identity
        if (
            before.capability_version != after.capability_version
            or before.account_ref != after.account_ref
            or gtm_before_identity.account_client_ref != gtm_after_identity.account_client_ref
            or gtm_before_identity.container_ref != gtm_after_identity.container_ref
            or gtm_before_identity.container_client_ref != gtm_after_identity.container_client_ref
        ):
            raise _scope_error()
        return GtmMeasurementComparisonScope(
            capability_version=before.capability_version,
            account_ref=before.account_ref,
            container_ref=gtm_before_identity.container_ref,
        )
    raise _scope_error()


def _refuse_uncomparable_source(record: Mapping[str, Any]) -> None:
    """Refuse a retained snapshot kind that has no defined comparison surface.

    The complete GTM inventory is a configuration read, not a comparable
    surface set: its coverage statuses, raw reference edges and bounded
    resource records would need their own difference semantics before any
    change claim could be honest.  Refusing explicitly is required rather than
    producing an empty or partial comparison.
    """

    payload = record.get("payload")
    snapshot = payload.get("snapshot") if isinstance(payload, Mapping) else None
    if record.get("record_kind") == CHUNKED_KIND or (
        isinstance(snapshot, Mapping) and snapshot.get("provider") == "gtm_web_inventory"
    ):
        raise AdhelmError(
            "NOT_COMPARABLE_FULL_INVENTORY",
            "The complete GTM inventory observation has no defined comparison surface.",
            exit_code=2,
            next_action=(
                "Review each complete GTM inventory observation with measurement show; "
                "no configuration comparison is defined for this snapshot kind."
            ),
        )


def _scope_error() -> AdhelmError:
    return AdhelmError(
        "MEASUREMENT_COMPARISON_SCOPE_MISMATCH",
        "Retained measurements must use one matching provider, account, stable identity and "
        "capability.",
        exit_code=2,
    )


def _facts(
    before: MeasurementSnapshot,
    after: MeasurementSnapshot,
) -> list[MeasurementComparisonFact]:
    before_surfaces = _surface_map(_surfaces(before))
    after_surfaces = _surface_map(_surfaces(after))
    facts: list[MeasurementComparisonFact] = []
    for key in sorted(set(before_surfaces) | set(after_surfaces)):
        prior, current = before_surfaces.get(key), after_surfaces.get(key)
        if prior is None:
            if current is None:
                raise AdhelmError(
                    "MEASUREMENT_COMPARISON_SOURCE_INVALID",
                    "Retained measurement has incompatible typed comparison resources.",
                    exit_code=2,
                )
            facts.append(_added_fact(current))
        elif current is None:
            facts.append(_absent_fact(prior))
        elif _surface_digest(prior) != _surface_digest(current):
            facts.append(_changed_fact(prior, current))
    return facts


def _surface_map(surfaces: list[_Surface]) -> dict[tuple[str, str], _Surface]:
    result: dict[tuple[str, str], _Surface] = {}
    for surface in surfaces:
        key = (surface.resource_type, surface.resource_ref)
        if key in result:
            raise AdhelmError(
                "MEASUREMENT_COMPARISON_SOURCE_INVALID",
                "Retained measurement has duplicate typed comparison resources.",
                exit_code=2,
            )
        result[key] = surface
    return result


def _added_fact(surface: _Surface) -> MeasurementComparisonFact:
    return MeasurementComparisonFact(
        resource_type=surface.resource_type,
        resource_ref=surface.resource_ref,
        status="ADDED",
        after_fact_digest=_surface_digest(surface),
        after_observed_count=surface.observed_count,
    )


def _absent_fact(surface: _Surface) -> MeasurementComparisonFact:
    return MeasurementComparisonFact(
        resource_type=surface.resource_type,
        resource_ref=surface.resource_ref,
        status="OBSERVED_ABSENT_FROM_LIST",
        before_fact_digest=_surface_digest(surface),
        before_observed_count=surface.observed_count,
    )


def _changed_fact(before: _Surface, after: _Surface) -> MeasurementComparisonFact:
    if (
        before.resource_type != after.resource_type
        or before.resource_ref != after.resource_ref
        or (before.observed_count is None) != (after.observed_count is None)
    ):
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_SOURCE_INVALID",
            "Retained measurement has incompatible typed comparison resources.",
            exit_code=2,
        )
    fields = tuple(
        field
        for field in _ALLOWED_FIELDS[before.resource_type]
        if canonical_bytes(before.values[field]) != canonical_bytes(after.values[field])
    )
    if not fields:
        raise AdhelmError(
            "MEASUREMENT_COMPARISON_SOURCE_INVALID",
            "Retained measurement changed outside the closed comparison surface.",
            exit_code=2,
        )
    return MeasurementComparisonFact(
        resource_type=before.resource_type,
        resource_ref=before.resource_ref,
        status="CHANGED",
        before_fact_digest=_surface_digest(before),
        after_fact_digest=_surface_digest(after),
        changed_fields=fields,
        before_observed_count=before.observed_count,
        after_observed_count=after.observed_count,
    )


def _surface_digest(surface: _Surface) -> str:
    return digest(
        {
            "resource_type": surface.resource_type,
            "resource_ref": surface.resource_ref,
            "values": surface.values,
            "observed_count": surface.observed_count,
        }
    )


def _surfaces(snapshot: MeasurementSnapshot) -> list[_Surface]:
    if isinstance(snapshot, Ga4MeasurementSnapshot):
        return _ga4_surfaces(snapshot)
    if isinstance(snapshot, OpenAIMeasurementSnapshot):
        return _openai_surfaces(snapshot)
    if isinstance(snapshot, GtmMeasurementSnapshot):
        return _gtm_surfaces(snapshot)
    raise AdhelmError(
        "MEASUREMENT_COMPARISON_SOURCE_INVALID",
        "Retained measurement has an unsupported comparison surface.",
        exit_code=2,
    )


def _ga4_surfaces(snapshot: Ga4MeasurementSnapshot) -> list[_Surface]:
    output: list[_Surface] = []
    for item in snapshot.inventory.key_events:
        output.append(
            _Surface(
                "GA4_KEY_EVENT",
                item.resource_ref,
                {
                    "configuration_fingerprint": item.configuration_fingerprint,
                    "event_name_fingerprint": item.event_name_fingerprint,
                    "fingerprint_scheme": item.fingerprint_scheme,
                    "counting_method": item.counting_method,
                    "default_value": (
                        item.default_value.model_dump(mode="json")
                        if item.default_value is not None
                        else None
                    ),
                    "custom": item.custom,
                    "deletable": item.deletable,
                },
            )
        )
    for create_rule in snapshot.inventory.event_create_rules:
        output.append(
            _Surface(
                "GA4_EVENT_CREATE_RULE",
                create_rule.resource_ref,
                {
                    "configuration_fingerprint": create_rule.configuration_fingerprint,
                    "fingerprint_scheme": create_rule.fingerprint_scheme,
                    "condition_count": create_rule.condition_count,
                    "parameter_mutation_count": create_rule.parameter_mutation_count,
                    "source_copy_parameters": create_rule.source_copy_parameters,
                    "inspection_status": create_rule.inspection_status,
                },
            )
        )
    for edit_rule in snapshot.inventory.event_edit_rules:
        output.append(
            _Surface(
                "GA4_EVENT_EDIT_RULE",
                edit_rule.resource_ref,
                {
                    "configuration_fingerprint": edit_rule.configuration_fingerprint,
                    "fingerprint_scheme": edit_rule.fingerprint_scheme,
                    "condition_count": edit_rule.condition_count,
                    "parameter_mutation_count": edit_rule.parameter_mutation_count,
                    "processing_order": edit_rule.processing_order,
                    "inspection_status": edit_rule.inspection_status,
                },
            )
        )
    return output


def _openai_surfaces(snapshot: OpenAIMeasurementSnapshot) -> list[_Surface]:
    output = [
        _Surface(
            "OPENAI_ACCOUNT_SETTINGS",
            snapshot.identity.account_ref,
            {
                "timezone": snapshot.identity.timezone,
                "currency": snapshot.identity.currency,
                "status": snapshot.identity.status,
            },
        )
    ]
    for item in snapshot.settings:
        output.append(
            _Surface(
                "OPENAI_CONVERSION_SETTING",
                item.conversion_setting_ref,
                {
                    "event_type": item.event_type,
                    "attribution_window_days": item.attribution_window_days,
                    "status": item.status,
                    "source_count": item.source_count,
                    "campaign_count": item.campaign_count,
                    "version": item.version,
                    "provider_api_version": item.provider_api_version,
                },
            )
        )
    return output


def _gtm_surfaces(snapshot: GtmMeasurementSnapshot) -> list[_Surface]:
    identity, inventory = snapshot.identity, snapshot.inventory
    account_features = snapshot.discovery.account_features
    container_features = snapshot.discovery.container_features
    output = [
        _Surface(
            "GTM_ACCOUNT_FEATURES",
            identity.account_ref,
            {
                "support_user_permissions": account_features.support_user_permissions,
                "support_multiple_containers": account_features.support_multiple_containers,
            },
        ),
        _Surface(
            "GTM_CONTAINER_FEATURES",
            identity.container_ref,
            {
                "support_user_permissions": container_features.support_user_permissions,
                "support_environments": container_features.support_environments,
                "support_workspaces": container_features.support_workspaces,
                "support_gtag_configs": container_features.support_gtag_configs,
                "support_built_in_variables": container_features.support_built_in_variables,
                "support_clients": container_features.support_clients,
                "support_folders": container_features.support_folders,
                "support_tags": container_features.support_tags,
                "support_templates": container_features.support_templates,
                "support_triggers": container_features.support_triggers,
                "support_variables": container_features.support_variables,
                "support_versions": container_features.support_versions,
                "support_zones": container_features.support_zones,
                "support_transformations": container_features.support_transformations,
            },
        ),
        _gtm_version_surface(
            "GTM_LIVE_VERSION",
            f"{identity.container_ref}.live_version_slot",
            inventory.live,
        ),
        _gtm_version_surface(
            "GTM_LATEST_VERSION",
            f"{identity.container_ref}.latest_version_slot",
            inventory.latest,
        ),
        _Surface(
            "GTM_LATEST_HEADER_COUNTS",
            identity.container_ref,
            {
                "status": inventory.latest_header_count_status.status,
                "missing_counter_fields": list(
                    inventory.latest_header_count_status.missing_counter_fields
                ),
            },
        ),
        _Surface(
            "GTM_INVENTORY_COVERAGE",
            identity.container_ref,
            {
                "inventory_coverage": snapshot.inventory_coverage,
                "dependency_completeness": snapshot.dependency_completeness,
                "observation_status": inventory.observation_status,
                "staging_eligibility": inventory.staging_eligibility,
            },
        ),
    ]
    for workspace in inventory.workspaces:
        output.append(
            _Surface(
                "GTM_WORKSPACE",
                workspace.workspace_ref,
                {
                    "configuration_fingerprint": workspace.configuration_fingerprint,
                    "contents_status": workspace.contents_status,
                    "dependency_status": workspace.dependency_status,
                },
            )
        )
    for context, version in (("live", inventory.live), ("latest", inventory.latest)):
        counts: dict[tuple[str, str], int] = {}
        for marker in version.opaque_code:
            key = (marker.kind, marker.configuration_fingerprint)
            counts[key] = counts.get(key, 0) + 1
        for (kind, configuration_fingerprint), count in counts.items():
            output.append(
                _Surface(
                    "GTM_OPAQUE_CODE_MARKER",
                    f"gtm.opaque_marker.{context}.{kind}.{configuration_fingerprint}",
                    {"observed_count": count},
                    observed_count=count,
                )
            )
    return output


def _gtm_version_surface(
    resource_type: Literal["GTM_LIVE_VERSION", "GTM_LATEST_VERSION"],
    slot_ref: str,
    version: GtmVersionSnapshot,
) -> _Surface:
    # The call sites are the closed GtmReadInventory fields.  Keep every
    # projected attribute explicit so no future provider payload field becomes
    # comparable by accident.
    values: dict[ComparisonField, object] = {
        "version_ref": version.version_ref,
        "configuration_fingerprint": version.configuration_fingerprint,
        "resource_counts.tags": version.resource_counts.tags,
        "resource_counts.triggers": version.resource_counts.triggers,
        "resource_counts.variables": version.resource_counts.variables,
        "resource_counts.folders": version.resource_counts.folders,
        "resource_counts.built_in_variables": version.resource_counts.built_in_variables,
        "resource_counts.zones": version.resource_counts.zones,
        "resource_counts.custom_templates": version.resource_counts.custom_templates,
        "resource_counts.clients": version.resource_counts.clients,
        "resource_counts.gtag_configs": version.resource_counts.gtag_configs,
        "resource_counts.transformations": version.resource_counts.transformations,
        "compiler_status": version.compiler_status,
        "browser_status": version.browser_status,
    }
    return _Surface(resource_type, slot_ref, values)


# ---------------------------------------------------------------------------
# Additive: provider and GA4 measurements presented side by side (ticket M00.2)
#
# `docs/definition-of-done.md` section 6 line 249 requires provider, GA4, and
# future CRM results to stay side by side, with no output presenting
# correlation or attributed credit as causal incrementality.  These rows
# therefore carry each source's own measurements in its own fields, with its
# own window, timezone, and attribution label.  No field on this view is
# derived from two sources, and `refuse_cross_source_derivation` refuses any
# such field before the view is emitted.
#
# Nothing here compares two configuration snapshots; the existing
# `compare_measurements` contract above is unchanged.
# ---------------------------------------------------------------------------


class SideBySideSourceRow(StrictModel):
    """One declared provider campaign beside its declared GA4 traffic.

    A ``null`` measurement stays ``null``.  It is never a zero, and it is never
    replaced by a value derived from the other source.
    """

    schema_version: Literal[1] = 1
    provider: Provider
    provider_campaign_ref: SafeRef
    join_status: JoinStatus
    join_source: JoinSource
    join_map_digest: Digest
    provider_report_id: SafeRef
    provider_window_start: date
    provider_window_end: date
    provider_timezone: str = Field(min_length=1, max_length=80)
    provider_attribution_model: str = Field(min_length=1, max_length=120)
    provider_impressions: int | None = Field(default=None, ge=0)
    provider_clicks: int | None = Field(default=None, ge=0)
    provider_spend: Money | None = None
    ga4_report_id: SafeRef
    ga4_property_ref: SafeRef
    ga4_window_start: date
    ga4_window_end: date
    ga4_timezone: str = Field(min_length=1, max_length=80)
    ga4_attribution_model: str = Field(min_length=1, max_length=120)
    ga4_sessions: int | None = Field(default=None, ge=0)
    ga4_key_events: int | None = Field(default=None, ge=0)
    ga4_dimension_row_count: int = Field(ge=1)
    boundary_status: JoinBoundaryStatus
    data_quality_statuses: tuple[QualityStatus, ...] = Field(default_factory=tuple, max_length=16)

    @model_validator(mode="after")
    def sources_stay_separate(self) -> SideBySideSourceRow:
        if self.join_status not in {"EXPLICIT_MAPPING", "EXACT_CONTROLLED_KEY"}:
            raise ValueError("Only a declared join produces a side-by-side row")
        if ("TIMEZONE_MISMATCH" in self.data_quality_statuses) != (
            self.boundary_status == "TIMEZONE_MISMATCH"
        ):
            raise ValueError("A timezone-mismatched row carries the TIMEZONE_MISMATCH status")
        if len(self.data_quality_statuses) != len(set(self.data_quality_statuses)):
            raise ValueError("A side-by-side row cannot repeat one quality status")
        return self


class SideBySideComparison(StrictModel):
    """Provider and GA4 measurements beside each other, with no derived measure."""

    schema_version: Literal[1] = 1
    join_map_digest: Digest
    rows: tuple[SideBySideSourceRow, ...] = Field(default_factory=tuple, max_length=1_000)
    # Every GA4 dimension tuple that stayed visible but out of the joined rows.
    excluded_join_status_counts: dict[str, int] = Field(default_factory=dict, max_length=8)
    boundary_status: JoinBoundaryStatus
    causal_claim: Literal[False] = False
    derived_cross_source_metrics: Literal[False] = False
    disclosure: Literal["SOURCES_SIDE_BY_SIDE_NO_CAUSAL_OR_INCREMENTALITY_CLAIM"] = (
        SIDE_BY_SIDE_DISCLOSURE
    )

    @model_validator(mode="after")
    def rows_are_ordered_and_bound(self) -> SideBySideComparison:
        keys = [(row.provider, row.provider_campaign_ref) for row in self.rows]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("Side-by-side rows must be canonically ordered and unique")
        if any(row.join_map_digest != self.join_map_digest for row in self.rows):
            raise ValueError("Every side-by-side row belongs to the declared join map")
        if any(row.boundary_status != self.boundary_status for row in self.rows):
            raise ValueError("Every side-by-side row carries the map's boundary status")
        return self


def _summed(values: list[int | None]) -> int | None:
    """Sum within one source only, and only when no contributing cell is null."""
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _summed_money(values: list[Money | None]) -> Money | None:
    if not values or any(value is None for value in values):
        return None
    currencies = {value.currency for value in values if value is not None}
    if len(currencies) != 1:
        return None
    return Money(
        currency=currencies.pop(),
        minor_units=sum(value.minor_units for value in values if value is not None),
    )


def build_side_by_side_comparison(
    *,
    join_map: CampaignJoinMap,
    provider_report: NormalizedReport,
    ga4_report: NormalizedReport,
) -> SideBySideComparison:
    """Place one provider report beside one GA4 report through a declared join.

    Each source keeps its own window, timezone, attribution label, and metric
    fields.  No ratio, cost-per-outcome, share, or causal label is produced;
    `refuse_cross_source_derivation` refuses one before the view is returned.
    """
    if ga4_report.envelope.provider != "ga4":
        raise AdhelmError(
            "JOIN_SOURCE_INVALID", "The GA4 side must be a retained GA4 report.", exit_code=2
        )
    if provider_report.envelope.provider not in AD_PROVIDERS:
        raise AdhelmError(
            "JOIN_SOURCE_INVALID",
            "The provider side must be a retained advertising report.",
            exit_code=2,
        )
    if ga4_report.envelope.source_account_ref != join_map.contract.ga4_property.property_ref:
        raise AdhelmError(
            "JOIN_MAP_PROPERTY_MISMATCH",
            "The join map is bound to a different GA4 property.",
            exit_code=2,
        )
    accounts = {
        item.account_ref
        for item in join_map.contract.provider_accounts
        if item.provider == provider_report.envelope.provider
    }
    if provider_report.envelope.source_account_ref not in accounts:
        raise AdhelmError(
            "JOIN_MAP_ACCOUNT_MISMATCH",
            "The join map does not bind this advertising account.",
            exit_code=2,
        )

    ga4_by_campaign: dict[tuple[str, str], list[PerformanceRow]] = {}
    for row in ga4_report.performance_rows:
        digest_value = row.provider_extensions.get("ga4_join_map_digest")
        if row.campaign_ref is None or digest_value != join_map.join_map_digest:
            continue
        source = row.provider_extensions.get("ga4_join_source")
        if not isinstance(source, str):
            continue
        ga4_by_campaign.setdefault((source, row.campaign_ref), []).append(row)

    provider_by_campaign: dict[str, list[PerformanceRow]] = {}
    for row in provider_report.performance_rows:
        if row.resource_level != "CAMPAIGN":
            continue
        provider_by_campaign.setdefault(row.resource_ref, []).append(row)

    resolutions: dict[str, CampaignJoinResolution] = {}
    for resolution in join_map.joined_resolutions():
        # `joined_resolutions()` returns only declared joins, so both the
        # provider and its campaign reference are present by construction.
        if resolution.provider == provider_report.envelope.provider:
            assert resolution.provider_campaign_ref is not None
            resolutions[resolution.provider_campaign_ref] = resolution
    # The boundary status must describe the two reports actually being shown,
    # not only the timezones the contract declared.  A retained provider report
    # read in another timezone than the retained GA4 report has different
    # calendar-day boundaries, and the row prints both timezone fields.
    boundary: JoinBoundaryStatus = (
        "TIMEZONE_MISMATCH"
        if (
            join_map.boundary_status == "TIMEZONE_MISMATCH"
            or provider_report.envelope.report_timezone != ga4_report.envelope.report_timezone
            or ga4_report.envelope.report_timezone
            != join_map.contract.ga4_property.property_timezone
        )
        else "ALIGNED"
    )
    rows: list[SideBySideSourceRow] = []
    for campaign_ref, resolution in sorted(resolutions.items()):
        join_source = resolution.join_source
        assert join_source is not None
        ga4_rows = ga4_by_campaign.get((join_source, campaign_ref), [])
        provider_rows = provider_by_campaign.get(campaign_ref, [])
        if not ga4_rows or not provider_rows:
            # A declared campaign missing from either retained report is not a
            # side-by-side row.  It stays visible in the join map instead.
            continue
        statuses: set[QualityStatus] = {
            status
            for row in (*ga4_rows, *provider_rows)
            for status in row.data_quality_statuses
            if status != "COMPLETE"
        }
        statuses |= set(resolution.data_quality_statuses)
        if boundary == "TIMEZONE_MISMATCH":
            statuses.add("TIMEZONE_MISMATCH")
        rows.append(
            SideBySideSourceRow(
                provider=provider_report.envelope.provider,
                provider_campaign_ref=campaign_ref,
                join_status=resolution.join_status,
                join_source=join_source,
                join_map_digest=join_map.join_map_digest,
                provider_report_id=provider_report.envelope.report_id,
                provider_window_start=provider_report.envelope.window_start,
                provider_window_end=provider_report.envelope.window_end,
                provider_timezone=provider_report.envelope.report_timezone,
                provider_attribution_model=provider_report.envelope.attribution_model,
                provider_impressions=_summed([row.impressions for row in provider_rows]),
                provider_clicks=_summed([row.clicks for row in provider_rows]),
                provider_spend=_summed_money([row.spend for row in provider_rows]),
                ga4_report_id=ga4_report.envelope.report_id,
                ga4_property_ref=ga4_report.envelope.source_account_ref,
                ga4_window_start=ga4_report.envelope.window_start,
                ga4_window_end=ga4_report.envelope.window_end,
                ga4_timezone=ga4_report.envelope.report_timezone,
                ga4_attribution_model=ga4_report.envelope.attribution_model,
                ga4_sessions=_summed([row.sessions for row in ga4_rows]),
                ga4_key_events=_summed([row.key_events for row in ga4_rows]),
                ga4_dimension_row_count=len(ga4_rows),
                boundary_status=boundary,
                data_quality_statuses=tuple(sorted(statuses)),
            )
        )
    excluded: dict[str, int] = {}
    for resolution in join_map.resolutions:
        if resolution.join_status in {"EXPLICIT_MAPPING", "EXACT_CONTROLLED_KEY"}:
            continue
        excluded[resolution.join_status] = excluded.get(resolution.join_status, 0) + 1
    comparison = SideBySideComparison(
        join_map_digest=join_map.join_map_digest,
        rows=tuple(rows),
        excluded_join_status_counts=dict(sorted(excluded.items())),
        boundary_status=boundary,
    )
    refuse_cross_source_derivation(comparison.model_dump(mode="json"))
    return comparison

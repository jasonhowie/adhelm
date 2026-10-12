"""Typed static artifacts for verified retained measurement comparisons.

This module has no provider, credential-store, authorization, or execution
path.  It accepts only a closed ``MeasurementComparison`` and rebuilds it from
one verified local ledger checkpoint before projecting an explicitly redacted
historical review through the shared safe artifact writer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from adhelm.artifact_detail import present_document
from adhelm.artifacts import (
    ArtifactContext,
    ArtifactDecision,
    ArtifactFormat,
    ArtifactGenerationError,
    ArtifactMetadata,
    ArtifactTaskOverride,
    _generate_typed_artifacts,
    _render,
    _validated_artifact_decision,
    resolve_artifact_policy,
)
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import (
    ComparisonField,
    ComparisonResourceType,
    ComparisonStatus,
    FingerprintKeyContinuity,
    Ga4MeasurementComparisonScope,
    GtmMeasurementComparisonScope,
    MeasurementComparison,
    MeasurementComparisonCompleteness,
    MeasurementComparisonFact,
    MeasurementComparisonProvider,
    MeasurementComparisonScope,
    MeasurementComparisonSource,
    OpenAIMeasurementComparisonScope,
    compare_measurements,
    fingerprint_continuity_matches_provider,
)
from adhelm.models import OutputPreferences, StrictModel
from adhelm.privacy import opaque_ref

_DIGEST = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_OPAQUE_REF = Annotated[str, Field(pattern=r"^opaque:[0-9a-f]{64}$")]
_RENDERER_IDENTITY = "measurement-comparison-artifact-v1"
_PREFLIGHT_KEY = b"\0" * 32
_PUBLIC_FAILURE_CODES = frozenset(
    {
        "ARTIFACT_POLICY_INVALID",
        "ARTIFACT_PUBLICATION_RACE",
        "ARTIFACT_ROW_LIMIT_EXCEEDED",
        "ARTIFACT_SIZE_LIMIT_EXCEEDED",
        "ARTIFACT_VERSION_EXHAUSTED",
        "DIRECTORY_CLOSE_FAILED",
        "DIRECTORY_FSYNC_FAILED",
        "LOCAL_IO_FAILED",
        "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID",
        "TEMPORARY_CLEANUP_FAILED",
        "UNSAFE_ARTIFACT_DESTINATION",
        "UNSUPPORTED_ARTIFACT_FORMAT",
    }
)
_PROVIDER_FACT_TYPES: dict[str, frozenset[ComparisonResourceType]] = {
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


def _is_artifact_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_safe_ref(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,160}", value) is not None


class MeasurementComparisonArtifactRequest(StrictModel):
    """One explicit request for a static retained-comparison artifact."""

    schema_version: Literal[1] = 1
    formats: tuple[ArtifactFormat, ...] = Field(default_factory=tuple, max_length=3)
    output_directory: str | None = Field(default=None, max_length=4096)
    replacement_authorized: bool | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class MeasurementComparisonArtifactProvenance(StrictModel):
    """Digest-only pair provenance for one static historical comparison."""

    before_record_digest: _DIGEST
    after_record_digest: _DIGEST
    checkpoint_digest: _DIGEST
    comparison_digest: _DIGEST

    @model_validator(mode="after")
    def distinct_records(self) -> MeasurementComparisonArtifactProvenance:
        if self.before_record_digest == self.after_record_digest:
            raise ValueError("Artifact provenance requires two distinct source records")
        return self


class MeasurementComparisonArtifactSource(StrictModel):
    """One retained source's digest and observation interval, without payload data."""

    position: Literal["before", "after"]
    record_digest: _DIGEST
    snapshot_digest: _DIGEST
    observation_digest: _DIGEST
    recorded_at: datetime
    read_started_at: datetime
    read_finished_at: datetime

    @field_validator("recorded_at", "read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Measurement artifact timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_interval(self) -> MeasurementComparisonArtifactSource:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("Measurement artifact source interval is reversed")
        return self


class Ga4MeasurementComparisonArtifactScope(StrictModel):
    provider: Literal["ga4_admin"] = "ga4_admin"
    capability_version: Literal["ga4-admin-fixed-inventory-1"]
    profile_ref: _OPAQUE_REF
    account_ref: _OPAQUE_REF
    property_ref: _OPAQUE_REF
    web_stream_ref: _OPAQUE_REF


class OpenAIMeasurementComparisonArtifactScope(StrictModel):
    provider: Literal["openai_ads"] = "openai_ads"
    capability_version: Literal["openai-standard-conversion-inventory-1"]
    profile_ref: _OPAQUE_REF
    account_ref: _OPAQUE_REF


class GtmMeasurementComparisonArtifactScope(StrictModel):
    provider: Literal["gtm"] = "gtm"
    capability_version: Literal["gtm-versions-workspace-metadata-1"]
    profile_ref: _OPAQUE_REF
    account_ref: _OPAQUE_REF
    container_ref: _OPAQUE_REF


MeasurementComparisonArtifactScope = Annotated[
    Ga4MeasurementComparisonArtifactScope
    | OpenAIMeasurementComparisonArtifactScope
    | GtmMeasurementComparisonArtifactScope,
    Field(discriminator="provider"),
]


class MeasurementComparisonArtifactFact(StrictModel):
    """One pseudonymized, allowlisted normalized-difference fact."""

    resource_type: ComparisonResourceType
    resource_ref: _OPAQUE_REF
    status: ComparisonStatus
    before_fact_digest: _DIGEST | None = None
    after_fact_digest: _DIGEST | None = None
    changed_fields: tuple[ComparisonField, ...] = Field(default_factory=tuple, max_length=32)
    before_observed_count: int | None = Field(default=None, ge=1, le=4_000)
    after_observed_count: int | None = Field(default=None, ge=1, le=4_000)

    @model_validator(mode="after")
    def matches_closed_fact_shape(self) -> MeasurementComparisonArtifactFact:
        try:
            MeasurementComparisonFact.model_validate(self.model_dump(mode="python"))
        except (TypeError, ValueError, ValidationError):
            raise ValueError("Measurement artifact fact violates the comparison surface") from None
        return self


class MeasurementComparisonArtifactCounts(StrictModel):
    changed_count: int = Field(ge=0, le=1_000)
    added_count: int = Field(ge=0, le=1_000)
    observed_absent_count: int = Field(ge=0, le=1_000)


class MeasurementComparisonArtifactHistoricalReview(StrictModel):
    """Fixed historical timing and non-authority semantics for a static file."""

    as_of: datetime
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
    current_remote_state: Literal["NOT_EVALUATED_HISTORICAL_ARTIFACT"] = (
        "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    )
    static_artifact: Literal[True] = True
    opening_or_editing_authorizes_execution: Literal[False] = False

    @field_validator("as_of", "observation_interval_start", "observation_interval_end")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Measurement artifact review timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("static_artifact", mode="before")
    @classmethod
    def exact_true_static_artifact(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("static_artifact must be true")
        return value

    @field_validator("opening_or_editing_authorizes_execution", mode="before")
    @classmethod
    def exact_false_opening_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("opening_or_editing_authorizes_execution must be false")
        return value

    @model_validator(mode="after")
    def ordered_interval(self) -> MeasurementComparisonArtifactHistoricalReview:
        if self.observation_interval_end < self.observation_interval_start:
            raise ValueError("Measurement artifact comparison interval is reversed")
        return self


class MeasurementComparisonArtifactAuthority(StrictModel):
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_command: Literal["NOT_INSTALLED"] = "NOT_INSTALLED"

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("Measurement artifact authority controls must be false")
        return value


class MeasurementComparisonArtifactContent(StrictModel):
    scope: MeasurementComparisonArtifactScope
    sources: tuple[MeasurementComparisonArtifactSource, ...] = Field(min_length=2, max_length=2)
    historical_review: MeasurementComparisonArtifactHistoricalReview
    source_completeness: tuple[MeasurementComparisonCompleteness, ...] = Field(
        min_length=2, max_length=2
    )
    facts: tuple[MeasurementComparisonArtifactFact, ...] = Field(max_length=1_000)
    counts: MeasurementComparisonArtifactCounts
    authority: MeasurementComparisonArtifactAuthority

    @model_validator(mode="after")
    def closed_content_invariants(self) -> MeasurementComparisonArtifactContent:
        before, after = self.sources
        if before.position != "before" or after.position != "after":
            raise ValueError("Measurement artifact source positions must be ordered")
        if before.read_finished_at > after.read_started_at:
            raise ValueError(
                "Measurement artifact sources must be chronological and nonoverlapping"
            )
        review = self.historical_review
        if (
            review.as_of != after.recorded_at
            or review.observation_interval_start != before.read_started_at
            or review.observation_interval_end != after.read_finished_at
        ):
            raise ValueError("Measurement artifact historical review does not bind source timing")
        if (
            self.source_completeness[0].source_record_digest != before.record_digest
            or self.source_completeness[1].source_record_digest != after.record_digest
            or any(item.provider != self.scope.provider for item in self.source_completeness)
        ):
            raise ValueError("Measurement artifact completeness does not bind both source records")
        allowed = _PROVIDER_FACT_TYPES[self.scope.provider]
        actual = {"CHANGED": 0, "ADDED": 0, "OBSERVED_ABSENT_FROM_LIST": 0}
        keys: list[tuple[str, str]] = []
        for fact in self.facts:
            if fact.resource_type not in allowed:
                raise ValueError("Measurement artifact fact conflicts with provider scope")
            actual[fact.status] += 1
            keys.append((fact.resource_type, fact.resource_ref))
        if (
            actual["CHANGED"] != self.counts.changed_count
            or actual["ADDED"] != self.counts.added_count
            or actual["OBSERVED_ABSENT_FROM_LIST"] != self.counts.observed_absent_count
            or len(keys) != len(set(keys))
        ):
            raise ValueError("Measurement artifact facts are inconsistent")
        if not fingerprint_continuity_matches_provider(
            review.fingerprint_key_continuity, self.scope.provider
        ):
            raise ValueError("Measurement artifact fingerprint continuity conflicts with scope")
        return self


class MeasurementComparisonArtifactDocument(StrictModel):
    """One schema-versioned redacted static comparison review."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["measurement_comparison_review"] = "measurement_comparison_review"
    source_evidence: MeasurementComparisonArtifactProvenance
    content: MeasurementComparisonArtifactContent

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def binds_full_source_pair(self) -> MeasurementComparisonArtifactDocument:
        before, after = self.content.sources
        evidence = self.source_evidence
        if (
            evidence.before_record_digest != before.record_digest
            or evidence.after_record_digest != after.record_digest
        ):
            raise ValueError("Measurement artifact evidence does not bind its source pair")
        return self


@dataclass(frozen=True)
class MeasurementComparisonArtifactResult:
    """Bounded local artifact outcome that does not alter retained history."""

    effective_decision: ArtifactDecision
    before_record_digest: str
    after_record_digest: str
    checkpoint_digest: str
    comparison_digest: str
    artifacts: tuple[ArtifactMetadata, ...]
    generation_failure_code: str | None = None

    def to_data(self) -> dict[str, object]:
        decision, metadata = _validated_measurement_comparison_artifact_result(self)
        return {
            "effective_policy": {
                "outcome": decision.outcome,
                "reason": decision.reason,
                "formats": list(decision.formats),
                "preferences_digest": decision.preferences_digest,
                "reuse_allowed": decision.reuse_allowed,
                "replacement_authorized": decision.replacement_authorized,
                "detail": decision.detail,
            },
            "before_record_digest": self.before_record_digest,
            "after_record_digest": self.after_record_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "comparison_digest": self.comparison_digest,
            "historical_static_artifact": True,
            "execution_authority": False,
            "remote_mutations": False,
            "opening_or_editing_authorizes_execution": False,
            "generation_failure_code": _public_failure_code(self.generation_failure_code),
            "artifacts": [
                {
                    "path": str(item.path),
                    "format": item.format,
                    "reused": item.reused,
                    "artifact_digest": item.artifact_digest,
                    "input_digest": item.input_digest,
                    "settings_digest": item.settings_digest,
                }
                for item in metadata
            ],
        }


def preflight_measurement_comparison_artifacts(
    preferences: OutputPreferences,
    *,
    provider: MeasurementComparisonProvider,
    profile_id: str,
) -> ArtifactDecision:
    """Resolve a saved diagnostics/comparison rule without source or key access."""
    _validate_context(provider, profile_id)
    decision = resolve_artifact_policy(
        ArtifactContext(
            workflow="diagnostics",
            stage="completion",
            purpose="comparison",
            provider=provider,
            profile_id=profile_id,
        ),
        preferences,
    )
    if decision.outcome == "NEEDS_CONFIGURATION":
        raise AdhelmError(
            "ARTIFACT_CONFIGURATION_REQUIRED",
            "A matching measurement-comparison artifact rule needs local configuration.",
            next_action="Configure supported formats and an existing local output directory.",
        )
    return decision


def explicit_measurement_comparison_artifact_decision(
    preferences: OutputPreferences,
    *,
    provider: MeasurementComparisonProvider,
    profile_id: str,
    formats: tuple[ArtifactFormat, ...],
    output_directory: str | None,
    replacement_authorized: bool | None,
) -> ArtifactDecision:
    """Resolve one explicit comparison-artifact request without saving a rule."""
    _validate_context(provider, profile_id)
    return resolve_artifact_policy(
        ArtifactContext(
            workflow="diagnostics",
            stage="completion",
            purpose="comparison",
            provider=provider,
            profile_id=profile_id,
        ),
        preferences,
        task_override=ArtifactTaskOverride(
            requested=True,
            formats=formats,
            output_directory=output_directory,
            purpose="comparison",
            replacement_authorized=replacement_authorized,
        ),
    )


def measurement_comparison_artifact_failure_preview(
    decision: ArtifactDecision,
    *,
    before_record_digest: str,
    after_record_digest: str,
    checkpoint_digest: str,
    comparison_digest: str,
) -> tuple[dict[str, object], list[str]]:
    """Reserve all post-publication result fields before key access or writing."""
    decision = _validated_artifact_decision(decision)
    _validated_references(
        before_record_digest,
        after_record_digest,
        checkpoint_digest,
        comparison_digest,
    )
    if decision.output_directory is None:
        raise _artifact_policy_error()
    artifacts = tuple(
        ArtifactMetadata(
            path=decision.output_directory
            / ("artifact-" + "f" * 16 + "-" + "f" * 16 + ".v9999." + format_name),
            format=format_name,
            reused=False,
            artifact_digest="f" * 64,
            input_digest="f" * 64,
            settings_digest="f" * 64,
            source_record_digest=before_record_digest,
            checkpoint_digest=checkpoint_digest,
        )
        for format_name in decision.formats
    )
    result = MeasurementComparisonArtifactResult(
        decision,
        before_record_digest,
        after_record_digest,
        checkpoint_digest,
        comparison_digest,
        artifacts,
        generation_failure_code="X" * 80,
    )
    return result.to_data(), ["artifact:sha256:" + item.artifact_digest for item in artifacts]


def revalidate_measurement_comparison_artifact_source(
    ledger: Ledger, comparison: MeasurementComparison
) -> MeasurementComparison:
    """Rebuild a supplied typed comparison and require exact canonical equality.

    The supplied object is never used as artifact input.  It is round-tripped
    through the closed comparison schema, then compared byte-for-byte with a
    fresh reconstruction from the selected pair in one verified checkpoint.
    """
    if type(comparison) is not MeasurementComparison:
        raise _source_error()
    try:
        # Validate the direct model serializer's original JSON before any
        # canonicalization.  Canonical JSON represents ``1.0`` as ``1``;
        # validating canonical bytes first would let a model_copy-injected
        # float bypass MeasurementComparison's exact Literal[1] guard.
        supplied = MeasurementComparison.model_validate_json(
            comparison.model_dump_json(warnings="error")
        )
        rebuilt = compare_measurements(
            ledger, supplied.before.record_digest, supplied.after.record_digest
        )
        supplied_bytes = canonical_bytes(supplied.model_dump(mode="json", warnings="error"))
        rebuilt_bytes = canonical_bytes(rebuilt.model_dump(mode="json", warnings="error"))
    except Exception:
        raise _source_error() from None
    if supplied_bytes != rebuilt_bytes or supplied.comparison_digest != rebuilt.comparison_digest:
        raise _source_error()
    return rebuilt


def preflight_measurement_comparison_artifact_output(
    ledger: Ledger,
    comparison: MeasurementComparison,
    decision: ArtifactDecision,
) -> MeasurementComparison:
    """Check source, rows, and all rendered sizes with a fixed synthetic HMAC key.

    This helper deliberately opens no output directory and accesses no real
    export key.  HMAC outputs have a fixed hexadecimal width, so the synthetic
    key reserves the same JSON/CSV/HTML byte budget as a real local key.
    """
    checked = revalidate_measurement_comparison_artifact_source(ledger, comparison)
    decision = _validated_artifact_decision(decision)
    if decision.outcome != "GENERATE":
        return checked
    _preflight_checked_document(checked, decision)
    return checked


def render_measurement_comparison_artifacts(
    ledger: Ledger,
    comparison: MeasurementComparison,
    decision: ArtifactDecision,
    *,
    key: bytes,
) -> MeasurementComparisonArtifactResult:
    """Render a redacted historical comparison after fresh source validation.

    This function repeats source reconstruction at the shared writer's
    document-building handoff, after preflight and directory setup. This is a
    historical checkpoint check, not an atomic transaction with ledger appends
    or a claim about current remote state.
    """
    checked = revalidate_measurement_comparison_artifact_source(ledger, comparison)
    decision = _validated_artifact_decision(decision)
    provenance = _provenance(checked)
    if decision.outcome != "GENERATE":
        return MeasurementComparisonArtifactResult(
            decision,
            provenance.before_record_digest,
            provenance.after_record_digest,
            provenance.checkpoint_digest,
            provenance.comparison_digest,
            (),
        )
    # Ensure direct library callers get the same no-key row/byte failure
    # boundary as the CLI before the descriptor-relative writer opens a path.
    _preflight_checked_document(checked, decision)
    try:
        artifacts = _generate_typed_artifacts(
            decision,
            source_record_digest=provenance.before_record_digest,
            checkpoint_digest=provenance.checkpoint_digest,
            document_builder=lambda max_rows: _comparison_artifact_document(
                revalidate_measurement_comparison_artifact_source(ledger, comparison),
                key=key,
                max_rows=max_rows,
            ),
            source_schema=MeasurementComparisonArtifactDocument.model_json_schema(),
            key=key,
            renderer_identity=_RENDERER_IDENTITY,
        )
    except ArtifactGenerationError as exc:
        return MeasurementComparisonArtifactResult(
            decision,
            provenance.before_record_digest,
            provenance.after_record_digest,
            provenance.checkpoint_digest,
            provenance.comparison_digest,
            exc.completed,
            generation_failure_code=_public_failure_code(exc.failure_code),
        )
    except AdhelmError as exc:
        return MeasurementComparisonArtifactResult(
            decision,
            provenance.before_record_digest,
            provenance.after_record_digest,
            provenance.checkpoint_digest,
            provenance.comparison_digest,
            (),
            generation_failure_code=_public_failure_code(exc.code),
        )
    except OSError:
        return MeasurementComparisonArtifactResult(
            decision,
            provenance.before_record_digest,
            provenance.after_record_digest,
            provenance.checkpoint_digest,
            provenance.comparison_digest,
            (),
            generation_failure_code="LOCAL_IO_FAILED",
        )
    return MeasurementComparisonArtifactResult(
        decision,
        provenance.before_record_digest,
        provenance.after_record_digest,
        provenance.checkpoint_digest,
        provenance.comparison_digest,
        artifacts,
    )


def _validate_context(provider: object, profile_id: object) -> None:
    if (
        type(provider) is not str
        or provider not in {"ga4_admin", "openai_ads", "gtm"}
        or not _is_safe_ref(profile_id)
    ):
        raise AdhelmError(
            "INVALID_MEASUREMENT_COMPARISON_ARTIFACT_CONTEXT",
            "Measurement-comparison artifact context is invalid.",
            exit_code=2,
        )


def _provenance(comparison: MeasurementComparison) -> MeasurementComparisonArtifactProvenance:
    try:
        return MeasurementComparisonArtifactProvenance(
            before_record_digest=comparison.before.record_digest,
            after_record_digest=comparison.after.record_digest,
            checkpoint_digest=comparison.checkpoint_digest,
            comparison_digest=comparison.comparison_digest,
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise _source_error() from None


def _preflight_checked_document(
    comparison: MeasurementComparison, decision: ArtifactDecision
) -> None:
    if (
        decision.max_rows is None
        or decision.max_output_bytes is None
        or decision.preferences_digest is None
    ):
        raise _artifact_policy_error()
    document = _comparison_artifact_document(
        comparison, key=_PREFLIGHT_KEY, max_rows=decision.max_rows
    )
    presented = present_document(
        document,
        decision.detail,
        MeasurementComparisonArtifactDocument.model_json_schema()
        if decision.detail == "detailed"
        else None,
    )
    rendered = tuple(_render(presented, format_name) for format_name in decision.formats)
    if any(len(contents) > decision.max_output_bytes for contents in rendered):
        raise AdhelmError(
            "ARTIFACT_SIZE_LIMIT_EXCEEDED",
            "The requested artifact exceeds the configured output-size bound.",
        )


def _comparison_artifact_document(
    comparison: MeasurementComparison,
    *,
    key: bytes,
    max_rows: int,
) -> dict[str, object]:
    """Build the sole supported redacted comparison document from closed facts."""
    if type(key) is not bytes or len(key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED",
            "Artifact redaction requires a private local 32-byte export key.",
        )
    if len(comparison.facts) > max_rows:
        raise AdhelmError(
            "ARTIFACT_ROW_LIMIT_EXCEEDED",
            "Measurement comparison exceeds the supported artifact row bound.",
        )
    try:
        document = MeasurementComparisonArtifactDocument(
            source_evidence=_provenance(comparison),
            content=MeasurementComparisonArtifactContent(
                scope=_artifact_scope(comparison.scope, comparison.before.profile_ref, key=key),
                sources=(
                    _artifact_source("before", comparison.before),
                    _artifact_source("after", comparison.after),
                ),
                historical_review=MeasurementComparisonArtifactHistoricalReview(
                    as_of=comparison.after.recorded_at,
                    observation_interval_start=comparison.observation_interval_start,
                    observation_interval_end=comparison.observation_interval_end,
                    fingerprint_key_continuity=comparison.fingerprint_key_continuity,
                ),
                source_completeness=comparison.source_completeness,
                facts=tuple(_artifact_fact(item, key=key) for item in comparison.facts),
                counts=MeasurementComparisonArtifactCounts(
                    changed_count=comparison.changed_count,
                    added_count=comparison.added_count,
                    observed_absent_count=comparison.observed_absent_count,
                ),
                authority=MeasurementComparisonArtifactAuthority(),
            ),
        )
        return document.model_dump(mode="json", warnings="error")
    except AdhelmError:
        raise
    except Exception:
        raise _source_error() from None


def _artifact_scope(
    scope: MeasurementComparisonScope, profile_ref: str, *, key: bytes
) -> MeasurementComparisonArtifactScope:
    profile = opaque_ref(key, profile_ref)
    if isinstance(scope, Ga4MeasurementComparisonScope):
        return Ga4MeasurementComparisonArtifactScope(
            capability_version=scope.capability_version,
            profile_ref=profile,
            account_ref=opaque_ref(key, scope.account_ref),
            property_ref=opaque_ref(key, scope.property_ref),
            web_stream_ref=opaque_ref(key, scope.web_stream_ref),
        )
    if isinstance(scope, OpenAIMeasurementComparisonScope):
        return OpenAIMeasurementComparisonArtifactScope(
            capability_version=scope.capability_version,
            profile_ref=profile,
            account_ref=opaque_ref(key, scope.account_ref),
        )
    if isinstance(scope, GtmMeasurementComparisonScope):
        return GtmMeasurementComparisonArtifactScope(
            capability_version=scope.capability_version,
            profile_ref=profile,
            account_ref=opaque_ref(key, scope.account_ref),
            container_ref=opaque_ref(key, scope.container_ref),
        )
    raise _source_error()


def _artifact_source(
    position: Literal["before", "after"], source: MeasurementComparisonSource
) -> MeasurementComparisonArtifactSource:
    try:
        return MeasurementComparisonArtifactSource(
            position=position,
            record_digest=source.record_digest,
            snapshot_digest=source.snapshot_digest,
            observation_digest=source.observation_digest,
            recorded_at=source.recorded_at,
            read_started_at=source.read_started_at,
            read_finished_at=source.read_finished_at,
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise _source_error() from None


def _artifact_fact(
    fact: MeasurementComparisonFact, *, key: bytes
) -> MeasurementComparisonArtifactFact:
    try:
        return MeasurementComparisonArtifactFact(
            resource_type=fact.resource_type,
            resource_ref=opaque_ref(key, fact.resource_ref),
            status=fact.status,
            before_fact_digest=fact.before_fact_digest,
            after_fact_digest=fact.after_fact_digest,
            changed_fields=fact.changed_fields,
            before_observed_count=fact.before_observed_count,
            after_observed_count=fact.after_observed_count,
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise _source_error() from None


def _validated_references(
    before_record_digest: object,
    after_record_digest: object,
    checkpoint_digest: object,
    comparison_digest: object,
) -> None:
    if (
        not _is_artifact_digest(before_record_digest)
        or not _is_artifact_digest(after_record_digest)
        or before_record_digest == after_record_digest
        or not _is_artifact_digest(checkpoint_digest)
        or not _is_artifact_digest(comparison_digest)
    ):
        raise _source_error()


def _public_failure_code(value: object) -> str | None:
    """Expose only a fixed artifact failure vocabulary in agent-visible output."""
    if value is None:
        return None
    if type(value) is not str:
        return "LOCAL_ARTIFACT_FAILURE"
    if value == "X" * 80:
        return value
    if value in _PUBLIC_FAILURE_CODES:
        return value
    return "LOCAL_ARTIFACT_FAILURE"


def _artifact_policy_error() -> AdhelmError:
    return AdhelmError(
        "ARTIFACT_POLICY_INVALID",
        "Artifact generation requires a resolved installed local policy.",
        exit_code=2,
    )


def _source_error() -> AdhelmError:
    return AdhelmError(
        "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID",
        "Verified measurement-comparison artifact source is invalid.",
        exit_code=2,
    )


def _result_error() -> AdhelmError:
    return AdhelmError(
        "MEASUREMENT_COMPARISON_ARTIFACT_RESULT_INVALID",
        "Measurement-comparison artifact result is invalid.",
        exit_code=2,
    )


def _matches_artifact_metadata_name(item: ArtifactMetadata, expected_format: str) -> bool:
    if not _is_artifact_digest(item.input_digest) or not _is_artifact_digest(item.settings_digest):
        return False
    base = f"artifact-{item.input_digest[:16]}-{item.settings_digest[:16]}"
    pattern = re.escape(base) + r"(?:\.v(?:[2-9]|[1-9][0-9]{1,3}))?\." + re.escape(expected_format)
    return re.fullmatch(pattern, item.path.name) is not None


def _validated_measurement_comparison_artifact_result(
    result: MeasurementComparisonArtifactResult,
) -> tuple[ArtifactDecision, tuple[ArtifactMetadata, ...]]:
    """Keep direct result serialization inside the closed sharing boundary."""
    if type(result) is not MeasurementComparisonArtifactResult:
        raise _result_error()
    decision = _validated_artifact_decision(result.effective_decision)
    try:
        _validated_references(
            result.before_record_digest,
            result.after_record_digest,
            result.checkpoint_digest,
            result.comparison_digest,
        )
    except AdhelmError:
        raise _result_error() from None
    if type(result.artifacts) is not tuple:
        raise _result_error()
    metadata = result.artifacts
    if decision.outcome != "GENERATE":
        if metadata or result.generation_failure_code is not None:
            raise _result_error()
        return decision, metadata
    if not metadata and result.generation_failure_code is None:
        raise _result_error()
    if len(metadata) > len(decision.formats) or (
        len(metadata) < len(decision.formats) and result.generation_failure_code is None
    ):
        raise _result_error()
    if decision.output_directory is None:
        raise _result_error()
    shared_input_digest: str | None = None
    shared_settings_digest: str | None = None
    expected_formats = decision.formats[: len(metadata)]
    for item, expected_format in zip(metadata, expected_formats, strict=True):
        if (
            type(item) is not ArtifactMetadata
            or type(item.path) is not type(Path())
            or item.path.parent != decision.output_directory
            or not _matches_artifact_metadata_name(item, expected_format)
            or item.path.suffix != "." + expected_format
            or type(item.format) is not str
            or item.format != expected_format
            or type(item.reused) is not bool
            or not _is_artifact_digest(item.artifact_digest)
            or not _is_artifact_digest(item.input_digest)
            or not _is_artifact_digest(item.settings_digest)
            or item.source_record_digest != result.before_record_digest
            or item.checkpoint_digest != result.checkpoint_digest
        ):
            raise _result_error()
        if shared_input_digest is None:
            shared_input_digest = item.input_digest
            shared_settings_digest = item.settings_digest
        elif (
            item.input_digest != shared_input_digest
            or item.settings_digest != shared_settings_digest
        ):
            raise _result_error()
    return decision, metadata

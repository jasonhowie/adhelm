"""Typed static artifacts for verified retained OpenAI pause-plan history.

This module has no provider, credential-store, authorization, or execution
path. It accepts only the closed action reconstructed by ``load_openai_pause_plan``
and emits a redacted historical review through the shared safe artifact writer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import Field, field_validator

from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.artifacts import (
    ArtifactContext,
    ArtifactDecision,
    ArtifactGenerationError,
    ArtifactMetadata,
    _generate_typed_artifacts,
    _validated_artifact_decision,
    resolve_artifact_policy,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.models import OutputPreferences, StrictModel
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.plan_views import OpenAIPauseReview, preview_openai_pause
from adhelm.providers.openai_pause_contract import OPENAI_PAUSE_STATUS, OpenAIPausePlannedAction

_DIGEST = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_RENDERER_IDENTITY = "openai-pause-plan-artifact-v1"
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
        "PLAN_ARTIFACT_SOURCE_UNVERIFIED",
        "TEMPORARY_CLEANUP_FAILED",
        "UNSAFE_ARTIFACT_DESTINATION",
        "UNSUPPORTED_ARTIFACT_FORMAT",
    }
)


def _is_artifact_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class PausePlanArtifactProvenance(StrictModel):
    """The allowlisted retained-history facts that bind a static plan review."""

    record_digest: _DIGEST
    action_digest: _DIGEST
    plan_digest: _DIGEST
    checkpoint_digest: _DIGEST
    recorded_at: datetime
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False]
    requires_current_kernel_checks: Literal[True]

    @field_validator("recorded_at", "expires_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pause plan artifact timestamps must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution_authority must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_kernel_requirement(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("requires_current_kernel_checks must be true")
        return value


class PausePlanArtifactHistoricalReview(StrictModel):
    """Deterministic time semantics for a static retained-plan view."""

    as_of: datetime
    expired_at_as_of: bool
    current_validity: Literal["NOT_EVALUATED_HISTORICAL_ARTIFACT"] = (
        "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    )
    static_artifact: Literal[True] = True
    opening_or_editing_authorizes_execution: Literal[False] = False

    @field_validator("as_of")
    @classmethod
    def utc_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact as-of time must be aware")
        return value.astimezone(UTC)


class PausePlanArtifactRecoveryLimits(StrictModel):
    """Exact fixed-plan recovery limits without a write instruction."""

    allowed_recovery_actions: tuple[Literal["PAUSE"], ...] = ("PAUSE",)
    blind_retry_permitted: Literal[False] = False
    ambiguity_status: Literal["UNRESOLVED_NO_BLIND_RETRY"] = "UNRESOLVED_NO_BLIND_RETRY"
    requires_current_kernel_checks: Literal[True] = True


class PausePlanArtifactAuthority(StrictModel):
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_command: Literal["UNRELEASED"] = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS


class PausePlanArtifactContent(StrictModel):
    provenance: PausePlanArtifactProvenance
    historical_review: PausePlanArtifactHistoricalReview
    review: OpenAIPauseReview
    recovery_limits: PausePlanArtifactRecoveryLimits
    authority: PausePlanArtifactAuthority


class PausePlanArtifactDocument(StrictModel):
    """One schema-versioned source-constrained static pause-plan artifact."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["openai_pause_plan_review"] = "openai_pause_plan_review"
    source_evidence: dict[Literal["source_record_digest", "checkpoint_digest"], _DIGEST]
    content: PausePlanArtifactContent


@dataclass(frozen=True)
class PausePlanArtifactResult:
    """Bounded local artifact outcome that never changes retained plan history."""

    effective_decision: ArtifactDecision
    source_record_digest: str
    checkpoint_digest: str | None
    artifacts: tuple[ArtifactMetadata, ...]
    generation_failure_code: str | None = None

    def to_data(self) -> dict[str, object]:
        decision, metadata = _validated_pause_plan_artifact_result(self)
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
            "source_record_digest": self.source_record_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "historical_static_artifact": True,
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


def pause_plan_artifact_failure_preview(
    decision: ArtifactDecision,
    *,
    source_record_digest: str,
    checkpoint_digest: str,
) -> tuple[dict[str, object], list[str]]:
    """Reserve every post-publication result field before creating a file."""
    decision = _validated_artifact_decision(decision)
    if not _is_artifact_digest(source_record_digest) or not _is_artifact_digest(checkpoint_digest):
        raise AdhelmError(
            "PLAN_ARTIFACT_SOURCE_INVALID",
            "Pause-plan artifact source reference is invalid.",
            exit_code=2,
        )
    if decision.output_directory is None:
        raise AdhelmError(
            "ARTIFACT_POLICY_INVALID", "Pause-plan artifact output directory is unavailable."
        )
    artifacts = tuple(
        ArtifactMetadata(
            path=decision.output_directory
            / ("artifact-" + "f" * 16 + "-" + "f" * 16 + ".v9999." + format_name),
            format=format_name,
            reused=False,
            artifact_digest="f" * 64,
            input_digest="f" * 64,
            settings_digest="f" * 64,
            source_record_digest=source_record_digest,
            checkpoint_digest=checkpoint_digest,
        )
        for format_name in decision.formats
    )
    result = PausePlanArtifactResult(
        effective_decision=decision,
        source_record_digest=source_record_digest,
        checkpoint_digest=checkpoint_digest,
        artifacts=artifacts,
        generation_failure_code="X" * 80,
    )
    return result.to_data(), ["artifact:sha256:" + item.artifact_digest for item in artifacts]


def preflight_openai_pause_plan_artifacts(
    preferences: OutputPreferences, *, profile_id: str
) -> ArtifactDecision:
    """Resolve a saved before-approval plan rule before provider access."""
    if type(profile_id) is not str or not profile_id or len(profile_id) > 160:
        raise AdhelmError(
            "INVALID_PLAN_ARTIFACT_CONTEXT", "Pause-plan artifact context is invalid."
        )
    decision = resolve_artifact_policy(
        ArtifactContext(
            workflow="plan",
            stage="before_approval",
            purpose="plan_review",
            provider="openai_ads",
            profile_id=profile_id,
        ),
        preferences,
    )
    if decision.outcome == "NEEDS_CONFIGURATION":
        raise AdhelmError(
            "ARTIFACT_CONFIGURATION_REQUIRED",
            "A matching pause-plan artifact rule needs local configuration.",
            next_action="Configure supported formats and an existing local output directory.",
        )
    return decision


def explicit_openai_pause_plan_artifact_decision(
    preferences: OutputPreferences,
    *,
    profile_id: str,
    formats: tuple[Literal["json", "csv", "html"], ...],
    output_directory: str | None,
    replacement_authorized: bool | None,
) -> ArtifactDecision:
    """Resolve one explicit user request without persisting a preference change."""
    from adhelm.artifacts import ArtifactTaskOverride

    if type(profile_id) is not str or not profile_id or len(profile_id) > 160:
        raise AdhelmError(
            "INVALID_PLAN_ARTIFACT_CONTEXT", "Pause-plan artifact context is invalid."
        )
    return resolve_artifact_policy(
        ArtifactContext(
            workflow="plan",
            stage="before_approval",
            purpose="plan_review",
            provider="openai_ads",
            profile_id=profile_id,
        ),
        preferences,
        task_override=ArtifactTaskOverride(
            requested=True,
            formats=formats,
            output_directory=output_directory,
            purpose="plan_review",
            replacement_authorized=replacement_authorized,
        ),
    )


def render_openai_pause_plan_artifacts(
    action: OpenAIPausePlannedAction,
    provenance: Mapping[str, object],
    decision: ArtifactDecision,
    *,
    key: bytes,
) -> PausePlanArtifactResult:
    """Render a canonical historical view after its source was loaded and verified.

    Callers must obtain ``action`` and ``provenance`` from
    ``load_openai_pause_plan``. This function repeats closed-action validation
    before redaction so constructed nested models cannot bypass the artifact
    boundary.
    """
    checked_action, source = revalidate_openai_pause_plan_artifact_source(action, provenance)
    decision = _validated_artifact_decision(decision)
    if decision.outcome != "GENERATE":
        return PausePlanArtifactResult(decision, source.record_digest, source.checkpoint_digest, ())
    try:
        artifacts = _generate_typed_artifacts(
            decision,
            source_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
            document_builder=lambda max_rows: _pause_plan_document(
                checked_action, source, key=key, max_rows=max_rows
            ),
            source_schema=PausePlanArtifactDocument.model_json_schema(),
            key=key,
            renderer_identity=_RENDERER_IDENTITY,
        )
    except ArtifactGenerationError as exc:
        return PausePlanArtifactResult(
            decision,
            source.record_digest,
            source.checkpoint_digest,
            exc.completed,
            generation_failure_code=_public_failure_code(exc.failure_code),
        )
    except AdhelmError as exc:
        return PausePlanArtifactResult(
            decision,
            source.record_digest,
            source.checkpoint_digest,
            (),
            generation_failure_code=_public_failure_code(exc.code),
        )
    except OSError:
        return PausePlanArtifactResult(
            decision,
            source.record_digest,
            source.checkpoint_digest,
            (),
            generation_failure_code="LOCAL_IO_FAILED",
        )
    return PausePlanArtifactResult(
        decision, source.record_digest, source.checkpoint_digest, artifacts
    )


def _public_failure_code(value: object) -> str | None:
    """Expose only a fixed artifact failure vocabulary in agent-visible output."""
    if value is None:
        return None
    if type(value) is not str:
        return "LOCAL_ARTIFACT_FAILURE"
    if value == "X" * 80:
        # The pre-write result probe reserves the full supported code width.
        return value
    if value in _PUBLIC_FAILURE_CODES:
        return value
    return "LOCAL_ARTIFACT_FAILURE"


def _result_error() -> AdhelmError:
    return AdhelmError(
        "PLAN_ARTIFACT_RESULT_INVALID",
        "Pause-plan artifact result is invalid.",
        exit_code=2,
    )


def _matches_artifact_metadata_name(item: ArtifactMetadata, expected_format: str) -> bool:
    """Bind a reported path to the exact versioned-writer name for its digests."""
    if not _is_artifact_digest(item.input_digest) or not _is_artifact_digest(item.settings_digest):
        return False
    base = f"artifact-{item.input_digest[:16]}-{item.settings_digest[:16]}"
    # ``_write_versioned`` uses the unsuffixed name first, then .v2 through
    # .v9999. It never emits .v1, leading-zero versions, or larger versions.
    pattern = re.escape(base) + r"(?:\.v(?:[2-9]|[1-9][0-9]{1,3}))?\." + re.escape(expected_format)
    return re.fullmatch(pattern, item.path.name) is not None


def _validated_pause_plan_artifact_result(
    result: PausePlanArtifactResult,
) -> tuple[ArtifactDecision, tuple[ArtifactMetadata, ...]]:
    """Keep direct result serialization inside the closed sharing boundary."""
    if type(result) is not PausePlanArtifactResult:
        raise _result_error()
    decision = _validated_artifact_decision(result.effective_decision)
    if not _is_artifact_digest(result.source_record_digest) or (
        result.checkpoint_digest is not None and not _is_artifact_digest(result.checkpoint_digest)
    ):
        raise _result_error()
    if type(result.artifacts) is not tuple:
        raise _result_error()
    metadata = result.artifacts
    if decision.outcome != "GENERATE":
        if metadata or result.generation_failure_code is not None:
            raise _result_error()
        return decision, metadata
    if not metadata and result.generation_failure_code is None:
        raise _result_error()
    if len(metadata) > len(decision.formats):
        raise _result_error()
    if len(metadata) < len(decision.formats) and result.generation_failure_code is None:
        raise _result_error()
    expected_formats = decision.formats[: len(metadata)]
    shared_input_digest: str | None = None
    shared_settings_digest: str | None = None
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
            or not _is_artifact_digest(item.source_record_digest)
            or not _is_artifact_digest(item.checkpoint_digest)
            or item.source_record_digest != result.source_record_digest
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


def revalidate_openai_pause_plan_artifact_source(
    action: OpenAIPausePlannedAction, provenance: Mapping[str, object]
) -> tuple[OpenAIPausePlannedAction, PausePlanArtifactProvenance]:
    """Bind the revalidated closed action to its allowlisted retained facts."""
    source = _checked_provenance(provenance)
    checked_action = revalidate_openai_pause_action(action)
    _assert_source_matches_action(source, checked_action)
    return checked_action, source


def _checked_provenance(value: Mapping[str, object]) -> PausePlanArtifactProvenance:
    """Keep only retained allowlisted facts and hide malformed source details."""
    names = (
        "record_digest",
        "action_digest",
        "plan_digest",
        "checkpoint_digest",
        "recorded_at",
        "expires_at",
        "capability_status",
        "historical_evidence_status",
        "execution_authority",
        "requires_current_kernel_checks",
    )
    try:
        selected = {name: value[name] for name in names}
        return PausePlanArtifactProvenance.model_validate_json(canonical_bytes(selected))
    except Exception:
        raise AdhelmError(
            "PLAN_ARTIFACT_SOURCE_INVALID",
            "Verified pause-plan artifact source is invalid.",
            exit_code=2,
        ) from None


def _assert_source_matches_action(
    source: PausePlanArtifactProvenance, action: OpenAIPausePlannedAction
) -> None:
    if (
        source.action_digest != digest(action)
        or source.plan_digest != action.plan.plan_digest
        or source.expires_at != action.plan.expires_at
        or source.capability_status != OPENAI_PAUSE_STATUS
        or action.status != source.capability_status
        or source.execution_authority is not False
        or source.requires_current_kernel_checks is not True
    ):
        raise AdhelmError(
            "PLAN_ARTIFACT_SOURCE_INVALID",
            "Verified pause-plan artifact source does not match its closed action.",
            exit_code=2,
        )


def _pause_plan_document(
    action: OpenAIPausePlannedAction,
    source: PausePlanArtifactProvenance,
    *,
    key: bytes,
    max_rows: int,
) -> dict[str, object]:
    """Build the only supported pause-plan artifact from redacted review facts."""
    if len(action.plan.subplans) > max_rows:
        raise AdhelmError(
            "ARTIFACT_ROW_LIMIT_EXCEEDED", "Pause plan exceeds the supported artifact row bound."
        )
    review = preview_openai_pause(action, opaque_key=key, now=source.recorded_at)
    raw_recovery_actions = tuple(item.value for item in action.plan.subplans[0].recovery_actions)
    if raw_recovery_actions != ("PAUSE",):
        raise AdhelmError(
            "PLAN_ARTIFACT_SOURCE_INVALID",
            "Pause-plan artifact recovery limits do not match the fixed action.",
            exit_code=2,
        )
    recovery_actions = cast(tuple[Literal["PAUSE"], ...], raw_recovery_actions)
    try:
        document = PausePlanArtifactDocument(
            source_evidence={
                "source_record_digest": source.record_digest,
                "checkpoint_digest": source.checkpoint_digest,
            },
            content=PausePlanArtifactContent(
                provenance=source,
                historical_review=PausePlanArtifactHistoricalReview(
                    as_of=source.recorded_at,
                    expired_at_as_of=review.expired_at_review,
                ),
                review=review,
                recovery_limits=PausePlanArtifactRecoveryLimits(
                    allowed_recovery_actions=recovery_actions,
                ),
                authority=PausePlanArtifactAuthority(),
            ),
        )
        return document.model_dump(mode="json", warnings="error")
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "PLAN_ARTIFACT_SOURCE_INVALID",
            "Pause-plan artifact could not be rendered from verified review facts.",
            exit_code=2,
        ) from None

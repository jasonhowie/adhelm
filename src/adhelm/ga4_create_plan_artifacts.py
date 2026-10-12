"""Typed static artifacts for verified retained GA4 key-event CREATE plans.

The module is deliberately history-only: it accepts the closed action returned
by :func:`load_ga4_key_event_create_plan`, derives a redacted static review,
and sends it through the shared descriptor-safe writer.  It has no provider,
credential, authorization, or execution path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
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
from adhelm.ga4_create_plan_records import ga4_key_event_create_plan_record
from adhelm.ga4_create_plan_views import preview_ga4_key_event_create_plan
from adhelm.models import Money, OutputPreferences, StrictModel
from adhelm.providers.ga4_create_contract import (
    GA4_KEY_EVENT_CREATE_PLAN_STATUS,
    Ga4KeyEventCreatePlannedAction,
    revalidate_ga4_key_event_create_planned_action,
)

_DIGEST = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_OPAQUE_REF = Annotated[str, Field(pattern=r"^opaque:[0-9a-f]{64}$")]
_RENDERER_IDENTITY = "ga4-key-event-create-plan-artifact-v1"
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
        "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID",
        "GA4_CREATE_PLAN_ARTIFACT_SOURCE_UNVERIFIED",
        "LOCAL_IO_FAILED",
        "TEMPORARY_CLEANUP_FAILED",
        "UNSAFE_ARTIFACT_DESTINATION",
        "UNSUPPORTED_ARTIFACT_FORMAT",
    }
)
_LONGEST_PUBLIC_FAILURE_CODE = max(_PUBLIC_FAILURE_CODES, key=len)
_DOCUMENT_FIELDS = frozenset({"schema_version", "artifact_kind", "source_evidence", "content"})
_SOURCE_EVIDENCE_FIELDS = frozenset({"source_record_digest", "checkpoint_digest"})


def _is_artifact_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class Ga4CreatePlanArtifactProvenance(StrictModel):
    """Allowlisted retained-history facts binding one static GA4 plan review."""

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
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GA4 artifact timestamps must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution_authority must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_true_kernel_check(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("requires_current_kernel_checks must be true")
        return value


class Ga4CreatePlanArtifactHistoricalReview(StrictModel):
    """Deterministic historical timing for a non-authorizing static file."""

    as_of: datetime
    expired_at_as_of: bool
    current_validity: Literal["NOT_EVALUATED_HISTORICAL_ARTIFACT"] = (
        "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    )
    static_artifact: Literal[True] = True
    opening_or_editing_authorizes_execution: Literal[False] = False

    @field_validator("as_of")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact as-of time must be aware")
        return value.astimezone(UTC)

    @field_validator("expired_at_as_of", mode="before")
    @classmethod
    def exact_expired_flag(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("expired_at_as_of must be a boolean")
        return value

    @field_validator("static_artifact", mode="before")
    @classmethod
    def exact_static(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("static_artifact must be true")
        return value

    @field_validator("opening_or_editing_authorizes_execution", mode="before")
    @classmethod
    def exact_no_opening_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("opening_or_editing_authorizes_execution must be false")
        return value


class Ga4CreatePlanArtifactRecoveryLimits(StrictModel):
    """The fixed GA4 CREATE recovery boundary, without a write instruction."""

    allowed_recovery_actions: tuple[()] = ()
    recovery_disposition: Literal["RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"] = (
        "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"
    )
    blind_retry_permitted: Literal[False] = False
    ambiguity_status: Literal["UNRESOLVED_NO_BLIND_RETRY"] = "UNRESOLVED_NO_BLIND_RETRY"
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("blind_retry_permitted", mode="before")
    @classmethod
    def exact_no_blind_retry(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("blind_retry_permitted must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_kernel_checks(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("requires_current_kernel_checks must be true")
        return value


class Ga4CreatePlanArtifactAuthority(StrictModel):
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_command: Literal["UNRELEASED"] = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_no_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("artifact authority controls must be false")
        return value


class Ga4CreatePlanArtifactState(StrictModel):
    member_count: int = Field(ge=0, le=4_000)
    state: _DIGEST

    @field_validator("member_count", mode="before")
    @classmethod
    def exact_count(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("member_count must be an integer")
        return value


class Ga4CreatePlanArtifactReview(StrictModel):
    """The fixed redacted GA4 CREATE review projection used in every format."""

    schema_version: Literal[1] = 1
    event_type: Literal["GA4_KEY_EVENT_CREATE_PLAN"] = "GA4_KEY_EVENT_CREATE_PLAN"
    provider: Literal["ga4_admin"] = "ga4_admin"
    operation: Literal["CREATE"] = "CREATE"
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    action_digest: _DIGEST
    plan_digest: _DIGEST
    subplan_digests: tuple[_DIGEST, ...] = Field(min_length=1, max_length=1)
    workspace_profile_ref: _OPAQUE_REF
    auth_profile_ref: _OPAQUE_REF
    property_ref: _OPAQUE_REF
    web_stream_ref: _OPAQUE_REF
    event_ref: _OPAQUE_REF
    declared_destination_profile_ref: _OPAQUE_REF
    destination_to_execution_profile_mapping: Literal["NOT_EVIDENCED"]
    expected_timezone: str = Field(min_length=1, max_length=80)
    expected_currency: str = Field(pattern=r"^[A-Z]{3}$")
    candidate_record_digest: _DIGEST
    contract_record_digest: _DIGEST
    contract_version: int = Field(ge=1, le=1_000_000)
    contract_effective_from: datetime
    observation_record_digest: _DIGEST
    configuration_digest: _DIGEST
    profile_configuration_digest: _DIGEST
    inventory_digest: _DIGEST
    read_started_at: datetime
    read_finished_at: datetime
    evaluated_at: datetime
    expires_at: datetime
    before: Ga4CreatePlanArtifactState
    after: Ga4CreatePlanArtifactState
    counting_method: Literal["ONCE_PER_EVENT"]
    default_value: Literal["NONE"]
    configuration_effect: Literal["IMMEDIATE_LIVE_GA4_KEY_EVENT_NO_DRAFT"]
    historical_backfill: Literal["NONE"]
    maximum_incremental_spend: Money
    egress_hosts: tuple[Literal["analyticsadmin.googleapis.com"], ...] = Field(
        min_length=1, max_length=1
    )
    recovery_disposition: Literal["RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"]
    remote_compensation_actions: tuple[()] = ()
    provider_conditional_write: Literal["NOT_DOCUMENTED"]
    provider_idempotency: Literal["NOT_DOCUMENTED"]
    ambiguous_attempt: Literal["UNRESOLVED_NO_BLIND_RETRY"]
    protected_grant_provenance: Literal["LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY"]
    provider_create_permission: Literal["NOT_PREPROVABLE"]
    semantic_source_mapping: Literal["NOT_VERIFIED"]
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"]
    configuration_readback: Literal["REQUIRED_AFTER_CREATE"]
    browser_network_verification: Literal["NOT_EVALUATED"]
    realtime_verification: Literal["NOT_EVALUATED"]
    processed_report_verification: Literal["NOT_EVALUATED"]
    native_conversion_mapping: Literal["NONE_CREATED"]
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False]
    requires_current_kernel_checks: Literal[True]
    execution_command: Literal["UNRELEASED"]
    reviewed_at: datetime
    expired_at_review: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_contract_version(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("contract_version must be an integer")
        return value

    @field_validator(
        "contract_effective_from",
        "read_started_at",
        "read_finished_at",
        "evaluated_at",
        "expires_at",
        "reviewed_at",
    )
    @classmethod
    def review_times_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review timestamps must be aware")
        return value.astimezone(UTC)

    @field_validator(
        "execution_authority", "requires_current_kernel_checks", "expired_at_review", mode="before"
    )
    @classmethod
    def exact_review_boolean(cls, value: object, info: object) -> bool:
        # ``info`` is intentionally not inspected: all three fields need exact
        # JSON booleans, while their literal values are enforced by Pydantic.
        if type(value) is not bool:
            raise ValueError("review controls must be booleans")
        return value

    @model_validator(mode="after")
    def coherent_review(self) -> Ga4CreatePlanArtifactReview:
        if (
            self.read_finished_at < self.read_started_at
            or self.evaluated_at < self.read_finished_at
            or self.contract_effective_from > self.evaluated_at
            or self.before.member_count + 1 != self.after.member_count
            or self.before.state == self.after.state
            or self.maximum_incremental_spend.minor_units != 0
            or self.expires_at <= self.evaluated_at
            or self.expired_at_review != (self.expires_at <= self.reviewed_at)
        ):
            raise ValueError("GA4 artifact review facts are inconsistent")
        return self


class Ga4CreatePlanArtifactContent(StrictModel):
    provenance: Ga4CreatePlanArtifactProvenance
    historical_review: Ga4CreatePlanArtifactHistoricalReview
    review: Ga4CreatePlanArtifactReview
    recovery_limits: Ga4CreatePlanArtifactRecoveryLimits
    authority: Ga4CreatePlanArtifactAuthority

    @model_validator(mode="after")
    def source_bound_content(self) -> Ga4CreatePlanArtifactContent:
        source = self.provenance
        review = self.review
        if (
            review.action_digest != source.action_digest
            or review.plan_digest != source.plan_digest
            or review.expires_at != source.expires_at
            or review.capability_status != source.capability_status
            or review.historical_evidence_status != source.historical_evidence_status
            or self.historical_review.as_of != source.recorded_at
            or review.reviewed_at != self.historical_review.as_of
            or self.historical_review.expired_at_as_of != review.expired_at_review
            or review.recovery_disposition != self.recovery_limits.recovery_disposition
        ):
            raise ValueError("GA4 artifact content does not bind its retained source")
        return self


class Ga4CreatePlanArtifactDocument(StrictModel):
    """One schema-versioned static historical GA4 key-event CREATE review."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["ga4_key_event_create_plan_review"] = "ga4_key_event_create_plan_review"
    source_evidence: dict[Literal["source_record_digest", "checkpoint_digest"], _DIGEST]
    content: Ga4CreatePlanArtifactContent

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def source_evidence_matches_content(self) -> Ga4CreatePlanArtifactDocument:
        source = self.content.provenance
        if self.source_evidence != {
            "source_record_digest": source.record_digest,
            "checkpoint_digest": source.checkpoint_digest,
        }:
            raise ValueError("artifact evidence does not bind retained source")
        return self

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_document(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Reject a copied document whose nested controls no longer bind.

        ``model_copy(update=...)`` does not rerun Pydantic validators.  This
        document is a public static-output boundary, so its serializer must
        independently validate the emitted nested content before a caller can
        hand a forged authority or recovery claim to the renderer.  The raw
        check deliberately reparses only the nested content model: calling
        this document's validator from its own serializer would recurse.
        """
        try:
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("document serialization")
            _validate_artifact_document_raw(cast(dict[str, object], output))
            return output
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 CREATE plan artifact document is invalid") from None


@dataclass(frozen=True)
class Ga4CreatePlanArtifactResult:
    """Bounded local artifact outcome that never changes plan history."""

    effective_decision: ArtifactDecision
    source_record_digest: str
    checkpoint_digest: str | None
    artifacts: tuple[ArtifactMetadata, ...]
    generation_failure_code: str | None = None

    def to_data(self) -> dict[str, object]:
        decision, metadata = _validated_ga4_create_plan_artifact_result(self)
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


def preflight_ga4_key_event_create_plan_artifacts(
    preferences: OutputPreferences, *, profile_id: str
) -> ArtifactDecision:
    """Resolve a saved GA4 before-approval plan rule before workflow reads."""
    _validate_context(profile_id)
    decision = resolve_artifact_policy(
        ArtifactContext(
            workflow="plan",
            stage="before_approval",
            purpose="plan_review",
            provider="ga4_admin",
            profile_id=profile_id,
        ),
        preferences,
    )
    if decision.outcome == "NEEDS_CONFIGURATION":
        raise AdhelmError(
            "ARTIFACT_CONFIGURATION_REQUIRED",
            "A matching GA4 CREATE plan artifact rule needs local configuration.",
            next_action="Configure supported formats and an existing local output directory.",
        )
    return decision


def explicit_ga4_key_event_create_plan_artifact_decision(
    preferences: OutputPreferences,
    *,
    profile_id: str,
    formats: tuple[ArtifactFormat, ...],
    output_directory: str | None,
    replacement_authorized: bool | None,
) -> ArtifactDecision:
    """Resolve one explicit GA4 plan-artifact request without changing preferences."""
    _validate_context(profile_id)
    return resolve_artifact_policy(
        ArtifactContext(
            workflow="plan",
            stage="before_approval",
            purpose="plan_review",
            provider="ga4_admin",
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


def ga4_create_plan_artifact_failure_preview(
    decision: ArtifactDecision,
    *,
    source_record_digest: str,
    checkpoint_digest: str,
) -> tuple[dict[str, object], list[str]]:
    """Reserve every possible partial-result field before a key or file access."""
    decision = _validated_artifact_decision(decision)
    if not _is_artifact_digest(source_record_digest) or not _is_artifact_digest(checkpoint_digest):
        raise _source_error()
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
            source_record_digest=source_record_digest,
            checkpoint_digest=checkpoint_digest,
        )
        for format_name in decision.formats
    )
    result = Ga4CreatePlanArtifactResult(
        decision,
        source_record_digest,
        checkpoint_digest,
        artifacts,
        generation_failure_code=_LONGEST_PUBLIC_FAILURE_CODE,
    )
    return result.to_data(), ["artifact:sha256:" + item.artifact_digest for item in artifacts]


def ga4_create_plan_artifact_failure_result(
    decision: ArtifactDecision,
    *,
    source_record_digest: str,
    checkpoint_digest: str | None,
    failure_code: object,
) -> Ga4CreatePlanArtifactResult:
    """Return a closed post-retention failure result without source payload data."""
    decision = _validated_artifact_decision(decision)
    if not _is_artifact_digest(source_record_digest) or (
        checkpoint_digest is not None and not _is_artifact_digest(checkpoint_digest)
    ):
        raise _result_error()
    if decision.outcome != "GENERATE":
        raise _result_error()
    return Ga4CreatePlanArtifactResult(
        decision,
        source_record_digest,
        checkpoint_digest,
        (),
        generation_failure_code=_public_failure_code(failure_code) or "LOCAL_ARTIFACT_FAILURE",
    )


def revalidate_ga4_key_event_create_plan_artifact_source(
    action: Ga4KeyEventCreatePlannedAction, provenance: Mapping[str, object]
) -> tuple[Ga4KeyEventCreatePlannedAction, Ga4CreatePlanArtifactProvenance]:
    """Rebuild a closed action and bind it to allowlisted retained facts."""
    checked_action = revalidate_ga4_key_event_create_planned_action(action)
    source = _checked_provenance(provenance, expires_at=checked_action.plan.expires_at)
    record = ga4_key_event_create_plan_record(checked_action)
    if (
        source.action_digest != record.action_digest
        or source.plan_digest != record.plan_digest
        or source.expires_at != record.expires_at
        or source.capability_status != GA4_KEY_EVENT_CREATE_PLAN_STATUS
        or checked_action.status != source.capability_status
        or source.historical_evidence_status != "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
        or source.execution_authority is not False
        or source.requires_current_kernel_checks is not True
    ):
        raise _source_error()
    return checked_action, source


def preflight_ga4_key_event_create_plan_artifact_output(
    action: Ga4KeyEventCreatePlannedAction,
    provenance: Mapping[str, object],
    decision: ArtifactDecision,
) -> None:
    """Render every selected document with a fixed-width synthetic HMAC key.

    This post-retention helper opens neither an output directory nor a sharing
    key.  It deliberately runs only after the caller reloads the one verified
    retained action; a plan's actual review cannot be known before its EDIT
    inventory workflow has completed.
    """
    checked_action, source = revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    decision = _validated_artifact_decision(decision)
    if decision.outcome != "GENERATE":
        return
    if (
        decision.max_rows is None
        or decision.max_output_bytes is None
        or decision.preferences_digest is None
    ):
        raise _artifact_policy_error()
    document = _ga4_create_plan_document(
        checked_action, source, key=_PREFLIGHT_KEY, max_rows=decision.max_rows
    )
    presented = present_document(
        document,
        decision.detail,
        (
            Ga4CreatePlanArtifactDocument.model_json_schema()
            if decision.detail == "detailed"
            else None
        ),
    )
    rendered = tuple(_render(presented, format_name) for format_name in decision.formats)
    if any(len(contents) > decision.max_output_bytes for contents in rendered):
        raise AdhelmError(
            "ARTIFACT_SIZE_LIMIT_EXCEEDED",
            "The requested GA4 CREATE plan artifact exceeds the configured output-size bound.",
        )


def render_ga4_key_event_create_plan_artifacts(
    action: Ga4KeyEventCreatePlannedAction,
    provenance: Mapping[str, object],
    decision: ArtifactDecision,
    *,
    key: bytes,
) -> Ga4CreatePlanArtifactResult:
    """Render a redacted static review after closed retained-source validation."""
    checked_action, source = revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    decision = _validated_artifact_decision(decision)
    if decision.outcome != "GENERATE":
        return Ga4CreatePlanArtifactResult(
            decision, source.record_digest, source.checkpoint_digest, ()
        )
    # A direct library call receives the same zero-key bound check as the CLI,
    # before the shared writer opens a destination or sees the real key.
    preflight_ga4_key_event_create_plan_artifact_output(
        checked_action, source.model_dump(mode="json"), decision
    )
    try:
        artifacts = _generate_typed_artifacts(
            decision,
            source_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
            document_builder=lambda max_rows: _ga4_create_plan_document(
                *revalidate_ga4_key_event_create_plan_artifact_source(
                    checked_action, source.model_dump(mode="json")
                ),
                key=key,
                max_rows=max_rows,
            ),
            source_schema=Ga4CreatePlanArtifactDocument.model_json_schema(),
            key=key,
            renderer_identity=_RENDERER_IDENTITY,
        )
    except ArtifactGenerationError as exc:
        return Ga4CreatePlanArtifactResult(
            decision,
            source.record_digest,
            source.checkpoint_digest,
            exc.completed,
            generation_failure_code=_public_failure_code(exc.failure_code),
        )
    except AdhelmError as exc:
        return Ga4CreatePlanArtifactResult(
            decision,
            source.record_digest,
            source.checkpoint_digest,
            (),
            generation_failure_code=_public_failure_code(exc.code),
        )
    except OSError:
        return Ga4CreatePlanArtifactResult(
            decision,
            source.record_digest,
            source.checkpoint_digest,
            (),
            generation_failure_code="LOCAL_IO_FAILED",
        )
    return Ga4CreatePlanArtifactResult(
        decision, source.record_digest, source.checkpoint_digest, artifacts
    )


def _validate_context(profile_id: object) -> None:
    if (
        type(profile_id) is not str
        or not profile_id
        or len(profile_id) > 160
        or "/" in profile_id
        or "\\" in profile_id
        or any(ord(character) < 32 or ord(character) == 127 for character in profile_id)
    ):
        raise AdhelmError(
            "INVALID_GA4_CREATE_PLAN_ARTIFACT_CONTEXT",
            "GA4 CREATE plan artifact context is invalid.",
            exit_code=2,
        )


def _checked_provenance(
    value: Mapping[str, object], *, expires_at: datetime
) -> Ga4CreatePlanArtifactProvenance:
    """Select one of the two closed loader/provenance shapes.

    The established loader shape has no capability or expiry fields.  Those
    values are derived from the revalidated action, never selected from caller
    input.  A future caller may pass the complete typed artifact provenance,
    but it must match the derived closed form exactly; intermediate or extra
    shapes fail closed.
    """
    loader_names = {
        "record_digest",
        "action_digest",
        "plan_digest",
        "checkpoint_digest",
        "recorded_at",
        "historical_evidence_status",
        "execution_authority",
        "requires_current_kernel_checks",
    }
    artifact_names = loader_names | {"expires_at", "capability_status"}
    try:
        if type(value) is not dict:
            raise ValueError("complete provenance required")
        supplied_names = set(value)
        if supplied_names != loader_names and supplied_names != artifact_names:
            raise ValueError("complete provenance required")
        selected = {name: value[name] for name in loader_names}
        selected["expires_at"] = expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        selected["capability_status"] = GA4_KEY_EVENT_CREATE_PLAN_STATUS
        source = Ga4CreatePlanArtifactProvenance.model_validate_json(canonical_bytes(selected))
        if supplied_names == artifact_names:
            asserted = Ga4CreatePlanArtifactProvenance.model_validate_json(canonical_bytes(value))
            if asserted != source:
                raise ValueError("artifact provenance differs from closed action")
        return source
    except Exception:
        raise _source_error() from None


def _validate_artifact_document_raw(data: dict[str, object]) -> None:
    """Validate emitted controls without recursively serializing a document.

    The writer receives JSON-mode handler output, including canonical timestamp
    text and lists.  Reparse the nested content through its own strict model;
    validating ``Ga4CreatePlanArtifactDocument`` here would call this serializer
    again.
    """
    if set(data) != _DOCUMENT_FIELDS:
        raise ValueError("document fields")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("document schema version")
    if data.get("artifact_kind") != "ga4_key_event_create_plan_review":
        raise ValueError("document kind")
    source_evidence = data.get("source_evidence")
    content = data.get("content")
    if type(source_evidence) is not dict or set(source_evidence) != _SOURCE_EVIDENCE_FIELDS:
        raise ValueError("document source evidence")
    if type(content) is not dict:
        raise ValueError("document content")
    if any(not _is_artifact_digest(source_evidence[name]) for name in _SOURCE_EVIDENCE_FIELDS):
        raise ValueError("document source digest")
    checked_content = Ga4CreatePlanArtifactContent.model_validate_json(canonical_bytes(content))
    expected_evidence = {
        "source_record_digest": checked_content.provenance.record_digest,
        "checkpoint_digest": checked_content.provenance.checkpoint_digest,
    }
    if source_evidence != expected_evidence:
        raise ValueError("document source binding")


def _ga4_create_plan_document(
    action: Ga4KeyEventCreatePlannedAction,
    source: Ga4CreatePlanArtifactProvenance,
    *,
    key: bytes,
    max_rows: int,
) -> dict[str, object]:
    """Build the only supported GA4 CREATE artifact from redacted review facts."""
    if type(key) is not bytes or len(key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED", "Artifact redaction requires a private local 32-byte key."
        )
    if max_rows < 1:
        raise AdhelmError(
            "ARTIFACT_ROW_LIMIT_EXCEEDED", "GA4 CREATE artifact row limit is invalid."
        )
    try:
        record = ga4_key_event_create_plan_record(action)
        review_data = preview_ga4_key_event_create_plan(
            record, opaque_key=key, reviewed_at=source.recorded_at
        )
        review = Ga4CreatePlanArtifactReview.model_validate_json(canonical_bytes(review_data))
        if len(review.subplan_digests) > max_rows:
            raise ValueError("review exceeds row bound")
        document = Ga4CreatePlanArtifactDocument(
            source_evidence={
                "source_record_digest": source.record_digest,
                "checkpoint_digest": source.checkpoint_digest,
            },
            content=Ga4CreatePlanArtifactContent(
                provenance=source,
                historical_review=Ga4CreatePlanArtifactHistoricalReview(
                    as_of=source.recorded_at,
                    expired_at_as_of=review.expired_at_review,
                ),
                review=review,
                recovery_limits=Ga4CreatePlanArtifactRecoveryLimits(),
                authority=Ga4CreatePlanArtifactAuthority(),
            ),
        )
        return document.model_dump(mode="json", warnings="error")
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _source_error() from None


def _public_failure_code(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        return "LOCAL_ARTIFACT_FAILURE"
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
        "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID",
        "Verified GA4 CREATE plan artifact source is invalid.",
        exit_code=2,
    )


def _result_error() -> AdhelmError:
    return AdhelmError(
        "GA4_CREATE_PLAN_ARTIFACT_RESULT_INVALID",
        "GA4 CREATE plan artifact result is invalid.",
        exit_code=2,
    )


def _matches_artifact_metadata_name(item: ArtifactMetadata, expected_format: str) -> bool:
    if not _is_artifact_digest(item.input_digest) or not _is_artifact_digest(item.settings_digest):
        return False
    base = f"artifact-{item.input_digest[:16]}-{item.settings_digest[:16]}"
    pattern = re.escape(base) + r"(?:\.v(?:[2-9]|[1-9][0-9]{1,3}))?\." + re.escape(expected_format)
    return re.fullmatch(pattern, item.path.name) is not None


def _validated_ga4_create_plan_artifact_result(
    result: Ga4CreatePlanArtifactResult,
) -> tuple[ArtifactDecision, tuple[ArtifactMetadata, ...]]:
    """Keep direct result serialization inside the closed sharing boundary."""
    if type(result) is not Ga4CreatePlanArtifactResult:
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
    if len(metadata) > len(decision.formats) or (
        len(metadata) < len(decision.formats) and result.generation_failure_code is None
    ):
        raise _result_error()
    if decision.output_directory is None:
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

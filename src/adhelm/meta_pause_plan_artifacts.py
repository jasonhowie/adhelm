"""Typed static artifacts for verified retained Meta campaign PAUSE plans.

This module is a history-only presentation boundary.  It reconstructs one
closed M40 action, embeds the existing redacted M40 review, and delegates JSON,
CSV, and HTML publication to the shared descriptor-safe artifact writer.  It
does not load a ledger, configuration, credential, provider, approval, or
execution capability.
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
from adhelm.errors import AdhelmError, ExitCode
from adhelm.meta_pause_plan_records import meta_campaign_pause_plan_record
from adhelm.meta_pause_plan_views import (
    MetaCampaignPausePlanReview,
    review_meta_campaign_pause_plan,
)
from adhelm.models import OutputPreferences, StrictModel
from adhelm.providers.meta_pause_contract import (
    META_CAMPAIGN_PAUSE_PLAN_STATUS,
    MetaCampaignPausePlannedAction,
    revalidate_meta_campaign_pause_planned_action,
)
from adhelm.strict_json import validate_json_value

_DIGEST = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_RENDERER_IDENTITY = "meta-campaign-pause-plan-artifact-v1"
_PREFLIGHT_KEY = b"\0" * 32
_MAX_REVIEW_OUTPUT_BYTES = 65_536
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
        "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID",
        "META_PAUSE_PLAN_ARTIFACT_SOURCE_UNVERIFIED",
        "TEMPORARY_CLEANUP_FAILED",
        "UNSAFE_ARTIFACT_DESTINATION",
        "UNSUPPORTED_ARTIFACT_FORMAT",
    }
)
_LONGEST_PUBLIC_FAILURE_CODE = max(_PUBLIC_FAILURE_CODES, key=len)
_DOCUMENT_FIELDS = frozenset({"schema_version", "artifact_kind", "source_evidence", "content"})
_SOURCE_EVIDENCE_FIELDS = frozenset({"source_record_digest", "checkpoint_digest"})


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MetaCampaignPausePlanArtifactProvenance(StrictModel):
    """Allowlisted retained-history facts for one static Meta PAUSE review."""

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
            raise ValueError("Meta plan artifact timestamps must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution authority must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_true_kernel_checks(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("current kernel checks must be true")
        return value


class MetaCampaignPausePlanArtifactHistoricalReview(StrictModel):
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
            raise ValueError("expired-at-as-of must be a boolean")
        return value

    @field_validator("static_artifact", mode="before")
    @classmethod
    def exact_static(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("static artifact marker must be true")
        return value

    @field_validator("opening_or_editing_authorizes_execution", mode="before")
    @classmethod
    def exact_no_opening_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("artifact opening authority must be false")
        return value


class MetaCampaignPausePlanArtifactRecoveryLimits(StrictModel):
    """The bounded Meta PAUSE recovery statement, not a retry instruction."""

    allowed_recovery_actions: tuple[Literal["PAUSE"], ...] = ("PAUSE",)
    recovery_disposition: Literal["RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"] = (
        "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"
    )
    blind_retry_permitted: Literal[False] = False
    ambiguity_status: Literal["UNRESOLVED_NO_BLIND_RETRY"] = "UNRESOLVED_NO_BLIND_RETRY"
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("blind_retry_permitted", mode="before")
    @classmethod
    def exact_no_blind_retry(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("blind retry must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_kernel_checks(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("current kernel checks must be true")
        return value


class MetaCampaignPausePlanArtifactAuthority(StrictModel):
    """Static files remain incapable of authorizing a remote effect."""

    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_command: Literal["UNRELEASED"] = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("artifact authority controls must be false")
        return value


class MetaCampaignPausePlanArtifactContent(StrictModel):
    """The one closed document body, with M40's existing review embedded."""

    provenance: MetaCampaignPausePlanArtifactProvenance
    historical_review: MetaCampaignPausePlanArtifactHistoricalReview
    review: MetaCampaignPausePlanReview
    recovery_limits: MetaCampaignPausePlanArtifactRecoveryLimits
    authority: MetaCampaignPausePlanArtifactAuthority

    @model_validator(mode="after")
    def source_bound_content(self) -> MetaCampaignPausePlanArtifactContent:
        source = self.provenance
        review = self.review
        if (
            review.action_digest != source.action_digest
            or review.plan_digest != source.plan_digest
            or review.expires_at != source.expires_at
            or review.capability_status != source.capability_status
            or review.historical_evidence_status != source.historical_evidence_status
            or review.reviewed_at != source.recorded_at
            or self.historical_review.as_of != source.recorded_at
            or self.historical_review.expired_at_as_of
            != (review.expiry_status == "EXPIRED_AT_REVIEW")
            or review.recovery_semantics != self.recovery_limits.recovery_disposition
        ):
            raise ValueError("artifact content does not bind its retained source")
        return self


class MetaCampaignPausePlanArtifactDocument(StrictModel):
    """One versioned, historical, redacted Meta campaign PAUSE artifact."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["meta_campaign_pause_plan_review"] = "meta_campaign_pause_plan_review"
    source_evidence: dict[Literal["source_record_digest", "checkpoint_digest"], _DIGEST]
    content: MetaCampaignPausePlanArtifactContent

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema version must be integer 1")
        return value

    @model_validator(mode="after")
    def source_evidence_matches_content(self) -> MetaCampaignPausePlanArtifactDocument:
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
        """Revalidate emitted nested controls before the writer can serialize them."""
        try:
            if type(self) is not MetaCampaignPausePlanArtifactDocument:
                raise ValueError("artifact document type")
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("artifact document output")
            _validate_artifact_document_raw(cast(dict[str, object], output))
            return output
        except (
            AdhelmError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("Meta campaign PAUSE artifact document is invalid") from None


@dataclass(frozen=True)
class MetaCampaignPausePlanArtifactResult:
    """Bounded local artifact outcome that never changes retained plan history."""

    effective_decision: ArtifactDecision
    source_record_digest: str
    checkpoint_digest: str | None
    artifacts: tuple[ArtifactMetadata, ...]
    generation_failure_code: str | None = None

    def to_data(self) -> dict[str, object]:
        (
            decision,
            metadata,
            source_record_digest,
            checkpoint_digest,
            generation_failure_code,
        ) = _validated_meta_campaign_pause_plan_artifact_result(self)
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
            "source_record_digest": source_record_digest,
            "checkpoint_digest": checkpoint_digest,
            "historical_static_artifact": True,
            "execution_authority": False,
            "remote_mutations": False,
            "opening_or_editing_authorizes_execution": False,
            "generation_failure_code": _public_failure_code(generation_failure_code),
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


def preflight_meta_campaign_pause_plan_artifacts(
    preferences: OutputPreferences,
    *,
    profile_id: str,
    task_override: ArtifactTaskOverride | None = None,
) -> ArtifactDecision:
    """Resolve a saved or one-task static-review policy before any plan activity."""
    checked_preferences = _checked_preferences(preferences)
    _validate_context(profile_id)
    checked_override = _checked_task_override(task_override)
    decision = resolve_artifact_policy(
        ArtifactContext(
            workflow="plan",
            stage="before_approval",
            purpose="plan_review",
            provider="meta_ads",
            profile_id=profile_id,
        ),
        checked_preferences,
        task_override=checked_override,
    )
    if decision.outcome == "NEEDS_CONFIGURATION":
        raise AdhelmError(
            "ARTIFACT_CONFIGURATION_REQUIRED",
            "A matching Meta campaign PAUSE plan artifact rule needs local configuration.",
            next_action="Configure supported formats and an existing local output directory.",
        )
    return _validated_artifact_decision(decision)


def explicit_meta_campaign_pause_plan_artifact_decision(
    preferences: OutputPreferences,
    *,
    profile_id: str,
    formats: tuple[ArtifactFormat, ...],
    output_directory: str | None,
    replacement_authorized: bool | None,
) -> ArtifactDecision:
    """Resolve one explicit Meta plan-artifact request without saving a rule."""
    return preflight_meta_campaign_pause_plan_artifacts(
        preferences,
        profile_id=profile_id,
        task_override=ArtifactTaskOverride(
            requested=True,
            formats=formats,
            output_directory=output_directory,
            purpose="plan_review",
            replacement_authorized=replacement_authorized,
        ),
    )


def meta_campaign_pause_plan_artifact_failure_preview(
    decision: ArtifactDecision,
    *,
    source_record_digest: str,
    checkpoint_digest: str,
) -> tuple[dict[str, object], list[str]]:
    """Reserve every possible partial-result field before a key or file access."""
    checked_decision = _validated_artifact_decision(decision)
    if not _is_digest(source_record_digest) or not _is_digest(checkpoint_digest):
        raise _source_error()
    if checked_decision.outcome != "GENERATE" or checked_decision.output_directory is None:
        raise _artifact_policy_error()
    artifacts = tuple(
        ArtifactMetadata(
            path=checked_decision.output_directory
            / ("artifact-" + "f" * 16 + "-" + "f" * 16 + ".v9999." + format_name),
            format=format_name,
            reused=False,
            artifact_digest="f" * 64,
            input_digest="f" * 64,
            settings_digest="f" * 64,
            source_record_digest=source_record_digest,
            checkpoint_digest=checkpoint_digest,
        )
        for format_name in checked_decision.formats
    )
    result = MetaCampaignPausePlanArtifactResult(
        checked_decision,
        source_record_digest,
        checkpoint_digest,
        artifacts,
        generation_failure_code=_LONGEST_PUBLIC_FAILURE_CODE,
    )
    return result.to_data(), ["artifact:sha256:" + item.artifact_digest for item in artifacts]


def meta_campaign_pause_plan_artifact_failure_result(
    decision: ArtifactDecision,
    *,
    source_record_digest: str,
    checkpoint_digest: str | None,
    failure_code: object,
) -> MetaCampaignPausePlanArtifactResult:
    """Return a closed post-retention failure outcome without action payload data."""
    checked_decision = _validated_artifact_decision(decision)
    if (
        checked_decision.outcome != "GENERATE"
        or not _is_digest(source_record_digest)
        or (checkpoint_digest is not None and not _is_digest(checkpoint_digest))
    ):
        raise _result_error()
    return MetaCampaignPausePlanArtifactResult(
        checked_decision,
        source_record_digest,
        checkpoint_digest,
        (),
        generation_failure_code=_public_failure_code(failure_code) or "LOCAL_ARTIFACT_FAILURE",
    )


def revalidate_meta_campaign_pause_plan_artifact_source(
    action: MetaCampaignPausePlannedAction,
    provenance: Mapping[str, object],
) -> tuple[MetaCampaignPausePlannedAction, MetaCampaignPausePlanArtifactProvenance]:
    """Rebuild the one closed action and bind it to retained loader facts."""
    checked_action = revalidate_meta_campaign_pause_planned_action(action)
    source = _checked_provenance(provenance, expires_at=checked_action.plan.expires_at)
    record = meta_campaign_pause_plan_record(checked_action)
    if (
        source.action_digest != record.action_digest
        or source.plan_digest != record.plan_digest
        or source.expires_at != record.expires_at
        or source.capability_status != META_CAMPAIGN_PAUSE_PLAN_STATUS
        or checked_action.status != source.capability_status
        or source.historical_evidence_status != "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
        or source.execution_authority is not False
        or source.requires_current_kernel_checks is not True
        or source.recorded_at < checked_action.observation_binding.read_finished_at
    ):
        raise _source_error()
    return checked_action, source


def preflight_meta_campaign_pause_plan_artifact_output(
    action: MetaCampaignPausePlannedAction,
    provenance: Mapping[str, object],
    decision: ArtifactDecision,
) -> None:
    """Render all selected documents with a fixed-width synthetic HMAC key.

    This post-retention check deliberately opens neither a sharing key nor an
    output destination.  The CLI must already have reloaded the exact retained
    action and checkpoint before it invokes this helper.
    """
    checked_action, source = revalidate_meta_campaign_pause_plan_artifact_source(action, provenance)
    checked_decision = _validated_artifact_decision(decision)
    if checked_decision.outcome != "GENERATE":
        return
    if (
        checked_decision.max_rows is None
        or checked_decision.max_output_bytes is None
        or checked_decision.preferences_digest is None
    ):
        raise _artifact_policy_error()
    document = _meta_campaign_pause_plan_document(
        checked_action,
        source,
        key=_PREFLIGHT_KEY,
        max_rows=checked_decision.max_rows,
    )
    presented = present_document(
        document,
        checked_decision.detail,
        (
            MetaCampaignPausePlanArtifactDocument.model_json_schema()
            if checked_decision.detail == "detailed"
            else None
        ),
    )
    rendered = tuple(_render(presented, format_name) for format_name in checked_decision.formats)
    if any(len(contents) > checked_decision.max_output_bytes for contents in rendered):
        raise AdhelmError(
            "ARTIFACT_SIZE_LIMIT_EXCEEDED",
            "The requested Meta campaign PAUSE plan artifact exceeds the configured "
            "output-size bound.",
        )


def render_meta_campaign_pause_plan_artifacts(
    action: MetaCampaignPausePlannedAction,
    provenance: Mapping[str, object],
    decision: ArtifactDecision,
    *,
    key: bytes,
) -> MetaCampaignPausePlanArtifactResult:
    """Render a redacted static history view after closed source validation."""
    checked_action, source = revalidate_meta_campaign_pause_plan_artifact_source(action, provenance)
    checked_decision = _validated_artifact_decision(decision)
    if checked_decision.outcome != "GENERATE":
        return MetaCampaignPausePlanArtifactResult(
            checked_decision, source.record_digest, source.checkpoint_digest, ()
        )
    # Direct callers get the same source and zero-key bound guard as the CLI
    # before the descriptor-safe writer opens a destination or sees the real key.
    preflight_meta_campaign_pause_plan_artifact_output(
        checked_action,
        source.model_dump(mode="json", warnings="error"),
        checked_decision,
    )
    try:
        artifacts = _generate_typed_artifacts(
            checked_decision,
            source_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
            document_builder=lambda max_rows: _meta_campaign_pause_plan_document(
                *revalidate_meta_campaign_pause_plan_artifact_source(
                    checked_action,
                    source.model_dump(mode="json", warnings="error"),
                ),
                key=key,
                max_rows=max_rows,
            ),
            source_schema=MetaCampaignPausePlanArtifactDocument.model_json_schema(),
            key=key,
            renderer_identity=_RENDERER_IDENTITY,
        )
    except ArtifactGenerationError as exc:
        return MetaCampaignPausePlanArtifactResult(
            checked_decision,
            source.record_digest,
            source.checkpoint_digest,
            exc.completed,
            generation_failure_code=_public_failure_code(exc.failure_code),
        )
    except AdhelmError as exc:
        return MetaCampaignPausePlanArtifactResult(
            checked_decision,
            source.record_digest,
            source.checkpoint_digest,
            (),
            generation_failure_code=_public_failure_code(exc.code),
        )
    except OSError:
        return MetaCampaignPausePlanArtifactResult(
            checked_decision,
            source.record_digest,
            source.checkpoint_digest,
            (),
            generation_failure_code="LOCAL_IO_FAILED",
        )
    return MetaCampaignPausePlanArtifactResult(
        checked_decision, source.record_digest, source.checkpoint_digest, artifacts
    )


def _checked_preferences(value: object) -> OutputPreferences:
    try:
        if type(value) is not OutputPreferences:
            raise ValueError("preferences type")
        data = value.model_dump(mode="json", warnings="error")
        if type(data) is not dict:
            raise ValueError("preferences data")
        return validate_json_value(OutputPreferences, data)
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise _artifact_policy_error() from None


def _checked_task_override(value: object) -> ArtifactTaskOverride | None:
    if value is None:
        return None
    if type(value) is not ArtifactTaskOverride:
        raise _artifact_policy_error()
    try:
        if type(value.requested) is not bool or value.purpose not in {None, "plan_review"}:
            raise ValueError("task override")
        if (
            value.replacement_authorized is not None
            and type(value.replacement_authorized) is not bool
        ):
            raise ValueError("replacement authorization")
        if type(value.formats) is not tuple or len(value.formats) > 3:
            raise ValueError("formats")
        if any(
            type(format_name) is not str or format_name not in {"json", "csv", "html"}
            for format_name in value.formats
        ):
            raise ValueError("format")
        if len(set(value.formats)) != len(value.formats):
            raise ValueError("duplicate format")
        if value.output_directory is not None and type(value.output_directory) is not str:
            raise ValueError("output directory")
        if not value.requested and (
            value.formats
            or value.output_directory is not None
            or value.replacement_authorized is not None
        ):
            raise ValueError("suppressed override")
        return ArtifactTaskOverride(
            requested=value.requested,
            formats=value.formats,
            output_directory=value.output_directory,
            purpose="plan_review",
            replacement_authorized=value.replacement_authorized,
        )
    except (OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise _artifact_policy_error() from None


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
            "INVALID_META_PAUSE_PLAN_ARTIFACT_CONTEXT",
            "Meta campaign PAUSE plan artifact context is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )


def _checked_provenance(
    value: Mapping[str, object], *, expires_at: datetime
) -> MetaCampaignPausePlanArtifactProvenance:
    """Accept only the exact loader mapping or its fully derived typed form."""
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
        selected["expires_at"] = _utc_text(expires_at)
        selected["capability_status"] = META_CAMPAIGN_PAUSE_PLAN_STATUS
        source = validate_json_value(MetaCampaignPausePlanArtifactProvenance, selected)
        if supplied_names == artifact_names:
            asserted = validate_json_value(MetaCampaignPausePlanArtifactProvenance, value)
            if asserted != source:
                raise ValueError("artifact provenance differs from closed action")
        return source
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise _source_error() from None


def _validate_artifact_document_raw(data: dict[str, object]) -> None:
    """Validate emitted controls without recursively serializing the document."""
    if set(data) != _DOCUMENT_FIELDS:
        raise ValueError("document fields")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("document schema version")
    if data.get("artifact_kind") != "meta_campaign_pause_plan_review":
        raise ValueError("document kind")
    source_evidence = data.get("source_evidence")
    content = data.get("content")
    if type(source_evidence) is not dict or set(source_evidence) != _SOURCE_EVIDENCE_FIELDS:
        raise ValueError("document source evidence")
    if type(content) is not dict:
        raise ValueError("document content")
    if any(not _is_digest(source_evidence[name]) for name in _SOURCE_EVIDENCE_FIELDS):
        raise ValueError("document source digest")
    checked_content = validate_json_value(
        MetaCampaignPausePlanArtifactContent,
        cast(dict[str, Any], content),
    )
    expected_evidence = {
        "source_record_digest": checked_content.provenance.record_digest,
        "checkpoint_digest": checked_content.provenance.checkpoint_digest,
    }
    if source_evidence != expected_evidence:
        raise ValueError("document source binding")


def _meta_campaign_pause_plan_document(
    action: MetaCampaignPausePlannedAction,
    source: MetaCampaignPausePlanArtifactProvenance,
    *,
    key: bytes,
    max_rows: int,
) -> dict[str, object]:
    """Build the sole supported Meta PAUSE static artifact from M40 review facts."""
    if type(key) is not bytes or len(key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED", "Artifact redaction requires a private local 32-byte key."
        )
    if type(max_rows) is not int or max_rows < 1:
        raise AdhelmError(
            "ARTIFACT_ROW_LIMIT_EXCEEDED", "Meta PAUSE artifact row limit is invalid."
        )
    try:
        record = meta_campaign_pause_plan_record(action)
        review = review_meta_campaign_pause_plan(
            record,
            opaque_key=key,
            reviewed_at=source.recorded_at,
            max_output_bytes=_MAX_REVIEW_OUTPUT_BYTES,
        )
        if len(review.subplan_digests) > max_rows:
            raise ValueError("review exceeds row bound")
        document = MetaCampaignPausePlanArtifactDocument(
            source_evidence={
                "source_record_digest": source.record_digest,
                "checkpoint_digest": source.checkpoint_digest,
            },
            content=MetaCampaignPausePlanArtifactContent(
                provenance=source,
                historical_review=MetaCampaignPausePlanArtifactHistoricalReview(
                    as_of=source.recorded_at,
                    expired_at_as_of=review.expiry_status == "EXPIRED_AT_REVIEW",
                ),
                review=review,
                recovery_limits=MetaCampaignPausePlanArtifactRecoveryLimits(),
                authority=MetaCampaignPausePlanArtifactAuthority(),
            ),
        )
        output = document.model_dump(mode="json", warnings="error")
        if type(output) is not dict:
            raise ValueError("document output")
        return cast(dict[str, object], output)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _source_error() from None


def _public_failure_code(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is str and value in _PUBLIC_FAILURE_CODES:
        return value
    return "LOCAL_ARTIFACT_FAILURE"


def _matches_artifact_metadata_name(item: ArtifactMetadata, expected_format: str) -> bool:
    if not _is_digest(item.input_digest) or not _is_digest(item.settings_digest):
        return False
    base = f"artifact-{item.input_digest[:16]}-{item.settings_digest[:16]}"
    pattern = re.escape(base) + r"(?:\.v(?:[2-9]|[1-9][0-9]{1,3}))?\." + re.escape(expected_format)
    return re.fullmatch(pattern, item.path.name) is not None


def _validated_meta_campaign_pause_plan_artifact_result(
    result: MetaCampaignPausePlanArtifactResult,
) -> tuple[
    ArtifactDecision,
    tuple[ArtifactMetadata, ...],
    str,
    str | None,
    str | None,
]:
    """Keep direct result serialization inside the closed sharing boundary."""
    try:
        if type(result) is not MetaCampaignPausePlanArtifactResult:
            raise _result_error()
        decision = _validated_artifact_decision(result.effective_decision)
        source_record_digest = result.source_record_digest
        checkpoint_digest = result.checkpoint_digest
        metadata = result.artifacts
        generation_failure_code = result.generation_failure_code
        if not _is_digest(source_record_digest) or (
            checkpoint_digest is not None and not _is_digest(checkpoint_digest)
        ):
            raise _result_error()
        if type(metadata) is not tuple:
            raise _result_error()
        if decision.outcome != "GENERATE":
            if metadata or generation_failure_code is not None:
                raise _result_error()
            return (
                decision,
                metadata,
                source_record_digest,
                checkpoint_digest,
                generation_failure_code,
            )
        if not metadata and generation_failure_code is None:
            raise _result_error()
        if len(metadata) > len(decision.formats) or (
            len(metadata) < len(decision.formats) and generation_failure_code is None
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
                or not _is_digest(item.artifact_digest)
                or not _is_digest(item.input_digest)
                or not _is_digest(item.settings_digest)
                or not _is_digest(item.source_record_digest)
                or not _is_digest(item.checkpoint_digest)
                or item.source_record_digest != source_record_digest
                or item.checkpoint_digest != checkpoint_digest
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
        return decision, metadata, source_record_digest, checkpoint_digest, generation_failure_code
    except (
        AdhelmError,
        AttributeError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        UnicodeError,
    ):
        raise _result_error() from None


def _artifact_policy_error() -> AdhelmError:
    return AdhelmError(
        "ARTIFACT_POLICY_INVALID",
        "Artifact generation requires a resolved installed local policy.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _source_error() -> AdhelmError:
    return AdhelmError(
        "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID",
        "Verified Meta campaign PAUSE plan artifact source is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _result_error() -> AdhelmError:
    return AdhelmError(
        "META_PAUSE_PLAN_ARTIFACT_RESULT_INVALID",
        "Meta campaign PAUSE plan artifact result is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


__all__ = [
    "MetaCampaignPausePlanArtifactDocument",
    "MetaCampaignPausePlanArtifactProvenance",
    "MetaCampaignPausePlanArtifactResult",
    "explicit_meta_campaign_pause_plan_artifact_decision",
    "meta_campaign_pause_plan_artifact_failure_preview",
    "meta_campaign_pause_plan_artifact_failure_result",
    "preflight_meta_campaign_pause_plan_artifact_output",
    "preflight_meta_campaign_pause_plan_artifacts",
    "render_meta_campaign_pause_plan_artifacts",
    "revalidate_meta_campaign_pause_plan_artifact_source",
]

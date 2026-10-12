"""Typed local planning and historical review for Meta campaign PAUSE.

This command group prepares and retains a fixed local plan only.  It has no
provider POST, approval, native bridge, or generic artifact payload path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import typer
from pydantic import Field, field_validator, model_validator

from adhelm.artifacts import ArtifactDecision, ArtifactTaskOverride
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.meta_pause_plan_records import meta_campaign_pause_plan_record
from adhelm.meta_pause_plan_views import (
    MetaCampaignPausePlanReview,
    review_meta_campaign_pause_plan,
)
from adhelm.models import Money, OutputPreferences, StrictModel
from adhelm.saved_meta_pause_plans import (
    load_meta_campaign_pause_plan,
    meta_campaign_pause_plan_catalog,
)
from adhelm.strict_json import validate_json_value

if TYPE_CHECKING:
    from adhelm.meta_pause_plan_artifacts import MetaCampaignPausePlanArtifactProvenance
    from adhelm.meta_pause_workflow import MetaCampaignPausePlanRequest

app = typer.Typer(no_args_is_help=True)

_PREFIX = "plan meta-campaign-pause"
_CREATE_OUTCOME = "META_CAMPAIGN_PAUSE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
_HISTORY_OUTCOME = "META_CAMPAIGN_PAUSE_PLAN_HISTORY_NO_EXECUTION_AUTHORITY"
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_MAX_UTC_TIMESTAMP = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
_M40_REVIEW_OUTPUT_MAXIMUM_BYTES = 65_536
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_ARTIFACT_SUPPRESSION_REASONS = frozenset(
    {
        "ARTIFACT_MODE_OFF",
        "NO_MATCHING_WORKFLOW_RULE",
        "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
        "PURPOSE_NOT_ENABLED",
        "TASK_OVERRIDE_DISABLED",
    }
)
_ARTIFACT_GENERATION_REASONS = frozenset({"EXPLICIT_USER_REQUEST", "MATCHING_SAVED_RULE"})


class _MetaCampaignPauseCatalogRow(StrictModel):
    """One allowlisted local-history row, never a live campaign claim."""

    record_digest: _Digest
    plan_digest: _Digest
    recorded_at: datetime
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    maximum_incremental_spend: Money
    execution_authority: Literal[False] = False

    @field_validator("recorded_at", "expires_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamps must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("catalog authority must be false")
        return value


class _MetaCampaignPauseCatalog(StrictModel):
    """Closed catalog response that cannot grow a raw retained action field."""

    plans: list[_MetaCampaignPauseCatalogRow] = Field(max_length=500)
    checkpoint_digest: _Digest
    total_count: int = Field(ge=0)
    next_cursor: str | None
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False

    @field_validator("total_count", mode="before")
    @classmethod
    def exact_count(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("catalog count must be an integer")
        return value

    @field_validator("next_cursor", mode="before")
    @classmethod
    def exact_cursor(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("catalog cursor is invalid")
        return value

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("catalog controls must be false")
        return value


class _MetaCampaignPausePlanProvenance(StrictModel):
    """Allowlisted verified-history provenance beside one redacted review."""

    record_digest: _Digest
    action_digest: _Digest
    plan_digest: _Digest
    checkpoint_digest: _Digest
    recorded_at: datetime
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def aware_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provenance timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("provenance authority must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_true_kernel_checks(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("provenance kernel control must be true")
        return value


class _MetaCampaignPauseArtifactPolicyNotice(StrictModel):
    """Closed saved/task output-policy fact beside the retained plan receipt."""

    outcome: Literal["GENERATE", "SUPPRESSED"]
    reason: str
    generation_supported: Literal[True] = True

    @field_validator("generation_supported", mode="before")
    @classmethod
    def exact_generation_support(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("artifact support marker is invalid")
        return value

    @model_validator(mode="after")
    def exact_reason_for_outcome(self) -> _MetaCampaignPauseArtifactPolicyNotice:
        allowed = (
            _ARTIFACT_GENERATION_REASONS
            if self.outcome == "GENERATE"
            else _ARTIFACT_SUPPRESSION_REASONS
        )
        if self.reason not in allowed:
            raise ValueError("artifact policy reason is invalid")
        return self


class _MetaCampaignPausePlanReceipt(StrictModel):
    """Compact retention receipt with no native Meta identity or action data."""

    record_digest: _Digest
    action_digest: _Digest
    plan_digest: _Digest
    subplan_digests: tuple[_Digest, ...] = Field(min_length=1, max_length=1)
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    maximum_incremental_spend: Money
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    review_required: Literal[True] = True
    review_command: str = Field(min_length=1, max_length=160)
    artifact_policy: _MetaCampaignPauseArtifactPolicyNotice
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    review_authorizes_execution: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt expiry must be aware")
        return value.astimezone(UTC)

    @field_validator(
        "execution_authority", "remote_mutations", "review_authorizes_execution", mode="before"
    )
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("receipt controls must be false")
        return value

    @field_validator("requires_current_kernel_checks", "review_required", mode="before")
    @classmethod
    def exact_true_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("receipt controls must be true")
        return value


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _reviewed_at() -> datetime:
    try:
        value = _now_utc()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID",
            "Meta campaign PAUSE history review requires an aware local clock.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise AdhelmError(
            "INVALID_META_PAUSE_PLAN_SELECTION",
            "An exact retained Meta campaign PAUSE record digest is required.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _artifact_request_invalid() -> AdhelmError:
    return AdhelmError(
        "INVALID_META_PAUSE_ARTIFACT_REQUEST",
        "Meta campaign PAUSE artifact options are inconsistent.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Use --no-artifacts alone, or supply consistent artifact options with "
            "supported formats and a local output directory."
        ),
    )


def _task_artifact_override(
    no_artifacts: object,
    artifact_formats: object,
    artifact_output: object,
) -> ArtifactTaskOverride | None:
    """Translate only closed command flags into the shared one-task policy."""
    if type(no_artifacts) is not bool:
        raise _artifact_request_invalid()
    if artifact_formats is None:
        formats: tuple[Literal["json", "csv", "html"], ...] = ()
    elif type(artifact_formats) is list:
        if (
            len(artifact_formats) > 3
            or any(
                type(item) is not str or item not in {"json", "csv", "html"}
                for item in artifact_formats
            )
            or len(set(artifact_formats)) != len(artifact_formats)
        ):
            raise _artifact_request_invalid()
        formats = cast(
            tuple[Literal["json", "csv", "html"], ...],
            tuple(sorted(artifact_formats)),
        )
    else:
        raise _artifact_request_invalid()
    if artifact_output is not None and not isinstance(artifact_output, Path):
        raise _artifact_request_invalid()
    if no_artifacts:
        if formats or artifact_output is not None:
            raise _artifact_request_invalid()
        return ArtifactTaskOverride(requested=False, purpose="plan_review")
    if formats or artifact_output is not None:
        return ArtifactTaskOverride(
            requested=True,
            formats=formats,
            output_directory=str(artifact_output) if artifact_output is not None else None,
            purpose="plan_review",
        )
    return None


def _preflight_artifact_policy(
    preferences: object,
    *,
    profile_id: object,
    task_override: ArtifactTaskOverride | None,
) -> ArtifactDecision:
    """Resolve the closed M43 policy before a Meta session or plan retention."""
    if type(preferences) is not OutputPreferences or type(profile_id) is not str:
        raise AdhelmError(
            "META_PAUSE_PLAN_ARTIFACT_POLICY_INVALID",
            "Meta campaign PAUSE output policy is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    from adhelm.meta_pause_plan_artifacts import preflight_meta_campaign_pause_plan_artifacts

    decision = preflight_meta_campaign_pause_plan_artifacts(
        preferences,
        profile_id=profile_id,
        task_override=task_override,
    )
    if type(decision) is not ArtifactDecision:
        raise AdhelmError(
            "META_PAUSE_PLAN_ARTIFACT_POLICY_INVALID",
            "Meta campaign PAUSE output policy is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if decision.outcome == "NEEDS_CONFIGURATION":
        raise AdhelmError(
            "ARTIFACT_CONFIGURATION_REQUIRED",
            "The selected Meta campaign PAUSE artifact policy needs local configuration.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Configure a complete local artifact policy or use --no-artifacts before planning."
            ),
        )
    allowed = (
        _ARTIFACT_GENERATION_REASONS
        if decision.outcome == "GENERATE"
        else _ARTIFACT_SUPPRESSION_REASONS
        if decision.outcome == "SUPPRESSED"
        else frozenset()
    )
    if type(decision.reason) is not str or decision.reason not in allowed:
        raise AdhelmError(
            "META_PAUSE_PLAN_ARTIFACT_POLICY_INVALID",
            "Meta campaign PAUSE output policy is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return decision


def _artifact_policy_notice(decision: object) -> _MetaCampaignPauseArtifactPolicyNotice:
    """Drop decision paths/settings while retaining the selected safe outcome."""
    try:
        if type(decision) is not ArtifactDecision:
            raise ValueError("decision")
        if decision.outcome == "GENERATE":
            outcome: Literal["GENERATE", "SUPPRESSED"] = "GENERATE"
        elif decision.outcome == "SUPPRESSED":
            outcome = "SUPPRESSED"
        else:
            raise ValueError("decision outcome")
        return _MetaCampaignPauseArtifactPolicyNotice(
            outcome=outcome,
            reason=decision.reason,
        )
    except Exception:
        raise AdhelmError(
            "META_PAUSE_PLAN_ARTIFACT_POLICY_INVALID",
            "Meta campaign PAUSE output policy is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _review_command(record_digest: str) -> str:
    return f"{_PREFIX} show {record_digest}"


def _receipt(
    *,
    record_digest: str,
    action_digest: str,
    plan_digest: str,
    subplan_digests: tuple[str, ...],
    expires_at: datetime,
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"],
    maximum_incremental_spend: Money,
    artifact_policy: _MetaCampaignPauseArtifactPolicyNotice,
) -> dict[str, object]:
    return _MetaCampaignPausePlanReceipt(
        record_digest=record_digest,
        action_digest=action_digest,
        plan_digest=plan_digest,
        subplan_digests=subplan_digests,
        expires_at=expires_at,
        capability_status=capability_status,
        maximum_incremental_spend=maximum_incremental_spend,
        historical_evidence_status="HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        review_command=_review_command(record_digest),
        artifact_policy=artifact_policy,
    ).model_dump(mode="json", warnings="error")


def _receipt_probe(artifact_policy: _MetaCampaignPauseArtifactPolicyNotice) -> dict[str, object]:
    """Reserve every fixed receipt field before a session, HTTP, or append."""
    placeholder = "0" * 64
    return _receipt(
        record_digest=placeholder,
        action_digest=placeholder,
        plan_digest=placeholder,
        subplan_digests=(placeholder,),
        expires_at=_MAX_UTC_TIMESTAMP,
        capability_status="DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
        maximum_incremental_spend=Money(minor_units=0, currency="USD"),
        artifact_policy=artifact_policy,
    )


def _create_result_data(
    receipt: dict[str, object], *, artifact_result: dict[str, object] | None = None
) -> dict[str, object]:
    """Keep the durable plan receipt visible if optional artifact work fails."""
    return receipt if artifact_result is None else {**receipt, "artifact_result": artifact_result}


def _require_receipt_fit(
    data: dict[str, object], maximum: int, *, artifact_refs: list[str] | None = None
) -> None:
    if not _fits(
        _PREFIX + " create",
        data,
        maximum,
        outcome=_CREATE_OUTCOME,
        artifact_refs=artifact_refs or [],
    ):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The required Meta campaign PAUSE plan receipt exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Increase saved max_output_bytes before planning; no Meta read or plan retention "
                "was performed."
            ),
        )


def _artifact_source_matches_receipt(
    receipt: object,
    source: MetaCampaignPausePlanArtifactProvenance,
) -> None:
    """Bind optional files to the exact durable receipt without serializing source facts."""
    try:
        if type(receipt) is not dict:
            raise ValueError("receipt")
        expected = {
            "record_digest": receipt["record_digest"],
            "action_digest": receipt["action_digest"],
            "plan_digest": receipt["plan_digest"],
        }
        if any(
            type(value) is not str or _DIGEST.fullmatch(value) is None
            for value in expected.values()
        ):
            raise ValueError("receipt digest")
        actual = {
            "record_digest": source.record_digest,
            "action_digest": source.action_digest,
            "plan_digest": source.plan_digest,
        }
        if actual != expected:
            raise ValueError("source mismatch")
    except Exception:
        raise AdhelmError(
            "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID",
            "The retained Meta campaign PAUSE source cannot be rendered safely.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _read_request(input_file: Path) -> MetaCampaignPausePlanRequest:
    from adhelm.meta_pause_workflow import MetaCampaignPausePlanRequest

    return validate_json_file(MetaCampaignPausePlanRequest, input_file)


def _receipt_retention_unverified() -> AdhelmError:
    return AdhelmError(
        "META_PAUSE_PLAN_RECEIPT_UNVERIFIED",
        "The Meta campaign PAUSE plan may be retained, but its receipt could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect history verify and plan meta-campaign-pause catalog before planning again; "
            "do not repeat an ambiguous plan preparation."
        ),
    )


def _receipt_from_retained(
    ledger: Ledger,
    value: object,
    *,
    artifact_policy: _MetaCampaignPauseArtifactPolicyNotice,
) -> dict[str, object]:
    """Reload and bind a compact receipt to the exact retained local record."""
    try:
        from adhelm.meta_pause_workflow import RetainedMetaCampaignPausePlan

        if type(value) is not RetainedMetaCampaignPausePlan:
            raise ValueError("retained result")
        returned = value
        record_digest = _require_digest(returned.plan_record_digest)
        returned_record = meta_campaign_pause_plan_record(returned.action)
        stored_action, provenance = load_meta_campaign_pause_plan(ledger, record_digest)
        record = meta_campaign_pause_plan_record(stored_action)
        if (
            type(provenance) is not dict
            or provenance.get("record_digest") != record_digest
            or provenance.get("action_digest") != record.action_digest
            or provenance.get("plan_digest") != record.plan_digest
            or canonical_bytes(returned_record.action) != canonical_bytes(record.action)
        ):
            raise ValueError("retained source")
        return _receipt(
            record_digest=record_digest,
            action_digest=record.action_digest,
            plan_digest=record.plan_digest,
            subplan_digests=record.action.plan.subplan_digests,
            expires_at=record.expires_at,
            capability_status=record.action.status,
            maximum_incremental_spend=Money(
                minor_units=record.maximum_incremental_spend.minor_units,
                currency=record.maximum_incremental_spend.currency,
            ),
            artifact_policy=artifact_policy,
        )
    except OSError:
        raise _receipt_retention_unverified() from None
    except Exception:
        raise AdhelmError(
            "META_PAUSE_PLAN_RESULT_INVALID",
            "Retained Meta campaign PAUSE plan result is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _review_data(review: object, provenance: object) -> dict[str, object]:
    """Close all loaded history before it reaches an agent-visible result."""
    try:
        if type(review) is not MetaCampaignPausePlanReview or type(provenance) is not dict:
            raise ValueError("review")
        review_data = review.model_dump(mode="json", warnings="error")
        closed_provenance = validate_json_value(
            _MetaCampaignPausePlanProvenance,
            cast(dict[str, Any], provenance),
        ).model_dump(mode="json", warnings="error")
        return {
            "review": review_data,
            "review_digest": review.review_digest,
            "provenance": closed_provenance,
            "historical_review": True,
            "execution_authority": False,
            "remote_mutations": False,
            "review_authorizes_execution": False,
        }
    except Exception:
        raise AdhelmError(
            "INVALID_META_PAUSE_PLAN_REVIEW",
            "Meta campaign PAUSE plan review is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _historical_review(
    record: object,
    *,
    opaque_key: bytes,
    reviewed_at: datetime,
) -> MetaCampaignPausePlanReview:
    """Use M40's fixed complete-review bound, then apply the CLI's saved cap.

    M40 treats a caller-supplied direct-view bound as input.  The installed
    CLI instead obtains its lower bound from saved configuration, so its
    inability to emit the complete view is a failed precondition.
    """
    try:
        return review_meta_campaign_pause_plan(
            record,
            opaque_key=opaque_key,
            reviewed_at=reviewed_at,
            max_output_bytes=_M40_REVIEW_OUTPUT_MAXIMUM_BYTES,
        )
    except AdhelmError as error:
        if error.code != "OUTPUT_LIMIT_EXCEEDED":
            raise
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "Complete Meta campaign PAUSE plan review exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Increase saved max_output_bytes to review every historical limitation.",
        ) from None


def _closed_catalog(value: object) -> dict[str, Any]:
    """Validate history data including its fixed no-authority controls."""
    try:
        if type(value) is not dict:
            raise ValueError("catalog")
        return validate_json_value(
            _MetaCampaignPauseCatalog,
            cast(dict[str, Any], value),
        ).model_dump(mode="json", warnings="error")
    except Exception:
        raise AdhelmError(
            "INVALID_META_PAUSE_PLAN_CATALOG",
            "Meta campaign PAUSE plan catalog is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


@app.command("create")
def create(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    no_artifacts: Annotated[bool, typer.Option("--no-artifacts")] = False,
    artifact_format: Annotated[list[str] | None, typer.Option("--artifact-format")] = None,
    artifact_output: Annotated[Path | None, typer.Option("--artifact-output")] = None,
) -> None:
    """Read one Meta baseline and retain a non-executable configuration-only pause plan."""
    from adhelm.cli import emit, guarded, root

    command = _PREFIX + " create"

    def work() -> None:
        from adhelm.meta_pause_artifact_cli import (
            _load_source,
            _require_same_source,
            _source_snapshot,
        )
        from adhelm.meta_pause_plan_artifacts import (
            meta_campaign_pause_plan_artifact_failure_preview,
            meta_campaign_pause_plan_artifact_failure_result,
            preflight_meta_campaign_pause_plan_artifact_output,
            render_meta_campaign_pause_plan_artifacts,
        )

        # Strict regular-file parsing completes before configuration, output
        # policy, a token session, transport, or durable plan retention.
        request = _read_request(input_file)
        directory = root(ctx)
        configuration = load_configuration(directory)
        override = _task_artifact_override(no_artifacts, artifact_format, artifact_output)
        decision = _preflight_artifact_policy(
            configuration.output,
            profile_id=request.profile_id,
            task_override=override,
        )
        artifact_policy = _artifact_policy_notice(decision)
        artifact_preview: dict[str, object] | None = None
        artifact_refs: list[str] = []
        if decision.outcome == "GENERATE":
            artifact_preview, artifact_refs = meta_campaign_pause_plan_artifact_failure_preview(
                decision,
                source_record_digest="0" * 64,
                checkpoint_digest="0" * 64,
            )
        # Reserve a full durable receipt plus the longest allowed partial
        # artifact result before any Meta session, HTTP request, key, or file.
        _require_receipt_fit(
            _create_result_data(_receipt_probe(artifact_policy), artifact_result=artifact_preview),
            configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        from adhelm.meta_pause_workflow import plan_configured_meta_campaign_pause

        retained = plan_configured_meta_campaign_pause(
            directory,
            request,
            expected_configuration_digest=digest(configuration),
        )
        try:
            receipt = _receipt_from_retained(
                Ledger(directory),
                retained,
                artifact_policy=artifact_policy,
            )
        except OSError:
            # The workflow may already have appended the plan. A failure opening
            # or rereading history remains inspect-before-repeat.
            raise _receipt_retention_unverified() from None

        artifact_result: dict[str, object] | None = None
        artifact_refs = []
        artifact_failure = False
        if decision.outcome == "GENERATE":
            source_checkpoint_digest: str | None = None
            try:
                action, provenance, source = _load_source(
                    directory, _require_digest(receipt["record_digest"])
                )
                _artifact_source_matches_receipt(receipt, source)
                first_snapshot = _source_snapshot(action, provenance, source)
                source_checkpoint_digest = source.checkpoint_digest
                # This complete-document synthetic-key preflight occurs after a
                # durable source reload, before the real key or writer.
                preflight_meta_campaign_pause_plan_artifact_output(action, provenance, decision)
                try:
                    final_action, final_provenance, final_source = _load_source(
                        directory, _require_digest(receipt["record_digest"])
                    )
                    _require_same_source(
                        first_snapshot,
                        _source_snapshot(final_action, final_provenance, final_source),
                    )
                    _artifact_source_matches_receipt(receipt, final_source)
                except (AdhelmError, AttributeError, OSError, TypeError, ValueError):
                    # The earlier checkpoint was sufficient for the zero-key
                    # preflight, but no longer describes the source that would
                    # have reached a writer after a failed final comparison.
                    source_checkpoint_digest = None
                    raise
                rendered = render_meta_campaign_pause_plan_artifacts(
                    final_action,
                    final_provenance,
                    decision,
                    key=export_key(),
                )
            except AdhelmError as error:
                rendered = meta_campaign_pause_plan_artifact_failure_result(
                    decision,
                    source_record_digest=_require_digest(receipt["record_digest"]),
                    checkpoint_digest=source_checkpoint_digest,
                    failure_code=error.code,
                )
            except (AttributeError, OSError, TypeError, ValueError):
                rendered = meta_campaign_pause_plan_artifact_failure_result(
                    decision,
                    source_record_digest=_require_digest(receipt["record_digest"]),
                    checkpoint_digest=source_checkpoint_digest,
                    failure_code="META_PAUSE_PLAN_ARTIFACT_SOURCE_UNVERIFIED",
                )
            artifact_result = rendered.to_data()
            artifact_refs = [
                "artifact:sha256:" + item.artifact_digest for item in rendered.artifacts
            ]
            artifact_failure = rendered.generation_failure_code is not None

        emit(
            command,
            _create_result_data(receipt, artifact_result=artifact_result),
            outcome="PARTIAL" if artifact_failure else _CREATE_OUTCOME,
            maximum_bytes=configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        if artifact_failure:
            raise typer.Exit(ExitCode.PARTIAL)

    guarded(command, work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List bounded retained Meta PAUSE plan metadata without keys or provider reads."""
    from adhelm.cli import emit, guarded, root

    command = _PREFIX + " catalog"

    def work() -> None:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_META_PAUSE_PLAN_SELECTION",
                "Meta campaign PAUSE plan pagination is invalid.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        directory = root(ctx)
        preferences = load_configuration(directory).output
        data = _closed_catalog(
            meta_campaign_pause_plan_catalog(
                Ledger(directory),
                cursor=cursor,
                limit=min(limit, preferences.max_rows),
            )
        )
        while not _fits(
            command,
            data,
            preferences.max_output_bytes,
            outcome=_HISTORY_OUTCOME,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            plans = data["plans"]
            if type(plans) is not list or len(plans) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Meta campaign PAUSE plan catalog exceeds the saved output bound.",
                    exit_code=ExitCode.PRECONDITION,
                    next_action="Increase saved max_output_bytes to review this catalog.",
                )
            plans.pop()
            data["next_cursor"] = str(cursor + len(plans))
        emit(
            command,
            data,
            outcome=_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(command, work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str) -> None:
    """Show one redacted historical Meta PAUSE plan; it never proves current delivery."""
    from adhelm.cli import emit, guarded, root

    command = _PREFIX + " show"

    def work() -> None:
        selected = _require_digest(record_digest)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        action, provenance = load_meta_campaign_pause_plan(Ledger(directory), selected)
        reviewed_at = _reviewed_at()
        record = meta_campaign_pause_plan_record(action)
        probe = _historical_review(
            record,
            opaque_key=b"\0" * 32,
            reviewed_at=reviewed_at,
        )
        probe_data = _review_data(probe, provenance)
        if not _fits(command, probe_data, preferences.max_output_bytes, outcome=_HISTORY_OUTCOME):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Complete Meta campaign PAUSE plan review exceeds the saved output bound.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Increase saved max_output_bytes to review every historical limitation."
                ),
            )
        review = _historical_review(
            record,
            opaque_key=export_key(),
            reviewed_at=reviewed_at,
        )
        emit(
            command,
            _review_data(review, provenance),
            outcome=_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(command, work)


__all__ = ["app"]

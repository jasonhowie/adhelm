"""Closed CLI orchestration for static retained Google Ads campaign PAUSE artifacts.

This module accepts only an exact retained-record digest and a closed local
artifact request. It never accepts a plan action, provider response, credential,
challenge, receipt, or generic document payload from the caller.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import Field, field_validator

from adhelm.artifacts import ArtifactDecision, ArtifactFormat
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_plan_artifacts import GoogleAdsCampaignPausePlanArtifactProvenance
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.models import StrictModel
from adhelm.providers.google_ads_pause_contract import GoogleAdsCampaignPausePlannedAction
from adhelm.saved_google_ads_pause_plans import load_google_ads_campaign_pause_plan

_COMMAND = "artifacts google-ads-campaign-pause"
_OUTCOME = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_ARTIFACT_NO_EXECUTION_AUTHORITY"
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)


class GoogleAdsCampaignPausePlanArtifactRequest(StrictModel):
    """One explicit request for static local history from one retained plan."""

    schema_version: Literal[1] = 1
    formats: tuple[ArtifactFormat, ...] = Field(min_length=1, max_length=3)
    output_directory: str | None = Field(default=None, max_length=4096)
    replacement_authorized: bool | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("formats")
    @classmethod
    def closed_formats(cls, value: tuple[ArtifactFormat, ...]) -> tuple[ArtifactFormat, ...]:
        """Reject aliases before configuration or retained-history access."""
        if (
            type(value) is not tuple
            or not 1 <= len(value) <= 3
            or any(type(format_name) is not str for format_name in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("formats must be a unique nonempty closed set")
        return tuple(sorted(value))


def _source_error() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID",
        "The retained Google Ads campaign PAUSE source cannot be rendered safely.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION",
            "An exact retained Google Ads campaign PAUSE record digest is required.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _load_source(
    directory: Path,
    record_digest: str,
) -> tuple[
    GoogleAdsCampaignPausePlannedAction,
    dict[str, Any],
    GoogleAdsCampaignPausePlanArtifactProvenance,
]:
    """Load one closed retained source and renderer-owned provenance shape."""
    try:
        from adhelm.google_ads_pause_plan_artifacts import (
            revalidate_google_ads_campaign_pause_plan_artifact_source,
        )

        action, provenance = load_google_ads_campaign_pause_plan(Ledger(directory), record_digest)
        checked, source = revalidate_google_ads_campaign_pause_plan_artifact_source(
            action, provenance
        )
        if source.record_digest != record_digest:
            raise ValueError("selected record")
        return checked, provenance, source
    except AdhelmError:
        raise
    except Exception:
        raise _source_error() from None


def _source_snapshot(
    action: GoogleAdsCampaignPausePlannedAction,
    provenance: dict[str, Any],
    source: GoogleAdsCampaignPausePlanArtifactProvenance,
) -> bytes:
    """Make a closed local comparison snapshot without exposing it externally."""
    try:
        action_data = action.model_dump(mode="json", warnings="error")
        source_data = source.model_dump(mode="json", warnings="error")
        if type(action_data) is not dict or type(source_data) is not dict:
            raise ValueError("source")
        return canonical_bytes(
            {
                "action": action_data,
                "loader_provenance": provenance,
                "artifact_provenance": source_data,
            }
        )
    except Exception:
        raise _source_error() from None


def _require_same_source(before: bytes, after: bytes) -> None:
    if before != after:
        raise _source_error()


def _summary(
    source: GoogleAdsCampaignPausePlanArtifactProvenance,
    artifact_result: dict[str, object],
) -> dict[str, object]:
    """Return only allowlisted historical references beside artifact metadata."""
    try:
        record_digest = source.record_digest
        action_digest = source.action_digest
        plan_digest = source.plan_digest
        checkpoint_digest = source.checkpoint_digest
        historical_evidence_status = source.historical_evidence_status
        execution_authority = source.execution_authority
        requires_current_kernel_checks = source.requires_current_kernel_checks
        if (
            not all(
                type(value) is str and _DIGEST.fullmatch(value) is not None
                for value in (record_digest, action_digest, plan_digest, checkpoint_digest)
            )
            or type(historical_evidence_status) is not str
            or type(execution_authority) is not bool
            or execution_authority is not False
            or type(requires_current_kernel_checks) is not bool
            or requires_current_kernel_checks is not True
        ):
            raise ValueError("source")
        return {
            "source_record_digest": record_digest,
            "action_digest": action_digest,
            "plan_digest": plan_digest,
            "checkpoint_digest": checkpoint_digest,
            "historical_evidence_status": historical_evidence_status,
            "historical_static_artifact": True,
            "execution_authority": execution_authority,
            "remote_mutations": False,
            "opening_or_editing_authorizes_execution": False,
            "requires_current_kernel_checks": requires_current_kernel_checks,
            "artifact_result": artifact_result,
        }
    except Exception:
        raise _source_error() from None


def google_ads_campaign_pause(
    ctx: typer.Context,
    record_digest: str,
    input_file: Annotated[Path, typer.Option("--input")],
) -> None:
    """Write requested static history for one retained Google Ads PAUSE plan."""
    from adhelm.cli import emit, guarded, root
    from adhelm.credentials import export_key
    from adhelm.google_ads_pause_plan_artifacts import (
        explicit_google_ads_campaign_pause_plan_artifact_decision,
        google_ads_campaign_pause_plan_artifact_failure_preview,
        preflight_google_ads_campaign_pause_plan_artifact_output,
        render_google_ads_campaign_pause_plan_artifacts,
    )

    def work() -> None:
        # The regular-file boundary completes before configuration, retained
        # history, a sharing key, or any output destination is reached.
        request = validate_json_file(GoogleAdsCampaignPausePlanArtifactRequest, input_file)
        selected = _require_digest(record_digest)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        action, provenance, source = _load_source(directory, selected)
        snapshot = _source_snapshot(action, provenance, source)
        profile_id = action.execution_binding.profile_ref
        decision = explicit_google_ads_campaign_pause_plan_artifact_decision(
            preferences,
            profile_id=profile_id,
            formats=request.formats,
            output_directory=request.output_directory,
            replacement_authorized=request.replacement_authorized,
        )
        if (
            type(decision) is not ArtifactDecision
            or decision.outcome != "GENERATE"
            or decision.output_directory is None
        ):
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                "The selected Google Ads campaign PAUSE artifact request needs a complete local "
                "output policy.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Select supported formats and an existing local output directory.",
            )
        # Build every complete redacted document with the renderer's synthetic
        # key before a real sharing key or any writer can be reached.
        preflight_google_ads_campaign_pause_plan_artifact_output(action, provenance, decision)
        preview, artifact_refs = google_ads_campaign_pause_plan_artifact_failure_preview(
            decision,
            source_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
        )
        summary = _summary(source, preview)
        if not _fits(
            _COMMAND,
            summary,
            preferences.max_output_bytes,
            outcome=_OUTCOME,
            artifact_refs=artifact_refs,
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Google Ads campaign PAUSE artifact references exceed the saved output bound.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Increase max_output_bytes or request fewer formats before generation.",
            )
        # Reload immediately before the real key/writer handoff. A changed
        # record or checkpoint is not an artifact source and must not publish.
        final_action, final_provenance, final_source = _load_source(directory, selected)
        _require_same_source(
            snapshot,
            _source_snapshot(final_action, final_provenance, final_source),
        )
        result = render_google_ads_campaign_pause_plan_artifacts(
            final_action,
            final_provenance,
            decision,
            key=export_key(),
        )
        result_data = result.to_data()
        emit(
            _COMMAND,
            _summary(final_source, result_data),
            outcome="PARTIAL" if result.generation_failure_code is not None else _OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            artifact_refs=["artifact:sha256:" + item.artifact_digest for item in result.artifacts],
        )
        if result.generation_failure_code is not None:
            raise typer.Exit(ExitCode.PARTIAL)

    guarded(_COMMAND, work)


def register_commands(target: typer.Typer) -> None:
    """Install the one explicit command into the existing artifacts group."""
    target.command("google-ads-campaign-pause")(google_ads_campaign_pause)


__all__ = ["GoogleAdsCampaignPausePlanArtifactRequest", "register_commands"]

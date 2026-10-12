"""Typed local artifact commands; no provider requests or operational authority."""

from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import Field, field_validator

from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.artifacts import (
    ArtifactContext,
    ArtifactDecision,
    ArtifactFormat,
    ArtifactGenerationError,
    ArtifactTaskOverride,
    Purpose,
    RetainedArtifactInput,
    Stage,
    Workflow,
    generate_artifacts,
    resolve_artifact_policy,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration, update_configuration
from adhelm.errors import AdhelmError
from adhelm.input_files import validate_json_file
from adhelm.models import (
    Configuration,
    OutputPreferences,
    Provider,
    ResultEnvelope,
    SafeRef,
    StrictModel,
)

app = typer.Typer(no_args_is_help=True)


class ArtifactPolicyQuery(StrictModel):
    workflow: Workflow
    stage: Stage
    purpose: Purpose
    provider: Provider
    profile_id: SafeRef | None = None


class ArtifactRenderRequest(StrictModel):
    schema_version: Literal[1] = 1
    formats: tuple[ArtifactFormat, ...] = Field(default_factory=tuple, max_length=3)
    output_directory: str | None = Field(default=None, max_length=4096)
    purpose: Literal["result", "comparison"] = "result"
    replacement_authorized: bool | None = None


class PausePlanArtifactRequest(StrictModel):
    """One explicit request for a static artifact of retained pause-plan history."""

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


class Ga4KeyEventCreatePlanArtifactRequest(StrictModel):
    """One explicit static-artifact request for a retained GA4 CREATE plan."""

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


def _decision_data(decision: ArtifactDecision) -> dict[str, object]:
    return {
        "outcome": decision.outcome,
        "reason": decision.reason,
        "formats": list(decision.formats),
        "output_directory": str(decision.output_directory) if decision.output_directory else None,
        "preferences_digest": decision.preferences_digest,
        "reuse_allowed": decision.reuse_allowed,
        "replacement_authorized": decision.replacement_authorized,
        "max_rows": decision.max_rows,
        "max_output_bytes": decision.max_output_bytes,
        "detail": decision.detail,
    }


@app.command("configure")
def configure(ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Save output preferences; these authorize no data access or provider changes."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        output = validate_json_file(OutputPreferences, input_file)
        update_configuration(
            root(ctx),
            lambda configuration: Configuration.model_validate(
                {**configuration.model_dump(), "output": output}
            ),
        )
        emit(
            "artifacts configure",
            {
                "preferences_digest": digest(output),
                "artifact_mode": output.artifact_mode,
                "scheduling_authorized": False,
                "provider_operations_authorized": False,
            },
        )

    guarded("artifacts configure", work)


@app.command("policy")
def policy(ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Inspect the saved decision for a workflow without creating optional files."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        query = validate_json_file(ArtifactPolicyQuery, input_file)
        configuration = load_configuration(root(ctx))
        if query.profile_id is not None and not any(
            profile.profile_id == query.profile_id and profile.provider == query.provider
            for profile in configuration.profiles
        ):
            raise AdhelmError(
                "ARTIFACT_PROFILE_MISMATCH", "Artifact rule context requires a matching profile."
            )
        decision = resolve_artifact_policy(
            ArtifactContext(**query.model_dump()), configuration.output
        )
        emit(
            "artifacts policy",
            _decision_data(decision),
            outcome=decision.outcome,
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded("artifacts policy", work)


@app.command("report")
def report(
    ctx: typer.Context,
    record_digest: str,
    input_file: Annotated[Path, typer.Option("--input")],
) -> None:
    """Create explicitly requested artifacts from one verified retained report."""
    from adhelm.cli import emit, guarded, root
    from adhelm.credentials import export_key
    from adhelm.ledger import Ledger
    from adhelm.saved_reports import load_report

    def work() -> None:
        request = validate_json_file(ArtifactRenderRequest, input_file)
        configuration = load_configuration(root(ctx))
        retained, provenance = load_report(Ledger(root(ctx)), record_digest)
        decision = resolve_artifact_policy(
            ArtifactContext("report", "completion", request.purpose, retained.envelope.provider),
            configuration.output,
            task_override=ArtifactTaskOverride(
                requested=True,
                formats=request.formats,
                output_directory=request.output_directory,
                purpose=request.purpose,
                replacement_authorized=request.replacement_authorized,
            ),
        )
        if decision.outcome != "GENERATE" or decision.output_directory is None:
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                decision.reason,
                next_action="Select a supported format and an existing local output directory.",
            )
        # Bound the result before file creation, including the longest supported version suffix.
        from adhelm import __version__

        summary: dict[str, object] = {
            "source_record_digest": record_digest,
            "checkpoint_digest": provenance["checkpoint_digest"],
            "effective_policy": _decision_data(decision),
        }
        preview = {
            **summary,
            # Reserve the fields emitted by a post-publication partial failure
            # before any file is created, so a bounded CLI result cannot erase
            # a completed artifact reference.
            # ArtifactGenerationError accepts at most 80 ASCII characters.  Use
            # that full bound and the actual partial outcome, rather than one
            # particular failure code, so post-publication metadata can never
            # be erased by a later output-limit failure.
            "artifact_failure_code": "X" * 80,
            "retained_source_unchanged": True,
            "artifacts": [
                {
                    "path": str(
                        decision.output_directory
                        / ("artifact-" + "f" * 16 + "-" + "f" * 16 + ".v9999." + format_name)
                    ),
                    "format": format_name,
                    "reused": False,
                    "artifact_digest": "f" * 64,
                }
                for format_name in decision.formats
            ],
        }
        if (
            len(
                canonical_bytes(
                    ResultEnvelope(
                        tool_version=__version__,
                        command="artifacts report",
                        outcome="PARTIAL",
                        data=preview,
                    )
                )
            )
            > configuration.output.max_output_bytes
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Artifact references would exceed the configured output-size bound.",
                next_action="Raise max_output_bytes or request fewer formats before generation.",
            )
        failure: ArtifactGenerationError | None = None
        try:
            metadata = generate_artifacts(
                RetainedArtifactInput(retained, record_digest, provenance["checkpoint_digest"]),
                decision,
                key=export_key(),
            )
        except ArtifactGenerationError as exc:
            failure = exc
            metadata = exc.completed
            summary["artifact_failure_code"] = exc.failure_code
            summary["retained_source_unchanged"] = True
        emit(
            "artifacts report",
            {
                **summary,
                "artifacts": [
                    {
                        "path": str(item.path),
                        "format": item.format,
                        "reused": item.reused,
                        "artifact_digest": item.artifact_digest,
                    }
                    for item in metadata
                ],
            },
            outcome="PARTIAL" if failure else "OK",
            maximum_bytes=configuration.output.max_output_bytes,
        )
        if failure:
            raise typer.Exit(failure.exit_code)

    guarded("artifacts report", work)


@app.command("plan")
def plan(
    ctx: typer.Context,
    record_digest: str,
    input_file: Annotated[Path, typer.Option("--input")],
) -> None:
    """Create a static redacted artifact from one verified retained pause plan."""
    from adhelm.cli import emit, guarded, root
    from adhelm.credentials import export_key
    from adhelm.ledger import Ledger
    from adhelm.pause_plan_artifacts import (
        explicit_openai_pause_plan_artifact_decision,
        pause_plan_artifact_failure_preview,
        render_openai_pause_plan_artifacts,
        revalidate_openai_pause_plan_artifact_source,
    )
    from adhelm.saved_plans import load_openai_pause_plan

    def work() -> None:
        request = validate_json_file(PausePlanArtifactRequest, input_file)
        directory = root(ctx)
        configuration = load_configuration(directory)
        action, provenance = load_openai_pause_plan(Ledger(directory), record_digest)
        action, source = revalidate_openai_pause_plan_artifact_source(action, provenance)
        if source.record_digest != record_digest:
            raise AdhelmError(
                "PLAN_ARTIFACT_SOURCE_INVALID",
                "Verified pause-plan artifact source does not match the selected record.",
                exit_code=2,
            )
        decision = explicit_openai_pause_plan_artifact_decision(
            configuration.output,
            profile_id=action.binding.profile_ref,
            formats=request.formats,
            output_directory=request.output_directory,
            replacement_authorized=request.replacement_authorized,
        )
        if decision.outcome != "GENERATE" or decision.output_directory is None:
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                decision.reason,
                next_action="Select a supported format and an existing local output directory.",
            )
        preview_result, artifact_refs = pause_plan_artifact_failure_preview(
            decision,
            source_record_digest=record_digest,
            checkpoint_digest=source.checkpoint_digest,
        )
        summary: dict[str, object] = {
            "source_record_digest": record_digest,
            "action_digest": source.action_digest,
            "plan_digest": source.plan_digest,
            "historical_evidence_status": source.historical_evidence_status,
            "execution_authority": False,
            "remote_mutations": False,
            "opening_or_editing_authorizes_execution": False,
            "artifact_result": preview_result,
        }
        from adhelm import __version__
        from adhelm.models import ResultEnvelope

        if (
            len(
                canonical_bytes(
                    ResultEnvelope(
                        tool_version=__version__,
                        command="artifacts plan",
                        outcome="PLAN_REVIEW_NO_EXECUTION_AUTHORITY",
                        data=summary,
                        artifact_refs=artifact_refs,
                    )
                )
            )
            + 1
            > configuration.output.max_output_bytes
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Pause-plan artifact references would exceed the configured output-size bound.",
                next_action="Raise max_output_bytes or request fewer formats before generation.",
            )
        result = render_openai_pause_plan_artifacts(
            action,
            source.model_dump(mode="json"),
            decision,
            key=export_key(),
        )
        data = {
            **summary,
            "artifact_result": result.to_data(),
        }
        refs = ["artifact:sha256:" + item.artifact_digest for item in result.artifacts]
        outcome = (
            "PARTIAL"
            if result.generation_failure_code is not None
            else "PLAN_REVIEW_NO_EXECUTION_AUTHORITY"
        )
        emit(
            "artifacts plan",
            data,
            outcome=outcome,
            maximum_bytes=configuration.output.max_output_bytes,
            artifact_refs=refs,
        )
        if result.generation_failure_code is not None:
            raise typer.Exit(5)

    guarded("artifacts plan", work)


@app.command("measurement-comparison")
def measurement_comparison(
    ctx: typer.Context,
    before_record_digest: str,
    after_record_digest: str,
    input_file: Annotated[Path, typer.Option("--input")],
) -> None:
    """Create optional static artifacts from a verified retained observation pair."""
    from adhelm.cli import emit, guarded, root
    from adhelm.credentials import export_key
    from adhelm.ledger import Ledger
    from adhelm.measurement_cli import _fits
    from adhelm.measurement_comparison import compare_measurements
    from adhelm.measurement_comparison_artifacts import (
        MeasurementComparisonArtifactRequest,
        explicit_measurement_comparison_artifact_decision,
        measurement_comparison_artifact_failure_preview,
        preflight_measurement_comparison_artifact_output,
        render_measurement_comparison_artifacts,
    )

    command = "artifacts measurement-comparison"
    outcome = "MEASUREMENT_COMPARISON_ARTIFACT_NO_AUTHORITY"

    def work() -> None:
        request = validate_json_file(MeasurementComparisonArtifactRequest, input_file)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        ledger = Ledger(directory)
        comparison = compare_measurements(ledger, before_record_digest, after_record_digest)
        decision = explicit_measurement_comparison_artifact_decision(
            preferences,
            provider=comparison.scope.provider,
            profile_id=comparison.before.profile_ref,
            formats=request.formats,
            output_directory=request.output_directory,
            replacement_authorized=request.replacement_authorized,
        )
        if decision.outcome != "GENERATE" or decision.output_directory is None:
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                decision.reason,
                next_action="Select a supported format and an existing local output directory.",
            )
        comparison = preflight_measurement_comparison_artifact_output(ledger, comparison, decision)
        preview_result, artifact_refs = measurement_comparison_artifact_failure_preview(
            decision,
            before_record_digest=before_record_digest,
            after_record_digest=after_record_digest,
            checkpoint_digest=comparison.checkpoint_digest,
            comparison_digest=comparison.comparison_digest,
        )
        summary: dict[str, object] = {
            "before_record_digest": before_record_digest,
            "after_record_digest": after_record_digest,
            "comparison_digest": comparison.comparison_digest,
            "checkpoint_digest": comparison.checkpoint_digest,
            "execution_authority": False,
            "remote_mutations": False,
            "opening_or_editing_authorizes_execution": False,
            "artifact_result": preview_result,
        }
        if not _fits(
            command,
            summary,
            preferences.max_output_bytes,
            outcome=outcome,
            artifact_refs=artifact_refs,
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Measurement comparison artifact references exceed the saved output bound.",
                next_action="Increase max_output_bytes or request fewer formats before generation.",
            )
        result = render_measurement_comparison_artifacts(
            ledger, comparison, decision, key=export_key()
        )
        emit(
            command,
            {**summary, "artifact_result": result.to_data()},
            outcome="PARTIAL" if result.generation_failure_code is not None else outcome,
            maximum_bytes=preferences.max_output_bytes,
            artifact_refs=["artifact:sha256:" + item.artifact_digest for item in result.artifacts],
        )
        if result.generation_failure_code is not None:
            raise typer.Exit(5)

    guarded(command, work)


@app.command("ga4-key-event-plan")
def ga4_key_event_plan(
    ctx: typer.Context,
    record_digest: str,
    input_file: Annotated[Path, typer.Option("--input")],
) -> None:
    """Create explicit static artifacts from one verified retained GA4 CREATE plan."""
    from adhelm.cli import emit, guarded, root
    from adhelm.credentials import export_key
    from adhelm.ga4_create_plan_artifacts import (
        explicit_ga4_key_event_create_plan_artifact_decision,
        ga4_create_plan_artifact_failure_preview,
        preflight_ga4_key_event_create_plan_artifact_output,
        render_ga4_key_event_create_plan_artifacts,
        revalidate_ga4_key_event_create_plan_artifact_source,
    )
    from adhelm.ledger import Ledger
    from adhelm.measurement_cli import _fits
    from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan

    command = "artifacts ga4-key-event-plan"
    outcome = "GA4_KEY_EVENT_CREATE_PLAN_ARTIFACT_NO_EXECUTION_AUTHORITY"

    def work() -> None:
        request = validate_json_file(Ga4KeyEventCreatePlanArtifactRequest, input_file)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        action, provenance = load_ga4_key_event_create_plan(Ledger(directory), record_digest)
        action, source = revalidate_ga4_key_event_create_plan_artifact_source(action, provenance)
        if source.record_digest != record_digest:
            raise AdhelmError(
                "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID",
                "Verified GA4 CREATE plan artifact source does not match the selected record.",
                exit_code=2,
            )
        decision = explicit_ga4_key_event_create_plan_artifact_decision(
            preferences,
            profile_id=action.execution_binding.auth_profile_ref,
            formats=request.formats,
            output_directory=request.output_directory,
            replacement_authorized=request.replacement_authorized,
        )
        if decision.outcome != "GENERATE" or decision.output_directory is None:
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                decision.reason,
                next_action="Select a supported format and an existing local output directory.",
            )
        # Render each complete document with a fixed-width synthetic HMAC key
        # before the real sharing key or a directory writer is reached.
        preflight_ga4_key_event_create_plan_artifact_output(action, provenance, decision)
        preview_result, artifact_refs = ga4_create_plan_artifact_failure_preview(
            decision,
            source_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
        )
        summary: dict[str, object] = {
            "source_record_digest": source.record_digest,
            "action_digest": source.action_digest,
            "plan_digest": source.plan_digest,
            "checkpoint_digest": source.checkpoint_digest,
            "historical_evidence_status": source.historical_evidence_status,
            "execution_authority": False,
            "remote_mutations": False,
            "opening_or_editing_authorizes_execution": False,
            "execution_command": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
            "artifact_result": preview_result,
        }
        if not _fits(
            command,
            summary,
            preferences.max_output_bytes,
            outcome=outcome,
            artifact_refs=artifact_refs,
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "GA4 CREATE plan artifact references exceed the saved output bound.",
                next_action="Increase max_output_bytes or request fewer formats before generation.",
            )
        result = render_ga4_key_event_create_plan_artifacts(
            action, provenance, decision, key=export_key()
        )
        emit(
            command,
            {**summary, "artifact_result": result.to_data()},
            outcome="PARTIAL" if result.generation_failure_code is not None else outcome,
            maximum_bytes=preferences.max_output_bytes,
            artifact_refs=["artifact:sha256:" + item.artifact_digest for item in result.artifacts],
        )
        if result.generation_failure_code is not None:
            raise typer.Exit(5)

    guarded(command, work)

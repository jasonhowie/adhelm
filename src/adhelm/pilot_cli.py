"""The live read pilot command: fixed read-only steps and a redacted local record."""

from pathlib import Path
from typing import Annotated, Any

import typer

from adhelm.pilot_records import PilotRecord
from adhelm.pilot_workflow import PilotRequest, PilotRun, StepResult, run_pilot

app = typer.Typer(no_args_is_help=True)


def _step_row(result: StepResult, status: str) -> dict[str, Any]:
    row: dict[str, Any] = {"step": result.step, "command": result.command, "status": status}
    if status == "FAILED":
        row.update(
            {
                "exit_code": result.exit_code,
                "outcome": result.outcome,
                "diagnostic_code": result.diagnostic_code,
                "next_action": result.next_action,
            }
        )
    return row


def _incomplete(provider: str, run: PilotRun) -> dict[str, Any]:
    assert run.failed is not None
    return {
        "provider": provider,
        "record_written": False,
        "steps": [
            *(_step_row(result, "PASSED") for result in run.completed),
            _step_row(run.failed, "FAILED"),
            *({"step": name, "status": "NOT_RUN"} for name in run.not_run),
        ],
        "next_action": (
            "Resolve the failed step, then rerun the pilot. A pilot record exists only for a "
            "complete run, so no label changed."
        ),
    }


def _recorded(record: PilotRecord, output: Path) -> dict[str, Any]:
    return {
        "provider": record.provider,
        "record_written": True,
        "record_digest": record.record_digest,
        "output": str(output.resolve()),
        "report_window": (
            record.report_window.model_dump(mode="json") if record.report_window else None
        ),
        "ui_comparison": record.ui_comparison,
        "steps": [step.model_dump(mode="json") for step in record.steps],
        "remote_mutations": False,
        "next_action": (
            "Review the record, then commit it under docs/pilots/ in a build of this exact "
            "version to label the provider LIVE_PILOTED. It holds a pseudonym and digests only."
        ),
    }


@app.command("run")
def run(
    ctx: typer.Context,
    provider: Annotated[str, typer.Option("--provider")],
    profile: Annotated[str, typer.Option("--profile")],
    ui_comparison: Annotated[str, typer.Option("--ui-comparison")],
    source_commit: Annotated[str, typer.Option("--source-commit")],
    output: Annotated[Path, typer.Option("--output")],
    admin_profile: Annotated[str | None, typer.Option("--admin-profile")] = None,
    stream_id: Annotated[str | None, typer.Option("--stream-id")] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
    report_input: Annotated[Path | None, typer.Option("--report-input")] = None,
    audit_policy: Annotated[Path | None, typer.Option("--audit-policy")] = None,
) -> None:
    """Read-only live pilot: enroll check, doctor, inventory, seven-day report, audit, verify.

    Runs the provider's fixed read commands against the account the profile names,
    takes the owner's provider-UI comparison result (MATCH or MISMATCH) as input, and
    writes a redacted record to a new file. It reads provider state and uses the
    credential store. It makes no change to any account and runs no write command.
    """
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        result = run_pilot(
            root(ctx),
            PilotRequest(
                provider=provider,
                profile=profile,
                ui_comparison=ui_comparison,
                source_commit=source_commit,
                output=output,
                admin_profile=admin_profile,
                stream_id=stream_id,
                container_id=container_id,
                report_input=report_input,
                audit_policy=audit_policy,
            ),
        )
        if result.record is None:
            emit("pilot run", _incomplete(provider, result), outcome="PILOT_INCOMPLETE")
            assert result.failed is not None
            raise typer.Exit(result.failed.exit_code)
        emit("pilot run", _recorded(result.record, output), outcome="PILOT_RECORDED")

    guarded("pilot run", work)

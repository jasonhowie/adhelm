"""Run the 0.1.0 live read pilot sequence and write its redacted record.

`docs/release-0.1.0.md` ("Live read pilot") lists what a provider pilot shows. This
workflow performs that sequence by invoking the installed read commands in order,
the same commands an agent would run, so the pilot cannot exercise a code path a
user's own run does not. It adds no provider access of its own, takes no generic
command from its caller, and runs no write: every step is a fixed read-only
command from :func:`adhelm.pilot_records.pilot_steps`.

Only the owner's own invocation of this workflow on an account they administer
produces pilot evidence. Nothing in the build session runs it live.

All local validation happens before the first step, so a malformed selection,
output path, audit policy or report input, or a profile that is not a matching READ
profile, costs no provider request and no credential read. The report input gets
exactly the checks ``report <provider>`` makes before any egress (one shared
function, :func:`adhelm.workflows.require_report_preconditions`) plus the
seven-day window, which is checked first, before those preconditions. A step that
fails stops the sequence and no record is written; a record therefore exists only for
a complete pilot. The record is built from closed fields only: each step's command,
outcome token and a digest of its result, never the result.
"""

from __future__ import annotations

import contextlib
import io
import os
import platform
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, cast

from typer import _click as click

from adhelm import __version__
from adhelm.audit import AuditPolicy
from adhelm.canonical import digest, parse_json
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.input_files import read_json_input_file, validate_json_file
from adhelm.ledger import Ledger
from adhelm.models import ConfiguredProfile
from adhelm.pilot_records import (
    RELEASE_PROVIDERS,
    PilotCoverage,
    PilotPlatform,
    PilotRecord,
    PilotRecordContent,
    PilotStep,
    PilotStepName,
    PilotUiComparison,
    PilotWindow,
    ReleaseProvider,
    pilot_record_bytes,
    pilot_steps,
    seal_pilot_record,
)
from adhelm.privacy import opaque_ref
from adhelm.saved_reports import load_report
from adhelm.workflows import ReportProvider, require_report_preconditions

UI_COMPARISONS: Final = ("MATCH", "MISMATCH")
_PLATFORMS: Final[dict[str, PilotPlatform]] = {
    "linux": "linux",
    "darwin": "macos",
    "win32": "windows",
}
_MAXIMUM_STEP_OUTPUT_BYTES: Final = 1_048_576
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")


#: The options beyond the always-required ones, in the order `discover` lists them.
_OPTIONAL_OPTIONS: Final = (
    "admin_profile",
    "stream_id",
    "container_id",
    "report_input",
    "audit_policy",
)
_PROVIDER_OPTIONS: Final[dict[str, tuple[str, ...]]] = {
    "ga4": ("admin_profile", "stream_id", "report_input", "audit_policy"),
    "gtm": ("container_id",),
}


def pilot_options(provider: str) -> tuple[str, ...]:
    """Return the optional options a provider's pilot requires; it accepts no others."""
    return _PROVIDER_OPTIONS.get(provider, ("report_input", "audit_policy"))


@dataclass(frozen=True)
class PilotRequest:
    """The selection a pilot run is told; which parts apply depends on the provider."""

    provider: str
    profile: str
    ui_comparison: str
    source_commit: str
    output: Path
    admin_profile: str | None = None
    stream_id: str | None = None
    container_id: str | None = None
    report_input: Path | None = None
    audit_policy: Path | None = None


@dataclass(frozen=True)
class StepResult:
    """What one fixed command returned, reduced to what the pilot may keep."""

    step: PilotStepName
    command: str
    exit_code: int
    outcome: str
    data: dict[str, Any]
    diagnostic_code: str | None = None
    next_action: str | None = None


@dataclass(frozen=True)
class PilotRun:
    """The completed steps, the first failure if any, and the record if complete."""

    completed: tuple[StepResult, ...]
    failed: StepResult | None
    not_run: tuple[PilotStepName, ...]
    record: PilotRecord | None


def _invalid(message: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_PILOT_SELECTION",
        message,
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Inspect `adhelm discover` for the `pilot run` options each provider needs.",
    )


def validate_request(request: PilotRequest) -> PilotPlatform:
    """Check the whole selection locally and return the platform this run is on.

    Nothing here reads a credential, the ledger, configuration or a provider.
    """
    if request.provider not in RELEASE_PROVIDERS:
        raise _invalid("The provider is not part of the 0.1.0 release.")
    if request.ui_comparison not in UI_COMPARISONS:
        raise _invalid("The provider UI comparison must be MATCH or MISMATCH.")
    if _COMMIT.fullmatch(request.source_commit) is None:
        raise _invalid("The source commit must be a full lowercase 40-character git commit.")
    applicable = pilot_options(request.provider)
    for option in _OPTIONAL_OPTIONS:
        supplied = getattr(request, option) is not None
        if supplied and option not in applicable:
            raise _invalid("The provider pilot was given an option that does not apply to it.")
        if not supplied and option in applicable:
            raise _invalid("The provider pilot is missing a required option.")
    if not request.output.parent.is_dir() or os.path.lexists(request.output):
        raise _invalid("The pilot record output must be a new file in an existing directory.")
    selected = _PLATFORMS.get(sys.platform)
    if selected is None:
        raise AdhelmError(
            "PILOT_PLATFORM_UNSUPPORTED",
            "The pilot record schema has no entry for this platform.",
            exit_code=ExitCode.PRECONDITION,
        )
    return selected


def _invalid_window() -> AdhelmError:
    return AdhelmError(
        "INVALID_PILOT_WINDOW",
        "The pilot report must cover exactly seven inclusive calendar days.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Set window_start and window_end in the report input six days apart.",
    )


def _iso_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError("A window bound is a date")


def _window(start: object, end: object) -> PilotWindow:
    """Build the pilot window from declared dates, or refuse with the window diagnostic."""
    try:
        return PilotWindow(window_start=_iso_date(start), window_end=_iso_date(end))
    except (TypeError, ValueError) as exc:
        raise _invalid_window() from exc


def _declared_window(
    request: PilotRequest, profile: ConfiguredProfile, report_input: Path
) -> PilotWindow:
    """Validate the whole report input and return its seven-day window.

    The window is checked first: input that is not JSON is refused with its own
    diagnostic, and a declared window that is not seven inclusive days is refused as
    ``INVALID_PILOT_WINDOW`` whatever else is wrong with the input; a report input that
    is not an object declares no window and is refused the same way. It must then
    be what ``report <provider>`` would accept for this profile and these options:
    :func:`adhelm.workflows.require_report_preconditions` is the one function that
    command runs before any credential read or provider request, and the pilot calls
    it before its first step so a report that would be refused costs no earlier step.
    """
    raw = read_json_input_file(report_input)
    declared = parse_json(raw)
    bounds = declared if isinstance(declared, dict) else {}
    window = _window(bounds.get("window_start"), bounds.get("window_end"))
    require_report_preconditions(
        profile,
        cast(ReportProvider, request.provider),
        raw,
        stream_id=request.stream_id,
        ci_enabled=False,
    )
    return window


def _read_profile(
    profiles: list[ConfiguredProfile], profile_id: str, provider: str
) -> ConfiguredProfile:
    """Return the named profile when it is a READ profile for the provider."""
    profile = next((item for item in profiles if item.profile_id == profile_id), None)
    if profile is None or profile.provider != provider or profile.purpose != "READ":
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching read profile is required.",
            next_action="Enroll a READ profile for the provider following its setup guide.",
        )
    return profile


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _invoke(root: Path, step: PilotStepName, command: str, arguments: list[str]) -> StepResult:
    """Run one installed command in this process and keep only its structured result."""
    from adhelm.cli import app

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            returned = app(["--data-dir", str(root), *arguments], standalone_mode=False)
    except click.ClickException:
        return StepResult(step, command, int(ExitCode.INVALID_INPUT), "REJECTED", {})
    exit_code = returned if isinstance(returned, int) else 0
    try:
        envelope = parse_json(captured.getvalue(), maximum_bytes=_MAXIMUM_STEP_OUTPUT_BYTES)
        data, diagnostics = envelope["data"], envelope["diagnostics"]
        if not isinstance(data, dict) or not isinstance(diagnostics, list):
            raise TypeError("Step output is not a result envelope")
        outcome = str(envelope["outcome"])
    except (AdhelmError, KeyError, TypeError):
        return StepResult(step, command, exit_code or int(ExitCode.INTERNAL), "UNREADABLE", {})
    first = diagnostics[0] if diagnostics and isinstance(diagnostics[0], dict) else {}
    return StepResult(
        step,
        command,
        exit_code,
        outcome,
        data,
        _text(first.get("code")),
        _text(first.get("next_action")),
    )


def _refused(result: StepResult, code: str, next_action: str) -> StepResult:
    """Fail a pilot step whose command exited cleanly but did not show what the pilot needs."""
    return StepResult(
        result.step,
        result.command,
        int(ExitCode.PRECONDITION),
        result.outcome,
        result.data,
        code,
        next_action,
    )


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _arguments(
    request: PilotRequest, step: PilotStepName, command: str, retained: dict[str, str]
) -> list[str]:
    """Build one step's argv from its planned command path and the pilot selection."""
    base = command.split()
    provider = request.provider
    if step == "INVENTORY":
        if provider == "gtm":
            assert request.container_id is not None
            return [*base, request.profile, request.container_id, "--full-inventory"]
        if provider == "ga4":
            assert request.admin_profile is not None and request.stream_id is not None
            return [*base, request.admin_profile, request.stream_id]
        return [*base, provider, request.profile]
    if step == "REPORT":
        assert request.report_input is not None
        stream = [request.stream_id] if provider == "ga4" and request.stream_id else []
        return [*base, request.profile, *stream, "--input", str(request.report_input)]
    if step == "AUDIT":
        if provider == "gtm":
            return [*base, retained["inventory"], "--section", "COVERAGE"]
        assert request.audit_policy is not None
        return [*base, retained["report"], "--input", str(request.audit_policy)]
    return base


def _coverage(provider: str, data: dict[str, Any]) -> list[PilotCoverage] | None:
    """Return the audit's whole coverage table, or ``None`` when it is absent or paged."""
    rows: object
    if provider == "gtm":
        rows = data.get("rows") if data.get("section") == "COVERAGE" else None
    else:
        sections = data.get("sections")
        rows = sections.get("coverage") if isinstance(sections, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    try:
        return [PilotCoverage(dimension=row["dimension"], status=row["status"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        return None


def _retained_for_window(root: Path, provider: str, record: str, window: PilotWindow) -> bool:
    """Whether verified local history holds this report, for this provider and window."""
    try:
        report, _ = load_report(Ledger(root), record)
    except AdhelmError:
        return False
    envelope = report.envelope
    return envelope.provider == provider and (envelope.window_start, envelope.window_end) == (
        window.window_start,
        window.window_end,
    )


def _verify(
    root: Path,
    request: PilotRequest,
    window: PilotWindow | None,
    result: StepResult,
    retained: dict[str, str],
) -> StepResult:
    """Apply the pilot's acceptance rule to a step whose command exited cleanly."""
    data = result.data
    provider = request.provider
    if result.step == "ENROLLMENT":
        needed = {"ga4", "ga4_admin"} if provider == "ga4" else {provider}
        profiles = data.get("profiles")
        configured = (
            {
                item.get("provider")
                for item in profiles
                if isinstance(item, dict) and item.get("purpose") == "READ"
            }
            if isinstance(profiles, list)
            else set()
        )
        if not needed <= configured:
            return _refused(
                result,
                "PILOT_READ_PROFILE_MISSING",
                "Enroll a READ profile for the provider following its setup guide, then rerun.",
            )
    elif result.step == "INVENTORY":
        inventory = data.get("record_digest")
        if provider == "gtm":
            complete = (
                data.get("persisted") is True
                and data.get("provider") == "gtm_web_inventory"
                and _is_digest(inventory)
            )
            if complete:
                retained["inventory"] = str(inventory)
        elif provider == "ga4":
            complete = (
                data.get("persisted") is True
                and data.get("provider") == "ga4_admin"
                and _is_digest(inventory)
            )
        else:
            complete = data.get("status") == "INVENTORY_COMPLETE" and data.get("ephemeral") is False
        if not complete:
            return _refused(
                result,
                "PILOT_INVENTORY_NOT_RETAINED",
                "The pilot needs a complete, retained inventory read; rerun after resolving why.",
            )
    elif result.step == "REPORT":
        # The window is read from the retained report in verified local history, not
        # from the displayed output: the output may be the compact form under a small
        # saved output bound, which still names the retained record.
        record = data.get("retained_record_digest")
        if (
            window is None
            or not _is_digest(record)
            or not _retained_for_window(root, provider, str(record), window)
        ):
            return _refused(
                result,
                "PILOT_REPORT_NOT_RETAINED_FOR_WINDOW",
                "The seven-day report was not retained for the declared window.",
            )
        retained["report"] = str(record)
    elif result.step == "AUDIT":
        if _coverage(provider, data) is None:
            return _refused(
                result,
                "PILOT_AUDIT_COVERAGE_UNAVAILABLE",
                "The audit returned no complete per-dimension coverage table.",
            )
    elif result.step == "HISTORY_VERIFY" and data.get("status") != "VERIFIED":
        return _refused(
            result,
            "PILOT_HISTORY_NOT_VERIFIED",
            "Local history did not verify; inspect it before rerunning the pilot.",
        )
    return result


def _named_failure(result: StepResult) -> StepResult:
    """Name the compact partial form a report takes under a small saved output bound.

    That form exits partial without a diagnostic. The report was not rejected; the
    saved bound could not show it, so the pilot says that rather than nothing.
    """
    if result.diagnostic_code is not None or result.data.get("output_limit_exceeded") is not True:
        return result
    return StepResult(
        result.step,
        result.command,
        result.exit_code,
        result.outcome,
        {},
        "OUTPUT_LIMIT_EXCEEDED",
        "Increase saved max_output_bytes so the step's result can be shown, then rerun.",
    )


def _run_steps(
    root: Path, request: PilotRequest, window: PilotWindow | None
) -> tuple[list[StepResult], StepResult | None, tuple[PilotStepName, ...]]:
    plan = pilot_steps(request.provider)
    completed: list[StepResult] = []
    retained: dict[str, str] = {}
    for index, (step, command) in enumerate(plan):
        result = _invoke(root, step, command, _arguments(request, step, command, retained))
        if result.exit_code == 0:
            result = _verify(root, request, window, result, retained)
        else:
            result = _named_failure(result)
        if result.exit_code != 0:
            return completed, result, tuple(name for name, _ in plan[index + 1 :])
        completed.append(result)
    return completed, None, ()


def run_pilot(root: Path, request: PilotRequest) -> PilotRun:
    """Validate, run the provider's fixed steps, then seal and write the record.

    An invalid selection or input raises before any step. A failing step returns the
    run without a record.
    """
    platform_name = validate_request(request)
    profiles = load_configuration(root).profiles
    profile = _read_profile(profiles, request.profile, request.provider)
    if request.admin_profile is not None:
        _read_profile(profiles, request.admin_profile, "ga4_admin")
    window = (
        _declared_window(request, profile, request.report_input)
        if request.report_input is not None
        else None
    )
    if request.audit_policy is not None:
        validate_json_file(AuditPolicy, request.audit_policy)

    completed, failed, not_run = _run_steps(root, request, window)
    if failed is not None:
        return PilotRun(tuple(completed), failed, not_run, None)

    from adhelm.credentials import export_key

    audit = next(result for result in completed if result.step == "AUDIT")
    coverage = _coverage(request.provider, audit.data)
    assert coverage is not None
    record = seal_pilot_record(
        PilotRecordContent(
            provider=cast(ReleaseProvider, request.provider),
            cli_version=__version__,
            source_commit=request.source_commit,
            platform=platform_name,
            python_version=platform.python_version(),
            pilot_date=datetime.now(UTC).date(),
            account_pseudonym=opaque_ref(export_key(), profile.account_ref),
            report_window=window,
            steps=[
                PilotStep(
                    step=result.step,
                    command=result.command,
                    outcome=result.outcome,
                    evidence_digest=digest(result.data),
                )
                for result in completed
            ],
            audit_coverage=coverage,
            ui_comparison=cast(PilotUiComparison, request.ui_comparison),
        )
    )
    _write_record(request.output, record)
    return PilotRun(tuple(completed), None, (), record)


def _write_record(path: Path, record: PilotRecord) -> None:
    """Create the record as a new private file; an existing path is never replaced."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise AdhelmError(
            "PILOT_OUTPUT_EXISTS",
            "The pilot record output already exists and is never replaced.",
            exit_code=ExitCode.PRECONDITION,
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pilot_record_bytes(record))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # Leave no partial file behind that a rerun would refuse to replace.
        with contextlib.suppress(OSError):
            path.unlink()
        raise

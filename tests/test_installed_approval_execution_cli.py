"""Installed-approval execution CLI wiring with synthetic workflow seams only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from test_ga4_create_execution_workflow import _material
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.execution_cli as execution_cli
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import ExitCode
from adhelm.ga4_create_execution_workflow import Ga4KeyEventCreateExecutionRequest
from adhelm.models import Configuration, OutputPreferences
from adhelm.openai_pause_execution_workflow import OpenAIPauseExecutionRequest
from adhelm.safety import EffectStatus, ExecutionResult, SubplanResult

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_RUNNER = CliRunner()
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_OPAQUE = "opaque:sha256:" + "c" * 64
_EVIDENCE = tuple("evidence:sha256:" + f"{index:064x}" for index in range(3))


def _invoke(directory: Path, *arguments: str) -> Any:
    return _RUNNER.invoke(root_cli.app, ["--data-dir", str(directory), "execute", *arguments])


def _error_code(result: Any) -> str:
    return str(json.loads(result.output)["diagnostics"][0]["code"])


def _write_openai_request(directory: Path) -> Path:
    path = directory / "openai-execution.json"
    path.write_text(
        OpenAIPauseExecutionRequest(plan_record_digest=_DIGEST_A).model_dump_json(),
        encoding="utf-8",
    )
    return path


def _write_ga4_request(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    request = _material(directory, monkeypatch)[0]
    assert type(request) is Ga4KeyEventCreateExecutionRequest
    path = directory / "ga4-execution.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    return path


def _result(
    provider: str,
    *,
    status: EffectStatus = EffectStatus.APPLIED_VERIFIED,
    recovery_required: bool = False,
    resources: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> ExecutionResult:
    return ExecutionResult(
        plan_digest=_DIGEST_A,
        authorization_id="private-authorization-id-must-not-reach-cli-output",
        results=(
            SubplanResult(
                provider=provider,
                subplan_digest=_DIGEST_B,
                status=status,
                resource_refs=resources,
                evidence_refs=evidence,
                recovery_action="private adapter recovery prose must not reach output",
                diagnostic_code="PRIVATE_ADAPTER_DIAGNOSTIC",
            ),
        ),
        consumed=True,
        recovery_required=recovery_required,
    )


def test_discovery_and_execution_schemas_are_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("discover or schema resolved the compiled binding"),
    )

    discovery = _RUNNER.invoke(root_cli.app, ["discover"])
    assert discovery.exit_code == 0, discovery.output
    data = json.loads(discovery.output)["data"]
    assert "execute openai-pause" in data["commands"]
    assert "execute ga4-key-event-create" in data["commands"]
    assert data["installed_approval_execution"] == {
        "automatic_execution_from_plan_or_artifact": False,
        "commands": [
            "execute openai-pause",
            "execute ga4-key-event-create",
            "execute meta-campaign-pause",
        ],
        "live_provider_execution_verified": False,
        "native_setup_prerequisite": "REQUIRED",
        "status": "UNRELEASED",
    }
    for name in (
        "openai-pause-installed-approval-execution-request",
        "ga4-key-event-create-installed-approval-execution-request",
    ):
        result = _RUNNER.invoke(root_cli.app, ["schema", name, "--root-only"])
        assert result.exit_code == 0, result.output


def test_default_binding_refuses_before_either_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openai_input = _write_openai_request(tmp_path)
    ga4_input = _write_ga4_request(tmp_path, monkeypatch)
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: pytest.fail("OpenAI workflow reached before compiled-binding refusal"),
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_ga4_key_event_create_with_installed_approval",
        lambda *_args: pytest.fail("GA4 workflow reached before compiled-binding refusal"),
    )

    for leaf, source in (("openai-pause", openai_input), ("ga4-key-event-create", ga4_input)):
        result = _invoke(tmp_path, leaf, "--input", str(source))
        assert result.exit_code == ExitCode.PRECONDITION
        assert _error_code(result) == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_invalid_input_rejects_before_binding_or_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid = tmp_path / "invalid-openai-execution.json"
    invalid.write_text(
        json.dumps({"schema_version": 1, "plan_record_digest": True}), encoding="utf-8"
    )
    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("invalid request reached binding readiness"),
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: pytest.fail("invalid request reached workflow"),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(invalid))

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(result) == "SCHEMA_VALIDATION_FAILED"


def test_output_preflight_blocks_before_binding_or_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(output=OutputPreferences(max_output_bytes=1024)),
    )
    source = _write_openai_request(tmp_path)
    monkeypatch.setattr(execution_cli, "_execution_result_probe", lambda: {"padding": "x" * 5_000})
    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("output preflight reached binding readiness"),
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: pytest.fail("output preflight reached workflow"),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"


def test_openai_execution_output_is_closed_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(output=OutputPreferences(max_output_bytes=1024)),
    )
    source = _write_openai_request(tmp_path)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: _result("openai_ads", evidence=(_EVIDENCE[0], _EVIDENCE[1])),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 1024
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "EXECUTION_RESULT"
    data = envelope["data"]
    assert data["plan_digest"] == _DIGEST_A
    assert data["authorization_consumed"] is True
    assert data["recovery_required"] is False
    assert data["history_command"] == "history status"
    assert data["provider_results"] == [
        {
            "evidence_refs": list(_EVIDENCE[:2]),
            "next_action": "Inspect history status.",
            "opaque_resource_refs": [],
            "provider": "openai_ads",
            "status": "APPLIED_VERIFIED",
            "subplan_digest": _DIGEST_B,
        }
    ]
    for private in (
        "private-authorization-id",
        "private adapter recovery prose",
        "PRIVATE_ADAPTER_DIAGNOSTIC",
        "execution_authority",
        "receipt",
        "grant",
    ):
        assert private not in result.output


def test_ga4_partial_execution_emits_bounded_recovery_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_ga4_request(tmp_path, monkeypatch)
    save_configuration(
        tmp_path,
        load_configuration(tmp_path).model_copy(
            update={"output": OutputPreferences(max_output_bytes=1024)}
        ),
        replace=True,
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_ga4_key_event_create_with_installed_approval",
        lambda *_args: _result(
            "ga4_admin",
            status=EffectStatus.PARTIAL_EFFECT,
            recovery_required=True,
            resources=(_OPAQUE,),
            evidence=_EVIDENCE,
        ),
    )

    result = _invoke(tmp_path, "ga4-key-event-create", "--input", str(source))

    assert result.exit_code == ExitCode.PARTIAL, result.output
    assert len(result.output.encode()) <= 1024
    data = json.loads(result.output)["data"]
    assert data["authorization_consumed"] is True
    assert data["recovery_required"] is True
    assert data["provider_results"][0] == {
        "evidence_refs": list(_EVIDENCE),
        "next_action": "Reconcile provider state before another execution.",
        "opaque_resource_refs": [_OPAQUE],
        "provider": "ga4_admin",
        "status": "PARTIAL_EFFECT",
        "subplan_digest": _DIGEST_B,
    }


@pytest.mark.parametrize(
    ("status", "recovery_required", "expected_exit"),
    (
        (EffectStatus.APPLIED_VERIFIED, False, ExitCode.OK),
        (EffectStatus.FAILED_NO_EFFECT, True, ExitCode.PRECONDITION),
        (EffectStatus.PARTIAL_EFFECT, True, ExitCode.PARTIAL),
        (EffectStatus.AMBIGUOUS_REMOTE_STATE, True, ExitCode.AMBIGUOUS),
        (EffectStatus.NOT_STARTED, True, ExitCode.PRECONDITION),
    ),
)
def test_execution_status_uses_a_fixed_exit_after_safe_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: EffectStatus,
    recovery_required: bool,
    expected_exit: ExitCode,
) -> None:
    source = _write_openai_request(tmp_path)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: _result("openai_ads", status=status, recovery_required=recovery_required),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert result.exit_code == expected_exit, result.output
    data = json.loads(result.output)["data"]
    assert data["provider_results"][0]["status"] == status.value
    assert data["recovery_required"] is recovery_required


def test_help_exposes_only_the_fixed_input_option() -> None:
    result = _RUNNER.invoke(root_cli.app, ["execute", "ga4-key-event-create", "--help"])

    assert result.exit_code == 0, result.output
    assert "--input" in Text.from_ansi(result.output).plain
    for forbidden in ("--receipt", "--challenge", "--trust", "--key", "--grant", "--transport"):
        assert forbidden not in Text.from_ansi(result.output).plain

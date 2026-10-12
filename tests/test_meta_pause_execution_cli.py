"""Closed Meta execution command with synthetic workflow seams only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_installed_approval_execution_cli import _result
from test_meta_pause_adapter_pipe_integration import _FixedMetaPauseHandler
from test_meta_pause_execution_integration import _install_synthetic_compiled_route
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.execution_cli as execution_cli
import adhelm.meta_pause_execution_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import ExitCode
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.safety import EffectStatus
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_RUNNER = CliRunner()
_COMMAND = "execute meta-campaign-pause"
_SCHEMA = "meta-campaign-pause-installed-approval-execution-request"


def _input(directory: Path, payload: object | None = None) -> Path:
    path = directory / "meta-execution.json"
    path.write_text(
        json.dumps(
            payload
            if payload is not None
            else {
                "schema_version": 1,
                "plan_record_digest": "a" * 64,
            }
        )
    )
    return path


def _invoke(directory: Path, path: Path) -> Any:
    return _RUNNER.invoke(
        root_cli.app,
        [
            "--data-dir",
            str(directory),
            "execute",
            "meta-campaign-pause",
            "--input",
            str(path),
        ],
    )


def _error(result: Any) -> str:
    return str(json.loads(result.output)["diagnostics"][0]["code"])


def _prohibited(*_args: object, **_kwargs: object) -> Any:
    pytest.fail("forbidden execution dependency reached")


def test_static_discovery_schema_and_guidance_do_not_resolve_native_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", _prohibited
    )
    discovered = _RUNNER.invoke(root_cli.app, ["discover"])
    assert discovered.exit_code == 0, discovered.output
    data = json.loads(discovered.output)["data"]
    assert _COMMAND in data["commands"]
    assert data["meta_campaign_pause"]["execution"] == "UNRELEASED"
    assert data["meta_campaign_pause"]["default_runtime_provider_mutations"] is False
    assert data["meta_campaign_pause"]["live_provider_execution_verified"] is False
    schema = _RUNNER.invoke(root_cli.app, ["schema", _SCHEMA, "--root-only"])
    assert schema.exit_code == 0, schema.output
    document = json.loads(schema.output)["data"]["schema"]
    assert document["additionalProperties"] is False
    assert set(document["properties"]) == {"schema_version", "plan_record_digest"}
    guidance = _RUNNER.invoke(root_cli.app, ["guidance", "show", "ad-program-optimization"])
    assert guidance.exit_code == 0, guidance.output
    assert _COMMAND in guidance.output


def test_default_meta_binding_fails_before_workflow_even_if_v1_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _input(tmp_path)
    save_configuration(tmp_path, Configuration())
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", _prohibited
    )
    result = _invoke(tmp_path, source)
    assert result.exit_code == ExitCode.PRECONDITION, result.output
    assert _error(result) == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": True, "plan_record_digest": "a" * 64},
        {"schema_version": 1, "plan_record_digest": "A" * 64},
        {"schema_version": 1, "plan_record_digest": "a" * 64, "receipt": "private-input-marker"},
    ],
)
def test_invalid_input_refuses_before_configuration_or_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    source = _input(tmp_path, payload)
    monkeypatch.setattr(execution_cli, "load_configuration", _prohibited)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", _prohibited
    )
    result = _invoke(tmp_path, source)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert _error(result) == "SCHEMA_VALIDATION_FAILED"
    assert "private-input-marker" not in result.output


def test_output_bounds_prevent_native_and_workflow_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _input(tmp_path)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", _prohibited
    )
    save_configuration(
        tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)), replace=True
    )
    monkeypatch.setattr(execution_cli, "_execution_result_probe", lambda: {"padding": "x" * 5000})
    result = _invoke(tmp_path, source)
    assert result.exit_code == ExitCode.PRECONDITION
    assert _error(result) == "OUTPUT_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        (EffectStatus.APPLIED_VERIFIED, ExitCode.OK),
        (EffectStatus.AMBIGUOUS_REMOTE_STATE, ExitCode.AMBIGUOUS),
        (EffectStatus.PARTIAL_EFFECT, ExitCode.PARTIAL),
        (EffectStatus.FAILED_NO_EFFECT, ExitCode.PRECONDITION),
        (EffectStatus.NOT_STARTED, ExitCode.PRECONDITION),
    ],
)
def test_single_bounded_redacted_result_preserves_outcome_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: EffectStatus,
    exit_code: ExitCode,
) -> None:
    source = _input(tmp_path)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    calls: list[str] = []

    def readiness() -> None:
        calls.append("meta-v2-ready")

    def workflow(directory: Path, request: object) -> object:
        assert directory == tmp_path
        assert type(request).__name__ == "MetaCampaignPauseExecutionRequest"
        calls.append("workflow")
        return _result(
            "meta_ads",
            status=status,
            recovery_required=status is not EffectStatus.APPLIED_VERIFIED,
            evidence=("evidence:sha256:" + "c" * 64, "evidence:sha256:" + "d" * 64),
        )

    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", readiness
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", workflow
    )
    result = _invoke(tmp_path, source)
    assert result.exit_code == exit_code, result.output
    assert calls == ["meta-v2-ready", "workflow"]
    assert len(result.output.encode()) <= 1024
    assert "private" not in result.output
    assert "PRIVATE_ADAPTER_DIAGNOSTIC" not in result.output
    envelope = json.loads(result.output)
    assert envelope["command"] == _COMMAND
    assert envelope["outcome"] == "EXECUTION_RESULT"
    assert envelope["artifact_refs"] == []
    data = envelope["data"]
    assert data["authorization_consumed"] is True
    assert data["recovery_required"] is (status is not EffectStatus.APPLIED_VERIFIED)
    assert data["provider_results"][0]["provider"] == "meta_ads"
    assert data["provider_results"][0]["status"] == status.value
    assert data["provider_results"][0]["opaque_resource_refs"] == []


@pytest.mark.parametrize("variant", ["other-provider", "resource-ref", "raw-result", "exception"])
def test_unpresentable_result_is_ambiguous_without_leaking_or_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    source = _input(tmp_path)
    calls: list[int] = []

    def workflow(*_args: object) -> object:
        calls.append(1)
        if variant == "exception":
            raise RuntimeError("private-result-marker raw-secret")
        if variant == "raw-result":
            return {"private-result-marker": "raw-secret"}
        return _result(
            "ga4_admin" if variant == "other-provider" else "meta_ads",
            resources=("opaque:sha256:" + "c" * 64,) if variant == "resource-ref" else (),
        )

    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready",
        lambda: None,
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", workflow
    )
    result = _invoke(tmp_path, source)
    assert result.exit_code == ExitCode.AMBIGUOUS, result.output
    assert _error(result) == (
        "EXECUTION_RESULT_UNAVAILABLE" if variant == "exception" else "EXECUTION_RESULT_INVALID"
    )
    assert calls == [1]
    assert "private-result-marker" not in result.output
    assert "raw-secret" not in result.output


@pytest.mark.parametrize(
    ("scenario", "status", "exit_code"),
    [
        ("success", "APPLIED_VERIFIED", ExitCode.OK),
        ("unknown_ack", "AMBIGUOUS_REMOTE_STATE", ExitCode.AMBIGUOUS),
        ("lost_reply", "AMBIGUOUS_REMOTE_STATE", ExitCode.AMBIGUOUS),
    ],
)
def test_cli_composes_retained_plan_native_pipe_and_fixed_meta_adapter(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    status: str,
    exit_code: ExitCode,
) -> None:
    handler = _FixedMetaPauseHandler(runtime, scenario=scenario)
    monkeypatch.setattr(runtime, "handle", handler)
    planned = runtime.plan()
    record_digest = planned["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _ = load_meta_campaign_pause_plan(ledger, record_digest)
    handler.bind_execution(
        ledger=ledger,
        plan_digest=action.plan.plan_digest,
        subplan_digest=action.plan.subplans[0].subplan_digest,
    )
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(workflow, "_now", lambda: clock[0])
    operations = _install_synthetic_compiled_route(monkeypatch, clock=clock)
    source = _input(runtime.root, {"schema_version": 1, "plan_record_digest": record_digest})

    result = _invoke(runtime.root, source)

    assert result.exit_code == exit_code, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "EXECUTION_RESULT"
    assert envelope["data"]["provider_results"][0]["status"] == status
    assert envelope["data"]["plan_digest"] == action.plan.plan_digest
    assert handler.post_count == 1
    assert handler.post_saw_durable_start is True
    assert operations.synthetic_replies == 1
    assert len(operations.launch_specs) == 1
    for marker in (_SUBJECT, _INSPECTOR, "raw_provider_marker", "raw-provider-marker"):
        assert marker not in result.output
    if exit_code == ExitCode.AMBIGUOUS:
        replay = _invoke(runtime.root, source)
        assert _error(replay) == "RECOVERY_REQUIRED"
        assert handler.post_count == 1
        assert operations.synthetic_replies == 1

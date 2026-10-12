"""Independent adversarial review of installed-approval execution CLI leaves.

These cases use only Typer's in-process runner and fake workflow results.  They
never configure a native binding, invoke a helper, acquire a credential, or
make a provider request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.execution_cli as execution_cli
import adhelm.ga4_create_cli as ga4_create_cli
import adhelm.ga4_create_plan_artifacts as ga4_artifacts
import adhelm.pause_plan_artifacts as pause_artifacts
import adhelm.plan_cli as plan_cli
from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.config import save_configuration
from adhelm.errors import ExitCode
from adhelm.models import Configuration, OutputPreferences
from adhelm.safety import EffectStatus, ExecutionResult, SubplanResult

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_EVIDENCE = "evidence:sha256:" + _DIGEST_C


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "execute", *arguments],
    )


def _error_code(result: Any) -> str:
    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output)
    return envelope["diagnostics"][0]["code"]


def _data(result: Any) -> dict[str, object]:
    envelope = json.loads(result.output)
    assert type(envelope["data"]) is dict
    return envelope["data"]


def _openai_input(directory: Path) -> Path:
    path = directory / "openai-installed-execution.json"
    path.write_text(
        json.dumps({"schema_version": 1, "plan_record_digest": _DIGEST_A}), encoding="utf-8"
    )
    return path


def _prohibited(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("execution CLI reached a forbidden downstream dependency")


def _safe_result(*, status: EffectStatus, recovery_required: bool) -> ExecutionResult:
    return ExecutionResult(
        plan_digest=_DIGEST_A,
        authorization_id="private-authorization-id-must-never-reach-cli-output",
        results=(
            SubplanResult(
                provider="openai_ads",
                subplan_digest=_DIGEST_B,
                status=status,
                resource_refs=(),
                evidence_refs=(_EVIDENCE,),
                recovery_action="private provider recovery prose",
                diagnostic_code="PRIVATE_PROVIDER_DIAGNOSTIC",
            ),
        ),
        consumed=True,
        recovery_required=recovery_required,
    )


@pytest.mark.parametrize("leaf", ("openai-pause", "ga4-key-event-create"))
@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("symlink", "INPUT_FILE_INVALID"),
        ("fifo", "INPUT_FILE_INVALID"),
        ("oversized", "INPUT_TOO_LARGE"),
        ("duplicate", "INVALID_JSON"),
        ("float_version", "SCHEMA_VALIDATION_FAILED"),
    ),
)
def test_execution_cli_rejects_unsafe_or_noncanonical_input_before_readiness_or_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
    kind: str,
    expected: str,
    make_link,
    make_pipe,
) -> None:
    selected = tmp_path / "private-installed-approval-input.json"
    if kind == "symlink":
        target = tmp_path / "private-target.json"
        target.write_bytes(b"{}")
        make_link(selected, target)
    elif kind == "fifo":
        selected = make_pipe("private-installed-approval-input.json")
    elif kind == "oversized":
        selected.write_bytes(b"x" * 1_048_577)
    elif kind == "duplicate":
        selected.write_bytes(b'{"schema_version":1,"schema_version":1}')
    elif kind == "float_version":
        selected.write_bytes(b'{"schema_version":1.0}')
    else:
        raise AssertionError(f"unknown input kind {kind}")

    monkeypatch.setattr(execution_cli, "load_configuration", _prohibited)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_openai_pause_with_installed_approval", _prohibited
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_ga4_key_event_create_with_installed_approval",
        _prohibited,
    )

    result = _invoke(tmp_path, leaf, "--input", str(selected))

    assert _error_code(result) == expected
    assert str(selected) not in result.output
    assert "private-target" not in result.output
    assert "forbidden downstream" not in result.output


@pytest.mark.parametrize(
    "forbidden_flag",
    (
        "--receipt",
        "--challenge",
        "--pin",
        "--key",
        "--grant",
        "--candidate",
        "--adapter",
        "--transport",
        "--credential",
    ),
)
def test_execution_cli_has_no_receipt_or_provider_override_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_flag: str,
) -> None:
    source = _openai_input(tmp_path)
    monkeypatch.setattr(execution_cli, "load_configuration", _prohibited)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_openai_pause_with_installed_approval", _prohibited
    )

    result = _invoke(
        tmp_path,
        "openai-pause",
        "--input",
        str(source),
        forbidden_flag,
        "private-override-value",
    )

    assert result.exit_code != 0, result.output
    assert "forbidden downstream" not in result.output
    assert "private-override-value" not in result.output


def test_default_binding_refuses_before_workflow_key_helper_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openai_input(tmp_path)
    monkeypatch.setattr(
        execution_cli, "execute_retained_openai_pause_with_installed_approval", _prohibited
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert _error_code(result) == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert "forbidden downstream" not in result.output
    assert str(source) not in result.output


def test_discovery_and_execution_schemas_do_not_resolve_native_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_cli, "load_configuration", _prohibited)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_openai_pause_with_installed_approval", _prohibited
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_ga4_key_event_create_with_installed_approval",
        _prohibited,
    )

    discover = CliRunner().invoke(root_cli.app, ["discover"])
    assert discover.exit_code == 0, discover.output
    discovered = _data(discover)
    assert "execute openai-pause" in discovered["commands"]
    assert "execute ga4-key-event-create" in discovered["commands"]
    installed = discovered["installed_approval_execution"]
    assert installed == {
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

    for schema_name in (
        "openai-pause-installed-approval-execution-request",
        "ga4-key-event-create-installed-approval-execution-request",
    ):
        result = CliRunner().invoke(root_cli.app, ["schema", schema_name])
        assert result.exit_code == 0, result.output
        data = _data(result)
        assert data["schema_name"] == schema_name


def test_plan_and_artifact_reviews_advertise_setup_without_execution_authority() -> None:
    class Review:
        review_digest = _DIGEST_A

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"review_fact": "bounded"}

    pause_data = plan_cli._review_data(Review(), {"record_digest": _DIGEST_A})
    assert pause_data["execution_authority"] is False
    assert pause_data["remote_mutations"] is False
    assert pause_data["execution_command"] == INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

    for authority in (
        pause_artifacts.PausePlanArtifactAuthority(),
        ga4_artifacts.Ga4CreatePlanArtifactAuthority(),
    ):
        assert authority.execution_authority is False
        assert authority.remote_mutations is False
        assert authority.execution_command == INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

    catalog = ga4_create_cli._Ga4KeyEventCreateCatalog(
        plans=[],
        checkpoint_digest=_DIGEST_A,
        total_count=0,
        next_cursor=None,
        historical_evidence_status="HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
    )
    assert catalog.execution_authority is False
    assert catalog.remote_mutations is False
    assert catalog.execution_command == INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS


def test_complete_result_is_projected_without_authorization_or_provider_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openai_input(tmp_path)
    save_configuration(
        tmp_path,
        Configuration(output=OutputPreferences(max_output_bytes=1024)),
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: _safe_result(status=EffectStatus.APPLIED_VERIFIED, recovery_required=False),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = _data(result)
    assert envelope["outcome"] == "EXECUTION_RESULT"
    assert len(result.output.encode("utf-8")) <= 1024
    assert data == {
        "authorization_consumed": True,
        "history_command": "history status",
        "plan_digest": _DIGEST_A,
        "provider_results": [
            {
                "evidence_refs": [_EVIDENCE],
                "next_action": "Inspect history status.",
                "opaque_resource_refs": [],
                "provider": "openai_ads",
                "status": "APPLIED_VERIFIED",
                "subplan_digest": _DIGEST_B,
            }
        ],
        "recovery_required": False,
    }
    assert "authorization_id" not in result.output
    assert "private-authorization-id" not in result.output
    assert "private provider recovery prose" not in result.output
    assert "PRIVATE_PROVIDER_DIAGNOSTIC" not in result.output
    assert "receipt" not in result.output
    assert "transport" not in result.output


def test_recovery_result_is_honest_and_exits_partial_after_safe_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openai_input(tmp_path)
    save_configuration(
        tmp_path,
        Configuration(output=OutputPreferences(max_output_bytes=1024)),
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: _safe_result(
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE, recovery_required=True
        ),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert result.exit_code == ExitCode.AMBIGUOUS, result.output
    assert len(result.output.encode("utf-8")) <= 1024
    data = _data(result)
    assert data["authorization_consumed"] is True
    assert data["recovery_required"] is True
    provider = data["provider_results"]
    assert provider == [
        {
            "evidence_refs": [_EVIDENCE],
            "next_action": "Reconcile provider state before another execution.",
            "opaque_resource_refs": [],
            "provider": "openai_ads",
            "status": "AMBIGUOUS_REMOTE_STATE",
            "subplan_digest": _DIGEST_B,
        }
    ]


@pytest.mark.parametrize(
    ("status", "expected_exit", "next_action"),
    (
        (
            EffectStatus.PARTIAL_EFFECT,
            ExitCode.PARTIAL,
            "Reconcile provider state before another execution.",
        ),
        (
            EffectStatus.FAILED_NO_EFFECT,
            ExitCode.PRECONDITION,
            "Prepare a fresh plan before another execution.",
        ),
        (
            EffectStatus.NOT_STARTED,
            ExitCode.PRECONDITION,
            "Resolve prior provider state before another execution.",
        ),
    ),
)
def test_nonverified_effect_statuses_have_distinct_closed_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: EffectStatus,
    expected_exit: ExitCode,
    next_action: str,
) -> None:
    source = _openai_input(tmp_path)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: _safe_result(status=status, recovery_required=True),
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert result.exit_code == expected_exit, result.output
    data = _data(result)
    assert data["authorization_consumed"] is True
    assert data["recovery_required"] is True
    provider_results = data["provider_results"]
    assert type(provider_results) is list and len(provider_results) == 1
    assert provider_results[0]["status"] == status.value
    assert provider_results[0]["next_action"] == next_action


def test_unpresentable_provider_result_fails_closed_without_raw_result_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openai_input(tmp_path)
    unsafe = ExecutionResult(
        plan_digest=_DIGEST_A,
        authorization_id="private-authorization-id-must-never-reach-cli-output",
        results=(
            SubplanResult(
                provider="openai_ads",
                subplan_digest=_DIGEST_B,
                status=EffectStatus.APPLIED_VERIFIED,
                resource_refs=("opaque:sha256:" + _DIGEST_C,),
                evidence_refs=(_EVIDENCE,),
                recovery_action="private raw recovery prose",
                diagnostic_code="PRIVATE_RAW_DIAGNOSTIC",
            ),
        ),
        consumed=True,
        recovery_required=False,
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_openai_pause_with_installed_approval",
        lambda *_args: unsafe,
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert _error_code(result) == "EXECUTION_RESULT_INVALID"
    assert "private-authorization-id" not in result.output
    assert "private raw recovery prose" not in result.output
    assert "PRIVATE_RAW_DIAGNOSTIC" not in result.output


def test_result_bound_is_reserved_before_readiness_or_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openai_input(tmp_path)
    monkeypatch.setattr(execution_cli, "_fits", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_openai_pause_with_installed_approval", _prohibited
    )

    result = _invoke(tmp_path, "openai-pause", "--input", str(source))

    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    assert "forbidden downstream" not in result.output

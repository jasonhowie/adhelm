"""Independent adversarial review of the Meta installed-execution CLI leaf.

The cases use Typer in process plus synthetic workflow results.  They never
issue a compiled binding, start a helper, acquire a credential, or contact a
provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.execution_cli as execution_cli
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration
from adhelm.safety import EffectStatus, ExecutionResult, SubplanResult

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_RUNNER = CliRunner()
_PLAN_DIGEST = "a" * 64
_SUBPLAN_DIGEST = "b" * 64
_RESOURCE_REF = "opaque:sha256:" + "c" * 64
_EVIDENCE_REFS = tuple("evidence:sha256:" + f"{index:064x}" for index in range(4))


def _input(directory: Path) -> Path:
    path = directory / "meta-pause-execution.json"
    path.write_text(
        json.dumps({"schema_version": 1, "plan_record_digest": _PLAN_DIGEST}),
        encoding="utf-8",
    )
    return path


def _invoke(directory: Path, *arguments: str) -> Any:
    return _RUNNER.invoke(
        root_cli.app,
        ["--data-dir", str(directory), "execute", "meta-campaign-pause", *arguments],
    )


def _error_code(result: Any) -> str:
    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output)
    return str(envelope["diagnostics"][0]["code"])


def _prohibited(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("forbidden downstream dependency reached")


def _result(*, resources: tuple[str, ...] = (), evidence: tuple[str, ...] = ()) -> ExecutionResult:
    return ExecutionResult(
        plan_digest=_PLAN_DIGEST,
        authorization_id="private-authorization-marker",
        results=(
            SubplanResult(
                provider="meta_ads",
                subplan_digest=_SUBPLAN_DIGEST,
                status=EffectStatus.APPLIED_VERIFIED,
                resource_refs=resources,
                evidence_refs=evidence,
                recovery_action="private recovery marker",
                diagnostic_code="PRIVATE_DIAGNOSTIC_MARKER",
            ),
        ),
        consumed=True,
        recovery_required=False,
    )


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_meta_leaf_rejects_special_input_before_configuration_or_either_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, make_link, make_pipe
) -> None:
    selected = tmp_path / "private-meta-input.json"
    if kind == "symlink":
        target = tmp_path / "private-target.json"
        target.write_text("{}", encoding="utf-8")
        make_link(selected, target)
    elif kind == "fifo":
        selected = make_pipe("private-meta-input.json")
    else:
        raise AssertionError(f"unexpected input kind {kind}")

    monkeypatch.setattr(execution_cli, "load_configuration", _prohibited)
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", _prohibited
    )

    result = _invoke(tmp_path, "--input", str(selected))

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert _error_code(result) == "INPUT_FILE_INVALID"
    assert str(selected) not in result.output
    assert "private-target" not in result.output
    assert "forbidden downstream" not in result.output


def test_meta_leaf_never_uses_the_v1_readiness_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _input(tmp_path)
    save_configuration(tmp_path, Configuration())
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", _prohibited
    )

    result = _invoke(tmp_path, "--input", str(source))

    assert result.exit_code == ExitCode.PRECONDITION, result.output
    assert _error_code(result) == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    assert "forbidden downstream" not in result.output
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize(
    ("resources", "evidence"),
    (
        ((_RESOURCE_REF,), ()),
        ((), _EVIDENCE_REFS),
    ),
)
def test_meta_result_rejects_resource_references_and_excess_evidence_without_leaking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resources: tuple[str, ...],
    evidence: tuple[str, ...],
) -> None:
    source = _input(tmp_path)
    save_configuration(tmp_path, Configuration())
    calls: list[str] = []

    def workflow(*_args: object) -> ExecutionResult:
        calls.append("workflow")
        return _result(resources=resources, evidence=evidence)

    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready",
        lambda: None,
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", workflow
    )

    result = _invoke(tmp_path, "--input", str(source))

    assert result.exit_code == ExitCode.AMBIGUOUS, result.output
    assert _error_code(result) == "EXECUTION_RESULT_INVALID"
    assert calls == ["workflow"]
    for marker in (
        "private-authorization-marker",
        "private recovery marker",
        "PRIVATE_DIAGNOSTIC_MARKER",
        _RESOURCE_REF,
        _EVIDENCE_REFS[-1],
    ):
        assert marker not in result.output


def test_shared_workflow_exception_boundary_keeps_v1_raw_text_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "openai-execution.json"
    source.write_text(
        json.dumps({"schema_version": 1, "plan_record_digest": _PLAN_DIGEST}), encoding="utf-8"
    )
    save_configuration(tmp_path, Configuration())
    calls: list[str] = []

    def workflow(*_args: object) -> ExecutionResult:
        calls.append("workflow")
        raise RuntimeError("ultra-private-runtime-marker")

    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_openai_pause_with_installed_approval", workflow
    )

    result = _RUNNER.invoke(
        root_cli.app,
        ["--data-dir", str(tmp_path), "execute", "openai-pause", "--input", str(source)],
    )

    assert result.exit_code == ExitCode.AMBIGUOUS, result.output
    assert _error_code(result) == "EXECUTION_RESULT_UNAVAILABLE"
    assert calls == ["workflow"]
    assert "ultra-private-runtime-marker" not in result.output


def test_shared_workflow_boundary_preserves_fixed_adhelm_error_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _input(tmp_path)
    save_configuration(tmp_path, Configuration())
    calls: list[str] = []

    def workflow(*_args: object) -> ExecutionResult:
        calls.append("workflow")
        raise AdhelmError(
            "META_FIXED_WORKFLOW_REFUSAL",
            "The closed fake workflow refused before provider activity.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Inspect the retained plan.",
        )

    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready",
        lambda: None,
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_meta_campaign_pause_with_installed_approval", workflow
    )

    result = _invoke(tmp_path, "--input", str(source))

    assert result.exit_code == ExitCode.AUTHORIZATION, result.output
    assert _error_code(result) == "META_FIXED_WORKFLOW_REFUSAL"
    assert "EXECUTION_RESULT_UNAVAILABLE" not in result.output
    assert calls == ["workflow"]


def test_meta_execution_schema_is_closed_and_requires_only_the_retained_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", _prohibited
    )
    monkeypatch.setattr(
        execution_cli, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", _prohibited
    )

    result = _RUNNER.invoke(
        root_cli.app,
        ["schema", "meta-campaign-pause-installed-approval-execution-request", "--root-only"],
    )

    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["plan_record_digest"]
    assert set(schema["properties"]) == {"schema_version", "plan_record_digest"}
    encoded = json.dumps(schema, sort_keys=True)
    for forbidden in ("receipt", "candidate", "grant", "artifact", "adapter", "transport"):
        assert forbidden not in encoded

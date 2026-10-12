"""The two generic command surfaces: `execute plan` and the `recover` family.

These exercise the CLI boundary with synthetic workflow returns and fake
readiness functions.  No native helper, credential store, approval host, or
provider is reached, and nothing here is evidence of installed native authority
or live provider execution.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from test_generic_execution_route import _interrupted
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.execution_cli as execution_cli
import adhelm.recovery_cli as recovery_cli
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.generic_execution_workflow import AttemptRecoveryOutcome
from adhelm.models import Configuration, OutputPreferences
from adhelm.safety import EffectStatus, ExecutionResult, SubplanResult

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_RUNNER = CliRunner()
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_OPAQUE = tuple("opaque:sha256:" + f"{index:064x}" for index in range(4))
_EVIDENCE = tuple("evidence:sha256:" + f"{index:064x}" for index in range(3))


def _invoke(directory: Path, *arguments: str) -> Any:
    return _RUNNER.invoke(root_cli.app, ["--data-dir", str(directory), *arguments])


def _payload(result: Any) -> dict[str, Any]:
    return dict(json.loads(result.output))


def _error_code(result: Any) -> str:
    return str(_payload(result)["diagnostics"][0]["code"])


def _execution_result(
    provider: str = "linkedin_ads",
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


def _recovery_outcome(**overrides: object) -> AttemptRecoveryOutcome:
    values: dict[str, Any] = {
        "result": _execution_result(
            "google_ads", status=EffectStatus.AMBIGUOUS_REMOTE_STATE, recovery_required=True
        ),
        "record_digest": _DIGEST_C,
        "started_record_digest": _DIGEST_A,
        "plan_digest": _DIGEST_B,
        "subplan_digest": _DIGEST_B,
        "provider": "google_ads",
        "operation": "PAUSE",
        "observed_relation": "AT_BASELINE",
        "disposition": "RECOVERED_AT_BASELINE",
        "actor_ref": "UNKNOWN",
        "attempt_effect_status": "AMBIGUOUS_REMOTE_STATE",
    }
    values.update(overrides)
    return AttemptRecoveryOutcome(**values)


# --- discovery --------------------------------------------------------------


def test_discovery_and_schemas_stay_static_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("discover or schema resolved the compiled binding"),
    )
    monkeypatch.setattr(
        recovery_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("discover or schema resolved the compiled binding"),
    )

    discovery = _RUNNER.invoke(root_cli.app, ["discover"])

    assert discovery.exit_code == 0, discovery.output
    data = _payload(discovery)["data"]
    assert {"execute plan", "recover attempt", "recover catalog"} <= set(data["commands"])
    assert data["attempt_recovery"] == {
        "commands": ["recover attempt", "recover catalog"],
        "status": "UNRELEASED",
        "selection": "POSITIONAL_RECORD_DIGEST_ONLY_NO_INPUT_FILE",
        "native_setup_prerequisite": "REQUIRED",
        "recorded_kind": "ATTEMPT_RECOVERY_OBSERVED",
        "executable_operations": [
            {"provider": "google_ads", "operation": "ACTIVATE"},
            {"provider": "google_ads", "operation": "CREATE"},
            {"provider": "google_ads", "operation": "PAUSE"},
            {"provider": "google_ads", "operation": "UPDATE"},
            {"provider": "meta_ads", "operation": "CREATE"},
            {"provider": "openai_ads", "operation": "CREATE"},
        ],
        "refusal_gates": [
            {
                "gate": "RELEASE_POLICY_WRITES_RELEASED",
                "satisfied": False,
                "refusal_code": "WRITES_NOT_RELEASED",
            },
            {
                "gate": "COMPILED_APPROVAL_BRIDGE_INSTALLED",
                "satisfied": None,
                "refusal_code": "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED",
            },
            {
                "gate": "APPROVAL_KIND_RENDERED_BY_INSTALLED_HOST",
                "satisfied": False,
                "refusal_code": "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED",
            },
            {
                "gate": "CONFIGURED_EXECUTION_ADAPTER",
                # P03 registered the one installed adapter; the caller still
                # never reaches this gate, because the release-policy gate and
                # both bridge gates precede it.
                "satisfied": True,
                "refusal_code": "EXECUTION_ADAPTER_NOT_CONFIGURED",
            },
        ],
        "first_refusal_code": "WRITES_NOT_RELEASED",
        "attribution": "NO_ACTOR_ATTRIBUTED",
        "barrier_scope": "GLOBAL_ACROSS_PROVIDERS_AND_ACCOUNTS",
        "settles_attempt": False,
        "authorizes_retry": False,
        "provider_mutations": False,
        "live_provider_execution_verified": False,
    }
    assert data["execution_plan"]["execution"] == "UNRELEASED"
    assert data["execution_plan"]["executable_operations"] == [
        {"provider": "google_ads", "operation": "ACTIVATE"},
        {"provider": "google_ads", "operation": "CREATE"},
        {"provider": "google_ads", "operation": "PAUSE"},
        {"provider": "google_ads", "operation": "UPDATE"},
        {"provider": "meta_ads", "operation": "CREATE"},
        {"provider": "openai_ads", "operation": "CREATE"},
    ]
    for name in (
        "execution-plan-installed-approval-execution-request",
        "attempt-recovery-installed-approval-request",
    ):
        assert name in data["schemas"]
        result = _RUNNER.invoke(root_cli.app, ["schema", name, "--root-only"])
        assert result.exit_code == 0, result.output


def test_discover_derives_executable_operations_from_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue is rendered at call time, never from a hard-coded list."""

    monkeypatch.setattr(
        root_cli,
        "executable_execution_operations",
        lambda: ({"provider": "google_ads", "operation": "CREATE"},),
    )

    data = _payload(_RUNNER.invoke(root_cli.app, ["discover"]))["data"]

    assert data["execution_plan"]["executable_operations"] == [
        {"provider": "google_ads", "operation": "CREATE"}
    ]
    assert data["attempt_recovery"]["executable_operations"] == [
        {"provider": "google_ads", "operation": "CREATE"}
    ]


# --- `execute plan` ---------------------------------------------------------


def test_the_unconfigured_build_refuses_execute_plan_before_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_execution_plan_with_installed_approval",
        lambda *_args: pytest.fail("an unconfigured build reached the generic workflow"),
    )

    result = _invoke(tmp_path, "execute", "plan", "0" * 64)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert _payload(result)["data"] == {}


@pytest.mark.parametrize("selector", ("not-a-digest", "A" * 64, "a" * 63, ""))
def test_a_malformed_selector_is_refused_before_any_other_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    monkeypatch.setattr(
        execution_cli,
        "_require_generic_result_fit",
        lambda *_args: pytest.fail("a malformed selector reached output preflight"),
    )
    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("a malformed selector reached native readiness"),
    )

    result = _invoke(tmp_path, "execute", "plan", selector)

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(result) == "INVALID_EXECUTION_SELECTION"
    assert _payload(result)["data"] == {}


def test_execute_plan_reserves_its_wider_envelope_before_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic leaf reserves the widest installed provider and child bound."""

    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    monkeypatch.setattr(
        execution_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("output preflight reached native readiness"),
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_execution_plan_with_installed_approval",
        lambda *_args: pytest.fail("output preflight reached the generic workflow"),
    )

    result = _invoke(tmp_path, "execute", "plan", _DIGEST_A)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    # The three fixed leaves keep their own narrower reservation, so the same
    # saved bound still admits them.
    assert execution_cli._fits(
        "execute openai-pause",
        execution_cli._execution_result_probe(),
        1024,
        outcome="EXECUTION_RESULT",
    )


def test_execute_plan_emits_the_planned_provider_in_a_closed_redacted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which provider ran is decided by the retained plan, not by the command."""

    captured: list[object] = []

    def _workflow(directory: Path, request: object) -> ExecutionResult:
        captured.append(request)
        return _execution_result("linkedin_ads", evidence=_EVIDENCE[:1])

    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_execution_plan_with_installed_approval", _workflow
    )

    result = _invoke(tmp_path, "execute", "plan", _DIGEST_A)

    assert result.exit_code == 0, result.output
    envelope = _payload(result)
    assert envelope["command"] == "execute plan"
    assert envelope["outcome"] == "EXECUTION_RESULT"
    assert envelope["data"]["provider_results"] == [
        {
            "evidence_refs": [_EVIDENCE[0]],
            "next_action": "Inspect history status.",
            "opaque_resource_refs": [],
            "provider": "linkedin_ads",
            "status": "APPLIED_VERIFIED",
            "subplan_digest": _DIGEST_B,
        }
    ]
    assert envelope["data"]["history_command"] == "history status"
    assert [getattr(item, "plan_record_digest", None) for item in captured] == [_DIGEST_A]
    for private in (
        "private-authorization-id",
        "private adapter recovery prose",
        "PRIVATE_ADAPTER_DIAGNOSTIC",
        "receipt",
        "grant",
    ):
        assert private not in result.output


def test_execute_plan_admits_a_multi_child_resource_list_the_fixed_leaves_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_execution_plan_with_installed_approval",
        lambda *_args: _execution_result("google_ads", resources=_OPAQUE[:3], evidence=_EVIDENCE),
    )

    result = _invoke(tmp_path, "execute", "plan", _DIGEST_A)

    assert result.exit_code == 0, result.output
    assert _payload(result)["data"]["provider_results"][0]["opaque_resource_refs"] == list(
        _OPAQUE[:3]
    )
    # The same result through a fixed single-resource leaf is refused.
    with pytest.raises(AdhelmError) as refused:
        execution_cli._execution_result_data(
            _execution_result("ga4_admin", resources=_OPAQUE[:3], evidence=_EVIDENCE),
            expected_provider="ga4_admin",
        )
    assert refused.value.code == "EXECUTION_RESULT_INVALID"


@pytest.mark.parametrize(
    ("status", "exit_code"),
    (
        (EffectStatus.PARTIAL_EFFECT, ExitCode.PARTIAL),
        (EffectStatus.AMBIGUOUS_REMOTE_STATE, ExitCode.AMBIGUOUS),
        (EffectStatus.FAILED_NO_EFFECT, ExitCode.PRECONDITION),
        (EffectStatus.NOT_STARTED, ExitCode.PRECONDITION),
    ),
)
def test_execute_plan_keeps_the_shared_status_to_exit_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: EffectStatus, exit_code: ExitCode
) -> None:
    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli,
        "execute_retained_execution_plan_with_installed_approval",
        lambda *_args: _execution_result(
            "google_ads", status=status, recovery_required=True, evidence=_EVIDENCE[:1]
        ),
    )

    result = _invoke(tmp_path, "execute", "plan", _DIGEST_A)

    assert result.exit_code == exit_code
    assert _payload(result)["data"]["provider_results"][0]["status"] == status.value


def test_an_unpresentable_generic_result_is_reported_without_its_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(*_args: object) -> ExecutionResult:
        raise RuntimeError("private workflow detail must not reach the envelope")

    monkeypatch.setattr(
        execution_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        execution_cli, "execute_retained_execution_plan_with_installed_approval", _explode
    )

    result = _invoke(tmp_path, "execute", "plan", _DIGEST_A)

    assert result.exit_code == ExitCode.AMBIGUOUS
    assert _error_code(result) == "EXECUTION_RESULT_UNAVAILABLE"
    assert "private workflow detail" not in result.output


# --- `recover attempt` ------------------------------------------------------


def test_the_unconfigured_build_refuses_recover_attempt_before_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recovery_cli,
        "record_retained_attempt_recovery_with_installed_approval",
        lambda *_args: pytest.fail("an unconfigured build reached the recovery workflow"),
    )

    result = _invoke(tmp_path, "recover", "attempt", "0" * 64)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert _payload(result)["data"] == {}


def test_recover_attempt_refuses_a_malformed_selector_before_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recovery_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("a refused recovery reached native readiness"),
    )

    malformed = _invoke(tmp_path, "recover", "attempt", "not-a-digest")
    assert malformed.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(malformed) == "INVALID_EXECUTION_SELECTION"


def test_recover_attempt_reserves_its_envelope_before_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    monkeypatch.setattr(recovery_cli, "_attempt_result_probe", lambda: {"padding": "x" * 5_000})
    monkeypatch.setattr(
        recovery_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("output preflight reached native readiness"),
    )
    monkeypatch.setattr(
        recovery_cli,
        "record_retained_attempt_recovery_with_installed_approval",
        lambda *_args: pytest.fail("output preflight reached the recovery workflow"),
    )

    result = _invoke(tmp_path, "recover", "attempt", _DIGEST_A)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"


def test_recover_attempt_emits_the_observation_and_what_it_does_not_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=2048)))
    monkeypatch.setattr(
        recovery_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        recovery_cli,
        "record_retained_attempt_recovery_with_installed_approval",
        lambda *_args: _recovery_outcome(),
    )

    result = _invoke(tmp_path, "recover", "attempt", _DIGEST_A)

    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 2048
    envelope = _payload(result)
    assert envelope["command"] == "recover attempt"
    assert envelope["outcome"] == "ATTEMPT_RECOVERY_OBSERVED_NO_ATTRIBUTION"
    assert envelope["data"] == {
        "schema_version": 1,
        "record_digest": _DIGEST_C,
        "started_record_digest": _DIGEST_A,
        "plan_digest": _DIGEST_B,
        "subplan_digest": _DIGEST_B,
        "provider": "google_ads",
        "operation": "PAUSE",
        "observed_relation": "AT_BASELINE",
        "disposition": "RECOVERED_AT_BASELINE",
        "actor_ref": "UNKNOWN",
        "attempt_effect_status": "AMBIGUOUS_REMOTE_STATE",
        "attribution": "NO_ACTOR_ATTRIBUTED",
        "provider_change_applied": False,
        "attempt_settled": False,
        "retry_authorized": False,
        "history_command": "history status",
        "next_action": (
            "Compile a new plan from a fresh read before any further mutation; "
            "inspect history status."
        ),
    }
    for private in ("private-authorization-id", "private adapter recovery prose", "receipt"):
        assert private not in result.output


def test_an_unpresentable_observation_is_ambiguous_and_never_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recovery_cli, "_require_compiled_installed_approval_bridge_ready", lambda: None
    )
    monkeypatch.setattr(
        recovery_cli,
        "record_retained_attempt_recovery_with_installed_approval",
        lambda *_args: _recovery_outcome(actor_ref="a-named-person"),
    )

    result = _invoke(tmp_path, "recover", "attempt", _DIGEST_A)

    assert result.exit_code == ExitCode.AMBIGUOUS
    assert _error_code(result) == "ATTEMPT_RECOVERY_RESULT_UNAVAILABLE"
    assert "a-named-person" not in result.output
    assert "do not repeat" in json.dumps(_payload(result)["diagnostics"])


# --- `recover catalog` ------------------------------------------------------


def test_recover_catalog_on_an_empty_ledger_returns_a_bounded_empty_envelope(
    tmp_path: Path,
) -> None:
    result = _invoke(tmp_path, "recover", "catalog")

    assert result.exit_code == 0, result.output
    envelope = _payload(result)
    assert envelope["outcome"] == "UNRESOLVED_ATTEMPT_HISTORY_NO_EXECUTION_AUTHORITY"
    assert envelope["total_count"] == 0
    assert envelope["next_cursor"] is None
    assert envelope["data"] == {
        "attempts": [],
        "total_count": 0,
        "next_cursor": None,
        "history_verification": "HASH_CHAIN_VERIFIED_LOCAL_SCAN",
        "blocking_effect": "BLOCKS_EVERY_LATER_GUARDED_WRITE",
        "attribution": "NO_ACTOR_ATTRIBUTED",
        "recovery_command": "recover attempt",
        "recovery_authorizes_retry": False,
    }


def test_recover_catalog_lists_the_unresolved_attempt_that_blocks_later_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, record, started = _interrupted(tmp_path, monkeypatch)
    monkeypatch.undo()

    result = _invoke(tmp_path, "recover", "catalog")

    assert result.exit_code == 0, result.output
    envelope = _payload(result)
    assert envelope["total_count"] == 1
    row = envelope["data"]["attempts"][0]
    assert row["started_record_digest"] == started
    assert row["plan_digest"] == record.plan_digest
    assert row["level"] == "SUBPLAN"
    assert row["refusal_code"] == "UNRESOLVED_APPLY"
    assert row["recoverable"] is True
    assert datetime.fromisoformat(row["recorded_at"]).tzinfo is not None


def test_recover_catalog_refuses_an_invalid_page_and_an_unpresentable_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid = _invoke(tmp_path, "recover", "catalog", "--limit", "0")
    assert invalid.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(invalid) == "INVALID_ATTEMPT_RECOVERY_SELECTION"

    _ledger, _record, _started = _interrupted(tmp_path, monkeypatch)
    monkeypatch.undo()
    save_configuration(
        tmp_path,
        Configuration(output=OutputPreferences(max_output_bytes=1024)),
        replace=True,
    )
    monkeypatch.setattr(recovery_cli, "_fits", lambda *_args, **_kwargs: False)

    unpresentable = _invoke(tmp_path, "recover", "catalog")

    assert unpresentable.exit_code == ExitCode.PRECONDITION
    assert _error_code(unpresentable) == "OUTPUT_LIMIT_EXCEEDED"


def test_the_catalog_page_shrinks_until_it_fits_the_saved_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unresolved rows and a bound that admits only one keeps a cursor."""

    _ledger, _record, _started = _interrupted(tmp_path, monkeypatch)
    monkeypatch.undo()
    rows = _payload(_invoke(tmp_path, "recover", "catalog"))["data"]["attempts"]
    assert len(rows) == 1
    duplicated = {
        "attempts": rows * 2,
        "total_count": 2,
        "next_cursor": None,
        "history_verification": "HASH_CHAIN_VERIFIED_LOCAL_SCAN",
        "blocking_effect": "BLOCKS_EVERY_LATER_GUARDED_WRITE",
        "attribution": "NO_ACTOR_ATTRIBUTED",
        "recovery_command": "recover attempt",
        "recovery_authorizes_retry": False,
    }
    monkeypatch.setattr(
        recovery_cli, "unresolved_attempt_catalog", lambda *_args, **_kwargs: duplicated
    )
    calls: list[int] = []

    def _fits(command: str, data: dict[str, Any], maximum: int, **kwargs: Any) -> bool:
        del command, maximum, kwargs
        calls.append(len(data["attempts"]))
        return len(data["attempts"]) <= 1

    monkeypatch.setattr(recovery_cli, "_fits", _fits)

    result = _invoke(tmp_path, "recover", "catalog")

    assert result.exit_code == 0, result.output
    envelope = _payload(result)
    assert calls == [2, 1]
    assert len(envelope["data"]["attempts"]) == 1
    assert envelope["data"]["next_cursor"] == "1"
    assert envelope["total_count"] == 2


def test_recover_catalog_is_a_local_read_that_never_touches_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recovery_cli,
        "_require_compiled_installed_approval_bridge_ready",
        lambda: pytest.fail("a local history read resolved the compiled binding"),
    )

    result = _invoke(tmp_path, "recover", "catalog")

    assert result.exit_code == 0, result.output
    assert _payload(result)["data"]["attempts"] == []

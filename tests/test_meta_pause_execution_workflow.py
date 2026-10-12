"""Fake-only checks for the private installed Meta campaign PAUSE workflow."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_meta_campaign_pause_adapter import _direct_grant, _FixedMetaPauseHandler
from test_meta_pause_planning_integration import _Runtime
from test_meta_pause_planning_integration import runtime as runtime

import adhelm.meta_pause_execution_workflow as workflow
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import ApprovalChallengeKind
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.meta_pause_execution_workflow import (
    MetaCampaignPauseExecutionRequest,
    execute_retained_meta_campaign_pause_with_installed_approval,
)
from adhelm.safety import EffectStatus
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _request(record_digest: str) -> MetaCampaignPauseExecutionRequest:
    return MetaCampaignPauseExecutionRequest(plan_record_digest=record_digest)


def _prepared_record(runtime: _Runtime) -> tuple[Ledger, object, MetaCampaignPauseExecutionRequest]:
    receipt = runtime.plan()
    record_digest = receipt["data"]["record_digest"]
    assert type(record_digest) is str
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    return ledger, action, _request(record_digest)


def _install_synthetic_approval(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    after_approval: Any = lambda: None,
    transform: Any = lambda value: value,
) -> _FixedMetaPauseHandler:
    """Return an exact durable direct grant without a helper or generated binding."""

    handler = _FixedMetaPauseHandler()
    monkeypatch.setattr(runtime, "handle", handler)
    monkeypatch.setattr(
        workflow, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", lambda: None
    )

    def approve(kernel: object, ledger: object, *, source_plan_record_digest: str) -> object:
        assert type(kernel).__name__ == "SafetyKernel"
        assert type(ledger) is Ledger
        action, _provenance = load_meta_campaign_pause_plan(ledger, source_plan_record_digest)
        _receipt_kernel, grant = _direct_grant(runtime, ledger, action, source_plan_record_digest)
        after_approval()
        accepted = AcceptedNativeApproval(
            receipt_claim_record_digest="a" * 64,
            receipt_digest="b" * 64,
            kind=ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN,
            direct_grant=grant,
            policy=None,
        )
        return transform(accepted)

    monkeypatch.setattr(
        workflow, "_request_and_record_installed_meta_campaign_pause_approval_v2", approve
    )
    return handler


def test_default_v2_binding_refuses_before_ledger_configuration_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request("a" * 64)
    monkeypatch.setattr(workflow, "Ledger", lambda _: pytest.fail("ledger opened"))
    monkeypatch.setattr(workflow, "load_configuration", lambda _: pytest.fail("configuration read"))
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_meta_campaign_pause_approval_v2",
        lambda *_args, **_kwargs: pytest.fail("native approval requested"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(tmp_path, request)

    assert raised.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    assert raised.value.exit_code is ExitCode.PRECONDITION


def test_constructed_request_is_rejected_before_compiled_ready_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = MetaCampaignPauseExecutionRequest.model_construct(
        schema_version=True, plan_record_digest="a" * 64
    )
    monkeypatch.setattr(
        workflow,
        "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready",
        lambda: pytest.fail("readiness checked for malformed request"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(tmp_path, request)

    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_EXECUTION_INPUT"


def test_exact_durable_meta_grant_rechecks_then_executes_one_guarded_post(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)
    handler = _install_synthetic_approval(runtime, monkeypatch)

    result = execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.recovery_required is False
    assert handler.post_count == 1
    rows = Ledger(runtime.root).records("changes")
    assert any(row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in rows)
    assert any(row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION" for row in rows)
    assert any(row["payload"].get("event_type") == "APPLY_STARTED" for row in rows)


def test_current_non_edit_profile_stops_before_native_approval_or_adapter(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)
    configuration = load_configuration(runtime.root)
    profile = configuration.profiles[0].model_copy(update={"purpose": "READ"})
    save_configuration(
        runtime.root, configuration.model_copy(update={"profiles": [profile]}), replace=True
    )
    monkeypatch.setattr(
        workflow, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", lambda: None
    )
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_meta_campaign_pause_approval_v2",
        lambda *_args, **_kwargs: pytest.fail("native approval requested"),
    )
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("adapter opened"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "META_TOKEN_EDIT_PROFILE_REQUIRED"


def test_expired_source_stops_before_configuration_or_native_approval(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, action, request = _prepared_record(runtime)
    monkeypatch.setattr(
        workflow, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", lambda: None
    )
    monkeypatch.setattr(workflow, "_now", lambda: action.plan.expires_at + timedelta(seconds=1))
    monkeypatch.setattr(
        workflow, "_require_current_configuration", lambda *_args: pytest.fail("configuration read")
    )
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_meta_campaign_pause_approval_v2",
        lambda *_args, **_kwargs: pytest.fail("native approval requested"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "PLAN_EXPIRED"


def test_configuration_drift_after_native_approval_blocks_adapter_open(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)

    def change_configuration() -> None:
        current = load_configuration(runtime.root)
        save_configuration(
            runtime.root,
            current.model_copy(
                update={"output": current.output.model_copy(update={"max_rows": 12})}
            ),
            replace=True,
        )

    _install_synthetic_approval(runtime, monkeypatch, after_approval=change_configuration)
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("adapter opened after configuration drift"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "CONFIGURATION_DRIFT"


def test_wrong_native_grant_cannot_open_adapter(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)

    def wrong_grant(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        assert accepted.direct_grant is not None
        return replace(
            accepted,
            direct_grant=accepted.direct_grant.model_copy(update={"plan_digest": "0" * 64}),
        )

    _install_synthetic_approval(runtime, monkeypatch, transform=wrong_grant)
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("adapter opened for wrong grant"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "META_CAMPAIGN_PAUSE_APPROVAL_REQUIRED"

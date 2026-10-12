"""Independent fake-only boundaries for installed Meta PAUSE execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_meta_campaign_pause_adapter import _FixedMetaPauseHandler
from test_meta_pause_execution_workflow import (
    _install_synthetic_approval,
    _prepared_record,
)
from test_meta_pause_planning_integration import _Runtime
from test_meta_pause_planning_integration import runtime as runtime

import adhelm.meta_pause_execution_workflow as workflow
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import ApprovalChallengeKind
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_pause_execution_workflow import (
    MetaCampaignPauseExecutionRequest,
    execute_retained_meta_campaign_pause_with_installed_approval,
)
from adhelm.models import OutputPreferences
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction
from adhelm.safety import EffectStatus, new_direct_grant
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def test_missing_slot_request_rejects_before_ready_or_local_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = object.__new__(MetaCampaignPauseExecutionRequest)

    def unexpected(*_args: object, **_kwargs: object) -> object:
        pytest.fail("malformed request reached a runtime boundary")

    monkeypatch.setattr(
        workflow, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", unexpected
    )
    monkeypatch.setattr(workflow, "Ledger", unexpected)
    monkeypatch.setattr(workflow, "load_configuration", unexpected)

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(tmp_path, request)

    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_EXECUTION_INPUT"


def test_expiry_after_approval_claim_blocks_adapter_and_fixed_post(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, action, request = _prepared_record(runtime)
    assert type(action) is MetaCampaignPausePlannedAction
    _install_synthetic_approval(
        runtime,
        monkeypatch,
        after_approval=lambda: monkeypatch.setattr(
            workflow, "_now", lambda: action.plan.expires_at
        ),
    )
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("expired action opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "PLAN_EXPIRED"
    assert any(
        row["payload"].get("event_type") == "APPROVAL_GRANTED"
        for row in Ledger(runtime.root).records("changes")
    )


def test_malformed_clock_after_approval_blocks_adapter_session(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)
    _install_synthetic_approval(
        runtime,
        monkeypatch,
        after_approval=lambda: monkeypatch.setattr(workflow, "_now", lambda: datetime(2026, 9, 6)),
    )
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("invalid clock opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "CLOCK_INVALID"


def test_configuration_drift_after_approval_blocks_adapter_session(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)

    def change_configuration() -> None:
        configuration = load_configuration(runtime.root)
        save_configuration(
            runtime.root,
            configuration.model_copy(update={"output": OutputPreferences(artifact_mode="OFF")}),
            replace=True,
        )

    _install_synthetic_approval(runtime, monkeypatch, after_approval=change_configuration)
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("configuration drift opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "CONFIGURATION_DRIFT"


def test_recovery_barrier_appearing_after_approval_blocks_adapter_session(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)
    _install_synthetic_approval(
        runtime,
        monkeypatch,
        after_approval=lambda: monkeypatch.setattr(
            Ledger, "unresolved_starts", lambda _ledger: [{"synthetic": "start"}]
        ),
    )
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("recovery-barred execution opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "RECOVERY_REQUIRED"


def test_copied_same_plan_grant_with_changed_durable_identity_cannot_open_adapter(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)

    def alter_identity(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        assert accepted.direct_grant is not None
        return replace(
            accepted,
            direct_grant=accepted.direct_grant.model_copy(
                update={"grantor_ref": "logical:unrecorded-meta-grantor"}
            ),
        )

    _install_synthetic_approval(runtime, monkeypatch, transform=alter_identity)
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("copied grant opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"


def test_non_durable_shape_valid_direct_grant_cannot_open_adapter(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)
    monkeypatch.setattr(
        workflow, "_require_compiled_meta_campaign_pause_approval_bridge_v2_ready", lambda: None
    )

    def unrecorded_approval(
        _kernel: object, ledger: Ledger, *, source_plan_record_digest: str
    ) -> AcceptedNativeApproval:
        action, _provenance = load_meta_campaign_pause_plan(ledger, source_plan_record_digest)
        grant = new_direct_grant(
            authorization_id="ultra-meta-unrecorded-grant",
            plan=action.plan,
            grantor_ref="logical:ultra-meta-unrecorded-grantor",
            host_ref="host:ultra-meta-unrecorded",
            user_interaction_ref="interaction:ultra-meta-unrecorded",
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        return AcceptedNativeApproval(
            receipt_claim_record_digest="a" * 64,
            receipt_digest="b" * 64,
            kind=ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN,
            direct_grant=grant,
            policy=None,
        )

    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_meta_campaign_pause_approval_v2",
        unrecorded_approval,
    )
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("non-durable grant opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"


def test_policy_result_cannot_substitute_for_meta_direct_grant(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)

    def inject_policy(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        return replace(accepted, policy=object())  # type: ignore[arg-type]

    _install_synthetic_approval(runtime, monkeypatch, transform=inject_policy)
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("policy result opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "META_CAMPAIGN_PAUSE_APPROVAL_REQUIRED"


def test_non_meta_kind_cannot_substitute_for_the_durable_meta_direct_grant(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)

    def alter_kind(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        return replace(accepted, kind=ApprovalChallengeKind.OPENAI_PAUSE_PLAN)

    _install_synthetic_approval(runtime, monkeypatch, transform=alter_kind)
    monkeypatch.setattr(
        workflow,
        "_configured_meta_campaign_pause_adapter",
        lambda *_args, **_kwargs: pytest.fail("wrong-kind result opened an EDIT session"),
    )

    with pytest.raises(AdhelmError) as raised:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert raised.value.code == "META_CAMPAIGN_PAUSE_APPROVAL_REQUIRED"


def test_ambiguous_post_blocks_second_workflow_approval_and_post(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger, _action, request = _prepared_record(runtime)
    _install_synthetic_approval(runtime, monkeypatch)
    handler = _FixedMetaPauseHandler(acknowledgement_body=b'{"success":false}')
    monkeypatch.setattr(runtime, "handle", handler)
    approval_calls = 0
    original_approval = workflow._request_and_record_installed_meta_campaign_pause_approval_v2

    def counted_approval(*args: object, **kwargs: object) -> object:
        nonlocal approval_calls
        approval_calls += 1
        return original_approval(*args, **kwargs)

    monkeypatch.setattr(
        workflow, "_request_and_record_installed_meta_campaign_pause_approval_v2", counted_approval
    )

    first = execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)
    assert first.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert first.recovery_required is True
    assert handler.post_count == 1

    with pytest.raises(AdhelmError) as replayed:
        execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)

    assert replayed.value.code == "RECOVERY_REQUIRED"
    assert approval_calls == 1
    assert handler.post_count == 1

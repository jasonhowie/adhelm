"""Independent containment checks for the fixed Meta campaign PAUSE kernel seam.

The fixtures use locally signed approval material and in-memory fake adapters.
They neither construct a Meta transport nor make a provider request.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from test_meta_pause_kernel import (
    _EVIDENCE,
    _NOW,
    _OPAQUE,
    _action,
    _GuardedMetaAdapter,
    _MetaAdapterBase,
    _signed_material,
)

from adhelm.egress_permit import EgressPermit
from adhelm.ledger import Ledger
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    DirectGrant,
    EffectStatus,
    EgressTarget,
    Money,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    SafetyKernel,
    StateRef,
    SubPlan,
    Verification,
    _unattributable_attempt_state,
    new_direct_grant,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _InterruptAfterMetaEgress(_MetaAdapterBase):
    """Simulates process interruption after durable start and guarded egress."""

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        self.apply_calls += 1
        permit.consume(
            plan_digest=self._action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        self._state = RemoteState(fingerprint=plan.after.fingerprint, revision=plan.after.revision)
        raise KeyboardInterrupt


class _ClaimedNoEffectAtTargetAdapter(_GuardedMetaAdapter):
    """A failed acknowledgement cannot make a target-state read attributable."""

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        response = super().apply_guarded(plan, idempotency_key=idempotency_key, permit=permit)
        return response.model_copy(update={"no_effect_proven": True})


class _MultiRunMetaAdapter(_MetaAdapterBase):
    """Confirms once, then reports a contradictory later baseline read."""

    def __init__(
        self,
        action: MetaCampaignPausePlannedAction,
        *,
        ledger: Ledger,
        plan: CompositePlan,
    ) -> None:
        super().__init__(action, ledger=ledger)
        self._parent_plan = plan

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        self.apply_calls += 1
        permit.consume(
            plan_digest=self._parent_plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        self._state = RemoteState(fingerprint=plan.after.fingerprint, revision=plan.after.revision)
        return ApplyResponse(
            outcome=ApplyOutcome.CONFIRMED,
            opaque_resource_refs=(_OPAQUE,),
            evidence_refs=(_EVIDENCE,),
        )

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self.reconcile_calls += 1
        assert plan == self._action.plan.subplans[0]
        return Reconciliation(
            state=RemoteState(fingerprint=plan.before.fingerprint, revision=plan.before.revision),
            status=EffectStatus.FAILED_NO_EFFECT,
            settled=True,
            evidence_refs=(_EVIDENCE,),
        )


class _UnsettledFollowerAdapter:
    provider = "synthetic_followup"

    def __init__(self, plan: SubPlan) -> None:
        self._plan = plan
        self.apply_calls = 0
        self.reconcile_calls = 0

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        assert plan == self._plan
        return AdapterCapabilities(
            provider_api_version=plan.provider_api_version,
            capability_version=plan.capability_version,
            supported_operations=(OperationKind.PAUSE,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        account = self._plan.account
        assert isinstance(account, AccountIdentity)
        assert account_ref == account.account_ref
        return RemoteIdentity(
            stable_account_id=account.stable_account_id,
            auth_profile_ref=account.auth_profile_ref,
            principal_ref=account.principal_ref,
            client_application_ref=account.client_application_ref,
            scopes=account.scopes,
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        assert plan == self._plan
        return RemoteState(fingerprint=plan.before.fingerprint, revision=plan.before.revision)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        assert plan == self._plan
        assert idempotency_key
        self.apply_calls += 1
        return ApplyResponse(outcome=ApplyOutcome.AMBIGUOUS, evidence_refs=(_EVIDENCE,))

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        """The uniform write rule covers this synthetic follower too."""

        assert permit is not None
        return self.apply(plan, idempotency_key=idempotency_key)

    def verify(self, plan: SubPlan) -> Verification:
        raise AssertionError(f"unexpected verification for {plan.subplan_id}")

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        assert plan == self._plan
        self.reconcile_calls += 1
        return Reconciliation(
            state=RemoteState(fingerprint=plan.before.fingerprint, revision=plan.before.revision),
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )


class _SyntheticApprovalHost:
    host_ref = "ultra-review-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return grant.user_interaction_ref == "ultra-review-interaction"

    def verifies_policy_configuration(self, policy: object) -> bool:
        del policy
        return False


def _follower_subplan(meta: SubPlan) -> SubPlan:
    return SubPlan(
        subplan_id="synthetic-unsettled-followup",
        provider=_UnsettledFollowerAdapter.provider,
        operation=OperationKind.PAUSE,
        account=meta.account,
        provider_api_version="synthetic-v1",
        capability_version="synthetic-cap-v1",
        before=StateRef(fingerprint="1" * 64, revision="before"),
        after=StateRef(fingerprint="2" * 64, revision="after"),
        max_spend=Money(minor_units=0, currency="USD"),
        egress=(EgressTarget(host="synthetic.invalid", purpose="unsettled test only"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=meta.expires_at,
    )


def test_keyboard_interrupt_after_meta_start_never_creates_a_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _InterruptAfterMetaEgress(action, ledger=ledger)

    with pytest.raises(KeyboardInterrupt):
        kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert adapter.apply_calls == 1
    assert not any(
        row["payload"].get("event_type") in {"APPLY_RESULT", "VERIFICATION_RESULT"}
        for row in ledger.records("changes")
    )
    assert ledger.unresolved_starts()

    reconciled = kernel.reconcile_run(action.plan, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciled.recovery_required is True
    assert adapter.reconcile_calls == 1
    assert ledger.unresolved_starts()
    assert not any(
        row["payload"].get("event_type") == "RECONCILIATION_RESULT"
        for row in ledger.records("changes")
    )


def test_failed_meta_acknowledgement_with_target_read_remains_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _ClaimedNoEffectAtTargetAdapter(
        action,
        ledger=ledger,
        outcome=ApplyOutcome.FAILED,
        state_after_apply=True,
    )

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.recovery_required is True
    assert adapter.verify_calls == 0
    assert any(
        row["payload"].get("event_type") == "APPLY_RESULT"
        and row["payload"].get("effect_status") == EffectStatus.AMBIGUOUS_REMOTE_STATE.value
        for row in ledger.records("changes")
    )


def test_exact_meta_history_requires_one_verified_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action,
        ledger=ledger,
        outcome=ApplyOutcome.CONFIRMED,
        state_after_apply=True,
    )
    kernel.execute(action.plan, grant, {adapter.provider: adapter})
    history = ledger.records("changes")

    assert (
        _unattributable_attempt_state(
            history,
            plan=action.plan,
            authorization=grant,
            subplan=action.plan.subplans[0],
        )
        == "VERIFIED"
    )

    duplicate_result = next(
        row
        for row in history
        if row["payload"].get("event_type") == "APPLY_RESULT"
        and row["payload"].get("effect_status") == EffectStatus.APPLIED_VERIFIED.value
    )
    duplicate_history = deepcopy(history)
    duplicate_history.append(deepcopy(duplicate_result))

    assert (
        _unattributable_attempt_state(
            duplicate_history,
            plan=action.plan,
            authorization=grant,
            subplan=action.plan.subplans[0],
        )
        == "UNSETTLED"
    )


def test_sparse_meta_verification_evidence_never_settles_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action,
        ledger=ledger,
        outcome=ApplyOutcome.CONFIRMED,
        state_after_apply=True,
    )
    kernel.execute(action.plan, grant, {adapter.provider: adapter})
    sparse_history = deepcopy(ledger.records("changes"))
    verification_index = next(
        index
        for index, row in enumerate(sparse_history)
        if row["payload"].get("event_type") == "VERIFICATION_RESULT"
    )
    verification = sparse_history[verification_index]
    sparse_history[verification_index] = {
        **verification,
        "payload": {**verification["payload"], "evidence_refs": []},
    }

    assert (
        _unattributable_attempt_state(
            sparse_history,
            plan=action.plan,
            authorization=grant,
            subplan=action.plan.subplans[0],
        )
        == "UNSETTLED"
    )


def test_later_baseline_read_cannot_rewrite_verified_meta_attempt_as_no_effect(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    action = _action(ledger.profile_ref)
    meta = action.plan.subplans[0]
    follower = _follower_subplan(meta)
    plan = CompositePlan(
        plan_id="ultra-meta-confirmed-then-unsettled",
        profile_ref=action.plan.profile_ref,
        subplans=(meta, follower),
        expires_at=meta.expires_at,
    )
    grant = new_direct_grant(
        authorization_id="ultra-meta-confirmed-grant",
        plan=plan,
        grantor_ref="synthetic-grantor",
        host_ref=_SyntheticApprovalHost.host_ref,
        user_interaction_ref="ultra-review-interaction",
        expires_at=_NOW + timedelta(minutes=5),
    )
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: _NOW)
    kernel.record_direct_grant(plan, grant, _SyntheticApprovalHost())
    meta_adapter = _MultiRunMetaAdapter(action, ledger=ledger, plan=plan)
    follower_adapter = _UnsettledFollowerAdapter(follower)

    initial = kernel.execute(
        plan,
        grant,
        {meta_adapter.provider: meta_adapter, follower_adapter.provider: follower_adapter},
    )
    assert tuple(item.status for item in initial.results) == (
        EffectStatus.APPLIED_VERIFIED,
        EffectStatus.AMBIGUOUS_REMOTE_STATE,
    )
    assert ledger.unresolved_runs()

    reconciled = kernel.reconcile_run(
        plan,
        grant,
        {meta_adapter.provider: meta_adapter, follower_adapter.provider: follower_adapter},
    )

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciled.recovery_required is True
    assert any(
        row["payload"].get("event_type") == "APPLY_RESULT"
        and row["payload"].get("subplan_digest") == meta.subplan_digest
        and row["payload"].get("effect_status") == EffectStatus.APPLIED_VERIFIED.value
        for row in ledger.records("changes")
    )
    assert not any(
        row["payload"].get("event_type") == "RECONCILIATION_RESULT"
        and row["payload"].get("subplan_digest") == meta.subplan_digest
        for row in ledger.records("changes")
    )

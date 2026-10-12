"""Only an active kernel call can consume its own one-use adapter permit."""

import copy
import pickle
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import timedelta
from pathlib import Path

import pytest
from test_account_key_identity import AccountKeyAdapter, _plan
from test_safety import NOW, FakeAdapter, FakeApprovalHost, delegated, direct_grant, kernel
from test_safety import plan as _google_plan

from adhelm.canonical import digest
from adhelm.egress_permit import EgressPermit, _kernel_permit
from adhelm.errors import AdhelmError
from adhelm.safety import (
    AdapterCapabilities,
    ApplyResponse,
    OperationKind,
    Reconciliation,
    RemoteState,
    SubPlan,
    Verification,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_BINDING = {"plan_digest": "a" * 64, "subplan_digest": "b" * 64, "attempt_ref": "c" * 64}


def test_permits_cannot_be_constructed_serialized_copied_or_used_outside_scope() -> None:
    with pytest.raises(TypeError):
        EgressPermit()
    forged = object.__new__(EgressPermit)
    with pytest.raises(AdhelmError):
        forged.consume(**_BINDING)
    with _kernel_permit(**_BINDING, check_current=lambda: None) as permit:
        for method in (pickle.dumps, copy.copy, copy.deepcopy):
            with pytest.raises(TypeError):
                method(permit)
        with pytest.raises(AdhelmError):
            forged.consume(**_BINDING)
        assert repr(permit) == "<EgressPermit>"
        captured = copy_context()
    # A copied context still points to an explicitly invalidated state.
    with pytest.raises(AdhelmError):
        captured.run(permit.consume, **_BINDING)
    with pytest.raises(AdhelmError):
        permit.consume(**_BINDING)


@pytest.mark.parametrize("field", list(_BINDING))
def test_permit_is_bound_to_composite_subplan_and_attempt(field: str) -> None:
    with _kernel_permit(**_BINDING, check_current=lambda: None) as permit:
        with pytest.raises(AdhelmError) as error:
            permit.consume(**{**_BINDING, field: "d" * 64})
        assert error.value.code == "KERNEL_EGRESS_PERMIT_REQUIRED"
        permit.consume(**_BINDING)
        with pytest.raises(AdhelmError):
            permit.consume(**_BINDING)


def test_permit_cannot_escape_after_an_exception() -> None:
    with pytest.raises(RuntimeError):
        with _kernel_permit(**_BINDING, check_current=lambda: None) as permit:
            captured = copy_context()
            raise RuntimeError("synthetic interruption")
    with pytest.raises(AdhelmError):
        captured.run(permit.consume, **_BINDING)


def test_competing_copied_contexts_cannot_consume_twice() -> None:
    def claim(permit: EgressPermit) -> bool:
        try:
            permit.consume(**_BINDING)
        except AdhelmError:
            return False
        return True

    with _kernel_permit(**_BINDING, check_current=lambda: None) as permit:
        contexts = [copy_context(), copy_context()]
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda context: context.run(claim, permit), contexts))
        assert sorted(outcomes) == [False, True]


def test_kernel_issues_only_after_durable_start_and_invalidates_returned_permit(
    tmp_path: Path,
) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 1  # FakeAdapter asserts durable start before its effect.
    assert adapter.last_permit is not None
    attempt = digest({"plan": compiled.plan_digest, "subplan": compiled.subplans[0].subplan_digest})
    with pytest.raises(AdhelmError):
        adapter.last_permit.consume(
            plan_digest=compiled.plan_digest,
            subplan_digest=compiled.subplans[0].subplan_digest,
            attempt_ref=attempt,
        )


def test_failed_start_append_issues_no_permit_or_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    original = ledger.append

    def fail_start(stream, record_kind, payload, **kwargs):
        if payload.get("event_type") == "APPLY_STARTED":
            raise OSError("synthetic durable append failure")
        return original(stream, record_kind, payload, **kwargs)

    monkeypatch.setattr(ledger, "append", fail_start)
    with pytest.raises(OSError):
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0 and adapter.last_permit is None


class _LegacyUnguardedAdapter:
    """The pre-generalization adapter surface: no ``apply_guarded`` at all.

    It is a standalone class rather than a subclass because ``GuardedAdapter``
    is a runtime-checkable protocol: an inherited ``apply_guarded`` cannot be
    removed by overriding it.
    """

    def __init__(self, inner: object, provider: str) -> None:
        self._inner = inner
        self.provider = provider
        self.apply_calls = 0

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        return self._inner.capabilities(plan)

    def read_identity(self, account_ref: str) -> object:
        return self._inner.read_identity(account_ref)

    def read_state(self, plan: SubPlan) -> RemoteState:
        return self._inner.read_state(plan)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        self.apply_calls += 1
        return self._inner.apply(plan, idempotency_key=idempotency_key)

    def verify(self, plan: SubPlan) -> Verification:
        return self._inner.verify(plan)

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        return self._inner.reconcile(plan)


@pytest.mark.parametrize("provider", ["openai_ads", "google_ads"])
def test_every_mutating_plan_refuses_a_legacy_unguarded_adapter_before_start(
    tmp_path: Path, provider: str
) -> None:
    """The guarded requirement is a uniform write rule, not a provider allowlist.

    ``openai_ads`` carries account-key evidence and ``google_ads`` carries an
    ordinary scoped identity; neither is one of the two operations the kernel
    used to name explicitly, and both are refused before any read or start.
    """

    instance, ledger = kernel(tmp_path)
    compiled = _plan() if provider == "openai_ads" else _google_plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    inner = AccountKeyAdapter() if provider == "openai_ads" else FakeAdapter()
    inner.ledger = ledger
    adapter = _LegacyUnguardedAdapter(inner, provider)
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})

    assert error.value.code == "ADAPTER_PERMIT_REQUIRED"
    assert adapter.apply_calls == 0
    assert ledger.unresolved_starts() == []
    assert ledger.checkpoint()["checkpoint_digest"] == before


@pytest.mark.parametrize("expires", ["grant", "plan"])
def test_expiry_after_dispatch_begins_is_rechecked_at_permit_consumption(
    tmp_path: Path, expires: str
) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    if expires == "plan":
        grant = grant.model_copy(update={"expires_at": NOW + timedelta(hours=2)})
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())

    class SlowReadAdapter(AccountKeyAdapter):
        def apply_guarded(self, plan, *, idempotency_key, permit):
            # Model slow identity/state reads after APPLY_STARTED, before POST.
            instance.clock = lambda: NOW + timedelta(minutes=11 if expires == "grant" else 61)
            return super().apply_guarded(plan, idempotency_key=idempotency_key, permit=permit)

    adapter = SlowReadAdapter()
    adapter.ledger = ledger
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0
    assert result.recovery_required is True
    assert ledger.unresolved_starts()


def test_expiry_rejection_permanently_invalidates_even_unconsumed_permit() -> None:
    expired = True

    def check_current() -> None:
        if expired:
            raise AdhelmError("AUTHORIZATION_EXPIRED", "Synthetic expired authorization.")

    with _kernel_permit(**_BINDING, check_current=check_current) as permit:
        with pytest.raises(AdhelmError) as first:
            permit.consume(**_BINDING)
        assert first.value.code == "AUTHORIZATION_EXPIRED"
        expired = False
        with pytest.raises(AdhelmError) as second:
            permit.consume(**_BINDING)
        assert second.value.code == "KERNEL_EGRESS_PERMIT_REQUIRED"


def test_delegation_policy_expiry_during_adapter_reads_prevents_egress(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = delegated(compiled, allowed=(OperationKind.PAUSE,))
    grant = grant.model_copy(
        update={
            "policy": grant.policy.model_copy(update={"expires_at": NOW + timedelta(minutes=10)})
        }
    )
    instance.install_delegation_policy(grant.policy, FakeApprovalHost())

    class SlowReadAdapter(AccountKeyAdapter):
        def apply_guarded(self, plan, *, idempotency_key, permit):
            instance.clock = lambda: NOW + timedelta(minutes=11)
            return super().apply_guarded(plan, idempotency_key=idempotency_key, permit=permit)

    adapter = SlowReadAdapter()
    adapter.ledger = ledger
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0
    assert result.recovery_required is True
    assert ledger.unresolved_starts()

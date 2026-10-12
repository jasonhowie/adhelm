"""Adversarial account-key identity and permit regressions."""

from __future__ import annotations

from contextvars import Context, copy_context
from pathlib import Path

import pytest
from test_account_key_identity import AccountKeyAdapter, _account, _plan
from test_safety import FakeAdapter, FakeApprovalHost, direct_grant, kernel, plan

from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.safety import (
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    OpenAIAccountKeyIdentity,
    RemoteOpenAIAccountKeyIdentity,
    SubPlan,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def test_constructed_account_key_plan_fails_before_authority_or_adapter_access(
    tmp_path: Path,
) -> None:
    valid = _plan()
    forged_account = OpenAIAccountKeyIdentity.model_construct(
        **{**_account().__dict__, "provider_scope_status": "VERIFIED"}
    )
    forged_child = SubPlan.model_construct(
        **{**valid.subplans[0].__dict__, "account": forged_account}
    )
    forged_plan = CompositePlan.model_construct(**{**valid.__dict__, "subplans": (forged_child,)})
    instance, ledger = kernel(tmp_path)
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger

    with pytest.raises(AdhelmError) as raised:
        instance.execute(forged_plan, direct_grant(valid), {adapter.provider: adapter})

    assert raised.value.code == "INVALID_SAFETY_INPUT"
    assert adapter.identity_reads == adapter.apply_calls == 0
    assert ledger.records("changes") == []


def test_scoped_plan_cannot_accept_an_account_key_remote_identity(tmp_path: Path) -> None:
    class KeyIdentityForScopedAdapter(FakeAdapter):
        def read_identity(self, account_ref: str) -> RemoteOpenAIAccountKeyIdentity:
            assert account_ref == "alias-account"
            return RemoteOpenAIAccountKeyIdentity(
                stable_account_id="stable-account",
                auth_profile_ref="edit-profile",
                credential_binding_ref="credential-binding:sha256:" + "c" * 64,
            )

    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = KeyIdentityForScopedAdapter()
    adapter.ledger = ledger

    with pytest.raises(AdhelmError) as raised:
        instance.execute(compiled, grant, {adapter.provider: adapter})

    assert raised.value.code == "ACCOUNT_IDENTITY_DRIFT"
    assert adapter.apply_calls == 0
    assert ledger.unresolved_starts() == []


@pytest.mark.parametrize("raise_during_dispatch", (False, True))
def test_copied_context_cannot_consume_a_permit_after_guarded_dispatch_ends(
    tmp_path: Path, raise_during_dispatch: bool
) -> None:
    class DelayedPermitAdapter(AccountKeyAdapter):
        captured_context: Context | None = None
        captured_permit: EgressPermit | None = None
        binding: dict[str, str] | None = None
        dispatch_returned = False
        delayed_error: AdhelmError | None = None

        def __init__(self) -> None:
            super().__init__()
            self.raise_during_dispatch = raise_during_dispatch

        def apply_guarded(
            self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
        ) -> ApplyResponse:
            self.captured_context = copy_context()
            self.captured_permit = permit
            self.binding = {
                "plan_digest": self.compiled_plan_digest,
                "subplan_digest": plan.subplan_digest,
                "attempt_ref": idempotency_key,
            }
            self.dispatch_returned = True
            if self.raise_during_dispatch:
                raise RuntimeError("synthetic guarded-dispatch failure")
            # This synthetic adapter deliberately does not consume the permit
            # or make a remote change. The subsequent readback attempts the
            # delayed claim captured during dispatch.
            return ApplyResponse(outcome=ApplyOutcome.AMBIGUOUS)

        def read_state(self, plan: SubPlan):
            if self.dispatch_returned:
                assert self.captured_context is not None
                assert self.captured_permit is not None
                assert self.binding is not None
                try:
                    self.captured_context.run(self.captured_permit.consume, **self.binding)
                except AdhelmError as exc:
                    self.delayed_error = exc
                else:
                    self.delayed_error = None
            return super().read_state(plan)

    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = DelayedPermitAdapter()
    adapter.compiled_plan_digest = compiled.plan_digest
    adapter.ledger = ledger

    instance.execute(compiled, grant, {adapter.provider: adapter})

    assert adapter.delayed_error is not None
    assert adapter.delayed_error.code == "KERNEL_EGRESS_PERMIT_REQUIRED"
    assert adapter.apply_calls == 0

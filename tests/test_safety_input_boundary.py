"""Invalid in-process value objects must fail before host, ledger, or provider work."""

from pathlib import Path

import pytest
from test_safety import FakeAdapter, FakeApprovalHost, delegated, direct_grant, kernel, plan

from adhelm.errors import AdhelmError
from adhelm.safety import DelegationPolicySource, DirectGrantSource

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class CountingHost(FakeApprovalHost):
    calls = 0

    def verifies_direct_grant(self, grant):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().verifies_direct_grant(grant)

    def verifies_policy_configuration(self, policy):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().verifies_policy_configuration(policy)


def test_agent_asserted_grant_cannot_reach_trusted_host_or_ledger(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled).model_copy(update={"source": DirectGrantSource.AGENT_ASSERTION})
    host = CountingHost()
    with pytest.raises(AdhelmError, match="safety contract"):
        instance.record_direct_grant(compiled, grant, host)
    assert host.calls == 0 and ledger.records("changes") == []


@pytest.mark.parametrize("operation", ["install_delegation_policy", "revoke_delegation_policy"])
def test_agent_proposed_policy_cannot_reach_host_or_history(tmp_path: Path, operation: str) -> None:
    instance, ledger = kernel(tmp_path)
    policy = delegated(plan()).policy.model_copy(
        update={"source": DelegationPolicySource.AGENT_PROPOSAL}
    )
    host = CountingHost()
    with pytest.raises(AdhelmError, match="safety contract"):
        getattr(instance, operation)(policy, host)
    assert host.calls == 0 and ledger.records("changes") == []


def test_negative_nested_spend_is_rejected_before_reserving_a_run(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    subplan = compiled.subplans[0]
    forged = compiled.model_copy(
        update={
            "subplans": (
                subplan.model_copy(
                    update={"max_spend": subplan.max_spend.model_copy(update={"minor_units": -1})}
                ),
            )
        }
    )
    grant = delegated(forged)
    instance.install_delegation_policy(grant.policy, FakeApprovalHost())
    before = ledger.checkpoint()["checkpoint_digest"]
    adapter = FakeAdapter()
    adapter.ledger = ledger
    with pytest.raises(AdhelmError, match="safety contract"):
        instance.execute(forged, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0
    assert ledger.checkpoint()["checkpoint_digest"] == before
    assert not ledger.unresolved_runs()


def test_reconciliation_rejects_bypassed_plan_before_any_ledger_lookup(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan().model_copy(update={"subplans": ()})
    with pytest.raises(AdhelmError, match="safety contract"):
        instance.reconcile_run(compiled, delegated(compiled), {})
    assert ledger.records("changes") == []

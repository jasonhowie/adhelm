"""Independent regressions for the public safety-kernel input boundary."""

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path

import pytest
from test_safety import NOW, FakeAdapter, FakeApprovalHost, delegated, direct_grant, kernel, plan

from adhelm.errors import AdhelmError
from adhelm.safety import (
    DelegationPolicySource,
    DirectGrant,
    DirectGrantSource,
    MoneyLimit,
    SafetyKernel,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class CountingHost(FakeApprovalHost):
    direct_calls = 0
    policy_calls = 0

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        self.direct_calls += 1
        return super().verifies_direct_grant(grant)

    def verifies_policy_configuration(self, policy: object) -> bool:
        self.policy_calls += 1
        return super().verifies_policy_configuration(policy)  # type: ignore[arg-type]


class CountingAdapter(FakeAdapter):
    identity_reads = 0

    def read_identity(self, account_ref: str):  # type: ignore[no-untyped-def]
        self.identity_reads += 1
        return super().read_identity(account_ref)


class ExplodingLedger:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"invalid input must not access ledger.{name}")


class NoOffsetTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None):  # type: ignore[override]
        return None

    def dst(self, dt: datetime | None):  # type: ignore[override]
        return None

    def tzname(self, dt: datetime | None):  # type: ignore[override]
        return "no-offset"


def test_model_construct_agent_assertion_cannot_reach_direct_approval_host(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    valid = direct_grant(compiled)
    forged = DirectGrant.model_construct(
        **{**valid.model_dump(mode="python"), "source": DirectGrantSource.AGENT_ASSERTION}
    )
    host = CountingHost()

    with pytest.raises(AdhelmError) as raised:
        instance.record_direct_grant(compiled, forged, host)

    assert raised.value.code == "INVALID_SAFETY_INPUT"
    assert host.direct_calls == 0
    assert ledger.records("changes") == []


def test_wrong_plan_direct_grant_is_rejected_before_approval_host(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    other = compiled.model_copy(update={"plan_id": "other-plan"})
    grant = direct_grant(other)
    host = CountingHost()

    with pytest.raises(AdhelmError) as raised:
        instance.record_direct_grant(compiled, grant, host)

    assert raised.value.code == "AUTHORIZATION_PLAN_MISMATCH"
    assert host.direct_calls == 0
    assert ledger.records("changes") == []


@pytest.mark.parametrize("operation", ["install_delegation_policy", "revoke_delegation_policy"])
def test_naive_policy_revocation_is_rejected_before_host_or_history(
    tmp_path: Path, operation: str
) -> None:
    instance, ledger = kernel(tmp_path)
    policy = delegated(plan()).policy.model_copy(update={"revoked_at": datetime(2030, 1, 1)})
    host = CountingHost()

    with pytest.raises(AdhelmError) as raised:
        getattr(instance, operation)(policy, host)

    assert raised.value.code == "INVALID_SAFETY_INPUT"
    assert host.policy_calls == 0
    assert ledger.records("changes") == []


@pytest.mark.parametrize("field", ["effective_from", "expires_at"])
def test_non_offset_policy_timestamp_is_rejected_before_policy_host(
    tmp_path: Path, field: str
) -> None:
    instance, ledger = kernel(tmp_path)
    policy = delegated(plan()).policy.model_copy(
        update={field: datetime(2030, 1, 1, tzinfo=NoOffsetTimezone())}
    )
    host = CountingHost()

    with pytest.raises(AdhelmError) as raised:
        instance.install_delegation_policy(policy, host)

    assert raised.value.code == "INVALID_SAFETY_INPUT"
    assert host.policy_calls == 0
    assert ledger.records("changes") == []


def test_model_construct_nested_policy_limit_cannot_reach_policy_host(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    valid = delegated(plan()).policy
    invalid_limit = MoneyLimit.model_construct(currency="USD", maximum_minor_units=-1)
    forged = valid.model_construct(
        **{
            **valid.__dict__,
            "source": DelegationPolicySource.USER_CONTROLLED_CONFIGURATION,
            "maximum_change_per_operation": (invalid_limit,),
        }
    )
    host = CountingHost()

    with pytest.raises(AdhelmError) as raised:
        instance.install_delegation_policy(forged, host)

    assert raised.value.code == "INVALID_SAFETY_INPUT"
    assert host.policy_calls == 0
    assert ledger.records("changes") == []


def test_model_construct_invalid_nested_plan_cannot_reach_reconciliation_adapter(
    tmp_path: Path,
) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    subplan = compiled.subplans[0]
    forged_subplan = subplan.model_construct(
        **{
            **subplan.__dict__,
            "max_spend": subplan.max_spend.model_construct(minor_units=-1, currency="USD"),
        }
    )
    forged_plan = compiled.model_construct(**{**compiled.__dict__, "subplans": (forged_subplan,)})
    adapter = CountingAdapter()
    adapter.ledger = ledger

    with pytest.raises(AdhelmError) as raised:
        instance.reconcile_run(forged_plan, direct_grant(compiled), {adapter.provider: adapter})

    assert raised.value.code == "INVALID_SAFETY_INPUT"
    assert adapter.identity_reads == 0
    assert ledger.records("changes") == []


def test_invalid_execution_plan_cannot_lookup_ledger_before_rejection(tmp_path: Path) -> None:
    compiled = plan()
    subplan = compiled.subplans[0]
    forged = compiled.model_construct(
        **{
            **compiled.__dict__,
            "subplans": (
                subplan.model_construct(
                    **{
                        **subplan.__dict__,
                        "max_spend": subplan.max_spend.model_construct(
                            minor_units=-1, currency="USD"
                        ),
                    }
                ),
            ),
        }
    )
    instance = SafetyKernel(tmp_path, ExplodingLedger(), clock=lambda: NOW)

    with pytest.raises(AdhelmError) as raised:
        instance.execute(forged, direct_grant(compiled), {})

    assert raised.value.code == "INVALID_SAFETY_INPUT"

"""A confirmed acknowledgement counts only when the adapter claimed its permit."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_kernel_child_egress import (
    EVIDENCE,
    _child_records,
    _execute,
    _GuardedOnlyAdapter,
    _HierarchyAdapter,
)

from adhelm.egress_permit import EgressPermit
from adhelm.safety import ApplyOutcome, ApplyResponse, EffectStatus, KernelExecution, SubPlan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _UnclaimingSingleRequestAdapter(_GuardedOnlyAdapter):
    """Returns CONFIRMED and moves state to the target without claiming the permit."""

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        del plan, idempotency_key, permit
        self.state = "after"
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))


class _UnclaimingHierarchyAdapter(_HierarchyAdapter):
    """Journals every child and reports CONFIRMED, but never consumes a child permit."""

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse:
        for index, label in zip(self._indexes, self._labels, strict=True):
            with execution.begin_child(index, label):
                self.children.append(index)
                execution.report_child(
                    index,
                    ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,)),
                )
        self.state = "after"
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))


def test_a_confirmed_result_without_a_claimed_permit_is_ambiguous(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, _UnclaimingSingleRequestAdapter, children=1
    )
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    subplan_result = result.results[0]
    assert subplan_result.status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert subplan_result.diagnostic_code == "GUARDED_EGRESS_NOT_CLAIMED"
    assert ledger.unresolved_starts(), "an unclaimed write must keep blocking"


def test_a_child_reported_confirmed_without_a_claimed_permit_stays_unresolved(
    tmp_path: Path,
) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, _UnclaimingHierarchyAdapter, children=3
    )
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    results = [
        record for record in _child_records(ledger) if record["record_kind"] == "CHILD_APPLY_RESULT"
    ]
    assert len(results) == 3
    assert all(record["payload"]["effect_status"] == "AMBIGUOUS_REMOTE_STATE" for record in results)
    assert ledger.unresolved_child_starts()

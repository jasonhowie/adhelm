"""Adversarial regressions for journaled multi-step child egress.

One approved provider operation is often several dependent requests.  These
tests exercise the kernel's child-egress journal directly with in-memory fakes:
no provider transport, credential, or network call is involved.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest
from pydantic import ValidationError
from test_safety import FakeApprovalHost, direct_grant

from adhelm.canonical import digest
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.privacy import validate_data
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    EffectStatus,
    EgressTarget,
    KernelExecution,
    Money,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    SafetyKernel,
    StateRef,
    SubPlan,
    Verification,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

NOW = datetime(2030, 1, 1, tzinfo=UTC)
BEFORE = "a" * 64
AFTER = "b" * 64
EVIDENCE = "evidence:sha256:" + "d" * 64
LABELS = ("create-campaign", "create-ad-set", "create-ad")


def _subplan(*, children: int = 3) -> SubPlan:
    return SubPlan(
        subplan_id="meta-create-hierarchy",
        provider="meta_ads",
        operation=OperationKind.CREATE,
        account=AccountIdentity(
            account_ref="alias-account",
            stable_account_id="stable-account",
            auth_profile_ref="edit-profile",
            principal_ref="principal-1",
            client_application_ref="client-1",
            scopes=("ads.write",),
        ),
        provider_api_version="v1",
        capability_version="cap-v1",
        before=StateRef(fingerprint=BEFORE, revision="r1"),
        after=StateRef(fingerprint=AFTER, revision="r1"),
        max_spend=Money(minor_units=100, currency="USD"),
        egress=(EgressTarget(host="graph.meta.test", purpose="paused hierarchy creation"),),
        recovery_actions=(OperationKind.PAUSE,),
        expected_child_egress_count=children,
        expires_at=NOW + timedelta(hours=1),
    )


def _plan(*, children: int = 3, plan_id: str = "child-plan") -> CompositePlan:
    return CompositePlan(
        plan_id=plan_id,
        profile_ref="test-profile",
        subplans=(_subplan(children=children),),
        expires_at=NOW + timedelta(hours=1),
    )


def _kernel(
    tmp_path: Path, *, clock: Callable[[], datetime] | None = None
) -> tuple[SafetyKernel, Ledger]:
    ledger = Ledger(tmp_path, profile_ref="test-profile")
    return SafetyKernel(tmp_path, ledger, clock=clock or (lambda: NOW)), ledger


class _GuardedOnlyAdapter:
    """A guarded single-request adapter with no journaled child interface."""

    provider = "meta_ads"

    def __init__(
        self,
        plan: CompositePlan,
        *,
        labels: Sequence[str] = LABELS,
        indexes: Sequence[int] | None = None,
        outcomes: Sequence[ApplyOutcome] | None = None,
        report: bool = True,
        no_effect_proven: bool = False,
        crash_at: int | None = None,
        reuse_permit: bool = False,
        state: str = BEFORE,
    ) -> None:
        self._plan = plan
        self._labels = tuple(labels)
        self._indexes = tuple(indexes or range(1, len(self._labels) + 1))
        self._outcomes = tuple(outcomes or (ApplyOutcome.CONFIRMED,) * len(self._labels))
        self._report = report
        self._no_effect_proven = no_effect_proven
        self._crash_at = crash_at
        self._reuse_permit = reuse_permit
        self.state = state
        self.children: list[int] = []
        self.refusals: list[str] = []
        self.reused: EgressPermit | None = None

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        del plan
        return AdapterCapabilities(
            provider_api_version="v1",
            capability_version="cap-v1",
            supported_operations=(OperationKind.CREATE,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        assert account_ref == "alias-account"
        return RemoteIdentity(
            stable_account_id="stable-account",
            auth_profile_ref="edit-profile",
            principal_ref="principal-1",
            client_application_ref="client-1",
            scopes=("ads.write",),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        del plan
        return RemoteState(fingerprint=self.state, revision="r1")

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError("PERMIT_REQUIRED", "This hierarchy needs journaled child egress.")

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        del plan, idempotency_key, permit
        raise AdhelmError("CHILD_EGRESS_REQUIRED", "This hierarchy needs journaled child egress.")

    def verify(self, plan: SubPlan) -> Verification:
        del plan
        return Verification(verified=True, evidence_refs=(EVIDENCE,))

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        del plan
        return Reconciliation(
            state=RemoteState(fingerprint=self.state, revision="r1"),
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )


class _HierarchyAdapter(_GuardedOnlyAdapter):
    """A guarded adapter whose single approved operation is several requests."""

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse:
        assert execution.expected_child_egress_count == plan.expected_child_egress_count
        assert execution.attempt_ref == idempotency_key
        assert execution.subplan_digest == plan.subplan_digest
        for position, (index, label) in enumerate(zip(self._indexes, self._labels, strict=True)):
            try:
                with execution.begin_child(index, label) as permit:
                    permit.consume(
                        plan_digest=execution.plan_digest,
                        subplan_digest=plan.subplan_digest,
                        attempt_ref=idempotency_key,
                        child_index=index,
                    )
                    if self._reuse_permit:
                        self.reused = permit
                    self.children.append(index)
                    if position == len(self._labels) - 1:
                        self.state = AFTER
                    if self._crash_at == index:
                        raise RuntimeError("synthetic transport loss inside a child request")
                    if self._report:
                        outcome = self._outcomes[position]
                        execution.report_child(
                            index,
                            ApplyResponse(
                                outcome=outcome,
                                no_effect_proven=self._no_effect_proven
                                and outcome is ApplyOutcome.FAILED,
                                evidence_refs=(EVIDENCE,),
                            ),
                        )
            except AdhelmError as error:
                # Record every refusal raised inside a child scope so the tests
                # can assert the exact diagnostic, not only the subplan outcome.
                self.refusals.append(error.code)
                raise
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))


def _execute(
    tmp_path: Path,
    adapter_factory: Callable[[CompositePlan], _HierarchyAdapter],
    *,
    children: int = 3,
    clock: Callable[[], datetime] | None = None,
) -> tuple[SafetyKernel, Ledger, CompositePlan, _HierarchyAdapter, object]:
    instance, ledger = _kernel(tmp_path, clock=clock)
    compiled = _plan(children=children)
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = adapter_factory(compiled)
    return instance, ledger, compiled, adapter, grant


def _child_records(ledger: Ledger) -> list[dict[str, object]]:
    return [
        record
        for record in ledger.records("changes")
        if record["record_kind"] in {"CHILD_APPLY_STARTED", "CHILD_APPLY_RESULT"}
    ]


def test_child_count_below_one_is_rejected_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        _subplan(children=0)


def test_single_request_subplans_keep_their_exact_retained_wire_and_digest() -> None:
    single = _subplan(children=1)
    wire = single.model_dump(mode="json")
    assert "expected_child_egress_count" not in wire
    assert single.subplan_digest == digest(wire)
    assert _subplan(children=2).model_dump(mode="json")["expected_child_egress_count"] == 2


def test_ordered_children_are_journaled_around_every_permit(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _HierarchyAdapter)

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert adapter.children == [1, 2, 3]
    kinds = [
        (record["record_kind"], record["payload"].get("child_index"))
        for record in ledger.records("changes")
        if record["record_kind"].startswith("CHILD_")
        or record["payload"].get("event_type") == "APPLY_STARTED"
    ]
    # The composite-level APPLY_STARTED still precedes the first child.
    assert kinds == [
        ("CHANGE_EVENT", None),
        ("CHILD_APPLY_STARTED", 1),
        ("CHILD_APPLY_RESULT", 1),
        ("CHILD_APPLY_STARTED", 2),
        ("CHILD_APPLY_RESULT", 2),
        ("CHILD_APPLY_STARTED", 3),
        ("CHILD_APPLY_RESULT", 3),
    ]
    assert ledger.unresolved_child_starts() == []


def test_out_of_order_child_index_is_refused_before_its_permit(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, indexes=(1, 3, 2))
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.children == [1]
    assert adapter.refusals == ["CHILD_EGRESS_OUT_OF_ORDER"]
    started = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_STARTED"]
    assert [record["payload"]["child_index"] for record in started] == [1]


def test_more_children_than_approved_are_refused(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path,
        lambda plan: _HierarchyAdapter(plan, labels=("one", "two", "three")),
        children=2,
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.children == [1, 2]
    assert adapter.refusals == ["CHILD_EGRESS_COUNT_EXCEEDED"]
    assert [record["payload"]["child_index"] for record in _child_records(ledger)] == [1, 1, 2, 2]


def test_fewer_children_than_approved_cannot_verify_the_subplan(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, labels=("one", "two")), children=3
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.children == [1, 2]


def test_ambiguous_child_transport_stays_ambiguous_and_blocks_retry(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path,
        lambda plan: _HierarchyAdapter(
            plan,
            outcomes=(ApplyOutcome.CONFIRMED, ApplyOutcome.AMBIGUOUS, ApplyOutcome.CONFIRMED),
        ),
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    unresolved = ledger.unresolved_child_starts()
    assert [record["payload"]["child_index"] for record in unresolved] == [2]
    retry_plan = _plan(plan_id="retry-plan")
    retry_grant = direct_grant(retry_plan)
    instance.record_direct_grant(retry_plan, retry_grant, FakeApprovalHost())
    with pytest.raises(AdhelmError) as error:
        instance.execute(retry_plan, retry_grant, {adapter.provider: adapter})
    assert error.value.code == "UNRESOLVED_CHILD_APPLY"


def test_unreported_child_is_recorded_ambiguous_and_never_resolves(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, report=False)
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    results = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_RESULT"]
    assert {record["payload"]["effect_status"] for record in results} == {"AMBIGUOUS_REMOTE_STATE"}
    assert len(ledger.unresolved_child_starts()) == 3


def test_restart_after_a_lost_child_result_blocks_writes_until_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable child start with no result survives a restart and fails closed."""

    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, crash_at=2)
    )
    original = ledger.append

    def lose_second_child_result(
        stream: str, record_kind: str, payload: dict[str, object], **kwargs: object
    ) -> object:
        if record_kind == "CHILD_APPLY_RESULT" and payload["child_index"] == 2:
            raise OSError("synthetic power loss before the child result was durable")
        return original(stream, record_kind, payload, **kwargs)

    monkeypatch.setattr(ledger, "append", lose_second_child_result)
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE

    # Restart: a fresh ledger and kernel read the same durable stream files.
    restarted_ledger = Ledger(tmp_path, profile_ref="test-profile")
    restarted = SafetyKernel(tmp_path, restarted_ledger, clock=lambda: NOW)
    unresolved = restarted_ledger.unresolved_child_starts()
    assert [record["payload"]["child_index"] for record in unresolved] == [2]

    retry_plan = _plan(plan_id="retry-plan")
    retry_grant = direct_grant(retry_plan)
    with pytest.raises(AdhelmError) as error:
        restarted.execute(retry_plan, retry_grant, {adapter.provider: adapter})
    assert error.value.code == "UNRESOLVED_CHILD_APPLY"
    assert adapter.children == [1, 2]


def test_reconciling_the_attempt_resolves_its_journaled_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SettledAdapter(_HierarchyAdapter):
        def reconcile(self, plan: SubPlan) -> Reconciliation:
            return Reconciliation(
                state=RemoteState(fingerprint=plan.before.fingerprint, revision="r1"),
                status=EffectStatus.FAILED_NO_EFFECT,
                settled=True,
                evidence_refs=(EVIDENCE,),
            )

    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _SettledAdapter(plan, crash_at=2)
    )
    original = ledger.append

    def lose_second_child_result(
        stream: str, record_kind: str, payload: dict[str, object], **kwargs: object
    ) -> object:
        if record_kind == "CHILD_APPLY_RESULT" and payload["child_index"] == 2:
            raise OSError("synthetic power loss before the child result was durable")
        return original(stream, record_kind, payload, **kwargs)

    monkeypatch.setattr(ledger, "append", lose_second_child_result)
    instance.execute(compiled, grant, {adapter.provider: adapter})
    monkeypatch.undo()

    assert len(ledger.unresolved_child_starts()) == 1
    adapter.state = BEFORE
    instance.reconcile_run(compiled, grant, {adapter.provider: adapter})
    assert ledger.unresolved_child_starts() == []


def test_child_permit_cannot_be_reused_after_its_scope_closes(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, reuse_permit=True)
    )

    instance.execute(compiled, grant, {adapter.provider: adapter})

    assert adapter.reused is not None
    attempt = digest({"plan": compiled.plan_digest, "subplan": compiled.subplans[0].subplan_digest})
    with pytest.raises(AdhelmError) as error:
        adapter.reused.consume(
            plan_digest=compiled.plan_digest,
            subplan_digest=compiled.subplans[0].subplan_digest,
            attempt_ref=attempt,
            child_index=3,
        )
    assert error.value.code == "KERNEL_EGRESS_PERMIT_REQUIRED"


def test_child_permit_cannot_be_claimed_for_another_child_index(tmp_path: Path) -> None:
    class _WrongIndexAdapter(_HierarchyAdapter):
        def apply_children(
            self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
        ) -> ApplyResponse:
            with execution.begin_child(1, "create-campaign") as permit:
                with pytest.raises(AdhelmError) as error:
                    permit.consume(
                        plan_digest=self._plan.plan_digest,
                        subplan_digest=plan.subplan_digest,
                        attempt_ref=idempotency_key,
                        child_index=2,
                    )
                assert error.value.code == "KERNEL_EGRESS_PERMIT_REQUIRED"
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))

    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _WrongIndexAdapter)

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE


def test_concurrent_child_permits_for_one_subplan_have_one_winner(tmp_path: Path) -> None:
    outcomes: list[str] = []

    class _ConcurrentAdapter(_HierarchyAdapter):
        def apply_children(
            self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
        ) -> ApplyResponse:
            del plan, idempotency_key
            barrier = Barrier(2, timeout=5)

            def claim() -> str:
                try:
                    barrier.wait()
                except BrokenBarrierError:  # pragma: no cover - defensive
                    return "barrier"
                try:
                    with execution.begin_child(1, "create-campaign"):
                        return "issued"
                except AdhelmError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes.extend(pool.map(lambda _: claim(), range(2)))
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))

    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _ConcurrentAdapter)

    instance.execute(compiled, grant, {adapter.provider: adapter})

    assert sorted(outcomes) == ["CHILD_EGRESS_ALREADY_ACTIVE", "issued"]
    started = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_STARTED"]
    assert len(started) == 1


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_expiry_between_children_stops_the_next_child(tmp_path: Path) -> None:
    """An expired plan never journals or permits another child request."""

    clock = _Clock()

    class _ExpiringAdapter(_HierarchyAdapter):
        def apply_children(
            self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
        ) -> ApplyResponse:
            with execution.begin_child(1, "create-campaign") as permit:
                permit.consume(
                    plan_digest=self._plan.plan_digest,
                    subplan_digest=plan.subplan_digest,
                    attempt_ref=idempotency_key,
                    child_index=1,
                )
                self.children.append(1)
                execution.report_child(
                    1, ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))
                )
            clock.now = NOW + timedelta(hours=2)
            with execution.begin_child(2, "create-ad-set") as permit:  # noqa: F841
                self.children.append(2)
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))

    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _ExpiringAdapter, clock=clock)

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.children == [1]
    started = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_STARTED"]
    assert [record["payload"]["child_index"] for record in started] == [1]


def test_child_records_carry_no_sensitive_or_unbounded_intent(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _HierarchyAdapter)

    instance.execute(compiled, grant, {adapter.provider: adapter})

    records = _child_records(ledger)
    assert records
    for record in records:
        payload = record["payload"]
        validate_data(payload)
        assert set(payload) <= {
            "schema_version",
            "operation",
            "resource_ref",
            "plan_ref",
            "plan_digest",
            "subplan_digest",
            "approval_ref",
            "idempotency_ref",
            "child_index",
            "expected_child_egress_count",
            "child_label",
            "effect_status",
            "evidence_refs",
            "intent",
        }
        intent = payload.get("intent")
        if intent is not None:
            assert intent["egress_hosts"] == ["graph.meta.test"]
            assert set(intent) == {
                "egress_hosts",
                "egress_allowlist_digest",
                "intent_digest",
            }
    rendered = str(records)
    for private in ("Bearer", "@", "campaign_fixture", "token"):
        assert private not in rendered


def test_multi_step_subplan_requires_the_child_egress_interface(tmp_path: Path) -> None:
    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _GuardedOnlyAdapter)
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})

    assert error.value.code == "ADAPTER_CHILD_EGRESS_REQUIRED"
    assert ledger.unresolved_child_starts() == []
    assert ledger.checkpoint()["checkpoint_digest"] == before


def test_kernel_execution_cannot_be_constructed_outside_the_kernel() -> None:
    compiled = _plan()
    with pytest.raises(TypeError):
        KernelExecution(
            _issuer=object(),
            kernel=None,  # type: ignore[arg-type]
            plan=compiled,
            subplan=compiled.subplans[0],
            authorization=direct_grant(compiled),
            idempotency_key="a" * 64,
        )


@pytest.mark.parametrize(
    "label",
    [
        "123456789",
        "campaign-88213",
        "create-123",
        "1-create",
        "Create-Campaign",
        "create_campaign",
        "-create",
        "create-",
        "",
        "a" * 65,
    ],
)
def test_child_labels_reject_identifier_shaped_names(tmp_path: Path, label: str) -> None:
    """A label names the step in the approved operation, never a native resource."""

    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, labels=(label, "b", "c"))
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.refusals == ["CHILD_EGRESS_LABEL_INVALID"]
    assert _child_records(ledger) == []


@pytest.mark.parametrize("label", ["create-campaign", "createcampaign", "create-ad-set-v2"])
def test_step_shaped_child_labels_are_accepted(tmp_path: Path, label: str) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, labels=(label, "step-two", "step-three"))
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    started = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_STARTED"]
    assert started[0]["payload"]["child_label"] == label


def test_a_claimed_child_no_effect_never_resolves_its_own_start(tmp_path: Path) -> None:
    """A child's own no-effect claim cannot clear its own pre-egress marker."""

    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path,
        lambda plan: _HierarchyAdapter(
            plan,
            outcomes=(ApplyOutcome.FAILED, ApplyOutcome.CONFIRMED, ApplyOutcome.CONFIRMED),
            no_effect_proven=True,
        ),
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert [record["payload"]["child_index"] for record in ledger.unresolved_child_starts()] == [1]


@pytest.mark.parametrize(
    "label",
    ["act120210000000000", "id120210000000000", "g-abc123def", "a", "v20240", "s"],
)
def test_child_labels_reject_provider_identifier_prefixes(tmp_path: Path, label: str) -> None:
    """A word starts with a letter, is at least two characters, and holds <= 4 digits."""

    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, labels=(label, "step-two", "step-three"))
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.refusals == ["CHILD_EGRESS_LABEL_INVALID"]
    assert _child_records(ledger) == []


@pytest.mark.parametrize("label", ["step2", "v2024", "create-ad-set-v2"])
def test_bounded_ordinal_child_labels_are_still_accepted(tmp_path: Path, label: str) -> None:
    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, lambda plan: _HierarchyAdapter(plan, labels=(label, "step-two", "step-three"))
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    started = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_STARTED"]
    assert started[0]["payload"]["child_label"] == label


def test_a_scope_opened_on_another_thread_is_still_force_closed(tmp_path: Path) -> None:
    """No child permit survives dispatch, whichever context opened its scope."""

    claimed: list[str] = []

    class _CrossThreadAdapter(_HierarchyAdapter):
        def __init__(self, plan: CompositePlan) -> None:
            super().__init__(plan)
            self.leaked: EgressPermit | None = None
            self.attempt = ""
            self.digest = ""

        def apply_children(
            self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
        ) -> ApplyResponse:
            self.attempt = idempotency_key
            self.digest = plan.subplan_digest

            def open_scope() -> None:
                scope = execution.begin_child(1, "create-campaign")
                self.leaked = scope.__enter__()

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(open_scope).result()
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))

        def read_state(self, plan: SubPlan) -> RemoteState:
            if self.leaked is not None and not claimed:
                try:
                    self.leaked.consume(
                        plan_digest=self._plan.plan_digest,
                        subplan_digest=self.digest,
                        attempt_ref=self.attempt,
                        child_index=1,
                    )
                    claimed.append("CLAIMED_DURING_READBACK")
                except AdhelmError as error:
                    claimed.append(error.code)
            return super().read_state(plan)

    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _CrossThreadAdapter)

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert claimed == ["KERNEL_EGRESS_PERMIT_REQUIRED"]
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    results = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_RESULT"]
    assert [r["payload"]["effect_status"] for r in results] == ["AMBIGUOUS_REMOTE_STATE"]


def test_a_scope_closed_between_its_claim_and_its_permit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No permit may be issued into a window the kernel can no longer revoke.

    A force-close that lands between the claim and the permit's creation used to
    find no stack to revoke, so the adapter left the scope holding a live,
    untracked permit whose own ``close()`` had already become a no-op.
    """

    original = KernelExecution._open_child

    def close_between(execution, scope, index, label):  # type: ignore[no-untyped-def]
        opened = original(execution, scope, index, label)
        execution._close()
        return opened

    monkeypatch.setattr(KernelExecution, "_open_child", close_between)
    refusals: list[str] = []

    class _RacingAdapter(_HierarchyAdapter):
        def apply_children(
            self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
        ) -> ApplyResponse:
            del plan, idempotency_key
            try:
                with execution.begin_child(1, "create-campaign"):
                    refusals.append("issued")
            except AdhelmError as error:
                refusals.append(error.code)
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))

    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _RacingAdapter)

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert refusals == ["CHILD_EGRESS_CLOSED"]
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    # The interrupted child is still journaled to a terminal ambiguous row, so
    # nothing is silently dropped and the child stays unresolved.
    results = [r for r in _child_records(ledger) if r["record_kind"] == "CHILD_APPLY_RESULT"]
    assert [r["payload"]["effect_status"] for r in results] == ["AMBIGUOUS_REMOTE_STATE"]
    assert [r["payload"]["child_index"] for r in ledger.unresolved_child_starts()] == [1]

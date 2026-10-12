"""Independent adversarial review of the S2 authorization-kernel generalization.

These regressions were written by a reviewer who did not implement the change.
Each one encodes the behaviour the reviewed contract claims, so a failure here
is a claim the current kernel does not keep.  No provider transport, credential,
or network call is involved: every adapter is an in-memory fake.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_ga4_create_kernel_ultra_review import _AmbiguousGuardedGa4Adapter
from test_ga4_create_kernel_ultra_review import _kernel_with_grant as _ga4_kernel_with_grant
from test_kernel_child_egress import (
    AFTER,
    BEFORE,
    EVIDENCE,
    LABELS,
    NOW,
    _execute,
    _HierarchyAdapter,
)
from test_safety import FakeApprovalHost, direct_grant

from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
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
    new_direct_grant,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _single_subplan(*, provider: str, operation: OperationKind) -> SubPlan:
    return SubPlan(
        subplan_id=f"{provider}-{operation.value.lower()}",
        provider=provider,
        operation=operation,
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
        max_spend=Money(minor_units=0, currency="USD"),
        egress=(EgressTarget(host="graph.meta.test", purpose="campaign pause"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=NOW + timedelta(hours=1),
    )


def _single_plan(
    *, provider: str, operation: OperationKind, plan_id: str = "single-plan"
) -> CompositePlan:
    return CompositePlan(
        plan_id=plan_id,
        profile_ref="test-profile",
        subplans=(_single_subplan(provider=provider, operation=operation),),
        expires_at=NOW + timedelta(hours=1),
    )


class _SingleRequestAdapter:
    """A guarded single-request adapter whose acknowledgement the test controls."""

    def __init__(
        self,
        provider: str,
        *,
        outcome: ApplyOutcome = ApplyOutcome.FAILED,
        no_effect_proven: bool = True,
        post_apply_state: str = BEFORE,
        reconcile_status: EffectStatus = EffectStatus.APPLIED_VERIFIED,
        reconcile_settled: bool = True,
    ) -> None:
        self.provider = provider
        self._outcome = outcome
        self._no_effect_proven = no_effect_proven
        self._post_apply_state = post_apply_state
        self._reconcile_status = reconcile_status
        self._reconcile_settled = reconcile_settled
        self.state = BEFORE
        self.apply_calls = 0
        self.plan_digest = ""

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_api_version="v1",
            capability_version="cap-v1",
            supported_operations=(plan.operation,),
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
        raise AdhelmError("PERMIT_REQUIRED", "The uniform write rule requires a permit.")

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        del plan, idempotency_key
        assert permit is not None
        self.apply_calls += 1
        # The request left the process; only the acknowledgement was lost.
        self.state = self._post_apply_state
        return ApplyResponse(
            outcome=self._outcome,
            no_effect_proven=self._no_effect_proven,
            evidence_refs=(EVIDENCE,),
        )

    def verify(self, plan: SubPlan) -> Verification:
        del plan
        return Verification(verified=False)

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        return Reconciliation(
            state=RemoteState(
                fingerprint=plan.after.fingerprint
                if self._reconcile_status is EffectStatus.APPLIED_VERIFIED
                else plan.before.fingerprint,
                revision="r1",
            ),
            status=self._reconcile_status,
            settled=self._reconcile_settled,
            evidence_refs=(EVIDENCE,) if self._reconcile_settled else (),
        )


def _run_single(
    tmp_path: Path, adapter: _SingleRequestAdapter, *, operation: OperationKind
) -> tuple[SafetyKernel, Ledger, CompositePlan, object]:
    ledger = Ledger(tmp_path, profile_ref="test-profile")
    instance = SafetyKernel(tmp_path, ledger, clock=lambda: NOW)
    compiled = _single_plan(provider=adapter.provider, operation=operation)
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    return instance, ledger, compiled, grant


# --- Finding 1 -------------------------------------------------------------


def test_meta_pause_no_effect_claim_at_baseline_stays_ambiguous(tmp_path: Path) -> None:
    """A Meta PAUSE acknowledgement other than CONFIRMED must stay ambiguous.

    ``docs/implementation/meta-pause-kernel-ultra-review.md`` records this as a
    verified boundary, and ``SafetyKernel.reconcile_run`` still refuses to call
    an unchanged read proof of no effect.  The S2 exception concludes
    ``FAILED_NO_EFFECT`` from exactly that pair -- an adapter-set
    ``no_effect_proven`` flag plus an adapter-supplied post-apply read that has
    not moved -- for the one operation whose acknowledgement was singled out as
    unattributable.  A stale eventually consistent read after a POST that did
    pause the campaign lands here, and the CLI reports exit 3 (no effect)
    instead of exit 6 (ambiguous).
    """

    adapter = _SingleRequestAdapter("meta_ads", post_apply_state=BEFORE)
    instance, ledger, compiled, grant = _run_single(
        tmp_path, adapter, operation=OperationKind.PAUSE
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert adapter.apply_calls == 1
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE


def test_ga4_create_no_effect_claim_at_baseline_stays_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same containment covered GA4 CREATE, using its own retained fixture.

    ``_AmbiguousGuardedGa4Adapter`` already proves the target-state case.  This
    variant only moves the adapter's post-apply read back to the approved
    baseline, which is what a GA4 Admin list that has not yet caught up returns
    after a key event was in fact created.
    """

    kernel, ledger, plan, grant = _ga4_kernel_with_grant(tmp_path, monkeypatch)
    adapter = _Ga4NoEffectAtBaselineAdapter(plan)

    result = kernel.execute(plan, grant, {adapter.provider: adapter})

    assert adapter.guarded_calls == 1
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE


class _Ga4NoEffectAtBaselineAdapter(_AmbiguousGuardedGa4Adapter):
    """A GA4 CREATE adapter that claims no effect while its own read lags."""

    def __init__(self, plan: CompositePlan) -> None:
        super().__init__(plan, outcome=ApplyOutcome.FAILED)

    def apply_guarded(
        self, subplan: SubPlan, *, idempotency_key: str, permit: object
    ) -> ApplyResponse:
        response = super().apply_guarded(subplan, idempotency_key=idempotency_key, permit=permit)
        # The request reached GA4; this caller's own list read has not caught up.
        self._state = subplan.before
        return response


# --- Finding 2 -------------------------------------------------------------


class _LeakingChildAdapter(_HierarchyAdapter):
    """Opens one journaled child scope and never closes it.

    A misbehaving or crashing adapter can leave a child context open exactly
    this way; nothing in the interface forces ``__exit__``.
    """

    def __init__(self, plan: CompositePlan) -> None:
        super().__init__(plan)
        self.scope: object | None = None
        self.leaked: EgressPermit | None = None
        self.attempt = ""
        self.subplan_digest = ""
        self.readback_claim: str | None = None

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse:
        self.attempt = idempotency_key
        self.subplan_digest = plan.subplan_digest
        scope = execution.begin_child(1, "create-campaign")
        self.scope = scope
        self.leaked = scope.__enter__()
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))

    def read_state(self, plan: SubPlan) -> RemoteState:
        if self.leaked is not None and self.readback_claim is None:
            try:
                self.leaked.consume(
                    plan_digest=self._plan.plan_digest,
                    subplan_digest=self.subplan_digest,
                    attempt_ref=self.attempt,
                    child_index=1,
                )
                self.readback_claim = "CLAIMED_DURING_READBACK"
            except AdhelmError as error:
                self.readback_claim = error.code
        return super().read_state(plan)


def test_child_permit_is_revoked_before_the_post_apply_readback(tmp_path: Path) -> None:
    """No kernel-issued permit may survive the adapter's dispatch.

    ``SafetyKernel._dispatch`` guarantees this for a single-request subplan with
    an explicit ``_invalidate_permit`` in a ``finally``.  The child path has no
    equivalent: ``_apply_children`` only sets ``KernelExecution._closed``, so a
    child scope the adapter never exits leaves an active, unconsumed,
    index-bound permit in the context variable.  The kernel then calls
    ``read_state`` and ``verify`` with that permit still claimable, which is the
    exact window the reviewed change says it closed.
    """

    instance, ledger, compiled, adapter, grant = _execute(tmp_path, _LeakingChildAdapter)
    try:
        instance.execute(compiled, grant, {adapter.provider: adapter})

        assert adapter.readback_claim == "KERNEL_EGRESS_PERMIT_REQUIRED"
    finally:
        if adapter.scope is not None:
            adapter.scope.__exit__(None, None, None)  # type: ignore[attr-defined]


# --- Finding 3 -------------------------------------------------------------


class _FabricatedChildNoEffectAdapter(_HierarchyAdapter):
    """Consumes a child permit, writes, then claims that child had no effect."""

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse:
        with execution.begin_child(1, LABELS[0]) as permit:
            permit.consume(
                plan_digest=self._plan.plan_digest,
                subplan_digest=plan.subplan_digest,
                attempt_ref=idempotency_key,
                child_index=1,
            )
            self.children.append(1)
            # The request created the resource; the readback below proves it.
            self.state = AFTER
            execution.report_child(
                1,
                ApplyResponse(
                    outcome=ApplyOutcome.FAILED,
                    no_effect_proven=True,
                    evidence_refs=(EVIDENCE,),
                ),
            )
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(EVIDENCE,))


def test_child_no_effect_claim_is_bound_to_a_readback_at_baseline(tmp_path: Path) -> None:
    """A journaled child's no-effect claim needs the same proof the subplan needs.

    ``SafetyKernel._apply_one`` only accepts ``FAILED_NO_EFFECT`` when the
    post-apply readback is still exactly the approved baseline.  ``_child_effect``
    accepts the adapter's ``no_effect_proven`` flag alone, and a
    ``FAILED_NO_EFFECT`` child result resolves that child's durable
    ``CHILD_APPLY_STARTED``.  So an adapter that really did write can clear its
    own pre-egress marker while the kernel's own readback shows the state moved
    off baseline, leaving a permanently false no-effect row in the journal that
    recovery and reconciliation read.
    """

    instance, ledger, compiled, adapter, grant = _execute(
        tmp_path, _FabricatedChildNoEffectAdapter, children=2
    )

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert adapter.children == [1]
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    child_results = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHILD_APPLY_RESULT"
    ]
    assert [payload["effect_status"] for payload in child_results] == ["AMBIGUOUS_REMOTE_STATE"]
    assert [record["payload"]["child_index"] for record in ledger.unresolved_child_starts()] == [1]


# --- Finding 4 -------------------------------------------------------------


def _reconciled_status(tmp_path: Path, *, provider: str, operation: OperationKind) -> EffectStatus:
    adapter = _SingleRequestAdapter(
        provider,
        outcome=ApplyOutcome.AMBIGUOUS,
        no_effect_proven=False,
        post_apply_state=AFTER,
    )
    instance, ledger, compiled, grant = _run_single(tmp_path, adapter, operation=operation)
    applied = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert applied.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert ledger.unresolved_starts()
    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})
    return reconciled.results[0].status


def test_unresolved_attempts_reconcile_the_same_way_for_every_provider(
    tmp_path: Path,
) -> None:
    """The reconciliation containment is still bound to one provider and operation.

    ``_meta_pause_attempt_history_state`` returns ``NO_ATTEMPT`` for anything
    that is not ``meta_ads``/``PAUSE``, so only Meta is protected from a later
    matching GET settling a durable unattributable attempt.  The uniform write
    rule this change introduces says the attribution gap is a property of remote
    writes rather than of Meta, but the read-side containment was not
    generalized with it: an identical google_ads attempt with an identical
    adapter is settled as ``APPLIED_VERIFIED`` and unblocks the next write.
    """

    meta = _reconciled_status(tmp_path / "meta", provider="meta_ads", operation=OperationKind.PAUSE)
    google = _reconciled_status(
        tmp_path / "google", provider="google_ads", operation=OperationKind.PAUSE
    )

    assert meta is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert google is meta


# --- Re-review finding R1 --------------------------------------------------


def test_a_settled_provider_proof_can_still_clear_the_global_write_barrier(
    tmp_path: Path,
) -> None:
    """Generalizing the read-side containment must not remove the only way out.

    ``_unattributable_attempt_state`` now returns ``UNSETTLED`` for every
    mutating subplan that recorded an ``APPLY_STARTED`` and did not reach
    ``APPLIED_VERIFIED``, and ``reconcile_run`` then discards the adapter's
    proof unconditionally.  Since the uniform write rule makes every
    non-confirmed acknowledgement ``AMBIGUOUS_REMOTE_STATE``, one lost
    acknowledgement on any provider leaves an ``APPLY_STARTED`` that no code
    path can ever resolve: nothing appends ``RECOVERY_RESULT`` for a subplan
    attempt, and ``_reject_unresolved_starts`` is global, so every later plan on
    every provider and account is refused with ``UNRESOLVED_APPLY`` forever.

    At ``dc5df26`` the same run reconciled to ``APPLIED_VERIFIED`` and cleared
    the barrier.  ``docs/lifecycle-contract.md`` line 174,
    ``docs/authorization-policy.md`` line 109 and AGENTS.md rule 6 all describe
    reconciliation as the documented exit from a partial or ambiguous run, and
    ``reconcile_run``'s own contract says the adapter "must prove settled
    provider-native operation state" -- which implies such a proof is
    sufficient for something.  The mechanism is the implementer's choice; this
    test only asserts that an exit exists.
    """

    adapter = _SingleRequestAdapter(
        "google_ads",
        outcome=ApplyOutcome.AMBIGUOUS,
        no_effect_proven=False,
        post_apply_state=AFTER,
    )
    instance, ledger, compiled, grant = _run_single(
        tmp_path, adapter, operation=OperationKind.PAUSE
    )
    assert (
        instance.execute(compiled, grant, {adapter.provider: adapter}).results[0].status
        is EffectStatus.AMBIGUOUS_REMOTE_STATE
    )
    assert ledger.unresolved_starts()

    # A settled provider-native proof, offered as many times as the operator
    # likes, never resolves the attempt.
    for _ in range(3):
        instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    assert ledger.unresolved_starts() == []
    assert ledger.unresolved_runs() == []

    later = _single_plan(provider="google_ads", operation=OperationKind.PAUSE, plan_id="later-plan")
    later_grant = new_direct_grant(
        authorization_id="direct-later",
        plan=later,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=NOW + timedelta(minutes=10),
    )
    instance.record_direct_grant(later, later_grant, FakeApprovalHost())
    adapter.state = BEFORE
    try:
        instance.execute(later, later_grant, {adapter.provider: adapter})
    except AdhelmError as error:  # pragma: no cover - the assertion is the point
        assert error.code != "UNRESOLVED_APPLY"


# --- Final-review finding F1 -----------------------------------------------


class _DivergentProofAdapter(_SingleRequestAdapter):
    """Its reconciliation proof reports a state its own current read denies."""

    diverged = "9" * 64

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        del plan
        return Reconciliation(
            state=RemoteState(fingerprint=self.diverged, revision="not-the-current-revision"),
            status=EffectStatus.APPLIED_VERIFIED,
            settled=True,
            evidence_refs=(EVIDENCE,),
        )


def test_a_released_attempt_records_a_state_the_kernel_actually_read(tmp_path: Path) -> None:
    """``ATTEMPT_RECOVERY_OBSERVED`` must describe an observation, not a claim.

    ``AttemptRecoveryObservation`` says it "records what was observed" and the
    kernel derives ``observed_relation`` and ``disposition`` from that value,
    so downstream tooling reads the record as the current remote state under
    rule 24.  ``record_attempt_recovery`` earns that: it re-checks identity and
    calls ``read_state`` under the coordinator lock.  The ``reconcile_run``
    writer instead copies ``Reconciliation.state`` straight through, and the
    ``UNSETTLED`` downgrade happens before the ``RECONCILIATION_STATE_MISMATCH``
    checks, so nothing constrains that fingerprint at all.

    Here the adapter's proof reports one state while the same adapter's current
    read reports another.  The kernel records the proof's fingerprint, computes
    ``OTHER``/``RECOVERED_DIVERGED`` from a value it never read, and releases
    the barrier.  Either taking a fresh read like the operator path, or
    refusing to release on a proof that contradicts the current read, satisfies
    this; the mechanism is the implementer's choice.
    """

    adapter = _DivergentProofAdapter(
        "google_ads",
        outcome=ApplyOutcome.AMBIGUOUS,
        no_effect_proven=False,
        post_apply_state=AFTER,
    )
    instance, ledger, compiled, grant = _run_single(
        tmp_path, adapter, operation=OperationKind.PAUSE
    )
    instance.execute(compiled, grant, {adapter.provider: adapter})
    instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    recoveries = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert len(recoveries) == 1
    current = adapter.read_state(compiled.subplans[0])
    assert recoveries[0]["observed_state_fingerprint"] == current.fingerprint


# --- Final-review finding F2 -----------------------------------------------


def test_no_installed_adapter_supplies_an_attributable_settlement_proof() -> None:
    """Pin the settlement hook's stated rule, which nothing enforced.

    ``AttributableSettlementProof`` is the one hook that restores full
    ``APPLIED_VERIFIED`` attribution to an attempt this kernel already recorded
    as unattributable: it makes ``reconcile_run`` write a ``RECONCILIATION_RESULT``
    and resolve the start.  Its docstring says "no installed adapter implements
    this today, and none may until its provider documents a durable operation or
    idempotency resource", but nothing checks that.  It is a ``runtime_checkable``
    Protocol, so any class that merely defines a method of that name qualifies,
    with no per-provider allowlist and no evidence gate.

    ``test_safety.py`` already pins the sibling policy with
    ``READ_ONLY_OPERATIONS == frozenset()``.  This is the same kind of pin; it
    was missing, so the reviewer contributes it here.  It passes today and will
    fail the moment a provider adapter grows the method, which is the signal a
    hook this consequential needs.
    """

    import importlib
    import pkgutil

    import adhelm.providers
    from adhelm.safety import AttributableSettlementProof

    implementers: list[str] = []
    for module in pkgutil.iter_modules(adhelm.providers.__path__):
        loaded = importlib.import_module(f"adhelm.providers.{module.name}")
        for name, value in vars(loaded).items():
            if (
                isinstance(value, type)
                and value.__module__ == loaded.__name__
                and issubclass(value, AttributableSettlementProof)
            ):
                implementers.append(f"{loaded.__name__}.{name}")
    assert implementers == []


# --- Item 3 regression (passing): the containment is no longer PAUSE-only ---


@pytest.mark.parametrize(
    "operation",
    [OperationKind.CREATE, OperationKind.UPDATE, OperationKind.ACTIVATE, OperationKind.PAUSE],
)
def test_the_unattributable_containment_covers_every_operation(
    tmp_path: Path, operation: OperationKind
) -> None:
    """``_attempt_events`` used to match the literal ``"PAUSE"`` operation.

    Every non-PAUSE attempt therefore scanned as ``NO_ATTEMPT`` and a later
    matching read could settle it.  The scan now matches the subplan's own
    operation, so the containment applies to every mutating kind.
    """

    from adhelm.safety import _attempt_events, _unattributable_attempt_state

    adapter = _SingleRequestAdapter(
        "google_ads",
        outcome=ApplyOutcome.AMBIGUOUS,
        no_effect_proven=False,
        post_apply_state=AFTER,
        reconcile_settled=False,
    )
    instance, ledger, compiled, grant = _run_single(tmp_path, adapter, operation=operation)
    instance.execute(compiled, grant, {adapter.provider: adapter})

    history = ledger.records("changes")
    subplan = compiled.subplans[0]
    scan = _attempt_events(history, plan=compiled, authorization=grant, subplan=subplan)
    assert scan is not None
    assert len(scan[0]) == 1 and len(scan[1]) == 1
    assert (
        _unattributable_attempt_state(history, plan=compiled, authorization=grant, subplan=subplan)
        == "UNSETTLED"
    )

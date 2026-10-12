"""Authorization-bound recording of an unresolved attempt's current state.

Every adapter here is an in-memory fake: nothing contacts a provider.  These
regressions cover the one exit from the global write barrier that exists for an
attempt this kernel never finished journaling.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_safety import (
    AFTER,
    BEFORE,
    EVIDENCE_REF,
    NOW,
    FakeAdapter,
    FakeApprovalHost,
    delegated,
    direct_grant,
    kernel,
    plan,
)

from adhelm import safety
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import (
    ApplyOutcome,
    ApplyResponse,
    AttributableSettlement,
    CompositePlan,
    DelegationPolicy,
    EffectStatus,
    OperationKind,
    Reconciliation,
    RemoteState,
    SafetyKernel,
    attempt_recovery_subject,
    new_attempt_recovery_grant,
    new_direct_grant,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _InterruptedAdapter(FakeAdapter):
    """Dies inside the write path after the kernel's durable start."""

    def apply_guarded(self, subplan, *, idempotency_key, permit):  # type: ignore[no-untyped-def]
        self.apply_calls += 1
        self.state = AFTER
        raise KeyboardInterrupt("synthetic process death after egress")


def _interrupted(tmp_path: Path) -> tuple[SafetyKernel, Ledger, CompositePlan, object, FakeAdapter]:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = _InterruptedAdapter()
    adapter.ledger = ledger
    with pytest.raises(KeyboardInterrupt):
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert ledger.unresolved_starts()
    return instance, ledger, compiled, grant, adapter


def _subjects(ledger: Ledger, compiled: CompositePlan, grant: object) -> tuple[str, ...]:
    started = next(
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
        and record["payload"].get("event_type") == "APPLY_STARTED"
    )
    return (
        attempt_recovery_subject(
            compiled, compiled.subplans[0], started_record_digest=started["record_digest"]
        ),
    )


def _recovery_grant(subjects: tuple[str, ...], *, authorization_id: str = "recovery-1") -> object:
    return new_attempt_recovery_grant(
        authorization_id=authorization_id,
        subjects=subjects,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=NOW + timedelta(minutes=10),
    )


def test_an_interrupted_attempt_is_not_released_by_reconciliation(tmp_path: Path) -> None:
    """A process that died inside the write path has no journaled terminal pair."""

    instance, ledger, compiled, grant, adapter = _interrupted(tmp_path)

    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert ledger.unresolved_starts()
    assert not [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]


def test_recording_the_observation_releases_the_barrier_without_attributing(
    tmp_path: Path,
) -> None:
    instance, ledger, compiled, grant, adapter = _interrupted(tmp_path)
    subjects = _subjects(ledger, compiled, grant)

    result = instance.record_attempt_recovery(
        compiled,
        grant,
        _recovery_grant(subjects),
        FakeApprovalHost(),
        {adapter.provider: adapter},
    )

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.results[0].diagnostic_code == "ATTEMPT_RECOVERY_RECORDED"
    recorded = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert len(recorded) == 1
    payload = recorded[0]
    assert payload["actor_ref"] == "UNKNOWN"
    assert payload["attempt_effect_status"] == "AMBIGUOUS_REMOTE_STATE"
    assert payload["observed_relation"] == "AT_TARGET"
    assert payload["disposition"] == "RECOVERED_AT_TARGET"
    assert payload["recovery_source"] == "OPERATOR_ATTESTED_OBSERVATION"
    assert payload["recovery_subject_digest"] == subjects[0]
    assert payload["observed_state_fingerprint"] == AFTER
    assert payload["observation_interval_start"]
    assert ledger.unresolved_starts() == []


def test_a_recovery_grant_for_another_attempt_is_refused(tmp_path: Path) -> None:
    instance, ledger, compiled, grant, adapter = _interrupted(tmp_path)

    with pytest.raises(AdhelmError) as error:
        instance.record_attempt_recovery(
            compiled,
            grant,
            _recovery_grant(("f" * 64,)),
            FakeApprovalHost(),
            {adapter.provider: adapter},
        )

    assert error.value.code == "ATTEMPT_RECOVERY_SUBJECT_MISMATCH"
    assert ledger.unresolved_starts()


def test_an_expired_or_unproven_recovery_grant_is_refused(tmp_path: Path) -> None:
    instance, ledger, compiled, grant, adapter = _interrupted(tmp_path)
    subjects = _subjects(ledger, compiled, grant)
    expired = new_attempt_recovery_grant(
        subjects=subjects,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(AdhelmError) as stale:
        instance.record_attempt_recovery(
            compiled, grant, expired, FakeApprovalHost(), {adapter.provider: adapter}
        )
    assert stale.value.code == "AUTHORIZATION_EXPIRED"

    unproven = new_attempt_recovery_grant(
        subjects=subjects,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="never-approved",
        expires_at=NOW + timedelta(minutes=10),
    )
    with pytest.raises(AdhelmError) as refused:
        instance.record_attempt_recovery(
            compiled, grant, unproven, FakeApprovalHost(), {adapter.provider: adapter}
        )
    assert refused.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert ledger.unresolved_starts()


def test_a_recovery_grant_is_one_use(tmp_path: Path) -> None:
    instance, ledger, compiled, grant, adapter = _interrupted(tmp_path)
    subjects = _subjects(ledger, compiled, grant)
    recovery = _recovery_grant(subjects)
    instance.record_attempt_recovery(
        compiled, grant, recovery, FakeApprovalHost(), {adapter.provider: adapter}
    )

    with pytest.raises(AdhelmError) as replay:
        instance.record_attempt_recovery(
            compiled, grant, recovery, FakeApprovalHost(), {adapter.provider: adapter}
        )

    assert replay.value.code == "ATTEMPT_RECOVERY_NOT_REQUIRED"


def test_a_recovery_grant_cannot_be_replayed_against_a_second_attempt(tmp_path: Path) -> None:
    first, ledger, compiled, grant, adapter = _interrupted(tmp_path)
    subjects = _subjects(ledger, compiled, grant)
    recovery = _recovery_grant(subjects)
    first.record_attempt_recovery(
        compiled, grant, recovery, FakeApprovalHost(), {adapter.provider: adapter}
    )

    second_plan = plan().model_copy(update={"plan_id": "second-plan"})
    second_grant = new_direct_grant(
        authorization_id="direct-2",
        plan=second_plan,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=NOW + timedelta(minutes=10),
    )
    first.record_direct_grant(second_plan, second_grant, FakeApprovalHost())
    second_adapter = _InterruptedAdapter()
    second_adapter.ledger = ledger
    second_adapter.state = BEFORE
    with pytest.raises(KeyboardInterrupt):
        first.execute(second_plan, second_grant, {second_adapter.provider: second_adapter})
    replay = new_attempt_recovery_grant(
        authorization_id=recovery.authorization_id,
        subjects=_subjects_for(ledger, second_plan, second_grant),
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(AdhelmError) as refused:
        first.record_attempt_recovery(
            second_plan,
            second_grant,
            replay,
            FakeApprovalHost(),
            {second_adapter.provider: second_adapter},
        )

    assert refused.value.code == "AUTHORIZATION_ALREADY_CONSUMED"


def _subjects_for(ledger: Ledger, compiled: CompositePlan, grant: object) -> tuple[str, ...]:
    started = [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
        and record["payload"].get("event_type") == "APPLY_STARTED"
        and record["payload"].get("plan_digest") == compiled.plan_digest
    ]
    return (
        attempt_recovery_subject(
            compiled, compiled.subplans[0], started_record_digest=started[-1]["record_digest"]
        ),
    )


def test_a_delegated_recovery_needs_an_explicit_recover_operation(tmp_path: Path) -> None:
    instance, ledger, compiled, grant, adapter = _interrupted(tmp_path)
    subjects = _subjects(ledger, compiled, grant)
    without = delegated(compiled)
    instance.install_delegation_policy(without.policy, FakeApprovalHost())
    bare = without.model_copy(update={"plan_digest": None}).model_copy(update={"plan_digest": None})
    from adhelm.canonical import digest

    denied = without.model_copy(update={"plan_digest": digest(list(subjects))})
    with pytest.raises(AdhelmError) as refused:
        instance.record_attempt_recovery(
            compiled, grant, denied, FakeApprovalHost(), {adapter.provider: adapter}
        )
    assert refused.value.code == "DELEGATION_OPERATION_DENIED"
    assert bare is not None

    allowed_policy = DelegationPolicy(
        **{
            **without.policy.model_dump(mode="python"),
            "policy_id": "recovery-policy",
            "allowed_operations": (OperationKind.CREATE, OperationKind.RECOVER),
        }
    )
    instance.install_delegation_policy(allowed_policy, FakeApprovalHost())
    allowed = without.model_copy(
        update={"policy": allowed_policy, "plan_digest": digest(list(subjects))}
    )

    instance.record_attempt_recovery(
        compiled, grant, allowed, FakeApprovalHost(), {adapter.provider: adapter}
    )

    assert ledger.unresolved_starts() == []
    assert EVIDENCE_REF is not None


class _AmbiguousAtTargetAdapter(FakeAdapter):
    """Writes, then loses its acknowledgement: locally terminated but unattributed."""

    def apply(self, subplan, *, idempotency_key):  # type: ignore[no-untyped-def]
        self.apply_calls += 1
        self.state = AFTER
        return ApplyResponse(outcome=ApplyOutcome.AMBIGUOUS, evidence_refs=(EVIDENCE_REF,))


class _DivergentProofAdapter(_AmbiguousAtTargetAdapter):
    """Its settled proof reports a state its own current read denies."""

    diverged = "9" * 64

    def reconcile(self, subplan):  # type: ignore[no-untyped-def]
        del subplan
        return Reconciliation(
            state=RemoteState(fingerprint=self.diverged, revision="not-the-current-revision"),
            status=EffectStatus.APPLIED_VERIFIED,
            settled=True,
            evidence_refs=(EVIDENCE_REF,),
        )


def _terminated(
    tmp_path: Path, adapter: FakeAdapter
) -> tuple[SafetyKernel, Ledger, CompositePlan, object]:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter.ledger = ledger
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    return instance, ledger, compiled, grant


def test_the_released_record_describes_the_kernels_own_read_not_the_proof(
    tmp_path: Path,
) -> None:
    """The reconcile_run writer meets the operator path's standard."""

    adapter = _DivergentProofAdapter()
    instance, ledger, compiled, grant = _terminated(tmp_path, adapter)

    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    recorded = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert len(recorded) == 1
    assert recorded[0]["observed_state_fingerprint"] == AFTER
    assert recorded[0]["observed_state_fingerprint"] != _DivergentProofAdapter.diverged
    assert recorded[0]["observed_relation"] == "AT_TARGET"
    assert recorded[0]["disposition"] == "RECOVERED_AT_TARGET"
    assert recorded[0]["recovery_source"] == "PROVIDER_SETTLEMENT_PROOF"
    # The contradiction is reported rather than quietly absorbed.
    assert reconciled.results[0].diagnostic_code == "RECONCILIATION_PROOF_CONTRADICTS_READ"
    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE


def test_an_agreeing_proof_records_the_read_without_a_contradiction_diagnostic(
    tmp_path: Path,
) -> None:
    adapter = _AmbiguousAtTargetAdapter()
    instance, ledger, compiled, grant = _terminated(tmp_path, adapter)

    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    recorded = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert recorded[0]["observed_state_fingerprint"] == AFTER
    assert reconciled.results[0].diagnostic_code is None
    assert ledger.unresolved_starts() == []


class _SettlingAdapter(_AmbiguousAtTargetAdapter):
    """The documented provider-native hook, exercised with a fake proof."""

    def __init__(self) -> None:
        super().__init__()
        self.plan_digest = ""

    def attributable_settlement(self, subplan, *, attempt_ref):  # type: ignore[no-untyped-def]
        return AttributableSettlement(
            plan_digest=self.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            provider_operation_ref="operations/settled-fixture",
            settled_status=EffectStatus.APPLIED_VERIFIED,
            evidence_refs=(EVIDENCE_REF,),
        )


def test_a_settlement_proof_is_ignored_unless_its_provider_is_allowlisted(
    tmp_path: Path,
) -> None:
    """Defining the Protocol method is not enough to restore attribution."""

    adapter = _SettlingAdapter()
    instance, ledger, compiled, grant = _terminated(tmp_path, adapter)
    adapter.plan_digest = compiled.plan_digest
    assert safety.ATTRIBUTABLE_SETTLEMENT_PROVIDERS == frozenset()

    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]


def test_an_allowlisted_settlement_proof_attributes_its_own_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook works once a provider is reviewed onto the allowlist."""

    adapter = _SettlingAdapter()
    instance, ledger, compiled, grant = _terminated(tmp_path, adapter)
    adapter.plan_digest = compiled.plan_digest
    monkeypatch.setattr(safety, "ATTRIBUTABLE_SETTLEMENT_PROVIDERS", frozenset({"google_ads"}))

    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ] == []


def test_an_allowlisted_proof_for_another_attempt_is_still_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _SettlingAdapter()
    instance, ledger, compiled, grant = _terminated(tmp_path, adapter)
    adapter.plan_digest = "f" * 64
    monkeypatch.setattr(safety, "ATTRIBUTABLE_SETTLEMENT_PROVIDERS", frozenset({"google_ads"}))

    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE

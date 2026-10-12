"""Durable acceptance of the two provider-neutral approval kinds.

These tests record authorizations in a real local ledger with synthetic
ephemeral keys and in-memory adapters. They establish no installed host, no
enrollment, no provider access, and no release readiness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from generate_composite_execution_plan_approval_vectors import (
    _CHALLENGE_EXPIRES,
    _NOW,
    _RECEIPT_ISSUED_AT,
    composite_plan,
    configuration,
)
from test_approval_acceptance_ultra_review import _timestamp
from test_approval_protocol import _private_key, _public_x963, _signed_receipt
from test_safety import FakeAdapter, FakeApprovalHost
from test_safety import subplan as safety_subplan

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import (
    PinnedApprovalTrust,
    accept_approval_receipt,
    accept_attempt_recovery_receipt,
    accept_composite_execution_plan_receipt,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    build_attempt_recovery_challenge,
    build_composite_execution_plan_challenge,
)
from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import (
    CompositePlan,
    EffectStatus,
    SafetyKernel,
    SubPlan,
    _verifies_direct_intent,
    attempt_recovery_subject,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_WORKSPACE = "workspace-ledger-1"
# Supplied lines never make a spend or authorization claim: those are derived
# from the structured subject and lead the list.
_LINES = ("Provider google ads operation CREATE", "Planned provider requests 1")


def _kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SafetyKernel, Ledger]:
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(_RECEIPT_ISSUED_AT))
    ledger = Ledger(tmp_path, profile_ref=_WORKSPACE)
    return SafetyKernel(tmp_path, ledger, clock=lambda: _RECEIPT_ISSUED_AT), ledger


def _trust(key: ec.EllipticCurvePrivateKey) -> PinnedApprovalTrust:
    return PinnedApprovalTrust(configuration(), _public_x963(key), _WORKSPACE)


def _composite_challenge(plan: CompositePlan) -> ApprovalChallenge:
    return build_composite_execution_plan_challenge(
        plan,
        grantor_ref="customer-grantor-1",
        review_lines=_LINES,
        expires_at=_CHALLENGE_EXPIRES,
        configuration=configuration(),
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )


def _receipt(
    challenge: ApprovalChallenge,
    key: ec.EllipticCurvePrivateKey,
    *,
    receipt_id: str = "approval-receipt:" + "A" * 43,
) -> bytes:
    return _signed_receipt(
        challenge,
        key=key,
        issued_at=_RECEIPT_ISSUED_AT,
        receipt_id=receipt_id,
        configuration=configuration(),
    )


_RECOVERY_RECEIPT_ID = "approval-receipt:" + "B" * 43


def test_a_composite_receipt_records_one_grant_bound_to_every_subplan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger = _kernel(tmp_path, monkeypatch)
    plan = composite_plan()
    challenge = _composite_challenge(plan)
    key = _private_key()

    accepted = accept_composite_execution_plan_receipt(
        kernel,
        ledger,
        receipt=_receipt(challenge, key),
        expected_challenge=challenge,
        trust=_trust(key),
        plan=plan,
    )

    grant = accepted.direct_grant
    assert accepted.kind is ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN
    assert grant is not None
    assert grant.plan_digest == plan.plan_digest
    assert grant.subplan_digests == plan.subplan_digests
    assert grant.expires_at == challenge.expires_at
    assert accepted.approval_host is None
    records = ledger.records("changes")
    claim, event = records[-2:]
    assert claim["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    assert claim["payload"]["receipt_kind"] == "COMPOSITE_EXECUTION_PLAN"
    assert event["payload"]["event_type"] == "APPROVAL_GRANTED"
    assert event["payload"]["plan_digest"] == plan.plan_digest
    assert event["payload"]["effect_status"] == "NO_REMOTE_WRITE"


def test_a_composite_receipt_is_one_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kernel, ledger = _kernel(tmp_path, monkeypatch)
    plan = composite_plan()
    challenge = _composite_challenge(plan)
    key = _private_key()
    receipt = _receipt(challenge, key)

    accept_composite_execution_plan_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=challenge,
        trust=_trust(key),
        plan=plan,
    )
    with pytest.raises(AdhelmError) as replayed:
        accept_composite_execution_plan_receipt(
            kernel,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=_trust(key),
            plan=plan,
        )
    assert replayed.value.code != "OK"
    assert (
        len(
            [
                record
                for record in ledger.records("changes")
                if record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
            ]
        )
        == 1
    )


def test_a_receipt_for_another_composite_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger = _kernel(tmp_path, monkeypatch)
    plan = composite_plan()
    other = plan.model_copy(update={"plan_id": "another-plan"})
    challenge = _composite_challenge(plan)
    key = _private_key()

    with pytest.raises(AdhelmError) as failed:
        accept_composite_execution_plan_receipt(
            kernel,
            ledger,
            receipt=_receipt(challenge, key),
            expected_challenge=challenge,
            trust=_trust(key),
            plan=other,
        )
    assert failed.value.code == "APPROVAL_SOURCE_PLAN_MISMATCH"
    assert not [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    ]


def test_a_composite_receipt_without_its_plan_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic pinned route has no plan to compare, so it fails closed."""

    kernel, ledger = _kernel(tmp_path, monkeypatch)
    challenge = _composite_challenge(composite_plan())
    key = _private_key()

    with pytest.raises(AdhelmError) as failed:
        accept_approval_receipt(
            kernel,
            ledger,
            receipt=_receipt(challenge, key),
            expected_challenge=challenge,
            trust=_trust(key),
        )
    assert failed.value.code == "APPROVAL_SOURCE_PLAN_REQUIRED"


def test_an_expired_receipt_is_refused_before_any_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(_CHALLENGE_EXPIRES))
    ledger = Ledger(tmp_path, profile_ref=_WORKSPACE)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: _CHALLENGE_EXPIRES)
    plan = composite_plan()
    challenge = _composite_challenge(plan)
    key = _private_key()

    with pytest.raises(AdhelmError) as failed:
        accept_composite_execution_plan_receipt(
            kernel,
            ledger,
            receipt=_receipt(challenge, key),
            expected_challenge=challenge,
            trust=_trust(key),
            plan=plan,
        )
    assert failed.value.code == "APPROVAL_RECEIPT_EXPIRED"


def _interrupted_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SafetyKernel, Ledger, CompositePlan, SubPlan, str, object]:
    """Drive one guarded attempt that dies inside the kernel's write path."""

    kernel, ledger = _kernel(tmp_path, monkeypatch)
    subplan = safety_subplan()
    plan = CompositePlan(
        plan_id="recovery-plan",
        profile_ref=_WORKSPACE,
        subplans=(subplan,),
        expires_at=_NOW + timedelta(hours=1),
    )
    challenge = _composite_challenge(plan)
    key = _private_key()
    accepted = accept_composite_execution_plan_receipt(
        kernel,
        ledger,
        receipt=_receipt(challenge, key),
        expected_challenge=challenge,
        trust=_trust(key),
        plan=plan,
    )
    grant = accepted.direct_grant
    assert grant is not None

    class _Interrupted(FakeAdapter):
        def apply_guarded(self, subplan, *, idempotency_key, permit):  # type: ignore[no-untyped-def]
            self.apply_calls += 1
            raise KeyboardInterrupt("synthetic process death after egress")

    adapter = _Interrupted()
    adapter.ledger = ledger
    with pytest.raises(KeyboardInterrupt):
        kernel.execute(plan, grant, {adapter.provider: adapter})
    assert ledger.unresolved_starts()
    started = next(
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
        and record["payload"].get("event_type") == "APPLY_STARTED"
    )
    return kernel, ledger, plan, subplan, str(started["record_digest"]), grant


def test_a_recovery_receipt_releases_exactly_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, plan, subplan, started, grant = _interrupted_attempt(tmp_path, monkeypatch)
    attempts = ((subplan, started),)
    challenge = build_attempt_recovery_challenge(
        plan,
        attempts,
        grantor_ref="customer-grantor-1",
        review_lines=("One unresolved attempt has no journaled result",),
        expires_at=_CHALLENGE_EXPIRES,
        configuration=configuration(),
        nonce=bytes(range(32, 64)),
        issued_at=_NOW,
        now=_NOW,
    )
    key = _private_key()

    accepted = accept_attempt_recovery_receipt(
        kernel,
        ledger,
        receipt=_receipt(challenge, key, receipt_id=_RECOVERY_RECEIPT_ID),
        expected_challenge=challenge,
        trust=_trust(key),
        plan=plan,
        attempts=attempts,
    )

    recovery = accepted.direct_grant
    subject = attempt_recovery_subject(plan, subplan, started_record_digest=started)
    assert recovery is not None
    assert recovery.plan_digest == digest([subject])
    assert recovery.subplan_digests == (subject,)
    # A recovery grant binds recovery subjects, not this plan, so acceptance
    # deliberately appends no APPROVAL_GRANTED event for it.
    assert accepted.approval_host is not None
    assert [
        record["payload"]["event_type"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
    ] == ["APPROVAL_GRANTED", "APPLY_STARTED"]

    adapter = FakeAdapter()
    adapter.ledger = ledger
    result = kernel.record_attempt_recovery(
        plan, grant, recovery, accepted.approval_host, {adapter.provider: adapter}
    )

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.results[0].diagnostic_code == "ATTEMPT_RECOVERY_RECORDED"
    assert not ledger.unresolved_starts()
    observed = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert len(observed) == 1
    assert observed[0]["actor_ref"] == "UNKNOWN"
    assert observed[0]["recovery_subject_digest"] == subject
    assert observed[0]["recovery_approval_ref"] == recovery.authorization_id


def test_a_recovery_receipt_naming_another_started_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, plan, subplan, started, _ = _interrupted_attempt(tmp_path, monkeypatch)
    challenge = build_attempt_recovery_challenge(
        plan,
        ((subplan, started),),
        grantor_ref="customer-grantor-1",
        review_lines=("One unresolved attempt has no journaled result",),
        expires_at=_CHALLENGE_EXPIRES,
        configuration=configuration(),
        nonce=bytes(range(32, 64)),
        issued_at=_NOW,
        now=_NOW,
    )
    key = _private_key()

    with pytest.raises(AdhelmError) as failed:
        accept_attempt_recovery_receipt(
            kernel,
            ledger,
            receipt=_receipt(challenge, key, receipt_id=_RECOVERY_RECEIPT_ID),
            expected_challenge=challenge,
            trust=_trust(key),
            plan=plan,
            attempts=((subplan, "0" * 64),),
        )
    assert failed.value.code == "APPROVAL_SOURCE_PLAN_MISMATCH"


def test_a_recovery_grant_is_not_accepted_by_the_composite_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovery grant cannot be recorded as an authorization for its plan."""

    kernel, ledger, plan, subplan, started, _ = _interrupted_attempt(tmp_path, monkeypatch)
    challenge = build_attempt_recovery_challenge(
        plan,
        ((subplan, started),),
        grantor_ref="customer-grantor-1",
        review_lines=("One unresolved attempt has no journaled result",),
        expires_at=_CHALLENGE_EXPIRES,
        configuration=configuration(),
        nonce=bytes(range(32, 64)),
        issued_at=_NOW,
        now=_NOW,
    )
    key = _private_key()

    with pytest.raises(AdhelmError) as failed:
        accept_composite_execution_plan_receipt(
            kernel,
            ledger,
            receipt=_receipt(challenge, key, receipt_id=_RECOVERY_RECEIPT_ID),
            expected_challenge=challenge,
            trust=_trust(key),
            plan=plan,
        )
    assert failed.value.code == "APPROVAL_SOURCE_PLAN_MISMATCH"


def test_a_composite_grant_is_not_a_recovery_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, plan, subplan, started, grant = _interrupted_attempt(tmp_path, monkeypatch)
    adapter = FakeAdapter()
    adapter.ledger = ledger

    with pytest.raises(AdhelmError) as failed:
        kernel.record_attempt_recovery(
            plan, grant, grant, FakeApprovalHost(), {adapter.provider: adapter}
        )
    assert failed.value.code == "ATTEMPT_RECOVERY_SUBJECT_MISMATCH"
    assert ledger.unresolved_starts()


def test_the_accepted_recovery_host_only_proves_its_own_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, plan, subplan, started, grant = _interrupted_attempt(tmp_path, monkeypatch)
    attempts = ((subplan, started),)
    challenge = build_attempt_recovery_challenge(
        plan,
        attempts,
        grantor_ref="customer-grantor-1",
        review_lines=("One unresolved attempt has no journaled result",),
        expires_at=_CHALLENGE_EXPIRES,
        configuration=configuration(),
        nonce=bytes(range(32, 64)),
        issued_at=_NOW,
        now=_NOW,
    )
    key = _private_key()
    accepted = accept_attempt_recovery_receipt(
        kernel,
        ledger,
        receipt=_receipt(challenge, key, receipt_id=_RECOVERY_RECEIPT_ID),
        expected_challenge=challenge,
        trust=_trust(key),
        plan=plan,
        attempts=attempts,
    )
    host = accepted.approval_host
    assert host is not None

    assert host.verifies_direct_grant(accepted.direct_grant) is True
    assert host.verifies_direct_grant(grant) is False
    assert host.verifies_policy_configuration(None) is False  # type: ignore[arg-type]


def test_the_receipt_clock_still_governs_a_late_recovery_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound host stops proving the grant once its receipt has expired."""

    kernel, ledger, plan, subplan, started, _ = _interrupted_attempt(tmp_path, monkeypatch)
    attempts = ((subplan, started),)
    challenge = build_attempt_recovery_challenge(
        plan,
        attempts,
        grantor_ref="customer-grantor-1",
        review_lines=("One unresolved attempt has no journaled result",),
        expires_at=_CHALLENGE_EXPIRES,
        configuration=configuration(),
        nonce=bytes(range(32, 64)),
        issued_at=_NOW,
        now=_NOW,
    )
    key = _private_key()
    accepted = accept_attempt_recovery_receipt(
        kernel,
        ledger,
        receipt=_receipt(challenge, key, receipt_id=_RECOVERY_RECEIPT_ID),
        expected_challenge=challenge,
        trust=_trust(key),
        plan=plan,
        attempts=attempts,
    )
    host = accepted.approval_host
    assert host is not None

    later = datetime(2030, 1, 1, 0, 10, tzinfo=UTC)
    monkeypatch.setattr(kernel, "clock", lambda: later)
    # The bound host re-verifies the receipt on every proof, so an expired
    # receipt stops proving its grant. The kernel's own wrapper turns that
    # refusal into USER_APPROVAL_HOST_REQUIRED rather than a leaked reason.
    with pytest.raises(AdhelmError) as failed:
        host.verifies_direct_grant(accepted.direct_grant)
    assert failed.value.code == "APPROVAL_RECEIPT_EXPIRED"
    assert _verifies_direct_intent(host, accepted.direct_grant) is False

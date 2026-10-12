"""Durable acceptance of actual public-only Swift receipt fixtures."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_approval_acceptance_ultra_review import _policy_material, _timestamp
from test_approval_protocol import _NOW, _config, _fixture_policy
from test_approval_protocol_ultra_review import _MATERIAL

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import ApprovalChallengeKind, parse_approval_challenge_wire
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import SafetyKernel
from adhelm.saved_plans import retain_openai_pause_plan


@pytest.mark.parametrize("index", (0, 1, 2, 3))
def test_actual_swift_receipt_is_consumed_and_records_only_its_exact_operation(
    index: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(now))
    vector = _MATERIAL["vectors"][index]
    challenge = parse_approval_challenge_wire(
        canonical_bytes(vector["challenge"]), configuration=_config()
    )
    if challenge.revoked_at is not None:
        now = max(now, challenge.revoked_at)
    workspace = challenge.action.plan.profile_ref if challenge.action else "policy-workspace"
    ledger = Ledger(tmp_path, profile_ref=workspace)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: now)
    source = (
        retain_openai_pause_plan(ledger, challenge.action)["record_digest"]
        if challenge.action
        else None
    )
    if challenge.kind == ApprovalChallengeKind.DELEGATION_POLICY_REVOKE:
        installation = _MATERIAL["vectors"][2]
        accept_approval_receipt(
            kernel,
            ledger,
            receipt=canonical_bytes(installation["receipt"]),
            expected_challenge=parse_approval_challenge_wire(
                canonical_bytes(installation["challenge"]), configuration=_config()
            ),
            trust=PinnedApprovalTrust(
                _config(), bytes.fromhex(_MATERIAL["public_key_x963_hex"]), workspace
            ),
        )
    accepted = accept_approval_receipt(
        kernel,
        ledger,
        receipt=canonical_bytes(vector["receipt"]),
        expected_challenge=challenge,
        trust=PinnedApprovalTrust(
            _config(), bytes.fromhex(_MATERIAL["public_key_x963_hex"]), workspace
        ),
        source_plan_record_digest=source,
    )
    records = ledger.records("changes")
    claim, event = records[-2:]
    assert claim["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    assert event["record_kind"] == "CHANGE_EVENT"
    assert accepted.receipt_claim_record_digest == claim["record_digest"]
    assert accepted.receipt_digest == vector["receipt_digest"]
    assert accepted.kind == challenge.kind
    assert event["payload"]["effect_status"] == "NO_REMOTE_WRITE"
    if challenge.kind == ApprovalChallengeKind.DELEGATION_POLICY_REVOKE:
        assert event["payload"]["event_type"] == "RECOVERY_RESULT"
        assert event["payload"]["operation"] == "PAUSE"
        assert claim["payload"]["receipt"]["revoked_at"] == challenge.revoked_at_text
    else:
        assert event["payload"]["event_type"] == "APPROVAL_GRANTED"


def test_policy_expiry_during_final_authorization_append_leaves_a_consumed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _fixture_policy().model_copy(update={"expires_at": _NOW + timedelta(seconds=6)})
    ledger, kernel, challenge, receipt, trust = _policy_material(
        tmp_path, monkeypatch, policy=policy
    )
    current = [_NOW + timedelta(seconds=2)]
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(current[0]))
    monkeypatch.setattr(kernel, "clock", lambda: current[0])
    original = ledger._make_record

    def expire(*args: object, **kwargs: object) -> dict:
        value = original(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] == "CHANGE_EVENT":
            current[0] = policy.expires_at
        return value

    monkeypatch.setattr(ledger, "_make_record", expire)
    with pytest.raises(AdhelmError) as failed:
        accept_approval_receipt(
            kernel, ledger, receipt=receipt, expected_challenge=challenge, trust=trust
        )
    assert failed.value.code == "APPROVAL_RECORDING_UNVERIFIED"
    assert [record["record_kind"] for record in ledger.records("changes")] == [
        "SEGMENT_STARTED",
        "APPROVAL_RECEIPT_CLAIMED",
    ]


def test_clock_failure_during_locked_policy_preflight_is_bounded_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, challenge, receipt, trust = _policy_material(
        tmp_path, monkeypatch, policy=_fixture_policy()
    )
    calls = 0

    def failing_clock():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("private clock backend detail")
        return _NOW + timedelta(seconds=2)

    monkeypatch.setattr(kernel, "clock", failing_clock)
    with pytest.raises(AdhelmError) as failed:
        accept_approval_receipt(
            kernel, ledger, receipt=receipt, expected_challenge=challenge, trust=trust
        )
    assert failed.value.code == "APPROVAL_POLICY_STATE_UNVERIFIED"
    assert "private" not in str(failed.value)
    assert not any(
        row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in ledger.records("changes")
    )

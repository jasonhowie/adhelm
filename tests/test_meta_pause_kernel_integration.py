"""Configured planning and signed approval into the synthetic Meta kernel seam."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_meta_pause_kernel import _GuardedMetaAdapter
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    build_meta_campaign_pause_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import ApplyOutcome, EffectStatus, SafetyKernel
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


@pytest.mark.parametrize(
    ("response", "expected_status", "unresolved"),
    [
        (ApplyOutcome.CONFIRMED, EffectStatus.APPLIED_VERIFIED, False),
        (ApplyOutcome.AMBIGUOUS, EffectStatus.AMBIGUOUS_REMOTE_STATE, True),
    ],
)
def test_retained_configured_plan_approval_and_kernel_preserve_exact_history(
    runtime: _Runtime,
    response: ApplyOutcome,
    expected_status: EffectStatus,
    unresolved: bool,
) -> None:
    prepared = runtime.plan()
    record_digest = prepared["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    now = datetime.now(UTC)
    receipt_at = now + timedelta(microseconds=(-now.microsecond) % 1000)
    configuration = ApprovalHostConfiguration(
        host_ref="host:adhelm-macos-approval-v1",
        signing_key_ref="approval-key:synthetic-meta-integration",
    )
    challenge = build_meta_campaign_pause_challenge(
        action,
        grantor_ref="synthetic-customer",
        configuration=configuration,
        nonce=bytes(range(32)),
        issued_at=now,
        now=now,
        expires_at=now + timedelta(seconds=90),
    )
    key = ec.generate_private_key(ec.SECP256R1())
    signing = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id="approval-receipt:" + "I" * 43,
        issued_at=receipt_at,
    )
    receipt = parse_json(signing.canonical_unsigned_receipt)
    assert type(receipt) is dict
    signature = key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    receipt["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    kernel = SafetyKernel(runtime.root, ledger, clock=lambda: receipt_at)
    accepted = accept_approval_receipt(
        kernel,
        ledger,
        receipt=canonical_bytes(receipt),
        expected_challenge=challenge,
        trust=PinnedApprovalTrust(configuration, public, ledger.profile_ref),
        source_plan_record_digest=record_digest,
    )
    assert accepted.direct_grant is not None
    adapter = _GuardedMetaAdapter(action, ledger=ledger, outcome=response, state_after_apply=True)
    result = kernel.execute(action.plan, accepted.direct_grant, {"meta_ads": adapter})

    assert result.results[0].status is expected_status
    assert result.recovery_required is unresolved
    assert adapter.apply_calls == 1
    assert bool(ledger.unresolved_starts()) is unresolved
    changes = list(ledger.records("changes"))
    start = next(row for row in changes if row["payload"].get("event_type") == "APPLY_STARTED")
    outcome = next(row for row in changes if row["payload"].get("event_type") == "APPLY_RESULT")
    assert changes.index(start) < changes.index(outcome)
    assert start["payload"]["plan_digest"] == prepared["data"]["plan_digest"]
    assert outcome["payload"]["effect_status"] == expected_status.value

    if unresolved:
        reconciled = kernel.reconcile_run(action.plan, accepted.direct_grant, {"meta_ads": adapter})
        # Reconciliation records the observed state and releases the global write
        # barrier without settling or attributing this attempt (ticket S2).
        assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
        assert [
            row["payload"]["attempt_effect_status"]
            for row in ledger.records("changes")
            if row["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
        ] == ["AMBIGUOUS_REMOTE_STATE"]
        with pytest.raises(AdhelmError) as refused:
            kernel.execute(action.plan, accepted.direct_grant, {"meta_ads": adapter})
        assert refused.value.code in {"AUTHORIZATION_ALREADY_CONSUMED", "PLAN_ALREADY_STARTED"}
        assert adapter.apply_calls == 1

    # Only the planning inspector/account/campaign fake GETs were issued.
    # The adapter above simulates the effect without any Meta POST transport.
    assert runtime.paths == [
        "/v26.0/debug_token",
        "/v26.0/act_12345",
        "/v26.0/987654321",
    ]
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    assert _SUBJECT.encode() not in history
    assert _INSPECTOR.encode() not in history

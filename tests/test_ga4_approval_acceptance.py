"""Receipt acceptance binds one GA4 native review to one retained plan record."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_ga4_admin_execution_observation import _NOW
from test_ga4_create_workflow import _setup

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    build_ga4_key_event_create_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
from adhelm.safety import SafetyKernel

_CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1",
    signing_key_ref="approval-key:fixture-v1",
)


def _signed_receipt(challenge: ApprovalChallenge, key: ec.EllipticCurvePrivateKey) -> bytes:
    payload = receipt_signing_payload(
        challenge,
        configuration=_CONFIGURATION,
        receipt_id="approval-receipt:" + "A" * 43,
        issued_at=_NOW,
    )
    unsigned = parse_json(payload.canonical_unsigned_receipt)
    assert type(unsigned) is dict
    signature = key.sign(payload.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    unsigned["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(unsigned)


def _challenge(action):
    return build_ga4_key_event_create_challenge(
        action,
        grantor_ref="customer-grantor-1",
        expires_at=_NOW + timedelta(minutes=4),
        configuration=_CONFIGURATION,
        nonce=bytes(range(64, 96)),
        issued_at=_NOW,
        now=_NOW,
    )


def test_acceptance_requires_and_records_the_exact_retained_ga4_create_plan(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    monkeypatch.setattr(
        ledger_module, "_utc_now", lambda: current[0].isoformat().replace("+00:00", "Z")
    )
    challenge = _challenge(retained.action)
    key = ec.generate_private_key(ec.SECP256R1())
    receipt = _signed_receipt(challenge, key)
    trust = PinnedApprovalTrust(
        _CONFIGURATION,
        key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        ),
        ledger.profile_ref,
    )
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: current[0])

    accepted = accept_approval_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=challenge,
        trust=trust,
        source_plan_record_digest=retained.plan_record_digest,
    )

    assert accepted.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert accepted.direct_grant is not None
    assert accepted.direct_grant.plan_digest == retained.action.plan.plan_digest
    changes = ledger.records("changes")
    claim = changes[-2]
    event = changes[-1]
    assert claim["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    assert claim["payload"]["receipt_kind"] == "GA4_KEY_EVENT_CREATE_PLAN"
    assert claim["payload"]["source_plan_record_digest"] == retained.plan_record_digest
    assert event["record_kind"] == "CHANGE_EVENT"
    assert event["payload"]["event_type"] == "APPROVAL_GRANTED"


def test_acceptance_rejects_a_missing_or_different_ga4_plan_before_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    challenge = _challenge(retained.action)
    key = ec.generate_private_key(ec.SECP256R1())
    trust = PinnedApprovalTrust(
        _CONFIGURATION,
        key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        ),
        ledger.profile_ref,
    )
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: current[0])
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as raised:
        accept_approval_receipt(
            kernel,
            ledger,
            receipt=_signed_receipt(challenge, key),
            expected_challenge=challenge,
            trust=trust,
            source_plan_record_digest="f" * 64,
        )

    assert raised.value.code == "GA4_CREATE_PLAN_NOT_RETAINED"
    assert ledger.checkpoint()["checkpoint_digest"] == before

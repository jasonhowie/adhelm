"""Independent adversarial checks for GA4 CREATE native approval acceptance."""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_ga4_approval_protocol import (
    _CONFIGURATION,
    _NOW,
    _action,
    _challenge,
    _fixture,
)
from test_ga4_create_workflow import _setup

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    approval_review_facts,
    build_ga4_key_event_create_challenge,
    decode_approval_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
from adhelm.projections import export_verified
from adhelm.safety import SafetyKernel


def _challenge_wire(mutator) -> bytes:
    value = json.loads(_fixture("ga4-key-event-create-challenge-v1"))
    assert type(value) is dict
    mutator(value)
    return canonical_bytes(value)


def _trust(key: ec.EllipticCurvePrivateKey, workspace_profile_ref: str) -> PinnedApprovalTrust:
    return PinnedApprovalTrust(
        _CONFIGURATION,
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        ),
        workspace_profile_ref,
    )


def _challenge_for(action, *, now=_NOW):
    return build_ga4_key_event_create_challenge(
        action,
        grantor_ref="customer-grantor-1",
        expires_at=now + timedelta(minutes=4),
        configuration=_CONFIGURATION,
        nonce=bytes(range(64, 96)),
        issued_at=now,
        now=now,
    )


def _signed_receipt_for(challenge, key: ec.EllipticCurvePrivateKey, *, issued_at) -> bytes:
    payload = receipt_signing_payload(
        challenge,
        configuration=_CONFIGURATION,
        receipt_id="approval-receipt:" + "A" * 43,
        issued_at=issued_at,
    )
    unsigned = parse_json(payload.canonical_unsigned_receipt)
    assert type(unsigned) is dict
    signature = key.sign(payload.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    unsigned["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(unsigned)


def _claim_count(ledger) -> int:
    return sum(
        record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for record in ledger.records("changes")
    )


@pytest.mark.parametrize(
    "raw",
    (
        _fixture("ga4-key-event-create-challenge-v1").replace(
            b'"schema_version":1', b'"schema_version":1.0', 1
        ),
        _fixture("ga4-key-event-create-challenge-v1").replace(
            b'"execution_authority":false', b'"execution_authority":0', 1
        ),
        _fixture("ga4-key-event-create-challenge-v1").replace(
            b'"defaultValue":null', b'"defaultValue":0', 1
        ),
        _challenge_wire(lambda value: value["action"].pop("native_request_binding")),
        _challenge_wire(
            lambda value: value["action"]["candidate"]["compilation"]["compiler_candidate"][
                "request"
            ].update({"unexpected": "not-a-request-field"})
        ),
    ),
)
def test_ga4_challenge_rejects_raw_scalar_null_missing_and_extra_field_bypasses(raw: bytes) -> None:
    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(raw, configuration=_CONFIGURATION, now=_NOW)

    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"
    assert "not-a-request-field" not in str(raised.value)


def test_constructed_action_cannot_promote_a_forged_semantic_transition() -> None:
    action = _action()
    forged = action.model_copy(
        update={
            "semantic_state": action.semantic_state.model_copy(
                update={"after_member_count": action.semantic_state.before_member_count}
            )
        }
    )

    with pytest.raises(AdhelmError) as raised:
        build_ga4_key_event_create_challenge(
            forged,
            grantor_ref="customer-grantor-1",
            expires_at=_NOW + timedelta(minutes=4),
            configuration=_CONFIGURATION,
            nonce=bytes(range(64, 96)),
            issued_at=_NOW,
            now=_NOW,
        )

    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_local_review_shows_safe_identity_and_change_but_not_raw_request_material() -> None:
    facts = approval_review_facts(_challenge(), configuration=_CONFIGURATION).to_data()
    ga4 = facts["ga4_key_event_create"]
    assert isinstance(ga4, dict)
    assert ga4["oauth_association"]["profile_ref"] == "ga4.edit.profile"
    assert ga4["target"]["property_client_ref"] == "123"
    assert ga4["target"]["web_stream_client_ref"] == "456"
    assert ga4["semantic_transition"]["event_name"] == "qualified_request"
    assert ga4["semantic_transition"]["counting_method"] == "ONCE_PER_EVENT"
    assert ga4["semantic_transition"]["default_value"] == "NONE"
    assert ga4["known_limits"]["execution_authority"] is False

    rendered = json.dumps(facts, sort_keys=True)
    for private in (
        "properties/123/keyEvents",
        "/v1alpha/properties/123/keyEvents",
        "principal.fixture",
        "client.fixture",
        "customer-defined qualified request",
        "fixture-edit-token",
    ):
        assert private not in rendered


def test_cross_kind_receipt_cannot_consume_a_ga4_plan_nonce(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    monkeypatch.setattr(
        ledger_module,
        "_utc_now",
        lambda: current[0].isoformat().replace("+00:00", "Z"),
    )
    challenge = _challenge_for(retained.action, now=current[0])
    key = ec.generate_private_key(ec.SECP256R1())
    receipt = parse_json(_signed_receipt_for(challenge, key, issued_at=current[0]))
    assert type(receipt) is dict
    receipt["receipt_kind"] = "OPENAI_PAUSE_PLAN"
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as raised:
        accept_approval_receipt(
            SafetyKernel(tmp_path, ledger, clock=lambda: current[0]),
            ledger,
            receipt=canonical_bytes(receipt),
            expected_challenge=challenge,
            trust=_trust(key, ledger.profile_ref),
            source_plan_record_digest=retained.plan_record_digest,
        )

    assert raised.value.code == "APPROVAL_RECEIPT_BINDING_MISMATCH"
    assert ledger.checkpoint()["checkpoint_digest"] == before
    assert _claim_count(ledger) == 0


def test_nonplan_history_digest_cannot_be_substituted_as_a_ga4_approval_source(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    monkeypatch.setattr(
        ledger_module,
        "_utc_now",
        lambda: current[0].isoformat().replace("+00:00", "Z"),
    )
    challenge = _challenge_for(retained.action, now=current[0])
    key = ec.generate_private_key(ec.SECP256R1())
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as raised:
        accept_approval_receipt(
            SafetyKernel(tmp_path, ledger, clock=lambda: current[0]),
            ledger,
            receipt=_signed_receipt_for(challenge, key, issued_at=current[0]),
            expected_challenge=challenge,
            trust=_trust(key, ledger.profile_ref),
            source_plan_record_digest=retained.action.observation_binding.observation_record_digest,
        )

    assert raised.value.code == "GA4_CREATE_PLAN_NOT_RETAINED"
    assert ledger.checkpoint()["checkpoint_digest"] == before
    assert _claim_count(ledger) == 0


def test_expired_or_replayed_ga4_receipt_never_creates_another_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    monkeypatch.setattr(
        ledger_module,
        "_utc_now",
        lambda: current[0].isoformat().replace("+00:00", "Z"),
    )
    initial = current[0]
    challenge = _challenge_for(retained.action, now=initial)
    key = ec.generate_private_key(ec.SECP256R1())
    receipt = _signed_receipt_for(challenge, key, issued_at=initial)
    trust = _trust(key, ledger.profile_ref)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: current[0])

    current[0] = challenge.expires_at
    before_expiry = ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as expired:
        accept_approval_receipt(
            kernel,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trust,
            source_plan_record_digest=retained.plan_record_digest,
        )
    assert expired.value.code == "APPROVAL_RECEIPT_EXPIRED"
    assert ledger.checkpoint()["checkpoint_digest"] == before_expiry
    assert _claim_count(ledger) == 0

    current[0] = initial
    accepted = accept_approval_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=challenge,
        trust=trust,
        source_plan_record_digest=retained.plan_record_digest,
    )
    assert accepted.kind.value == "GA4_KEY_EVENT_CREATE_PLAN"
    claimed_checkpoint = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as replay:
        accept_approval_receipt(
            kernel,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trust,
            source_plan_record_digest=retained.plan_record_digest,
        )
    assert replay.value.code == "APPROVAL_RECEIPT_REPLAY"
    assert ledger.checkpoint()["checkpoint_digest"] == claimed_checkpoint
    assert _claim_count(ledger) == 1


def test_exported_ga4_receipt_claim_drops_challenge_receipt_key_and_native_input(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    monkeypatch.setattr(
        ledger_module,
        "_utc_now",
        lambda: current[0].isoformat().replace("+00:00", "Z"),
    )
    challenge = _challenge_for(retained.action, now=current[0])
    key = ec.generate_private_key(ec.SECP256R1())
    accept_approval_receipt(
        SafetyKernel(tmp_path, ledger, clock=lambda: current[0]),
        ledger,
        receipt=_signed_receipt_for(challenge, key, issued_at=current[0]),
        expected_challenge=challenge,
        trust=_trust(key, ledger.profile_ref),
        source_plan_record_digest=retained.plan_record_digest,
    )

    export_verified(ledger, tmp_path / "export", opaque_key=b"x" * 32)
    lines = (tmp_path / "export" / "records.jsonl").read_text().splitlines()
    exported = [json.loads(line) for line in lines]
    claim = next(
        row["payload"]
        for row in exported
        if row["payload"].get("event_type") == "APPROVAL_RECEIPT_CLAIMED"
    )
    assert set(claim) == {
        "schema_version",
        "event_type",
        "evidence_scope",
        "workspace_profile_ref",
        "source_plan_record_digest",
        "configuration_digest",
        "public_key_fingerprint",
        "challenge_digest",
        "challenge_nonce_digest",
        "receipt_digest",
        "receipt_id",
        "receipt_kind",
        "accepted_at",
    }
    text = "\n".join(lines)
    for private in (
        "qualified_request",
        "properties/123/keyEvents",
        "principal.fixture",
        "client.fixture",
        "approval-key:fixture-v1",
        "host:adhelm-macos-approval-v1",
        "verification_public_key_x963_hex",
        '"challenge"',
        '"receipt"',
    ):
        assert private not in text

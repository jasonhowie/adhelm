"""Closed Python/native vectors for the exact GA4 key-event CREATE subject."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from adhelm.approval_protocol import (
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    Ga4KeyEventCreateApprovalSubject,
    approval_review_facts,
    build_ga4_key_event_create_challenge,
    decode_approval_challenge,
    receipt_signing_payload,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreatePlannedAction,
    revalidate_ga4_key_event_create_planned_action,
)

_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures"
)
_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1",
    signing_key_ref="approval-key:fixture-v1",
)
_SWIFT_INTEROPERABILITY_CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1",
    signing_key_ref="key:approval-test-v1",
)
_SWIFT_INTEROPERABILITY_VECTOR = (
    Path(__file__).parent / "fixtures/swift-ga4-create-approval-receipt-v1.json"
)
_SWIFT_INTEROPERABILITY_NOW = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)


def _fixture(name: str) -> bytes:
    suffix = "" if name.endswith(".json") else ".json"
    return (_FIXTURES / f"{name}{suffix}").read_bytes()


def _action() -> Ga4KeyEventCreatePlannedAction:
    raw = json.loads(_fixture("ga4-key-event-create-action-v1"))
    assert type(raw) is dict
    return revalidate_ga4_key_event_create_planned_action(raw)


def _nonce() -> bytes:
    raw = json.loads(_fixture("ga4-key-event-create-challenge-v1"))["nonce"]
    assert type(raw) is str
    return base64.urlsafe_b64decode(raw + "=")


def _challenge():
    return build_ga4_key_event_create_challenge(
        _action(),
        grantor_ref="customer-grantor-1",
        expires_at=_NOW + timedelta(minutes=4),
        configuration=_CONFIGURATION,
        nonce=_nonce(),
        issued_at=_NOW,
        now=_NOW,
    )


def _signed_receipt(challenge, key: ec.EllipticCurvePrivateKey) -> bytes:
    payload = receipt_signing_payload(
        challenge,
        configuration=_CONFIGURATION,
        receipt_id="approval-receipt:" + "A" * 43,
        issued_at=_NOW,
    )
    wire = parse_json(payload.canonical_unsigned_receipt)
    assert type(wire) is dict
    signature = key.sign(payload.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    wire["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(wire)


def test_python_builder_matches_the_checked_in_native_ga4_challenge_vector() -> None:
    challenge = _challenge()

    assert challenge.canonical_bytes == _fixture("ga4-key-event-create-challenge-v1")
    assert challenge.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert isinstance(challenge.subject, Ga4KeyEventCreateApprovalSubject)
    assert challenge.ga4_action == _action()


def test_ga4_review_facts_match_native_vector_show_safe_identity_and_redact_sources() -> None:
    facts = approval_review_facts(_challenge(), configuration=_CONFIGURATION)

    assert facts.canonical_bytes == _fixture("ga4-key-event-create-review-v1")
    text = facts.canonical_bytes.decode("utf-8")
    for private in (
        "customer-defined qualified request",
        "synthetic-fixture-token",
        "properties/123/keyEvents",
    ):
        assert private not in text
    parsed = facts.to_data()
    ga4 = parsed["ga4_key_event_create"]
    assert isinstance(ga4, dict)
    assert ga4["fixture_status"] == "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    assert ga4["known_limits"]["execution_authority"] is False
    assert ga4["oauth_association"]["profile_ref"] == "ga4.edit.profile"
    assert ga4["target"]["property_client_ref"] == "123"
    assert ga4["target"]["web_stream_client_ref"] == "456"
    assert ga4["semantic_transition"]["event_name"] == "qualified_request"
    assert ga4["semantic_transition"]["counting_method"] == "ONCE_PER_EVENT"
    assert ga4["semantic_transition"]["default_value"] == "NONE"
    assert ga4["semantic_transition"]["current_provider_state"] == "NOT_ESTABLISHED"


def test_ephemeral_p256_receipt_verifies_only_for_the_exact_ga4_challenge() -> None:
    challenge = _challenge()
    key = ec.generate_private_key(ec.SECP256R1())
    receipt = _signed_receipt(challenge, key)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_CONFIGURATION,
        trusted_public_key_x963=public_key,
        now=_NOW,
    )

    assert verified.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert verified.ga4_action == _action()
    assert verified.action_digest is not None
    assert verified.plan_digest == _action().plan.plan_digest
    assert verified.subplan_digests == _action().plan.subplan_digests


def test_actual_swift_signed_ga4_receipt_verifies_with_the_production_python_parser() -> None:
    """A public-only CryptoKit vector proves the cross-language receipt boundary."""
    material = parse_json(_SWIFT_INTEROPERABILITY_VECTOR.read_bytes())
    assert type(material) is dict
    assert set(material) == {
        "challenge",
        "fixture_name",
        "producer",
        "public_key_x963_hex",
        "receipt",
        "receipt_digest",
        "review_digest",
        "review_facts",
        "schema_version",
        "scope",
        "verified_at",
    }
    assert material["schema_version"] == 1
    assert material["producer"] == "SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"
    assert material["scope"] == "SYNTHETIC_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION"
    challenge_data = material["challenge"]
    receipt_data = material["receipt"]
    public_key_hex = material["public_key_x963_hex"]
    assert type(challenge_data) is dict
    assert type(receipt_data) is dict
    assert type(public_key_hex) is str
    assert b"private_key" not in canonical_bytes(material)

    challenge = decode_approval_challenge(
        canonical_bytes(challenge_data),
        configuration=_SWIFT_INTEROPERABILITY_CONFIGURATION,
        now=_SWIFT_INTEROPERABILITY_NOW,
    )
    facts = approval_review_facts(challenge, configuration=_SWIFT_INTEROPERABILITY_CONFIGURATION)
    assert challenge.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert facts.canonical_bytes == canonical_bytes(material["review_facts"])
    assert facts.digest == material["review_digest"]

    receipt = verify_approval_receipt(
        canonical_bytes(receipt_data),
        expected_challenge=challenge,
        configuration=_SWIFT_INTEROPERABILITY_CONFIGURATION,
        trusted_public_key_x963=bytes.fromhex(public_key_hex),
        now=_SWIFT_INTEROPERABILITY_NOW,
    )

    assert receipt.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert receipt.digest == material["receipt_digest"]
    assert receipt.review_digest == facts.digest
    assert receipt.challenge_digest == challenge.digest
    assert receipt.ga4_action == _action()


@pytest.mark.parametrize(
    "raw",
    (
        _fixture("ga4-key-event-create-challenge-v1").replace(
            b'"schema_version":1', b'"schema_version":1.0', 1
        ),
        _fixture("ga4-key-event-create-challenge-v1").replace(
            b'"execution_authority":false', b'"execution_authority":true', 1
        ),
    ),
)
def test_ga4_challenge_rejects_noncanonical_or_authority_tampering(raw: bytes) -> None:
    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(raw, configuration=_CONFIGURATION, now=_NOW)
    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_ga4_builder_rejects_copied_action_and_challenge_expiry_after_plan() -> None:
    copied = _action().model_copy(update={"execution_authority": True})
    with pytest.raises(AdhelmError) as copied_error:
        build_ga4_key_event_create_challenge(
            copied,
            grantor_ref="customer-grantor-1",
            expires_at=_NOW + timedelta(minutes=4),
            configuration=_CONFIGURATION,
            nonce=_nonce(),
            issued_at=_NOW,
            now=_NOW,
        )
    assert copied_error.value.code == "INVALID_APPROVAL_CHALLENGE"

    with pytest.raises(AdhelmError) as expiry_error:
        build_ga4_key_event_create_challenge(
            _action(),
            grantor_ref="customer-grantor-1",
            expires_at=_action().plan.expires_at + timedelta(seconds=1),
            configuration=_CONFIGURATION,
            nonce=_nonce(),
            issued_at=_NOW,
            now=_NOW,
        )
    assert expiry_error.value.code == "INVALID_APPROVAL_CHALLENGE"

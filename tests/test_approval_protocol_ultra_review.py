"""Independent checks against actual Swift CryptoKit receipt bytes."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_approval_protocol import _config, _private_key, _public_x963

from adhelm.approval_protocol import (
    ApprovalChallengeKind,
    approval_review_facts,
    parse_approval_challenge_wire,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError

_VECTOR_FILE = Path(__file__).parent / "fixtures/swift-approval-receipts-v1.json"
_MATERIAL = parse_json(_VECTOR_FILE.read_bytes())
_NOW = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("vector", _MATERIAL["vectors"], ids=lambda v: v["fixture_name"])
def test_actual_swift_signed_receipt_and_review_bytes_verify_in_python(vector: dict) -> None:
    challenge = parse_approval_challenge_wire(
        canonical_bytes(vector["challenge"]), configuration=_config()
    )
    facts = approval_review_facts(challenge, configuration=_config())
    assert facts.canonical_bytes == canonical_bytes(vector["review_facts"])
    assert facts.digest == vector["review_digest"]
    receipt = verify_approval_receipt(
        canonical_bytes(vector["receipt"]),
        expected_challenge=challenge,
        configuration=_config(),
        trusted_public_key_x963=bytes.fromhex(_MATERIAL["public_key_x963_hex"]),
        now=_NOW,
    )
    assert receipt.digest == vector["receipt_digest"]
    assert receipt.review_digest == facts.digest
    assert receipt.challenge_digest == challenge.digest
    assert receipt.kind is ApprovalChallengeKind(vector["receipt"]["receipt_kind"])


@pytest.mark.parametrize("vector", _MATERIAL["vectors"], ids=lambda v: v["fixture_name"])
def test_actual_swift_receipt_does_not_verify_with_a_receipt_selected_replacement_pin(
    vector: dict,
) -> None:
    challenge = parse_approval_challenge_wire(
        canonical_bytes(vector["challenge"]), configuration=_config()
    )
    with pytest.raises(AdhelmError) as rejected:
        verify_approval_receipt(
            canonical_bytes(vector["receipt"]),
            expected_challenge=challenge,
            configuration=_config(),
            trusted_public_key_x963=_public_x963(_private_key()),
            now=_NOW,
        )
    assert rejected.value.code == "INVALID_APPROVAL_RECEIPT_SIGNATURE"

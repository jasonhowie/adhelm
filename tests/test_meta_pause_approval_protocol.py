"""Synthetic Python/native vectors for the fixed Meta campaign PAUSE subject.

These tests exercise closed review and receipt binding only.  They never open
an approval bridge, credential store, native host, provider, or ledger.
"""

from __future__ import annotations

import base64
import hashlib
import runpy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    MetaCampaignPauseApprovalSubject,
    approval_review_facts,
    build_meta_campaign_pause_challenge,
    decode_approval_challenge,
    parse_approval_receipt_wire,
    receipt_signing_payload,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
    revalidate_meta_campaign_pause_planned_action,
)

_FIXTURE_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause"
)
_FIXTURE_PATH = _FIXTURE_DIRECTORY / "meta-campaign-pause-approval-v1.json"
_FRACTIONAL_FIXTURE_PATH = _FIXTURE_DIRECTORY / "meta-campaign-pause-approval-fractional-v1.json"
_GENERATOR_PATH = _FIXTURE_DIRECTORY / "generate_meta_pause_approval_vectors.py"
_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_FRACTIONAL_NOW = datetime(2030, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)


def _fixture() -> dict[str, Any]:
    value = parse_json(_FIXTURE_PATH.read_bytes())
    assert type(value) is dict
    return value


def _configuration() -> ApprovalHostConfiguration:
    data = _fixture()["configuration"]
    assert type(data) is dict
    return ApprovalHostConfiguration(**data)


def _challenge() -> ApprovalChallenge:
    data = _fixture()["challenge"]
    assert type(data) is dict
    return decode_approval_challenge(
        canonical_bytes(data), configuration=_configuration(), now=_NOW
    )


def _action() -> MetaCampaignPausePlannedAction:
    action = _challenge().meta_pause_action
    assert action is not None
    return action


def _signed_receipt(challenge: ApprovalChallenge, key: ec.EllipticCurvePrivateKey) -> bytes:
    unsigned = _fixture()["unsigned_receipt"]
    assert type(unsigned) is dict
    receipt_id = unsigned["receipt_id"]
    assert type(receipt_id) is str
    payload = receipt_signing_payload(
        challenge,
        configuration=_configuration(),
        receipt_id=receipt_id,
        issued_at=_NOW,
    )
    wire = parse_json(payload.canonical_unsigned_receipt)
    assert type(wire) is dict
    signature = key.sign(payload.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    wire["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(wire)


def test_generator_reproduces_the_checked_in_closed_meta_pause_fixture() -> None:
    namespace = runpy.run_path(str(_GENERATOR_PATH))
    build_fixture = cast(Callable[[], dict[str, Any]], namespace["build_fixture"])
    build_fractional_fixture = cast(
        Callable[[], dict[str, Any]], namespace["build_fractional_fixture"]
    )

    assert canonical_bytes(build_fixture()) == _FIXTURE_PATH.read_bytes()
    assert canonical_bytes(build_fractional_fixture()) == _FRACTIONAL_FIXTURE_PATH.read_bytes()


def test_meta_pause_challenge_and_safe_review_match_the_cross_language_fixture() -> None:
    fixture = _fixture()
    challenge = _challenge()
    review = approval_review_facts(challenge, configuration=_configuration())

    assert challenge.canonical_bytes == canonical_bytes(fixture["challenge"])
    assert challenge.digest == fixture["challenge_digest"]
    assert challenge.kind is ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
    assert isinstance(challenge.subject, MetaCampaignPauseApprovalSubject)
    assert challenge.meta_pause_action == _action()
    assert review.canonical_bytes == canonical_bytes(fixture["review_facts"])
    assert review.digest == fixture["review_digest"]

    facts = review.to_data()
    meta = facts["meta_campaign_pause"]
    assert type(meta) is dict
    assert meta["fixture_status"] == "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    assert meta["campaign_baseline"] == {
        "account_client_ref": "123456",
        "campaign_client_ref": "987654",
        "campaign_ref": "meta_ads.campaign.987654",
        "configured_status": "ACTIVE",
        "effective_status": "WITH_ISSUES",
    }
    assert meta["transition"] == {
        "configured_status_after": "PAUSED",
        "configured_status_before": "ACTIVE",
        "effective_delivery": "NOT_PREDICTED",
        "operation": "PAUSE",
        "postcondition_kind": "CONFIGURATION_ONLY",
    }
    assert meta["known_limits"] == {
        "conditional_write_proof": False,
        "configuration_readback": "REQUIRED_AFTER_PAUSE",
        "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
        "execution_authority": False,
        "idempotency_proof": False,
        "live_validated": False,
    }

    review_text = review.canonical_bytes.decode("utf-8")
    for forbidden in (
        "credential-binding:sha256:",
        "meta_token_principal_v1.",
        "configuration_digest",
        "profile_configuration_digest",
        "access_token",
        "provider_response",
    ):
        assert forbidden not in review_text


def test_unsigned_vector_and_ephemeral_p256_receipt_bind_the_exact_meta_action() -> None:
    fixture = _fixture()
    challenge = _challenge()
    payload = receipt_signing_payload(
        challenge,
        configuration=_configuration(),
        receipt_id="approval-receipt:" + "M" * 43,
        issued_at=_NOW,
    )
    assert payload.canonical_unsigned_receipt == canonical_bytes(fixture["unsigned_receipt"])
    assert (
        hashlib.sha256(payload.canonical_unsigned_receipt).hexdigest()
        == fixture["unsigned_receipt_digest"]
    )
    assert (
        hashlib.sha256(payload.domain_separated_bytes).hexdigest()
        == fixture["domain_separated_payload_sha256"]
    )

    key = ec.generate_private_key(ec.SECP256R1())
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    receipt = _signed_receipt(challenge, key)
    wire = parse_approval_receipt_wire(receipt)
    assert wire["receipt_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    assert wire["action_digest"] == hashlib.sha256(canonical_bytes(_action().to_data())).hexdigest()
    assert wire["plan_digest"] == _action().plan.plan_digest
    assert wire["subplan_digests"] == list(_action().plan.subplan_digests)
    assert "policy_digest" not in wire

    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_configuration(),
        trusted_public_key_x963=public_key,
        now=_NOW,
    )
    assert verified.kind is ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
    assert verified.meta_pause_action == _action()
    assert verified.action == _action()
    assert verified.action_digest == wire["action_digest"]
    assert verified.plan_digest == _action().plan.plan_digest
    assert verified.subplan_digests == _action().plan.subplan_digests


def test_nonzero_six_fraction_vector_remains_a_valid_distinct_python_wire() -> None:
    fixture = parse_json(_FRACTIONAL_FIXTURE_PATH.read_bytes())
    assert type(fixture) is dict
    configuration_data = fixture["configuration"]
    challenge_data = fixture["challenge"]
    unsigned_data = fixture["unsigned_receipt"]
    assert type(configuration_data) is dict
    assert type(challenge_data) is dict
    assert type(unsigned_data) is dict
    configuration = ApprovalHostConfiguration(**configuration_data)
    challenge = decode_approval_challenge(
        canonical_bytes(challenge_data), configuration=configuration, now=_FRACTIONAL_NOW
    )
    action = challenge.meta_pause_action
    assert action is not None
    action_data = action.to_data()
    execution = action_data["execution_binding"]
    observation = action_data["observation_binding"]
    plan = action_data["plan"]
    assert type(execution) is dict
    assert type(observation) is dict
    assert type(plan) is dict
    subplans = plan["subplans"]
    assert type(subplans) is list and len(subplans) == 1 and type(subplans[0]) is dict
    for timestamp in (
        execution["inspected_at"],
        execution["expires_at"],
        execution["data_access_expires_at"],
        observation["read_started_at"],
        observation["read_finished_at"],
        plan["expires_at"],
        subplans[0]["expires_at"],
    ):
        assert type(timestamp) is str and timestamp.endswith(".123456Z")

    facts = approval_review_facts(challenge, configuration=configuration)
    receipt_id = unsigned_data["receipt_id"]
    assert type(receipt_id) is str
    payload = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id=receipt_id,
        issued_at=_FRACTIONAL_NOW + timedelta(microseconds=544),
    )
    assert challenge.digest == fixture["challenge_digest"]
    assert facts.canonical_bytes == canonical_bytes(fixture["review_facts"])
    assert facts.digest == fixture["review_digest"]
    assert payload.canonical_unsigned_receipt == canonical_bytes(unsigned_data)
    assert (
        hashlib.sha256(payload.canonical_unsigned_receipt).hexdigest()
        == fixture["unsigned_receipt_digest"]
    )
    assert (
        hashlib.sha256(payload.domain_separated_bytes).hexdigest()
        == fixture["domain_separated_payload_sha256"]
    )


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (b'"schema_version":1', b'"schema_version":1.0'),
        (b'"minor_units":0', b'"minor_units":0.0'),
        (b'"execution_authority":false', b'"execution_authority":true'),
        (b'"configured_status":"ACTIVE"', b'"configured_status":"PAUSED"'),
    ),
)
def test_meta_pause_challenge_rejects_noncanonical_or_out_of_subject_action_data(
    original: bytes, replacement: bytes
) -> None:
    raw = canonical_bytes(_fixture()["challenge"])
    assert original in raw
    raw = raw.replace(original, replacement, 1)
    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(raw, configuration=_configuration(), now=_NOW)
    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_meta_pause_builder_rejects_copied_authority_and_expiry_beyond_the_plan() -> None:
    copied = _action().model_copy(update={"execution_authority": True})
    with pytest.raises(AdhelmError) as copied_error:
        build_meta_campaign_pause_challenge(
            copied,
            grantor_ref="customer-grantor-1",
            expires_at=_NOW + timedelta(minutes=4),
            configuration=_configuration(),
            nonce=bytes(range(96, 128)),
            issued_at=_NOW,
            now=_NOW,
        )
    assert copied_error.value.code == "INVALID_APPROVAL_CHALLENGE"

    short_plan = build_meta_campaign_pause_plan(
        workspace_profile_ref=_action().workspace_binding.workspace_profile_ref,
        account_binding=_action().account_binding,
        execution_binding=_action().execution_binding,
        observation_binding=_action().observation_binding,
        expires_at=_NOW + timedelta(minutes=4),
    )
    with pytest.raises(AdhelmError) as expiry_error:
        build_meta_campaign_pause_challenge(
            short_plan,
            grantor_ref="customer-grantor-1",
            expires_at=short_plan.plan.expires_at + timedelta(seconds=1),
            configuration=_configuration(),
            nonce=bytes(range(96, 128)),
            issued_at=_NOW,
            now=_NOW,
        )
    assert expiry_error.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_meta_pause_action_rejects_redundant_whole_second_fraction_before_hashing() -> None:
    """Keep the native action hash boundary aligned with M40's lexical UTC rule."""

    raw = _action().to_data()
    execution = raw["execution_binding"]
    assert type(execution) is dict
    execution["inspected_at"] = "2029-12-31T23:59:58.000000Z"

    with pytest.raises(AdhelmError) as raised:
        revalidate_meta_campaign_pause_planned_action(raw)
    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN"

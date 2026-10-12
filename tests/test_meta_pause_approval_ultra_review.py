"""Independent adversarial checks for the exact Meta PAUSE approval subject.

These tests exercise only synthetic local actions and ephemeral P-256 keys.
They do not start a native host, read credentials, or contact Meta.
"""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_meta_pause_plan_ultra_review import _START, _action, _execution, _observation

from adhelm._native_approval_bridge_protocol import build_native_approval_bridge_request_v1
from adhelm.approval_protocol import (
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    approval_review_facts,
    build_meta_campaign_pause_challenge,
    decode_approval_challenge,
    receipt_signing_payload,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError
from adhelm.providers.meta_pause_contract import (
    build_meta_campaign_pause_plan,
    revalidate_meta_campaign_pause_planned_action,
)

_CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1",
    signing_key_ref="approval-key:meta-ultra-review",
)
_NOW = _START + timedelta(seconds=3)
_FIXTURE_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause"
)


def _challenge(*, expires_at: datetime | None = None):
    action = _action()
    return build_meta_campaign_pause_challenge(
        action,
        grantor_ref="customer-grantor-1",
        expires_at=_NOW + timedelta(minutes=4) if expires_at is None else expires_at,
        configuration=_CONFIGURATION,
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )


def _wire(challenge) -> dict[str, Any]:
    data = challenge.to_data()
    assert type(data) is dict
    return deepcopy(data)


def _rebuilt_zero_fraction_action_data() -> dict[str, object]:
    """Build the native-style alternate spelling through every derived ID.

    The raw object stays deliberately invalid to Python.  Rebuilding its
    action and plan identifiers first ensures that rejection is lexical
    timestamp parity, rather than a stale derived identifier.
    """

    action = _action().to_data()
    execution = action["execution_binding"]
    observation = action["observation_binding"]
    plan = action["plan"]
    assert type(execution) is dict
    assert type(observation) is dict
    assert type(plan) is dict
    subplans = plan["subplans"]
    assert type(subplans) is list and len(subplans) == 1
    subplan = subplans[0]
    assert type(subplan) is dict

    def zero_fraction(value: object) -> str:
        assert type(value) is str
        assert value.endswith("Z") and "." not in value
        return value.removesuffix("Z") + ".000000Z"

    for field in ("inspected_at", "expires_at", "data_access_expires_at"):
        execution[field] = zero_fraction(execution[field])
    for field in ("read_started_at", "read_finished_at"):
        observation[field] = zero_fraction(observation[field])
    plan["expires_at"] = zero_fraction(plan["expires_at"])
    subplan["expires_at"] = zero_fraction(subplan["expires_at"])

    plan_expiry = plan["expires_at"]
    assert type(plan_expiry) is str
    action_id = digest(
        {
            "kind": "meta-campaign-pause-action-v1",
            "workspace": action["workspace_binding"],
            "account": action["account_binding"],
            "execution": execution,
            "observation": observation,
            "postcondition": action["postcondition"],
            "expires_at": plan_expiry.removesuffix("Z") + "+00:00",
            "status": "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_PAUSE",
            "recovery_semantics": "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED",
            "execution_authority": False,
            "live_validated": False,
            "conditional_write_proof": False,
            "idempotency_proof": False,
        }
    )
    action["action_id"] = action_id
    plan["plan_id"] = f"meta-campaign-pause-{action_id}"
    subplan["subplan_id"] = f"meta-campaign-pause-{action_id}"
    return action


def _signed_receipt(challenge, key: ec.EllipticCurvePrivateKey) -> bytes:
    signing = receipt_signing_payload(
        challenge,
        configuration=_CONFIGURATION,
        receipt_id="approval-receipt:" + "U" * 43,
        issued_at=_NOW,
    )
    unsigned = parse_json(signing.canonical_unsigned_receipt)
    assert type(unsigned) is dict
    signature = key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    unsigned["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(unsigned)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value["action"]["execution_binding"].update(
            {"configuration_digest": "e" * 64}
        ),
        lambda value: value["action"]["account_binding"].update({"currency": "EUR"}),
        lambda value: value["action"]["observation_binding"]["campaign"].update(
            {"effective_status": "PAUSED"}
        ),
    ),
)
def test_meta_challenge_rejects_any_canonical_outer_binding_substitution(mutate) -> None:
    """Every outer M40 fact is part of the reconstructed exact action."""

    raw = _wire(_challenge())
    mutate(raw)

    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(canonical_bytes(raw), configuration=_CONFIGURATION, now=_NOW)

    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_meta_challenge_rejects_noncanonical_nested_numeric_before_normalization() -> None:
    raw = _wire(_challenge())
    raw["action"]["plan"]["subplans"][0]["max_spend"]["minor_units"] = 0.0
    encoded = json.dumps(
        raw,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(encoded, configuration=_CONFIGURATION, now=_NOW)

    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_meta_timestamp_lexemes_reject_redundant_zero_fraction_but_keep_microseconds() -> None:
    """Keep Python's exact UTC spelling aligned with the native Meta decoder."""

    zero_fraction = _rebuilt_zero_fraction_action_data()
    plan = zero_fraction["plan"]
    assert type(plan) is dict
    subplans = plan["subplans"]
    assert type(subplans) is list and type(subplans[0]) is dict
    assert plan["plan_id"] == f"meta-campaign-pause-{zero_fraction['action_id']}"
    assert subplans[0]["subplan_id"] == f"meta-campaign-pause-{zero_fraction['action_id']}"
    with pytest.raises(AdhelmError) as zero_error:
        revalidate_meta_campaign_pause_planned_action(zero_fraction)
    assert zero_error.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN"

    action = build_meta_campaign_pause_plan(
        workspace_profile_ref="workspace-main",
        account_binding=_action().account_binding,
        execution_binding=_execution(
            inspected_at=_START + timedelta(microseconds=123456),
        ),
        observation_binding=_observation(
            read_started_at=_START + timedelta(seconds=1, microseconds=123456),
            read_finished_at=_START + timedelta(seconds=2, microseconds=123456),
        ),
        expires_at=_START + timedelta(minutes=10, microseconds=123456),
    )
    challenge = build_meta_campaign_pause_challenge(
        action,
        grantor_ref="customer-grantor-1",
        expires_at=_NOW + timedelta(minutes=4),
        configuration=_CONFIGURATION,
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )
    assert b".123456Z" in challenge.canonical_bytes
    assert (
        decode_approval_challenge(
            challenge.canonical_bytes, configuration=_CONFIGURATION, now=_NOW
        ).meta_pause_action
        == action
    )


def test_meta_review_discloses_only_the_fixed_local_review_facts() -> None:
    action = _action()
    facts = approval_review_facts(_challenge(), configuration=_CONFIGURATION).to_data()
    subject = facts["meta_campaign_pause"]
    assert isinstance(subject, dict)

    assert facts["approval_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    assert subject["fixture_status"] == "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    assert subject["local_association"]["required_scope"] == "ads_management"
    assert subject["campaign_baseline"] == {
        "account_client_ref": "123456",
        "campaign_client_ref": "987654",
        "campaign_ref": "meta_ads.campaign.987654",
        "configured_status": "ACTIVE",
        "effective_status": "WITH_ISSUES",
    }
    assert subject["transition"] == {
        "configured_status_after": "PAUSED",
        "configured_status_before": "ACTIVE",
        "effective_delivery": "NOT_PREDICTED",
        "operation": "PAUSE",
        "postcondition_kind": "CONFIGURATION_ONLY",
    }
    assert subject["plan"]["maximum_incremental_spend"] == {
        "currency": "USD",
        "minor_units": 0,
    }
    assert subject["known_limits"] == {
        "conditional_write_proof": False,
        "configuration_readback": "REQUIRED_AFTER_PAUSE",
        "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
        "execution_authority": False,
        "idempotency_proof": False,
        "live_validated": False,
    }

    rendered = json.dumps(facts, sort_keys=True)
    for forbidden in (
        action.execution_binding.credential_binding_ref,
        action.execution_binding.principal_ref,
        action.execution_binding.configuration_digest,
        action.execution_binding.profile_configuration_digest,
        "credential_binding_ref",
        "principal_ref",
        "configuration_digest",
        "profile_configuration_digest",
    ):
        assert forbidden not in rendered


def test_meta_receipt_binds_complete_action_and_never_enters_bridge_v1() -> None:
    challenge = _challenge()
    key = ec.generate_private_key(ec.SECP256R1())
    receipt = _signed_receipt(challenge, key)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )

    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_CONFIGURATION,
        trusted_public_key_x963=public_key,
        now=_NOW,
    )
    assert verified.kind is ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
    assert verified.meta_pause_action == _action()
    assert verified.plan_digest == _action().plan.plan_digest
    assert verified.subplan_digests == _action().plan.subplan_digests

    tampered = parse_json(receipt)
    assert type(tampered) is dict
    tampered["plan_digest"] = "0" * 64
    with pytest.raises(AdhelmError) as receipt_error:
        verify_approval_receipt(
            canonical_bytes(tampered),
            expected_challenge=challenge,
            configuration=_CONFIGURATION,
            trusted_public_key_x963=public_key,
            now=_NOW,
        )
    assert receipt_error.value.code == "APPROVAL_RECEIPT_BINDING_MISMATCH"

    with pytest.raises(AdhelmError) as bridge_error:
        build_native_approval_bridge_request_v1(
            challenge,
            configuration=_CONFIGURATION,
            request_id=b"r" * 32,
            source_context_digest="e" * 64,
            expected_metadata_digest="f" * 64,
        )
    assert bridge_error.value.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"


def test_meta_challenge_expiry_cannot_outlive_its_retained_plan() -> None:
    action = _action()
    with pytest.raises(AdhelmError) as raised:
        build_meta_campaign_pause_challenge(
            action,
            grantor_ref="customer-grantor-1",
            expires_at=action.plan.expires_at + timedelta(microseconds=1),
            configuration=_CONFIGURATION,
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )
    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


@pytest.mark.parametrize(
    ("fixture_name", "fixture_kind"),
    (
        ("meta-campaign-pause-approval-v1.json", "META_CAMPAIGN_PAUSE_APPROVAL_V1"),
        (
            "meta-campaign-pause-approval-fractional-v1.json",
            "META_CAMPAIGN_PAUSE_APPROVAL_FRACTIONAL_V1",
        ),
    ),
)
def test_checked_in_meta_fixture_preserves_closed_python_wire_parity(
    fixture_name: str, fixture_kind: str
) -> None:
    """The shared unsigned fixture has no signing or execution implication."""

    raw = (_FIXTURE_DIRECTORY / fixture_name).read_bytes()
    fixture = parse_json(raw)
    assert type(fixture) is dict
    assert set(fixture) == {
        "challenge",
        "challenge_digest",
        "configuration",
        "domain_separated_payload_sha256",
        "fixture_kind",
        "review_digest",
        "review_facts",
        "schema_version",
        "scope",
        "unsigned_receipt",
        "unsigned_receipt_digest",
    }
    assert fixture["schema_version"] == 1
    assert fixture["fixture_kind"] == fixture_kind
    assert (
        fixture["scope"]
        == "SYNTHETIC_PROTOCOL_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION_OR_EXECUTION"
    )
    assert b"private_key" not in raw

    configuration_data = fixture["configuration"]
    challenge_data = fixture["challenge"]
    review_data = fixture["review_facts"]
    unsigned_data = fixture["unsigned_receipt"]
    assert type(configuration_data) is dict
    assert type(challenge_data) is dict
    assert type(review_data) is dict
    assert type(unsigned_data) is dict
    configuration = ApprovalHostConfiguration(**configuration_data)
    issued_at = datetime.fromisoformat(str(challenge_data["issued_at"]).replace("Z", "+00:00"))
    challenge = decode_approval_challenge(
        canonical_bytes(challenge_data), configuration=configuration, now=issued_at
    )
    facts = approval_review_facts(challenge, configuration=configuration)
    assert challenge.kind is ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
    assert challenge.digest == fixture["challenge_digest"]
    assert facts.canonical_bytes == canonical_bytes(review_data)
    assert facts.digest == fixture["review_digest"]

    receipt_issued_at = datetime.fromisoformat(
        str(unsigned_data["issued_at"]).replace("Z", "+00:00")
    )
    unsigned = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id=str(unsigned_data["receipt_id"]),
        issued_at=receipt_issued_at,
    )
    assert unsigned.canonical_unsigned_receipt == canonical_bytes(unsigned_data)
    assert (
        hashlib.sha256(unsigned.canonical_unsigned_receipt).hexdigest()
        == fixture["unsigned_receipt_digest"]
    )
    assert (
        hashlib.sha256(unsigned.domain_separated_bytes).hexdigest()
        == fixture["domain_separated_payload_sha256"]
    )

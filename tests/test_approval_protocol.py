"""Offline vectors and adversarial tests for the closed Python approval bridge."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    DelegationPolicyInstallSubject,
    DelegationPolicyRevokeSubject,
    OpenAIPauseApprovalSubject,
    approval_review_facts,
    build_delegation_policy_install_challenge,
    build_delegation_policy_revoke_challenge,
    build_openai_pause_challenge,
    decode_approval_challenge,
    parse_approval_challenge_wire,
    parse_approval_receipt_wire,
    receipt_signing_payload,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.providers.openai_pause_contract import (
    OpenAIPauseBinding,
    OpenAIPausePlannedAction,
    build_openai_pause_plan,
)
from adhelm.safety import DelegationPolicy

_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures"
)
_HOST_REF = "host:adhelm-macos-approval-v1"
_KEY_REF = "key:approval-test-v1"
_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_EXPIRES = _NOW + timedelta(minutes=4)


def _config(**overrides: object) -> ApprovalHostConfiguration:
    values: dict[str, object] = {
        "host_ref": _HOST_REF,
        "signing_key_ref": _KEY_REF,
    }
    values.update(overrides)
    return ApprovalHostConfiguration(**values)  # type: ignore[arg-type]


def _fixture_json(name: str) -> dict[str, object]:
    loaded = json.loads((_FIXTURES / name).read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def _fixture_action() -> OpenAIPausePlannedAction:
    return revalidate_openai_pause_action(
        OpenAIPausePlannedAction.model_validate_json(
            (_FIXTURES / "openai-pause-action-v1.json").read_bytes()
        )
    )


def _fixture_policy() -> DelegationPolicy:
    return DelegationPolicy.model_validate_json(
        (_FIXTURES / "delegation-policy-v1.json").read_bytes()
    )


def _nonce(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4))


def _private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _public_x963(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _signed_receipt(
    challenge: ApprovalChallenge,
    *,
    key: ec.EllipticCurvePrivateKey,
    issued_at: datetime,
    receipt_id: str = "approval-receipt:" + "A" * 43,
    configuration: ApprovalHostConfiguration | None = None,
) -> bytes:
    payload = receipt_signing_payload(
        challenge,
        configuration=_config() if configuration is None else configuration,
        receipt_id=receipt_id,
        issued_at=issued_at,
    )
    signature = key.sign(payload.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    unsigned = parse_json(payload.canonical_unsigned_receipt)
    assert isinstance(unsigned, dict)
    unsigned["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(unsigned)


def _pause_challenge() -> ApprovalChallenge:
    fixture = _fixture_json("openai-pause-challenge-v1.json")
    return build_openai_pause_challenge(
        _fixture_action(),
        grantor_ref="customer-grantor-1",
        expires_at=_EXPIRES,
        configuration=_config(),
        nonce=_nonce(str(fixture["nonce"])),
        issued_at=_NOW,
        now=_NOW,
    )


def test_host_configuration_is_closed_canonical_and_non_secret() -> None:
    config = _config()
    assert asdict(config) == config.to_data()
    assert config.canonical_bytes == canonical_bytes(config.to_data())
    assert len(config.configuration_digest) == 64
    with pytest.raises(ValueError):
        ApprovalHostConfiguration(host_ref="host\ninvalid", signing_key_ref="key")
    with pytest.raises(ValueError):
        ApprovalHostConfiguration(
            host_ref="host", signing_key_ref="key", maximum_challenge_bytes=True
        )
    with pytest.raises(TypeError):
        ApprovalHostConfiguration(host_ref="host", signing_key_ref="key", unknown="value")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "name",
    (
        "openai-pause-challenge-v1.json",
        "openai-pause-whole-seconds-challenge-v1.json",
        "delegation-policy-install-challenge-v1.json",
        "delegation-policy-revoke-challenge-v1.json",
    ),
)
def test_decode_matches_the_checked_in_native_challenge_vectors(name: str) -> None:
    raw = (_FIXTURES / name).read_bytes()
    challenge = decode_approval_challenge(raw, configuration=_config(), now=_NOW)
    assert challenge.canonical_bytes == raw
    assert challenge.digest == __import__("hashlib").sha256(raw).hexdigest()
    assert challenge.to_data() == _fixture_json(name)


def test_builders_match_native_pause_and_policy_vectors_exactly() -> None:
    pause_fixture = _fixture_json("openai-pause-challenge-v1.json")
    pause = build_openai_pause_challenge(
        _fixture_action(),
        grantor_ref="customer-grantor-1",
        expires_at=_EXPIRES,
        configuration=_config(),
        nonce=_nonce(str(pause_fixture["nonce"])),
        issued_at=_NOW,
        now=_NOW,
    )
    assert pause.canonical_bytes == (_FIXTURES / "openai-pause-challenge-v1.json").read_bytes()

    policy = _fixture_policy()
    install_fixture = _fixture_json("delegation-policy-install-challenge-v1.json")
    install = build_delegation_policy_install_challenge(
        policy,
        expires_at=_EXPIRES,
        configuration=_config(),
        nonce=_nonce(str(install_fixture["nonce"])),
        issued_at=_NOW,
        now=_NOW,
    )
    assert (
        install.canonical_bytes
        == (_FIXTURES / "delegation-policy-install-challenge-v1.json").read_bytes()
    )

    revoke = build_delegation_policy_revoke_challenge(
        policy,
        revoked_at=_NOW + timedelta(minutes=3),
        expires_at=_EXPIRES,
        configuration=_config(),
        nonce=_nonce(str(install_fixture["nonce"])),
        issued_at=_NOW,
        now=_NOW,
    )
    assert (
        revoke.canonical_bytes
        == (_FIXTURES / "delegation-policy-revoke-challenge-v1.json").read_bytes()
    )


def test_review_facts_bind_native_semantics_without_credential_binding_value() -> None:
    facts = approval_review_facts(_pause_challenge(), configuration=_config()).to_data()
    assert facts["approval_kind"] == "OPENAI_PAUSE_PLAN"
    pause = facts["openai_pause"]
    assert isinstance(pause, dict)
    account = pause["account"]
    assert isinstance(account, dict)
    assert account["credential_binding_status"] == "BOUND_NOT_DISPLAYED"
    assert "credential_binding_ref" not in account
    assert pause["transition"] == {
        "operation": "PAUSE",
        "status_after": "paused",
        "status_before": "active",
    }
    assert facts["challenge_nonce"] == _fixture_json("openai-pause-challenge-v1.json")["nonce"]


@pytest.mark.parametrize(
    ("kind", "subject_type"),
    (
        (ApprovalChallengeKind.OPENAI_PAUSE_PLAN, OpenAIPauseApprovalSubject),
        (ApprovalChallengeKind.DELEGATION_POLICY_INSTALL, DelegationPolicyInstallSubject),
        (ApprovalChallengeKind.DELEGATION_POLICY_REVOKE, DelegationPolicyRevokeSubject),
    ),
)
def test_all_kinds_expose_their_typed_subjects(
    kind: ApprovalChallengeKind, subject_type: type[object]
) -> None:
    fixture = {
        ApprovalChallengeKind.OPENAI_PAUSE_PLAN: "openai-pause-challenge-v1.json",
        ApprovalChallengeKind.DELEGATION_POLICY_INSTALL: (
            "delegation-policy-install-challenge-v1.json"
        ),
        ApprovalChallengeKind.DELEGATION_POLICY_REVOKE: (
            "delegation-policy-revoke-challenge-v1.json"
        ),
    }[kind]
    challenge = decode_approval_challenge(
        (_FIXTURES / fixture).read_bytes(), configuration=_config(), now=_NOW
    )
    assert challenge.kind is kind
    assert isinstance(challenge.subject, subject_type)
    assert challenge.grantor_ref == "customer-grantor-1"


@pytest.mark.parametrize(
    "raw",
    (
        b' {"schema_version":1}',
        b'{"schema_version":1,"schema_version":1}',
        (_FIXTURES / "openai-pause-challenge-v1.json")
        .read_bytes()
        .replace(b'"schema_version":1', b'"schema_version":1.0', 1),
        (_FIXTURES / "openai-pause-challenge-v1.json")
        .read_bytes()
        .replace(
            b'"nonce":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"',
            b'"nonce":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh9"',
        ),
    ),
)
def test_challenge_rejects_noncanonical_duplicate_float_and_nonce_pad_bits(raw: bytes) -> None:
    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(raw, configuration=_config(), now=_NOW)
    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_nested_closed_scalar_types_cannot_normalize_through_json_or_model_validation() -> None:
    raw = _fixture_json("openai-pause-challenge-v1.json")
    action = raw["action"]
    assert isinstance(action, dict)
    plan = action["plan"]
    assert isinstance(plan, dict)
    plan["stop_on_non_verified"] = 1
    with pytest.raises(AdhelmError) as pause_type:
        decode_approval_challenge(canonical_bytes(raw), configuration=_config(), now=_NOW)
    assert pause_type.value.code == "INVALID_APPROVAL_CHALLENGE"
    policy = _fixture_json("delegation-policy-install-challenge-v1.json")
    nested_policy = policy["policy"]
    assert isinstance(nested_policy, dict)
    nested_policy["policy_version"] = True
    with pytest.raises(AdhelmError) as policy_type:
        decode_approval_challenge(canonical_bytes(policy), configuration=_config(), now=_NOW)
    assert policy_type.value.code == "INVALID_APPROVAL_CHALLENGE"


def test_current_decode_enforces_initial_age_but_historical_wire_does_not() -> None:
    raw = (_FIXTURES / "openai-pause-challenge-v1.json").read_bytes()
    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(raw, configuration=_config(), now=_NOW + timedelta(minutes=2))
    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"
    historical = parse_approval_challenge_wire(raw, configuration=_config())
    assert historical.issued_at == _NOW


def test_policy_host_binding_fails_closed() -> None:
    policy = _fixture_policy().model_copy(update={"configuration_host_ref": "host:other"})
    with pytest.raises(AdhelmError):
        build_delegation_policy_install_challenge(
            policy,
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )


def _pause_action_with_timezone(name: str) -> OpenAIPausePlannedAction:
    original = _fixture_action()
    return build_openai_pause_plan(
        OpenAIPauseBinding.model_validate(
            {**original.binding.model_dump(mode="python"), "account_timezone": name}
        ),
        expires_at=original.plan.expires_at,
        workspace_profile_ref="workspace-ledger-1",
    )


def test_pause_timezone_is_a_validated_iana_name_rather_than_a_fixed_pair() -> None:
    """ADR 0012 retired the two-entry allowlist; a real IANA name now passes.

    The two names the earlier allowlist held still build byte-identically -- the
    checked-in vectors in this module prove that -- but a plan carrying another
    current IANA zone is no longer refused by the Python builder alone.
    """

    challenge = build_openai_pause_challenge(
        _pause_action_with_timezone("Europe/London"),
        grantor_ref="customer-grantor-1",
        expires_at=_EXPIRES,
        configuration=_config(),
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )
    assert challenge.kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN
    assert challenge.action is not None
    assert challenge.action.binding.account_timezone == "Europe/London"


@pytest.mark.parametrize(
    "name",
    (
        "Not/A_Real_Zone",
        "posix/UTC",
        "right/UTC",
        "Factory",
        "localtime",
        "../../etc/passwd",
        "UTC ",
        "a" * 65,
    ),
)
def test_a_name_that_is_not_a_current_iana_zone_fails_closed(name: str) -> None:
    try:
        action = _pause_action_with_timezone(name)
    except (AdhelmError, ValidationError):
        return
    with pytest.raises(AdhelmError):
        build_openai_pause_challenge(
            action,
            grantor_ref="customer-grantor-1",
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )


@pytest.mark.parametrize("invalid_expiry", ("PRIVATE-UNTRUSTED-EXPIRY", True, None))
def test_builders_sanitize_invalid_pure_input(invalid_expiry: object) -> None:
    with pytest.raises(AdhelmError) as raised:
        build_openai_pause_challenge(
            _fixture_action(),
            grantor_ref="PRIVATE-UNTRUSTED-GRANTOR",
            expires_at=invalid_expiry,  # type: ignore[arg-type]
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )
    assert raised.value.code == "INVALID_APPROVAL_CHALLENGE"
    assert "PRIVATE-UNTRUSTED-EXPIRY" not in str(raised.value)
    assert "PRIVATE-UNTRUSTED-GRANTOR" not in str(raised.value)


def test_constructed_action_and_policy_cannot_bypass_the_closed_builder_boundary() -> None:
    action = _fixture_action()
    forged_action = OpenAIPausePlannedAction.model_construct(
        schema_version=True,
        status=action.status,
        binding=action.binding,
        workspace_binding=action.workspace_binding,
        plan=action.plan,
    )
    with pytest.raises(AdhelmError):
        build_openai_pause_challenge(
            forged_action,
            grantor_ref="customer-grantor-1",
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )
    policy = _fixture_policy()
    forged_policy = DelegationPolicy.model_construct(
        **{
            **policy.model_dump(mode="python"),
            "policy_version": True,
        }
    )
    with pytest.raises(AdhelmError):
        build_delegation_policy_install_challenge(
            forged_policy,
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )


def test_receipt_verifies_exact_signing_domain_and_exposes_closed_bindings() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_config(),
        trusted_public_key_x963=_public_x963(key),
        now=_NOW + timedelta(seconds=2),
    )
    assert verified.kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN
    assert verified.action is not None
    assert verified.policy is None
    assert challenge.action is not None
    assert verified.action_digest is not None
    assert verified.plan_digest == challenge.action.plan.plan_digest
    assert verified.subplan_digests == challenge.action.plan.subplan_digests
    assert verified.configuration_digest == _config().configuration_digest
    assert verified.to_data() == parse_approval_receipt_wire(receipt)
    assert not hasattr(verified, "authorizes_execution")


def test_receipt_does_not_repeat_initial_challenge_age_after_long_review() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(minutes=2))
    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_config(),
        trusted_public_key_x963=_public_x963(key),
        now=_NOW + timedelta(minutes=2, seconds=1),
    )
    assert verified.issued_at == _NOW + timedelta(minutes=2)


@pytest.mark.parametrize(
    "name",
    (
        "delegation-policy-install-challenge-v1.json",
        "delegation-policy-revoke-challenge-v1.json",
    ),
)
def test_policy_receipts_bind_exact_policy_subjects(name: str) -> None:
    challenge = decode_approval_challenge(
        (_FIXTURES / name).read_bytes(), configuration=_config(), now=_NOW
    )
    key = _private_key()
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_config(),
        trusted_public_key_x963=_public_x963(key),
        now=_NOW + timedelta(seconds=2),
    )
    assert verified.policy is not None
    assert verified.policy_digest == verified.policy.policy_digest
    assert verified.policy_id == "native-approval-policy-1"
    assert verified.revoked_at == (
        _NOW + timedelta(minutes=3)
        if name == "delegation-policy-revoke-challenge-v1.json"
        else None
    )


def test_receipt_reparses_canonical_expected_challenge_instead_of_trusting_fields() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    object.__setattr__(challenge, "kind", ApprovalChallengeKind.DELEGATION_POLICY_REVOKE)
    object.__setattr__(challenge, "nonce", b"x" * 32)
    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=_config(),
        trusted_public_key_x963=_public_x963(key),
        now=_NOW + timedelta(seconds=2),
    )
    assert verified.kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN
    assert verified.challenge_digest != "x" * 64


def test_receipt_rejects_wrong_pin_tampered_binding_and_expiry() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    with pytest.raises(AdhelmError) as wrong_pin:
        verify_approval_receipt(
            receipt,
            expected_challenge=challenge,
            configuration=_config(),
            trusted_public_key_x963=_public_x963(_private_key()),
            now=_NOW + timedelta(seconds=2),
        )
    assert wrong_pin.value.code == "INVALID_APPROVAL_RECEIPT_SIGNATURE"
    tampered = parse_json(receipt)
    assert isinstance(tampered, dict)
    tampered["host_ref"] = "host:other"
    with pytest.raises(AdhelmError) as binding:
        verify_approval_receipt(
            canonical_bytes(tampered),
            expected_challenge=challenge,
            configuration=_config(),
            trusted_public_key_x963=_public_x963(key),
            now=_NOW + timedelta(seconds=2),
        )
    assert binding.value.code == "APPROVAL_RECEIPT_BINDING_MISMATCH"
    with pytest.raises(AdhelmError) as expired:
        verify_approval_receipt(
            receipt,
            expected_challenge=challenge,
            configuration=_config(),
            trusted_public_key_x963=_public_x963(key),
            now=_EXPIRES,
        )
    assert expired.value.code == "APPROVAL_RECEIPT_EXPIRED"


def test_receipt_rejects_noncanonical_signature_and_issue_time() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    data = parse_json(receipt)
    assert isinstance(data, dict)
    data["signature"] = "AA=="
    with pytest.raises(AdhelmError) as signature:
        parse_approval_receipt_wire(canonical_bytes(data))
    assert signature.value.code == "INVALID_APPROVAL_RECEIPT"
    with pytest.raises(AdhelmError) as issue:
        receipt_signing_payload(
            challenge,
            configuration=_config(),
            receipt_id="approval-receipt:" + "B" * 43,
            issued_at=_NOW + timedelta(microseconds=1),
        )
    assert issue.value.code == "INVALID_APPROVAL_RECEIPT"


def test_receipt_rejects_alternate_base64url_signature_pad_bits() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    for value in range(48):
        receipt = _signed_receipt(
            challenge,
            key=key,
            issued_at=_NOW + timedelta(seconds=1),
            receipt_id="approval-receipt:"
            + base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("="),
        )
        data = parse_json(receipt)
        assert isinstance(data, dict)
        signature = data["signature"]
        assert isinstance(signature, str)
        decoded = base64.urlsafe_b64decode(signature + "=" * ((4 - len(signature) % 4) % 4))
        remainder = len(decoded) % 3
        if remainder == 0:
            continue
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        index = alphabet.index(signature[-1])
        if (remainder == 1 and index % 16 != 0) or (remainder == 2 and index % 4 != 0):
            continue
        data["signature"] = signature[:-1] + alphabet[index + 1]
        with pytest.raises(AdhelmError):
            verify_approval_receipt(
                canonical_bytes(data),
                expected_challenge=challenge,
                configuration=_config(),
                trusted_public_key_x963=_public_x963(key),
                now=_NOW + timedelta(seconds=2),
            )
        return
    pytest.fail("unable to generate a signature with unused Base64URL pad bits")


def test_verifier_rejects_a_cryptographically_valid_receipt_with_an_invalid_issue_window() -> None:
    challenge = _pause_challenge()
    key = _private_key()
    receipt = _signed_receipt(
        challenge,
        key=key,
        issued_at=_NOW - timedelta(minutes=2),
    )
    with pytest.raises(AdhelmError) as raised:
        verify_approval_receipt(
            receipt,
            expected_challenge=challenge,
            configuration=_config(),
            trusted_public_key_x963=_public_x963(key),
            now=_NOW,
        )
    assert raised.value.code == "INVALID_APPROVAL_RECEIPT_TIME"


def test_protocol_errors_do_not_echo_untrusted_input() -> None:
    sentinel = b'{"secret":"PRIVATE-UNTRUSTED-PROTOCOL-SENTINEL"}'
    with pytest.raises(AdhelmError) as raised:
        decode_approval_challenge(sentinel, configuration=_config(), now=_NOW)
    assert "PRIVATE-UNTRUSTED-PROTOCOL-SENTINEL" not in str(raised.value)

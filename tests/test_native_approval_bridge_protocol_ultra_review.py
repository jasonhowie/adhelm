"""Independent adversarial coverage for the private M32 bridge wire contract.

These tests use only the checked synthetic cross-language fixture.  They do
not launch a helper, open a pipe or XPC connection, access a credential store,
write a ledger record, or call a provider.
"""

from __future__ import annotations

import base64
import hashlib
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeCandidateV1,
    NativeApprovalBridgeRequestV1,
    decode_native_approval_bridge_request_v1,
    derive_native_approval_bridge_source_context_digest_v1,
    revalidate_native_approval_bridge_candidate_v1,
    validate_native_approval_bridge_candidate_v1,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError

_ROOT = Path(__file__).resolve().parents[1]
_VECTORS = (
    _ROOT
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridge"
    / "native-approval-bridge-protocol-v1.json"
)
_ERROR_CODES = {
    "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID",
    "NATIVE_APPROVAL_BRIDGE_BINDING_INVALID",
    "NATIVE_APPROVAL_BRIDGE_TIME_INVALID",
}


def _document() -> dict[str, object]:
    raw = _VECTORS.read_bytes()
    document = parse_json(raw)
    assert type(document) is dict
    assert canonical_bytes(document) == raw
    return document


def _case(case_id: str = "swift_openai_pause") -> dict[str, object]:
    cases = _document()["source_cases"]
    assert type(cases) is list
    matches = [item for item in cases if type(item) is dict and item.get("case_id") == case_id]
    assert len(matches) == 1
    return matches[0]


def _metadata(case: dict[str, object]) -> dict[str, object]:
    value = case["metadata_fixture"]
    assert type(value) is dict
    return value


def _configuration(case: dict[str, object]) -> ApprovalHostConfiguration:
    metadata = _metadata(case)
    values = {
        name: metadata[name]
        for name in (
            "host_ref",
            "signing_key_ref",
            "maximum_challenge_bytes",
            "maximum_challenge_lifetime_seconds",
            "maximum_issued_at_skew_seconds",
            "maximum_subplans",
            "maximum_policy_items",
        )
    }
    assert type(values["host_ref"]) is str
    assert type(values["signing_key_ref"]) is str
    for name in (
        "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds",
        "maximum_issued_at_skew_seconds",
        "maximum_subplans",
        "maximum_policy_items",
    ):
        assert type(values[name]) is int
    return ApprovalHostConfiguration(**values)  # type: ignore[arg-type]


def _metadata_digest(case: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(_metadata(case))).hexdigest()


def _time(value: object) -> datetime:
    assert type(value) is str
    pattern = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(value, pattern).replace(tzinfo=UTC)


def _request(case: dict[str, object]) -> NativeApprovalBridgeRequestV1:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_native_approval_bridge_request_v1(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


def _candidate_data(case: dict[str, object]) -> dict[str, object]:
    value = case["candidate"]
    assert type(value) is dict
    return value


def _candidate_raw(data: dict[str, object]) -> bytes:
    return canonical_bytes(data)


def _validate(case: dict[str, object], raw: bytes) -> NativeApprovalBridgeCandidateV1:
    return validate_native_approval_bridge_candidate_v1(
        raw,
        request=_request(case),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        exchange_started_at=_time(case["exchange_started_at"]),
        current_now=_time(case["current_now"]),
    )


def _assert_rejected(case: dict[str, object], raw: bytes) -> AdhelmError:
    with pytest.raises(AdhelmError) as raised:
        _validate(case, raw)
    assert raised.value.code in _ERROR_CODES
    assert "sentinel" not in raised.value.message
    return raised.value


def _mutate_candidate(case: dict[str, object], field: str, replacement: object) -> AdhelmError:
    candidate = dict(_candidate_data(case))
    candidate[field] = replacement
    return _assert_rejected(case, _candidate_raw(candidate))


def test_cross_language_fixture_binds_both_direct_kinds_without_authority() -> None:
    for case_id in ("swift_openai_pause", "swift_ga4_key_event_create"):
        case = _case(case_id)
        evidence = _validate(case, str(case["candidate_canonical_json"]).encode("utf-8"))
        assert evidence.kind.value == _candidate_data(case)["receipt_kind"]
        assert evidence.request_digest == case["request_digest"]
        assert "non-authorizing" in repr(evidence)
        assert not hasattr(evidence, "canonical_bytes")
        assert not hasattr(evidence, "active_public_key_x963")


def test_candidate_rejects_matching_replacement_key_fingerprint_without_matching_signature() -> (
    None
):
    case = _case()
    replacement_key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    candidate = dict(_candidate_data(case))
    candidate["active_public_key_x963_b64url"] = (
        base64.urlsafe_b64encode(replacement_key).decode("ascii").rstrip("=")
    )
    candidate["active_public_key_fingerprint"] = hashlib.sha256(replacement_key).hexdigest()
    assert _assert_rejected(case, _candidate_raw(candidate)).code in _ERROR_CODES

    original = base64.urlsafe_b64decode(
        str(_candidate_data(case)["active_public_key_x963_b64url"])
        + "=" * ((4 - len(str(_candidate_data(case)["active_public_key_x963_b64url"])) % 4) % 4)
    )
    malformed_x963 = b"\x02" + original[1:]
    candidate = dict(_candidate_data(case))
    candidate["active_public_key_x963_b64url"] = (
        base64.urlsafe_b64encode(malformed_x963).decode("ascii").rstrip("=")
    )
    candidate["active_public_key_fingerprint"] = hashlib.sha256(malformed_x963).hexdigest()
    assert _assert_rejected(case, _candidate_raw(candidate)).code in _ERROR_CODES


def test_candidate_rejects_all_critical_scalar_aliases_and_noncanonical_frames() -> None:
    case = _case()
    for field, replacement in (
        ("schema_version", True),
        ("message_type", 1),
        ("request_id_digest", False),
        ("source_context_digest", 1.0),
        ("native_observation_status", False),
        ("native_observed_at", 1),
        ("canonical_challenge_b64url", True),
        ("canonical_receipt_b64url", 1.0),
    ):
        _mutate_candidate(case, field, replacement)

    canonical = str(case["candidate_canonical_json"]).encode("utf-8")
    raw_float_alias = canonical.replace(b'"schema_version":1', b'"schema_version":1.0', 1)
    _assert_rejected(case, raw_float_alias)
    _assert_rejected(case, canonical + b"\n")
    duplicate = canonical[:-1] + b',"schema_version":1}'
    _assert_rejected(case, duplicate)


def test_candidate_requires_exact_observation_boundaries_and_trusted_exchange_clock() -> None:
    case = _case()
    candidate = _candidate_data(case)
    receipt_wire = base64.urlsafe_b64decode(
        str(candidate["canonical_receipt_b64url"])
        + "=" * ((4 - len(str(candidate["canonical_receipt_b64url"])) % 4) % 4)
    )
    receipt = parse_json(receipt_wire)
    assert type(receipt) is dict
    issued_at = receipt["issued_at"]
    assert type(issued_at) is str and issued_at.endswith("Z")
    observed_at_issue = issued_at[:-1] + "000Z"
    assert len(observed_at_issue.encode("utf-8")) == 27

    accepted = dict(candidate)
    accepted["native_observed_at"] = observed_at_issue
    assert _validate(case, _candidate_raw(accepted)).native_observed_at == _time(observed_at_issue)

    expiry = candidate["expires_at"]
    assert type(expiry) is str and expiry.endswith("Z")
    _mutate_candidate(case, "native_observed_at", expiry[:-1] + ".000000Z")
    _mutate_candidate(case, "native_observed_at", "2030-01-01T00:00:30Z")

    raw = str(case["candidate_canonical_json"]).encode("utf-8")
    with pytest.raises(AdhelmError) as early_exchange:
        validate_native_approval_bridge_candidate_v1(
            raw,
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=datetime(2029, 12, 31, 23, 58, tzinfo=UTC),
            current_now=_time(case["current_now"]),
        )
    assert early_exchange.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"

    with pytest.raises(AdhelmError) as rollback:
        validate_native_approval_bridge_candidate_v1(
            raw,
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["current_now"]),
            current_now=_time(case["exchange_started_at"]),
        )
    assert rollback.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"

    with pytest.raises(AdhelmError) as at_expiry:
        validate_native_approval_bridge_candidate_v1(
            raw,
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(candidate["expires_at"]),
        )
    assert at_expiry.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"


def test_request_and_candidate_bind_source_and_static_metadata_without_raw_input_leaks() -> None:
    case = _case()
    context = case["source_context"]
    assert type(context) is dict
    workspace = context["workspace_profile_ref"]
    source = context["source_plan_record_digest"]
    checkpoint = context["checkpoint_digest"]
    assert type(workspace) is str
    assert type(source) is str
    assert type(checkpoint) is str
    original = derive_native_approval_bridge_source_context_digest_v1(
        workspace_profile_ref=workspace,
        source_plan_record_digest=source,
        checkpoint_digest=checkpoint,
    )
    assert original == case["source_context_digest"]
    assert (
        derive_native_approval_bridge_source_context_digest_v1(
            workspace_profile_ref=workspace + ".other",
            source_plan_record_digest=source,
            checkpoint_digest=checkpoint,
        )
        != original
    )
    assert (
        derive_native_approval_bridge_source_context_digest_v1(
            workspace_profile_ref=workspace,
            source_plan_record_digest=source,
            checkpoint_digest="0" * 64,
        )
        != original
    )

    for field, replacement in (
        ("request_digest", "0" * 64),
        ("metadata_digest", "0" * 64),
        ("protocol_configuration_digest", "0" * 64),
        ("canonical_challenge_b64url", "AA"),
    ):
        assert _mutate_candidate(case, field, replacement).code == (
            "NATIVE_APPROVAL_BRIDGE_BINDING_INVALID"
        )


def test_private_values_do_not_serialize_and_revalidation_ignores_constructed_slots() -> None:
    case = _case()
    request = _request(case)
    candidate = _validate(case, str(case["candidate_canonical_json"]).encode("utf-8"))

    assert "canonical_challenge_b64url" not in repr(request)
    assert "canonical_receipt_b64url" not in repr(candidate)
    with pytest.raises(TypeError):
        vars(request)
    with pytest.raises(TypeError):
        vars(candidate)
    with pytest.raises(TypeError):
        pickle.dumps(request)
    with pytest.raises(TypeError):
        pickle.dumps(candidate)

    forged = object.__new__(NativeApprovalBridgeCandidateV1)
    object.__setattr__(forged, "_NativeApprovalBridgeCandidateV1__canonical_bytes", b"{}")
    object.__setattr__(forged, "_NativeApprovalBridgeCandidateV1__canonical_receipt", b"sentinel")
    with pytest.raises(AdhelmError) as rejected:
        revalidate_native_approval_bridge_candidate_v1(
            forged,
            request=request,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    assert rejected.value.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"
    assert "sentinel" not in rejected.value.message


def test_wire_byte_caps_fail_before_private_candidate_can_be_built() -> None:
    case = _case()
    with pytest.raises(AdhelmError) as request_error:
        decode_native_approval_bridge_request_v1(
            b"x" * 98_305,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
        )
    assert request_error.value.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"
    candidate_error = _assert_rejected(case, b"x" * 131_073)
    assert candidate_error.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"


def test_opaque_candidate_is_not_a_mapping_or_direct_authority_value() -> None:
    evidence: Any = _validate(_case(), str(_case()["candidate_canonical_json"]).encode("utf-8"))
    assert not isinstance(evidence, dict)
    assert not hasattr(evidence, "direct_grant")
    assert not hasattr(evidence, "trust")
    assert not hasattr(evidence, "permit")

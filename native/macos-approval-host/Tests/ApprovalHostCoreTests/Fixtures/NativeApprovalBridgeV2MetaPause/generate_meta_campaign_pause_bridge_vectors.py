"""Generate synthetic V2 Meta campaign PAUSE approval-bridge vectors.

The generator consumes the checked-in M42 whole-second and fractional approval
fixtures, revalidates their action, challenge, review, and unsigned receipt
payloads, then attaches fixed synthetic P-256 signatures.  It does not open a
credential store, contact a provider, create a host, consume a nonce, record a
grant, or execute a plan.

Run from the repository root:

``UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run python
native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/
NativeApprovalBridgeV2MetaPause/generate_meta_campaign_pause_bridge_vectors.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    build_meta_campaign_pause_approval_bridge_request_v2,
    derive_meta_campaign_pause_approval_bridge_source_context_digest_v2,
    validate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    approval_review_facts,
    parse_approval_challenge_wire,
    receipt_signing_payload,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json

FIXTURE_DIRECTORY = Path(__file__).parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[6]
OUTPUT_PATH = FIXTURE_DIRECTORY / "meta-campaign-pause-approval-bridge-v2.json"
_M42_DIRECTORY = (
    REPOSITORY_ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause"
)
WHOLE_SOURCE_PATH = _M42_DIRECTORY / "meta-campaign-pause-approval-v1.json"
FRACTIONAL_SOURCE_PATH = _M42_DIRECTORY / "meta-campaign-pause-approval-fractional-v1.json"
_MAXIMUM_SOURCE_BYTES = 131_072
_OUTPUT_SCOPE = (
    "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_INTEROPERABILITY_ONLY_"
    "NOT_ACCEPTANCE_OR_AUTHORIZATION"
)
_M42_SCOPE = "SYNTHETIC_PROTOCOL_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION_OR_EXECUTION"
_PUBLIC_KEY_X963_HEX = (
    "04031ff50c7a88b0d4e9fafef22ea004a8a72adfc9d235fe3a00593d3eae9a55"
    "f0d2318ea6d224cd220d5a9f1030acf18df073fa2d2f9b03167f833f6cabaf8cc5"
)
_SIGNED_RECEIPT_SIGNATURES = {
    "whole": (
        "MEYCIQC9jfu1Fu2PF6LI3SWTRdSZ8aFA_hReK8DoaA-d1STlLwIhAN24fuWUME"
        "r95-vBLeBBTadKePKbTDlIzVU2kAHpoPtx"
    ),
    "fractional": (
        "MEUCIB_S_Tj0e44ndMyn51rm8FdS-ZrhOOH6b4YF7sYuJpcyAiEAqoLb6RHPWE"
        "QWU504stV_O7czTLvfa_i14iLEMXmbuKg"
    ),
}
_METADATA_FIXTURE: dict[str, str | int | None] = {
    "schema_version": 1,
    "host_ref": "host:adhelm-macos-approval-v1",
    "signing_key_ref": "approval-key:fixture-v1",
    "mach_service_name": "com.aptsonic.adhelm.approvalhost",
    "client_bundle_identifier": "com.aptsonic.adhelm.client",
    "client_team_identifier": "ABCDE12345",
    "host_bundle_identifier": "com.aptsonic.adhelm.host",
    "host_team_identifier": "ABCDE12345",
    "signing_key_application_tag_hex": "00616263ff",
    "signing_key_access_group": "ABCDE12345.com.aptsonic.adhelm.host",
    "key_storage": "KEYCHAIN_P256",
    "provenance_service": "com.aptsonic.adhelm.provenance",
    "provenance_account": "approval-host-v1",
    "maximum_challenge_bytes": 65_536,
    "maximum_challenge_lifetime_seconds": 300,
    "maximum_issued_at_skew_seconds": 60,
    "maximum_subplans": 6,
    "maximum_policy_items": 64,
}
_M42_TOP_LEVEL_FIELDS = {
    "schema_version",
    "fixture_kind",
    "scope",
    "configuration",
    "challenge",
    "challenge_digest",
    "review_facts",
    "review_digest",
    "unsigned_receipt",
    "unsigned_receipt_digest",
    "domain_separated_payload_sha256",
}


def _fail() -> NoReturn:
    raise ValueError("Meta campaign PAUSE approval-bridge vector source is invalid.")


def _exact_mapping(value: object, *, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail()
    return cast(dict[str, object], value)


def _exact_string(value: object) -> str:
    if type(value) is not str or not value:
        _fail()
    return value


def _exact_integer(value: object, expected: int) -> int:
    if type(value) is not int or isinstance(value, bool) or value != expected:
        _fail()
    return value


def _read_canonical_object(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        _fail()
    if not raw or len(raw) > _MAXIMUM_SOURCE_BYTES:
        _fail()
    try:
        data = parse_json(raw)
    except Exception:
        _fail()
    if type(data) is not dict or canonical_bytes(data) != raw:
        _fail()
    return cast(dict[str, object], data)


def _configuration(metadata: dict[str, str | int | None]) -> ApprovalHostConfiguration:
    try:
        return ApprovalHostConfiguration(
            host_ref=_exact_string(metadata["host_ref"]),
            signing_key_ref=_exact_string(metadata["signing_key_ref"]),
            maximum_challenge_bytes=_exact_integer(metadata["maximum_challenge_bytes"], 65_536),
            maximum_challenge_lifetime_seconds=_exact_integer(
                metadata["maximum_challenge_lifetime_seconds"], 300
            ),
            maximum_issued_at_skew_seconds=_exact_integer(
                metadata["maximum_issued_at_skew_seconds"], 60
            ),
            maximum_subplans=_exact_integer(metadata["maximum_subplans"], 6),
            maximum_policy_items=_exact_integer(metadata["maximum_policy_items"], 64),
        )
    except Exception:
        _fail()


def _time(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        _fail()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail()
    return parsed.astimezone(UTC)


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _signed_receipt(
    *,
    challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    unsigned_receipt: dict[str, object],
    signature: str,
) -> tuple[bytes, bytes]:
    receipt_id = _exact_string(unsigned_receipt["receipt_id"])
    payload = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id=receipt_id,
        issued_at=_time(unsigned_receipt["issued_at"]),
    )
    if payload.canonical_unsigned_receipt != canonical_bytes(unsigned_receipt):
        _fail()
    try:
        receipt = parse_json(payload.canonical_unsigned_receipt)
    except Exception:
        _fail()
    if type(receipt) is not dict:
        _fail()
    receipt["signature"] = signature
    canonical_receipt = canonical_bytes(receipt)
    try:
        key = bytes.fromhex(_PUBLIC_KEY_X963_HEX)
        verified = verify_approval_receipt(
            canonical_receipt,
            expected_challenge=challenge,
            configuration=configuration,
            trusted_public_key_x963=key,
            now=challenge.issued_at,
        )
    except Exception:
        _fail()
    if (
        verified.canonical_bytes != canonical_receipt
        or verified.kind is not ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
        or verified.receipt_id != receipt_id
    ):
        _fail()
    return canonical_receipt, key


def _m42_source(
    *,
    source_path: Path,
    expected_fixture_kind: str,
    signature_key: str,
) -> tuple[str, bytes, bytes, bytes, ApprovalHostConfiguration, ApprovalChallenge]:
    data = _exact_mapping(_read_canonical_object(source_path), fields=_M42_TOP_LEVEL_FIELDS)
    if (
        _exact_integer(data["schema_version"], 1) != 1
        or _exact_string(data["fixture_kind"]) != expected_fixture_kind
        or _exact_string(data["scope"]) != _M42_SCOPE
    ):
        _fail()
    configuration_data = data["configuration"]
    challenge_data = data["challenge"]
    review_data = data["review_facts"]
    unsigned_receipt = data["unsigned_receipt"]
    if (
        type(configuration_data) is not dict
        or type(challenge_data) is not dict
        or type(review_data) is not dict
        or type(unsigned_receipt) is not dict
    ):
        _fail()
    try:
        configuration = ApprovalHostConfiguration(**configuration_data)
        metadata_configuration = _configuration(_METADATA_FIXTURE)
    except Exception:
        _fail()
    if configuration != metadata_configuration or canonical_bytes(
        configuration_data
    ) != canonical_bytes(metadata_configuration.to_data()):
        _fail()
    canonical_challenge = canonical_bytes(challenge_data)
    try:
        challenge = parse_approval_challenge_wire(canonical_challenge, configuration=configuration)
        review = approval_review_facts(challenge, configuration=configuration)
    except Exception:
        _fail()
    if (
        challenge.kind is not ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
        or challenge.canonical_bytes != canonical_challenge
        or _exact_string(data["challenge_digest"]) != challenge.digest
        or review.canonical_bytes != canonical_bytes(review_data)
        or _exact_string(data["review_digest"]) != review.digest
    ):
        _fail()
    if (
        _exact_string(data["unsigned_receipt_digest"])
        != hashlib.sha256(canonical_bytes(unsigned_receipt)).hexdigest()
    ):
        _fail()
    signature = _SIGNED_RECEIPT_SIGNATURES.get(signature_key)
    if signature is None:
        _fail()
    canonical_receipt, key = _signed_receipt(
        challenge=challenge,
        configuration=configuration,
        unsigned_receipt=unsigned_receipt,
        signature=signature,
    )
    payload = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id=_exact_string(unsigned_receipt["receipt_id"]),
        issued_at=_time(unsigned_receipt["issued_at"]),
    )
    if (
        _exact_string(data["domain_separated_payload_sha256"])
        != hashlib.sha256(payload.domain_separated_bytes).hexdigest()
    ):
        _fail()
    return (
        f"Fixtures/MetaPause/{source_path.name}",
        canonical_challenge,
        canonical_receipt,
        key,
        configuration,
        challenge,
    )


def _source_context(case_number: int) -> dict[str, object]:
    digits = str(case_number)
    return {
        "schema_version": 2,
        "scope": "ADHELM_NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V2",
        "workspace_profile_ref": f"workspace.meta-bridge.fixture.{digits}",
        "source_stream": "changes",
        "source_plan_record_digest": digits * 64,
        "checkpoint_digest": str(case_number + 2) * 64,
    }


def _source_case(
    *,
    case_number: int,
    case_id: str,
    source_fixture: str,
    canonical_challenge: bytes,
    canonical_receipt: bytes,
    public_key: bytes,
    configuration: ApprovalHostConfiguration,
    challenge: ApprovalChallenge,
    exchange_started_at: str,
    current_now: str,
) -> dict[str, object]:
    context = _source_context(case_number)
    context_digest = derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(
        workspace_profile_ref=_exact_string(context["workspace_profile_ref"]),
        source_plan_record_digest=_exact_string(context["source_plan_record_digest"]),
        checkpoint_digest=_exact_string(context["checkpoint_digest"]),
    )
    if context_digest != hashlib.sha256(canonical_bytes(context)).hexdigest():
        _fail()
    metadata_digest = hashlib.sha256(canonical_bytes(_METADATA_FIXTURE)).hexdigest()
    request_id = bytes(range(case_number, case_number + 32))
    request = build_meta_campaign_pause_approval_bridge_request_v2(
        challenge,
        configuration=configuration,
        request_id=request_id,
        source_context_digest=context_digest,
        expected_metadata_digest=metadata_digest,
    )
    try:
        request_data = parse_json(request.canonical_bytes)
        receipt_data = parse_json(canonical_receipt)
    except Exception:
        _fail()
    if type(request_data) is not dict or type(receipt_data) is not dict:
        _fail()
    candidate = {
        "schema_version": 2,
        "message_type": "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_CANDIDATE_V2",
        "native_observation_status": "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED",
        "request_id_digest": hashlib.sha256(request_id).hexdigest(),
        "request_digest": request.digest,
        "source_context_digest": context_digest,
        "metadata_digest": metadata_digest,
        "protocol_configuration_digest": configuration.configuration_digest,
        "canonical_challenge_b64url": _encode_base64url(canonical_challenge),
        "challenge_digest": challenge.digest,
        "challenge_nonce_digest": hashlib.sha256(challenge.nonce).hexdigest(),
        "receipt_kind": challenge.kind.value,
        "expires_at": challenge.expires_at_text,
        "canonical_receipt_b64url": _encode_base64url(canonical_receipt),
        "receipt_digest": hashlib.sha256(canonical_receipt).hexdigest(),
        "receipt_id": _exact_string(receipt_data["receipt_id"]),
        "active_public_key_x963_b64url": _encode_base64url(public_key),
        "active_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
        "native_observed_at": current_now,
    }
    candidate_bytes = canonical_bytes(candidate)
    try:
        validate_meta_campaign_pause_approval_bridge_candidate_v2(
            candidate_bytes,
            request=request,
            configuration=configuration,
            expected_metadata_digest=metadata_digest,
            exchange_started_at=_time(exchange_started_at),
            current_now=_time(current_now),
        )
    except Exception:
        _fail()
    return {
        "case_id": case_id,
        "source_fixture": source_fixture,
        "metadata_fixture": _METADATA_FIXTURE,
        "source_context": context,
        "source_context_canonical_json": canonical_bytes(context).decode("utf-8"),
        "source_context_digest": context_digest,
        "request": request_data,
        "request_canonical_json": request.canonical_bytes.decode("utf-8"),
        "request_digest": request.digest,
        "candidate": candidate,
        "candidate_canonical_json": candidate_bytes.decode("utf-8"),
        "candidate_digest": hashlib.sha256(candidate_bytes).hexdigest(),
        "exchange_started_at": exchange_started_at,
        "current_now": current_now,
    }


def _invalid_input(case_id: str, target: str, raw: bytes) -> dict[str, str]:
    if target not in {"REQUEST", "CANDIDATE"}:
        _fail()
    return {"case_id": case_id, "target": target, "raw_json": raw.decode("utf-8")}


def _invalid_inputs(case: dict[str, object]) -> list[dict[str, str]]:
    request = cast(dict[str, object], case["request"])
    candidate = cast(dict[str, object], case["candidate"])
    request_v1 = dict(request)
    request_v1["schema_version"] = 1
    request_v1["message_type"] = "NATIVE_APPROVAL_BRIDGE_REQUEST_V1"
    request_float = dict(request)
    request_float["schema_version"] = 2.0
    candidate_v1 = dict(candidate)
    candidate_v1["schema_version"] = 1
    candidate_v1["message_type"] = "NATIVE_APPROVAL_BRIDGE_CANDIDATE_V1"
    candidate_kind = dict(candidate)
    candidate_kind["receipt_kind"] = "OPENAI_PAUSE_PLAN"
    candidate_time = dict(candidate)
    candidate_time["native_observed_at"] = "2030-01-01T00:00:30Z"
    candidate_extra = dict(candidate)
    candidate_extra["unexpected"] = "rejected"
    return [
        _invalid_input("request_v1_cross_version", "REQUEST", canonical_bytes(request_v1)),
        _invalid_input(
            "request_schema_version_float_alias",
            "REQUEST",
            json.dumps(request_float, separators=(",", ":")).encode("utf-8"),
        ),
        _invalid_input("candidate_v1_cross_version", "CANDIDATE", canonical_bytes(candidate_v1)),
        _invalid_input("candidate_non_meta_kind", "CANDIDATE", canonical_bytes(candidate_kind)),
        _invalid_input(
            "candidate_native_observed_at_whole_seconds",
            "CANDIDATE",
            canonical_bytes(candidate_time),
        ),
        _invalid_input("candidate_extra_field", "CANDIDATE", canonical_bytes(candidate_extra)),
    ]


def build_document() -> dict[str, object]:
    whole = _m42_source(
        source_path=WHOLE_SOURCE_PATH,
        expected_fixture_kind="META_CAMPAIGN_PAUSE_APPROVAL_V1",
        signature_key="whole",
    )
    fractional = _m42_source(
        source_path=FRACTIONAL_SOURCE_PATH,
        expected_fixture_kind="META_CAMPAIGN_PAUSE_APPROVAL_FRACTIONAL_V1",
        signature_key="fractional",
    )
    whole_case = _source_case(
        case_number=1,
        case_id="meta_campaign_pause_whole_seconds",
        source_fixture=whole[0],
        canonical_challenge=whole[1],
        canonical_receipt=whole[2],
        public_key=whole[3],
        configuration=whole[4],
        challenge=whole[5],
        exchange_started_at="2030-01-01T00:00:29.000000Z",
        current_now="2030-01-01T00:00:30.000000Z",
    )
    fractional_case = _source_case(
        case_number=2,
        case_id="meta_campaign_pause_fractional",
        source_fixture=fractional[0],
        canonical_challenge=fractional[1],
        canonical_receipt=fractional[2],
        public_key=fractional[3],
        configuration=fractional[4],
        challenge=fractional[5],
        exchange_started_at="2030-01-01T00:00:29.123456Z",
        current_now="2030-01-01T00:00:30.123456Z",
    )
    return {
        "schema_version": 1,
        "fixture_kind": "META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2",
        "scope": _OUTPUT_SCOPE,
        "source_cases": [whole_case, fractional_case],
        "invalid_inputs": _invalid_inputs(whole_case),
    }


def main() -> None:
    OUTPUT_PATH.write_bytes(canonical_bytes(build_document()))


if __name__ == "__main__":
    main()

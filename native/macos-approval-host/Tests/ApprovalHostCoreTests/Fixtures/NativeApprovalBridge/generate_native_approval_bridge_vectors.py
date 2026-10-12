"""Generate synthetic, non-authorizing macOS approval-bridge wire vectors.

The fixed public Swift receipt fixtures are verified through the Python protocol
before this generator emits canonical request/candidate bytes.  The generated
vectors exercise syntax and cross-language binding only.  They do not install a
host, resolve trust, consume a nonce, record a grant, or execute a provider.

Run from the repository root:

  UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run python \
    native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridge/generate_native_approval_bridge_vectors.py
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

from adhelm._native_approval_bridge_protocol import (
    build_native_approval_bridge_request_v1,
    derive_native_approval_bridge_source_context_digest_v1,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalHostConfiguration,
    parse_approval_challenge_wire,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json

FIXTURE_DIRECTORY = Path(__file__).parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[6]
OUTPUT_PATH = FIXTURE_DIRECTORY / "native-approval-bridge-protocol-v1.json"
GA4_SOURCE_PATH = REPOSITORY_ROOT / "tests/fixtures/swift-ga4-create-approval-receipt-v1.json"
PAUSE_SOURCE_PATH = REPOSITORY_ROOT / "tests/fixtures/swift-approval-receipts-v1.json"
_MAXIMUM_SOURCE_BYTES = 1_048_576
_SOURCE_SCOPE = "SYNTHETIC_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION"
_OUTPUT_SCOPE = (
    "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INTEROPERABILITY_ONLY_NOT_ACCEPTANCE_OR_AUTHORIZATION"
)
_SOURCE_NOW = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)
_EXCHANGE_STARTED_AT = "2030-01-01T00:00:29.000000Z"
_CURRENT_NOW = "2030-01-01T00:00:31.000000Z"
_SOURCE_CONTEXT_SCOPE = "ADHELM_NATIVE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V1"
_METADATA_FIXTURE: dict[str, str | int | None] = {
    "schema_version": 1,
    "host_ref": "host:adhelm-macos-approval-v1",
    "signing_key_ref": "key:approval-test-v1",
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


def _fail() -> NoReturn:
    raise ValueError("Native approval-bridge vector source is invalid.")


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


def _read_canonical_json(
    path: Path, *, allow_one_legacy_trailing_newline: bool = False
) -> dict[str, object]:
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
    canonical = canonical_bytes(data)
    if type(data) is not dict or (
        raw != canonical and not (allow_one_legacy_trailing_newline and raw == canonical + b"\n")
    ):
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


def _verified_source(
    *,
    challenge_data: object,
    receipt_data: object,
    public_key_x963_hex: object,
    receipt_digest: object,
    review_digest: object,
) -> tuple[bytes, bytes, bytes, ApprovalHostConfiguration, ApprovalChallenge]:
    challenge = _exact_mapping(
        challenge_data,
        fields={
            "schema_version",
            "kind",
            "nonce",
            "issued_at",
            "expires_at",
            "grantor_ref",
            "action",
        },
    )
    receipt = _exact_mapping(
        receipt_data,
        fields={
            "schema_version",
            "receipt_id",
            "host_ref",
            "key_ref",
            "grantor_ref",
            "issued_at",
            "expires_at",
            "receipt_kind",
            "challenge_digest",
            "challenge_nonce",
            "review_digest",
            "signature_algorithm",
            "signature",
            "action_digest",
            "plan_digest",
            "subplan_digests",
            "authentication_mode",
        },
    )
    _exact_integer(challenge["schema_version"], 1)
    _exact_integer(receipt["schema_version"], 1)
    if (
        _exact_string(receipt["host_ref"]) != _METADATA_FIXTURE["host_ref"]
        or _exact_string(receipt["key_ref"]) != _METADATA_FIXTURE["signing_key_ref"]
        or _exact_string(receipt["expires_at"]) != _exact_string(challenge["expires_at"])
        or _exact_string(receipt["receipt_kind"]) != _exact_string(challenge["kind"])
    ):
        _fail()
    try:
        key = bytes.fromhex(_exact_string(public_key_x963_hex))
        configuration = _configuration(_METADATA_FIXTURE)
        canonical_challenge = canonical_bytes(challenge)
        decoded_challenge = parse_approval_challenge_wire(
            canonical_challenge, configuration=configuration
        )
        canonical_receipt = canonical_bytes(receipt)
        verified_receipt = verify_approval_receipt(
            canonical_receipt,
            expected_challenge=decoded_challenge,
            configuration=configuration,
            trusted_public_key_x963=key,
            now=_SOURCE_NOW,
        )
    except Exception:
        _fail()
    if (
        len(key) != 65
        or _exact_string(receipt_digest) != verified_receipt.digest
        or _exact_string(review_digest) != verified_receipt.review_digest
        or verified_receipt.configuration_digest != configuration.configuration_digest
        or verified_receipt.challenge_digest != decoded_challenge.digest
        or verified_receipt.expires_at_text != decoded_challenge.expires_at_text
    ):
        _fail()
    return canonical_challenge, canonical_receipt, key, configuration, decoded_challenge


def _ga4_source() -> tuple[
    str, str, bytes, bytes, bytes, ApprovalHostConfiguration, ApprovalChallenge
]:
    data = _exact_mapping(
        _read_canonical_json(GA4_SOURCE_PATH),
        fields={
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
        },
    )
    if (
        _exact_integer(data["schema_version"], 1) != 1
        or _exact_string(data["fixture_name"]) != "ga4-key-event-create-challenge-v1"
        or _exact_string(data["producer"]) != "SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"
        or _exact_string(data["scope"]) != _SOURCE_SCOPE
        or _exact_string(data["verified_at"]) != "2030-01-01T00:00:30Z"
    ):
        _fail()
    values = _verified_source(
        challenge_data=data["challenge"],
        receipt_data=data["receipt"],
        public_key_x963_hex=data["public_key_x963_hex"],
        receipt_digest=data["receipt_digest"],
        review_digest=data["review_digest"],
    )
    return (
        "swift_ga4_key_event_create",
        "tests/fixtures/swift-ga4-create-approval-receipt-v1.json",
        *values,
    )


def _pause_source() -> tuple[
    str, str, bytes, bytes, bytes, ApprovalHostConfiguration, ApprovalChallenge
]:
    data = _exact_mapping(
        _read_canonical_json(PAUSE_SOURCE_PATH, allow_one_legacy_trailing_newline=True),
        fields={
            "producer",
            "public_key_x963_hex",
            "schema_version",
            "scope",
            "vectors",
            "verified_at",
        },
    )
    vectors = data["vectors"]
    if (
        _exact_integer(data["schema_version"], 1) != 1
        or _exact_string(data["producer"]) != "SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"
        or _exact_string(data["scope"]) != _SOURCE_SCOPE
        or _exact_string(data["verified_at"]) != "2030-01-01T00:00:30Z"
        or type(vectors) is not list
    ):
        _fail()
    selected: dict[str, object] | None = None
    for item in cast(list[object], vectors):
        vector = _exact_mapping(
            item,
            fields={
                "challenge",
                "fixture_name",
                "receipt",
                "receipt_digest",
                "review_digest",
                "review_facts",
            },
        )
        if _exact_string(vector["fixture_name"]) == "openai-pause-challenge-v1":
            if selected is not None:
                _fail()
            selected = vector
    if selected is None:
        _fail()
    values = _verified_source(
        challenge_data=selected["challenge"],
        receipt_data=selected["receipt"],
        public_key_x963_hex=data["public_key_x963_hex"],
        receipt_digest=selected["receipt_digest"],
        review_digest=selected["review_digest"],
    )
    return (
        "swift_openai_pause",
        "tests/fixtures/swift-approval-receipts-v1.json#openai-pause-challenge-v1",
        *values,
    )


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _source_context(case_number: int) -> dict[str, object]:
    digits = str(case_number)
    return {
        "schema_version": 1,
        "scope": _SOURCE_CONTEXT_SCOPE,
        "workspace_profile_ref": f"workspace.native-bridge.fixture.{digits}",
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
    decoded_challenge: ApprovalChallenge,
) -> dict[str, object]:
    context = _source_context(case_number)
    context_digest = derive_native_approval_bridge_source_context_digest_v1(
        workspace_profile_ref=_exact_string(context["workspace_profile_ref"]),
        source_plan_record_digest=_exact_string(context["source_plan_record_digest"]),
        checkpoint_digest=_exact_string(context["checkpoint_digest"]),
    )
    if context_digest != hashlib.sha256(canonical_bytes(context)).hexdigest():
        _fail()
    metadata_digest = hashlib.sha256(canonical_bytes(_METADATA_FIXTURE)).hexdigest()
    request_id = bytes(range(case_number, case_number + 32))
    request = build_native_approval_bridge_request_v1(
        decoded_challenge,
        configuration=configuration,
        request_id=request_id,
        source_context_digest=context_digest,
        expected_metadata_digest=metadata_digest,
    )
    request_data = parse_json(request.canonical_bytes)
    if type(request_data) is not dict:
        _fail()
    try:
        receipt_data = parse_json(canonical_receipt)
    except Exception:
        _fail()
    if type(receipt_data) is not dict:
        _fail()
    candidate = {
        "schema_version": 1,
        "message_type": "NATIVE_APPROVAL_BRIDGE_CANDIDATE_V1",
        "native_observation_status": "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED",
        "request_id_digest": hashlib.sha256(request_id).hexdigest(),
        "request_digest": request.digest,
        "source_context_digest": context_digest,
        "metadata_digest": metadata_digest,
        "protocol_configuration_digest": configuration.configuration_digest,
        "canonical_challenge_b64url": _encode_base64url(canonical_challenge),
        "challenge_digest": decoded_challenge.digest,
        "challenge_nonce_digest": hashlib.sha256(decoded_challenge.nonce).hexdigest(),
        "receipt_kind": decoded_challenge.kind.value,
        "expires_at": decoded_challenge.expires_at_text,
        "canonical_receipt_b64url": _encode_base64url(canonical_receipt),
        "receipt_digest": hashlib.sha256(canonical_receipt).hexdigest(),
        "receipt_id": _exact_string(receipt_data["receipt_id"]),
        "active_public_key_x963_b64url": _encode_base64url(public_key),
        "active_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
        "native_observed_at": "2030-01-01T00:00:30.000000Z",
    }
    candidate_bytes = canonical_bytes(candidate)
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
        "exchange_started_at": _EXCHANGE_STARTED_AT,
        "current_now": _CURRENT_NOW,
    }


def _invalid_input(case_id: str, target: str, raw: bytes) -> dict[str, str]:
    if target not in {"REQUEST", "CANDIDATE"}:
        _fail()
    return {"case_id": case_id, "target": target, "raw_json": raw.decode("utf-8")}


def _invalid_inputs(case: dict[str, object]) -> list[dict[str, str]]:
    request = cast(dict[str, object], case["request"])
    candidate = cast(dict[str, object], case["candidate"])
    request_float = dict(request)
    request_float["schema_version"] = 1.0
    request_metadata = dict(request)
    request_metadata["metadata_digest"] = "0" * 64
    candidate_status = dict(candidate)
    candidate_status["native_observation_status"] = "PENDING"
    candidate_request_digest = dict(candidate)
    candidate_request_digest["request_digest"] = "0" * 64
    candidate_whole_seconds = dict(candidate)
    candidate_whole_seconds["native_observed_at"] = "2030-01-01T00:00:30Z"
    candidate_extra = dict(candidate)
    candidate_extra["unexpected"] = "rejected"
    receipt = parse_json(
        base64.urlsafe_b64decode(
            _exact_string(candidate["canonical_receipt_b64url"])
            + "=" * ((4 - len(_exact_string(candidate["canonical_receipt_b64url"])) % 4) % 4)
        )
    )
    if type(receipt) is not dict:
        _fail()
    changed_receipt = dict(receipt)
    changed_receipt["signature"] = "AAAAAAAAAAA"
    changed_receipt_bytes = canonical_bytes(changed_receipt)
    candidate_signature = dict(candidate)
    candidate_signature["canonical_receipt_b64url"] = _encode_base64url(changed_receipt_bytes)
    candidate_signature["receipt_digest"] = hashlib.sha256(changed_receipt_bytes).hexdigest()
    return [
        _invalid_input(
            "request_schema_version_float_alias",
            "REQUEST",
            json.dumps(request_float, separators=(",", ":")).encode("utf-8"),
        ),
        _invalid_input(
            "request_metadata_digest_mismatch", "REQUEST", canonical_bytes(request_metadata)
        ),
        _invalid_input(
            "candidate_non_success_status", "CANDIDATE", canonical_bytes(candidate_status)
        ),
        _invalid_input(
            "candidate_request_digest_mismatch",
            "CANDIDATE",
            canonical_bytes(candidate_request_digest),
        ),
        _invalid_input(
            "candidate_native_observed_at_whole_seconds",
            "CANDIDATE",
            canonical_bytes(candidate_whole_seconds),
        ),
        _invalid_input("candidate_extra_field", "CANDIDATE", canonical_bytes(candidate_extra)),
        _invalid_input(
            "candidate_receipt_signature_mismatch",
            "CANDIDATE",
            canonical_bytes(candidate_signature),
        ),
    ]


def build_document() -> dict[str, object]:
    sources = (_ga4_source(), _pause_source())
    cases: list[dict[str, object]] = []
    for number, source in enumerate(sources, start=1):
        (
            case_id,
            source_fixture,
            canonical_challenge,
            canonical_receipt,
            public_key,
            configuration,
            decoded_challenge,
        ) = source
        cases.append(
            _source_case(
                case_number=number,
                case_id=case_id,
                source_fixture=source_fixture,
                canonical_challenge=canonical_challenge,
                canonical_receipt=canonical_receipt,
                public_key=public_key,
                configuration=configuration,
                decoded_challenge=decoded_challenge,
            )
        )
    return {
        "schema_version": 1,
        "scope": _OUTPUT_SCOPE,
        "source_cases": cases,
        "invalid_inputs": _invalid_inputs(cases[0]),
    }


def main() -> None:
    OUTPUT_PATH.write_bytes(canonical_bytes(build_document()))


if __name__ == "__main__":
    main()

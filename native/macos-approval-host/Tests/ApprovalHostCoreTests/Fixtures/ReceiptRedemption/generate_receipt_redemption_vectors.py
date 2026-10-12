"""Generate closed receipt-redemption state vectors from public Swift receipts.

This offline generator has two fixed inputs: the checked-in public-only Swift
GA4 CREATE and OpenAI PAUSE receipt fixtures.  It verifies each receipt with
the production Python parser, then emits only digest-bound protected-state
records.  The output is not a receipt acceptance, trust installation, permit,
or execution result.

Run from the repository root:

  UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run python \
    native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/ReceiptRedemption/generate_receipt_redemption_vectors.py
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    decode_approval_challenge,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json

FIXTURE_DIR = Path(__file__).parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[6]
OUTPUT_PATH = FIXTURE_DIR / "receipt-redemption-state-v1.json"
GA4_SOURCE_PATH = REPOSITORY_ROOT / "tests/fixtures/swift-ga4-create-approval-receipt-v1.json"
PAUSE_SOURCE_PATH = REPOSITORY_ROOT / "tests/fixtures/swift-approval-receipts-v1.json"
_MAXIMUM_SOURCE_BYTES = 1_048_576
_SOURCE_SCOPE = "SYNTHETIC_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION"
_OUTPUT_SCOPE = (
    "RECEIPT_REDEMPTION_STATE_INTEROPERABILITY_ONLY_NOT_RECEIPT_ACCEPTANCE_OR_AUTHORIZATION"
)
_SOURCE_NOW_TEXT = "2030-01-01T00:00:30Z"
_SOURCE_NOW = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)
_BINDING_FIELDS = {
    "challenge_digest",
    "challenge_nonce_digest",
    "configuration_digest",
    "expires_at",
    "receipt_digest",
    "receipt_id",
    "receipt_kind",
}


def _fail() -> None:
    raise ValueError("Receipt-redemption vector source is invalid.")


def _exact_mapping(value: object, *, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail()
    return cast(dict[str, object], value)


def _exact_string(value: object) -> str:
    if type(value) is not str or not value:
        _fail()
    return cast(str, value)


def _exact_integer(value: object, expected: int) -> int:
    if type(value) is not int or isinstance(value, bool) or value != expected:
        _fail()
    return cast(int, value)


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


def _verified_source(
    *,
    challenge_data: object,
    receipt_data: object,
    public_key_x963_hex: object,
    receipt_digest: object,
    review_digest: object,
) -> dict[str, object]:
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
    host_ref = _exact_string(receipt["host_ref"])
    key_ref = _exact_string(receipt["key_ref"])
    if _exact_string(receipt["expires_at"]) != _exact_string(challenge["expires_at"]):
        _fail()
    if _exact_string(receipt["receipt_kind"]) != _exact_string(challenge["kind"]):
        _fail()
    try:
        public_key = bytes.fromhex(_exact_string(public_key_x963_hex))
    except ValueError:
        _fail()
    if len(public_key) != 65:
        _fail()
    configuration = ApprovalHostConfiguration(host_ref=host_ref, signing_key_ref=key_ref)
    try:
        decoded_challenge = decode_approval_challenge(
            canonical_bytes(challenge), configuration=configuration, now=_SOURCE_NOW
        )
        verified_receipt = verify_approval_receipt(
            canonical_bytes(receipt),
            expected_challenge=decoded_challenge,
            configuration=configuration,
            trusted_public_key_x963=public_key,
            now=_SOURCE_NOW,
        )
    except Exception:
        _fail()
    if (
        _exact_string(receipt_digest) != verified_receipt.digest
        or _exact_string(review_digest) != verified_receipt.review_digest
        or verified_receipt.configuration_digest != configuration.configuration_digest
        or verified_receipt.challenge_digest != decoded_challenge.digest
        or verified_receipt.expires_at_text != decoded_challenge.expires_at_text
    ):
        _fail()
    return {
        "challenge_digest": decoded_challenge.digest,
        "challenge_nonce_digest": hashlib.sha256(decoded_challenge.nonce).hexdigest(),
        "configuration_digest": configuration.configuration_digest,
        "expires_at": decoded_challenge.expires_at_text,
        "receipt_digest": verified_receipt.digest,
        "receipt_id": verified_receipt.receipt_id,
        "receipt_kind": verified_receipt.kind.value,
    }


def _ga4_source_case() -> tuple[str, str, dict[str, object]]:
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
    _exact_integer(data["schema_version"], 1)
    if (
        _exact_string(data["fixture_name"]) != "ga4-key-event-create-challenge-v1"
        or _exact_string(data["producer"]) != "SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"
        or _exact_string(data["scope"]) != _SOURCE_SCOPE
        or _exact_string(data["verified_at"]) != _SOURCE_NOW_TEXT
    ):
        _fail()
    return (
        "swift_ga4_key_event_create",
        "tests/fixtures/swift-ga4-create-approval-receipt-v1.json",
        _verified_source(
            challenge_data=data["challenge"],
            receipt_data=data["receipt"],
            public_key_x963_hex=data["public_key_x963_hex"],
            receipt_digest=data["receipt_digest"],
            review_digest=data["review_digest"],
        ),
    )


def _pause_source_case() -> tuple[str, str, dict[str, object]]:
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
    _exact_integer(data["schema_version"], 1)
    vectors = data["vectors"]
    if (
        _exact_string(data["producer"]) != "SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"
        or _exact_string(data["scope"]) != _SOURCE_SCOPE
        or _exact_string(data["verified_at"]) != _SOURCE_NOW_TEXT
        or type(vectors) is not list
    ):
        _fail()
    matching: list[dict[str, object]] = []
    for value in cast(list[object], vectors):
        vector = _exact_mapping(
            value,
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
            matching.append(vector)
    if len(matching) != 1:
        _fail()
    selected = matching[0]
    return (
        "swift_openai_pause",
        "tests/fixtures/swift-approval-receipts-v1.json#openai-pause-challenge-v1",
        _verified_source(
            challenge_data=selected["challenge"],
            receipt_data=selected["receipt"],
            public_key_x963_hex=data["public_key_x963_hex"],
            receipt_digest=selected["receipt_digest"],
            review_digest=selected["review_digest"],
        ),
    )


def _state_case(case_id: str, source_fixture: str, binding: dict[str, object]) -> dict[str, object]:
    if (
        type(case_id) is not str
        or type(source_fixture) is not str
        or set(binding) != _BINDING_FIELDS
    ):
        _fail()
    pending = {
        "schema_version": 1,
        "state": "PENDING",
        "binding": binding,
        "reserved_at": "2030-01-01T00:00:30.000000Z",
    }
    verified = {
        "schema_version": 1,
        "state": "VERIFIED",
        "binding": binding,
        "reserved_at": "2030-01-01T00:00:30.000000Z",
        "verified_at": "2030-01-01T00:00:31.000000Z",
    }
    initial_global = {
        "schema_version": 1,
        "state": "READY",
        "generation": 1,
        "observed_at": "2030-01-01T00:00:29.000000Z",
    }
    intent_pending_global = {
        "schema_version": 1,
        "state": "INTENT_PENDING",
        "generation": 2,
        "observed_at": "2030-01-01T00:00:30.000000Z",
        "binding": binding,
    }
    final_global = {
        "schema_version": 1,
        "state": "READY",
        "generation": 3,
        "observed_at": "2030-01-01T00:00:31.000000Z",
    }
    return {
        "case_id": case_id,
        "source_fixture": source_fixture,
        "binding": binding,
        "binding_canonical_json": canonical_bytes(binding).decode("utf-8"),
        "initial_global_state": initial_global,
        "initial_global_state_canonical_json": canonical_bytes(initial_global).decode("utf-8"),
        "intent_pending_global_state": intent_pending_global,
        "intent_pending_global_state_canonical_json": canonical_bytes(intent_pending_global).decode(
            "utf-8"
        ),
        "pending_state": pending,
        "pending_state_canonical_json": canonical_bytes(pending).decode("utf-8"),
        "verified_state": verified,
        "verified_state_canonical_json": canonical_bytes(verified).decode("utf-8"),
        "final_global_state": final_global,
        "final_global_state_canonical_json": canonical_bytes(final_global).decode("utf-8"),
    }


def _invalid_inputs(seed: dict[str, object]) -> list[dict[str, object]]:
    pending = {
        "schema_version": 1,
        "state": "PENDING",
        "binding": seed,
        "reserved_at": "2030-01-01T00:00:30.000000Z",
    }
    ready_global = {
        "schema_version": 1,
        "state": "READY",
        "generation": 1,
        "observed_at": "2030-01-01T00:00:29.000000Z",
    }
    intent_pending_global = {
        "schema_version": 1,
        "state": "INTENT_PENDING",
        "generation": 2,
        "observed_at": "2030-01-01T00:00:30.000000Z",
        "binding": seed,
    }
    canonical_pending = canonical_bytes(pending).decode("utf-8")
    canonical_ready_global = canonical_bytes(ready_global).decode("utf-8")
    kind = _exact_string(seed["receipt_kind"])
    expiry = _exact_string(seed["expires_at"])
    uppercase_digest = dict(seed)
    uppercase_digest["receipt_digest"] = _exact_string(seed["receipt_digest"]).upper()
    return [
        {
            "case_id": "pending_schema_version_float_alias",
            "target": "NONCE_STATE",
            "raw_json": canonical_pending.replace('"schema_version":1', '"schema_version":1.0', 1),
        },
        {
            "case_id": "pending_schema_version_boolean",
            "target": "NONCE_STATE",
            "raw_json": canonical_pending.replace('"schema_version":1', '"schema_version":true', 1),
        },
        {
            "case_id": "pending_receipt_kind_boolean",
            "target": "NONCE_STATE",
            "raw_json": canonical_pending.replace(
                f'"receipt_kind":"{kind}"', '"receipt_kind":false', 1
            ),
        },
        {
            "case_id": "pending_expiry_number",
            "target": "NONCE_STATE",
            "raw_json": canonical_pending.replace(f'"expires_at":"{expiry}"', '"expires_at":1', 1),
        },
        {
            "case_id": "pending_uppercase_receipt_digest",
            "target": "NONCE_STATE",
            "raw_json": canonical_bytes({**pending, "binding": uppercase_digest}).decode("utf-8"),
        },
        {
            "case_id": "pending_trailing_newline",
            "target": "NONCE_STATE",
            "raw_json": canonical_pending + "\n",
        },
        {
            "case_id": "global_schema_version_float_alias",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_ready_global.replace(
                '"schema_version":1', '"schema_version":1.0', 1
            ),
        },
        {
            "case_id": "global_schema_version_boolean",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_ready_global.replace(
                '"schema_version":1', '"schema_version":true', 1
            ),
        },
        {
            "case_id": "global_generation_float_alias",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_ready_global.replace('"generation":1', '"generation":1.0', 1),
        },
        {
            "case_id": "global_generation_boolean",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_ready_global.replace('"generation":1', '"generation":true', 1),
        },
        {
            "case_id": "global_missing_observed_at",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_bytes(
                {"schema_version": 1, "state": "READY", "generation": 1}
            ).decode("utf-8"),
        },
        {
            "case_id": "global_observed_at_null",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_bytes({**ready_global, "observed_at": None}).decode("utf-8"),
        },
        {
            "case_id": "global_observed_at_whole_seconds",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_bytes(
                {**ready_global, "observed_at": "2030-01-01T00:00:29Z"}
            ).decode("utf-8"),
        },
        {
            "case_id": "global_null",
            "target": "GLOBAL_STATE",
            "raw_json": "null",
        },
        {
            "case_id": "global_intent_missing_binding",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_bytes(
                {
                    "schema_version": 1,
                    "state": "INTENT_PENDING",
                    "generation": 2,
                    "observed_at": "2030-01-01T00:00:30.000000Z",
                }
            ).decode("utf-8"),
        },
        {
            "case_id": "global_intent_null_binding",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_bytes({**intent_pending_global, "binding": None}).decode("utf-8"),
        },
        {
            "case_id": "global_intent_receipt_digest_number",
            "target": "GLOBAL_STATE",
            "raw_json": canonical_bytes(
                {
                    **intent_pending_global,
                    "binding": {**seed, "receipt_digest": 1},
                }
            ).decode("utf-8"),
        },
    ]


def _recovery_inputs(case: dict[str, object]) -> list[dict[str, object]]:
    binding = _exact_mapping(case["binding"], fields=_BINDING_FIELDS)
    pending = _exact_mapping(
        case["pending_state"],
        fields={"schema_version", "state", "binding", "reserved_at"},
    )
    ready = _exact_mapping(
        case["initial_global_state"],
        fields={"schema_version", "state", "generation", "observed_at"},
    )
    intent = _exact_mapping(
        case["intent_pending_global_state"],
        fields={"schema_version", "state", "generation", "observed_at", "binding"},
    )
    mismatched_binding = dict(binding)
    mismatched_binding["receipt_digest"] = "f" * 64
    mismatched_intent = {**intent, "binding": mismatched_binding}
    return [
        {
            "case_id": "missing_or_null_global_state",
            "target": "GLOBAL_STATE",
            "missing_global_state": True,
            "null_global_raw_json": "null",
        },
        {
            "case_id": "pending_with_ready_global_state",
            "target": "STATE_PAIR",
            "nonce_state": pending,
            "nonce_state_canonical_json": canonical_bytes(pending).decode("utf-8"),
            "global_state": ready,
            "global_state_canonical_json": canonical_bytes(ready).decode("utf-8"),
        },
        {
            "case_id": "pending_with_different_intent_binding",
            "target": "STATE_PAIR",
            "nonce_state": pending,
            "nonce_state_canonical_json": canonical_bytes(pending).decode("utf-8"),
            "global_state": mismatched_intent,
            "global_state_canonical_json": canonical_bytes(mismatched_intent).decode("utf-8"),
        },
    ]


def build_vector_document() -> dict[str, object]:
    """Build the complete closed vector document from the two fixed sources."""

    ga4 = _state_case(*_ga4_source_case())
    pause = _state_case(*_pause_source_case())
    pause_binding = _exact_mapping(pause["binding"], fields=_BINDING_FIELDS)
    return {
        "schema_version": 1,
        "scope": _OUTPUT_SCOPE,
        "source_cases": [ga4, pause],
        "invalid_inputs": _invalid_inputs(pause_binding),
        "recovery_inputs": _recovery_inputs(pause),
    }


def main() -> None:
    OUTPUT_PATH.write_bytes(canonical_bytes(build_vector_document()))


if __name__ == "__main__":
    main()

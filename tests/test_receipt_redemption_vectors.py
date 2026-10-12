"""Cross-language vectors for closed native receipt-redemption state records."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import pytest

from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    decode_approval_challenge,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DIRECTORY = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/ReceiptRedemption"
)
_GENERATOR_PATH = _FIXTURE_DIRECTORY / "generate_receipt_redemption_vectors.py"
_VECTOR_PATH = _FIXTURE_DIRECTORY / "receipt-redemption-state-v1.json"
_GA4_SOURCE_PATH = _ROOT / "tests/fixtures/swift-ga4-create-approval-receipt-v1.json"
_PAUSE_SOURCE_PATH = _ROOT / "tests/fixtures/swift-approval-receipts-v1.json"
_NOW = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID = re.compile(r"^approval-receipt:[A-Za-z0-9_-]{43}$")


class _GeneratorApi(Protocol):
    OUTPUT_PATH: Path

    def main(self) -> None: ...


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "receipt_redemption_vector_generator", _GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _document() -> dict[str, object]:
    raw = _VECTOR_PATH.read_bytes()
    data = parse_json(raw)
    assert type(data) is dict
    assert canonical_bytes(data) == raw
    return data


def _source_binding(
    *, challenge_data: object, receipt_data: object, public_key_hex: object
) -> dict[str, object]:
    assert type(challenge_data) is dict
    assert type(receipt_data) is dict
    assert type(public_key_hex) is str
    assert type(challenge_data["schema_version"]) is int
    assert not isinstance(challenge_data["schema_version"], bool)
    assert type(receipt_data["schema_version"]) is int
    assert not isinstance(receipt_data["schema_version"], bool)
    host_ref = receipt_data["host_ref"]
    key_ref = receipt_data["key_ref"]
    assert type(host_ref) is str
    assert type(key_ref) is str
    configuration = ApprovalHostConfiguration(host_ref=host_ref, signing_key_ref=key_ref)
    challenge = decode_approval_challenge(
        canonical_bytes(challenge_data), configuration=configuration, now=_NOW
    )
    receipt = verify_approval_receipt(
        canonical_bytes(receipt_data),
        expected_challenge=challenge,
        configuration=configuration,
        trusted_public_key_x963=bytes.fromhex(public_key_hex),
        now=_NOW,
    )
    assert receipt.configuration_digest == configuration.configuration_digest
    assert receipt.challenge_digest == challenge.digest
    return {
        "challenge_digest": challenge.digest,
        "challenge_nonce_digest": hashlib.sha256(challenge.nonce).hexdigest(),
        "configuration_digest": configuration.configuration_digest,
        "expires_at": challenge.expires_at_text,
        "receipt_digest": receipt.digest,
        "receipt_id": receipt.receipt_id,
        "receipt_kind": receipt.kind.value,
    }


def _source_cases() -> dict[str, dict[str, object]]:
    ga4 = parse_json(_GA4_SOURCE_PATH.read_bytes())
    pause = parse_json(_PAUSE_SOURCE_PATH.read_bytes())
    assert type(ga4) is dict
    assert type(pause) is dict
    assert ga4["verified_at"] == "2030-01-01T00:00:30Z"
    assert pause["verified_at"] == "2030-01-01T00:00:30Z"
    vectors = pause["vectors"]
    assert type(vectors) is list
    pause_vector = next(
        item
        for item in vectors
        if type(item) is dict and item.get("fixture_name") == "openai-pause-challenge-v1"
    )
    assert type(pause_vector) is dict
    return {
        "swift_ga4_key_event_create": _source_binding(
            challenge_data=ga4["challenge"],
            receipt_data=ga4["receipt"],
            public_key_hex=ga4["public_key_x963_hex"],
        ),
        "swift_openai_pause": _source_binding(
            challenge_data=pause_vector["challenge"],
            receipt_data=pause_vector["receipt"],
            public_key_hex=pause["public_key_x963_hex"],
        ),
    }


def test_receipt_redemption_vectors_regenerate_exactly_from_signed_swift_fixtures(
    tmp_path: Path,
) -> None:
    module = cast(_GeneratorApi, _generator())
    output = tmp_path / "receipt-redemption-state-v1.json"
    module.OUTPUT_PATH = output
    module.main()
    assert output.read_bytes() == _VECTOR_PATH.read_bytes()


def test_source_bindings_are_derived_from_the_exact_signed_swift_receipts() -> None:
    document = _document()
    cases = document["source_cases"]
    assert type(cases) is list
    actual = _source_cases()
    assert {case["case_id"] for case in cases if type(case) is dict} == set(actual)
    for case in cases:
        assert type(case) is dict
        case_id = case["case_id"]
        assert type(case_id) is str
        assert case["binding"] == actual[case_id]


def test_vector_surface_is_closed_canonical_and_non_authorizing() -> None:
    document = _document()
    assert set(document) == {
        "schema_version",
        "scope",
        "source_cases",
        "invalid_inputs",
        "recovery_inputs",
    }
    assert document["schema_version"] == 1
    assert type(document["schema_version"]) is int
    assert (
        document["scope"]
        == "RECEIPT_REDEMPTION_STATE_INTEROPERABILITY_ONLY_NOT_RECEIPT_ACCEPTANCE_OR_AUTHORIZATION"
    )
    cases = document["source_cases"]
    assert type(cases) is list and len(cases) == 2
    rendered = _VECTOR_PATH.read_text(encoding="utf-8")
    for forbidden in (
        '"signature"',
        '"public_key',
        '"challenge_nonce"',
        "private_key",
        "permit",
        "execution_authority",
        '"accepted"',
    ):
        assert forbidden not in rendered
    for case in cases:
        assert type(case) is dict
        assert set(case) == {
            "case_id",
            "source_fixture",
            "binding",
            "binding_canonical_json",
            "initial_global_state",
            "initial_global_state_canonical_json",
            "intent_pending_global_state",
            "intent_pending_global_state_canonical_json",
            "pending_state",
            "pending_state_canonical_json",
            "verified_state",
            "verified_state_canonical_json",
            "final_global_state",
            "final_global_state_canonical_json",
        }
        binding = case["binding"]
        assert type(binding) is dict
        assert set(binding) == {
            "challenge_digest",
            "challenge_nonce_digest",
            "configuration_digest",
            "expires_at",
            "receipt_digest",
            "receipt_id",
            "receipt_kind",
        }
        for name in (
            "challenge_digest",
            "challenge_nonce_digest",
            "configuration_digest",
            "receipt_digest",
        ):
            assert type(binding[name]) is str
            assert _DIGEST.fullmatch(binding[name])
        assert type(binding["receipt_id"]) is str
        assert _RECEIPT_ID.fullmatch(binding["receipt_id"])
        assert binding["receipt_kind"] in {"OPENAI_PAUSE_PLAN", "GA4_KEY_EVENT_CREATE_PLAN"}
        assert type(case["binding_canonical_json"]) is str
        assert case["binding_canonical_json"].encode("utf-8") == canonical_bytes(binding)
        pending = case["pending_state"]
        verified = case["verified_state"]
        initial_global = case["initial_global_state"]
        intent_pending_global = case["intent_pending_global_state"]
        final_global = case["final_global_state"]
        assert type(pending) is dict
        assert type(verified) is dict
        assert type(initial_global) is dict
        assert type(intent_pending_global) is dict
        assert type(final_global) is dict
        assert initial_global == {
            "schema_version": 1,
            "state": "READY",
            "generation": 1,
            "observed_at": "2030-01-01T00:00:29.000000Z",
        }
        assert intent_pending_global == {
            "schema_version": 1,
            "state": "INTENT_PENDING",
            "generation": 2,
            "observed_at": "2030-01-01T00:00:30.000000Z",
            "binding": binding,
        }
        assert pending == {
            "schema_version": 1,
            "state": "PENDING",
            "binding": binding,
            "reserved_at": "2030-01-01T00:00:30.000000Z",
        }
        assert verified == {
            "schema_version": 1,
            "state": "VERIFIED",
            "binding": binding,
            "reserved_at": "2030-01-01T00:00:30.000000Z",
            "verified_at": "2030-01-01T00:00:31.000000Z",
        }
        assert final_global == {
            "schema_version": 1,
            "state": "READY",
            "generation": 3,
            "observed_at": "2030-01-01T00:00:31.000000Z",
        }
        assert case["initial_global_state_canonical_json"].encode("utf-8") == canonical_bytes(
            initial_global
        )
        assert case["intent_pending_global_state_canonical_json"].encode(
            "utf-8"
        ) == canonical_bytes(intent_pending_global)
        assert case["pending_state_canonical_json"].encode("utf-8") == canonical_bytes(pending)
        assert case["verified_state_canonical_json"].encode("utf-8") == canonical_bytes(verified)
        assert case["final_global_state_canonical_json"].encode("utf-8") == canonical_bytes(
            final_global
        )


def test_invalid_inputs_cover_exact_numeric_canonical_and_scalar_boundaries() -> None:
    document = _document()
    invalid = document["invalid_inputs"]
    assert type(invalid) is list
    expected = {
        "pending_schema_version_float_alias",
        "pending_schema_version_boolean",
        "pending_receipt_kind_boolean",
        "pending_expiry_number",
        "pending_uppercase_receipt_digest",
        "pending_trailing_newline",
        "global_schema_version_float_alias",
        "global_schema_version_boolean",
        "global_generation_float_alias",
        "global_generation_boolean",
        "global_missing_observed_at",
        "global_observed_at_null",
        "global_observed_at_whole_seconds",
        "global_null",
        "global_intent_missing_binding",
        "global_intent_null_binding",
        "global_intent_receipt_digest_number",
    }
    assert {entry["case_id"] for entry in invalid if type(entry) is dict} == expected
    by_id = {entry["case_id"]: entry for entry in invalid if type(entry) is dict}
    for entry in by_id.values():
        assert set(entry) == {"case_id", "target", "raw_json"}
        assert entry["target"] in {"NONCE_STATE", "GLOBAL_STATE"}
        assert type(entry["raw_json"]) is str
        assert len(entry["raw_json"].encode("utf-8")) <= 4096
        assert 'challenge_nonce"' not in entry["raw_json"]

    float_alias = parse_json(by_id["pending_schema_version_float_alias"]["raw_json"])
    assert type(float_alias["schema_version"]) is float
    assert canonical_bytes(float_alias) != by_id["pending_schema_version_float_alias"][
        "raw_json"
    ].encode("utf-8")
    boolean_alias = parse_json(by_id["pending_schema_version_boolean"]["raw_json"])
    assert type(boolean_alias["schema_version"]) is bool
    kind_alias = parse_json(by_id["pending_receipt_kind_boolean"]["raw_json"])
    assert type(kind_alias["binding"]["receipt_kind"]) is bool
    expiry_alias = parse_json(by_id["pending_expiry_number"]["raw_json"])
    assert type(expiry_alias["binding"]["expires_at"]) is int
    uppercase = parse_json(by_id["pending_uppercase_receipt_digest"]["raw_json"])
    assert uppercase["binding"]["receipt_digest"].isupper()
    trailing = by_id["pending_trailing_newline"]["raw_json"]
    assert trailing.endswith("\n")
    assert canonical_bytes(parse_json(trailing)) != trailing.encode("utf-8")
    global_schema_float = parse_json(by_id["global_schema_version_float_alias"]["raw_json"])
    assert type(global_schema_float) is dict
    assert type(global_schema_float["schema_version"]) is float
    global_schema_boolean = parse_json(by_id["global_schema_version_boolean"]["raw_json"])
    assert type(global_schema_boolean) is dict
    assert type(global_schema_boolean["schema_version"]) is bool
    global_generation_float = parse_json(by_id["global_generation_float_alias"]["raw_json"])
    assert type(global_generation_float) is dict
    assert type(global_generation_float["generation"]) is float
    global_generation_boolean = parse_json(by_id["global_generation_boolean"]["raw_json"])
    assert type(global_generation_boolean) is dict
    assert type(global_generation_boolean["generation"]) is bool
    global_missing_observed_at = parse_json(by_id["global_missing_observed_at"]["raw_json"])
    assert type(global_missing_observed_at) is dict
    assert "observed_at" not in global_missing_observed_at
    global_observed_at_null = parse_json(by_id["global_observed_at_null"]["raw_json"])
    assert type(global_observed_at_null) is dict
    assert global_observed_at_null["observed_at"] is None
    global_whole_seconds = parse_json(by_id["global_observed_at_whole_seconds"]["raw_json"])
    assert type(global_whole_seconds) is dict
    assert global_whole_seconds["observed_at"] == "2030-01-01T00:00:29Z"
    assert parse_json(by_id["global_null"]["raw_json"]) is None
    global_intent_missing_binding = parse_json(by_id["global_intent_missing_binding"]["raw_json"])
    assert type(global_intent_missing_binding) is dict
    assert "binding" not in global_intent_missing_binding
    global_intent_null_binding = parse_json(by_id["global_intent_null_binding"]["raw_json"])
    assert type(global_intent_null_binding) is dict
    assert global_intent_null_binding["binding"] is None
    global_intent_digest_number = parse_json(
        by_id["global_intent_receipt_digest_number"]["raw_json"]
    )
    assert type(global_intent_digest_number) is dict
    global_intent_binding = global_intent_digest_number["binding"]
    assert type(global_intent_binding) is dict
    assert type(global_intent_binding["receipt_digest"]) is int


def test_recovery_inputs_are_closed_non_promoting_state_pairs() -> None:
    document = _document()
    recovery_inputs = document["recovery_inputs"]
    assert type(recovery_inputs) is list
    assert {entry["case_id"] for entry in recovery_inputs if type(entry) is dict} == {
        "missing_or_null_global_state",
        "pending_with_ready_global_state",
        "pending_with_different_intent_binding",
    }
    by_id = {entry["case_id"]: entry for entry in recovery_inputs if type(entry) is dict}

    missing = by_id["missing_or_null_global_state"]
    assert missing == {
        "case_id": "missing_or_null_global_state",
        "target": "GLOBAL_STATE",
        "missing_global_state": True,
        "null_global_raw_json": "null",
    }
    assert parse_json(missing["null_global_raw_json"]) is None

    ready_pair = by_id["pending_with_ready_global_state"]
    mismatch_pair = by_id["pending_with_different_intent_binding"]
    for pair in (ready_pair, mismatch_pair):
        assert set(pair) == {
            "case_id",
            "target",
            "nonce_state",
            "nonce_state_canonical_json",
            "global_state",
            "global_state_canonical_json",
        }
        assert pair["target"] == "STATE_PAIR"
        nonce_state = pair["nonce_state"]
        global_state = pair["global_state"]
        assert type(nonce_state) is dict
        assert type(global_state) is dict
        assert nonce_state["state"] == "PENDING"
        assert pair["nonce_state_canonical_json"].encode("utf-8") == canonical_bytes(nonce_state)
        assert pair["global_state_canonical_json"].encode("utf-8") == canonical_bytes(global_state)

    ready_global = ready_pair["global_state"]
    assert type(ready_global) is dict
    assert ready_global["state"] == "READY"
    assert "binding" not in ready_global

    mismatched_nonce = mismatch_pair["nonce_state"]
    mismatched_global = mismatch_pair["global_state"]
    assert type(mismatched_nonce) is dict
    assert type(mismatched_global) is dict
    nonce_binding = mismatched_nonce["binding"]
    intent_binding = mismatched_global["binding"]
    assert type(nonce_binding) is dict
    assert type(intent_binding) is dict
    assert set(nonce_binding) == set(intent_binding)
    assert nonce_binding["receipt_digest"] != intent_binding["receipt_digest"]
    assert type(intent_binding["receipt_digest"]) is str
    assert _DIGEST.fullmatch(intent_binding["receipt_digest"])


def test_generator_rejects_noncanonical_source_wrappers_except_the_one_known_legacy_fixture(
    tmp_path: Path,
) -> None:
    module = _generator()
    raw = canonical_bytes({"schema_version": 1})
    path = tmp_path / "source.json"
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="source is invalid"):
        module._read_canonical_json(path)
    assert module._read_canonical_json(path, allow_one_legacy_trailing_newline=True) == {
        "schema_version": 1
    }
    path.write_bytes(raw + b"\n\n")
    with pytest.raises(ValueError, match="source is invalid"):
        module._read_canonical_json(path, allow_one_legacy_trailing_newline=True)

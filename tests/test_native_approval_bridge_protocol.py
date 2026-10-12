"""Parity tests for the private, non-authorizing macOS bridge wire checker."""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import pickle
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

import pytest

from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeCandidateV1,
    NativeApprovalBridgeRequestV1,
    build_native_approval_bridge_request_v1,
    decode_native_approval_bridge_request_v1,
    derive_native_approval_bridge_source_context_digest_v1,
    revalidate_native_approval_bridge_candidate_v1,
    validate_native_approval_bridge_candidate_v1,
)
from adhelm.approval_protocol import ApprovalChallenge, ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DIRECTORY = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridge"
)
_GENERATOR_PATH = _FIXTURE_DIRECTORY / "generate_native_approval_bridge_vectors.py"
_VECTOR_PATH = _FIXTURE_DIRECTORY / "native-approval-bridge-protocol-v1.json"
_EXPECTED_SCOPE = (
    "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INTEROPERABILITY_ONLY_NOT_ACCEPTANCE_OR_AUTHORIZATION"
)
_DIGEST = "a" * 64


class _GeneratorApi(Protocol):
    OUTPUT_PATH: Path

    def main(self) -> None: ...


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "native_approval_bridge_vector_generator", _GENERATOR_PATH
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


def _cases() -> list[dict[str, object]]:
    cases = _document()["source_cases"]
    assert type(cases) is list and len(cases) == 2
    checked = [case for case in cases if type(case) is dict]
    assert len(checked) == 2
    return checked


def _case(case_id: str = "swift_ga4_key_event_create") -> dict[str, object]:
    matches = [case for case in _cases() if case["case_id"] == case_id]
    assert len(matches) == 1
    return matches[0]


def _metadata(case: dict[str, object]) -> dict[str, object]:
    value = case["metadata_fixture"]
    assert type(value) is dict
    assert set(value) == {
        "schema_version",
        "host_ref",
        "signing_key_ref",
        "mach_service_name",
        "client_bundle_identifier",
        "client_team_identifier",
        "host_bundle_identifier",
        "host_team_identifier",
        "signing_key_application_tag_hex",
        "signing_key_access_group",
        "key_storage",
        "provenance_service",
        "provenance_account",
        "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds",
        "maximum_issued_at_skew_seconds",
        "maximum_subplans",
        "maximum_policy_items",
    }
    return value


def _configuration(case: dict[str, object]) -> ApprovalHostConfiguration:
    metadata = _metadata(case)
    host_ref = metadata["host_ref"]
    signing_key_ref = metadata["signing_key_ref"]
    maximum_challenge_bytes = metadata["maximum_challenge_bytes"]
    maximum_challenge_lifetime_seconds = metadata["maximum_challenge_lifetime_seconds"]
    maximum_issued_at_skew_seconds = metadata["maximum_issued_at_skew_seconds"]
    maximum_subplans = metadata["maximum_subplans"]
    maximum_policy_items = metadata["maximum_policy_items"]
    assert type(host_ref) is str
    assert type(signing_key_ref) is str
    assert type(maximum_challenge_bytes) is int and not isinstance(maximum_challenge_bytes, bool)
    assert type(maximum_challenge_lifetime_seconds) is int and not isinstance(
        maximum_challenge_lifetime_seconds, bool
    )
    assert type(maximum_issued_at_skew_seconds) is int and not isinstance(
        maximum_issued_at_skew_seconds, bool
    )
    assert type(maximum_subplans) is int and not isinstance(maximum_subplans, bool)
    assert type(maximum_policy_items) is int and not isinstance(maximum_policy_items, bool)
    return ApprovalHostConfiguration(
        host_ref=host_ref,
        signing_key_ref=signing_key_ref,
        maximum_challenge_bytes=maximum_challenge_bytes,
        maximum_challenge_lifetime_seconds=maximum_challenge_lifetime_seconds,
        maximum_issued_at_skew_seconds=maximum_issued_at_skew_seconds,
        maximum_subplans=maximum_subplans,
        maximum_policy_items=maximum_policy_items,
    )


def _metadata_digest(case: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(_metadata(case))).hexdigest()


def _time(value: object) -> datetime:
    assert type(value) is str
    pattern = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    parsed = datetime.strptime(value, pattern)
    return parsed.replace(tzinfo=UTC)


def _request(case: dict[str, object]) -> NativeApprovalBridgeRequestV1:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_native_approval_bridge_request_v1(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


def _candidate(case: dict[str, object]) -> NativeApprovalBridgeCandidateV1:
    raw = case["candidate_canonical_json"]
    assert type(raw) is str
    return validate_native_approval_bridge_candidate_v1(
        raw.encode("utf-8"),
        request=_request(case),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        exchange_started_at=_time(case["exchange_started_at"]),
        current_now=_time(case["current_now"]),
    )


def _candidate_data(case: dict[str, object]) -> dict[str, object]:
    value = case["candidate"]
    assert type(value) is dict
    return value


def _candidate_raw(data: dict[str, object]) -> bytes:
    return canonical_bytes(data)


def _assert_sanitized(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code.startswith("NATIVE_APPROVAL_BRIDGE_")
    assert "sentinel" not in error.value.message


def test_native_approval_bridge_vectors_regenerate_exactly(tmp_path: Path) -> None:
    module = cast(_GeneratorApi, _generator())
    output = tmp_path / "native-approval-bridge-protocol-v1.json"
    module.OUTPUT_PATH = output
    module.main()
    assert output.read_bytes() == _VECTOR_PATH.read_bytes()


def test_vector_surface_is_closed_synthetic_and_non_authorizing() -> None:
    document = _document()
    assert set(document) == {"schema_version", "scope", "source_cases", "invalid_inputs"}
    assert document["schema_version"] == 1
    assert type(document["schema_version"]) is int
    assert document["scope"] == _EXPECTED_SCOPE
    rendered = _VECTOR_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "PinnedApprovalTrust",
        "accept_approval_receipt",
        "execution_authority",
        "provider_egress",
        "ledger_claim",
        "permit",
    ):
        assert forbidden not in rendered
    for case in _cases():
        assert set(case) == {
            "case_id",
            "source_fixture",
            "metadata_fixture",
            "source_context",
            "source_context_canonical_json",
            "source_context_digest",
            "request",
            "request_canonical_json",
            "request_digest",
            "candidate",
            "candidate_canonical_json",
            "candidate_digest",
            "exchange_started_at",
            "current_now",
        }
        assert type(case["source_context_canonical_json"]) is str
        source_context = case["source_context"]
        assert type(source_context) is dict
        assert set(source_context) == {
            "schema_version",
            "scope",
            "workspace_profile_ref",
            "source_stream",
            "source_plan_record_digest",
            "checkpoint_digest",
        }
        source_context_canonical = case["source_context_canonical_json"]
        request_canonical = case["request_canonical_json"]
        candidate_canonical = case["candidate_canonical_json"]
        assert type(source_context_canonical) is str
        assert type(request_canonical) is str
        assert type(candidate_canonical) is str
        assert source_context_canonical.encode("utf-8") == canonical_bytes(source_context)
        assert (
            case["source_context_digest"]
            == hashlib.sha256(canonical_bytes(source_context)).hexdigest()
        )
        assert request_canonical.encode("utf-8") == canonical_bytes(case["request"])
        assert candidate_canonical.encode("utf-8") == canonical_bytes(case["candidate"])
        assert (
            case["request_digest"] == hashlib.sha256(request_canonical.encode("utf-8")).hexdigest()
        )
        assert (
            case["candidate_digest"]
            == hashlib.sha256(candidate_canonical.encode("utf-8")).hexdigest()
        )


@pytest.mark.parametrize("case_id", ["swift_ga4_key_event_create", "swift_openai_pause"])
def test_signed_swift_fixture_vectors_validate_as_non_authorizing_private_evidence(
    case_id: str,
) -> None:
    case = _case(case_id)
    request = _request(case)
    evidence = _candidate(case)
    candidate = _candidate_data(case)
    assert request.kind.value == candidate["receipt_kind"]
    assert evidence.kind == request.kind
    assert evidence.receipt_digest == candidate["receipt_digest"]
    assert evidence.receipt_id == candidate["receipt_id"]
    assert evidence.request_digest == case["request_digest"]
    assert evidence.native_observed_at == _time(candidate["native_observed_at"])
    assert evidence.expires_at == _time(candidate["expires_at"])
    assert "private non-authorizing evidence" in repr(request)
    assert "private non-authorizing evidence" in repr(evidence)
    assert str(candidate["canonical_receipt_b64url"]) not in repr(evidence)
    with pytest.raises(TypeError):
        vars(request)
    with pytest.raises(TypeError):
        vars(evidence)
    with pytest.raises(TypeError):
        asdict(cast(Any, evidence))
    with pytest.raises(TypeError):
        pickle.dumps(evidence)


def test_source_context_digest_is_domain_separated_and_request_binds_it() -> None:
    case = _case()
    context = case["source_context"]
    assert type(context) is dict
    workspace = context["workspace_profile_ref"]
    source_plan_record_digest = context["source_plan_record_digest"]
    checkpoint_digest = context["checkpoint_digest"]
    assert type(workspace) is str
    assert type(source_plan_record_digest) is str
    assert type(checkpoint_digest) is str
    derived = derive_native_approval_bridge_source_context_digest_v1(
        workspace_profile_ref=workspace,
        source_plan_record_digest=source_plan_record_digest,
        checkpoint_digest=checkpoint_digest,
    )
    assert derived == case["source_context_digest"]
    request = _request(case)
    assert request.source_context_digest == derived
    assert (
        derive_native_approval_bridge_source_context_digest_v1(
            workspace_profile_ref=workspace,
            source_plan_record_digest=source_plan_record_digest[:-1] + "a",
            checkpoint_digest=checkpoint_digest,
        )
        != derived
    )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("request_id_digest", "0" * 64),
        ("request_digest", "0" * 64),
        ("source_context_digest", "0" * 64),
        ("metadata_digest", "0" * 64),
        ("protocol_configuration_digest", "0" * 64),
        ("canonical_challenge_b64url", "AA"),
        ("challenge_digest", "0" * 64),
        ("challenge_nonce_digest", "0" * 64),
        ("receipt_kind", "DELEGATION_POLICY_INSTALL"),
        ("expires_at", "2030-01-01T00:03:59Z"),
        ("receipt_digest", "0" * 64),
        ("receipt_id", "approval-receipt:___________________________________________"),
        ("active_public_key_fingerprint", "0" * 64),
        ("native_observation_status", "PENDING"),
    ],
)
def test_candidate_cross_binding_rejects_each_changed_closed_fact(
    field: str, replacement: str
) -> None:
    case = _case()
    candidate = dict(_candidate_data(case))
    candidate[field] = replacement
    with pytest.raises(AdhelmError) as error:
        validate_native_approval_bridge_candidate_v1(
            _candidate_raw(candidate),
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    _assert_sanitized(error)


@pytest.mark.parametrize(
    "case_id",
    [
        "request_schema_version_float_alias",
        "request_metadata_digest_mismatch",
        "candidate_non_success_status",
        "candidate_request_digest_mismatch",
        "candidate_native_observed_at_whole_seconds",
        "candidate_extra_field",
        "candidate_receipt_signature_mismatch",
    ],
)
def test_shared_invalid_vectors_reject_without_raw_diagnostic(case_id: str) -> None:
    document = _document()
    values = document["invalid_inputs"]
    assert type(values) is list
    matching = [item for item in values if type(item) is dict and item.get("case_id") == case_id]
    assert len(matching) == 1
    invalid = matching[0]
    target = invalid["target"]
    raw = invalid["raw_json"]
    assert type(target) is str
    assert type(raw) is str
    case = _case()
    with pytest.raises(AdhelmError) as error:
        if target == "REQUEST":
            decode_native_approval_bridge_request_v1(
                raw.encode("utf-8"),
                configuration=_configuration(case),
                expected_metadata_digest=_metadata_digest(case),
            )
        else:
            assert target == "CANDIDATE"
            validate_native_approval_bridge_candidate_v1(
                raw.encode("utf-8"),
                request=_request(case),
                configuration=_configuration(case),
                expected_metadata_digest=_metadata_digest(case),
                exchange_started_at=_time(case["exchange_started_at"]),
                current_now=_time(case["current_now"]),
            )
    _assert_sanitized(error)


def test_candidate_rejects_exchange_start_before_challenge_issuance_window() -> None:
    case = _case()
    with pytest.raises(AdhelmError) as error:
        validate_native_approval_bridge_candidate_v1(
            str(case["candidate_canonical_json"]).encode("utf-8"),
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=datetime(2029, 12, 31, 23, 58, tzinfo=UTC),
            current_now=_time(case["current_now"]),
        )
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"


def test_candidate_rejects_python_current_time_at_or_after_expiry() -> None:
    case = _case()
    with pytest.raises(AdhelmError) as error:
        validate_native_approval_bridge_candidate_v1(
            str(case["candidate_canonical_json"]).encode("utf-8"),
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=datetime(2030, 1, 1, 0, 4, tzinfo=UTC),
        )
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"


@pytest.mark.parametrize(
    "exchange_started_at,current_now",
    [
        (
            datetime(2030, 1, 1, 0, 0, 31, tzinfo=UTC),
            datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC),
        ),
        (
            datetime(2030, 1, 1, 0, 0, 29, tzinfo=UTC),
            datetime(2030, 1, 1, 0, 0, 29, tzinfo=UTC),
        ),
    ],
)
def test_candidate_rejects_local_clock_rollback_and_observation_after_current_time(
    exchange_started_at: datetime, current_now: datetime
) -> None:
    case = _case()
    with pytest.raises(AdhelmError) as error:
        validate_native_approval_bridge_candidate_v1(
            str(case["candidate_canonical_json"]).encode("utf-8"),
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=exchange_started_at,
            current_now=current_now,
        )
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"


def test_constructed_or_tampered_private_values_reparse_to_a_fixed_failure() -> None:
    case = _case()
    forged_request = object.__new__(NativeApprovalBridgeRequestV1)
    with pytest.raises(AdhelmError) as request_error:
        validate_native_approval_bridge_candidate_v1(
            str(case["candidate_canonical_json"]).encode("utf-8"),
            request=forged_request,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    _assert_sanitized(request_error)

    forged_candidate = object.__new__(NativeApprovalBridgeCandidateV1)
    with pytest.raises(AdhelmError) as candidate_error:
        revalidate_native_approval_bridge_candidate_v1(
            forged_candidate,
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    _assert_sanitized(candidate_error)

    candidate = _candidate(case)
    object.__setattr__(candidate, "_NativeApprovalBridgeCandidateV1__canonical_bytes", b"{}")
    with pytest.raises(AdhelmError) as mutated_error:
        revalidate_native_approval_bridge_candidate_v1(
            candidate,
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    _assert_sanitized(mutated_error)


def test_private_module_does_not_import_or_construct_an_authority_surface() -> None:
    module_path = _ROOT / "src/adhelm/_native_approval_bridge_protocol.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        f"{node.module}.{alias.name}" if node.module else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for forbidden in (
        "adhelm.approval_acceptance.PinnedApprovalTrust",
        "adhelm.approval_acceptance.accept_approval_receipt",
        "adhelm.ledger.Ledger",
        "subprocess",
        "keyring",
        "os",
    ):
        assert forbidden not in imports


def test_request_builder_rejects_noncanonical_or_wrong_expected_metadata_before_candidate_use() -> (
    None
):
    case = _case()
    request = _request(case)
    source_context = request.source_context_digest
    challenge_raw = parse_json(request.canonical_bytes)["canonical_challenge_b64url"]
    assert type(challenge_raw) is str
    challenge_bytes = base64.urlsafe_b64decode(
        challenge_raw + "=" * ((4 - len(challenge_raw) % 4) % 4)
    )
    with pytest.raises(AdhelmError) as error:
        build_native_approval_bridge_request_v1(
            object.__new__(ApprovalChallenge),
            configuration=_configuration(case),
            request_id=b"0" * 32,
            source_context_digest=source_context,
            expected_metadata_digest=_DIGEST,
        )
    _assert_sanitized(error)
    with pytest.raises(AdhelmError) as oversized:
        decode_native_approval_bridge_request_v1(
            b"x" * 98_305,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
        )
    _assert_sanitized(oversized)
    assert len(challenge_bytes) <= 65_536

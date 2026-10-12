"""Parity tests for the private, non-authorizing Meta approval-bridge V2 wire."""

from __future__ import annotations

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

from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    build_meta_campaign_pause_approval_bridge_request_v2,
    decode_meta_campaign_pause_approval_bridge_request_v2,
    derive_meta_campaign_pause_approval_bridge_source_context_digest_v2,
    revalidate_meta_campaign_pause_approval_bridge_candidate_v2,
    revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange,
    validate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm._native_approval_bridge_protocol import (
    decode_native_approval_bridge_request_v1,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DIRECTORY = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridgeV2MetaPause"
)
_GENERATOR_PATH = _FIXTURE_DIRECTORY / "generate_meta_campaign_pause_bridge_vectors.py"
_VECTOR_PATH = _FIXTURE_DIRECTORY / "meta-campaign-pause-approval-bridge-v2.json"
_V1_VECTOR_PATH = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridge/"
    "native-approval-bridge-protocol-v1.json"
)
_EXPECTED_SCOPE = (
    "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_INTEROPERABILITY_ONLY_"
    "NOT_ACCEPTANCE_OR_AUTHORIZATION"
)
_METADATA_FIELDS = {
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


class _GeneratorApi(Protocol):
    OUTPUT_PATH: Path
    WHOLE_SOURCE_PATH: Path

    def build_document(self) -> dict[str, object]: ...

    def main(self) -> None: ...


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "meta_campaign_pause_approval_bridge_v2_vector_generator", _GENERATOR_PATH
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


def _case(case_id: str = "meta_campaign_pause_whole_seconds") -> dict[str, object]:
    matches = [case for case in _cases() if case["case_id"] == case_id]
    assert len(matches) == 1
    return matches[0]


def _metadata(case: dict[str, object]) -> dict[str, object]:
    value = case["metadata_fixture"]
    assert type(value) is dict and set(value) == _METADATA_FIELDS
    return value


def _configuration(case: dict[str, object]) -> ApprovalHostConfiguration:
    metadata = _metadata(case)
    values = (
        metadata["host_ref"],
        metadata["signing_key_ref"],
        metadata["maximum_challenge_bytes"],
        metadata["maximum_challenge_lifetime_seconds"],
        metadata["maximum_issued_at_skew_seconds"],
        metadata["maximum_subplans"],
        metadata["maximum_policy_items"],
    )
    assert type(values[0]) is str and type(values[1]) is str
    assert all(type(value) is int and not isinstance(value, bool) for value in values[2:])
    return ApprovalHostConfiguration(
        host_ref=values[0],
        signing_key_ref=values[1],
        maximum_challenge_bytes=cast(int, values[2]),
        maximum_challenge_lifetime_seconds=cast(int, values[3]),
        maximum_issued_at_skew_seconds=cast(int, values[4]),
        maximum_subplans=cast(int, values[5]),
        maximum_policy_items=cast(int, values[6]),
    )


def _metadata_digest(case: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(_metadata(case))).hexdigest()


def _time(value: object) -> datetime:
    assert type(value) is str
    pattern = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    parsed = datetime.strptime(value, pattern)
    return parsed.replace(tzinfo=UTC)


def _request(case: dict[str, object]) -> NativeMetaCampaignPauseApprovalBridgeRequestV2:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_meta_campaign_pause_approval_bridge_request_v2(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


def _candidate(case: dict[str, object]) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
    raw = case["candidate_canonical_json"]
    assert type(raw) is str
    return validate_meta_campaign_pause_approval_bridge_candidate_v2(
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


def _assert_sanitized(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code.startswith("NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_")
    assert "sentinel" not in error.value.message


def test_vectors_regenerate_exactly(tmp_path: Path) -> None:
    module = cast(_GeneratorApi, _generator())
    output = tmp_path / "meta-campaign-pause-approval-bridge-v2.json"
    module.OUTPUT_PATH = output
    module.main()
    assert output.read_bytes() == _VECTOR_PATH.read_bytes()


def test_generator_rejects_changed_m42_source_before_emitting(tmp_path: Path) -> None:
    module = cast(_GeneratorApi, _generator())
    source = parse_json(
        (
            _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause/"
            "meta-campaign-pause-approval-v1.json"
        ).read_bytes()
    )
    assert type(source) is dict
    source["challenge_digest"] = "0" * 64
    changed = tmp_path / "changed-m42-source.json"
    changed.write_bytes(canonical_bytes(source))
    module.WHOLE_SOURCE_PATH = changed
    with pytest.raises(ValueError, match="vector source is invalid"):
        module.build_document()


def test_generator_rejects_scalar_alias_in_m42_configuration(tmp_path: Path) -> None:
    module = cast(_GeneratorApi, _generator())
    source = parse_json(
        (
            _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause/"
            "meta-campaign-pause-approval-v1.json"
        ).read_bytes()
    )
    assert type(source) is dict
    configuration = source["configuration"]
    assert type(configuration) is dict
    configuration["maximum_challenge_bytes"] = True
    changed = tmp_path / "changed-m42-configuration.json"
    changed.write_bytes(canonical_bytes(source))
    module.WHOLE_SOURCE_PATH = changed
    with pytest.raises(ValueError, match="vector source is invalid"):
        module.build_document()


def test_vector_surface_is_closed_synthetic_and_non_authorizing() -> None:
    document = _document()
    assert set(document) == {
        "schema_version",
        "fixture_kind",
        "scope",
        "source_cases",
        "invalid_inputs",
    }
    assert document["schema_version"] == 1
    assert document["fixture_kind"] == "META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2"
    assert document["scope"] == _EXPECTED_SCOPE
    rendered = _VECTOR_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "PinnedApprovalTrust",
        "credential_binding_ref",
        "provider_egress",
        "ledger_claim",
        "execution_permit",
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
        context = case["source_context"]
        assert type(context) is dict
        assert set(context) == {
            "schema_version",
            "scope",
            "workspace_profile_ref",
            "source_stream",
            "source_plan_record_digest",
            "checkpoint_digest",
        }
        source_canonical = case["source_context_canonical_json"]
        request_canonical = case["request_canonical_json"]
        candidate_canonical = case["candidate_canonical_json"]
        assert type(source_canonical) is str
        assert type(request_canonical) is str
        assert type(candidate_canonical) is str
        assert source_canonical.encode() == canonical_bytes(context)
        assert request_canonical.encode() == canonical_bytes(case["request"])
        assert candidate_canonical.encode() == canonical_bytes(case["candidate"])
        assert case["source_context_digest"] == hashlib.sha256(canonical_bytes(context)).hexdigest()
        assert case["request_digest"] == hashlib.sha256(request_canonical.encode()).hexdigest()
        assert case["candidate_digest"] == hashlib.sha256(candidate_canonical.encode()).hexdigest()


@pytest.mark.parametrize(
    "case_id", ["meta_campaign_pause_whole_seconds", "meta_campaign_pause_fractional"]
)
def test_m42_vectors_validate_as_private_non_authorizing_v2_evidence(case_id: str) -> None:
    case = _case(case_id)
    request = _request(case)
    evidence = _candidate(case)
    candidate = _candidate_data(case)
    assert request.kind.value == "META_CAMPAIGN_PAUSE_PLAN"
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


def test_source_context_is_exactly_v2_domain_separated_and_request_binds_it() -> None:
    case = _case()
    context = case["source_context"]
    assert type(context) is dict
    workspace = context["workspace_profile_ref"]
    source_plan_record_digest = context["source_plan_record_digest"]
    checkpoint_digest = context["checkpoint_digest"]
    assert type(workspace) is str
    assert type(source_plan_record_digest) is str
    assert type(checkpoint_digest) is str
    derived = derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(
        workspace_profile_ref=workspace,
        source_plan_record_digest=source_plan_record_digest,
        checkpoint_digest=checkpoint_digest,
    )
    assert derived == case["source_context_digest"]
    assert _request(case).source_context_digest == derived
    assert (
        derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(
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
        ("receipt_kind", "OPENAI_PAUSE_PLAN"),
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
        validate_meta_campaign_pause_approval_bridge_candidate_v2(
            canonical_bytes(candidate),
            request=_request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    _assert_sanitized(error)


def test_invalid_vector_inputs_fail_closed() -> None:
    case = _case()
    inputs = _document()["invalid_inputs"]
    assert type(inputs) is list
    for item in inputs:
        assert type(item) is dict
        target = item["target"]
        raw = item["raw_json"]
        assert type(target) is str and type(raw) is str
        with pytest.raises(AdhelmError) as error:
            if target == "REQUEST":
                decode_meta_campaign_pause_approval_bridge_request_v2(
                    raw.encode(),
                    configuration=_configuration(case),
                    expected_metadata_digest=_metadata_digest(case),
                )
            else:
                validate_meta_campaign_pause_approval_bridge_candidate_v2(
                    raw.encode(),
                    request=_request(case),
                    configuration=_configuration(case),
                    expected_metadata_digest=_metadata_digest(case),
                    exchange_started_at=_time(case["exchange_started_at"]),
                    current_now=_time(case["current_now"]),
                )
        _assert_sanitized(error)


def test_v1_and_v2_are_mutually_non_interoperable_and_v2_is_meta_only() -> None:
    v2_case = _case()
    v2_request = v2_case["request_canonical_json"]
    assert type(v2_request) is str
    with pytest.raises(AdhelmError) as v1_error:
        decode_native_approval_bridge_request_v1(
            v2_request.encode(),
            configuration=_configuration(v2_case),
            expected_metadata_digest=_metadata_digest(v2_case),
        )
    assert v1_error.value.code.startswith("NATIVE_APPROVAL_BRIDGE_")

    v1_document = parse_json(_V1_VECTOR_PATH.read_bytes())
    assert type(v1_document) is dict
    v1_cases = v1_document["source_cases"]
    assert type(v1_cases) is list and type(v1_cases[0]) is dict
    v1_case = cast(dict[str, object], v1_cases[0])
    v1_metadata = v1_case["metadata_fixture"]
    assert type(v1_metadata) is dict
    v1_configuration = ApprovalHostConfiguration(
        host_ref=cast(str, v1_metadata["host_ref"]),
        signing_key_ref=cast(str, v1_metadata["signing_key_ref"]),
        maximum_challenge_bytes=cast(int, v1_metadata["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=cast(
            int, v1_metadata["maximum_challenge_lifetime_seconds"]
        ),
        maximum_issued_at_skew_seconds=cast(int, v1_metadata["maximum_issued_at_skew_seconds"]),
        maximum_subplans=cast(int, v1_metadata["maximum_subplans"]),
        maximum_policy_items=cast(int, v1_metadata["maximum_policy_items"]),
    )
    v1_raw = v1_case["request_canonical_json"]
    assert type(v1_raw) is str
    with pytest.raises(AdhelmError) as v2_error:
        decode_meta_campaign_pause_approval_bridge_request_v2(
            v1_raw.encode(),
            configuration=v1_configuration,
            expected_metadata_digest=hashlib.sha256(canonical_bytes(v1_metadata)).hexdigest(),
        )
    _assert_sanitized(v2_error)

    v1_wire = parse_json(v1_raw.encode())
    assert type(v1_wire) is dict
    v1_wire["schema_version"] = 2
    v1_wire["message_type"] = "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_REQUEST_V2"
    with pytest.raises(AdhelmError) as kind_error:
        decode_meta_campaign_pause_approval_bridge_request_v2(
            canonical_bytes(v1_wire),
            configuration=v1_configuration,
            expected_metadata_digest=hashlib.sha256(canonical_bytes(v1_metadata)).hexdigest(),
        )
    _assert_sanitized(kind_error)


def test_request_decode_rejects_canonical_but_incomplete_meta_challenge() -> None:
    """Typed V2 admission reconstructs the complete M42 subject immediately."""
    case = _case()
    request = case["request"]
    assert type(request) is dict
    altered_request = dict(request)
    encoded_challenge = altered_request["canonical_challenge_b64url"]
    assert type(encoded_challenge) is str
    challenge = parse_json(base64.urlsafe_b64decode(encoded_challenge + "==="))
    assert type(challenge) is dict
    action = challenge["action"]
    assert type(action) is dict
    # This remains canonical JSON and has the exact Meta kind, but it is not
    # the closed M42 action shape.  Decode must not defer that fact to a
    # later exchange-time call.
    del action["plan"]
    altered_request["canonical_challenge_b64url"] = (
        base64.urlsafe_b64encode(canonical_bytes(challenge)).decode("ascii").rstrip("=")
    )
    with pytest.raises(AdhelmError) as error:
        decode_meta_campaign_pause_approval_bridge_request_v2(
            canonical_bytes(altered_request),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
        )
    _assert_sanitized(error)


def test_v2_instance_revalidation_and_time_window_fail_closed() -> None:
    case = _case()
    request = _request(case)
    candidate = _candidate(case)
    reparsed_request = revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange(
        request,
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        exchange_started_at=_time(case["exchange_started_at"]),
    )
    assert reparsed_request.digest == request.digest
    assert (
        revalidate_meta_campaign_pause_approval_bridge_candidate_v2(
            candidate,
            request=request,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        ).receipt_digest
        == candidate.receipt_digest
    )
    with pytest.raises(AdhelmError) as error:
        revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange(
            request,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=datetime(2030, 1, 1, 0, 4, tzinfo=UTC),
        )
    _assert_sanitized(error)


@pytest.mark.parametrize("maximum", [98_305, 131_073])
def test_oversized_wire_fails_before_any_decode(maximum: int) -> None:
    case = _case()
    raw = b"x" * maximum
    with pytest.raises(AdhelmError) as error:
        if maximum == 98_305:
            decode_meta_campaign_pause_approval_bridge_request_v2(
                raw,
                configuration=_configuration(case),
                expected_metadata_digest=_metadata_digest(case),
            )
        else:
            validate_meta_campaign_pause_approval_bridge_candidate_v2(
                raw,
                request=_request(case),
                configuration=_configuration(case),
                expected_metadata_digest=_metadata_digest(case),
                exchange_started_at=_time(case["exchange_started_at"]),
                current_now=_time(case["current_now"]),
            )
    _assert_sanitized(error)


def test_constructed_request_does_not_bypass_canonical_reparse() -> None:
    case = _case()
    constructed = object.__new__(NativeMetaCampaignPauseApprovalBridgeRequestV2)
    with pytest.raises(AdhelmError) as error:
        revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange(
            constructed,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
        )
    _assert_sanitized(error)


def test_builder_rejects_non_meta_challenge_before_request_issue() -> None:
    v1_document = parse_json(_V1_VECTOR_PATH.read_bytes())
    assert type(v1_document) is dict
    source_cases = v1_document["source_cases"]
    assert type(source_cases) is list and type(source_cases[0]) is dict
    source = cast(dict[str, object], source_cases[0])
    metadata = source["metadata_fixture"]
    raw_challenge = source["request"]
    assert type(metadata) is dict and type(raw_challenge) is dict
    configuration = ApprovalHostConfiguration(
        host_ref=cast(str, metadata["host_ref"]),
        signing_key_ref=cast(str, metadata["signing_key_ref"]),
        maximum_challenge_bytes=cast(int, metadata["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=cast(
            int, metadata["maximum_challenge_lifetime_seconds"]
        ),
        maximum_issued_at_skew_seconds=cast(int, metadata["maximum_issued_at_skew_seconds"]),
        maximum_subplans=cast(int, metadata["maximum_subplans"]),
        maximum_policy_items=cast(int, metadata["maximum_policy_items"]),
    )
    encoded = base64.urlsafe_b64decode(
        cast(str, raw_challenge["canonical_challenge_b64url"]) + "==="
    )
    from adhelm.approval_protocol import parse_approval_challenge_wire

    challenge = parse_approval_challenge_wire(encoded, configuration=configuration)
    with pytest.raises(AdhelmError) as error:
        build_meta_campaign_pause_approval_bridge_request_v2(
            challenge,
            configuration=configuration,
            request_id=bytes(range(32)),
            source_context_digest="1" * 64,
            expected_metadata_digest=hashlib.sha256(canonical_bytes(metadata)).hexdigest(),
        )
    _assert_sanitized(error)

"""Independent adversarial checks for the private Meta approval-bridge V2 codec.

The cases consume only checked synthetic fixture bytes.  They do not open a
native connection, use a credential store, create a grant, write a ledger, or
call a provider.
"""

from __future__ import annotations

import ast
import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    decode_meta_campaign_pause_approval_bridge_request_v2,
    derive_meta_campaign_pause_approval_bridge_source_context_digest_v2,
    revalidate_meta_campaign_pause_approval_bridge_candidate_v2,
    validate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm._native_approval_bridge_protocol import (
    decode_native_approval_bridge_request_v1,
    derive_native_approval_bridge_source_context_digest_v1,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError

_ROOT = Path(__file__).resolve().parents[1]
_V2_VECTORS = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridgeV2MetaPause/meta-campaign-pause-approval-bridge-v2.json"
)
_V1_VECTORS = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridge/native-approval-bridge-protocol-v1.json"
)
_MODULE = _ROOT / "src/adhelm/_meta_campaign_pause_approval_bridge_protocol_v2.py"
_ERROR_CODES = {
    "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID",
    "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_BINDING_INVALID",
    "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_TIME_INVALID",
}


def _document(path: Path = _V2_VECTORS) -> dict[str, object]:
    raw = path.read_bytes()
    value = parse_json(raw)
    assert type(value) is dict
    assert canonical_bytes(value) == raw
    return value


def _case(case_id: str = "meta_campaign_pause_whole_seconds") -> dict[str, object]:
    values = _document()["source_cases"]
    assert type(values) is list
    matches = [item for item in values if type(item) is dict and item.get("case_id") == case_id]
    assert len(matches) == 1
    return matches[0]


def _configuration(case: dict[str, object]) -> ApprovalHostConfiguration:
    metadata = case["metadata_fixture"]
    assert type(metadata) is dict
    names = (
        "host_ref",
        "signing_key_ref",
        "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds",
        "maximum_issued_at_skew_seconds",
        "maximum_subplans",
        "maximum_policy_items",
    )
    values = {name: metadata[name] for name in names}
    assert type(values["host_ref"]) is str
    assert type(values["signing_key_ref"]) is str
    for name in names[2:]:
        assert type(values[name]) is int and not isinstance(values[name], bool)
    return ApprovalHostConfiguration(
        host_ref=values["host_ref"],
        signing_key_ref=values["signing_key_ref"],
        maximum_challenge_bytes=cast(int, values["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=cast(int, values["maximum_challenge_lifetime_seconds"]),
        maximum_issued_at_skew_seconds=cast(int, values["maximum_issued_at_skew_seconds"]),
        maximum_subplans=cast(int, values["maximum_subplans"]),
        maximum_policy_items=cast(int, values["maximum_policy_items"]),
    )


def _metadata_digest(case: dict[str, object]) -> str:
    metadata = case["metadata_fixture"]
    assert type(metadata) is dict
    return hashlib.sha256(canonical_bytes(metadata)).hexdigest()


def _time(value: object) -> datetime:
    assert type(value) is str and value.endswith("Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(UTC)


def _request(case: dict[str, object]) -> NativeMetaCampaignPauseApprovalBridgeRequestV2:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_meta_campaign_pause_approval_bridge_request_v2(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


def _candidate_data(case: dict[str, object]) -> dict[str, object]:
    value = case["candidate"]
    assert type(value) is dict
    return value


def _validate(
    case: dict[str, object], raw: bytes
) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
    return validate_meta_campaign_pause_approval_bridge_candidate_v2(
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


@pytest.mark.parametrize(
    "case_id", ["meta_campaign_pause_whole_seconds", "meta_campaign_pause_fractional"]
)
def test_m42_whole_and_fractional_vectors_remain_exact_private_v2_evidence(
    case_id: str,
) -> None:
    case = _case(case_id)
    request = _request(case)
    candidate_raw = case["candidate_canonical_json"]
    assert type(candidate_raw) is str
    evidence = _validate(case, candidate_raw.encode("utf-8"))

    assert request.kind.value == "META_CAMPAIGN_PAUSE_PLAN"
    assert evidence.kind is request.kind
    assert evidence.request_digest == case["request_digest"]
    assert evidence.receipt_digest == _candidate_data(case)["receipt_digest"]
    assert "private non-authorizing evidence" in repr(request)
    assert "private non-authorizing evidence" in repr(evidence)
    assert not hasattr(evidence, "grant")
    assert not hasattr(evidence, "trust")
    assert not hasattr(evidence, "permit")


def test_v2_source_context_is_domain_separated_from_v1_and_strict() -> None:
    case = _case()
    context = case["source_context"]
    assert type(context) is dict
    workspace = context["workspace_profile_ref"]
    record_digest = context["source_plan_record_digest"]
    checkpoint_digest = context["checkpoint_digest"]
    assert type(workspace) is str
    assert type(record_digest) is str
    assert type(checkpoint_digest) is str

    v2 = derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(
        workspace_profile_ref=workspace,
        source_plan_record_digest=record_digest,
        checkpoint_digest=checkpoint_digest,
    )
    v1 = derive_native_approval_bridge_source_context_digest_v1(
        workspace_profile_ref=workspace,
        source_plan_record_digest=record_digest,
        checkpoint_digest=checkpoint_digest,
    )
    assert v2 == case["source_context_digest"]
    assert v2 != v1

    for args in (
        {
            "workspace_profile_ref": "bad/sentinel",
            "source_plan_record_digest": record_digest,
            "checkpoint_digest": checkpoint_digest,
        },
        {
            "workspace_profile_ref": workspace,
            "source_plan_record_digest": True,
            "checkpoint_digest": checkpoint_digest,
        },
        {
            "workspace_profile_ref": workspace,
            "source_plan_record_digest": record_digest,
            "checkpoint_digest": "A" * 64,
        },
    ):
        with pytest.raises(AdhelmError) as raised:
            derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(**args)
        assert raised.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"
        assert "sentinel" not in raised.value.message


def test_v1_codec_cannot_be_relabelled_to_accept_the_meta_v2_challenge() -> None:
    case = _case()
    request_data = case["request"]
    assert type(request_data) is dict
    v1_shaped = dict(request_data)
    v1_shaped["schema_version"] = 1
    v1_shaped["message_type"] = "NATIVE_APPROVAL_BRIDGE_REQUEST_V1"

    with pytest.raises(AdhelmError) as v1_rejected:
        decode_native_approval_bridge_request_v1(
            canonical_bytes(v1_shaped),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
        )
    assert v1_rejected.value.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"

    v1_document = _document(_V1_VECTORS)
    v1_cases = v1_document["source_cases"]
    assert type(v1_cases) is list and type(v1_cases[0]) is dict
    v1_case = v1_cases[0]
    raw = v1_case["request_canonical_json"]
    assert type(raw) is str
    with pytest.raises(AdhelmError) as v2_rejected:
        decode_meta_campaign_pause_approval_bridge_request_v2(
            raw.encode("utf-8"),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
        )
    assert v2_rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"


def test_request_rejects_a_canonical_but_incomplete_meta_subject() -> None:
    case = _case()
    request = case["request"]
    assert type(request) is dict
    altered = dict(request)
    incomplete_challenge = canonical_bytes(
        {"schema_version": 1, "kind": "META_CAMPAIGN_PAUSE_PLAN"}
    )
    altered["canonical_challenge_b64url"] = (
        base64.urlsafe_b64encode(incomplete_challenge).decode("ascii").rstrip("=")
    )
    with pytest.raises(AdhelmError) as rejected:
        decode_meta_campaign_pause_approval_bridge_request_v2(
            canonical_bytes(altered),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
        )
    assert rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"


def test_request_reconstructs_nested_meta_plan_spend_account_and_expiry_bindings() -> None:
    case = _case()
    request = case["request"]
    assert type(request) is dict
    encoded = request["canonical_challenge_b64url"]
    assert type(encoded) is str
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    original = parse_json(base64.urlsafe_b64decode(encoded + padding))
    assert type(original) is dict

    def changed_wire(mutated: dict[str, object]) -> bytes:
        altered = dict(request)
        altered["canonical_challenge_b64url"] = (
            base64.urlsafe_b64encode(canonical_bytes(mutated)).decode("ascii").rstrip("=")
        )
        return canonical_bytes(altered)

    for mutation in ("spend", "account", "expiry"):
        challenge = parse_json(canonical_bytes(original))
        assert type(challenge) is dict
        action = challenge["action"]
        assert type(action) is dict
        if mutation == "spend":
            plan = action["plan"]
            assert type(plan) is dict
            subplans = plan["subplans"]
            assert type(subplans) is list and len(subplans) == 1 and type(subplans[0]) is dict
            maximum_spend = subplans[0]["max_spend"]
            assert type(maximum_spend) is dict
            maximum_spend["minor_units"] = 1
        elif mutation == "account":
            account = action["account_binding"]
            assert type(account) is dict
            account["account_client_ref"] = "act_999999"
        else:
            challenge["expires_at"] = "2030-01-01T00:10:01Z"
        with pytest.raises(AdhelmError) as rejected:
            decode_meta_campaign_pause_approval_bridge_request_v2(
                changed_wire(challenge),
                configuration=_configuration(case),
                expected_metadata_digest=_metadata_digest(case),
            )
        assert rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", True),
        ("native_observed_at", "2030-01-01T00:00:30Z"),
        ("canonical_receipt_b64url", "AA="),
        ("active_public_key_x963_b64url", "AA"),
        ("receipt_id", "approval-receipt:bad"),
    ],
)
def test_candidate_rejects_strict_scalar_timestamp_and_base64_aliases(
    field: str, replacement: object
) -> None:
    case = _case()
    altered = dict(_candidate_data(case))
    altered[field] = replacement
    _assert_rejected(case, canonical_bytes(altered))

    canonical = str(case["candidate_canonical_json"]).encode("utf-8")
    assert b'"schema_version":2' in canonical
    _assert_rejected(case, canonical.replace(b'"schema_version":2', b'"schema_version":2.0', 1))
    _assert_rejected(case, canonical + b"\n")


def test_candidate_revalidation_reparses_canonical_bytes_and_normalizes_forged_slots() -> None:
    case = _case()
    request = _request(case)
    forged = object.__new__(NativeMetaCampaignPauseApprovalBridgeCandidateV2)
    object.__setattr__(
        forged,
        "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_bytes",
        b'{"sentinel":true}',
    )
    with pytest.raises(AdhelmError) as invalid:
        revalidate_meta_campaign_pause_approval_bridge_candidate_v2(
            forged,
            request=request,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            exchange_started_at=_time(case["exchange_started_at"]),
            current_now=_time(case["current_now"]),
        )
    assert invalid.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"
    assert "sentinel" not in invalid.value.message

    checked = _validate(case, str(case["candidate_canonical_json"]).encode("utf-8"))
    object.__setattr__(
        checked,
        "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_receipt",
        b"sentinel",
    )
    reparsed = revalidate_meta_campaign_pause_approval_bridge_candidate_v2(
        checked,
        request=request,
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        exchange_started_at=_time(case["exchange_started_at"]),
        current_now=_time(case["current_now"]),
    )
    assert reparsed.receipt_digest == checked.receipt_digest
    assert "sentinel" not in repr(reparsed)


def test_codec_import_surface_has_no_runtime_or_authority_dependencies() -> None:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden_prefixes = (
        "adhelm.ledger",
        "adhelm.safety",
        "adhelm._installed_native_approval_bridge",
        "adhelm._native_approval_bridge_transport",
        "subprocess",
        "os",
        "pathlib",
    )
    assert not any(
        name == forbidden or name.startswith(forbidden + ".")
        for name in imported
        for forbidden in forbidden_prefixes
    )
    assert {
        "adhelm.approval_protocol",
        "adhelm.canonical",
        "adhelm.errors",
    }.issubset(imported)

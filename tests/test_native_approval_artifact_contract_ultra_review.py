"""Independent adversarial review of M30 build-only native metadata selectors."""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@cache
def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/native_approval_artifact_contract.py"
    spec = importlib.util.spec_from_file_location("native_artifact_contract_ultra_target", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _raw(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:review-fixture",
        "signing_key_ref": "key:receipt-fixture",
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
    value.update(overrides)
    return value


def _contract(**overrides: object) -> Any:
    return _module().parse_native_approval_artifact_contract(_raw(**overrides))


@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("maximum_challenge_bytes", False),
        ("maximum_challenge_bytes", 1.0),
        ("maximum_challenge_lifetime_seconds", True),
        ("maximum_challenge_lifetime_seconds", 300.0),
        ("maximum_issued_at_skew_seconds", False),
        ("maximum_issued_at_skew_seconds", 0.0),
        ("maximum_subplans", True),
        ("maximum_subplans", 6.0),
        ("maximum_policy_items", False),
        ("maximum_policy_items", 64.0),
    ],
)
def test_wire_parser_preserves_original_scalar_types_before_metadata_rendering(
    field: str,
    alias: object,
) -> None:
    raw = _raw(**{field: alias})
    with pytest.raises(_module().NativeApprovalArtifactContractError):
        _module().parse_native_approval_artifact_contract_json(json.dumps(raw).encode())


def test_wire_parser_rejects_duplicate_keys_nonfinite_constants_and_dict_subclasses() -> None:
    loaded = _module()
    valid = json.dumps(_raw(), separators=(",", ":"))
    duplicate = valid[:-1] + ',"maximum_subplans":6}'
    nonfinite = valid.replace('"maximum_subplans":6', '"maximum_subplans":NaN')
    for raw in (duplicate.encode(), nonfinite.encode(), b'{"maximum_subplans":Infinity}'):
        with pytest.raises(loaded.NativeApprovalArtifactContractError):
            loaded.parse_native_approval_artifact_contract_json(raw)

    class MappingSubclass(dict[str, object]):
        pass

    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        loaded.parse_native_approval_artifact_contract(MappingSubclass(_raw()))


def test_renderer_escapes_host_reference_interpolation_and_cannot_emit_extra_swift_syntax() -> None:
    loaded = _module()
    sentinel = 'host:\\(fatalError("private-m30-source-injection"))'
    contract = _contract(host_ref=sentinel, signing_key_ref='key:"\\(print("private"))')
    source = loaded.render_generated_approval_host_build_metadata(contract)

    assert source == loaded.render_generated_approval_host_build_metadata(contract)
    assert sentinel not in source
    assert "fatalError" not in source
    assert "private-m30-source-injection" not in source
    assert "\\(" not in source
    assert "\\u{5c}" in source
    assert "\\u{22}" in source
    assert source.count("package enum GeneratedApprovalHostBuildMetadata") == 1
    assert "import Foundation" in source
    for forbidden in ("import AppKit", "ProcessInfo", "CommandLine", "NSXPC", "@main"):
        assert forbidden not in source


def test_renderer_is_order_independent_and_fixed_to_closed_enum_and_byte_tokens() -> None:
    loaded = _module()
    forward = _raw(key_storage="SECURE_ENCLAVE_P256", signing_key_access_group=None)
    reversed_raw = {key: forward[key] for key in reversed(tuple(forward))}
    first = loaded.render_generated_approval_host_build_metadata(
        loaded.parse_native_approval_artifact_contract(forward)
    )
    second = loaded.render_generated_approval_host_build_metadata(
        loaded.parse_native_approval_artifact_contract(reversed_raw)
    )

    assert first == second
    assert "keyStorage: .secureEnclaveP256" in first
    assert "signingKeyAccessGroup: nil" in first
    assert "Data([0x00, 0x61, 0x62, 0x63, 0xff])" in first


def test_public_contract_and_descriptor_serializers_normalize_missing_or_forged_objects() -> None:
    loaded = _module()
    malformed_contract = object.__new__(loaded.NativeApprovalHostArtifactContractV1)
    malformed_descriptor = object.__new__(loaded.NativeApprovalHostArtifactDescriptorV1)

    for value, members in (
        (
            malformed_contract,
            ("to_data", "protocol_configuration", "protocol_configuration_digest"),
        ),
        (
            malformed_descriptor,
            (
                "to_data",
                "metadata_canonical_bytes",
                "protocol_configuration",
                "expected_host_code_signing_requirement",
                "canonical_bytes",
                "digest",
            ),
        ),
    ):
        for member in members:
            with pytest.raises(loaded.NativeApprovalArtifactContractError) as raised:
                result = getattr(value, member)
                if callable(result):
                    result()
            assert "attribute" not in str(raised.value).lower()
            assert "schema_version" not in str(raised.value)

    forged = object.__new__(loaded.NativeApprovalHostArtifactContractV1)
    for key, value in _raw(maximum_challenge_bytes=True).items():
        object.__setattr__(forged, key, value)
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        forged.to_data()
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        loaded.native_approval_artifact_descriptor(forged)


def test_descriptor_binds_metadata_and_protocol_surfaces_without_overclaiming_installation() -> (
    None
):
    loaded = _module()
    base = loaded.native_approval_artifact_descriptor(_contract())
    changed_host = loaded.native_approval_artifact_descriptor(
        _contract(host_ref="host:review-fixture-v2")
    )
    changed_client = loaded.native_approval_artifact_descriptor(
        _contract(client_team_identifier="FGHIJ67890")
    )
    changed_provenance = loaded.native_approval_artifact_descriptor(
        _contract(provenance_account="approval-host-v2")
    )

    assert changed_host.metadata_digest != base.metadata_digest
    assert changed_host.protocol_configuration_digest != base.protocol_configuration_digest
    assert changed_client.metadata_digest != base.metadata_digest
    assert changed_client.protocol_configuration_digest == base.protocol_configuration_digest
    assert (
        changed_client.expected_client_code_signing_requirement
        != base.expected_client_code_signing_requirement
    )
    assert changed_provenance.metadata_digest != base.metadata_digest
    assert changed_provenance.protocol_configuration_digest == base.protocol_configuration_digest
    assert (
        changed_provenance.to_data()["scope"] == "UNSIGNED_BUILD_METADATA_ONLY_NOT_OS_ATTESTATION"
    )
    assert changed_provenance.to_data()["artifact_status"] == "UNSIGNED_UNVERIFIED"

"""Closed build-only selectors must not become runtime native configuration."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes


@cache
def module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/native_approval_artifact_contract.py"
    spec = importlib.util.spec_from_file_location("native_approval_artifact_contract", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _raw(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": 'host:review-"é😀',
        "signing_key_ref": "key:receipt-\\東京",
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
    return module().parse_native_approval_artifact_contract(_raw(**overrides))


def test_contract_rebuilds_exact_protocol_configuration_and_descriptor() -> None:
    loaded = module()
    contract = _contract()
    expected_protocol = ApprovalHostConfiguration(
        host_ref='host:review-"é😀',
        signing_key_ref="key:receipt-\\東京",
    )

    assert contract.to_data() == _raw()
    assert contract.signing_key_application_tag == b"\x00abc\xff"
    assert contract.protocol_configuration == expected_protocol.to_data()
    assert contract.protocol_configuration_canonical_bytes == expected_protocol.canonical_bytes
    assert contract.protocol_configuration_digest == expected_protocol.configuration_digest
    assert loaded.canonical_native_approval_artifact_contract(contract) == canonical_bytes(_raw())

    descriptor = loaded.native_approval_artifact_descriptor(contract)
    expected_metadata_digest = __import__("hashlib").sha256(canonical_bytes(_raw())).hexdigest()
    assert descriptor.metadata_digest == expected_metadata_digest
    assert descriptor.protocol_configuration_digest == expected_protocol.configuration_digest
    assert descriptor.to_data()["scope"] == "UNSIGNED_BUILD_METADATA_ONLY_NOT_OS_ATTESTATION"
    assert descriptor.to_data()["artifact_status"] == "UNSIGNED_UNVERIFIED"
    assert descriptor.to_data()["metadata"] == _raw()
    assert descriptor.canonical_bytes == canonical_bytes(descriptor.to_data())
    assert len(descriptor.digest) == 64
    assert descriptor.expected_client_code_signing_requirement == (
        'anchor apple generic and identifier "com.aptsonic.adhelm.client"'
        ' and certificate leaf[subject.OU] = "ABCDE12345"'
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("maximum_challenge_bytes", True),
        ("maximum_challenge_bytes", 1.0),
        ("maximum_challenge_lifetime_seconds", 300.0),
        ("maximum_issued_at_skew_seconds", False),
        ("maximum_subplans", 6.0),
        ("maximum_policy_items", True),
        ("maximum_challenge_bytes", 65_537),
        ("maximum_challenge_lifetime_seconds", 0),
        ("maximum_issued_at_skew_seconds", 301),
        ("maximum_subplans", 0),
        ("maximum_policy_items", 65),
        ("signing_key_access_group", False),
        ("key_storage", "OTHER"),
    ],
)
def test_contract_rejects_original_scalar_aliases_and_bounds(field: str, value: object) -> None:
    with pytest.raises(module().NativeApprovalArtifactContractError):
        _contract(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_bundle_identifier": "com.example.*"},
        {"client_bundle_identifier": "com.example.placeholder.client"},
        {"host_team_identifier": "abcde12345"},
        {"mach_service_name": "com.example"},
        {"provenance_account": "TODO"},
        {"provenance_service": "com.example.${untrusted}"},
        {"signing_key_application_tag_hex": "0"},
        {"signing_key_application_tag_hex": "AA"},
        {"signing_key_application_tag_hex": ""},
        {"signing_key_access_group": "FGHIJ67890.com.aptsonic.adhelm.host"},
        {"client_bundle_identifier": "com.aptsonic.adhelm.host"},
    ],
)
def test_contract_rejects_selector_and_tag_cross_binding(overrides: dict[str, object]) -> None:
    with pytest.raises(module().NativeApprovalArtifactContractError):
        _contract(**overrides)


def test_json_parser_rejects_duplicates_non_utf8_and_oversized_input() -> None:
    loaded = module()
    duplicate = b'{"schema_version":1,"schema_version":1}'
    for raw in (duplicate, b"\xff", b"{" + b"x" * 16_384 + b"}"):
        with pytest.raises(loaded.NativeApprovalArtifactContractError):
            loaded.parse_native_approval_artifact_contract_json(raw)

    raw = json.dumps(_raw()).encode()
    assert loaded.parse_native_approval_artifact_contract_json(raw).to_data() == _raw()


def test_mapping_parser_closes_keys_and_rebuilds_direct_or_forged_objects() -> None:
    loaded = module()
    missing = _raw()
    del missing["key_storage"]
    extra = _raw(extra="unexpected")
    for raw in (missing, extra, [("schema_version", 1)]):
        with pytest.raises(loaded.NativeApprovalArtifactContractError):
            loaded.parse_native_approval_artifact_contract(raw)

    contract = _contract()
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        replace(contract, host_team_identifier="invalid")

    forged = object.__new__(loaded.NativeApprovalHostArtifactContractV1)
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        forged.to_data()
    for key, value in _raw(maximum_challenge_bytes=True).items():
        object.__setattr__(forged, key, value)
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        loaded.canonical_native_approval_artifact_contract(forged)
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        loaded.native_approval_artifact_descriptor(forged).to_data()


def test_renderer_uses_only_scalar_escapes_and_fixed_swift_tokens() -> None:
    loaded = module()
    source = loaded.render_generated_approval_host_build_metadata(_contract())

    assert "package enum GeneratedApprovalHostBuildMetadata" in source
    assert "package static func make() throws -> ApprovalHostBuildMetadataV1" in source
    assert "role: .client" in source and "role: .host" in source
    assert "keyStorage: .keychainP256" in source
    assert "Data([0x00, 0x61, 0x62, 0x63, 0xff])" in source
    assert 'host:review-"é😀' not in source
    assert "\\u{1f600}" in source
    assert "\\u{22}" in source and "\\u{5c}" in source

    injected = _contract(host_ref='host:\\(fatalError("injected"))')
    injected_source = loaded.render_generated_approval_host_build_metadata(injected)
    assert "fatalError" not in injected_source
    assert "\\(" not in injected_source
    assert "\\u{5c}" in injected_source


def test_descriptor_rebuilds_every_field_and_rejects_direct_descriptor_bypass() -> None:
    loaded = module()
    original = loaded.native_approval_artifact_descriptor(_contract())
    changed = loaded.native_approval_artifact_descriptor(
        _contract(provenance_account="approval-host-v2")
    )
    assert changed.metadata_digest != original.metadata_digest
    assert changed.digest != original.digest
    assert changed.protocol_configuration_digest == original.protocol_configuration_digest

    forged = object.__new__(loaded.NativeApprovalHostArtifactDescriptorV1)
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        forged.to_data()
    object.__setattr__(forged, "contract", object())
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        forged.to_data()

"""Static generated binding facts must remain closed and unconfigured by default."""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import adhelm._generated_installed_approval_bridge_binding as default_binding
import adhelm._installed_native_approval_bridge as bridge
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.errors import AdhelmError

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _ROOT / "scripts/native_approval_artifact_contract.py"
_DEFAULT_BINDING_PATH = _ROOT / "src/adhelm/_generated_installed_approval_bridge_binding.py"


@cache
def _contract_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generated_binding_contract_target", _CONTRACT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _raw(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:generated-approval-v1",
        "signing_key_ref": "key:generated-approval-v1",
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
    return _contract_module().parse_native_approval_artifact_contract(_raw(**overrides))


def _generated_module(source: str) -> ModuleType:
    module = types.ModuleType("generated_binding_test_module")
    exec(compile(source, "<generated-binding>", "exec"), module.__dict__)
    return module


def _generated_binding(
    contract: object,
) -> tuple[bridge._CompiledInstalledApprovalBridgeBinding, ModuleType]:
    source = _contract_module().render_generated_installed_approval_bridge_binding(contract)
    generated = _generated_module(source)
    resolver = generated.__dict__["_resolve_generated_installed_approval_bridge_binding"]
    assert callable(resolver)
    binding = resolver()
    assert type(binding) is bridge._CompiledInstalledApprovalBridgeBinding
    return binding, generated


def test_checked_in_generated_binding_is_explicitly_unconfigured_without_a_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert default_binding._resolve_generated_installed_approval_bridge_binding() is None
    source = _DEFAULT_BINDING_PATH.read_text(encoding="utf-8")
    assert "_issue_compiled_installed_approval_bridge_binding" not in source
    parsed = ast.parse(source)
    assert not [node for node in ast.walk(parsed) if type(node) is ast.Import]
    assert [node.module for node in ast.walk(parsed) if type(node) is ast.ImportFrom] == [
        "__future__"
    ]
    monkeypatch.setattr(
        bridge,
        "_issue_compiled_installed_approval_bridge_binding_from_generated_metadata",
        lambda **_kwargs: pytest.fail("the unconfigured stub must not issue a binding"),
    )

    with pytest.raises(AdhelmError) as raised:
        bridge._resolve_compiled_installed_binding()
    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_renderer_emits_static_18_field_binding_with_matching_digests() -> None:
    contract = _contract()
    rendered = _contract_module().render_generated_installed_approval_bridge_binding(contract)
    binding, generated = _generated_binding(contract)
    configuration, metadata_digest = bridge._binding_facts(binding)

    assert rendered == _contract_module().render_generated_installed_approval_bridge_binding(
        contract
    )
    assert "_METADATA_FIELDS: Final" in rendered
    assert "_issue_compiled_installed_approval_bridge_binding_from_generated_metadata" in rendered
    assert "PinnedApprovalTrust" not in rendered
    assert "approval_acceptance" not in rendered
    assert "subprocess" not in rendered
    assert "os." not in rendered
    assert "open(" not in rendered
    assert configuration.to_data() == contract.protocol_configuration
    assert configuration.configuration_digest == contract.protocol_configuration_digest
    assert (
        metadata_digest
        == _contract_module().native_approval_artifact_descriptor(contract).metadata_digest
    )
    assert generated.__dict__["_METADATA_DIGEST"] == metadata_digest
    assert (
        generated.__dict__["_PROTOCOL_CONFIGURATION_DIGEST"] == configuration.configuration_digest
    )
    assert repr(binding) == "<compiled installed approval bridge binding>"


def test_generated_factory_rebuilds_exact_tuple_and_rejects_forged_scalar_aliases() -> None:
    contract = _contract()
    binding, generated = _generated_binding(contract)
    assert type(binding) is bridge._CompiledInstalledApprovalBridgeBinding
    fields = generated.__dict__["_METADATA_FIELDS"]
    metadata_digest = generated.__dict__["_METADATA_DIGEST"]
    protocol_digest = generated.__dict__["_PROTOCOL_CONFIGURATION_DIGEST"]
    assert type(fields) is tuple

    cases: tuple[object, ...] = (
        fields[:-1] + (("maximum_policy_items", True),),
        fields[:1] + (("schema_version", fields[1][1]),) + fields[2:],
        fields[:-1] + (("unexpected", 64),),
        fields[:-1] + (("maximum_policy_items", 64.0),),
        fields[:-1] + (("maximum_policy_items", 64),) + (("extra", 1),),
    )
    for malformed in cases:
        with pytest.raises(AdhelmError) as raised:
            bridge._issue_compiled_installed_approval_bridge_binding_from_generated_metadata(
                metadata_fields=malformed,
                expected_metadata_digest=metadata_digest,
                expected_protocol_configuration_digest=protocol_digest,
            )
        assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_full_metadata_and_protocol_digests_must_match_their_static_tuple() -> None:
    original = _contract()
    changed_metadata = _contract(provenance_account="approval-host-v2")
    original_binding, original_generated = _generated_binding(original)
    changed_binding, changed_generated = _generated_binding(changed_metadata)
    original_configuration, original_metadata_digest = bridge._binding_facts(original_binding)
    changed_configuration, changed_metadata_digest = bridge._binding_facts(changed_binding)

    assert changed_metadata_digest != original_metadata_digest
    assert changed_configuration.configuration_digest == original_configuration.configuration_digest
    with pytest.raises(AdhelmError) as metadata_mismatch:
        bridge._issue_compiled_installed_approval_bridge_binding_from_generated_metadata(
            metadata_fields=changed_generated.__dict__["_METADATA_FIELDS"],
            expected_metadata_digest=original_generated.__dict__["_METADATA_DIGEST"],
            expected_protocol_configuration_digest=changed_generated.__dict__[
                "_PROTOCOL_CONFIGURATION_DIGEST"
            ],
        )
    assert metadata_mismatch.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"

    changed_limits = _contract(maximum_policy_items=63)
    _changed_limit_binding, changed_limit_generated = _generated_binding(changed_limits)
    with pytest.raises(AdhelmError) as protocol_mismatch:
        bridge._issue_compiled_installed_approval_bridge_binding_from_generated_metadata(
            metadata_fields=changed_limit_generated.__dict__["_METADATA_FIELDS"],
            expected_metadata_digest=changed_limit_generated.__dict__["_METADATA_DIGEST"],
            expected_protocol_configuration_digest=original_generated.__dict__[
                "_PROTOCOL_CONFIGURATION_DIGEST"
            ],
        )
    assert protocol_mismatch.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_binding_rechecks_the_retained_protocol_digest_after_low_level_mutation() -> None:
    binding, _generated = _generated_binding(_contract())
    configuration, _metadata_digest = bridge._binding_facts(binding)
    object.__setattr__(
        binding,
        "_CompiledInstalledApprovalBridgeBinding__configuration",
        ApprovalHostConfiguration(
            host_ref=configuration.host_ref,
            signing_key_ref=configuration.signing_key_ref,
            maximum_policy_items=configuration.maximum_policy_items - 1,
        ),
    )

    with pytest.raises(AdhelmError) as raised:
        bridge._binding_facts(binding)
    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_binding_rechecks_the_retained_full_metadata_digest_after_low_level_mutation() -> None:
    binding, _generated = _generated_binding(_contract())
    object.__setattr__(
        binding,
        "_CompiledInstalledApprovalBridgeBinding__metadata_digest",
        "0" * 64,
    )

    with pytest.raises(AdhelmError) as raised:
        bridge._binding_facts(binding)
    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_m34_resolver_lazily_imports_a_generated_module_after_factory_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _contract_module().render_generated_installed_approval_bridge_binding(_contract())
    generated = types.ModuleType("adhelm._generated_installed_approval_bridge_binding")
    exec(compile(source, "<generated-binding>", "exec"), generated.__dict__)
    monkeypatch.setitem(
        sys.modules, "adhelm._generated_installed_approval_bridge_binding", generated
    )

    binding = bridge._resolve_compiled_installed_binding()

    configuration, metadata_digest = bridge._binding_facts(binding)
    assert (
        configuration.configuration_digest == generated.__dict__["_PROTOCOL_CONFIGURATION_DIGEST"]
    )
    assert metadata_digest == generated.__dict__["_METADATA_DIGEST"]


def test_m34_resolver_sanitizes_a_generated_module_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = types.ModuleType("adhelm._generated_installed_approval_bridge_binding")

    def invalid_resolver() -> object:
        raise AdhelmError("PRIVATE_GENERATED_BINDING_FAILURE", "private", exit_code=2)

    generated.__dict__["_resolve_generated_installed_approval_bridge_binding"] = invalid_resolver
    monkeypatch.setitem(
        sys.modules, "adhelm._generated_installed_approval_bridge_binding", generated
    )

    with pytest.raises(AdhelmError) as raised:
        bridge._resolve_compiled_installed_binding()
    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_renderer_rejects_forged_contract_and_never_emits_runtime_syntax() -> None:
    loaded = _contract_module()
    forged = object.__new__(loaded.NativeApprovalHostArtifactContractV1)
    with pytest.raises(loaded.NativeApprovalArtifactContractError):
        loaded.render_generated_installed_approval_bridge_binding(forged)

    rendered = loaded.render_generated_installed_approval_bridge_binding(
        _contract(host_ref='host:\\"; import os; os.system("bad") #')
    )
    parsed = ast.parse(rendered)
    assert not [node for node in ast.walk(parsed) if type(node) is ast.Import]
    assert [node.module for node in ast.walk(parsed) if type(node) is ast.ImportFrom] == [
        "__future__",
        "typing",
        "adhelm._installed_native_approval_bridge",
    ]

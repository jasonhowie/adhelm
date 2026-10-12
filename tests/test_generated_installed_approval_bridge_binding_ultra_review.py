"""Independent adversarial coverage for generated installed-bridge bindings."""

from __future__ import annotations

import importlib.util
import sys
import types
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

import adhelm._installed_native_approval_bridge as bridge
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.errors import AdhelmError


@cache
def _contract_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/native_approval_artifact_contract.py"
    spec = importlib.util.spec_from_file_location("generated_binding_ultra_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _raw(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:generated-ultra-v1",
        "signing_key_ref": "key:generated-ultra-v1",
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


def _issued_binding(**overrides: object) -> object:
    module = _contract_module()
    contract = module.parse_native_approval_artifact_contract(_raw(**overrides))
    source = module.render_generated_installed_approval_bridge_binding(contract)
    generated = types.ModuleType("generated_binding_ultra_fixture")
    exec(compile(source, "<generated-binding-ultra>", "exec"), generated.__dict__)
    resolver = generated.__dict__["_resolve_generated_installed_approval_bridge_binding"]
    assert callable(resolver)
    return resolver()


def test_mutated_issued_binding_cannot_rebind_protocol_facts() -> None:
    """A bypassed slots object must not decouple config from generated protocol facts."""
    binding = _issued_binding()
    assert type(binding) is bridge._CompiledInstalledApprovalBridgeBinding
    original, _metadata_digest = bridge._binding_facts(binding)
    forged = ApprovalHostConfiguration(
        host_ref="host:forged-generated-ultra-v1",
        signing_key_ref=original.signing_key_ref,
        maximum_challenge_bytes=original.maximum_challenge_bytes,
        maximum_challenge_lifetime_seconds=original.maximum_challenge_lifetime_seconds,
        maximum_issued_at_skew_seconds=original.maximum_issued_at_skew_seconds,
        maximum_subplans=original.maximum_subplans,
        maximum_policy_items=original.maximum_policy_items,
    )
    object.__setattr__(
        binding,
        "_CompiledInstalledApprovalBridgeBinding__configuration",
        forged,
    )

    with pytest.raises(AdhelmError) as raised:
        bridge._binding_facts(binding)

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"

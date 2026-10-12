"""Build-only Meta renderers must bind identical metadata without runtime loaders."""

from __future__ import annotations

import ast
import re

import pytest
from test_generated_installed_approval_bridge_binding import (
    _contract,
    _contract_module,
    _generated_module,
)

import adhelm._compiled_meta_campaign_pause_approval_bridge_v2 as bridge
from adhelm.errors import AdhelmError


@pytest.mark.parametrize("access_group", [None, "ABCDE12345.com.aptsonic.adhelm.host"])
def test_meta_renderers_bind_identical_full_metadata_and_protocol(access_group: str | None) -> None:
    contract = _contract(signing_key_access_group=access_group)
    module = _contract_module()
    python = module.render_generated_meta_campaign_pause_approval_bridge_binding_v2(contract)
    swift = module.render_generated_meta_campaign_pause_approval_route_binding_v2(contract)
    decoded_swift = re.sub(r"\\u\{([0-9a-f]+)\}", lambda match: chr(int(match.group(1), 16)), swift)
    generated = _generated_module(python)
    binding = generated._resolve_generated_meta_campaign_pause_approval_bridge_binding_v2()
    configuration, metadata_digest = (
        bridge._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(binding)
    )

    assert type(binding) is bridge._CompiledMetaCampaignPauseApprovalBridgeBindingV2
    assert generated._METADATA_FIELDS == tuple(contract.to_data().items())
    assert len(generated._METADATA_FIELDS) == 18
    assert configuration.to_data() == contract.protocol_configuration
    assert metadata_digest == module.native_approval_artifact_descriptor(contract).metadata_digest
    assert generated._METADATA_DIGEST == metadata_digest
    assert generated._PROTOCOL_CONFIGURATION_DIGEST == configuration.configuration_digest
    assert f'expectedMetadataDigest: "{metadata_digest}"' in decoded_swift
    assert (
        f'expectedProtocolConfigurationDigest: "{configuration.configuration_digest}"'
        in decoded_swift
    )
    assert python == module.render_generated_meta_campaign_pause_approval_bridge_binding_v2(
        contract
    )
    assert swift == module.render_generated_meta_campaign_pause_approval_route_binding_v2(contract)
    assert "try NativeMetaCampaignPauseApprovalRouteBindingV2(" in swift
    assert "try?" not in swift and "return nil" not in swift
    tree = ast.parse(python)
    assert not any(type(node) is ast.Import for node in ast.walk(tree))
    assert [node.module for node in ast.walk(tree) if type(node) is ast.ImportFrom] == [
        "__future__",
        "typing",
        "adhelm._compiled_meta_campaign_pause_approval_bridge_v2",
    ]
    calls = [node for node in ast.walk(tree) if type(node) is ast.Call]
    assert len(calls) == 1 and type(calls[0].func) is ast.Name
    assert calls[0].func.id == (
        "_issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_from_generated_metadata"
    )


@pytest.mark.parametrize(
    "renderer",
    [
        "render_generated_meta_campaign_pause_approval_bridge_binding_v2",
        "render_generated_meta_campaign_pause_approval_route_binding_v2",
    ],
)
def test_meta_renderer_rejects_malformed_contract_before_rendering(renderer: str) -> None:
    module = _contract_module()
    contract = _contract()
    object.__setattr__(contract, "maximum_policy_items", True)
    with pytest.raises(module.NativeApprovalArtifactContractError):
        getattr(module, renderer)(contract)
    with pytest.raises(module.NativeApprovalArtifactContractError):
        getattr(module, renderer)({})


def test_nonprotocol_metadata_change_changes_both_renderers_and_rejects_mixed_facts() -> None:
    module = _contract_module()
    original = _contract()
    changed = _contract(provenance_account="another-static-account")
    assert original.protocol_configuration_digest == changed.protocol_configuration_digest
    original_python = module.render_generated_meta_campaign_pause_approval_bridge_binding_v2(
        original
    )
    changed_python = module.render_generated_meta_campaign_pause_approval_bridge_binding_v2(changed)
    original_swift = module.render_generated_meta_campaign_pause_approval_route_binding_v2(original)
    changed_swift = module.render_generated_meta_campaign_pause_approval_route_binding_v2(changed)
    assert original_python != changed_python and original_swift != changed_swift
    old = _generated_module(original_python)
    generated = _generated_module(changed_python)
    generated._METADATA_DIGEST = old._METADATA_DIGEST
    with pytest.raises(AdhelmError) as raised:
        generated._resolve_generated_meta_campaign_pause_approval_bridge_binding_v2()
    assert raised.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"

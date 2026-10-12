"""Independent fake-only checks for the compiled Meta approval route V2."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import types
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

import adhelm._compiled_meta_campaign_pause_approval_bridge_v2 as compiled
import adhelm._installed_native_approval_bridge as v1_bridge
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.safety import SafetyKernel

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _ROOT / "scripts/native_approval_artifact_contract.py"
_DEFAULT_GENERATED_PATH = (
    _ROOT / "src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
)
_GENERATED_MODULE_NAME = "adhelm._generated_meta_campaign_pause_approval_bridge_binding_v2"
_METADATA_FIELD_ORDER = (
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
)


@cache
def _contract_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m49_ultra_contract", _CONTRACT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:generated-meta-ultra-v2",
        "signing_key_ref": "key:generated-meta-ultra-v2",
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
    assert tuple(value) == _METADATA_FIELD_ORDER
    return value


def _configuration(value: dict[str, object]) -> ApprovalHostConfiguration:
    return ApprovalHostConfiguration(
        host_ref=cast(str, value["host_ref"]),
        signing_key_ref=cast(str, value["signing_key_ref"]),
        maximum_challenge_bytes=cast(int, value["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=cast(int, value["maximum_challenge_lifetime_seconds"]),
        maximum_issued_at_skew_seconds=cast(int, value["maximum_issued_at_skew_seconds"]),
        maximum_subplans=cast(int, value["maximum_subplans"]),
        maximum_policy_items=cast(int, value["maximum_policy_items"]),
    )


def _fields(value: dict[str, object]) -> tuple[tuple[str, object], ...]:
    return tuple((field, value[field]) for field in _METADATA_FIELD_ORDER)


def _rendered_generated_module(value: dict[str, object] | None = None) -> ModuleType:
    raw = _metadata() if value is None else value
    contract = _contract_module().parse_native_approval_artifact_contract(raw)
    source = _contract_module().render_generated_meta_campaign_pause_approval_bridge_binding_v2(
        contract
    )
    generated = types.ModuleType(_GENERATED_MODULE_NAME)
    exec(compile(source, "<m49-ultra-generated-binding>", "exec"), generated.__dict__)
    return generated


def _rendered_binding(value: dict[str, object] | None = None) -> object:
    generated = _rendered_generated_module(value)
    resolver = generated.__dict__[
        "_resolve_generated_meta_campaign_pause_approval_bridge_binding_v2"
    ]
    assert callable(resolver)
    return resolver()


def _assert_not_installed(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    assert error.value.exit_code is ExitCode.PRECONDITION


def test_renderer_generated_meta_route_is_static_deterministic_and_v2_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured artifact emits static V2 facts without a V1/runtime fallback."""
    raw = _metadata()
    contract = _contract_module().parse_native_approval_artifact_contract(raw)
    renderer = _contract_module().render_generated_meta_campaign_pause_approval_bridge_binding_v2
    source = renderer(contract)
    assert source == renderer(contract)
    parsed = ast.parse(source)
    assert not [node for node in ast.walk(parsed) if type(node) is ast.Import]
    assert [node.module for node in ast.walk(parsed) if type(node) is ast.ImportFrom] == [
        "__future__",
        "typing",
        "adhelm._compiled_meta_campaign_pause_approval_bridge_v2",
    ]
    assert "_issue_compiled_installed_approval_bridge_binding" not in source
    assert "_installed_native_approval_bridge" not in source
    assert all(token not in source for token in ("open(", "os.", "subprocess", "environ"))

    generated = _rendered_generated_module(raw)
    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, generated)
    binding = compiled._resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2()
    configuration, metadata_digest = (
        compiled._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(binding)
    )

    assert configuration.to_data() == _configuration(raw).to_data()
    assert configuration.configuration_digest == _configuration(raw).configuration_digest
    assert metadata_digest == hashlib.sha256(canonical_bytes(raw)).hexdigest()
    assert type(binding) is compiled._CompiledMetaCampaignPauseApprovalBridgeBindingV2


def test_v1_generated_binding_is_refused_before_meta_source_or_helper_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact shared metadata cannot turn the V1 binding route into Meta readiness."""
    raw = _metadata()
    issue_v1_binding = (
        v1_bridge._issue_compiled_installed_approval_bridge_binding_from_generated_metadata
    )
    v1_binding = issue_v1_binding(
        metadata_fields=_fields(raw),
        expected_metadata_digest=hashlib.sha256(canonical_bytes(raw)).hexdigest(),
        expected_protocol_configuration_digest=_configuration(raw).configuration_digest,
    )
    generated = types.ModuleType(_GENERATED_MODULE_NAME)
    generated.__dict__["_resolve_generated_meta_campaign_pause_approval_bridge_binding_v2"] = (
        lambda: v1_binding
    )
    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, generated)
    monkeypatch.setattr(
        compiled,
        "_prepare_installed_meta_campaign_pause_approval_bridge_v2",
        lambda *_args, **_kwargs: pytest.fail("V1 binding must fail before source work"),
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pytest.fail("V1 binding must fail before helper construction"),
    )

    with pytest.raises(AdhelmError) as rejected:
        compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
            cast(SafetyKernel, object()),
            cast(Ledger, object()),
            source_plan_record_digest="not-reached",
        )

    _assert_not_installed(rejected)


def test_malformed_v2_binding_object_is_refused_before_meta_source_or_helper_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A type-shaped generated result with absent slots cannot become route readiness."""
    malformed = object.__new__(compiled._CompiledMetaCampaignPauseApprovalBridgeBindingV2)
    generated = types.ModuleType(_GENERATED_MODULE_NAME)
    generated.__dict__["_resolve_generated_meta_campaign_pause_approval_bridge_binding_v2"] = (
        lambda: malformed
    )
    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, generated)
    monkeypatch.setattr(
        compiled,
        "_prepare_installed_meta_campaign_pause_approval_bridge_v2",
        lambda *_args, **_kwargs: pytest.fail("malformed binding must fail before source work"),
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pytest.fail("malformed binding must fail before helper construction"),
    )

    with pytest.raises(AdhelmError) as rejected:
        compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
            cast(SafetyKernel, object()),
            cast(Ledger, object()),
            source_plan_record_digest="not-reached",
        )

    _assert_not_installed(rejected)


def test_nonprotocol_metadata_mutation_is_rejected_on_use_before_pending_or_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full 18-field digest remains required even when protocol seven still match."""
    raw = _metadata()
    binding = _rendered_binding(raw)
    assert type(binding) is compiled._CompiledMetaCampaignPauseApprovalBridgeBindingV2
    replacement = _metadata(provenance_account="approval-host-v2")
    assert (
        _configuration(replacement).configuration_digest == _configuration(raw).configuration_digest
    )
    object.__setattr__(
        binding,
        "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_fields",
        _fields(replacement),
    )
    generated = types.ModuleType(_GENERATED_MODULE_NAME)
    generated.__dict__["_resolve_generated_meta_campaign_pause_approval_bridge_binding_v2"] = (
        lambda: binding
    )
    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, generated)
    monkeypatch.setattr(
        compiled,
        "_prepare_installed_meta_campaign_pause_approval_bridge_v2",
        lambda *_args, **_kwargs: pytest.fail("incoherent metadata must fail before pending"),
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pytest.fail("incoherent metadata must fail before helper construction"),
    )

    with pytest.raises(AdhelmError) as rejected:
        compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
            cast(SafetyKernel, object()),
            cast(Ledger, object()),
            source_plan_record_digest="not-reached",
        )

    _assert_not_installed(rejected)


def test_checked_in_default_does_not_touch_malformed_source_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary wheel fails before it can validate or log a caller source selector."""
    monkeypatch.setattr(
        compiled,
        "_prepare_installed_meta_campaign_pause_approval_bridge_v2",
        lambda *_args, **_kwargs: pytest.fail("default must not inspect a retained source"),
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pytest.fail("default must not construct a system adapter"),
    )

    with pytest.raises(AdhelmError) as rejected:
        compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
            cast(SafetyKernel, object()),
            cast(Ledger, object()),
            source_plan_record_digest=cast(str, object()),
        )

    _assert_not_installed(rejected)

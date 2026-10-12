"""Fake-only compiled Meta campaign PAUSE approval-bridge V2 boundaries."""

from __future__ import annotations

import ast
import hashlib
import sys
import types
from pathlib import Path
from typing import cast

import pytest
import test_meta_campaign_pause_pending_exchange_v2 as pending_exchange_tests

import adhelm._compiled_meta_campaign_pause_approval_bridge_v2 as compiled
import adhelm._generated_meta_campaign_pause_approval_bridge_binding_v2 as default_binding
import adhelm._installed_meta_campaign_pause_approval_bridge_v2 as private_bridge
import adhelm._installed_native_approval_bridge as v1_bridge
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.safety import SafetyKernel

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BINDING_PATH = (
    _ROOT / "src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
)
_GENERATED_MODULE_NAME = "adhelm._generated_meta_campaign_pause_approval_bridge_binding_v2"
_ISSUE_META_BINDING = (
    compiled._issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_from_generated_metadata
)
_ISSUE_V1_BINDING = (
    v1_bridge._issue_compiled_installed_approval_bridge_binding_from_generated_metadata
)
_DEFAULT_RESOLVER = (
    default_binding._resolve_generated_meta_campaign_pause_approval_bridge_binding_v2
)
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


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:generated-meta-v2",
        "signing_key_ref": "key:generated-meta-v2",
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


def _configuration(metadata: dict[str, object]) -> ApprovalHostConfiguration:
    return ApprovalHostConfiguration(
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


def _fields(metadata: dict[str, object]) -> tuple[tuple[str, object], ...]:
    return tuple((field, metadata[field]) for field in _METADATA_FIELD_ORDER)


def _issue(
    metadata: dict[str, object] | None = None,
) -> compiled._CompiledMetaCampaignPauseApprovalBridgeBindingV2:
    raw = _metadata() if metadata is None else metadata
    return _ISSUE_META_BINDING(
        metadata_fields=_fields(raw),
        expected_metadata_digest=hashlib.sha256(canonical_bytes(raw)).hexdigest(),
        expected_protocol_configuration_digest=_configuration(raw).configuration_digest,
    )


def _generated_module(resolver: object) -> types.ModuleType:
    module = types.ModuleType(_GENERATED_MODULE_NAME)
    module.__dict__["_resolve_generated_meta_campaign_pause_approval_bridge_binding_v2"] = resolver
    return module


def test_checked_in_generated_meta_binding_is_unconfigured_without_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _DEFAULT_RESOLVER() is None
    source = _DEFAULT_BINDING_PATH.read_text(encoding="utf-8")
    assert "_issue_compiled_meta_campaign_pause" not in source
    parsed = ast.parse(source)
    assert not [node for node in ast.walk(parsed) if type(node) is ast.Import]
    assert [node.module for node in ast.walk(parsed) if type(node) is ast.ImportFrom] == [
        "__future__"
    ]
    monkeypatch.setattr(
        compiled,
        "_prepare_installed_meta_campaign_pause_approval_bridge_v2",
        lambda *_args, **_kwargs: pytest.fail("absent binding must not read retained source"),
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pytest.fail("absent binding must not construct a helper adapter"),
    )

    with pytest.raises(AdhelmError) as raised:
        compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
            cast(SafetyKernel, object()),
            cast(Ledger, object()),
            source_plan_record_digest="0" * 64,
        )

    assert raised.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    assert raised.value.exit_code is ExitCode.PRECONDITION


def test_factory_issues_meta_only_opaque_binding_and_rechecks_full_facts() -> None:
    metadata = _metadata()
    binding = _issue(metadata)

    configuration, metadata_digest = (
        compiled._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(binding)
    )

    assert type(binding) is compiled._CompiledMetaCampaignPauseApprovalBridgeBindingV2
    assert repr(binding) == "<compiled Meta campaign PAUSE approval bridge V2 binding>"
    assert metadata_digest == hashlib.sha256(canonical_bytes(metadata)).hexdigest()
    assert configuration.to_data() == _configuration(metadata).to_data()
    with pytest.raises(TypeError):
        binding.__reduce__()
    with pytest.raises(AttributeError):
        binding.anything = "forged"  # type: ignore[attr-defined]

    # The returned configuration is a detached fact, not mutable retained state.
    object.__setattr__(configuration, "maximum_policy_items", 1)
    fresh_configuration, fresh_digest = (
        compiled._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(binding)
    )
    assert fresh_configuration.maximum_policy_items == 64
    assert fresh_digest == metadata_digest


def test_factory_rejects_nonexact_full18_scalars_and_digest_mismatches() -> None:
    metadata = _metadata()
    fields = _fields(metadata)
    metadata_digest = hashlib.sha256(canonical_bytes(metadata)).hexdigest()
    protocol_digest = _configuration(metadata).configuration_digest
    malformed: tuple[object, ...] = (
        fields[:-1] + (("maximum_policy_items", True),),
        fields[:1] + (("schema_version", 1),) + fields[2:],
        fields[:-1] + (("unknown", 64),),
        fields[:-1] + (("maximum_policy_items", 64.0),),
        fields[:-1] + (("maximum_policy_items", 64),) + (("extra", 1),),
    )
    for value in malformed:
        with pytest.raises(AdhelmError) as raised:
            _ISSUE_META_BINDING(
                metadata_fields=value,
                expected_metadata_digest=metadata_digest,
                expected_protocol_configuration_digest=protocol_digest,
            )
        assert raised.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"

    changed_metadata = _metadata(provenance_account="approval-host-v2")
    with pytest.raises(AdhelmError) as changed_full_digest:
        _ISSUE_META_BINDING(
            metadata_fields=_fields(changed_metadata),
            expected_metadata_digest=metadata_digest,
            expected_protocol_configuration_digest=_configuration(
                changed_metadata
            ).configuration_digest,
        )
    assert (
        changed_full_digest.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    )

    changed_protocol = _metadata(maximum_policy_items=63)
    with pytest.raises(AdhelmError) as changed_protocol_digest:
        _ISSUE_META_BINDING(
            metadata_fields=_fields(changed_protocol),
            expected_metadata_digest=hashlib.sha256(canonical_bytes(changed_protocol)).hexdigest(),
            expected_protocol_configuration_digest=protocol_digest,
        )
    assert (
        changed_protocol_digest.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"
    )


def test_facts_revalidate_mutated_binding_and_never_accept_v1_binding() -> None:
    binding = _issue()
    object.__setattr__(
        binding,
        "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_digest",
        "0" * 64,
    )
    with pytest.raises(AdhelmError) as mutated:
        compiled._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(binding)
    assert mutated.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"

    metadata = _metadata()
    v1_binding = _ISSUE_V1_BINDING(
        metadata_fields=_fields(metadata),
        expected_metadata_digest=hashlib.sha256(canonical_bytes(metadata)).hexdigest(),
        expected_protocol_configuration_digest=_configuration(metadata).configuration_digest,
    )
    with pytest.raises(AdhelmError) as legacy:
        compiled._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(v1_binding)
    assert legacy.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"


def test_resolver_is_lazy_and_rejects_v1_or_generated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _issue()
    generated = _generated_module(lambda: binding)
    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, generated)

    resolved = compiled._resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2()
    configuration, metadata_digest = (
        compiled._compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(resolved)
    )
    assert configuration.configuration_digest == _configuration(_metadata()).configuration_digest
    assert metadata_digest == hashlib.sha256(canonical_bytes(_metadata())).hexdigest()

    metadata = _metadata()
    v1_binding = _ISSUE_V1_BINDING(
        metadata_fields=_fields(metadata),
        expected_metadata_digest=hashlib.sha256(canonical_bytes(metadata)).hexdigest(),
        expected_protocol_configuration_digest=_configuration(metadata).configuration_digest,
    )
    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, _generated_module(lambda: v1_binding))
    with pytest.raises(AdhelmError) as legacy:
        compiled._resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2()
    assert legacy.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"

    def failing_resolver() -> object:
        raise AdhelmError("UNTRUSTED_GENERATED_FAILURE", "untrusted", exit_code=ExitCode.INTERNAL)

    monkeypatch.setitem(sys.modules, _GENERATED_MODULE_NAME, _generated_module(failing_resolver))
    with pytest.raises(AdhelmError) as failed:
        compiled._resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2()
    assert failed.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"


def test_ready_check_revalidates_binding_before_any_installed_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _issue()
    monkeypatch.setattr(
        compiled,
        "_resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2",
        lambda: binding,
    )
    compiled._require_compiled_meta_campaign_pause_approval_bridge_v2_ready()

    object.__setattr__(
        binding,
        "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__protocol_configuration_digest",
        "0" * 64,
    )
    with pytest.raises(AdhelmError) as raised:
        compiled._require_compiled_meta_campaign_pause_approval_bridge_v2_ready()
    assert raised.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED"


def test_installed_entry_derives_reviewed_metadata_then_uses_only_fixed_private_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _issue()
    prepared = object()
    adapter = object()
    cancellation = object()
    accepted = cast(AcceptedNativeApproval, object())
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        compiled,
        "_resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2",
        lambda: binding,
    )

    def prepare(
        kernel: SafetyKernel,
        ledger: Ledger,
        *,
        source_plan_record_digest: str,
        reviewed_metadata: private_bridge._MetaCampaignPauseApprovalBridgeReviewedMetadataV2,
    ) -> object:
        calls["kernel"] = kernel
        calls["ledger"] = ledger
        calls["source"] = source_plan_record_digest
        calls["reviewed_metadata"] = reviewed_metadata
        configuration, metadata_digest = private_bridge._reviewed_metadata_facts(reviewed_metadata)
        assert configuration.to_data() == _configuration(_metadata()).to_data()
        assert metadata_digest == hashlib.sha256(canonical_bytes(_metadata())).hexdigest()
        return prepared

    def exchange(
        kernel: SafetyKernel,
        ledger: Ledger,
        *,
        pending: object,
        adapter: object,
        cancellation: object,
    ) -> AcceptedNativeApproval:
        calls["exchange"] = (kernel, ledger, pending, adapter, cancellation)
        return accepted

    monkeypatch.setattr(
        compiled,
        "_prepare_installed_meta_campaign_pause_approval_bridge_v2",
        prepare,
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: adapter,
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_cancellation_v2",
        lambda: cancellation,
    )
    monkeypatch.setattr(
        compiled,
        "_exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2",
        exchange,
    )

    kernel = cast(SafetyKernel, object())
    ledger = cast(Ledger, object())
    result = compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
        kernel,
        ledger,
        source_plan_record_digest="a" * 64,
    )

    assert result is accepted
    assert calls["kernel"] is kernel
    assert calls["ledger"] is ledger
    assert calls["source"] == "a" * 64
    assert calls["exchange"] == (kernel, ledger, prepared, adapter, cancellation)


def test_installed_entry_composes_a_checked_binding_with_the_sealed_fake_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise M46/M48/M47 through a fake pipe, never the system helper."""
    material = pending_exchange_tests.material.__wrapped__(tmp_path, monkeypatch)
    material.clock.now = pending_exchange_tests._T2
    metadata = _metadata(
        host_ref=material.configuration.host_ref,
        signing_key_ref=material.configuration.signing_key_ref,
    )
    binding = _issue(metadata)
    operations = pending_exchange_tests._FakePosixOperations(material)
    monkeypatch.setattr(
        compiled,
        "_resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2",
        lambda: binding,
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pending_exchange_tests._adapter(operations),
    )

    accepted = compiled._request_and_record_installed_meta_campaign_pause_approval_v2(
        material.kernel,
        material.ledger,
        source_plan_record_digest=material.record_digest,
    )

    assert accepted.direct_grant is not None
    assert len(operations.launch_specs) == 1
    assert operations.writes and operations.writes[0][1][4:]

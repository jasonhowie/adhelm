"""Private compiled Meta campaign PAUSE approval-bridge V2 binding.

The checked-in generated resolver is deliberately absent.  A separately
reviewed artifact may replace only that generated source with static metadata
facts which this module reconstructs and checks on every use.  This does not
make the existing V1 compiled binding, a retained digest, or a same-process
object into Meta route readiness.
"""

from __future__ import annotations

import hashlib
from typing import Final

from adhelm._installed_meta_campaign_pause_approval_bridge_v2 import (
    _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2,
    _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2,
    _prepare_installed_meta_campaign_pause_approval_bridge_v2,
)
from adhelm._installed_native_approval_bridge import (
    _configuration_from_generated_metadata,
    _generated_metadata_fields,
    _rebuild_generated_metadata,
    _require_generated_digest,
)
from adhelm._meta_campaign_pause_approval_bridge_transport_v2 import (
    _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2,
    _MetaCampaignPauseApprovalBridgePipeAdapterV2,
)
from adhelm._native_approval_bridge_transport import (
    _NativeApprovalBridgeCancellation,
    _SystemNativeApprovalBridgePosixOperations,
)
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.safety import SafetyKernel

_ISSUER: Final = object()


class _CompiledMetaCampaignPauseApprovalBridgeBindingV2:
    """Opaque reviewed static facts for the distinct Meta V2 route only."""

    __slots__ = (
        "__issuer",
        "__configuration",
        "__metadata_fields",
        "__metadata_digest",
        "__protocol_configuration_digest",
    )

    def __init__(
        self,
        issuer: object,
        *,
        configuration: ApprovalHostConfiguration,
        metadata_fields: tuple[tuple[str, object], ...],
        metadata_digest: str,
        protocol_configuration_digest: str,
    ) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Meta campaign PAUSE approval bridge bindings are module-issued only.")
        object.__setattr__(
            self, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__issuer", issuer
        )
        object.__setattr__(
            self,
            "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__configuration",
            configuration,
        )
        object.__setattr__(
            self,
            "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_fields",
            metadata_fields,
        )
        object.__setattr__(
            self,
            "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_digest",
            metadata_digest,
        )
        object.__setattr__(
            self,
            "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__protocol_configuration_digest",
            protocol_configuration_digest,
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Meta campaign PAUSE approval bridge bindings are immutable.")

    def __repr__(self) -> str:
        return "<compiled Meta campaign PAUSE approval bridge V2 binding>"

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("Meta campaign PAUSE approval bridge bindings are not serializable.")


def _compiled_binding_not_installed() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED",
        "A reviewed compiled Meta campaign PAUSE approval bridge V2 binding is not available.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Install a reviewed Meta campaign PAUSE approval bridge V2 artifact before "
            "requesting a new review."
        ),
    )


def _detached_configuration(value: ApprovalHostConfiguration) -> ApprovalHostConfiguration:
    """Rebuild the exact seven protocol fields without widening metadata access."""
    return ApprovalHostConfiguration(
        host_ref=value.host_ref,
        signing_key_ref=value.signing_key_ref,
        maximum_challenge_bytes=value.maximum_challenge_bytes,
        maximum_challenge_lifetime_seconds=value.maximum_challenge_lifetime_seconds,
        maximum_issued_at_skew_seconds=value.maximum_issued_at_skew_seconds,
        maximum_subplans=value.maximum_subplans,
        maximum_policy_items=value.maximum_policy_items,
    )


def _issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_from_generated_metadata(
    *,
    metadata_fields: object,
    expected_metadata_digest: object,
    expected_protocol_configuration_digest: object,
) -> _CompiledMetaCampaignPauseApprovalBridgeBindingV2:
    """Issue one Meta-only opaque binding from exact generated 18-field metadata.

    The generated module is a replacement in a reviewed artifact source tree.
    This factory checks static field order, scalar types, full metadata digest,
    and the exact seven-field protocol projection.  It does not defend against
    arbitrary same-process rewriting of every private fact and resolver.
    """
    try:
        metadata = _rebuild_generated_metadata(metadata_fields)
        checked_fields = _generated_metadata_fields(metadata)
        metadata_digest = _require_generated_digest(expected_metadata_digest)
        protocol_digest = _require_generated_digest(expected_protocol_configuration_digest)
        if hashlib.sha256(canonical_bytes(metadata)).hexdigest() != metadata_digest:
            raise ValueError("generated metadata digest")
        configuration = _configuration_from_generated_metadata(metadata)
        if configuration.configuration_digest != protocol_digest:
            raise ValueError("generated protocol digest")
        return _CompiledMetaCampaignPauseApprovalBridgeBindingV2(
            _ISSUER,
            configuration=_detached_configuration(configuration),
            metadata_fields=checked_fields,
            metadata_digest=metadata_digest,
            protocol_configuration_digest=protocol_digest,
        )
    except Exception:
        raise _compiled_binding_not_installed() from None


def _checked_compiled_meta_campaign_pause_approval_bridge_binding_v2(
    value: object,
) -> _CompiledMetaCampaignPauseApprovalBridgeBindingV2:
    """Revalidate every retained static fact and return a detached binding copy."""
    try:
        if type(value) is not _CompiledMetaCampaignPauseApprovalBridgeBindingV2:
            raise ValueError("binding type")
        issuer = object.__getattribute__(
            value, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__issuer"
        )
        configuration = object.__getattribute__(
            value, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__configuration"
        )
        metadata_fields = object.__getattribute__(
            value, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_fields"
        )
        metadata_digest = object.__getattribute__(
            value, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_digest"
        )
        protocol_digest = object.__getattribute__(
            value,
            "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__protocol_configuration_digest",
        )
        if issuer is not _ISSUER or type(configuration) is not ApprovalHostConfiguration:
            raise ValueError("binding issuer or configuration")
        checked_metadata_digest = _require_generated_digest(metadata_digest)
        checked_protocol_digest = _require_generated_digest(protocol_digest)
        metadata = _rebuild_generated_metadata(metadata_fields)
        if hashlib.sha256(canonical_bytes(metadata)).hexdigest() != checked_metadata_digest:
            raise ValueError("binding metadata digest")
        metadata_configuration = _configuration_from_generated_metadata(metadata)
        checked_configuration = _detached_configuration(configuration)
        if (
            checked_configuration.to_data() != metadata_configuration.to_data()
            or checked_configuration.configuration_digest != checked_protocol_digest
        ):
            raise ValueError("binding protocol configuration")
        return _CompiledMetaCampaignPauseApprovalBridgeBindingV2(
            _ISSUER,
            configuration=checked_configuration,
            metadata_fields=_generated_metadata_fields(metadata),
            metadata_digest=checked_metadata_digest,
            protocol_configuration_digest=checked_protocol_digest,
        )
    except Exception:
        raise _compiled_binding_not_installed() from None


def _compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(
    value: object,
) -> tuple[ApprovalHostConfiguration, str]:
    """Return only detached configuration and metadata-digest facts after recheck."""
    checked = _checked_compiled_meta_campaign_pause_approval_bridge_binding_v2(value)
    try:
        configuration = object.__getattribute__(
            checked, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__configuration"
        )
        metadata_digest = object.__getattribute__(
            checked, "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__metadata_digest"
        )
        protocol_digest = object.__getattribute__(
            checked,
            "_CompiledMetaCampaignPauseApprovalBridgeBindingV2__protocol_configuration_digest",
        )
        if (
            type(configuration) is not ApprovalHostConfiguration
            or type(metadata_digest) is not str
            or type(protocol_digest) is not str
            or configuration.configuration_digest != protocol_digest
        ):
            raise ValueError("binding facts")
        return _detached_configuration(configuration), metadata_digest
    except Exception:
        raise _compiled_binding_not_installed() from None


def _resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2() -> (
    _CompiledMetaCampaignPauseApprovalBridgeBindingV2
):
    """Resolve only a separately generated Meta V2 binding, failing closed by default."""
    try:
        from adhelm._generated_meta_campaign_pause_approval_bridge_binding_v2 import (
            _resolve_generated_meta_campaign_pause_approval_bridge_binding_v2,
        )

        return _checked_compiled_meta_campaign_pause_approval_bridge_binding_v2(
            _resolve_generated_meta_campaign_pause_approval_bridge_binding_v2()
        )
    except AdhelmError as error:
        if error.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED":
            raise
        raise _compiled_binding_not_installed() from None
    except Exception:
        raise _compiled_binding_not_installed() from None


def _require_compiled_meta_campaign_pause_approval_bridge_v2_ready() -> None:
    """Fail before source, credential, helper, or native-review activity when absent."""
    _compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(
        _resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2()
    )


class _NeverCancelled:
    """Installed composition never exposes a caller-controlled cancellation handle."""

    def cancelled(self) -> bool:
        return False


def _fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2() -> (
    _MetaCampaignPauseApprovalBridgePipeAdapterV2
):
    """Construct only M47's sealed fixed-system-helper role after binding validation."""
    return _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(
        _SystemNativeApprovalBridgePosixOperations()
    )


def _fixed_meta_campaign_pause_approval_bridge_cancellation_v2() -> (
    _NativeApprovalBridgeCancellation
):
    return _NeverCancelled()


def _request_and_record_installed_meta_campaign_pause_approval_v2(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    source_plan_record_digest: str,
) -> AcceptedNativeApproval:
    """Run one compiled-bound private Meta V2 pending exchange and local claim.

    The checked-in generated resolver returns ``None``, so ordinary source
    fails with a fixed precondition before retained-source reads, credential
    work, helper construction, or native activity.  No caller can supply
    metadata, key, clock, adapter, receipt, path, candidate, or grantor facts.
    """
    binding = _resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2()
    configuration, metadata_digest = _compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(
        binding
    )
    reviewed_metadata = _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
        configuration, metadata_digest
    )
    pending = _prepare_installed_meta_campaign_pause_approval_bridge_v2(
        kernel,
        ledger,
        source_plan_record_digest=source_plan_record_digest,
        reviewed_metadata=reviewed_metadata,
    )
    return _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
        kernel,
        ledger,
        pending=pending,
        adapter=_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2(),
        cancellation=_fixed_meta_campaign_pause_approval_bridge_cancellation_v2(),
    )

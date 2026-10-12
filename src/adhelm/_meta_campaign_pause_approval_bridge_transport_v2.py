"""Private one-shot POSIX exchange for the Meta campaign PAUSE bridge V2.

This module validates one already-issued V2 request before a fixed helper can
be launched, then validates one framed V2 candidate after it returns.  It has
no installed-binding resolver, pending-request owner, receipt acceptance,
ledger, credential, provider, CLI, or runtime integration.  The shared pipe
adapter owns only bounded anonymous-pipe mechanics and derives its executable
from a closed private launch role.
"""

from __future__ import annotations

from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange,
    validate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm._native_approval_bridge_transport import (
    _CancellationObserved,
    _check_not_cancelled,
    _checked_utc_now,
    _frame,
    _NativeApprovalBridgeCancellation,
    _NativeApprovalBridgeLaunchRole,
    _NativeApprovalBridgePosixOperations,
    _NativeApprovalBridgeValidationClock,
    _PosixNativeApprovalBridgePipeAdapter,
    _PreLaunchCancellation,
    _PreLaunchUnavailable,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.errors import AdhelmError, ExitCode

_ISSUER = object()


class _MetaCampaignPauseApprovalBridgePipeAdapterV2:
    """Module-issued V2 pipe capability with a non-selectable launch role."""

    __slots__ = ("__issuer", "__delegate")

    def __init__(self, issuer: object, *, delegate: _PosixNativeApprovalBridgePipeAdapter) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Meta campaign PAUSE V2 pipe adapters are module-issued only.")
        object.__setattr__(self, "_MetaCampaignPauseApprovalBridgePipeAdapterV2__issuer", issuer)
        object.__setattr__(
            self, "_MetaCampaignPauseApprovalBridgePipeAdapterV2__delegate", delegate
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Meta campaign PAUSE V2 pipe adapters are immutable.")

    def __repr__(self) -> str:
        return "<fixed Meta campaign PAUSE V2 pipe adapter>"


def _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(
    operations: _NativeApprovalBridgePosixOperations,
) -> _MetaCampaignPauseApprovalBridgePipeAdapterV2:
    """Bind the shared POSIX state machine to the one closed V2 helper role."""
    return _MetaCampaignPauseApprovalBridgePipeAdapterV2(
        _ISSUER,
        delegate=_PosixNativeApprovalBridgePipeAdapter(
            operations,
            launch_role=_NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2,
        ),
    )


def exchange_meta_campaign_pause_approval_bridge_once_v2(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    *,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    adapter: _MetaCampaignPauseApprovalBridgePipeAdapterV2,
    validation_clock: _NativeApprovalBridgeValidationClock,
    cancellation: _NativeApprovalBridgeCancellation,
) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
    """Run exactly one V2 exchange through an injected fixed pipe adapter.

    The caller supplies typed local facts and an injected fakeable adapter, but
    cannot choose a helper path, command, environment, key, receipt, or
    provider target.  Invalid request facts and cancellation before launch
    remain preconditions.  Once ``adapter.exchange`` returns or raises, every
    error is an ambiguous no-retry result because a native nonce may already
    have been consumed.
    """
    try:
        exchange_started_at = _checked_utc_now(validation_clock)
        checked_request = revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange(
            request,
            configuration=configuration,
            expected_metadata_digest=expected_metadata_digest,
            exchange_started_at=exchange_started_at,
        )
        checked_adapter = _checked_adapter(adapter)
        _check_not_cancelled(cancellation)
    except (_CancellationObserved, _PreLaunchCancellation):
        raise _transport_cancelled() from None
    except AdhelmError as exc:
        if exc.code.startswith("NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_"):
            raise
        raise _transport_precondition() from None
    except Exception:
        raise _transport_precondition() from None

    try:
        response = checked_adapter.exchange(
            _frame(checked_request.canonical_bytes),
            cancellation=cancellation,
        )
    except _PreLaunchCancellation:
        raise _transport_cancelled() from None
    except _PreLaunchUnavailable:
        raise _transport_precondition() from None
    except BaseException:
        raise _transport_unverified() from None

    try:
        _check_not_cancelled(cancellation)
        current_now = _checked_utc_now(validation_clock)
        candidate = validate_meta_campaign_pause_approval_bridge_candidate_v2(
            response,
            request=checked_request,
            configuration=configuration,
            expected_metadata_digest=expected_metadata_digest,
            exchange_started_at=exchange_started_at,
            current_now=current_now,
        )
        final_now = _checked_utc_now(validation_clock)
        if final_now < current_now or final_now >= candidate.expires_at:
            raise ValueError("candidate validity changed during local verification")
        _check_not_cancelled(cancellation)
        return candidate
    except BaseException:
        raise _transport_unverified() from None


def _checked_adapter(value: object) -> _PosixNativeApprovalBridgePipeAdapter:
    """Recover only a module-issued wrapper around the exact V2-role adapter."""
    try:
        if type(value) is not _MetaCampaignPauseApprovalBridgePipeAdapterV2:
            raise ValueError("adapter type")
        issuer = object.__getattribute__(
            value, "_MetaCampaignPauseApprovalBridgePipeAdapterV2__issuer"
        )
        delegate = object.__getattribute__(
            value, "_MetaCampaignPauseApprovalBridgePipeAdapterV2__delegate"
        )
        role = object.__getattribute__(delegate, "_launch_role")
        if (
            issuer is not _ISSUER
            or type(delegate) is not _PosixNativeApprovalBridgePipeAdapter
            or role is not _NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2
        ):
            raise ValueError("adapter binding")
        return delegate
    except Exception:
        raise ValueError("adapter is invalid") from None


def _transport_cancelled() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_CANCELLED",
        "Meta campaign PAUSE approval bridge exchange was cancelled before helper launch.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Start a fresh Meta campaign PAUSE approval exchange after cancellation is cleared."
        ),
    )


def _transport_precondition() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE",
        (
            "Meta campaign PAUSE approval bridge helper exchange cannot start from the fixed "
            "local binding."
        ),
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Inspect the fixed Meta campaign PAUSE approval bridge binding before a fresh review."
        ),
    )


def _transport_unverified() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED",
        "Meta campaign PAUSE approval bridge exchange outcome is unverified after helper launch.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect native approval state and start a fresh Meta campaign PAUSE review; "
            "do not retry this exchange."
        ),
    )

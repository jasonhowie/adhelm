"""Private retained-Meta source preparation and local V2 candidate acceptance.

This module is deliberately not an installed bridge resolver, default runtime,
pipe implementation, XPC client, credential reader, provider client, or
execution entry point. It holds only caller-supplied reviewed metadata facts,
creates a one-use in-process pending request for one retained Meta campaign
PAUSE plan, and can privately compose that held request with M47's sealed pipe
capability before the shared exact receipt-claim core.

The existing V1 installed bridge remains independently versioned and unchanged.
"""

from __future__ import annotations

import re
import secrets
from _thread import LockType
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Final, Literal, cast

from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    build_meta_campaign_pause_approval_bridge_request_v2,
    derive_meta_campaign_pause_approval_bridge_source_context_digest_v2,
)
from adhelm._meta_campaign_pause_approval_bridge_transport_v2 import (
    _MetaCampaignPauseApprovalBridgePipeAdapterV2,
    exchange_meta_campaign_pause_approval_bridge_once_v2,
)
from adhelm._native_approval_bridge_transport import _NativeApprovalBridgeCancellation
from adhelm.approval_acceptance import (
    AcceptedNativeApproval,
    _accept_native_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalHostConfiguration,
    build_meta_campaign_pause_challenge,
    parse_approval_challenge_wire,
)
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import STREAMS, Ledger
from adhelm.meta_pause_plan_records import revalidate_meta_campaign_pause_plan_record
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPausePlannedAction,
    revalidate_meta_campaign_pause_planned_action,
)
from adhelm.safety import SafetyKernel

_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_META_PLAN_RECORD_KIND: Final = "META_CAMPAIGN_PAUSE_PLAN"
_GRANTOR_PREFIX: Final = "logical:adhelm-meta-campaign-pause-approval-bridge-v2:"
_ISSUER: Final = object()


class _MetaCampaignPauseApprovalBridgeReviewedMetadataV2:
    """Detached caller-held metadata facts; this does not attest installation."""

    __slots__ = ("__issuer", "__configuration", "__metadata_digest")

    def __init__(
        self,
        issuer: object,
        *,
        configuration: ApprovalHostConfiguration,
        metadata_digest: str,
    ) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Meta campaign PAUSE bridge metadata is module-issued only.")
        object.__setattr__(
            self, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__issuer", issuer
        )
        object.__setattr__(
            self, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__configuration", configuration
        )
        object.__setattr__(
            self,
            "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__metadata_digest",
            metadata_digest,
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Meta campaign PAUSE bridge metadata is immutable.")

    def __repr__(self) -> str:
        return "<caller-held Meta campaign PAUSE bridge metadata>"

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("Meta campaign PAUSE bridge metadata is not serializable.")


def _reviewed_metadata_facts(
    value: _MetaCampaignPauseApprovalBridgeReviewedMetadataV2,
) -> tuple[ApprovalHostConfiguration, str]:
    """Return detached closed facts only after module-issued metadata validation.

    This deliberately remains a module-local accessor rather than exposing a
    generic configuration or metadata-digest property on the caller-held
    evidence value.  It re-detaches the configuration on every use.
    """
    checked = _checked_reviewed_metadata(value)
    try:
        configuration = object.__getattribute__(
            checked, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__configuration"
        )
        metadata_digest = object.__getattribute__(
            checked, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__metadata_digest"
        )
        if type(configuration) is not ApprovalHostConfiguration:
            raise ValueError("metadata configuration")
        return _detached_configuration(configuration), _require_digest(metadata_digest)
    except Exception:
        raise _metadata_invalid() from None


def _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
    configuration: object, expected_metadata_digest: object
) -> _MetaCampaignPauseApprovalBridgeReviewedMetadataV2:
    """Detach reviewed caller facts without resolving an installed binding.

    The full metadata digest is a caller-held expected fact in this offline
    layer.  The seven-field protocol digest is always recomputed from the
    detached closed configuration before a request is built.
    """
    try:
        if type(configuration) is not ApprovalHostConfiguration:
            raise ValueError("configuration type")
        detached = _detached_configuration(configuration)
        metadata_digest = _require_digest(expected_metadata_digest)
        return _MetaCampaignPauseApprovalBridgeReviewedMetadataV2(
            _ISSUER,
            configuration=detached,
            metadata_digest=metadata_digest,
        )
    except Exception:
        raise _metadata_invalid() from None


def _detached_configuration(value: ApprovalHostConfiguration) -> ApprovalHostConfiguration:
    """Reconstruct the closed seven-field protocol configuration without coercion."""
    return ApprovalHostConfiguration(
        host_ref=value.host_ref,
        signing_key_ref=value.signing_key_ref,
        maximum_challenge_bytes=value.maximum_challenge_bytes,
        maximum_challenge_lifetime_seconds=value.maximum_challenge_lifetime_seconds,
        maximum_issued_at_skew_seconds=value.maximum_issued_at_skew_seconds,
        maximum_subplans=value.maximum_subplans,
        maximum_policy_items=value.maximum_policy_items,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _MetaCampaignPauseBridgeStreamPrefixV2:
    stream: Literal["changes", "creative", "performance"]
    stream_id: str
    high_watermark: int
    head_digest: str | None


@dataclass(frozen=True, slots=True, repr=False)
class _RetainedMetaCampaignPauseSourceV2:
    record_digest: str
    profile_ref: str
    action: MetaCampaignPausePlannedAction
    canonical_action: bytes
    checkpoint_digest: str
    stream_prefixes: tuple[_MetaCampaignPauseBridgeStreamPrefixV2, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _PendingMetaCampaignPauseApprovalBridgeDataV2:
    reviewed_metadata: _MetaCampaignPauseApprovalBridgeReviewedMetadataV2
    source: _RetainedMetaCampaignPauseSourceV2
    challenge: ApprovalChallenge
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2


class _PendingInstalledMetaCampaignPauseApprovalBridgeV2:
    """One in-process request; consuming it never authorizes provider egress."""

    __slots__ = ("__issuer", "__data", "__lock", "__attempted")

    def __init__(self, issuer: object, data: _PendingMetaCampaignPauseApprovalBridgeDataV2) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Pending Meta campaign PAUSE bridge requests are module-issued only.")
        object.__setattr__(
            self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__issuer", issuer
        )
        object.__setattr__(self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__data", data)
        object.__setattr__(self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__lock", Lock())
        object.__setattr__(
            self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__attempted", False
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Pending Meta campaign PAUSE bridge requests are immutable.")

    def __repr__(self) -> str:
        return "<pending Meta campaign PAUSE approval bridge V2 request>"

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("Pending Meta campaign PAUSE bridge requests are not serializable.")

    def _consume_once(self) -> _PendingMetaCampaignPauseApprovalBridgeDataV2:
        try:
            issuer = object.__getattribute__(
                self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__issuer"
            )
            lock = object.__getattribute__(
                self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__lock"
            )
            data = object.__getattribute__(
                self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__data"
            )
            attempted = object.__getattribute__(
                self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__attempted"
            )
            if (
                issuer is not _ISSUER
                or type(lock) is not LockType
                or type(data) is not _PendingMetaCampaignPauseApprovalBridgeDataV2
                or type(attempted) is not bool
            ):
                raise ValueError("pending state")
        except Exception:
            raise _pending_invalid() from None
        with lock:
            if (
                object.__getattribute__(
                    self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__attempted"
                )
                is not False
            ):
                raise _pending_consumed()
            object.__setattr__(
                self, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__attempted", True
            )
            return data


class _RecordedKernelValidationClockV2:
    """Private UTC samples shared by one transport and exact acceptance handoff.

    This is deliberately a wall-clock wrapper, not a replacement for M47's
    monotonic pipe deadline. It rejects rollback across the transport's
    exchange-start/current/final samples and retains only those private bounds
    for the post-candidate receipt continuation.
    """

    __slots__ = ("__kernel", "__first", "__last")

    def __init__(self, kernel: SafetyKernel) -> None:
        object.__setattr__(self, "_RecordedKernelValidationClockV2__kernel", kernel)
        object.__setattr__(self, "_RecordedKernelValidationClockV2__first", None)
        object.__setattr__(self, "_RecordedKernelValidationClockV2__last", None)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Recorded Meta campaign PAUSE bridge clock is immutable.")

    def utc_now(self) -> datetime:
        current = _kernel_now(
            object.__getattribute__(self, "_RecordedKernelValidationClockV2__kernel")
        )
        prior = object.__getattribute__(self, "_RecordedKernelValidationClockV2__last")
        if prior is not None and current < prior:
            raise ValueError("local bridge clock rolled back")
        if object.__getattribute__(self, "_RecordedKernelValidationClockV2__first") is None:
            object.__setattr__(self, "_RecordedKernelValidationClockV2__first", current)
        object.__setattr__(self, "_RecordedKernelValidationClockV2__last", current)
        return current

    def exchange_bounds(self) -> tuple[datetime, datetime]:
        try:
            first = object.__getattribute__(self, "_RecordedKernelValidationClockV2__first")
            final = object.__getattribute__(self, "_RecordedKernelValidationClockV2__last")
            if (
                type(first) is not datetime
                or first.tzinfo is None
                or first.utcoffset() is None
                or type(final) is not datetime
                or final.tzinfo is None
                or final.utcoffset() is None
                or final < first
            ):
                raise ValueError("exchange clock bounds")
            return first.astimezone(UTC), final.astimezone(UTC)
        except Exception:
            raise ValueError("exchange clock bounds") from None


def _prepare_installed_meta_campaign_pause_approval_bridge_v2(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    source_plan_record_digest: str,
    reviewed_metadata: _MetaCampaignPauseApprovalBridgeReviewedMetadataV2,
    nonce: bytes | None = None,
    request_id: bytes | None = None,
) -> _PendingInstalledMetaCampaignPauseApprovalBridgeV2:
    """Capture one retained Meta source and build one private V2 request.

    ``nonce`` and ``request_id`` are private deterministic-test seams only.
    Normal callers receive fresh 32-byte values generated in this module.
    No caller provides a grantor display value: the signed logical label is
    deterministically derived from the selected source record digest.
    """
    _require_coordinator(kernel, ledger)
    metadata = _checked_reviewed_metadata(reviewed_metadata)
    configuration, metadata_digest = _reviewed_metadata_facts(metadata)
    source = _select_retained_meta_campaign_pause_source(ledger, source_plan_record_digest)
    now = _kernel_now(kernel)
    expires_at = min(
        source.action.plan.expires_at,
        now + timedelta(seconds=configuration.maximum_challenge_lifetime_seconds),
    )
    try:
        checked_nonce = secrets.token_bytes(32) if nonce is None else _exact_token(nonce)
        checked_request_id = (
            secrets.token_bytes(32) if request_id is None else _exact_token(request_id)
        )
        challenge = build_meta_campaign_pause_challenge(
            source.action,
            grantor_ref=_derived_grantor_ref(source.record_digest),
            expires_at=expires_at,
            configuration=configuration,
            nonce=checked_nonce,
            issued_at=now,
            now=now,
        )
        source_context_digest = derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(
            workspace_profile_ref=source.profile_ref,
            source_plan_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
        )
        request = build_meta_campaign_pause_approval_bridge_request_v2(
            challenge,
            configuration=configuration,
            request_id=checked_request_id,
            source_context_digest=source_context_digest,
            expected_metadata_digest=metadata_digest,
        )
    except AdhelmError:
        raise _source_invalid() from None
    except Exception:
        raise _source_invalid() from None
    return _PendingInstalledMetaCampaignPauseApprovalBridgeV2(
        _ISSUER,
        _PendingMetaCampaignPauseApprovalBridgeDataV2(
            reviewed_metadata=metadata,
            source=source,
            challenge=challenge,
            request=request,
        ),
    )


def _accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    pending: _PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    exchange_started_at: datetime,
    minimum_current_time: datetime,
) -> AcceptedNativeApproval:
    """Accept one already-returned V2 candidate after exact source replay checks.

    This function deliberately has no transport parameter.  Once a candidate
    has reached it, every source replay, time, candidate, claim, or recording
    failure is ambiguous and the one-use pending request remains consumed.
    """
    data = _pending_data(pending)
    return _accept_held_meta_campaign_pause_approval_bridge_v2(
        kernel,
        ledger,
        data=data,
        candidate=candidate,
        exchange_started_at=exchange_started_at,
        minimum_current_time=minimum_current_time,
    )


def _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    pending: _PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    adapter: _MetaCampaignPauseApprovalBridgePipeAdapterV2,
    cancellation: _NativeApprovalBridgeCancellation,
) -> AcceptedNativeApproval:
    """Spend one held pending request before one fixed M47 V2 exchange.

    No caller receives the request, candidate, raw receipt, key, helper path,
    or grantor reference. A successful pending structural check marks it used
    before the prelaunch source reload and before the pipe adapter can launch.
    The existing candidate-only acceptance entry retains its one-use API; this
    composition uses the held-data continuation instead of consuming twice.
    """
    data = _pending_data(pending)
    try:
        _require_coordinator(kernel, ledger)
        _reload_and_compare_retained_meta_campaign_pause_source(ledger, data.source)
        configuration, metadata_digest = _reviewed_metadata_facts(data.reviewed_metadata)
        clock = _RecordedKernelValidationClockV2(kernel)
    except BaseException:
        raise _pending_exchange_precondition() from None

    candidate = exchange_meta_campaign_pause_approval_bridge_once_v2(
        data.request,
        configuration=configuration,
        expected_metadata_digest=metadata_digest,
        adapter=adapter,
        validation_clock=clock,
        cancellation=cancellation,
    )
    try:
        exchange_started_at, minimum_current_time = clock.exchange_bounds()
        return _accept_held_meta_campaign_pause_approval_bridge_v2(
            kernel,
            ledger,
            data=data,
            candidate=candidate,
            exchange_started_at=exchange_started_at,
            minimum_current_time=minimum_current_time,
        )
    except BaseException:
        raise _acceptance_unverified() from None


def _accept_held_meta_campaign_pause_approval_bridge_v2(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    data: _PendingMetaCampaignPauseApprovalBridgeDataV2,
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    exchange_started_at: datetime,
    minimum_current_time: datetime,
) -> AcceptedNativeApproval:
    """Accept one candidate from already-consumed, module-held pending facts."""
    try:
        data = _revalidate_pending_data(data)
        _require_coordinator(kernel, ledger)
        _reload_and_compare_retained_meta_campaign_pause_source(ledger, data.source)
        configuration, metadata_digest = _reviewed_metadata_facts(data.reviewed_metadata)
        return _accept_native_meta_campaign_pause_approval_bridge_candidate_v2(
            kernel,
            ledger,
            candidate=candidate,
            request=data.request,
            expected_challenge=data.challenge,
            configuration=configuration,
            expected_metadata_digest=metadata_digest,
            exchange_started_at=exchange_started_at,
            minimum_current_time=minimum_current_time,
            source_plan_record_digest=data.source.record_digest,
        )
    except BaseException:
        raise _acceptance_unverified() from None


def _pending_data(
    value: object,
) -> _PendingMetaCampaignPauseApprovalBridgeDataV2:
    """Validate pending facts before any candidate is examined."""
    try:
        if type(value) is not _PendingInstalledMetaCampaignPauseApprovalBridgeV2:
            raise ValueError("pending type")
        data = value._consume_once()
    except AdhelmError as error:
        if error.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED":
            raise
        raise _pending_invalid() from None
    except Exception:
        raise _pending_invalid() from None
    return _revalidate_pending_data(data)


def _revalidate_pending_data(
    value: object,
) -> _PendingMetaCampaignPauseApprovalBridgeDataV2:
    try:
        if type(value) is not _PendingMetaCampaignPauseApprovalBridgeDataV2:
            raise ValueError("pending data")
        metadata = _checked_reviewed_metadata(value.reviewed_metadata)
        configuration, _ = _reviewed_metadata_facts(metadata)
        source = _revalidate_retained_meta_campaign_pause_source(value.source)
        challenge = _revalidate_pending_meta_campaign_pause_challenge(
            value.challenge, source, configuration
        )
        request = _revalidate_pending_meta_campaign_pause_request(
            value.request,
            source=source,
            challenge=challenge,
            metadata=metadata,
        )
        return _PendingMetaCampaignPauseApprovalBridgeDataV2(
            reviewed_metadata=metadata,
            source=source,
            challenge=challenge,
            request=request,
        )
    except Exception:
        raise _pending_invalid() from None


def _checked_reviewed_metadata(
    value: object,
) -> _MetaCampaignPauseApprovalBridgeReviewedMetadataV2:
    try:
        if type(value) is not _MetaCampaignPauseApprovalBridgeReviewedMetadataV2:
            raise ValueError("metadata type")
        issuer = object.__getattribute__(
            value, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__issuer"
        )
        configuration = object.__getattribute__(
            value, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__configuration"
        )
        metadata_digest = object.__getattribute__(
            value, "_MetaCampaignPauseApprovalBridgeReviewedMetadataV2__metadata_digest"
        )
        if issuer is not _ISSUER:
            raise ValueError("metadata issuer")
        return _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
            configuration, metadata_digest
        )
    except AdhelmError:
        raise
    except Exception:
        raise _metadata_invalid() from None


def _select_retained_meta_campaign_pause_source(
    ledger: Ledger, record_digest: object
) -> _RetainedMetaCampaignPauseSourceV2:
    checkpoint = _verified_checkpoint(ledger)
    record = _selected_meta_campaign_pause_record(checkpoint, record_digest)
    return _retained_meta_campaign_pause_source_from_record(
        checkpoint,
        record,
        record_digest=record_digest,
        profile_ref=ledger.profile_ref,
    )


def _reload_and_compare_retained_meta_campaign_pause_source(
    ledger: Ledger, expected: _RetainedMetaCampaignPauseSourceV2
) -> None:
    checkpoint = _verified_checkpoint(ledger)
    _require_checkpoint_extension(expected.stream_prefixes, checkpoint)
    record = _selected_meta_campaign_pause_record(checkpoint, expected.record_digest)
    actual = _retained_meta_campaign_pause_source_from_record(
        checkpoint,
        record,
        record_digest=expected.record_digest,
        profile_ref=ledger.profile_ref,
    )
    if (
        actual.record_digest != expected.record_digest
        or actual.profile_ref != expected.profile_ref
        or actual.canonical_action != expected.canonical_action
    ):
        raise _source_changed()


def _revalidate_retained_meta_campaign_pause_source(
    value: object,
) -> _RetainedMetaCampaignPauseSourceV2:
    try:
        if type(value) is not _RetainedMetaCampaignPauseSourceV2:
            raise ValueError("source type")
        record_digest = _require_digest(value.record_digest)
        if (
            type(value.profile_ref) is not str
            or not 1 <= len(value.profile_ref.encode("utf-8")) <= 160
        ):
            raise ValueError("profile")
        action = revalidate_meta_campaign_pause_planned_action(value.action)
        canonical_action = canonical_bytes(action.to_data())
        if type(value.canonical_action) is not bytes or value.canonical_action != canonical_action:
            raise ValueError("action bytes")
        checkpoint_digest = _require_digest(value.checkpoint_digest)
        prefixes = _revalidate_prefixes(value.stream_prefixes, value.profile_ref)
        return _RetainedMetaCampaignPauseSourceV2(
            record_digest=record_digest,
            profile_ref=value.profile_ref,
            action=action,
            canonical_action=canonical_action,
            checkpoint_digest=checkpoint_digest,
            stream_prefixes=prefixes,
        )
    except Exception:
        raise _source_invalid() from None


def _revalidate_pending_meta_campaign_pause_challenge(
    value: object,
    source: _RetainedMetaCampaignPauseSourceV2,
    configuration: ApprovalHostConfiguration,
) -> ApprovalChallenge:
    try:
        if type(value) is not ApprovalChallenge:
            raise ValueError("challenge type")
        challenge = parse_approval_challenge_wire(
            value.canonical_bytes, configuration=configuration
        )
        if (
            challenge.kind.value != "META_CAMPAIGN_PAUSE_PLAN"
            or challenge.expires_at > source.action.plan.expires_at
            or challenge.meta_pause_action is None
            or canonical_bytes(challenge.meta_pause_action.to_data()) != source.canonical_action
            or challenge.grantor_ref != _derived_grantor_ref(source.record_digest)
        ):
            raise ValueError("challenge binding")
        return challenge
    except Exception:
        raise _pending_invalid() from None


def _revalidate_pending_meta_campaign_pause_request(
    value: object,
    *,
    source: _RetainedMetaCampaignPauseSourceV2,
    challenge: ApprovalChallenge,
    metadata: _MetaCampaignPauseApprovalBridgeReviewedMetadataV2,
) -> NativeMetaCampaignPauseApprovalBridgeRequestV2:
    try:
        if type(value) is not NativeMetaCampaignPauseApprovalBridgeRequestV2:
            raise ValueError("request type")
        raw = value.canonical_bytes
        configuration, metadata_digest = _reviewed_metadata_facts(metadata)
        request = build_meta_campaign_pause_approval_bridge_request_v2(
            challenge,
            configuration=configuration,
            request_id=_request_identifier(value),
            source_context_digest=derive_meta_campaign_pause_approval_bridge_source_context_digest_v2(
                workspace_profile_ref=source.profile_ref,
                source_plan_record_digest=source.record_digest,
                checkpoint_digest=source.checkpoint_digest,
            ),
            expected_metadata_digest=metadata_digest,
        )
        if request.canonical_bytes != raw:
            raise ValueError("request bytes")
        return request
    except Exception:
        raise _pending_invalid() from None


def _request_identifier(value: NativeMetaCampaignPauseApprovalBridgeRequestV2) -> bytes:
    try:
        raw = object.__getattribute__(
            value, "_NativeMetaCampaignPauseApprovalBridgeRequestV2__request_id"
        )
        return _exact_token(raw)
    except Exception:
        raise _pending_invalid() from None


def _retained_meta_campaign_pause_source_from_record(
    checkpoint: dict[str, Any],
    record: dict[str, Any],
    *,
    record_digest: object,
    profile_ref: object,
) -> _RetainedMetaCampaignPauseSourceV2:
    try:
        if (
            type(record_digest) is not str
            or record.get("record_digest") != record_digest
            or record.get("record_kind") != _META_PLAN_RECORD_KIND
        ):
            raise ValueError("record selection")
        payload = record.get("payload")
        if type(payload) is not dict:
            raise ValueError("record payload")
        retained = revalidate_meta_campaign_pause_plan_record(payload)
        action = retained.action
        if (
            type(profile_ref) is not str
            or action.plan.profile_ref != profile_ref
            or retained.execution_authority is not False
            or retained.requires_current_kernel_checks is not True
        ):
            raise ValueError("retained binding")
        checkpoint_digest = _require_digest(checkpoint.get("checkpoint_digest"))
        prefixes = _revalidate_prefixes(
            _capture_stream_prefixes(checkpoint),
            action.plan.profile_ref,
        )
        return _RetainedMetaCampaignPauseSourceV2(
            record_digest=record_digest,
            profile_ref=action.plan.profile_ref,
            action=action,
            canonical_action=canonical_bytes(action.to_data()),
            checkpoint_digest=checkpoint_digest,
            stream_prefixes=prefixes,
        )
    except Exception:
        raise _source_invalid() from None


def _verified_checkpoint(ledger: Ledger) -> dict[str, Any]:
    try:
        checkpoint = ledger.checkpoint()
        if type(checkpoint) is not dict or checkpoint.get("status") != "VERIFIED_CHECKPOINT":
            raise ValueError("checkpoint status")
        _require_digest(checkpoint.get("checkpoint_digest"))
        streams = checkpoint.get("streams")
        records = checkpoint.get("records_by_stream")
        if type(streams) is not dict or type(records) is not dict:
            raise ValueError("checkpoint maps")
        if set(streams) != set(STREAMS) or set(records) != set(STREAMS):
            raise ValueError("checkpoint streams")
        _capture_stream_prefixes(checkpoint)
        return checkpoint
    except AdhelmError:
        raise
    except Exception:
        raise _source_unverified() from None


def _capture_stream_prefixes(
    checkpoint: dict[str, Any],
) -> tuple[_MetaCampaignPauseBridgeStreamPrefixV2, ...]:
    try:
        streams = cast(dict[str, Any], checkpoint["streams"])
        records_by_stream = cast(dict[str, Any], checkpoint["records_by_stream"])
        prefixes: list[_MetaCampaignPauseBridgeStreamPrefixV2] = []
        for stream in sorted(STREAMS):
            facts = streams[stream]
            records = records_by_stream[stream]
            if type(facts) is not dict or type(records) is not list:
                raise ValueError("stream shape")
            stream_id = facts.get("stream_id")
            high_watermark = facts.get("high_watermark")
            head_digest = facts.get("head_digest")
            if (
                type(stream_id) is not str
                or not 1 <= len(stream_id.encode("utf-8")) <= 160
                or type(high_watermark) is not int
                or isinstance(high_watermark, bool)
                or high_watermark < 0
                or len(records) != high_watermark
                or (high_watermark == 0) != (head_digest is None)
                or (head_digest is not None and _DIGEST.fullmatch(head_digest) is None)
            ):
                raise ValueError("stream facts")
            if high_watermark:
                row = records[high_watermark - 1]
                if (
                    type(row) is not dict
                    or row.get("stream_sequence") != high_watermark
                    or row.get("record_digest") != head_digest
                ):
                    raise ValueError("stream head")
            prefixes.append(
                _MetaCampaignPauseBridgeStreamPrefixV2(
                    stream=cast(Literal["changes", "creative", "performance"], stream),
                    stream_id=stream_id,
                    high_watermark=high_watermark,
                    head_digest=head_digest,
                )
            )
        return tuple(prefixes)
    except Exception:
        raise _source_unverified() from None


def _revalidate_prefixes(
    value: object, profile_ref: str
) -> tuple[_MetaCampaignPauseBridgeStreamPrefixV2, ...]:
    try:
        if type(value) is not tuple or len(value) != len(STREAMS):
            raise ValueError("prefix type")
        if type(profile_ref) is not str or not profile_ref:
            raise ValueError("profile")
        prefixes: list[_MetaCampaignPauseBridgeStreamPrefixV2] = []
        for prefix in value:
            if type(prefix) is not _MetaCampaignPauseBridgeStreamPrefixV2:
                raise ValueError("prefix member")
            if prefix.stream not in STREAMS:
                raise ValueError("stream")
            if (
                type(prefix.stream_id) is not str
                or prefix.stream_id != f"{profile_ref}:{prefix.stream}"
                or not 1 <= len(prefix.stream_id.encode("utf-8")) <= 160
                or type(prefix.high_watermark) is not int
                or isinstance(prefix.high_watermark, bool)
                or prefix.high_watermark < 0
                or (prefix.high_watermark == 0) != (prefix.head_digest is None)
                or (
                    prefix.head_digest is not None and _DIGEST.fullmatch(prefix.head_digest) is None
                )
            ):
                raise ValueError("prefix fields")
            prefixes.append(prefix)
        if {prefix.stream for prefix in prefixes} != set(STREAMS):
            raise ValueError("prefix coverage")
        return tuple(prefixes)
    except Exception:
        raise _source_invalid() from None


def _require_checkpoint_extension(
    expected: tuple[_MetaCampaignPauseBridgeStreamPrefixV2, ...], checkpoint: dict[str, Any]
) -> None:
    try:
        actual = {prefix.stream: prefix for prefix in _capture_stream_prefixes(checkpoint)}
        if (
            set(actual) != set(STREAMS)
            or len(expected) != len(STREAMS)
            or {prefix.stream for prefix in expected} != set(STREAMS)
        ):
            raise ValueError("stream set")
        for prior in expected:
            current = actual.get(prior.stream)
            if (
                current is None
                or current.stream_id != prior.stream_id
                or current.high_watermark < prior.high_watermark
            ):
                raise ValueError("stream replacement")
            if prior.high_watermark:
                records = checkpoint["records_by_stream"][prior.stream]
                head = records[prior.high_watermark - 1]
                if (
                    type(head) is not dict
                    or head.get("stream_sequence") != prior.high_watermark
                    or head.get("record_digest") != prior.head_digest
                ):
                    raise ValueError("stream prefix")
    except Exception:
        raise _source_changed() from None


def _selected_meta_campaign_pause_record(
    checkpoint: dict[str, Any], record_digest: object
) -> dict[str, Any]:
    try:
        checked_digest = _require_digest(record_digest)
        changes = checkpoint["records_by_stream"]["changes"]
        if type(changes) is not list:
            raise ValueError("changes")
        rows = [
            row
            for row in changes
            if type(row) is dict
            and row.get("record_digest") == checked_digest
            and row.get("record_kind") == _META_PLAN_RECORD_KIND
        ]
        if len(rows) != 1:
            raise ValueError("selection")
        return rows[0]
    except Exception:
        raise _source_invalid() from None


def _derived_grantor_ref(source_record_digest: str) -> str:
    return _GRANTOR_PREFIX + _require_digest(source_record_digest)


def _exact_token(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError("token")
    return value


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest")
    return value


def _kernel_now(kernel: SafetyKernel) -> datetime:
    try:
        value = kernel.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_CLOCK_INVALID",
            "The local Meta campaign PAUSE approval bridge requires an aware trusted clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _require_coordinator(kernel: SafetyKernel, ledger: Ledger) -> None:
    try:
        if kernel.ledger is not ledger or kernel.root.resolve() != ledger.root.resolve():
            raise ValueError("coordinator")
    except Exception:
        raise AdhelmError(
            "APPROVAL_COORDINATOR_MISMATCH",
            "Receipt recording and the safety kernel must use the same canonical ledger.",
            exit_code=ExitCode.AUTHORIZATION,
        ) from None


def _metadata_invalid() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_METADATA_INVALID",
        "Caller-held reviewed Meta approval metadata is invalid.",
        exit_code=ExitCode.PRECONDITION,
    )


def _source_invalid() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_SOURCE_INVALID",
        "An exact retained Meta campaign PAUSE plan record is required.",
        exit_code=ExitCode.PRECONDITION,
    )


def _source_unverified() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_SOURCE_UNVERIFIED",
        "The retained Meta campaign PAUSE source could not be verified.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect canonical ledger history before starting a fresh review.",
    )


def _source_changed() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_SOURCE_CHANGED",
        "The retained Meta campaign PAUSE source changed during approval handling.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect canonical ledger history and start a fresh review; do not reuse this exchange."
        ),
    )


def _pending_invalid() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_INVALID",
        "The pending Meta campaign PAUSE approval bridge request is invalid.",
        exit_code=ExitCode.PRECONDITION,
    )


def _pending_consumed() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED",
        "This Meta campaign PAUSE approval bridge request was already consumed.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect approval history and start a fresh review; never retry this exchange.",
    )


def _pending_exchange_precondition() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_EXCHANGE_PRECONDITION",
        (
            "The consumed Meta campaign PAUSE approval bridge request could not complete "
            "its fixed prelaunch checks."
        ),
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect canonical ledger history and start a fresh review.",
    )


def _acceptance_unverified() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED",
        "Meta campaign PAUSE approval acceptance could not be verified after candidate handling.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect approval history and start a fresh review; never retry this exchange.",
    )

"""Private retained-plan coordinator for the proposed installed approval bridge.

This module is deliberately unwired: no public command reaches it and its
compiled binding resolver fails closed until a reviewed installed product adds
one.  It keeps Python as the plan, ledger, and execution owner.  The only
possible result is an existing local approval record; provider execution still
belongs exclusively to :class:`adhelm.safety.SafetyKernel`.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Final, Literal, cast

from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeRequestV1,
    build_native_approval_bridge_request_v1,
    decode_native_approval_bridge_request_v1,
    derive_native_approval_bridge_source_context_digest_v1,
)
from adhelm._native_approval_bridge_transport import (
    _NativeApprovalBridgeCancellation,
    _NativeApprovalBridgePipeAdapter,
    _PosixNativeApprovalBridgePipeAdapter,
    _SystemNativeApprovalBridgePosixOperations,
    exchange_native_approval_bridge_once_v1,
)
from adhelm.approval_acceptance import (
    AcceptedNativeApproval,
    _accept_native_approval_bridge_candidate,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    build_ga4_key_event_create_challenge,
    build_openai_pause_challenge,
    parse_approval_challenge_wire,
)
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_create_plan_records import revalidate_ga4_key_event_create_plan_record
from adhelm.ledger import STREAMS, Ledger
from adhelm.plan_records import OpenAIPauseExecutionPlanRecord, revalidate_openai_pause_action
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreatePlannedAction,
    revalidate_ga4_key_event_create_planned_action,
)
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction
from adhelm.safety import SafetyKernel

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TAG_HEX = re.compile(r"^[0-9a-f]+$")
_GENERATED_METADATA_FIELD_ORDER: Final = (
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
_GENERATED_KEY_STORAGES: Final = frozenset({"KEYCHAIN_P256", "SECURE_ENCLAVE_P256"})
#: The installed V1 bridge request, helper, and host review cover only the two
#: direct retained-plan kinds.  ADR 0012's provider-neutral
#: ``COMPOSITE_EXECUTION_PLAN`` and ``ATTEMPT_RECOVERY`` kinds are refused here,
#: and by ``_native_approval_bridge_protocol._DIRECT_KINDS``, until a reviewed
#: host build that renders them is signed and installed.  Adding the Python kind
#: adds no installed capability.
_INSTALLED_BRIDGE_KINDS: Final = frozenset(
    {
        ApprovalChallengeKind.OPENAI_PAUSE_PLAN,
        ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN,
    }
)
_ISSUER: Final = object()


class _CompiledInstalledApprovalBridgeBinding:
    """Reviewed compiled facts, not a runtime loader or trust/pin value."""

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
            raise TypeError("Installed approval bridge bindings are module-issued only.")
        object.__setattr__(self, "_CompiledInstalledApprovalBridgeBinding__issuer", issuer)
        object.__setattr__(
            self, "_CompiledInstalledApprovalBridgeBinding__configuration", configuration
        )
        object.__setattr__(
            self, "_CompiledInstalledApprovalBridgeBinding__metadata_fields", metadata_fields
        )
        object.__setattr__(
            self, "_CompiledInstalledApprovalBridgeBinding__metadata_digest", metadata_digest
        )
        object.__setattr__(
            self,
            "_CompiledInstalledApprovalBridgeBinding__protocol_configuration_digest",
            protocol_configuration_digest,
        )

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Installed approval bridge bindings are immutable.")

    def __repr__(self) -> str:
        return "<compiled installed approval bridge binding>"

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("Installed approval bridge bindings are not serializable.")


class _PendingInstalledApprovalBridgeRequest:
    """One in-memory retained-plan exchange, consumed before helper launch."""

    __slots__ = ("__issuer", "__data", "__lock", "__attempted")

    def __init__(self, issuer: object, data: _PendingRequestData) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Pending approval bridge requests are module-issued only.")
        object.__setattr__(self, "_PendingInstalledApprovalBridgeRequest__issuer", issuer)
        object.__setattr__(self, "_PendingInstalledApprovalBridgeRequest__data", data)
        object.__setattr__(self, "_PendingInstalledApprovalBridgeRequest__lock", Lock())
        object.__setattr__(self, "_PendingInstalledApprovalBridgeRequest__attempted", False)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Pending approval bridge requests are immutable.")

    def __repr__(self) -> str:
        return "<pending installed approval bridge request>"

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("Pending approval bridge requests are not serializable.")

    def _consume_once(self) -> _PendingRequestData:
        try:
            issuer = object.__getattribute__(self, "_PendingInstalledApprovalBridgeRequest__issuer")
            lock = object.__getattribute__(self, "_PendingInstalledApprovalBridgeRequest__lock")
            data = object.__getattribute__(self, "_PendingInstalledApprovalBridgeRequest__data")
            attempted = object.__getattribute__(
                self, "_PendingInstalledApprovalBridgeRequest__attempted"
            )
            if (
                issuer is not _ISSUER
                or type(data) is not _PendingRequestData
                or type(attempted) is not bool
            ):
                raise ValueError("pending request is invalid")
        except Exception:
            raise _pending_invalid() from None
        with lock:
            if (
                object.__getattribute__(self, "_PendingInstalledApprovalBridgeRequest__attempted")
                is not False
            ):
                raise _pending_consumed()
            object.__setattr__(self, "_PendingInstalledApprovalBridgeRequest__attempted", True)
            return data


@dataclass(frozen=True, slots=True, repr=False)
class _StreamPrefix:
    stream: Literal["changes", "creative", "performance"]
    stream_id: str
    high_watermark: int
    head_digest: str | None


@dataclass(frozen=True, slots=True, repr=False)
class _RetainedDirectSource:
    record_digest: str
    record_kind: Literal["EXECUTION_PLAN", "GA4_KEY_EVENT_CREATE_PLAN"]
    approval_kind: ApprovalChallengeKind
    profile_ref: str
    action: OpenAIPausePlannedAction | Ga4KeyEventCreatePlannedAction
    canonical_action: bytes
    checkpoint_digest: str
    stream_prefixes: tuple[_StreamPrefix, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _PendingRequestData:
    binding: _CompiledInstalledApprovalBridgeBinding
    source: _RetainedDirectSource
    challenge: ApprovalChallenge
    request: NativeApprovalBridgeRequestV1


class _RecordedKernelClock:
    """M33 clock seam that preserves its exact first/final trusted samples."""

    def __init__(self, kernel: SafetyKernel) -> None:
        self._kernel = kernel
        self._exchange_started_at: datetime | None = None
        self._last: datetime | None = None

    def utc_now(self) -> datetime:
        value = _kernel_now(self._kernel)
        if self._last is not None and value < self._last:
            raise ValueError("local bridge clock rolled back")
        if self._exchange_started_at is None:
            self._exchange_started_at = value
        self._last = value
        return value

    def exchange_times(self) -> tuple[datetime, datetime]:
        if self._exchange_started_at is None or self._last is None:
            raise ValueError("bridge exchange did not sample its clock")
        return self._exchange_started_at, self._last


class _NeverCancelled:
    def cancelled(self) -> bool:
        return False


def _compiled_binding_not_installed() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED",
        "A reviewed compiled installed approval bridge binding is not available.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Install a reviewed approval bridge product before requesting a new review.",
    )


def _issue_compiled_installed_approval_bridge_binding_from_generated_metadata(
    *,
    metadata_fields: object,
    expected_metadata_digest: object,
    expected_protocol_configuration_digest: object,
) -> _CompiledInstalledApprovalBridgeBinding:
    """Issue one opaque binding from fixed code-generated metadata only.

    The generated module is a replacement in an isolated artifact source tree;
    this factory rebuilds every field and digest to reject malformed or
    incoherent generated facts. It is not a defense against arbitrary code in
    the same process replacing every private fact and resolver.
    """
    try:
        metadata = _rebuild_generated_metadata(metadata_fields)
        checked_metadata_fields = _generated_metadata_fields(metadata)
        metadata_digest = _require_generated_digest(expected_metadata_digest)
        protocol_digest = _require_generated_digest(expected_protocol_configuration_digest)
        if hashlib.sha256(canonical_bytes(metadata)).hexdigest() != metadata_digest:
            raise ValueError("generated metadata digest is invalid")
        configuration = _configuration_from_generated_metadata(metadata)
        if configuration.configuration_digest != protocol_digest:
            raise ValueError("generated protocol digest is invalid")
        return _CompiledInstalledApprovalBridgeBinding(
            _ISSUER,
            configuration=configuration,
            metadata_fields=checked_metadata_fields,
            metadata_digest=metadata_digest,
            protocol_configuration_digest=protocol_digest,
        )
    except Exception:
        raise _compiled_binding_not_installed() from None


def _resolve_compiled_installed_binding() -> _CompiledInstalledApprovalBridgeBinding:
    """Fail closed until generated installed Python binding code is reviewed."""
    try:
        from adhelm._generated_installed_approval_bridge_binding import (
            _resolve_generated_installed_approval_bridge_binding,
        )

        return _checked_compiled_binding(_resolve_generated_installed_approval_bridge_binding())
    except AdhelmError as error:
        if error.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED":
            raise
        raise _compiled_binding_not_installed() from None
    except Exception:
        raise _compiled_binding_not_installed() from None


def _require_compiled_installed_approval_bridge_ready() -> None:
    """Check only the fixed compiled binding before an explicit CLI request.

    This deliberately returns no metadata, trust, transport, or helper handle.
    It exists so an explicitly requested execution command can fail before its
    provider workflow reaches local credentials, native review, or egress.
    Discovery and schema commands never call this private gate.
    """
    _binding_facts(_resolve_compiled_installed_binding())


def _require_installed_bridge_kind(kind: object) -> ApprovalChallengeKind:
    """Refuse an approval kind this installed bridge and host cannot review.

    This fails before a helper launch, a native review, a credential, or any
    egress.  It is a capability gate, not a claim that the refused kind is
    unsafe: it states only that no installed, signed host renders it yet.
    """

    if type(kind) is not ApprovalChallengeKind or kind not in _INSTALLED_BRIDGE_KINDS:
        raise AdhelmError(
            "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED",
            "This approval kind has no installed native review capability.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a reviewed signed approval host that renders this kind, "
                "then request the review again."
            ),
        )
    return kind


def _resolve_pipe_adapter() -> _NativeApprovalBridgePipeAdapter:
    """Return mechanics only after the compiled binding gate has passed."""
    return _PosixNativeApprovalBridgePipeAdapter(_SystemNativeApprovalBridgePosixOperations())


def _resolve_cancellation() -> _NativeApprovalBridgeCancellation:
    return _NeverCancelled()


def _request_and_record_installed_native_approval(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    source_plan_record_digest: str,
    grantor_ref: str,
) -> AcceptedNativeApproval:
    """Run one private bridge exchange then record an existing direct grant.

    There is no default configured binding in this checkout, so production
    callers receive the fixed ``NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED`` result
    before a source read or helper launch. Test seams remain private.
    """
    pending = _prepare_installed_native_approval_request(
        kernel,
        ledger,
        source_plan_record_digest=source_plan_record_digest,
        grantor_ref=grantor_ref,
    )
    return _exchange_and_record_pending_installed_native_approval(kernel, ledger, pending=pending)


def _prepare_installed_native_approval_request(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    source_plan_record_digest: str,
    grantor_ref: str,
) -> _PendingInstalledApprovalBridgeRequest:
    """Capture one immutable retained source and build a one-use private frame."""
    _require_coordinator(kernel, ledger)
    binding = _checked_compiled_binding(_resolve_compiled_installed_binding())
    configuration, metadata_digest = _binding_facts(binding)
    source = _select_retained_direct_source(ledger, source_plan_record_digest)
    now = _kernel_now(kernel)
    expires_at = min(
        source.action.plan.expires_at,
        now + timedelta(seconds=configuration.maximum_challenge_lifetime_seconds),
    )
    _require_installed_bridge_kind(source.approval_kind)
    try:
        nonce = secrets.token_bytes(32)
        request_id = secrets.token_bytes(32)
        if source.approval_kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN:
            if not isinstance(source.action, OpenAIPausePlannedAction):
                raise ValueError("source action type is invalid")
            challenge = build_openai_pause_challenge(
                source.action,
                grantor_ref=grantor_ref,
                expires_at=expires_at,
                configuration=configuration,
                nonce=nonce,
                issued_at=now,
                now=now,
            )
        elif source.approval_kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN:
            if not isinstance(source.action, Ga4KeyEventCreatePlannedAction):
                raise ValueError("source action type is invalid")
            challenge = build_ga4_key_event_create_challenge(
                source.action,
                grantor_ref=grantor_ref,
                expires_at=expires_at,
                configuration=configuration,
                nonce=nonce,
                issued_at=now,
                now=now,
            )
        else:  # pragma: no cover - source selection fixes the two direct kinds
            raise ValueError("source kind is invalid")
        source_context_digest = derive_native_approval_bridge_source_context_digest_v1(
            workspace_profile_ref=source.profile_ref,
            source_plan_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
        )
        request = build_native_approval_bridge_request_v1(
            challenge,
            configuration=configuration,
            request_id=request_id,
            source_context_digest=source_context_digest,
            expected_metadata_digest=metadata_digest,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _source_invalid() from None
    return _PendingInstalledApprovalBridgeRequest(
        _ISSUER,
        _PendingRequestData(binding=binding, source=source, challenge=challenge, request=request),
    )


def _exchange_and_record_pending_installed_native_approval(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    pending: _PendingInstalledApprovalBridgeRequest,
) -> AcceptedNativeApproval:
    """Consume one pending request; all post-candidate failures are ambiguous."""
    _require_coordinator(kernel, ledger)
    data = _pending_data(pending)
    configuration, metadata_digest = _binding_facts(data.binding)
    try:
        adapter = _resolve_pipe_adapter()
        cancellation = _resolve_cancellation()
    except BaseException:
        raise _transport_unavailable() from None
    clock = _RecordedKernelClock(kernel)
    candidate = exchange_native_approval_bridge_once_v1(
        data.request,
        configuration=configuration,
        expected_metadata_digest=metadata_digest,
        adapter=adapter,
        validation_clock=clock,
        cancellation=cancellation,
    )
    try:
        exchange_started_at, exchange_finished_at = clock.exchange_times()
        _reload_and_compare_retained_source(ledger, data.source)
        return _accept_native_approval_bridge_candidate(
            kernel,
            ledger,
            candidate=candidate,
            request=data.request,
            expected_challenge=data.challenge,
            configuration=configuration,
            expected_metadata_digest=metadata_digest,
            exchange_started_at=exchange_started_at,
            minimum_current_time=exchange_finished_at,
            source_plan_record_digest=data.source.record_digest,
        )
    except BaseException:
        raise _post_consumption_unverified() from None


def _pending_data(value: object) -> _PendingRequestData:
    try:
        if type(value) is not _PendingInstalledApprovalBridgeRequest:
            raise ValueError("pending type is invalid")
        data = value._consume_once()
    except AdhelmError as error:
        if error.code == "NATIVE_APPROVAL_BRIDGE_PENDING_CONSUMED":
            raise
        raise _pending_invalid() from None
    except Exception:
        raise _pending_invalid() from None
    return _revalidate_pending_data(data)


def _revalidate_pending_data(value: object) -> _PendingRequestData:
    """Rebuild every prelaunch fact before an opaque request can reach pipes."""
    try:
        if type(value) is not _PendingRequestData:
            raise ValueError("pending data type is invalid")
        binding = _checked_compiled_binding(value.binding)
        configuration, metadata_digest = _binding_facts(binding)
        source = _revalidate_pending_source(value.source)
        challenge = _revalidate_pending_challenge(value.challenge, source, configuration)
        request = _revalidate_pending_request(
            value.request,
            source=source,
            challenge=challenge,
            configuration=configuration,
            metadata_digest=metadata_digest,
        )
        return _PendingRequestData(
            binding=binding,
            source=source,
            challenge=challenge,
            request=request,
        )
    except Exception:
        raise _pending_invalid() from None


def _revalidate_pending_source(value: object) -> _RetainedDirectSource:
    try:
        if type(value) is not _RetainedDirectSource:
            raise ValueError("pending source type is invalid")
        if type(value.record_digest) is not str or _DIGEST.fullmatch(value.record_digest) is None:
            raise ValueError("pending source record digest is invalid")
        if type(value.profile_ref) is not str or not value.profile_ref:
            raise ValueError("pending source profile is invalid")
        if (
            type(value.checkpoint_digest) is not str
            or _DIGEST.fullmatch(value.checkpoint_digest) is None
        ):
            raise ValueError("pending source checkpoint is invalid")
        prefixes = _revalidate_pending_prefixes(value.stream_prefixes, value.profile_ref)
        if (
            value.record_kind == "EXECUTION_PLAN"
            and value.approval_kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN
            and type(value.action) is OpenAIPausePlannedAction
        ):
            action: OpenAIPausePlannedAction | Ga4KeyEventCreatePlannedAction
            action = revalidate_openai_pause_action(value.action)
            canonical_action = canonical_bytes(action.model_dump(mode="json"))
        elif (
            value.record_kind == "GA4_KEY_EVENT_CREATE_PLAN"
            and value.approval_kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
            and type(value.action) is Ga4KeyEventCreatePlannedAction
        ):
            action = revalidate_ga4_key_event_create_planned_action(value.action)
            canonical_action = canonical_bytes(action.model_dump(mode="json", warnings="error"))
        else:
            raise ValueError("pending source kind is invalid")
        if (
            canonical_action != value.canonical_action
            or action.plan.profile_ref != value.profile_ref
        ):
            raise ValueError("pending source action is invalid")
        return _RetainedDirectSource(
            record_digest=value.record_digest,
            record_kind=value.record_kind,
            approval_kind=value.approval_kind,
            profile_ref=value.profile_ref,
            action=action,
            canonical_action=canonical_action,
            checkpoint_digest=value.checkpoint_digest,
            stream_prefixes=prefixes,
        )
    except Exception:
        raise _pending_invalid() from None


def _revalidate_pending_prefixes(value: object, profile_ref: str) -> tuple[_StreamPrefix, ...]:
    try:
        if type(value) is not tuple or len(value) != len(STREAMS):
            raise ValueError("pending source prefixes are invalid")
        checked: list[_StreamPrefix] = []
        for prefix in value:
            if type(prefix) is not _StreamPrefix or prefix.stream not in STREAMS:
                raise ValueError("pending stream prefix is invalid")
            if (
                type(prefix.stream_id) is not str
                or prefix.stream_id != f"{profile_ref}:{prefix.stream}"
                or type(prefix.high_watermark) is not int
                or isinstance(prefix.high_watermark, bool)
                or prefix.high_watermark < 0
                or (prefix.high_watermark == 0) != (prefix.head_digest is None)
                or (
                    prefix.head_digest is not None
                    and (
                        type(prefix.head_digest) is not str
                        or _DIGEST.fullmatch(prefix.head_digest) is None
                    )
                )
            ):
                raise ValueError("pending stream prefix fields are invalid")
            checked.append(
                _StreamPrefix(
                    stream=prefix.stream,
                    stream_id=prefix.stream_id,
                    high_watermark=prefix.high_watermark,
                    head_digest=prefix.head_digest,
                )
            )
        if {prefix.stream for prefix in checked} != set(STREAMS):
            raise ValueError("pending stream prefix coverage is invalid")
        return tuple(checked)
    except Exception:
        raise _pending_invalid() from None


def _revalidate_pending_challenge(
    value: object,
    source: _RetainedDirectSource,
    configuration: ApprovalHostConfiguration,
) -> ApprovalChallenge:
    try:
        if type(value) is not ApprovalChallenge:
            raise ValueError("pending challenge type is invalid")
        challenge = parse_approval_challenge_wire(
            value.canonical_bytes, configuration=configuration
        )
        if (
            challenge.kind is not source.approval_kind
            or challenge.expires_at > source.action.plan.expires_at
        ):
            raise ValueError("pending challenge facts are invalid")
        if source.approval_kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN:
            if (
                challenge.action is None
                or canonical_bytes(challenge.action.model_dump(mode="json"))
                != source.canonical_action
            ):
                raise ValueError("pending pause action is invalid")
        elif source.approval_kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN:
            if (
                challenge.ga4_action is None
                or canonical_bytes(challenge.ga4_action.model_dump(mode="json", warnings="error"))
                != source.canonical_action
            ):
                raise ValueError("pending GA4 action is invalid")
        else:  # pragma: no cover - source revalidation fixes the closed kinds
            raise ValueError("pending challenge kind is invalid")
        return challenge
    except Exception:
        raise _pending_invalid() from None


def _revalidate_pending_request(
    value: object,
    *,
    source: _RetainedDirectSource,
    challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    metadata_digest: str,
) -> NativeApprovalBridgeRequestV1:
    try:
        if type(value) is not NativeApprovalBridgeRequestV1:
            raise ValueError("pending request type is invalid")
        request = decode_native_approval_bridge_request_v1(
            value.canonical_bytes,
            configuration=configuration,
            expected_metadata_digest=metadata_digest,
        )
        request_challenge = object.__getattribute__(
            request, "_NativeApprovalBridgeRequestV1__canonical_challenge"
        )
        expected_source_context = derive_native_approval_bridge_source_context_digest_v1(
            workspace_profile_ref=source.profile_ref,
            source_plan_record_digest=source.record_digest,
            checkpoint_digest=source.checkpoint_digest,
        )
        if (
            type(request_challenge) is not bytes
            or request_challenge != challenge.canonical_bytes
            or request.kind is not source.approval_kind
            or request.source_context_digest != expected_source_context
        ):
            raise ValueError("pending request binding is invalid")
        return request
    except Exception:
        raise _pending_invalid() from None


def _checked_compiled_binding(value: object) -> _CompiledInstalledApprovalBridgeBinding:
    try:
        if type(value) is not _CompiledInstalledApprovalBridgeBinding:
            raise ValueError("binding type is invalid")
        issuer = object.__getattribute__(value, "_CompiledInstalledApprovalBridgeBinding__issuer")
        configuration = object.__getattribute__(
            value, "_CompiledInstalledApprovalBridgeBinding__configuration"
        )
        metadata_fields = object.__getattribute__(
            value, "_CompiledInstalledApprovalBridgeBinding__metadata_fields"
        )
        metadata_digest = object.__getattribute__(
            value, "_CompiledInstalledApprovalBridgeBinding__metadata_digest"
        )
        protocol_configuration_digest = object.__getattribute__(
            value, "_CompiledInstalledApprovalBridgeBinding__protocol_configuration_digest"
        )
        if (
            issuer is not _ISSUER
            or type(configuration) is not ApprovalHostConfiguration
            or type(metadata_digest) is not str
            or _DIGEST.fullmatch(metadata_digest) is None
            or type(protocol_configuration_digest) is not str
            or _DIGEST.fullmatch(protocol_configuration_digest) is None
        ):
            raise ValueError("binding fields are invalid")
        metadata = _rebuild_generated_metadata(metadata_fields)
        if hashlib.sha256(canonical_bytes(metadata)).hexdigest() != metadata_digest:
            raise ValueError("binding metadata changed")
        metadata_configuration = _configuration_from_generated_metadata(metadata)
        checked_configuration = ApprovalHostConfiguration(
            host_ref=configuration.host_ref,
            signing_key_ref=configuration.signing_key_ref,
            maximum_challenge_bytes=configuration.maximum_challenge_bytes,
            maximum_challenge_lifetime_seconds=configuration.maximum_challenge_lifetime_seconds,
            maximum_issued_at_skew_seconds=configuration.maximum_issued_at_skew_seconds,
            maximum_subplans=configuration.maximum_subplans,
            maximum_policy_items=configuration.maximum_policy_items,
        )
        if (
            checked_configuration.to_data() != metadata_configuration.to_data()
            or checked_configuration.configuration_digest != protocol_configuration_digest
        ):
            raise ValueError("binding protocol configuration changed")
        return _CompiledInstalledApprovalBridgeBinding(
            _ISSUER,
            configuration=checked_configuration,
            metadata_fields=_generated_metadata_fields(metadata),
            metadata_digest=metadata_digest,
            protocol_configuration_digest=protocol_configuration_digest,
        )
    except Exception:
        raise AdhelmError(
            "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED",
            "A reviewed compiled installed approval bridge binding is not available.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _binding_facts(
    value: _CompiledInstalledApprovalBridgeBinding,
) -> tuple[ApprovalHostConfiguration, str]:
    """Return copied fixed facts only after the module-issued binding check."""
    checked = _checked_compiled_binding(value)
    try:
        configuration = object.__getattribute__(
            checked, "_CompiledInstalledApprovalBridgeBinding__configuration"
        )
        metadata_digest = object.__getattribute__(
            checked, "_CompiledInstalledApprovalBridgeBinding__metadata_digest"
        )
        protocol_configuration_digest = object.__getattribute__(
            checked, "_CompiledInstalledApprovalBridgeBinding__protocol_configuration_digest"
        )
        if (
            type(configuration) is not ApprovalHostConfiguration
            or type(metadata_digest) is not str
            or type(protocol_configuration_digest) is not str
            or configuration.configuration_digest != protocol_configuration_digest
        ):
            raise ValueError("binding facts are invalid")
        return configuration, metadata_digest
    except Exception:
        raise AdhelmError(
            "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED",
            "A reviewed compiled installed approval bridge binding is not available.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _rebuild_generated_metadata(value: object) -> dict[str, object]:
    """Rebuild the exact static 18-field metadata tuple without coercion."""
    if type(value) is not tuple or len(value) != len(_GENERATED_METADATA_FIELD_ORDER):
        raise ValueError("generated metadata fields are invalid")
    result: dict[str, object] = {}
    for expected_key, pair in zip(_GENERATED_METADATA_FIELD_ORDER, value, strict=True):
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not str
            or pair[0] != expected_key
            or expected_key in result
        ):
            raise ValueError("generated metadata field order is invalid")
        result[expected_key] = pair[1]
    if tuple(result) != _GENERATED_METADATA_FIELD_ORDER:
        raise ValueError("generated metadata keys are invalid")
    _validate_generated_metadata(result)
    return result


def _generated_metadata_fields(value: dict[str, object]) -> tuple[tuple[str, object], ...]:
    """Return the only immutable field ordering retained inside a binding."""
    if tuple(value) != _GENERATED_METADATA_FIELD_ORDER:
        raise ValueError("generated metadata keys are invalid")
    return tuple((key, value[key]) for key in _GENERATED_METADATA_FIELD_ORDER)


def _configuration_from_generated_metadata(
    value: dict[str, object],
) -> ApprovalHostConfiguration:
    try:
        return ApprovalHostConfiguration(
            host_ref=_require_generated_safe_text(value["host_ref"], maximum_bytes=256),
            signing_key_ref=_require_generated_safe_text(
                value["signing_key_ref"], maximum_bytes=256
            ),
            maximum_challenge_bytes=_require_generated_int(
                value["maximum_challenge_bytes"], minimum=1, maximum=65_536
            ),
            maximum_challenge_lifetime_seconds=_require_generated_int(
                value["maximum_challenge_lifetime_seconds"], minimum=1, maximum=300
            ),
            maximum_issued_at_skew_seconds=_require_generated_int(
                value["maximum_issued_at_skew_seconds"], minimum=0, maximum=300
            ),
            maximum_subplans=_require_generated_int(
                value["maximum_subplans"], minimum=1, maximum=6
            ),
            maximum_policy_items=_require_generated_int(
                value["maximum_policy_items"], minimum=1, maximum=64
            ),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("generated protocol configuration is invalid") from None


def _validate_generated_metadata(value: dict[str, object]) -> None:
    if tuple(value) != _GENERATED_METADATA_FIELD_ORDER:
        raise ValueError("generated metadata keys are invalid")
    if _require_generated_int(value["schema_version"], minimum=1, maximum=1) != 1:
        raise ValueError("generated metadata schema is invalid")
    _require_generated_safe_text(value["host_ref"], maximum_bytes=256)
    _require_generated_safe_text(value["signing_key_ref"], maximum_bytes=256)
    _require_generated_bundle_identifier(value["mach_service_name"])
    client_bundle_identifier = _require_generated_bundle_identifier(
        value["client_bundle_identifier"]
    )
    _require_generated_team_identifier(value["client_team_identifier"])
    host_bundle_identifier = _require_generated_bundle_identifier(value["host_bundle_identifier"])
    host_team_identifier = _require_generated_team_identifier(value["host_team_identifier"])
    if client_bundle_identifier == host_bundle_identifier:
        raise ValueError("generated metadata identities are invalid")
    _require_generated_tag_hex(value["signing_key_application_tag_hex"])
    _require_generated_access_group(value["signing_key_access_group"], host_team_identifier)
    if type(value["key_storage"]) is not str or value["key_storage"] not in _GENERATED_KEY_STORAGES:
        raise ValueError("generated metadata storage is invalid")
    _require_generated_fixed_ascii(value["provenance_service"], maximum_bytes=256)
    _require_generated_fixed_ascii(value["provenance_account"], maximum_bytes=256)
    _configuration_from_generated_metadata(value)


def _require_generated_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("generated digest is invalid")
    return value


def _require_generated_safe_text(value: object, *, maximum_bytes: int) -> str:
    if type(value) is not str:
        raise ValueError("generated text is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("generated text is invalid") from None
    if (
        not value
        or len(encoded) > maximum_bytes
        or any(ord(item) < 32 or ord(item) == 127 for item in value)
    ):
        raise ValueError("generated text is invalid")
    return value


def _require_generated_fixed_ascii(value: object, *, maximum_bytes: int) -> str:
    result = _require_generated_safe_text(value, maximum_bytes=maximum_bytes)
    try:
        result.encode("ascii")
    except UnicodeError:
        raise ValueError("generated ASCII selector is invalid") from None
    lowered = result.lower()
    if (
        "*" in result
        or "${" in result
        or "placeholder" in lowered
        or "todo" in lowered
        or any(ord(item) < 33 or ord(item) > 126 for item in result)
    ):
        raise ValueError("generated ASCII selector is invalid")
    return result


def _require_generated_bundle_identifier(value: object) -> str:
    result = _require_generated_fixed_ascii(value, maximum_bytes=255)
    labels = result.split(".")
    if len(labels) < 3 or any(
        not label
        or len(label.encode("ascii")) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(not (item.isascii() and (item.isalnum() or item == "-")) for item in label)
        for label in labels
    ):
        raise ValueError("generated bundle identifier is invalid")
    return result


def _require_generated_team_identifier(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 10
        or any(not ("A" <= item <= "Z" or "0" <= item <= "9") for item in value)
    ):
        raise ValueError("generated team identifier is invalid")
    return value


def _require_generated_tag_hex(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 256
        or len(value) % 2
        or _TAG_HEX.fullmatch(value) is None
    ):
        raise ValueError("generated key tag is invalid")
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        raise ValueError("generated key tag is invalid") from None
    if not decoded or len(decoded) > 128:
        raise ValueError("generated key tag is invalid")
    return value


def _require_generated_access_group(value: object, host_team_identifier: str) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise ValueError("generated access group is invalid")
    prefix = f"{host_team_identifier}."
    if not value.startswith(prefix):
        raise ValueError("generated access group is invalid")
    _require_generated_bundle_identifier(value[len(prefix) :])


def _require_generated_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("generated integer is invalid")
    return value


def _select_retained_direct_source(ledger: Ledger, record_digest: object) -> _RetainedDirectSource:
    checkpoint = _verified_checkpoint(ledger)
    record = _selected_source_record(checkpoint, record_digest)
    return _retained_source_from_record(
        checkpoint, record, record_digest=record_digest, profile_ref=ledger.profile_ref
    )


def _reload_and_compare_retained_source(ledger: Ledger, expected: _RetainedDirectSource) -> None:
    checkpoint = _verified_checkpoint(ledger)
    _require_checkpoint_extension(expected.stream_prefixes, checkpoint)
    record = _selected_source_record(checkpoint, expected.record_digest)
    actual = _retained_source_from_record(
        checkpoint,
        record,
        record_digest=expected.record_digest,
        profile_ref=ledger.profile_ref,
    )
    if (
        actual.record_kind != expected.record_kind
        or actual.approval_kind is not expected.approval_kind
        or actual.profile_ref != expected.profile_ref
        or actual.canonical_action != expected.canonical_action
    ):
        raise _source_changed()


def _verified_checkpoint(ledger: Ledger) -> dict[str, Any]:
    try:
        checkpoint = ledger.checkpoint()
        if type(checkpoint) is not dict or checkpoint.get("status") != "VERIFIED_CHECKPOINT":
            raise ValueError("checkpoint status")
        if (
            type(checkpoint.get("checkpoint_digest")) is not str
            or _DIGEST.fullmatch(checkpoint["checkpoint_digest"]) is None
        ):
            raise ValueError("checkpoint digest")
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
        raise AdhelmError(
            "NATIVE_APPROVAL_BRIDGE_SOURCE_UNVERIFIED",
            "The retained local approval source could not be verified.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Inspect canonical ledger history before starting a fresh review.",
        ) from None


def _capture_stream_prefixes(checkpoint: dict[str, Any]) -> tuple[_StreamPrefix, ...]:
    try:
        streams = cast(dict[str, Any], checkpoint["streams"])
        records_by_stream = cast(dict[str, Any], checkpoint["records_by_stream"])
        prefixes: list[_StreamPrefix] = []
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
                _StreamPrefix(
                    stream=cast(Literal["changes", "creative", "performance"], stream),
                    stream_id=stream_id,
                    high_watermark=high_watermark,
                    head_digest=head_digest,
                )
            )
        return tuple(prefixes)
    except Exception:
        raise _source_unverified() from None


def _require_checkpoint_extension(
    expected: tuple[_StreamPrefix, ...], checkpoint: dict[str, Any]
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


def _selected_source_record(checkpoint: dict[str, Any], record_digest: object) -> dict[str, Any]:
    try:
        if type(record_digest) is not str or _DIGEST.fullmatch(record_digest) is None:
            raise ValueError("record digest")
        changes = checkpoint["records_by_stream"]["changes"]
        if type(changes) is not list:
            raise ValueError("changes")
        rows = [
            row
            for row in changes
            if type(row) is dict
            and row.get("record_digest") == record_digest
            and row.get("record_kind") in {"EXECUTION_PLAN", "GA4_KEY_EVENT_CREATE_PLAN"}
        ]
        if len(rows) != 1:
            raise ValueError("source selection")
        return rows[0]
    except Exception:
        raise _source_invalid() from None


def _retained_source_from_record(
    checkpoint: dict[str, Any],
    record: dict[str, Any],
    *,
    record_digest: object,
    profile_ref: object,
) -> _RetainedDirectSource:
    try:
        if type(record_digest) is not str or record.get("record_digest") != record_digest:
            raise ValueError("record digest")
        record_kind = record.get("record_kind")
        payload = record.get("payload")
        if type(payload) is not dict:
            raise ValueError("record payload")
        action: OpenAIPausePlannedAction | Ga4KeyEventCreatePlannedAction
        if record_kind == "EXECUTION_PLAN":
            openai_retained = OpenAIPauseExecutionPlanRecord.model_validate_json(
                canonical_bytes(payload)
            )
            action = revalidate_openai_pause_action(openai_retained.action)
            approval_kind = ApprovalChallengeKind.OPENAI_PAUSE_PLAN
            typed_kind: Literal["EXECUTION_PLAN", "GA4_KEY_EVENT_CREATE_PLAN"] = "EXECUTION_PLAN"
            canonical_action = canonical_bytes(action.model_dump(mode="json"))
        elif record_kind == "GA4_KEY_EVENT_CREATE_PLAN":
            ga4_retained = revalidate_ga4_key_event_create_plan_record(payload)
            action = ga4_retained.action
            approval_kind = ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
            typed_kind = "GA4_KEY_EVENT_CREATE_PLAN"
            canonical_action = canonical_bytes(action.model_dump(mode="json", warnings="error"))
        else:
            raise ValueError("record kind")
        action_profile_ref = action.plan.profile_ref
        if (
            type(profile_ref) is not str
            or type(action_profile_ref) is not str
            or action_profile_ref != profile_ref
        ):
            raise ValueError("workspace profile")
        checkpoint_digest = checkpoint.get("checkpoint_digest")
        if type(checkpoint_digest) is not str or _DIGEST.fullmatch(checkpoint_digest) is None:
            raise ValueError("checkpoint digest")
        return _RetainedDirectSource(
            record_digest=record_digest,
            record_kind=typed_kind,
            approval_kind=approval_kind,
            profile_ref=action_profile_ref,
            action=action,
            canonical_action=canonical_action,
            checkpoint_digest=checkpoint_digest,
            stream_prefixes=_capture_stream_prefixes(checkpoint),
        )
    except Exception:
        raise _source_invalid() from None


def _kernel_now(kernel: SafetyKernel) -> datetime:
    try:
        value = kernel.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "NATIVE_APPROVAL_BRIDGE_CLOCK_INVALID",
            "The local approval bridge requires an aware trusted clock.",
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


def _source_invalid() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_SOURCE_INVALID",
        "A retained OpenAI pause or GA4 CREATE plan record is required.",
        exit_code=ExitCode.PRECONDITION,
    )


def _source_unverified() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_SOURCE_UNVERIFIED",
        "The retained local approval source could not be verified.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect canonical ledger history before starting a fresh review.",
    )


def _source_changed() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_SOURCE_CHANGED",
        "The retained local approval source changed during the approval exchange.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect canonical ledger history and start a fresh review; do not retry this exchange."
        ),
    )


def _pending_invalid() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_PENDING_INVALID",
        "The private approval bridge request is invalid.",
        exit_code=ExitCode.PRECONDITION,
    )


def _pending_consumed() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_PENDING_CONSUMED",
        "This private approval bridge request was already attempted.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Start a fresh approval review; never reuse a bridge request.",
    )


def _post_consumption_unverified() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_ACCEPTANCE_UNVERIFIED",
        "Native approval may have been consumed but local acceptance could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect native and canonical local history; start a fresh review and do not retry "
            "this exchange."
        ),
    )


def _transport_unavailable() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_TRANSPORT_UNAVAILABLE",
        "Native approval bridge helper exchange cannot start from the fixed local binding.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect the installed approval bridge binding before a fresh review.",
    )

"""Internal configured GA4 CREATE execution through exact local approval.

The legacy pinned-receipt route remains available for its existing internal
callers. The installed-approval sibling owns neither a receipt, challenge,
trust pin, key, grant, transport, bearer, nor adapter: it uses the private
retained-plan coordinator after ordinary GA4 preflight has completed. Neither
route by itself proves that an installed approval product exists. The sibling
has a separately guarded development CLI entry.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator

from adhelm import release_policy
from adhelm._installed_native_approval_bridge import _request_and_record_installed_native_approval
from adhelm.approval_acceptance import (
    AcceptedNativeApproval,
    PinnedApprovalTrust,
    accept_approval_receipt,
)
from adhelm.approval_protocol import ApprovalChallenge, ApprovalChallengeKind
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import configuration_transaction
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_execution_observation import validate_ga4_admin_edit_execution_observation
from adhelm.ga4_create_execution_sources import validate_current_ga4_create_sources
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput, _revalidate_candidate_input
from adhelm.ledger import Ledger
from adhelm.models import StrictModel
from adhelm.providers.ga4_create_contract import Ga4KeyEventCreatePlannedAction
from adhelm.safety import DirectGrant, ExecutionResult, SafetyKernel
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan
from adhelm.strict_json import validate_json_value

_INSTALLED_GA4_CREATE_LOGICAL_GRANTOR_REF: Final = (
    "logical:adhelm-installed-approval-bridge-ga4-create-v1"
)


class Ga4KeyEventCreateExecutionRequest(StrictModel):
    schema_version: Literal[1] = 1
    plan_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    fresh: Ga4KeyEventCandidateInput

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("fresh", mode="before")
    @classmethod
    def complete_fresh_source(cls, value: object) -> Ga4KeyEventCandidateInput:
        return _revalidate_candidate_input(value)


@dataclass(frozen=True, slots=True, repr=False)
class _Ga4CreateExecutionContext:
    """Checked local facts retained only across one approval handoff."""

    directory: Path
    request: Ga4KeyEventCreateExecutionRequest
    ledger: Ledger
    action: Ga4KeyEventCreatePlannedAction
    fingerprint_key: bytes
    kernel: SafetyKernel


def _now() -> datetime:
    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        current = _now()
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("clock")
        return current.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID", "CREATE execution requires an aware local clock."
        ) from None


def _checked_execution_request(
    directory: object, request: object
) -> tuple[Path, Ga4KeyEventCreateExecutionRequest]:
    try:
        if (
            not isinstance(directory, Path)
            or type(request) is not Ga4KeyEventCreateExecutionRequest
        ):
            raise ValueError("execution input")
        checked = validate_json_value(
            Ga4KeyEventCreateExecutionRequest,
            request.model_dump(mode="json", warnings="error"),
        )
        return directory, checked
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_EXECUTION_INPUT", "Execution input is invalid."
        ) from None


def _action_bytes(action: Ga4KeyEventCreatePlannedAction) -> bytes:
    return canonical_bytes(action.model_dump(mode="json", warnings="error"))


def _require_current_configuration(directory: Path, action: Ga4KeyEventCreatePlannedAction) -> str:
    """Require the approved local OAuth EDIT configuration without OAuth I/O."""
    binding = action.execution_binding
    configuration, profile, target, expected = validate_ga4_admin_edit_execution_observation(
        directory,
        binding.auth_profile_ref,
        stream_id=binding.target.web_stream_client_ref,
        expected_configuration_digest=binding.configuration_digest,
    )
    if (
        target != binding.target
        or digest(profile) != binding.profile_configuration_digest
        or digest(configuration) != binding.configuration_digest
        or type(expected) is not str
        or expected != binding.configuration_digest
    ):
        raise AdhelmError(
            "CONFIGURATION_DRIFT", "CREATE configuration differs from the approved plan."
        )
    return expected


def _require_no_unresolved_execution_history(ledger: Ledger) -> None:
    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED", "Resolve outstanding execution history before approval acceptance."
        )


def _current_fingerprint_key(directory: Path, configuration_digest: str) -> bytes:
    """Read the current local key after a short configuration continuity check."""
    with configuration_transaction(directory) as transaction:
        transaction.require_unchanged(configuration_digest)
        key = export_key()
    if type(key) is not bytes:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "CREATE execution requires the current private local fingerprint key.",
            exit_code=ExitCode.PRECONDITION,
        )
    return key


def _preflight_ga4_create_execution(
    directory: object, request: object
) -> _Ga4CreateExecutionContext:
    """Perform all pre-approval GA4 checks without an approval or OAuth exchange."""
    checked_directory, checked_request = _checked_execution_request(directory, request)
    ledger = Ledger(checked_directory)
    action, _ = load_ga4_key_event_create_plan(ledger, checked_request.plan_record_digest)
    configuration_digest = _require_current_configuration(checked_directory, action)
    _require_no_unresolved_execution_history(ledger)
    fingerprint_key = _current_fingerprint_key(checked_directory, configuration_digest)
    validate_current_ga4_create_sources(
        ledger,
        action,
        fresh=checked_request.fresh,
        fingerprint_key=fingerprint_key,
        now=_checked_now(),
    )
    kernel = SafetyKernel(checked_directory, ledger, clock=_checked_now)
    return _Ga4CreateExecutionContext(
        directory=checked_directory,
        request=checked_request,
        ledger=ledger,
        action=action,
        fingerprint_key=fingerprint_key,
        kernel=kernel,
    )


def _require_exact_ga4_direct_grant(
    value: object, action: Ga4KeyEventCreatePlannedAction
) -> DirectGrant:
    try:
        if (
            type(value) is not AcceptedNativeApproval
            or value.kind is not ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
            or type(value.direct_grant) is not DirectGrant
            or value.policy is not None
            or value.direct_grant.plan_digest != action.plan.plan_digest
            or value.direct_grant.subplan_digests != action.plan.subplan_digests
        ):
            raise ValueError("approval result")
        return value.direct_grant
    except Exception:
        raise AdhelmError(
            "GA4_CREATE_APPROVAL_REQUIRED",
            "Receipt acceptance did not produce the exact CREATE grant.",
            exit_code=ExitCode.AUTHORIZATION,
        ) from None


def _recheck_after_approval(
    context: _Ga4CreateExecutionContext,
) -> tuple[Ga4KeyEventCreatePlannedAction, bytes]:
    """Recheck local sources before OAuth after a claimed approval may exist."""
    action, _ = load_ga4_key_event_create_plan(context.ledger, context.request.plan_record_digest)
    if _action_bytes(action) != _action_bytes(context.action):
        raise AdhelmError(
            "PLAN_BINDING_MISMATCH", "The retained CREATE action changed after approval."
        )
    configuration_digest = _require_current_configuration(context.directory, action)
    fingerprint_key = _current_fingerprint_key(context.directory, configuration_digest)
    if not hmac.compare_digest(fingerprint_key, context.fingerprint_key):
        raise AdhelmError(
            "FINGERPRINT_KEY_CHANGED",
            "The local fingerprint key changed after approval.",
            exit_code=ExitCode.PRECONDITION,
        )
    _require_no_unresolved_execution_history(context.ledger)
    validate_current_ga4_create_sources(
        context.ledger,
        action,
        fresh=context.request.fresh,
        fingerprint_key=fingerprint_key,
        now=_checked_now(),
    )
    return action, fingerprint_key


def _execute_approved_ga4_create(
    context: _Ga4CreateExecutionContext, accepted: object
) -> ExecutionResult:
    direct_grant = _require_exact_ga4_direct_grant(accepted, context.action)
    action, fingerprint_key = _recheck_after_approval(context)
    # A shape-valid copied grant can still change its authorization or grantor
    # identity. Require the exact durable approval event before touching OAuth;
    # the kernel repeats this same check under its coordinator locks before
    # APPLY_STARTED. This read occurs outside every append/configuration lock.
    context.kernel._ensure_durable_authorization(action.plan, direct_grant)

    # Import after every local approval, source, configuration, and key check.
    # The configured factory owns the fresh OAuth handoff and fixed HTTP lifetime.
    from adhelm.providers.ga4_admin_key_event_create_adapter import _configured_ga4_create_adapter

    with _configured_ga4_create_adapter(
        context.directory,
        action,
        context.ledger,
        fresh=context.request.fresh,
        fingerprint_key=fingerprint_key,
        clock=_checked_now,
    ) as adapter:
        if _action_bytes(adapter.action) != _action_bytes(action):
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH", "The configured adapter changed the approved action."
            )
        return context.kernel.execute(action.plan, direct_grant, {"ga4_admin": adapter})


def _checked_legacy_challenge(value: object) -> ApprovalChallenge:
    try:
        if type(value) is not ApprovalChallenge:
            raise ValueError("challenge")
        # Receipt acceptance reparses canonical bytes. This is only an early
        # kind gate to preserve the historical pinned-route input contract.
        if value.kind is not ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN:
            raise ValueError("challenge kind")
        return value
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_EXECUTION_INPUT",
            "An exact GA4 CREATE request and challenge are required.",
        ) from None


def execute_retained_ga4_key_event_create(
    directory: Path,
    request: Ga4KeyEventCreateExecutionRequest,
    *,
    receipt: bytes,
    expected_challenge: ApprovalChallenge,
    trust: PinnedApprovalTrust,
) -> ExecutionResult:
    """Accept one exact legacy pinned receipt and execute through SafetyKernel.

    This internal route retains its historical receipt/challenge/trust API for
    synthetic and future explicitly pinned integrations. It is not the installed
    bridge resolver: that sibling below owns its challenge and claim privately.
    """
    release_policy.require_writes_released()
    checked_challenge = _checked_legacy_challenge(expected_challenge)
    context = _preflight_ga4_create_execution(directory, request)
    accepted = accept_approval_receipt(
        context.kernel,
        context.ledger,
        receipt=receipt,
        expected_challenge=checked_challenge,
        trust=trust,
        source_plan_record_digest=context.request.plan_record_digest,
    )
    return _execute_approved_ga4_create(context, accepted)


def execute_retained_ga4_key_event_create_with_installed_approval(
    directory: Path,
    request: Ga4KeyEventCreateExecutionRequest,
) -> ExecutionResult:
    """Execute one retained GA4 CREATE plan through the private installed bridge.

    The checked-in compiled binding fails closed before a helper exchange. This
    surface accepts no receipt, challenge, trust pin, key, grant, candidate,
    transport, bearer, identity, inventory, adapter, or provider request.
    """
    release_policy.require_writes_released()
    context = _preflight_ga4_create_execution(directory, request)
    accepted = _request_and_record_installed_native_approval(
        context.kernel,
        context.ledger,
        source_plan_record_digest=context.request.plan_record_digest,
        grantor_ref=_INSTALLED_GA4_CREATE_LOGICAL_GRANTOR_REF,
    )
    return _execute_approved_ga4_create(context, accepted)

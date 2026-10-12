"""Private installed-approval execution of one retained Meta campaign PAUSE.

The workflow has no receipt, key, candidate, adapter, transport, clock, or
credential input.  It first proves that the separate compiled Meta V2 route is
available, then keeps retained-plan, local configuration, native approval, and
provider execution in a fixed sequence.  The checked-in route remains absent,
so normal source fails before local history, configuration, protected-store,
helper, or provider activity.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError, field_validator

from adhelm import release_policy
from adhelm._compiled_meta_campaign_pause_approval_bridge_v2 import (
    _request_and_record_installed_meta_campaign_pause_approval_v2,
    _require_compiled_meta_campaign_pause_approval_bridge_v2_ready,
)
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import ApprovalChallengeKind
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.models import (
    Configuration,
    MetaAccountTokenProfile,
    StrictModel,
    validate_configuration,
)
from adhelm.providers.meta_campaign_pause_adapter import _configured_meta_campaign_pause_adapter
from adhelm.providers.meta_pause_contract import (
    META_CAMPAIGN_PAUSE_SCOPE,
    MetaCampaignPausePlannedAction,
    revalidate_meta_campaign_pause_planned_action,
)
from adhelm.safety import AccountIdentity, DirectGrant, ExecutionResult, OperationKind, SafetyKernel
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan
from adhelm.strict_json import validate_json_value

_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


class MetaCampaignPauseExecutionRequest(StrictModel):
    """The closed selection for one already-retained Meta PAUSE plan."""

    schema_version: Literal[1] = 1
    plan_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("plan_record_digest", mode="before")
    @classmethod
    def exact_record_digest(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("plan_record_digest must be a lowercase SHA-256 digest")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class _MetaCampaignPauseExecutionContext:
    """Locally checked facts held across one native approval handoff."""

    directory: Path
    request: MetaCampaignPauseExecutionRequest
    ledger: Ledger
    action: MetaCampaignPausePlannedAction
    action_bytes: bytes
    configuration_digest: str
    profile_digest: str
    kernel: SafetyKernel


def _now() -> datetime:
    """Private UTC clock seam for deterministic fake-only tests."""

    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        value = _now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID",
            "Meta campaign PAUSE execution requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_input() -> AdhelmError:
    return AdhelmError(
        "INVALID_META_CAMPAIGN_PAUSE_EXECUTION_INPUT",
        "Meta campaign PAUSE execution input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _checked_execution_request(
    directory: object, request: object
) -> tuple[Path, MetaCampaignPauseExecutionRequest]:
    """Reparse constructed values before any local side effect."""

    try:
        if (
            not isinstance(directory, Path)
            or type(request) is not MetaCampaignPauseExecutionRequest
        ):
            raise ValueError("execution input")
        checked = validate_json_value(
            MetaCampaignPauseExecutionRequest,
            request.model_dump(mode="json", warnings="error"),
        )
        return directory, checked
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_input() from None


def _action_bytes(action: MetaCampaignPausePlannedAction) -> bytes:
    return canonical_bytes(revalidate_meta_campaign_pause_planned_action(action).to_data())


def _require_current_source(
    ledger: Ledger,
    record_digest: str,
    *,
    expected_action_bytes: bytes | None = None,
) -> MetaCampaignPausePlannedAction:
    """Load and bind the exact retained action before local approval or egress."""

    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    checked = revalidate_meta_campaign_pause_planned_action(action)
    encoded = _action_bytes(checked)
    if expected_action_bytes is not None and not hmac.compare_digest(
        encoded, expected_action_bytes
    ):
        raise AdhelmError(
            "PLAN_BINDING_MISMATCH",
            "The retained Meta campaign PAUSE action changed during execution.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile and approve a new exact Meta campaign PAUSE plan.",
        )
    try:
        subplans = checked.plan.subplans
        if (
            checked.workspace_binding.workspace_profile_ref != ledger.profile_ref
            or checked.plan.profile_ref != ledger.profile_ref
            or len(subplans) != 1
            or subplans[0].provider != "meta_ads"
            or subplans[0].operation is not OperationKind.PAUSE
            or type(subplans[0].account) is not AccountIdentity
            or subplans[0].account.account_ref != checked.account_binding.account_ref
            or subplans[0].account.stable_account_id != checked.account_binding.account_client_ref
            or subplans[0].account.auth_profile_ref != checked.execution_binding.profile_ref
            or subplans[0].account.principal_ref != checked.execution_binding.principal_ref
            or subplans[0].account.client_application_ref
            != checked.execution_binding.client_application_ref
            or subplans[0].account.scopes != (META_CAMPAIGN_PAUSE_SCOPE,)
        ):
            raise ValueError("workspace or plan binding")
    except Exception:
        raise AdhelmError(
            "WORKSPACE_BINDING_REQUIRED",
            "Meta campaign PAUSE execution requires the exact retained local workspace.",
            exit_code=ExitCode.PRECONDITION,
        ) from None
    current = _checked_now()
    if (
        checked.plan.expires_at <= current
        or checked.execution_binding.expires_at <= current
        or checked.execution_binding.data_access_expires_at <= current
        or any(item.expires_at <= current for item in checked.plan.subplans)
    ):
        raise AdhelmError(
            "PLAN_EXPIRED",
            "The retained Meta campaign PAUSE plan has expired.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Compile and approve a new exact Meta campaign PAUSE plan.",
        )
    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED",
            "Resolve outstanding execution history before Meta campaign PAUSE approval.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Reconcile retained execution history before requesting a new approval.",
        )
    return checked


def _selected_edit_profile(
    configuration: Configuration, action: MetaCampaignPausePlannedAction
) -> MetaAccountTokenProfile:
    """Return the sole customer-declared EDIT profile without reading its bundle."""

    execution = action.execution_binding
    selected = [
        profile for profile in configuration.profiles if profile.profile_id == execution.profile_ref
    ]
    if (
        len(selected) != 1
        or type(selected[0]) is not MetaAccountTokenProfile
        or selected[0].purpose != "EDIT"
        or selected[0].required_scope != META_CAMPAIGN_PAUSE_SCOPE
        or selected[0].credential_source != "OS_META_TOKEN"
    ):
        raise AdhelmError(
            "META_TOKEN_EDIT_PROFILE_REQUIRED",
            "Meta campaign PAUSE execution requires a configured OS-store Meta EDIT profile.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Configure the exact Meta Ads token EDIT profile before execution.",
        )
    return selected[0]


def _require_current_configuration(
    directory: Path, action: MetaCampaignPausePlannedAction
) -> tuple[str, str]:
    """Bind only local EDIT configuration facts; never read a credential here."""

    configuration = validate_configuration(load_configuration(directory))
    profile = _selected_edit_profile(configuration, action)
    execution = action.execution_binding
    account = action.account_binding
    configuration_digest = digest(configuration)
    profile_digest = digest(profile)
    if (
        not hmac.compare_digest(configuration_digest, execution.configuration_digest)
        or not hmac.compare_digest(profile_digest, execution.profile_configuration_digest)
        or profile.profile_id != execution.profile_ref
        or profile.account_ref != account.account_client_ref.removeprefix("act_")
        or profile.client_application_ref != execution.client_application_ref
        or profile.currency != account.currency
        or profile.timezone != account.timezone
        or profile.required_scope != execution.required_scope
    ):
        raise AdhelmError(
            "CONFIGURATION_DRIFT",
            "Current Meta EDIT configuration differs from the retained pause action.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Review local Meta configuration and compile a new exact plan.",
        )
    return configuration_digest, profile_digest


def _preflight_execution(directory: object, request: object) -> _MetaCampaignPauseExecutionContext:
    """Perform every local check before native review or protected credentials."""

    checked_directory, checked_request = _checked_execution_request(directory, request)
    # This must remain the first runtime boundary after pure request parsing.
    _require_compiled_meta_campaign_pause_approval_bridge_v2_ready()
    ledger = Ledger(checked_directory)
    action = _require_current_source(ledger, checked_request.plan_record_digest)
    configuration_digest, profile_digest = _require_current_configuration(checked_directory, action)
    kernel = SafetyKernel(checked_directory, ledger, clock=_checked_now)
    return _MetaCampaignPauseExecutionContext(
        directory=checked_directory,
        request=checked_request,
        ledger=ledger,
        action=action,
        action_bytes=_action_bytes(action),
        configuration_digest=configuration_digest,
        profile_digest=profile_digest,
        kernel=kernel,
    )


def _require_exact_meta_direct_grant(
    value: object, action: MetaCampaignPausePlannedAction
) -> DirectGrant:
    """Require the native V2 handoff's exact direct Meta plan grant, never policy."""

    try:
        if (
            type(value) is not AcceptedNativeApproval
            or value.kind is not ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
            or type(value.direct_grant) is not DirectGrant
            or value.policy is not None
            or type(value.receipt_claim_record_digest) is not str
            or _DIGEST.fullmatch(value.receipt_claim_record_digest) is None
            or type(value.receipt_digest) is not str
            or _DIGEST.fullmatch(value.receipt_digest) is None
        ):
            raise ValueError("accepted approval")
        grant = validate_json_value(
            DirectGrant, value.direct_grant.model_dump(mode="json", warnings="error")
        )
        if (
            grant.plan_digest != action.plan.plan_digest
            or grant.subplan_digests != action.plan.subplan_digests
        ):
            raise ValueError("grant binding")
        return grant
    except Exception:
        raise AdhelmError(
            "META_CAMPAIGN_PAUSE_APPROVAL_REQUIRED",
            "Native approval did not produce the exact Meta campaign PAUSE direct grant.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new exact Meta campaign PAUSE approval review.",
        ) from None


def _recheck_after_approval(
    context: _MetaCampaignPauseExecutionContext,
) -> MetaCampaignPausePlannedAction:
    """Reload source and configuration before protected-store or provider work."""

    action = _require_current_source(
        context.ledger,
        context.request.plan_record_digest,
        expected_action_bytes=context.action_bytes,
    )
    configuration_digest, profile_digest = _require_current_configuration(context.directory, action)
    if not hmac.compare_digest(
        configuration_digest, context.configuration_digest
    ) or not hmac.compare_digest(profile_digest, context.profile_digest):
        raise AdhelmError(
            "CONFIGURATION_DRIFT",
            "Local Meta EDIT configuration changed after native approval.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile and approve a new exact Meta campaign PAUSE plan.",
        )
    return action


def _execute_approved_meta_campaign_pause(
    context: _MetaCampaignPauseExecutionContext, accepted: object
) -> ExecutionResult:
    grant = _require_exact_meta_direct_grant(accepted, context.action)
    action = _recheck_after_approval(context)
    # A native review may finish close to its finite challenge/grant window.
    # Refuse that local expiry before the configured adapter opens protected
    # credentials; ``execute`` repeats this check under its coordinator locks.
    context.kernel._check_expiry(action.plan, grant, _checked_now())
    # Require the exact claimed direct grant before the configured adapter can
    # open its protected credential session. SafetyKernel repeats the check
    # beneath its coordinator locks immediately before APPLY_STARTED.
    context.kernel._ensure_durable_authorization(action.plan, grant)
    with _configured_meta_campaign_pause_adapter(
        context.directory, action, context.ledger
    ) as adapter:
        if not hmac.compare_digest(_action_bytes(adapter.action), _action_bytes(action)):
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "The configured Meta adapter differs from the approved pause action.",
                exit_code=ExitCode.PRECONDITION,
            )
        return context.kernel.execute(action.plan, grant, {"meta_ads": adapter})


def execute_retained_meta_campaign_pause_with_installed_approval(
    directory: Path, request: MetaCampaignPauseExecutionRequest
) -> ExecutionResult:
    """Approve and execute one retained Meta PAUSE through the fixed private V2 route.

    This entry has no fallback to V1 or an arbitrary native/client/provider
    surface.  A missing generated Meta V2 binding fails before retained source,
    configuration, protected credentials, helper construction, or HTTP.
    """

    release_policy.require_writes_released()
    context = _preflight_execution(directory, request)
    accepted = _request_and_record_installed_meta_campaign_pause_approval_v2(
        context.kernel,
        context.ledger,
        source_plan_record_digest=context.request.plan_record_digest,
    )
    return _execute_approved_meta_campaign_pause(context, accepted)


__all__ = [
    "MetaCampaignPauseExecutionRequest",
    "execute_retained_meta_campaign_pause_with_installed_approval",
]

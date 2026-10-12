"""Internal configured OpenAI pause through the installed native approval bridge.

The development checkout has no compiled installed binding. This is a typed
library integration, not an advertised execution command or live-tested adapter.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, field_validator

from adhelm import release_policy
from adhelm._installed_native_approval_bridge import _request_and_record_installed_native_approval
from adhelm.approval_protocol import ApprovalChallengeKind
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import configuration_transaction
from adhelm.credentials import credential, openai_pause_binding_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.models import (
    Configuration,
    OpenAIAccountKeyProfile,
    StrictModel,
    validate_configuration,
)
from adhelm.providers.openai_pause import (
    OpenAIPauseAdapter,
    OpenAIPauseCredentials,
    _credential_binding,
    execute_openai_campaign_pause,
)
from adhelm.providers.openai_pause_contract import OPENAI_PAUSE_HOST, OpenAIPausePlannedAction
from adhelm.safety import ExecutionResult, SafetyKernel
from adhelm.saved_plans import load_openai_pause_plan
from adhelm.strict_json import validate_json_value
from adhelm.transport import SafeHttpClient

# A fixed protocol label, never an assertion of a human's identity.
_INSTALLED_OPENAI_PAUSE_LOGICAL_GRANTOR_REF = (
    "logical:adhelm-installed-approval-bridge-openai-pause-v1"
)


class OpenAIPauseExecutionRequest(StrictModel):
    schema_version: Literal[1] = 1
    plan_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


def _now() -> datetime:
    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        value = _now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID", "Pause execution requires an aware local clock."
        ) from None


def _require_current_source(
    ledger: Ledger, record_digest: str, action: OpenAIPausePlannedAction
) -> None:
    current, _ = load_openai_pause_plan(ledger, record_digest)
    if canonical_bytes(current) != canonical_bytes(action):
        raise AdhelmError("PLAN_BINDING_MISMATCH", "The retained pause source changed.")
    if (
        current.workspace_binding is None
        or current.workspace_binding.workspace_profile_ref != ledger.profile_ref
        or current.plan.profile_ref != ledger.profile_ref
    ):
        raise AdhelmError(
            "WORKSPACE_BINDING_REQUIRED", "Pause execution requires the exact explicit workspace."
        )
    if current.plan.expires_at <= _checked_now():
        raise AdhelmError("PLAN_EXPIRED", "The retained pause plan has expired.")
    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError("RECOVERY_REQUIRED", "Resolve outstanding execution history first.")


def _configuration_digest(directory: Path, action: OpenAIPausePlannedAction) -> str:
    with configuration_transaction(directory) as transaction:
        configuration = validate_configuration(transaction.read())
        _matching_profile(configuration, action)
        expected = digest(configuration)
        transaction.require_unchanged(expected)
        return expected


def _matching_profile(
    configuration: Configuration, action: OpenAIPausePlannedAction
) -> OpenAIAccountKeyProfile:
    selected = [p for p in configuration.profiles if p.profile_id == action.binding.profile_ref]
    if (
        len(selected) != 1
        or type(selected[0]) is not OpenAIAccountKeyProfile
        or selected[0].purpose != "EDIT"
        or selected[0].credential_source != "OS_STORE"
    ):
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_EDIT_PROFILE_REQUIRED",
            "Pause execution requires the configured OS-store OpenAI account-key EDIT profile.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    profile = selected[0]
    assert isinstance(profile, OpenAIAccountKeyProfile)
    binding = action.binding
    if (
        profile.account_ref != binding.account_client_ref
        or profile.currency != binding.account_currency
        or profile.timezone != binding.account_timezone
    ):
        raise AdhelmError(
            "CONFIGURATION_DRIFT", "Current OpenAI configuration differs from the plan."
        )
    return profile


def _current_credentials(
    directory: Path, action: OpenAIPausePlannedAction, expected_configuration_digest: str
) -> OpenAIPauseCredentials:
    # Config first, then private credential-slot locks. The transaction ends
    # before native review or any provider request. Reobserve on every use.
    with configuration_transaction(directory) as transaction:
        configuration = validate_configuration(
            transaction.require_unchanged(expected_configuration_digest)
        )
        profile = _matching_profile(configuration, action)
        value = OpenAIPauseCredentials(
            profile_ref=profile.profile_id,
            secret=credential(profile),
            binding_key=openai_pause_binding_key(profile),
        )
        if not hmac.compare_digest(
            _credential_binding(value), action.binding.credential_binding_ref
        ):
            raise AdhelmError(
                "CREDENTIAL_BINDING_MISMATCH",
                "Current OpenAI credentials differ from the pause plan.",
            )
        transaction.require_unchanged(expected_configuration_digest)
        return value


def execute_retained_openai_pause_with_installed_approval(
    directory: Path, request: OpenAIPauseExecutionRequest
) -> ExecutionResult:
    """Approve one retained pause and execute only through the existing kernel.

    The caller cannot supply a receipt, trust key, grant, adapter, transport or
    credential. Consumed or ambiguous native approval is never retried here.
    """
    release_policy.require_writes_released()
    if not isinstance(directory, Path) or type(request) is not OpenAIPauseExecutionRequest:
        raise AdhelmError(
            "INVALID_OPENAI_PAUSE_EXECUTION_INPUT", "Pause execution input is invalid."
        )
    try:
        checked = validate_json_value(
            OpenAIPauseExecutionRequest, request.model_dump(mode="json", warnings="error")
        )
    except Exception:
        raise AdhelmError(
            "INVALID_OPENAI_PAUSE_EXECUTION_INPUT", "Pause execution input is invalid."
        ) from None
    ledger = Ledger(directory)
    action, _ = load_openai_pause_plan(ledger, checked.plan_record_digest)
    _require_current_source(ledger, checked.plan_record_digest, action)
    expected = _configuration_digest(directory, action)
    _current_credentials(directory, action, expected)
    kernel = SafetyKernel(directory, ledger, clock=_checked_now)
    accepted = _request_and_record_installed_native_approval(
        kernel,
        ledger,
        source_plan_record_digest=checked.plan_record_digest,
        grantor_ref=_INSTALLED_OPENAI_PAUSE_LOGICAL_GRANTOR_REF,
    )
    if (
        accepted.kind is not ApprovalChallengeKind.OPENAI_PAUSE_PLAN
        or accepted.direct_grant is None
        or accepted.direct_grant.plan_digest != action.plan.plan_digest
        or accepted.direct_grant.subplan_digests != action.plan.subplan_digests
        or accepted.policy is not None
    ):
        raise AdhelmError(
            "OPENAI_PAUSE_APPROVAL_REQUIRED",
            "Native approval did not produce the exact pause grant.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    _require_current_source(ledger, checked.plan_record_digest, action)
    _current_credentials(directory, action, expected)
    with httpx.Client(trust_env=False) as client:
        adapter = OpenAIPauseAdapter(
            action,
            SafeHttpClient(client, {OPENAI_PAUSE_HOST}),
            lambda: _current_credentials(directory, action, expected),
            ledger,
            clock=_checked_now,
        )
        return execute_openai_campaign_pause(
            kernel=kernel, action=action, authorization=accepted.direct_grant, adapter=adapter
        )

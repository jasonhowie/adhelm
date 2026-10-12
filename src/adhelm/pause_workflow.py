"""Explicit read-only preparation of a retained OpenAI campaign pause plan."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, field_validator

from adhelm.config import load_configuration
from adhelm.credentials import credential, openai_pause_binding_key
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import (
    Configuration,
    OpenAIAccountKeyProfile,
    SafeRef,
    StrictModel,
    validate_configuration,
)
from adhelm.providers.openai_ads import OpenAIAdsClient
from adhelm.providers.openai_pause import OpenAIPauseCredentials, plan_openai_campaign_pause
from adhelm.providers.openai_pause_contract import OPENAI_PAUSE_HOST, OpenAIPausePlannedAction
from adhelm.transport import SafeHttpClient


class OpenAIPauseRequest(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: SafeRef
    campaign_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    maximum_inventory_records: int = Field(default=500, ge=1, le=5000)
    expires_in_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


def now_utc() -> datetime:
    return datetime.now(UTC)


def checked_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("invalid clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID", "Plan preparation requires an aware local clock."
        ) from None


def pause_configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID", "Local configuration failed validation.", exit_code=2
        ) from None


def prepare_openai_pause(
    directory: Path,
    request: OpenAIPauseRequest,
    *,
    clock: Callable[[], datetime] = now_utc,
) -> tuple[OpenAIPausePlannedAction, Configuration]:
    """Read a complete fixed baseline; never append, authorize, or mutate remotely.

    An EDIT account-key profile binds the key intended for a future guarded pause.
    Its purpose is a local role declaration, not proof of provider-side scopes.
    """
    if not isinstance(directory, Path) or type(request) is not OpenAIPauseRequest:
        raise AdhelmError("INVALID_PLAN_INPUT", "Invalid pause planning input.", exit_code=2)
    try:
        request = OpenAIPauseRequest.model_validate(
            request.model_dump(mode="python", warnings="error")
        )
    except Exception:
        raise AdhelmError(
            "INVALID_PLAN_INPUT", "Invalid pause planning input.", exit_code=2
        ) from None
    configuration = pause_configuration(directory)
    selected = [p for p in configuration.profiles if p.profile_id == request.profile_id]
    if (
        len(selected) != 1
        or type(selected[0]) is not OpenAIAccountKeyProfile
        or selected[0].purpose != "EDIT"
    ):
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_EDIT_PROFILE_REQUIRED",
            "Pause planning requires an explicit OpenAI account-key EDIT profile.",
            exit_code=4,
            next_action="Configure openai-account-key-profile with EDIT purpose.",
        )
    profile = selected[0]
    assert isinstance(profile, OpenAIAccountKeyProfile)
    if profile.credential_source != "OS_STORE":
        raise AdhelmError(
            "OPENAI_PAUSE_BINDING_KEY_CI_UNSUPPORTED",
            "CI pause planning has no configured private binding-key channel.",
            exit_code=4,
        )
    if profile.currency is None:
        raise AdhelmError(
            "EXPECTED_CURRENCY_REQUIRED",
            "Pause planning requires an expected currency.",
            exit_code=2,
        )
    ledger = Ledger(directory)
    ledger.checkpoint()
    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED",
            "Unresolved mutation history must be reconciled before preparing a new pause plan.",
            next_action="Inspect history verify and the retained recovery evidence.",
        )
    expires_at = checked_now(clock) + timedelta(seconds=request.expires_in_seconds)
    secret = credential(profile)
    if (
        type(secret) is not str
        or not secret
        or len(secret) > 16_384
        or any(not 33 <= ord(character) <= 126 for character in secret)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED", "OpenAI Ads authorization is unavailable.", exit_code=4
        )
    credentials = OpenAIPauseCredentials(
        profile_ref=profile.profile_id,
        secret=secret,
        binding_key=openai_pause_binding_key(profile),
    )
    with httpx.Client(trust_env=False) as client:
        transport = SafeHttpClient(client, {OPENAI_PAUSE_HOST})
        action = plan_openai_campaign_pause(
            client=OpenAIAdsClient(transport, lambda: credentials.secret),
            credentials=credentials,
            expected_account_client_ref=profile.account_ref,
            expected_account_currency=profile.currency,
            expected_account_timezone=profile.timezone,
            campaign_client_ref=request.campaign_id,
            maximum_inventory_records=request.maximum_inventory_records,
            expires_at=expires_at,
            workspace_profile_ref=ledger.profile_ref,
            clock=clock,
        )
    if action.plan.expires_at <= checked_now(clock):
        raise AdhelmError(
            "PLAN_EXPIRED", "Pause plan expired during its provider reads.", exit_code=4
        )
    return action, configuration

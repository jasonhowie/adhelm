"""Configured, non-authorizing Meta campaign PAUSE plan preparation.

This module combines the fixed Meta EDIT observation session with the closed
M40 campaign-pause contract and retains one local plan.  It never creates an
authorization, calls a mutation endpoint, invokes native approval, or exposes
a token, generic provider client, or raw provider response.
"""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError, field_validator

from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.meta_ads_token_edit_session import (
    MetaAdsTokenEditSession,
    configured_meta_ads_token_edit_session,
)
from adhelm.models import Configuration, MetaAccountTokenProfile, SafeRef, StrictModel
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan
from adhelm.strict_json import validate_json_value

_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_NATIVE_ID: Final = re.compile(r"^[1-9][0-9]{0,39}$", re.ASCII)
_CI_VALUES: Final = frozenset({"1", "true"})


class MetaCampaignPausePlanRequest(StrictModel):
    """The closed input for one configured Meta campaign PAUSE plan."""

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    campaign_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    expires_in_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("campaign_id", mode="before")
    @classmethod
    def exact_campaign_identifier(cls, value: object) -> str:
        if type(value) is not str or _NATIVE_ID.fullmatch(value) is None:
            raise ValueError("campaign_id must be a native Meta decimal identifier")
        return value

    @field_validator("expires_in_seconds", mode="before")
    @classmethod
    def exact_expiry_seconds(cls, value: object) -> int:
        if type(value) is not int or not 60 <= value <= 900:
            raise ValueError("expires_in_seconds must be an integer from 60 through 900")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class RetainedMetaCampaignPausePlan:
    """Internal result whose action needs closed historical review before output."""

    action: MetaCampaignPausePlannedAction
    plan_record_digest: str

    def __repr__(self) -> str:
        return "<RetainedMetaCampaignPausePlan>"


def _now() -> datetime:
    """Private clock seam for deterministic local tests."""

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
            "Meta campaign PAUSE planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_input() -> AdhelmError:
    return AdhelmError(
        "INVALID_META_CAMPAIGN_PAUSE_PLAN_INPUT",
        "Meta campaign PAUSE planning input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _checked_request(value: object) -> MetaCampaignPausePlanRequest:
    if type(value) is not MetaCampaignPausePlanRequest:
        raise _invalid_input()
    try:
        return validate_json_value(
            MetaCampaignPausePlanRequest,
            value.model_dump(mode="json", warnings="error"),
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_input() from None


def _checked_expected_configuration_digest(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise AdhelmError(
            "CONFIGURATION_CHANGED",
            "Local configuration changed before Meta campaign PAUSE planning.",
            exit_code=ExitCode.PRECONDITION,
        )
    return value


def _require_local_interactive_runtime() -> None:
    """Reject CI before any local configuration, ledger, store, or provider access."""

    if os.environ.get("CI", "").casefold() in _CI_VALUES:
        raise AdhelmError(
            "META_CAMPAIGN_PAUSE_CI_UNSUPPORTED",
            "Meta campaign PAUSE planning requires the configured local protected-token runtime.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Run this fixed Meta planning command from the configured local operator runtime."
            ),
        )


def _selected_edit_profile(
    configuration: Configuration, profile_id: str
) -> MetaAccountTokenProfile:
    selected = [profile for profile in configuration.profiles if profile.profile_id == profile_id]
    if (
        len(selected) != 1
        or type(selected[0]) is not MetaAccountTokenProfile
        or selected[0].purpose != "EDIT"
        or selected[0].required_scope != "ads_management"
    ):
        raise AdhelmError(
            "META_TOKEN_EDIT_PROFILE_REQUIRED",
            "Meta campaign PAUSE planning requires a configured Meta Ads token EDIT profile.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Configure a dedicated Meta EDIT profile before a fixed campaign operation."
            ),
        )
    return selected[0]


def _require_no_recovery(ledger: Ledger) -> None:
    """Block new planning on outstanding mutation history before credential use."""

    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED",
            "Unresolved mutation history must be reconciled before preparing a Meta PAUSE plan.",
            next_action="Inspect verified history and reconcile the retained recovery evidence.",
        )


def _configuration_and_profile(
    directory: Path,
    profile_id: str,
    expected: str | None,
) -> tuple[Configuration, str, MetaAccountTokenProfile]:
    """Capture configuration before protected-store or provider activity."""

    configuration = load_configuration(directory)
    captured = digest(configuration)
    if expected is not None and not hmac.compare_digest(captured, expected):
        raise AdhelmError(
            "CONFIGURATION_CHANGED",
            "Local configuration changed before Meta campaign PAUSE planning.",
            exit_code=ExitCode.PRECONDITION,
        )
    return configuration, captured, _selected_edit_profile(configuration, profile_id)


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "META_CAMPAIGN_PAUSE_FACTS_INVALID",
        "The configured Meta campaign PAUSE facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _build_action_from_session(
    *,
    workspace_profile_ref: str,
    profile: MetaAccountTokenProfile,
    configuration_digest: str,
    campaign_id: str,
    expires_in_seconds: int,
    session: MetaAdsTokenEditSession,
) -> tuple[MetaCampaignPausePlannedAction, datetime]:
    """Construct M40 facts from one closed EDIT session and fixed campaign GET."""

    try:
        binding = session.binding
        identity = session.identity
        state = session.read_campaign_state(campaign_id)
        read_finished_at = _checked_now()
        read_started_at = binding.observed_at
        expires_at = read_finished_at + timedelta(seconds=expires_in_seconds)
        if (
            binding.profile_ref != profile.profile_id
            or binding.client_application_ref != profile.client_application_ref
            or binding.required_scope != "ads_management"
            or not hmac.compare_digest(binding.configuration_digest, configuration_digest)
            or not hmac.compare_digest(binding.profile_configuration_digest, digest(profile))
            or identity.account_client_ref != f"act_{profile.account_ref}"
            or identity.account_ref != f"meta_ads.account.act_{profile.account_ref}"
            or identity.currency != profile.currency
            or identity.timezone != profile.timezone
            or state.account_client_ref != profile.account_ref
            or state.campaign_client_ref != campaign_id
            or state.configured_status != "ACTIVE"
        ):
            raise ValueError("bound facts")
        if read_finished_at < read_started_at:
            raise AdhelmError(
                "CLOCK_INVALID",
                "The Meta campaign PAUSE planning clock moved backwards during its fixed reads.",
                exit_code=ExitCode.PRECONDITION,
            )
        if expires_at > binding.expires_at or expires_at > binding.data_access_expires_at:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The requested Meta PAUSE plan lifetime exceeds the inspected token window.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Inspect the configured Meta token again before a shorter deliberate plan."
                ),
            )
        account = MetaCampaignPauseAccountBinding(
            account_ref=identity.account_ref,
            account_client_ref=identity.account_client_ref,
            timezone=identity.timezone,
            currency=identity.currency,
            account_status=identity.status,
            provider_api_version=identity.provider_api_version,
        )
        execution = MetaCampaignPauseExecutionBinding(
            profile_ref=binding.profile_ref,
            client_application_ref=binding.client_application_ref,
            principal_ref=binding.principal_ref,
            credential_binding_ref=binding.credential_binding_ref,
            configuration_digest=binding.configuration_digest,
            profile_configuration_digest=binding.profile_configuration_digest,
            inspected_at=read_started_at,
            expires_at=binding.expires_at,
            data_access_expires_at=binding.data_access_expires_at,
        )
        observation = MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref=state.campaign_ref,
                campaign_client_ref=state.campaign_client_ref,
                account_client_ref=state.account_client_ref,
                configured_status=state.configured_status,
                effective_status=state.effective_status,
            ),
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        )
        action = build_meta_campaign_pause_plan(
            workspace_profile_ref=workspace_profile_ref,
            account_binding=account,
            execution_binding=execution,
            observation_binding=observation,
            expires_at=expires_at,
        )
        return action, read_finished_at
    except AdhelmError:
        raise
    except (AttributeError, OverflowError, RecursionError, TypeError, ValueError, ValidationError):
        raise _facts_invalid() from None


def plan_configured_meta_campaign_pause(
    directory: Path,
    request: MetaCampaignPausePlanRequest,
    *,
    expected_configuration_digest: str | None = None,
) -> RetainedMetaCampaignPausePlan:
    """Read one fixed Meta campaign and retain one non-authorizing PAUSE plan.

    The CLI is responsible for resolving output/artifact policy and ensuring
    its complete bounded receipt fits before calling this workflow.  M40 has
    no artifact renderer: a requested generated artifact must be refused at
    that caller boundary before protected-store or provider activity.
    """

    if not isinstance(directory, Path):
        raise _invalid_input()
    checked = _checked_request(request)
    expected = _checked_expected_configuration_digest(expected_configuration_digest)
    _require_local_interactive_runtime()
    _configuration, captured_digest, profile = _configuration_and_profile(
        directory, checked.profile_id, expected
    )
    ledger = Ledger(directory)
    _require_no_recovery(ledger)

    with configured_meta_ads_token_edit_session(
        directory,
        profile.profile_id,
        expected_configuration_digest=captured_digest,
    ) as session:
        action, read_finished_at = _build_action_from_session(
            workspace_profile_ref=ledger.profile_ref,
            profile=profile,
            configuration_digest=captured_digest,
            campaign_id=checked.campaign_id,
            expires_in_seconds=checked.expires_in_seconds,
            session=session,
        )
        expiry = action.plan.expires_at
        with session.retention_recheck() as recheck:

            def before_durable_append() -> None:
                # The ledger owns its stream lock at this point.  This callback
                # only checks already-held configuration/credential state and
                # the local clock; it never performs ledger or provider I/O.
                recheck()
                current = _checked_now()
                if current < read_finished_at:
                    raise AdhelmError(
                        "CLOCK_INVALID",
                        "The Meta campaign PAUSE planning clock moved backwards before retention.",
                        exit_code=ExitCode.PRECONDITION,
                    )
                if current >= expiry:
                    raise AdhelmError(
                        "PLAN_EXPIRED",
                        "The Meta campaign PAUSE plan expired before local retention.",
                        exit_code=ExitCode.AUTHORIZATION,
                    )

            try:
                receipt = retain_meta_campaign_pause_plan(
                    ledger,
                    action,
                    before_durable_append=before_durable_append,
                )
            except OSError:
                raise AdhelmError(
                    "META_PAUSE_PLAN_RETENTION_UNVERIFIED",
                    "The Meta campaign read completed, but local plan retention is unverified.",
                    exit_code=ExitCode.AMBIGUOUS,
                    next_action="Inspect Meta PAUSE plan history before preparing another plan.",
                ) from None

    try:
        record_digest = receipt["record_digest"]
        if type(record_digest) is not str or _DIGEST.fullmatch(record_digest) is None:
            raise ValueError("record digest")
        return RetainedMetaCampaignPausePlan(action=action, plan_record_digest=record_digest)
    except (KeyError, TypeError, ValueError):
        raise AdhelmError(
            "META_PAUSE_PLAN_RETENTION_UNVERIFIED",
            "The Meta campaign read completed, but local plan retention is unverified.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect Meta PAUSE plan history before preparing another plan.",
        ) from None


__all__ = [
    "MetaCampaignPausePlanRequest",
    "RetainedMetaCampaignPausePlan",
    "plan_configured_meta_campaign_pause",
]

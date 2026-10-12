"""Configured Google Ads PAUSE proposals retained without execution authority."""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Never

from pydantic import Field, ValidationError, field_validator

from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_planning_session import (
    GoogleAdsCampaignPausePlanningSession,
    configured_google_ads_pause_planning_session,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile, SafeRef, StrictModel
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPausePlannedAction,
    build_google_ads_campaign_pause_plan,
)
from adhelm.saved_google_ads_pause_plans import retain_google_ads_campaign_pause_plan
from adhelm.strict_json import validate_json_value

_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_CAMPAIGN: Final = re.compile(r"^[1-9][0-9]{0,18}$", re.ASCII)
_MAX_CAMPAIGN_ID: Final = 9_223_372_036_854_775_807
_SCOPE: Final = "https://www.googleapis.com/auth/adwords"


class GoogleAdsCampaignPausePlanRequest(StrictModel):
    """One exact campaign selection and requested finite proposal lifetime."""

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    campaign_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    expires_in_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("campaign_id", mode="before")
    @classmethod
    def canonical_campaign_id(cls, value: object) -> str:
        if (
            type(value) is not str
            or _CAMPAIGN.fullmatch(value) is None
            or int(value) > _MAX_CAMPAIGN_ID
        ):
            raise ValueError("campaign_id must be a canonical positive signed-int64 identifier")
        return value

    @field_validator("expires_in_seconds", mode="before")
    @classmethod
    def exact_lifetime(cls, value: object) -> int:
        if type(value) is not int or not 60 <= value <= 900:
            raise ValueError("expires_in_seconds must be an integer from 60 through 900")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class RetainedGoogleAdsCampaignPausePlan:
    """Private result; its action requires the closed historical sharing view."""

    action: GoogleAdsCampaignPausePlannedAction
    plan_record_digest: str

    def __repr__(self) -> str:
        return "<RetainedGoogleAdsCampaignPausePlan>"

    def __reduce__(self) -> Never:
        raise TypeError("Retained Google Ads plans require the historical review boundary")

    def __getstate__(self) -> Never:
        raise TypeError("Retained Google Ads plans require the historical review boundary")


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
            "CLOCK_INVALID",
            "Google Ads PAUSE planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_input() -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_INPUT",
        "Google Ads campaign PAUSE planning input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _checked_request(value: object) -> GoogleAdsCampaignPausePlanRequest:
    try:
        if type(value) is not GoogleAdsCampaignPausePlanRequest:
            raise ValueError("request")
        return validate_json_value(
            GoogleAdsCampaignPausePlanRequest,
            value.model_dump(mode="json", warnings="error"),
        )
    except Exception:
        raise _invalid_input() from None


def _configuration_and_profile(
    directory: Path,
    profile_id: str,
    expected: str | None,
) -> tuple[Configuration, str, Profile]:
    configuration = load_configuration(directory)
    captured = digest(configuration)
    if expected is not None and not hmac.compare_digest(captured, expected):
        raise AdhelmError(
            "CONFIGURATION_CHANGED",
            "Local configuration changed before Google Ads PAUSE planning.",
            exit_code=ExitCode.PRECONDITION,
        )
    profiles = [profile for profile in configuration.profiles if profile.profile_id == profile_id]
    if (
        len(profiles) != 1
        or type(profiles[0]) is not Profile
        or profiles[0].provider != "google_ads"
        or profiles[0].purpose != "EDIT"
        or profiles[0].credential_source != "OS_OAUTH_GOOGLE"
        or profiles[0].scopes != [_SCOPE]
        or profiles[0].currency is None
    ):
        raise AdhelmError(
            "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
            "Google Ads PAUSE planning requires a configured local OAuth EDIT profile.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Configure the Google Ads EDIT profile.",
        )
    return configuration, captured, profiles[0]


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_CAMPAIGN_PAUSE_FACTS_INVALID",
        "The configured Google Ads PAUSE facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _build_action_from_session(
    *,
    workspace_profile_ref: str,
    profile: Profile,
    configuration_digest: str,
    request: GoogleAdsCampaignPausePlanRequest,
    session: GoogleAdsCampaignPausePlanningSession,
) -> tuple[GoogleAdsCampaignPausePlannedAction, datetime]:
    try:
        execution = session.execution_binding
        observed = session.observe_campaign(request.campaign_id)
        account = observed.account_binding
        observation = observed.observation_binding
        campaign = observation.campaign
        finished_at = observation.read_finished_at
        expires_at = finished_at + timedelta(seconds=request.expires_in_seconds)
        current = _checked_now()
        if current < finished_at:
            raise AdhelmError(
                "CLOCK_INVALID",
                "The Google Ads planning clock moved backwards after the fixed reads.",
                exit_code=ExitCode.PRECONDITION,
            )
        if (
            execution.profile_ref != profile.profile_id
            or execution.client_application_ref != profile.client_ref
            or execution.principal_ref != profile.principal_ref
            or execution.required_scope != _SCOPE
            or execution.login_customer_client_ref != profile.login_customer_ref
            or execution.configuration_digest != configuration_digest
            or execution.profile_configuration_digest != digest(profile)
            or account.account_client_ref != profile.account_ref
            or account.account_ref != f"google_ads.customer.{profile.account_ref}"
            or account.currency != profile.currency
            or account.timezone != profile.timezone
            or campaign.account_client_ref != profile.account_ref
            or campaign.campaign_client_ref != request.campaign_id
            or campaign.status != "ENABLED"
        ):
            raise ValueError("bound facts")
        if expires_at > execution.expires_at or current >= expires_at:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The requested Google Ads PAUSE lifetime exceeds the usable credential window.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Prepare a fresh deliberate plan with a usable credential window.",
            )
        action = build_google_ads_campaign_pause_plan(
            workspace_profile_ref=workspace_profile_ref,
            account_binding=account,
            execution_binding=execution,
            observation_binding=observation,
            expires_at=expires_at,
        )
        return action, current
    except AdhelmError:
        raise
    except (AttributeError, OverflowError, RecursionError, TypeError, ValueError, ValidationError):
        raise _facts_invalid() from None


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_PLAN_RETENTION_UNVERIFIED",
        "The Google Ads read completed, but local plan retention is unverified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect retained Google Ads PAUSE plan history before preparing another plan.",
    )


def plan_configured_google_ads_campaign_pause(
    directory: Path,
    request: GoogleAdsCampaignPausePlanRequest,
    *,
    expected_configuration_digest: str | None = None,
) -> RetainedGoogleAdsCampaignPausePlan:
    """Read a fresh fixed pair and retain one proposal, without authorizing it.

    A future command caller must preflight its complete bounded output and
    artifact preferences before entering this private workflow.
    """

    if not isinstance(directory, Path):
        raise _invalid_input()
    checked = _checked_request(request)
    if expected_configuration_digest is not None and (
        type(expected_configuration_digest) is not str
        or _DIGEST.fullmatch(expected_configuration_digest) is None
    ):
        raise _invalid_input()
    if os.environ.get("CI", "").casefold() in {"1", "true"}:
        raise AdhelmError(
            "GOOGLE_ADS_CAMPAIGN_PAUSE_CI_UNSUPPORTED",
            "Google Ads PAUSE planning requires the configured local protected-store runtime.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    _, captured, profile = _configuration_and_profile(
        directory,
        checked.profile_id,
        expected_configuration_digest,
    )
    ledger = Ledger(directory)
    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED",
            "Unresolved mutation history must be reconciled before Google Ads PAUSE planning.",
            next_action="Inspect verified history and reconcile the retained recovery evidence.",
        )
    with configured_google_ads_pause_planning_session(
        directory,
        profile.profile_id,
        expected_configuration_digest=captured,
    ) as session:
        action, planning_checked_at = _build_action_from_session(
            workspace_profile_ref=ledger.profile_ref,
            profile=profile,
            configuration_digest=captured,
            request=checked,
            session=session,
        )
        with session.retention_recheck() as recheck:

            def before_durable_append() -> None:
                # The ledger already owns its stream lock. Recheck held local
                # facts and the clock only; never read the ledger or provider.
                recheck()
                current = _checked_now()
                if current < planning_checked_at:
                    raise AdhelmError(
                        "CLOCK_INVALID",
                        "The Google Ads planning clock moved backwards.",
                        exit_code=ExitCode.PRECONDITION,
                    )
                if current >= action.plan.expires_at:
                    raise AdhelmError(
                        "PLAN_EXPIRED",
                        "The Google Ads PAUSE plan expired before retention.",
                        exit_code=ExitCode.AUTHORIZATION,
                    )

            try:
                receipt = retain_google_ads_campaign_pause_plan(
                    ledger,
                    action,
                    before_durable_append=before_durable_append,
                )
            except OSError:
                raise _retention_unverified() from None
    try:
        record_digest = receipt["record_digest"]
        if type(record_digest) is not str or _DIGEST.fullmatch(record_digest) is None:
            raise ValueError("record digest")
        return RetainedGoogleAdsCampaignPausePlan(action=action, plan_record_digest=record_digest)
    except (KeyError, TypeError, ValueError):
        raise _retention_unverified() from None


__all__ = [
    "GoogleAdsCampaignPausePlanRequest",
    "RetainedGoogleAdsCampaignPausePlan",
    "plan_configured_google_ads_campaign_pause",
]


# P03 registers the ("google_ads", PAUSE) execution planner and adapter factory
# here, at the end of the module the Google PAUSE CLI (and therefore `cli.py`)
# already imports, so `discover` renders the pair from the two registries
# themselves.  Nothing above changed: `plan google-ads-campaign-pause create`
# still retains exactly its provider-specific record, and the registered
# planner runs this same flow before retaining the provider-neutral one.  The
# import is deliberately last so the registration module can reach this
# module's request model lazily without an import cycle.
from adhelm.google_ads_pause_execution import (  # noqa: E402
    register_google_ads_pause_execution,
)

register_google_ads_pause_execution()

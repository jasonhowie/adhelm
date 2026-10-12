"""Closed, pure Google Ads v25 campaign-pause plan reconstruction.

This module converts already-proved, nonsecret local and remote facts into one
exact, non-authorizing :class:`~adhelm.safety.CompositePlan`.  It deliberately
does not import configuration, credentials, transports, ledger history, native
approval, or provider mutation code.  A later guarded workflow must obtain and
compare fresh facts before any provider request.

The contract is limited to the configured campaign-state transition
``ENABLED`` -> ``PAUSED``.  Google Ads v25 mutation acknowledgement,
partial-failure, conditional-write, idempotency, delivery, spend, and live
permission semantics are deliberately outside this pure contract.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_protocol import (
    is_google_oauth_client_id,
    is_google_oauth_operational_principal_ref,
)
from adhelm.models import SafeRef, StrictModel
from adhelm.safety import (
    AccountIdentity,
    CompositePlan,
    EgressTarget,
    FieldChange,
    Money,
    OperationKind,
    StateRef,
    SubPlan,
)

GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS: Final[
    Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
] = "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
GOOGLE_ADS_CAMPAIGN_PAUSE_HOST: Final[Literal["googleads.googleapis.com"]] = (
    "googleads.googleapis.com"
)
GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION: Final[Literal["v25"]] = "v25"
GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION: Final = (
    "google-ads-v25-campaign-pause-contract-2026-09-06"
)
GOOGLE_ADS_CAMPAIGN_PAUSE_EGRESS_PURPOSE: Final = "fixed Google Ads v25 campaign pause"
GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE: Final[Literal["https://www.googleapis.com/auth/adwords"]] = (
    "https://www.googleapis.com/auth/adwords"
)

_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_CUSTOMER_ID: Final = re.compile(r"^[0-9]{10}$", re.ASCII)
_CAMPAIGN_ID: Final = re.compile(r"^[1-9][0-9]{0,18}$", re.ASCII)
_CREDENTIAL_BINDING_REF: Final = re.compile(r"^credential-binding:sha256:[a-f0-9]{64}$", re.ASCII)
_SAFE_WORKSPACE_REF: Final = re.compile(r"^[a-zA-Z0-9_.:-]{1,160}$", re.ASCII)
_SAFE_PROFILE_REF: Final = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$", re.ASCII)
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$", re.ASCII)
_MAX_INT64: Final = 9_223_372_036_854_775_807
_CUSTOMER_STATUSES: Final = frozenset({"CANCELED", "CLOSED", "ENABLED", "SUSPENDED"})
_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 131_072
_BEFORE_REVISION: Final = "GOOGLE_ADS_V25_ALLOWLISTED_REMOTE_STATE_V1"
_AFTER_REVISION: Final = "GOOGLE_ADS_V25_PAUSE_CONFIGURATION_POSTCONDITION_V1"

GoogleAdsCustomerStatus = Literal["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"]


def _invalid_plan(message: str = "Google Ads campaign pause plan data is invalid.") -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN", message, exit_code=ExitCode.INVALID_INPUT
    )


def _exact_int(value: object, *, expected: int) -> int:
    if type(value) is not int or value != expected:
        raise ValueError("expected exact integer")
    return value


def _exact_false(value: object) -> bool:
    if type(value) is not bool or value is not False:
        raise ValueError("expected false")
    return value


def _aware_utc(value: object, *, label: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be an aware datetime")
    return value.astimezone(UTC)


def _wire_or_aware_utc(value: object, *, label: str) -> datetime:
    """Accept an aware value or its sole canonical JSON UTC representation."""

    if type(value) is datetime:
        return _aware_utc(value, label=label)
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"{label} must be an aware UTC time")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be an aware UTC time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be an aware UTC time")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat().replace("+00:00", "Z") != value:
        raise ValueError(f"{label} must use canonical UTC text")
    return normalized


def _safe_workspace_ref(value: object) -> str:
    if type(value) is not str or _SAFE_WORKSPACE_REF.fullmatch(value) is None:
        raise ValueError("workspace profile reference is invalid")
    return value


def _safe_profile_ref(value: object) -> str:
    if type(value) is not str or _SAFE_PROFILE_REF.fullmatch(value) is None:
        raise ValueError("profile reference is invalid")
    return value


def _customer_id(value: object, *, label: str) -> str:
    if type(value) is not str or _CUSTOMER_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _campaign_id(value: object) -> str:
    if type(value) is not str or _CAMPAIGN_ID.fullmatch(value) is None:
        raise ValueError("campaign ID is invalid")
    if int(value) > _MAX_INT64:
        raise ValueError("campaign ID is invalid")
    return value


def _currency(value: object) -> str:
    if type(value) is not str or _CURRENCY.fullmatch(value) is None:
        raise ValueError("currency is invalid")
    return value


def _timezone(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 80:
        raise ValueError("timezone is invalid")
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone is invalid") from exc
    return value


class GoogleAdsCampaignPauseWorkspaceBinding(StrictModel):
    """The mandatory local ledger workspace, separate from the EDIT profile."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["LOCAL_LEDGER_WORKSPACE"] = "LOCAL_LEDGER_WORKSPACE"
    workspace_profile_ref: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("workspace_profile_ref", mode="before")
    @classmethod
    def exact_workspace_profile_ref(cls, value: object) -> str:
        return _safe_workspace_ref(value)


class GoogleAdsCampaignPauseAccountBinding(StrictModel):
    """Closed v25 customer facts, with ``CLOSED`` retained as non-gating data."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_CUSTOMER_BASELINE"] = "GOOGLE_ADS_V25_CUSTOMER_BASELINE"
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    currency: str
    timezone: str = Field(min_length=1, max_length=80)
    account_status: GoogleAdsCustomerStatus
    provider_api_version: Literal["v25"] = GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "account_ref", "account_client_ref", "currency", "timezone", "account_status", mode="before"
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("account baseline scalar is invalid")
        return value

    @field_validator("account_client_ref")
    @classmethod
    def exact_customer_id(cls, value: str) -> str:
        return _customer_id(value, label="customer ID")

    @field_validator("currency")
    @classmethod
    def exact_currency(cls, value: str) -> str:
        return _currency(value)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        return _timezone(value)

    @field_validator("account_status")
    @classmethod
    def closed_customer_status(cls, value: str) -> str:
        if value not in _CUSTOMER_STATUSES:
            raise ValueError("customer status is invalid")
        return value

    @model_validator(mode="after")
    def exact_google_ads_customer_identity(self) -> GoogleAdsCampaignPauseAccountBinding:
        if self.account_ref != f"google_ads.customer.{self.account_client_ref}":
            raise ValueError("account references do not bind the Google Ads customer")
        return self


class GoogleAdsCampaignPauseExecutionBinding(StrictModel):
    """Closed declared local OAuth/developer-token association facts.

    This is deliberately a nonsecret declaration, not a token snapshot,
    credential-slot selector, session marker, current provider permission, or
    execution authority.  A later configured workflow must establish fresh
    protected continuity separately.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_EDIT_DECLARED_OAUTH_AND_DEVELOPER_TOKEN_ASSOCIATION"] = (
        "GOOGLE_ADS_EDIT_DECLARED_OAUTH_AND_DEVELOPER_TOKEN_ASSOCIATION"
    )
    profile_ref: str
    client_application_ref: str
    principal_ref: str
    required_scope: Literal["https://www.googleapis.com/auth/adwords"] = (
        GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE
    )
    login_customer_client_ref: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    credential_binding_ref: str
    configuration_digest: str
    profile_configuration_digest: str
    resolved_at: datetime
    expires_at: datetime
    local_association_status: Literal["LOCAL_OAUTH_AND_DEVELOPER_TOKEN_ASSOCIATION_ONLY"] = (
        "LOCAL_OAUTH_AND_DEVELOPER_TOKEN_ASSOCIATION_ONLY"
    )
    provider_write_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("profile_ref", mode="before")
    @classmethod
    def safe_profile_ref(cls, value: object) -> str:
        return _safe_profile_ref(value)

    @field_validator(
        "client_application_ref",
        "principal_ref",
        "credential_binding_ref",
        "configuration_digest",
        "profile_configuration_digest",
        mode="before",
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("execution binding scalar is invalid")
        return value

    @field_validator("client_application_ref")
    @classmethod
    def valid_google_client_id(cls, value: str) -> str:
        if not is_google_oauth_client_id(value):
            raise ValueError("client application identifier is invalid")
        return value

    @field_validator("principal_ref")
    @classmethod
    def valid_derived_google_principal(cls, value: str) -> str:
        if not is_google_oauth_operational_principal_ref(value):
            raise ValueError("derived principal identifier is invalid")
        return value

    @field_validator("login_customer_client_ref", mode="before")
    @classmethod
    def optional_login_customer(cls, value: object) -> object:
        if value is None:
            return None
        return _customer_id(value, label="login customer ID")

    @field_validator("credential_binding_ref")
    @classmethod
    def credential_binding_identifier(cls, value: str) -> str:
        if _CREDENTIAL_BINDING_REF.fullmatch(value) is None:
            raise ValueError("credential binding identifier is invalid")
        return value

    @field_validator("configuration_digest", "profile_configuration_digest")
    @classmethod
    def exact_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("execution binding digest is invalid")
        return value

    @field_validator("resolved_at", "expires_at", mode="before")
    @classmethod
    def exact_time_input(cls, value: object) -> datetime:
        return _wire_or_aware_utc(value, label="execution binding time")

    @model_validator(mode="after")
    def resolved_before_expiry(self) -> GoogleAdsCampaignPauseExecutionBinding:
        if self.resolved_at >= self.expires_at:
            raise ValueError("EDIT association is not strictly before its expiry")
        return self


class GoogleAdsCampaignPauseCampaignBaseline(StrictModel):
    """The one eligible fixed v25 campaign observation."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_CAMPAIGN_BASELINE"] = "GOOGLE_ADS_V25_CAMPAIGN_BASELINE"
    campaign_ref: SafeRef
    campaign_client_ref: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    status: Literal["ENABLED"] = "ENABLED"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "campaign_ref",
        "campaign_client_ref",
        "account_ref",
        "account_client_ref",
        "status",
        mode="before",
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("campaign baseline scalar is invalid")
        return value

    @field_validator("campaign_client_ref")
    @classmethod
    def exact_campaign_id(cls, value: str) -> str:
        return _campaign_id(value)

    @field_validator("account_client_ref")
    @classmethod
    def exact_customer_id(cls, value: str) -> str:
        return _customer_id(value, label="campaign customer ID")

    @model_validator(mode="after")
    def exact_campaign_account_association(self) -> GoogleAdsCampaignPauseCampaignBaseline:
        if (
            self.account_ref != f"google_ads.customer.{self.account_client_ref}"
            or self.campaign_ref
            != f"google_ads.campaign.{self.account_client_ref}.{self.campaign_client_ref}"
        ):
            raise ValueError("campaign baseline does not bind the fixed Google Ads observation")
        return self


class GoogleAdsCampaignPauseObservationBinding(StrictModel):
    """One bounded sequential customer/campaign observation interval."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GOOGLE_ADS_V25_CAMPAIGN_STATE_OBSERVATION"] = (
        "GOOGLE_ADS_V25_CAMPAIGN_STATE_OBSERVATION"
    )
    campaign: GoogleAdsCampaignPauseCampaignBaseline
    read_started_at: datetime
    read_finished_at: datetime
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("campaign", mode="before")
    @classmethod
    def exact_campaign(
        cls, value: object
    ) -> GoogleAdsCampaignPauseCampaignBaseline | dict[str, object]:
        if type(value) is GoogleAdsCampaignPauseCampaignBaseline:
            return value
        if type(value) is not dict:
            raise ValueError("campaign observation requires a closed campaign baseline")
        return value

    @field_validator("read_started_at", "read_finished_at", mode="before")
    @classmethod
    def exact_time_input(cls, value: object) -> datetime:
        return _wire_or_aware_utc(value, label="campaign observation time")

    @model_validator(mode="after")
    def bounded_read_interval(self) -> GoogleAdsCampaignPauseObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("campaign observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("campaign observation interval exceeds the local bound")
        return self


class GoogleAdsCampaignPauseConfigurationPostcondition(StrictModel):
    """Closed configuration-only postcondition with no delivery prediction."""

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    currency: str
    timezone: str = Field(min_length=1, max_length=80)
    account_status: GoogleAdsCustomerStatus
    provider_api_version: Literal["v25"] = GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION
    campaign_ref: SafeRef
    campaign_client_ref: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    campaign_account_ref: SafeRef
    campaign_account_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    configured_status: Literal["PAUSED"] = "PAUSED"
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "account_ref",
        "account_client_ref",
        "currency",
        "timezone",
        "account_status",
        "campaign_ref",
        "campaign_client_ref",
        "campaign_account_ref",
        "campaign_account_client_ref",
        mode="before",
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("pause postcondition scalar is invalid")
        return value

    @field_validator("account_client_ref", "campaign_account_client_ref")
    @classmethod
    def exact_customer_ids(cls, value: str) -> str:
        return _customer_id(value, label="postcondition customer ID")

    @field_validator("campaign_client_ref")
    @classmethod
    def exact_campaign_id(cls, value: str) -> str:
        return _campaign_id(value)

    @field_validator("currency")
    @classmethod
    def exact_currency(cls, value: str) -> str:
        return _currency(value)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        return _timezone(value)

    @field_validator("account_status")
    @classmethod
    def closed_customer_status(cls, value: str) -> str:
        if value not in _CUSTOMER_STATUSES:
            raise ValueError("customer status is invalid")
        return value

    @model_validator(mode="after")
    def exact_unchanged_association(self) -> GoogleAdsCampaignPauseConfigurationPostcondition:
        account = GoogleAdsCampaignPauseAccountBinding(
            account_ref=self.account_ref,
            account_client_ref=self.account_client_ref,
            currency=self.currency,
            timezone=self.timezone,
            account_status=self.account_status,
            provider_api_version=self.provider_api_version,
        )
        campaign = GoogleAdsCampaignPauseCampaignBaseline(
            campaign_ref=self.campaign_ref,
            campaign_client_ref=self.campaign_client_ref,
            account_ref=self.campaign_account_ref,
            account_client_ref=self.campaign_account_client_ref,
            status="ENABLED",
        )
        if (
            campaign.account_ref != account.account_ref
            or campaign.account_client_ref != account.account_client_ref
        ):
            raise ValueError("pause postcondition changes the campaign account association")
        return self


def _postcondition(
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsCampaignPauseObservationBinding,
) -> GoogleAdsCampaignPauseConfigurationPostcondition:
    campaign = observation.campaign
    return GoogleAdsCampaignPauseConfigurationPostcondition(
        account_ref=account.account_ref,
        account_client_ref=account.account_client_ref,
        currency=account.currency,
        timezone=account.timezone,
        account_status=account.account_status,
        provider_api_version=account.provider_api_version,
        campaign_ref=campaign.campaign_ref,
        campaign_client_ref=campaign.campaign_client_ref,
        campaign_account_ref=campaign.account_ref,
        campaign_account_client_ref=campaign.account_client_ref,
    )


def _before_state(
    account: GoogleAdsCampaignPauseAccountBinding,
    observation: GoogleAdsCampaignPauseObservationBinding,
) -> StateRef:
    """Fingerprint only allowlisted remote facts; local association stays outside it."""

    return StateRef(
        fingerprint=digest(
            {
                "state_kind": _BEFORE_REVISION,
                "account": account.model_dump(mode="json", warnings="error"),
                "campaign": observation.campaign.model_dump(mode="json", warnings="error"),
            }
        )
    )


def _after_state(postcondition: GoogleAdsCampaignPauseConfigurationPostcondition) -> StateRef:
    return StateRef(
        fingerprint=digest(
            {
                "state_kind": _AFTER_REVISION,
                "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            }
        )
    )


def _action_id(
    *,
    workspace: GoogleAdsCampaignPauseWorkspaceBinding,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    observation: GoogleAdsCampaignPauseObservationBinding,
    postcondition: GoogleAdsCampaignPauseConfigurationPostcondition,
    expires_at: datetime,
) -> str:
    """Commit every outer local and remote fact to the exact action ID."""

    return digest(
        {
            "kind": "google-ads-campaign-pause-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "account": account.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS,
            "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
            "configuration_readback": "REQUIRED_AFTER_PAUSE",
            "recovery_semantics": "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED",
            "execution_authority": False,
            "live_validated": False,
            "conditional_write_proof": False,
            "idempotency_proof": False,
        }
    )


def _build_plan(
    *,
    action_id: str,
    workspace: GoogleAdsCampaignPauseWorkspaceBinding,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    observation: GoogleAdsCampaignPauseObservationBinding,
    postcondition: GoogleAdsCampaignPauseConfigurationPostcondition,
    expires_at: datetime,
) -> CompositePlan:
    before = _before_state(account, observation)
    after = _after_state(postcondition)
    identity = AccountIdentity(
        account_ref=account.account_ref,
        stable_account_id=account.account_client_ref,
        auth_profile_ref=execution.profile_ref,
        principal_ref=execution.principal_ref,
        client_application_ref=execution.client_application_ref,
        scopes=(GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,),
    )
    subplan = SubPlan(
        subplan_id=f"google-ads-campaign-pause-{action_id}",
        provider="google_ads",
        operation=OperationKind.PAUSE,
        account=identity,
        provider_api_version=GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION,
        capability_version=GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path="campaign/status", before=before, after=after),),
        max_spend=Money(minor_units=0, currency=account.currency),
        egress=(
            EgressTarget(
                host=GOOGLE_ADS_CAMPAIGN_PAUSE_HOST,
                purpose=GOOGLE_ADS_CAMPAIGN_PAUSE_EGRESS_PURPOSE,
            ),
        ),
        # A later adapter may only reconcile and require a new exact
        # authorization.  This pure contract never permits a blind retry.
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"google-ads-campaign-pause-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: GoogleAdsCampaignPauseExecutionBinding,
    observation: GoogleAdsCampaignPauseObservationBinding,
    expires_at: datetime,
) -> None:
    campaign = observation.campaign
    if (
        campaign.account_ref != account.account_ref
        or campaign.account_client_ref != account.account_client_ref
    ):
        raise ValueError("campaign observation is outside the account baseline")
    if execution.resolved_at > observation.read_started_at:
        raise ValueError("EDIT association occurs after the campaign read began")
    if observation.read_finished_at >= expires_at:
        raise ValueError("plan expiry must follow the completed campaign read")
    if expires_at > execution.expires_at:
        raise ValueError("plan expiry exceeds the declared bearer expiry")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")


class GoogleAdsCampaignPausePlannedAction(StrictModel):
    """One exact Google Ads campaign PAUSE action, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS
    )
    action_id: str
    workspace_binding: GoogleAdsCampaignPauseWorkspaceBinding
    account_binding: GoogleAdsCampaignPauseAccountBinding
    execution_binding: GoogleAdsCampaignPauseExecutionBinding
    observation_binding: GoogleAdsCampaignPauseObservationBinding
    postcondition: GoogleAdsCampaignPauseConfigurationPostcondition
    plan: CompositePlan
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    configuration_readback: Literal["REQUIRED_AFTER_PAUSE"] = "REQUIRED_AFTER_PAUSE"
    recovery_semantics: Literal["RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"] = (
        "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"
    )
    execution_authority: Literal[False] = False
    live_validated: Literal[False] = False
    conditional_write_proof: Literal[False] = False
    idempotency_proof: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("action_id", mode="before")
    @classmethod
    def exact_action_id(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("action ID must be a full SHA-256 digest")
        return value

    @field_validator(
        "workspace_binding",
        "account_binding",
        "execution_binding",
        "observation_binding",
        "postcondition",
        "plan",
        mode="before",
    )
    @classmethod
    def exact_closed_members(cls, value: object, info: Any) -> object:
        expected = {
            "workspace_binding": GoogleAdsCampaignPauseWorkspaceBinding,
            "account_binding": GoogleAdsCampaignPauseAccountBinding,
            "execution_binding": GoogleAdsCampaignPauseExecutionBinding,
            "observation_binding": GoogleAdsCampaignPauseObservationBinding,
            "postcondition": GoogleAdsCampaignPauseConfigurationPostcondition,
            "plan": CompositePlan,
        }[info.field_name]
        if type(value) is not expected:
            raise ValueError("planned action requires an exact closed member")
        return value

    @field_validator(
        "execution_authority",
        "live_validated",
        "conditional_write_proof",
        "idempotency_proof",
        mode="before",
    )
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        return _exact_false(value)

    @model_validator(mode="after")
    def exact_reconstructed_action(self) -> GoogleAdsCampaignPausePlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only complete, closed, validated serializable action data."""

        checked = revalidate_google_ads_campaign_pause_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def _strict_json_bytes(value: object, *, label: str) -> bytes:
    """Encode untrusted JSON without normalizing scalar types first."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise ValueError(f"{label} JSON") from None
    if len(encoded) > _MAX_ACTION_BYTES:
        raise ValueError(f"{label} exceeds bound")
    return encoded


def _checked_member[T: BaseModel](value: object, expected: type[T]) -> T:
    if type(value) is not expected:
        raise ValueError("member type")
    try:
        raw = value.model_dump(mode="json", warnings="error")
        parsed = expected.model_validate_json(_strict_json_bytes(raw, label="member"))
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("member value") from None
    if type(parsed) is not expected or canonical_bytes(raw) != canonical_bytes(
        parsed.model_dump(mode="json", warnings="error")
    ):
        raise ValueError("member canonical shape")
    return parsed


def _checked_expiry(value: object) -> datetime:
    return _aware_utc(value, label="plan expiry")


def build_google_ads_campaign_pause_plan(
    *,
    workspace_profile_ref: str,
    account_binding: GoogleAdsCampaignPauseAccountBinding,
    execution_binding: GoogleAdsCampaignPauseExecutionBinding,
    observation_binding: GoogleAdsCampaignPauseObservationBinding,
    expires_at: datetime,
) -> GoogleAdsCampaignPausePlannedAction:
    """Build the one fixed non-authorizing Google Ads configuration pause plan."""

    try:
        workspace = GoogleAdsCampaignPauseWorkspaceBinding(
            workspace_profile_ref=workspace_profile_ref
        )
        account = _checked_member(account_binding, GoogleAdsCampaignPauseAccountBinding)
        execution = _checked_member(execution_binding, GoogleAdsCampaignPauseExecutionBinding)
        observation = _checked_member(observation_binding, GoogleAdsCampaignPauseObservationBinding)
        expiry = _checked_expiry(expires_at)
        _assert_outer_bindings(
            account=account, execution=execution, observation=observation, expires_at=expiry
        )
        postcondition = _postcondition(account, observation)
        action_id = _action_id(
            workspace=workspace,
            account=account,
            execution=execution,
            observation=observation,
            postcondition=postcondition,
            expires_at=expiry,
        )
        plan = _build_plan(
            action_id=action_id,
            workspace=workspace,
            account=account,
            execution=execution,
            observation=observation,
            postcondition=postcondition,
            expires_at=expiry,
        )
        return GoogleAdsCampaignPausePlannedAction(
            action_id=action_id,
            workspace_binding=workspace,
            account_binding=account,
            execution_binding=execution,
            observation_binding=observation,
            postcondition=postcondition,
            plan=plan,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


_ACTION_FIELDS: Final = frozenset(
    {
        "schema_version",
        "status",
        "action_id",
        "workspace_binding",
        "account_binding",
        "execution_binding",
        "observation_binding",
        "postcondition",
        "plan",
        "current_state_status",
        "configuration_readback",
        "recovery_semantics",
        "execution_authority",
        "live_validated",
        "conditional_write_proof",
        "idempotency_proof",
    }
)


def _raw_mapping(value: object) -> dict[str, object]:
    if type(value) is GoogleAdsCampaignPausePlannedAction:
        raw = value.model_dump(mode="json", warnings="error")
    elif type(value) is dict:
        raw = value
    else:
        raise ValueError("action input type")
    encoded = _strict_json_bytes(raw, label="action")
    try:
        parsed = parse_json(encoded, maximum_bytes=_MAX_ACTION_BYTES)
    except (AdhelmError, UnicodeError, ValueError):
        raise ValueError("action JSON") from None
    if type(parsed) is not dict:
        raise ValueError("action object")
    return cast(dict[str, object], parsed)


def _require_exact_keys(data: dict[str, object], expected: frozenset[str]) -> None:
    if set(data) != expected or any(type(key) is not str for key in data):
        raise ValueError("action keys")


def _raw_member(data: dict[str, object], name: str, expected: type[BaseModel]) -> BaseModel:
    value = data[name]
    if type(value) is not dict:
        raise ValueError("nested action member")
    encoded = _strict_json_bytes(value, label="nested action member")
    try:
        parsed = expected.model_validate_json(encoded)
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("nested action member") from None
    if type(parsed) is not expected or canonical_bytes(value) != canonical_bytes(
        parsed.model_dump(mode="json", warnings="error")
    ):
        raise ValueError("nested action canonical shape")
    return parsed


def _action_from_raw(data: dict[str, object]) -> GoogleAdsCampaignPausePlannedAction:
    _require_exact_keys(data, _ACTION_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    if data["status"] != GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS:
        raise ValueError("action status")
    if type(data["action_id"]) is not str or _DIGEST.fullmatch(data["action_id"]) is None:
        raise ValueError("action ID")
    if data["current_state_status"] != "RECHECK_REQUIRED_BEFORE_EGRESS":
        raise ValueError("current-state semantics")
    if data["configuration_readback"] != "REQUIRED_AFTER_PAUSE":
        raise ValueError("readback semantics")
    if data["recovery_semantics"] != "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED":
        raise ValueError("recovery semantics")
    for name in (
        "execution_authority",
        "live_validated",
        "conditional_write_proof",
        "idempotency_proof",
    ):
        _exact_false(data[name])
    workspace = _raw_member(data, "workspace_binding", GoogleAdsCampaignPauseWorkspaceBinding)
    account = _raw_member(data, "account_binding", GoogleAdsCampaignPauseAccountBinding)
    execution = _raw_member(data, "execution_binding", GoogleAdsCampaignPauseExecutionBinding)
    observation = _raw_member(data, "observation_binding", GoogleAdsCampaignPauseObservationBinding)
    postcondition = _raw_member(
        data, "postcondition", GoogleAdsCampaignPauseConfigurationPostcondition
    )
    plan = _raw_member(data, "plan", CompositePlan)
    try:
        return GoogleAdsCampaignPausePlannedAction(
            action_id=data["action_id"],
            workspace_binding=cast(GoogleAdsCampaignPauseWorkspaceBinding, workspace),
            account_binding=cast(GoogleAdsCampaignPauseAccountBinding, account),
            execution_binding=cast(GoogleAdsCampaignPauseExecutionBinding, execution),
            observation_binding=cast(GoogleAdsCampaignPauseObservationBinding, observation),
            postcondition=cast(GoogleAdsCampaignPauseConfigurationPostcondition, postcondition),
            plan=cast(CompositePlan, plan),
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("planned action") from None


def _assert_action_instance(action: GoogleAdsCampaignPausePlannedAction) -> None:
    if type(action) is not GoogleAdsCampaignPausePlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, GoogleAdsCampaignPauseWorkspaceBinding)
    account = _checked_member(action.account_binding, GoogleAdsCampaignPauseAccountBinding)
    execution = _checked_member(action.execution_binding, GoogleAdsCampaignPauseExecutionBinding)
    observation = _checked_member(
        action.observation_binding, GoogleAdsCampaignPauseObservationBinding
    )
    postcondition = _checked_member(
        action.postcondition, GoogleAdsCampaignPauseConfigurationPostcondition
    )
    plan = _checked_member(action.plan, CompositePlan)
    expiry = _checked_expiry(plan.expires_at)
    _assert_outer_bindings(
        account=account, execution=execution, observation=observation, expires_at=expiry
    )
    expected_postcondition = _postcondition(account, observation)
    if postcondition != expected_postcondition:
        raise ValueError("postcondition differs from the exact baseline")
    expected_action_id = _action_id(
        workspace=workspace,
        account=account,
        execution=execution,
        observation=observation,
        postcondition=postcondition,
        expires_at=expiry,
    )
    if action.action_id != expected_action_id:
        raise ValueError("action ID differs from complete action facts")
    expected_plan = _build_plan(
        action_id=expected_action_id,
        workspace=workspace,
        account=account,
        execution=execution,
        observation=observation,
        postcondition=postcondition,
        expires_at=expiry,
    )
    if plan != expected_plan:
        raise ValueError("plan differs from reconstructed Google Ads pause action")


def revalidate_google_ads_campaign_pause_planned_action(
    value: object,
) -> GoogleAdsCampaignPausePlannedAction:
    """Strictly parse and reconstruct one closed locally retained action.

    This verifies only self-consistency.  It cannot prove current customer or
    campaign state, dual-slot continuity, provider access, authorization, or a
    mutation outcome.  A later guarded workflow must freshly re-read and
    reauthorize before any provider write.
    """

    try:
        raw = _raw_mapping(value)
        action = _action_from_raw(raw)
        _assert_action_instance(action)
        if canonical_bytes(raw) != canonical_bytes(
            action.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("action canonical shape")
        return action
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


__all__ = [
    "GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION",
    "GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION",
    "GOOGLE_ADS_CAMPAIGN_PAUSE_EGRESS_PURPOSE",
    "GOOGLE_ADS_CAMPAIGN_PAUSE_HOST",
    "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS",
    "GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE",
    "GoogleAdsCampaignPauseAccountBinding",
    "GoogleAdsCampaignPauseCampaignBaseline",
    "GoogleAdsCampaignPauseConfigurationPostcondition",
    "GoogleAdsCampaignPauseExecutionBinding",
    "GoogleAdsCampaignPauseObservationBinding",
    "GoogleAdsCampaignPausePlannedAction",
    "GoogleAdsCampaignPauseWorkspaceBinding",
    "build_google_ads_campaign_pause_plan",
    "revalidate_google_ads_campaign_pause_planned_action",
]

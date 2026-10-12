"""Closed, pure Meta Graph v26 campaign-pause plan reconstruction.

This module turns already validated local facts into one exact, non-authorizing
``CompositePlan``.  It deliberately has no configuration, credential,
transport, ledger, native-approval, or provider-mutation dependency.  A later
guarded workflow must obtain the facts again immediately before egress.

The contract represents only the narrow configured-status transition
``ACTIVE`` -> ``PAUSED``.  It does not predict effective delivery, provider
write eligibility, conditional-write support, idempotency behavior, or a live
outcome.
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

META_CAMPAIGN_PAUSE_PLAN_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
META_CAMPAIGN_PAUSE_HOST: Final[Literal["graph.facebook.com"]] = "graph.facebook.com"
META_CAMPAIGN_PAUSE_API_VERSION: Final[Literal["v26.0"]] = "v26.0"
META_CAMPAIGN_PAUSE_CAPABILITY_VERSION: Final = "meta-graph-v26-campaign-pause-contract-2026-09-06"
META_CAMPAIGN_PAUSE_EGRESS_PURPOSE: Final = "fixed Meta Graph v26 campaign pause"
META_CAMPAIGN_PAUSE_SCOPE: Final[Literal["ads_management"]] = "ads_management"

_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_NATIVE_ID = re.compile(r"^[1-9][0-9]{0,39}$", re.ASCII)
_ACCOUNT_CLIENT_REF = re.compile(r"^act_([1-9][0-9]{0,39})$", re.ASCII)
_ACCOUNT_STATUS = re.compile(r"^(?:0|[1-9][0-9]{0,9})$", re.ASCII)
_PRINCIPAL_REF = re.compile(r"^meta_token_principal_v1\.[a-f0-9]{64}$", re.ASCII)
_CREDENTIAL_BINDING_REF = re.compile(r"^credential-binding:sha256:[a-f0-9]{64}$", re.ASCII)
_SAFE_WORKSPACE_REF = re.compile(r"^[a-zA-Z0-9_.:-]{1,160}$", re.ASCII)
_SAFE_PROFILE_REF = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$", re.ASCII)
_META_CURRENCIES: Final = frozenset(
    {
        "AUD",
        "CAD",
        "CHF",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "JPY",
        "MXN",
        "NOK",
        "NZD",
        "SEK",
        "SGD",
        "USD",
    }
)
_EFFECTIVE_STATUSES: Final = frozenset(
    {"ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"}
)
_MAX_READ_INTERVAL: Final = timedelta(minutes=5)
_MAX_LOCAL_FRESHNESS: Final = timedelta(minutes=15)
_MAX_ACTION_BYTES: Final = 131_072
_BEFORE_REVISION: Final = "META_GRAPH_V26_ALLOWLISTED_REMOTE_STATE_V1"
_AFTER_REVISION: Final = "META_GRAPH_V26_PAUSE_CONFIGURATION_POSTCONDITION_V1"


def _invalid_plan(message: str = "Meta campaign pause plan data is invalid.") -> AdhelmError:
    return AdhelmError(
        "INVALID_META_CAMPAIGN_PAUSE_PLAN", message, exit_code=ExitCode.INVALID_INPUT
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
    """Accept an aware value or its one canonical JSON UTC representation."""

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


class MetaCampaignPauseWorkspaceBinding(StrictModel):
    """The mandatory local ledger workspace, distinct from the EDIT profile."""

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


class MetaCampaignPauseAccountBinding(StrictModel):
    """Closed allowlisted remote Meta account baseline.

    ``account_status`` is retained exactly as the canonical tagged SDK integer
    representation.  It is not mapped to an invented eligibility state.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_GRAPH_V26_ACCOUNT_BASELINE"] = "META_GRAPH_V26_ACCOUNT_BASELINE"
    account_ref: SafeRef
    account_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: str
    account_status: str
    provider_api_version: Literal["v26.0"] = META_CAMPAIGN_PAUSE_API_VERSION

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "account_ref", "account_client_ref", "timezone", "currency", "account_status", mode="before"
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("account baseline scalar is invalid")
        return value

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("account timezone must be an IANA timezone") from exc
        return value

    @field_validator("currency")
    @classmethod
    def reviewed_currency(cls, value: str) -> str:
        if value not in _META_CURRENCIES:
            raise ValueError("account currency has no reviewed Meta conversion contract")
        return value

    @field_validator("account_status")
    @classmethod
    def canonical_account_status(cls, value: str) -> str:
        if _ACCOUNT_STATUS.fullmatch(value) is None:
            raise ValueError("account status is not canonical")
        return value

    @model_validator(mode="after")
    def exact_meta_account_identity(self) -> MetaCampaignPauseAccountBinding:
        match = _ACCOUNT_CLIENT_REF.fullmatch(self.account_client_ref)
        if match is None or self.account_ref != f"meta_ads.account.{self.account_client_ref}":
            raise ValueError("account references do not bind the Meta account")
        return self


class MetaCampaignPauseExecutionBinding(StrictModel):
    """Closed nonsecret EDIT facts needed to reconstruct ``AccountIdentity``.

    It proves only a local, inspected token/profile association.  It does not
    prove current campaign access or provider permission to change status.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_ADS_EDIT_LOCAL_TOKEN_ASSOCIATION"] = (
        "META_ADS_EDIT_LOCAL_TOKEN_ASSOCIATION"
    )
    profile_ref: str
    client_application_ref: str
    principal_ref: str
    required_scope: Literal["ads_management"] = META_CAMPAIGN_PAUSE_SCOPE
    credential_binding_ref: str
    configuration_digest: str
    profile_configuration_digest: str
    inspected_at: datetime
    expires_at: datetime
    data_access_expires_at: datetime
    local_association_status: Literal["LOCAL_INSPECTED_TOKEN_ASSOCIATION_ONLY"] = (
        "LOCAL_INSPECTED_TOKEN_ASSOCIATION_ONLY"
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
    def native_application_identifier(cls, value: str) -> str:
        if _NATIVE_ID.fullmatch(value) is None:
            raise ValueError("client application identifier is invalid")
        return value

    @field_validator("principal_ref")
    @classmethod
    def derived_principal_identifier(cls, value: str) -> str:
        if _PRINCIPAL_REF.fullmatch(value) is None:
            raise ValueError("principal reference is invalid")
        return value

    @field_validator("credential_binding_ref")
    @classmethod
    def credential_binding_identifier(cls, value: str) -> str:
        if _CREDENTIAL_BINDING_REF.fullmatch(value) is None:
            raise ValueError("credential binding reference is invalid")
        return value

    @field_validator("configuration_digest", "profile_configuration_digest")
    @classmethod
    def exact_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("execution binding digest is invalid")
        return value

    @field_validator("inspected_at", "expires_at", "data_access_expires_at", mode="before")
    @classmethod
    def exact_time_input(cls, value: object) -> datetime:
        return _wire_or_aware_utc(value, label="execution binding time")

    @model_validator(mode="after")
    def inspected_before_both_expiries(self) -> MetaCampaignPauseExecutionBinding:
        if self.inspected_at >= self.expires_at or self.inspected_at >= self.data_access_expires_at:
            raise ValueError("EDIT inspection is not strictly before both expiries")
        return self


class MetaCampaignPauseCampaignBaseline(StrictModel):
    """The complete allowlisted v26 observation for one eligible campaign."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_GRAPH_V26_CAMPAIGN_BASELINE"] = "META_GRAPH_V26_CAMPAIGN_BASELINE"
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    account_client_ref: SafeRef
    configured_status: Literal["ACTIVE"] = "ACTIVE"
    effective_status: Literal[
        "ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"
    ]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "campaign_ref",
        "campaign_client_ref",
        "account_client_ref",
        "configured_status",
        "effective_status",
        mode="before",
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("campaign baseline scalar is invalid")
        return value

    @model_validator(mode="after")
    def exact_campaign_account_association(self) -> MetaCampaignPauseCampaignBaseline:
        if (
            _NATIVE_ID.fullmatch(self.campaign_client_ref) is None
            or _NATIVE_ID.fullmatch(self.account_client_ref) is None
            or self.campaign_ref != f"meta_ads.campaign.{self.campaign_client_ref}"
            or self.effective_status not in _EFFECTIVE_STATUSES
        ):
            raise ValueError("campaign baseline does not bind the fixed Meta observation")
        return self


class MetaCampaignPauseObservationBinding(StrictModel):
    """One bounded, sequential account/campaign read interval."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["META_GRAPH_V26_CAMPAIGN_STATE_OBSERVATION"] = (
        "META_GRAPH_V26_CAMPAIGN_STATE_OBSERVATION"
    )
    campaign: MetaCampaignPauseCampaignBaseline
    read_started_at: datetime
    read_finished_at: datetime
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("campaign", mode="before")
    @classmethod
    def exact_campaign(cls, value: object) -> MetaCampaignPauseCampaignBaseline | dict[str, object]:
        if type(value) is MetaCampaignPauseCampaignBaseline:
            return value
        if type(value) is not dict:
            raise ValueError("campaign observation requires a closed campaign baseline")
        return value

    @field_validator("read_started_at", "read_finished_at", mode="before")
    @classmethod
    def exact_time_input(cls, value: object) -> datetime:
        return _wire_or_aware_utc(value, label="campaign observation time")

    @model_validator(mode="after")
    def bounded_read_interval(self) -> MetaCampaignPauseObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("campaign observation interval is reversed")
        if self.read_finished_at - self.read_started_at > _MAX_READ_INTERVAL:
            raise ValueError("campaign observation interval exceeds the local bound")
        return self


class MetaCampaignPauseConfigurationPostcondition(StrictModel):
    """Closed configuration-only pause postcondition.

    Effective delivery is deliberately not predicted.  A later adapter must
    retain a fresh actual effective-status observation and classify any
    uncertain acknowledgement separately.
    """

    schema_version: Literal[1] = 1
    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    account_ref: SafeRef
    account_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: str
    account_status: str
    provider_api_version: Literal["v26.0"] = META_CAMPAIGN_PAUSE_API_VERSION
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    campaign_account_client_ref: SafeRef
    configured_status: Literal["PAUSED"] = "PAUSED"
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "account_ref",
        "account_client_ref",
        "timezone",
        "currency",
        "account_status",
        "campaign_ref",
        "campaign_client_ref",
        "campaign_account_client_ref",
        mode="before",
    )
    @classmethod
    def exact_string_scalars(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("pause postcondition scalar is invalid")
        return value

    @model_validator(mode="after")
    def exact_unchanged_association(self) -> MetaCampaignPauseConfigurationPostcondition:
        account = MetaCampaignPauseAccountBinding(
            account_ref=self.account_ref,
            account_client_ref=self.account_client_ref,
            timezone=self.timezone,
            currency=self.currency,
            account_status=self.account_status,
            provider_api_version=self.provider_api_version,
        )
        campaign = MetaCampaignPauseCampaignBaseline(
            campaign_ref=self.campaign_ref,
            campaign_client_ref=self.campaign_client_ref,
            account_client_ref=self.campaign_account_client_ref,
            configured_status="ACTIVE",
            effective_status="ACTIVE",
        )
        if campaign.account_client_ref != account.account_client_ref.removeprefix("act_"):
            raise ValueError("pause postcondition changes the campaign account association")
        return self


def _postcondition(
    account: MetaCampaignPauseAccountBinding,
    observation: MetaCampaignPauseObservationBinding,
) -> MetaCampaignPauseConfigurationPostcondition:
    campaign = observation.campaign
    return MetaCampaignPauseConfigurationPostcondition(
        account_ref=account.account_ref,
        account_client_ref=account.account_client_ref,
        timezone=account.timezone,
        currency=account.currency,
        account_status=account.account_status,
        provider_api_version=account.provider_api_version,
        campaign_ref=campaign.campaign_ref,
        campaign_client_ref=campaign.campaign_client_ref,
        campaign_account_client_ref=campaign.account_client_ref,
    )


def _before_state(
    account: MetaCampaignPauseAccountBinding,
    observation: MetaCampaignPauseObservationBinding,
) -> StateRef:
    """Fingerprint remote facts only; local observation/provenance stays outside it."""

    return StateRef(
        fingerprint=digest(
            {
                "state_kind": _BEFORE_REVISION,
                "account": account.model_dump(mode="json", warnings="error"),
                "campaign": observation.campaign.model_dump(mode="json", warnings="error"),
            }
        ),
    )


def _after_state(postcondition: MetaCampaignPauseConfigurationPostcondition) -> StateRef:
    return StateRef(
        fingerprint=digest(
            {
                "state_kind": _AFTER_REVISION,
                "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            }
        ),
    )


def _action_id(
    *,
    workspace: MetaCampaignPauseWorkspaceBinding,
    account: MetaCampaignPauseAccountBinding,
    execution: MetaCampaignPauseExecutionBinding,
    observation: MetaCampaignPauseObservationBinding,
    postcondition: MetaCampaignPauseConfigurationPostcondition,
    expires_at: datetime,
) -> str:
    """Commit every outer local and remote fact to the authorizable plan ID."""

    return digest(
        {
            "kind": "meta-campaign-pause-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "account": account.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "postcondition": postcondition.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
            "status": META_CAMPAIGN_PAUSE_PLAN_STATUS,
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
    workspace: MetaCampaignPauseWorkspaceBinding,
    account: MetaCampaignPauseAccountBinding,
    execution: MetaCampaignPauseExecutionBinding,
    observation: MetaCampaignPauseObservationBinding,
    postcondition: MetaCampaignPauseConfigurationPostcondition,
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
        scopes=(META_CAMPAIGN_PAUSE_SCOPE,),
    )
    subplan = SubPlan(
        subplan_id=f"meta-campaign-pause-{action_id}",
        provider="meta_ads",
        operation=OperationKind.PAUSE,
        account=identity,
        provider_api_version=META_CAMPAIGN_PAUSE_API_VERSION,
        capability_version=META_CAMPAIGN_PAUSE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(FieldChange(path="campaign/configured-status", before=before, after=after),),
        max_spend=Money(minor_units=0, currency=account.currency),
        egress=(
            EgressTarget(
                host=META_CAMPAIGN_PAUSE_HOST,
                purpose=META_CAMPAIGN_PAUSE_EGRESS_PURPOSE,
            ),
        ),
        # This is only a bounded candidate for a later reconciliation and a
        # newly authorized exact plan.  It never permits a blind retry.
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"meta-campaign-pause-{action_id}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _assert_outer_bindings(
    *,
    account: MetaCampaignPauseAccountBinding,
    execution: MetaCampaignPauseExecutionBinding,
    observation: MetaCampaignPauseObservationBinding,
    expires_at: datetime,
) -> None:
    if observation.campaign.account_client_ref != account.account_client_ref.removeprefix("act_"):
        raise ValueError("campaign observation is outside the account baseline")
    if execution.inspected_at > observation.read_started_at:
        raise ValueError("EDIT inspection occurs after the campaign read began")
    if observation.read_finished_at >= execution.expires_at:
        raise ValueError("campaign read finished after the inspected token expiry")
    if observation.read_finished_at >= execution.data_access_expires_at:
        raise ValueError("campaign read finished after the inspected data-access expiry")
    if expires_at <= observation.read_finished_at:
        raise ValueError("plan expiry must follow the completed campaign read")
    if expires_at > execution.expires_at or expires_at > execution.data_access_expires_at:
        raise ValueError("plan expiry exceeds an inspected expiry")
    if expires_at > observation.read_finished_at + _MAX_LOCAL_FRESHNESS:
        raise ValueError("plan expiry exceeds the local observation freshness bound")


class MetaCampaignPausePlannedAction(StrictModel):
    """One exact Meta campaign PAUSE action, without execution authority."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = META_CAMPAIGN_PAUSE_PLAN_STATUS
    action_id: str
    workspace_binding: MetaCampaignPauseWorkspaceBinding
    account_binding: MetaCampaignPauseAccountBinding
    execution_binding: MetaCampaignPauseExecutionBinding
    observation_binding: MetaCampaignPauseObservationBinding
    postcondition: MetaCampaignPauseConfigurationPostcondition
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
            "workspace_binding": MetaCampaignPauseWorkspaceBinding,
            "account_binding": MetaCampaignPauseAccountBinding,
            "execution_binding": MetaCampaignPauseExecutionBinding,
            "observation_binding": MetaCampaignPauseObservationBinding,
            "postcondition": MetaCampaignPauseConfigurationPostcondition,
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
    def exact_reconstructed_action(self) -> MetaCampaignPausePlannedAction:
        _assert_action_instance(self)
        return self

    def to_data(self) -> dict[str, object]:
        """Return only the closed validated serializable action data."""

        checked = revalidate_meta_campaign_pause_planned_action(self)
        try:
            data = checked.model_dump(mode="json", warnings="error")
        except Exception:
            raise _invalid_plan() from None
        if type(data) is not dict:
            raise _invalid_plan()
        return cast(dict[str, object], data)


def _strict_json_bytes(value: object, *, label: str) -> bytes:
    """Encode untrusted JSON without normalizing number types first."""

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


def build_meta_campaign_pause_plan(
    *,
    workspace_profile_ref: str,
    account_binding: MetaCampaignPauseAccountBinding,
    execution_binding: MetaCampaignPauseExecutionBinding,
    observation_binding: MetaCampaignPauseObservationBinding,
    expires_at: datetime,
) -> MetaCampaignPausePlannedAction:
    """Build the one fixed non-authorizing Meta configured-status pause plan."""

    try:
        workspace = MetaCampaignPauseWorkspaceBinding(workspace_profile_ref=workspace_profile_ref)
        account = _checked_member(account_binding, MetaCampaignPauseAccountBinding)
        execution = _checked_member(execution_binding, MetaCampaignPauseExecutionBinding)
        observation = _checked_member(observation_binding, MetaCampaignPauseObservationBinding)
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
        return MetaCampaignPausePlannedAction(
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
    if type(value) is MetaCampaignPausePlannedAction:
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


def _action_from_raw(data: dict[str, object]) -> MetaCampaignPausePlannedAction:
    _require_exact_keys(data, _ACTION_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    if data["status"] != META_CAMPAIGN_PAUSE_PLAN_STATUS:
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
    workspace = _raw_member(data, "workspace_binding", MetaCampaignPauseWorkspaceBinding)
    account = _raw_member(data, "account_binding", MetaCampaignPauseAccountBinding)
    execution = _raw_member(data, "execution_binding", MetaCampaignPauseExecutionBinding)
    observation = _raw_member(data, "observation_binding", MetaCampaignPauseObservationBinding)
    postcondition = _raw_member(data, "postcondition", MetaCampaignPauseConfigurationPostcondition)
    plan = _raw_member(data, "plan", CompositePlan)
    try:
        action = MetaCampaignPausePlannedAction(
            action_id=data["action_id"],
            workspace_binding=cast(MetaCampaignPauseWorkspaceBinding, workspace),
            account_binding=cast(MetaCampaignPauseAccountBinding, account),
            execution_binding=cast(MetaCampaignPauseExecutionBinding, execution),
            observation_binding=cast(MetaCampaignPauseObservationBinding, observation),
            postcondition=cast(MetaCampaignPauseConfigurationPostcondition, postcondition),
            plan=cast(CompositePlan, plan),
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("planned action") from None
    return action


def _assert_action_instance(action: MetaCampaignPausePlannedAction) -> None:
    if type(action) is not MetaCampaignPausePlannedAction:
        raise ValueError("action type")
    workspace = _checked_member(action.workspace_binding, MetaCampaignPauseWorkspaceBinding)
    account = _checked_member(action.account_binding, MetaCampaignPauseAccountBinding)
    execution = _checked_member(action.execution_binding, MetaCampaignPauseExecutionBinding)
    observation = _checked_member(action.observation_binding, MetaCampaignPauseObservationBinding)
    postcondition = _checked_member(
        action.postcondition, MetaCampaignPauseConfigurationPostcondition
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
        raise ValueError("plan differs from reconstructed Meta pause action")


def revalidate_meta_campaign_pause_planned_action(value: object) -> MetaCampaignPausePlannedAction:
    """Strictly parse and reconstruct a closed locally retained pause action.

    This checks self-consistency only.  It cannot establish current campaign
    state, profile continuity, provider access, authorization, or an outcome.
    A future guarded workflow must re-read and reauthorize before any POST.
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
    "META_CAMPAIGN_PAUSE_API_VERSION",
    "META_CAMPAIGN_PAUSE_CAPABILITY_VERSION",
    "META_CAMPAIGN_PAUSE_EGRESS_PURPOSE",
    "META_CAMPAIGN_PAUSE_HOST",
    "META_CAMPAIGN_PAUSE_PLAN_STATUS",
    "META_CAMPAIGN_PAUSE_SCOPE",
    "MetaCampaignPauseAccountBinding",
    "MetaCampaignPauseCampaignBaseline",
    "MetaCampaignPauseConfigurationPostcondition",
    "MetaCampaignPauseExecutionBinding",
    "MetaCampaignPauseObservationBinding",
    "MetaCampaignPausePlannedAction",
    "MetaCampaignPauseWorkspaceBinding",
    "build_meta_campaign_pause_plan",
    "revalidate_meta_campaign_pause_planned_action",
]

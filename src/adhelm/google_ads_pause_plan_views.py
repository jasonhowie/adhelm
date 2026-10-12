"""Closed historical sharing views for retained Google Ads PAUSE plans.

The sole raw-reference helper in this module is private and intended only for
``projections._redact_payload`` immediately before its recursive HMAC pass.
It is never a serializable or caller-facing contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_plan_records import (
    GoogleAdsCampaignPausePlanRecord,
    revalidate_google_ads_campaign_pause_plan_record,
)
from adhelm.models import StrictModel
from adhelm.safety import Money
from adhelm.strict_json import validate_json_value

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueRef = Annotated[str, Field(pattern=r"^opaque:[0-9a-f]{64}$")]
_MAX_REVIEW_OUTPUT_BYTES = 65_536
_PROBE_KEY = b"\0" * 32


def _invalid_review() -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_PAUSE_PLAN_REVIEW",
        "The retained Google Ads campaign PAUSE plan cannot be reviewed safely.",
        exit_code=ExitCode.INVALID_INPUT,
    )


class GoogleAdsCampaignPauseBaselineReview(StrictModel):
    """The one historical configured campaign state admitted by the plan."""

    configured_status: Literal["ENABLED"] = "ENABLED"

    @field_validator("configured_status", mode="before")
    @classmethod
    def exact_status_string(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("campaign status must be a string")
        return value


class GoogleAdsCampaignPausePostconditionReview(StrictModel):
    """A configuration-only postcondition, never a delivery claim."""

    postcondition_kind: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    configured_status: Literal["PAUSED"] = "PAUSED"
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    configuration_only: Literal[True] = True

    @field_validator("configuration_only", mode="before")
    @classmethod
    def exact_true(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("configuration-only marker must be true")
        return value


class GoogleAdsCampaignPauseObservationIntervalReview(StrictModel):
    """The bounded sequential customer/campaign observation interval."""

    read_started_at: datetime
    read_finished_at: datetime
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_interval(self) -> GoogleAdsCampaignPauseObservationIntervalReview:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("observation interval is reversed")
        return self


class _GoogleAdsCampaignPausePlanReviewSurface(StrictModel):
    """Closed agent-visible historical facts from one Google Ads plan."""

    schema_version: Literal[1] = 1
    event_type: Literal["GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"] = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    provider: Literal["google_ads"] = "google_ads"
    operation: Literal["PAUSE"] = "PAUSE"
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    action_digest: Digest
    plan_digest: Digest
    subplan_digests: tuple[Digest, ...] = Field(min_length=1, max_length=1)
    workspace_profile_ref: OpaqueRef
    account_ref: OpaqueRef
    account_client_ref: OpaqueRef
    campaign_ref: OpaqueRef
    campaign_client_ref: OpaqueRef
    auth_profile_ref: OpaqueRef
    client_application_ref: OpaqueRef
    login_customer_client_ref: OpaqueRef | None
    account_timezone: str = Field(min_length=1, max_length=80)
    account_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    account_status: Literal["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"]
    provider_api_version: Literal["v25"] = "v25"
    baseline: GoogleAdsCampaignPauseBaselineReview
    postcondition: GoogleAdsCampaignPausePostconditionReview
    observation_interval: GoogleAdsCampaignPauseObservationIntervalReview
    maximum_incremental_spend: Money
    expires_at: datetime
    reviewed_at: datetime
    expiry_status: Literal["NOT_EXPIRED_AT_REVIEW", "EXPIRED_AT_REVIEW"]
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"]
    configuration_readback: Literal["REQUIRED_AFTER_PAUSE"]
    recovery_semantics: Literal["RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"]
    provider_conditional_write: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider_idempotency: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    concurrency_limitation: Literal["LOCAL_BASELINE_NOT_PROVIDER_COMPARE_AND_SWAP"] = (
        "LOCAL_BASELINE_NOT_PROVIDER_COMPARE_AND_SWAP"
    )
    effective_delivery_limitation: Literal[
        "CONFIGURED_PAUSE_DOES_NOT_PROVE_EFFECTIVE_DELIVERY_OR_ZERO_RESIDUAL_SPEND"
    ] = "CONFIGURED_PAUSE_DOES_NOT_PROVE_EFFECTIVE_DELIVERY_OR_ZERO_RESIDUAL_SPEND"
    remote_side_effects: Literal[False] = False
    execution_authority: Literal[False] = False
    review_authorizes_execution: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("account_timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("account timezone must remain IANA") from exc
        return value

    @field_validator("expires_at", "reviewed_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review times must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator(
        "remote_side_effects",
        "execution_authority",
        "review_authorizes_execution",
        mode="before",
    )
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("review authority controls must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_true_control(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("review kernel control must be true")
        return value

    @model_validator(mode="after")
    def review_facts_are_historical_and_bound(self) -> _GoogleAdsCampaignPausePlanReviewSurface:
        if self.maximum_incremental_spend.minor_units != 0:
            raise ValueError("pause review cannot claim incremental spend")
        if self.maximum_incremental_spend.currency != self.account_currency:
            raise ValueError("pause review currency must bind the account")
        expected_expiry = (
            "EXPIRED_AT_REVIEW" if self.expires_at <= self.reviewed_at else "NOT_EXPIRED_AT_REVIEW"
        )
        if self.expiry_status != expected_expiry:
            raise ValueError("review expiry status is inconsistent")
        if self.reviewed_at < self.observation_interval.read_finished_at:
            raise ValueError("review cannot predate the completed observation")
        return self


class GoogleAdsCampaignPausePlanReview(_GoogleAdsCampaignPausePlanReviewSurface):
    """Bounded redacted review of a historical Google Ads PAUSE plan."""

    @property
    def review_digest(self) -> str:
        return digest(self)

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_review(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        try:
            if type(self) is not GoogleAdsCampaignPausePlanReview:
                raise ValueError("review type")
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("review output")
            validate_json_value(
                _GoogleAdsCampaignPausePlanReviewSurface,
                cast(dict[str, Any], _review_json_shape(output)),
            )
            return output
        except (
            AdhelmError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("Google Ads campaign PAUSE review is invalid") from None


def _review_json_shape(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if type(value) is tuple or type(value) is list:
        return [_review_json_shape(item) for item in value]
    if type(value) is dict:
        return {key: _review_json_shape(item) for key, item in value.items()}
    return value


def _google_ads_campaign_pause_plan_export_hmac_input(value: object) -> dict[str, Any]:
    """Return the private allowlisted HMAC preimage for sharing projections only."""

    record = revalidate_google_ads_campaign_pause_plan_record(value)
    action = record.action
    account = action.account_binding
    execution = action.execution_binding
    observation = action.observation_binding
    campaign = observation.campaign
    postcondition = action.postcondition
    return {
        "schema_version": 1,
        "event_type": "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN",
        "provider": "google_ads",
        "operation": "PAUSE",
        "capability_status": action.status,
        "action_digest": record.action_digest,
        "plan_digest": record.plan_digest,
        "subplan_digests": list(action.plan.subplan_digests),
        "workspace_profile_ref": action.workspace_binding.workspace_profile_ref,
        "account_ref": account.account_ref,
        "account_client_ref": account.account_client_ref,
        "campaign_ref": campaign.campaign_ref,
        "campaign_client_ref": campaign.campaign_client_ref,
        "auth_profile_ref": execution.profile_ref,
        "client_application_ref": execution.client_application_ref,
        "login_customer_client_ref": execution.login_customer_client_ref,
        "account_timezone": account.timezone,
        "account_currency": account.currency,
        "account_status": account.account_status,
        "provider_api_version": account.provider_api_version,
        "baseline": {"configured_status": campaign.status},
        "postcondition": {
            "postcondition_kind": postcondition.postcondition_kind,
            "configured_status": postcondition.configured_status,
            "effective_delivery": postcondition.effective_delivery,
            "configuration_only": True,
        },
        "observation_interval": {
            "read_started_at": observation.read_started_at.isoformat(),
            "read_finished_at": observation.read_finished_at.isoformat(),
            "consistency": observation.consistency,
        },
        "maximum_incremental_spend": record.maximum_incremental_spend.model_dump(mode="json"),
        "expires_at": record.expires_at.isoformat(),
        "historical_evidence_status": record.historical_evidence_status,
        "current_state_status": action.current_state_status,
        "configuration_readback": action.configuration_readback,
        "recovery_semantics": action.recovery_semantics,
        "provider_conditional_write": "NOT_DOCUMENTED",
        "provider_idempotency": "NOT_DOCUMENTED",
        "concurrency_limitation": "LOCAL_BASELINE_NOT_PROVIDER_COMPARE_AND_SWAP",
        "effective_delivery_limitation": (
            "CONFIGURED_PAUSE_DOES_NOT_PROVE_EFFECTIVE_DELIVERY_OR_ZERO_RESIDUAL_SPEND"
        ),
        "remote_side_effects": False,
        "execution_authority": False,
        "review_authorizes_execution": False,
        "requires_current_kernel_checks": True,
    }


def _checked_opaque_key(value: object) -> bytes:
    if type(value) is not bytes or not 32 <= len(value) <= 4_096:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED",
            "Plan review requires a private local sharing key of at least 32 bytes.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _checked_reviewed_at(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AdhelmError(
            "CLOCK_INVALID",
            "Plan review requires an aware UTC instant.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value.astimezone(UTC)


def _checked_output_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVIEW_OUTPUT_BYTES:
        raise AdhelmError(
            "INVALID_OUTPUT_LIMIT",
            "Plan review output limit must be an integer from 1 through 65536 bytes.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _review_from_record(
    record: GoogleAdsCampaignPausePlanRecord, *, opaque_key: bytes, reviewed_at: datetime
) -> GoogleAdsCampaignPausePlanReview:
    try:
        from adhelm.projections import _redact_payload

        raw = record.model_dump(mode="json", warnings="error")
        data = _redact_payload(raw, opaque_key, False)
        if type(data) is not dict:
            raise ValueError("redacted review must be an object")
        review_data = dict(cast(dict[str, Any], data))
        review_data["reviewed_at"] = reviewed_at.isoformat()
        review_data["expiry_status"] = (
            "EXPIRED_AT_REVIEW" if record.expires_at <= reviewed_at else "NOT_EXPIRED_AT_REVIEW"
        )
        return validate_json_value(GoogleAdsCampaignPausePlanReview, review_data)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_review() from None


def review_google_ads_campaign_pause_plan(
    value: object,
    *,
    opaque_key: bytes,
    reviewed_at: datetime,
    max_output_bytes: int = _MAX_REVIEW_OUTPUT_BYTES,
) -> GoogleAdsCampaignPausePlanReview:
    """Return a bounded redacted historical review without a current grant."""

    key = _checked_opaque_key(opaque_key)
    now = _checked_reviewed_at(reviewed_at)
    maximum = _checked_output_limit(max_output_bytes)
    record = revalidate_google_ads_campaign_pause_plan_record(value)
    probe = _review_from_record(record, opaque_key=_PROBE_KEY, reviewed_at=now)
    try:
        if len(canonical_bytes(probe)) > maximum:
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "The complete Google Ads campaign PAUSE review exceeds the output limit.",
                exit_code=ExitCode.INVALID_INPUT,
                next_action="Increase max_output_bytes to inspect the complete historical review.",
            )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise _invalid_review() from None
    return _review_from_record(record, opaque_key=key, reviewed_at=now)


__all__ = ["GoogleAdsCampaignPausePlanReview", "review_google_ads_campaign_pause_plan"]

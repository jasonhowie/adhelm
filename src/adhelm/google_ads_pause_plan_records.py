"""Closed historical Google Ads campaign PAUSE plans, without execution authority."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement_records import Digest
from adhelm.models import StrictModel
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPausePlannedAction,
    revalidate_google_ads_campaign_pause_planned_action,
)
from adhelm.safety import Money, OperationKind
from adhelm.strict_json import validate_json_value


class GoogleAdsCampaignPausePlanRecord(StrictModel):
    """One exact local Google Ads campaign PAUSE plan retained as history."""

    schema_version: Literal[1] = 1
    event_type: Literal["GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"] = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    provider: Literal["google_ads"] = "google_ads"
    action: GoogleAdsCampaignPausePlannedAction
    action_digest: Digest
    plan_digest: Digest
    expires_at: datetime
    maximum_incremental_spend: Money
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"] = (
        "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    )
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("execution_authority", "requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_boolean_controls(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("plan record controls require exact booleans")
        return value

    @field_validator("action", mode="before")
    @classmethod
    def closed_action(cls, value: object) -> GoogleAdsCampaignPausePlannedAction:
        return revalidate_google_ads_campaign_pause_planned_action(value)

    @field_validator("expires_at", mode="before")
    @classmethod
    def canonical_utc_expiry(cls, value: object) -> datetime:
        """Accept a typed instant or the one canonical JSON UTC spelling."""

        if type(value) is datetime:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("expiry must be timezone-aware")
            return value.astimezone(UTC)
        if type(value) is not str or not value.endswith("Z"):
            raise ValueError("expiry must use canonical UTC text")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("expiry must use canonical UTC text") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("expiry must use canonical UTC text")
        normalized = parsed.astimezone(UTC)
        if normalized.isoformat().replace("+00:00", "Z") != value:
            raise ValueError("expiry must use canonical UTC text")
        return normalized

    @model_validator(mode="after")
    def exact_action_binding(self) -> "GoogleAdsCampaignPausePlanRecord":
        action = self.action
        if len(action.plan.subplans) != 1:
            raise ValueError("one exact Google Ads PAUSE subplan is required")
        subplan = action.plan.subplans[0]
        if (
            self.action_digest != digest(action)
            or self.plan_digest != action.plan.plan_digest
            or self.expires_at != action.plan.expires_at
            or subplan.provider != self.provider
            or subplan.operation is not OperationKind.PAUSE
            or self.maximum_incremental_spend != subplan.max_spend
            or self.maximum_incremental_spend.minor_units != 0
        ):
            raise ValueError("retained Google Ads PAUSE facts do not match the exact action")
        return self


def google_ads_campaign_pause_plan_record(
    action: GoogleAdsCampaignPausePlannedAction,
) -> GoogleAdsCampaignPausePlanRecord:
    """Reconstruct one complete non-authorizing record from an exact action."""

    checked = revalidate_google_ads_campaign_pause_planned_action(action)
    return GoogleAdsCampaignPausePlanRecord(
        action=checked,
        action_digest=digest(checked),
        plan_digest=checked.plan.plan_digest,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_google_ads_campaign_pause_plan_record(
    value: object,
) -> GoogleAdsCampaignPausePlanRecord:
    """Reject incomplete, aliased, or incoherent retained Google Ads facts."""

    try:
        if type(value) is GoogleAdsCampaignPausePlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            GoogleAdsCampaignPausePlanRecord.model_fields
        ):
            raise ValueError("a complete record is required")
        return validate_json_value(GoogleAdsCampaignPausePlanRecord, value)
    except Exception:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_RECORD",
            "A complete closed Google Ads campaign PAUSE plan record is required.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None

"""Closed local history for a GA4 key-event CREATE plan, without authority."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement_records import Digest
from adhelm.models import StrictModel
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreatePlannedAction,
    revalidate_ga4_key_event_create_planned_action,
)
from adhelm.safety import Money
from adhelm.strict_json import validate_json_value


class Ga4KeyEventCreatePlanRecord(StrictModel):
    schema_version: Literal[1] = 1
    event_type: Literal["GA4_KEY_EVENT_CREATE_PLAN"] = "GA4_KEY_EVENT_CREATE_PLAN"
    provider: Literal["ga4_admin"] = "ga4_admin"
    action: Ga4KeyEventCreatePlannedAction
    action_digest: Digest
    plan_digest: Digest
    expires_at: datetime
    maximum_incremental_spend: Money
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"] = (
        "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    )
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("execution_authority", "requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("plan record controls require exact booleans")
        return value

    @field_validator("action", mode="before")
    @classmethod
    def closed_action(cls, value: object) -> Ga4KeyEventCreatePlannedAction:
        return revalidate_ga4_key_event_create_planned_action(value)

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_action_binding(self) -> "Ga4KeyEventCreatePlanRecord":
        if (
            self.action_digest != digest(self.action)
            or self.plan_digest != self.action.plan.plan_digest
            or self.expires_at != self.action.plan.expires_at
            or len(self.action.plan.subplans) != 1
            or self.action.plan.subplans[0].provider != self.provider
            or self.maximum_incremental_spend != self.action.plan.subplans[0].max_spend
            or self.maximum_incremental_spend.minor_units != 0
        ):
            raise ValueError("retained GA4 CREATE plan facts do not match the action")
        return self


def ga4_key_event_create_plan_record(
    action: Ga4KeyEventCreatePlannedAction,
) -> Ga4KeyEventCreatePlanRecord:
    checked = revalidate_ga4_key_event_create_planned_action(action)
    return Ga4KeyEventCreatePlanRecord(
        action=checked,
        action_digest=digest(checked),
        plan_digest=checked.plan.plan_digest,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
    )


def revalidate_ga4_key_event_create_plan_record(value: object) -> Ga4KeyEventCreatePlanRecord:
    try:
        if type(value) is Ga4KeyEventCreatePlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(Ga4KeyEventCreatePlanRecord.model_fields):
            raise ValueError("complete record required")
        return validate_json_value(Ga4KeyEventCreatePlanRecord, value)
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_PLAN_RECORD",
            "A complete closed GA4 key-event CREATE plan record is required.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None

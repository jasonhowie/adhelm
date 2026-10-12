"""Closed durable records for one reviewed OpenAI Ads pause action.

The record is local history only.  It does not issue authority, prove fresh
remote state, or expose a generic provider-payload store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.openai_pause_contract import (
    OPENAI_PAUSE_STATUS,
    OpenAIPausePlannedAction,
)
from adhelm.safety import Money

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_JSON_MAPPING = TypeAdapter(dict[str, Any])


def _same_shape(raw: object, checked: object) -> bool:
    """Reject omitted defaults from a constructed nested action.

    The retained form is a complete reconstruction, including false-authority
    and baseline fields.  Values are still validated separately by the closed
    action model, so this only compares container shape.
    """
    if isinstance(checked, dict):
        return (
            isinstance(raw, dict)
            and set(raw) == set(checked)
            and all(_same_shape(raw[key], value) for key, value in checked.items())
        )
    if isinstance(checked, list):
        return (
            isinstance(raw, list)
            and len(raw) == len(checked)
            and all(_same_shape(item, value) for item, value in zip(raw, checked, strict=True))
        )
    return True


def _exact_action_control_scalars(raw: dict[str, Any]) -> None:
    """Preserve strict JSON types that Pydantic ``Literal`` would otherwise merge."""
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("action schema_version must be the integer 1")
    plan = raw.get("plan")
    if not isinstance(plan, dict):
        return
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise ValueError("plan schema_version must be the integer 1")
    if (
        type(plan.get("stop_on_non_verified")) is not bool
        or plan["stop_on_non_verified"] is not True
    ):
        raise ValueError("plan stop_on_non_verified must be true")


def _require_json_shaped(value: object, *, depth: int = 0) -> None:
    """Reject Python values that a serializer could silently coerce into JSON."""
    if depth > 16:
        raise ValueError("OpenAI pause action exceeds the allowed JSON depth")
    if value is None or type(value) in {bool, int, float, str}:
        return
    if type(value) is list:
        for item in value:
            _require_json_shaped(item, depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("OpenAI pause action JSON keys must be strings")
            _require_json_shaped(item, depth=depth + 1)
        return
    raise ValueError("OpenAI pause action must contain JSON values")


def _closed_action(value: object) -> OpenAIPausePlannedAction:
    """Reparse every action through strict JSON, including constructed models."""
    try:
        if isinstance(value, BaseModel):
            raw_json = value.model_dump_json(warnings="error").encode("utf-8")
        elif isinstance(value, dict):
            _require_json_shaped(value)
            # Apply JCS size, integer-domain, and finite-number checks without
            # feeding its normalized bytes to Pydantic. The serializer below
            # preserves a raw ``1.0`` for strict model validation.
            canonical_bytes(value)
            raw_json = _JSON_MAPPING.dump_json(value, warnings="error")
        else:
            raise ValueError("OpenAI pause action must be an object")
    except Exception as exc:
        # Pydantic serializer warnings can interpolate an arbitrary nested
        # object's repr. Do not let a constructed input surface it at a public
        # retention boundary.
        raise ValueError("OpenAI pause action cannot be safely serialized") from exc
    raw = parse_json(raw_json)
    if not isinstance(raw, dict):
        raise ValueError("OpenAI pause action must be an object")
    _exact_action_control_scalars(raw)
    checked = OpenAIPausePlannedAction.model_validate_json(raw_json)
    if not _same_shape(raw, checked.model_dump(mode="json")):
        raise ValueError("OpenAI pause action must include every closed field")
    return checked


def revalidate_openai_pause_action(value: object) -> OpenAIPausePlannedAction:
    """Return the installed closed action or a safe storage-boundary error."""
    try:
        return _closed_action(value)
    except (TypeError, ValueError, ValidationError, AdhelmError) as exc:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN",
            "OpenAI pause retention requires a complete installed closed action.",
            exit_code=2,
        ) from exc


class OpenAIPauseExecutionPlanRecord(StrictModel):
    """One complete fixture-only OpenAI pause action retained in local history."""

    schema_version: Literal[1]
    event_type: Literal["EXECUTION_PLAN"]
    provider: Literal["openai_ads"]
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    action: OpenAIPausePlannedAction
    action_digest: Digest
    plan_digest: Digest
    expires_at: datetime
    maximum_incremental_spend: Money
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False]
    requires_current_kernel_checks: Literal[True]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("action", mode="before")
    @classmethod
    def reparse_nested_action(cls, value: object) -> OpenAIPausePlannedAction:
        return _closed_action(value)

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution_authority must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_current_kernel_checks(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("requires_current_kernel_checks must be true")
        return value

    @model_validator(mode="after")
    def exact_action_binding(self) -> OpenAIPauseExecutionPlanRecord:
        action = _closed_action(self.action)
        subplan = action.plan.subplans[0]
        if (
            self.capability_status != OPENAI_PAUSE_STATUS
            or action.status != self.capability_status
            or subplan.provider != self.provider
            or self.action_digest != digest(action)
            or self.plan_digest != action.plan.plan_digest
            or self.expires_at != action.plan.expires_at
            or self.maximum_incremental_spend != subplan.max_spend
        ):
            raise ValueError("retained action and plan bindings must match exactly")
        return self


def openai_pause_execution_plan_record(
    action: OpenAIPausePlannedAction,
) -> OpenAIPauseExecutionPlanRecord:
    """Create the one permitted persisted plan record from a closed action."""
    checked = revalidate_openai_pause_action(action)
    return OpenAIPauseExecutionPlanRecord(
        schema_version=1,
        event_type="EXECUTION_PLAN",
        provider="openai_ads",
        capability_status=OPENAI_PAUSE_STATUS,
        action=checked,
        action_digest=digest(checked),
        plan_digest=checked.plan.plan_digest,
        expires_at=checked.plan.expires_at,
        maximum_incremental_spend=checked.plan.subplans[0].max_spend,
        historical_evidence_status="HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        execution_authority=False,
        requires_current_kernel_checks=True,
    )

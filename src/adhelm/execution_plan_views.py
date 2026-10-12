"""Closed historical sharing views for retained provider-neutral plans.

The review is built from an explicit allowlisted preimage rather than from the
whole retained record, so a future record field cannot reach an agent, an
artifact, or a support bundle without a deliberate change here.  The private
preimage helper exists only for ``projections._redact_payload``'s recursive
HMAC pass and is never a serializable or caller-facing contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

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
from adhelm.execution_plan_records import (
    ExecutionPlanCurrencyBound,
    ExecutionPlanRecord,
    revalidate_execution_plan_record,
)
from adhelm.models import MAX_CHILD_EGRESS_COUNT, Provider, StrictModel
from adhelm.strict_json import validate_json_value

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueRef = Annotated[str, Field(pattern=r"^opaque:[0-9a-f]{64}$")]
_MAX_REVIEW_OUTPUT_BYTES = 65_536
_PROBE_KEY = b"\0" * 32
_MAXIMUM_EGRESS_HOSTS = 16
_MAXIMUM_RECOVERY_ACTIONS = 12


def _invalid_review() -> AdhelmError:
    return AdhelmError(
        "INVALID_EXECUTION_PLAN_REVIEW",
        "The retained execution plan cannot be reviewed safely.",
        exit_code=ExitCode.INVALID_INPUT,
    )


class ExecutionPlanSourceReview(StrictModel):
    """The bounded read interval and connector the plan was compiled from."""

    identity_fingerprint: Digest
    observed_state_fingerprint: Annotated[str, Field(min_length=16, max_length=256)]
    observed_state_revision: Annotated[str, Field(max_length=256)] | None
    read_started_at: datetime
    read_finished_at: datetime
    read_consistency: Literal["SEQUENTIAL_NONATOMIC"]
    connector_version: Annotated[str, Field(min_length=1, max_length=80)]

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation times must be timezone-aware")
        return value.astimezone(UTC)


class _ExecutionPlanReviewSurface(StrictModel):
    """Closed agent-visible historical facts from one retained plan."""

    schema_version: Literal[1] = 1
    event_type: Literal["EXECUTION_PLAN_RETAINED"] = "EXECUTION_PLAN_RETAINED"
    provider: Provider
    operation: str = Field(min_length=1, max_length=32)
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    plan_digest: Digest
    subplan_digests: tuple[Digest, ...] = Field(min_length=1, max_length=1)
    expected_child_egress_count: Annotated[int, Field(ge=1, le=MAX_CHILD_EGRESS_COUNT)]
    workspace_profile_ref: OpaqueRef
    account_ref: OpaqueRef
    auth_profile_ref: OpaqueRef
    provider_api_version: Annotated[str, Field(min_length=1, max_length=80)]
    capability_version: Annotated[str, Field(min_length=1, max_length=80)]
    predicted_state_fingerprint: Annotated[str, Field(min_length=16, max_length=256)]
    egress_hosts: tuple[Annotated[str, Field(min_length=1, max_length=253)], ...] = Field(
        min_length=1, max_length=_MAXIMUM_EGRESS_HOSTS
    )
    recovery_actions: tuple[Annotated[str, Field(min_length=1, max_length=32)], ...] = Field(
        max_length=_MAXIMUM_RECOVERY_ACTIONS
    )
    review_lines: tuple[Annotated[str, Field(min_length=1, max_length=120)], ...] = Field(
        min_length=1, max_length=24
    )
    review_source: Literal["COMPOSITE_SUBPLAN_INTENT", "COMPILER_CANDIDATE_REVIEW"]
    spend_bounds_by_currency: tuple[ExecutionPlanCurrencyBound, ...] = Field(max_length=1)
    source: ExecutionPlanSourceReview
    supersedes_record_digest: Digest | None
    expires_at: datetime
    reviewed_at: datetime
    expiry_status: Literal["NOT_EXPIRED_AT_REVIEW", "EXPIRED_AT_REVIEW"]
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    provider_conditional_write: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider_idempotency: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    concurrency_limitation: Literal["LOCAL_BASELINE_NOT_PROVIDER_COMPARE_AND_SWAP"] = (
        "LOCAL_BASELINE_NOT_PROVIDER_COMPARE_AND_SWAP"
    )
    recovery_semantics: Literal["RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"] = (
        "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"
    )
    remote_side_effects: Literal[False] = False
    execution_authority: Literal[False] = False
    review_authorizes_execution: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

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
    def review_facts_are_historical_and_bound(self) -> _ExecutionPlanReviewSurface:
        expected_expiry = (
            "EXPIRED_AT_REVIEW" if self.expires_at <= self.reviewed_at else "NOT_EXPIRED_AT_REVIEW"
        )
        if self.expiry_status != expected_expiry:
            raise ValueError("review expiry status is inconsistent")
        if self.reviewed_at < self.source.read_finished_at:
            raise ValueError("review cannot predate the completed observation")
        if self.operation == "RECOVER":
            raise ValueError("RECOVER is not a retained plan operation")
        return self


class ExecutionPlanReview(_ExecutionPlanReviewSurface):
    """Bounded redacted review of one historical provider-neutral plan."""

    @property
    def review_digest(self) -> str:
        return digest(self)

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_review(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        try:
            if type(self) is not ExecutionPlanReview:
                raise ValueError("review type")
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("review output")
            validate_json_value(
                _ExecutionPlanReviewSurface,
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
            raise ValueError("execution plan review is invalid") from None


def _review_json_shape(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if type(value) is tuple or type(value) is list:
        return [_review_json_shape(item) for item in value]
    if type(value) is dict:
        return {key: _review_json_shape(item) for key, item in value.items()}
    return value


def _execution_plan_export_hmac_input(value: object) -> dict[str, Any]:
    """Return the private allowlisted HMAC preimage for sharing projections only."""

    record = revalidate_execution_plan_record(value)
    subplan = record.plan.subplans[0]
    continuity = record.source_continuity
    return {
        "schema_version": 1,
        "event_type": "EXECUTION_PLAN_RETAINED",
        "provider": record.provider,
        "operation": record.operation.value,
        "capability_status": record.provenance.capability_status,
        "plan_digest": record.plan_digest,
        "subplan_digests": list(record.subplan_digests),
        "expected_child_egress_count": record.expected_child_egress_count,
        "workspace_profile_ref": record.provenance.workspace_profile_ref,
        "account_ref": record.provenance.account_ref,
        "auth_profile_ref": record.provenance.auth_profile_ref,
        "provider_api_version": record.provenance.provider_api_version,
        "capability_version": record.provenance.capability_version,
        "predicted_state_fingerprint": subplan.after.fingerprint,
        "egress_hosts": sorted({target.host for target in subplan.egress}),
        "recovery_actions": [item.value for item in subplan.recovery_actions],
        "review_lines": list(record.review_lines),
        "review_source": record.review_source,
        "spend_bounds_by_currency": [
            bound.model_dump(mode="json") for bound in record.spend_bounds_by_currency
        ],
        "source": {
            "identity_fingerprint": continuity.identity_fingerprint,
            "observed_state_fingerprint": continuity.observed_state.fingerprint,
            "observed_state_revision": continuity.observed_state.revision,
            "read_started_at": continuity.read_started_at.isoformat(),
            "read_finished_at": continuity.read_finished_at.isoformat(),
            "read_consistency": continuity.read_consistency,
            "connector_version": continuity.connector_version,
        },
        "supersedes_record_digest": record.supersedes_record_digest,
        "expires_at": record.expires_at.isoformat(),
        "historical_evidence_status": record.historical_evidence_status,
        "current_state_status": "RECHECK_REQUIRED_BEFORE_EGRESS",
        "provider_conditional_write": "NOT_DOCUMENTED",
        "provider_idempotency": "NOT_DOCUMENTED",
        "concurrency_limitation": "LOCAL_BASELINE_NOT_PROVIDER_COMPARE_AND_SWAP",
        "recovery_semantics": "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED",
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
    record: ExecutionPlanRecord, *, opaque_key: bytes, reviewed_at: datetime
) -> ExecutionPlanReview:
    try:
        from adhelm.projections import _redact_payload

        data = _redact_payload(_execution_plan_export_hmac_input(record), opaque_key, False)
        if type(data) is not dict:
            raise ValueError("redacted review must be an object")
        review_data = dict(cast(dict[str, Any], data))
        review_data["reviewed_at"] = reviewed_at.isoformat()
        review_data["expiry_status"] = (
            "EXPIRED_AT_REVIEW" if record.expires_at <= reviewed_at else "NOT_EXPIRED_AT_REVIEW"
        )
        return validate_json_value(ExecutionPlanReview, review_data)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_review() from None


def review_execution_plan(
    value: object,
    *,
    opaque_key: bytes,
    reviewed_at: datetime,
    max_output_bytes: int = _MAX_REVIEW_OUTPUT_BYTES,
) -> ExecutionPlanReview:
    """Return a bounded redacted historical review without a current grant."""

    key = _checked_opaque_key(opaque_key)
    now = _checked_reviewed_at(reviewed_at)
    maximum = _checked_output_limit(max_output_bytes)
    record = revalidate_execution_plan_record(value)
    probe = _review_from_record(record, opaque_key=_PROBE_KEY, reviewed_at=now)
    try:
        if len(canonical_bytes(probe)) > maximum:
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "The complete execution plan review exceeds the output limit.",
                exit_code=ExitCode.INVALID_INPUT,
                next_action="Increase max_output_bytes to inspect the complete historical review.",
            )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise _invalid_review() from None
    return _review_from_record(record, opaque_key=key, reviewed_at=now)


__all__ = ["ExecutionPlanReview", "review_execution_plan"]

"""One provider-neutral retained execution plan, without execution authority.

This is the generic replacement for the four provider-specific retained-plan
records (``EXECUTION_PLAN``, ``GA4_KEY_EVENT_CREATE_PLAN``,
``META_CAMPAIGN_PAUSE_PLAN``, ``GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN``).  Those four
each wrap a provider contract's ``PlannedAction``; this one wraps the
provider-neutral :class:`adhelm.safety.CompositePlan` the kernel already
executes, so a new provider CREATE, ACTIVATE, or MAINTAIN operation needs a
compiler and a contract but no fifth retained-plan family.

The record is local history only.  It issues no authority, proves no current
remote state, and carries no raw provider payload, credential, secret, or free
text beyond the closed :func:`adhelm.approval_protocol.validate_review_lines`
list a native host renders verbatim.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.approval_protocol import composite_subplan_review_lines, validate_review_lines
from adhelm.canonical import digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import MAX_CHILD_EGRESS_COUNT, Provider, StrictModel
from adhelm.safety import CompositePlan, OperationKind, StateRef, SubPlan
from adhelm.strict_json import validate_json_value

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

#: The one additive ledger record kind this module registers, in ``changes``.
EXECUTION_PLAN_RECORD_KIND: Final = "EXECUTION_PLAN_RETAINED"

#: How the retained ``review_lines`` were derived.  Both values name an S1
#: derivation in :mod:`adhelm.approval_protocol`; nothing else may produce them.
ExecutionPlanReviewSource = Literal["COMPOSITE_SUBPLAN_INTENT", "COMPILER_CANDIDATE_REVIEW"]

#: Restates the closed set :mod:`adhelm.approval_protocol` uses to decide
#: ``NOT_SPEND_CAPABLE``.  A subplan using one of these with an explicit zero
#: cap is summarized as incapable; every other operation always shows its
#: integer cap, including a zero one.
NON_SPEND_OPERATIONS: Final = frozenset(
    {OperationKind.PAUSE, OperationKind.ARCHIVE, OperationKind.RECOVER}
)

#: Domain separation for the fingerprint of the identity a plan was compiled
#: against.  It is a restatement of the subplan's own account binding, so it is
#: recomputable at revalidation and can never become an unverifiable claim.
_SOURCE_IDENTITY_DOMAIN: Final = "adhelm.execution-plan.source-identity.v1"

_MAXIMUM_REVIEW_LINES: Final = 24
#: One retained record describes one provider's plan, because the ledger
#: envelope binds exactly one provider and one account reference.  A
#: multi-provider composite is a separate decision with its own envelope,
#: ordering, and account-binding analysis; see the implementation note.
_MAXIMUM_SUBPLANS: Final = 1


def _invalid_record() -> AdhelmError:
    return AdhelmError(
        "INVALID_EXECUTION_PLAN_RECORD",
        "A complete closed provider-neutral execution plan record is required.",
        exit_code=ExitCode.INVALID_INPUT,
    )


class ExecutionPlanCurrencyBound(StrictModel):
    """The maximum declared cap of one currency across the plan's subplans."""

    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    maximum_minor_units: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]


class ExecutionPlanSourceContinuity(StrictModel):
    """The exact bounded read this plan was compiled against.

    ``observed_state`` must be the subplan's own ``before`` fingerprint, and
    ``identity_fingerprint`` must restate the subplan's account binding, so the
    record cannot claim continuity with a read it does not describe.  The
    interval and connector version are the facts the plan itself does not hold.
    """

    identity_fingerprint: Digest
    observed_state: StateRef
    read_started_at: datetime
    read_finished_at: datetime
    read_consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    connector_version: Annotated[str, Field(min_length=1, max_length=80)]

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_interval(self) -> ExecutionPlanSourceContinuity:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("the source observation interval is reversed")
        return self


class ExecutionPlanProvenance(StrictModel):
    """Local compilation provenance, restated from the plan it describes."""

    workspace_profile_ref: Annotated[str, Field(min_length=1, max_length=160)]
    account_ref: Annotated[str, Field(min_length=1, max_length=160)]
    auth_profile_ref: Annotated[str, Field(min_length=1, max_length=160)]
    provider_api_version: Annotated[str, Field(min_length=1, max_length=80)]
    capability_version: Annotated[str, Field(min_length=1, max_length=80)]
    compiled_by: Literal["LOCAL_CLI"] = "LOCAL_CLI"
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    live_validated: Literal[False] = False

    @field_validator("live_validated", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("retained provenance cannot claim live validation")
        return value


class ExecutionPlanRecord(StrictModel):
    """One exact local execution plan retained as history, for any provider."""

    schema_version: Literal[1] = 1
    event_type: Literal["EXECUTION_PLAN_RETAINED"] = "EXECUTION_PLAN_RETAINED"
    provider: Provider
    operation: OperationKind
    plan: CompositePlan
    plan_digest: Digest
    subplan_digests: tuple[Digest, ...] = Field(min_length=1, max_length=_MAXIMUM_SUBPLANS)
    expected_child_egress_count: Annotated[int, Field(ge=1, le=MAX_CHILD_EGRESS_COUNT)]
    review_lines: tuple[str, ...] = Field(min_length=1, max_length=_MAXIMUM_REVIEW_LINES)
    review_source: ExecutionPlanReviewSource
    spend_bounds_by_currency: tuple[ExecutionPlanCurrencyBound, ...] = Field(
        max_length=_MAXIMUM_SUBPLANS
    )
    expires_at: datetime
    source_continuity: ExecutionPlanSourceContinuity
    supersedes_record_digest: Digest | None = None
    provenance: ExecutionPlanProvenance
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

    @field_validator("expires_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_plan_binding(self) -> ExecutionPlanRecord:
        plan = self.plan
        if len(plan.subplans) != _MAXIMUM_SUBPLANS:
            raise ValueError("one retained record describes exactly one provider subplan")
        subplan = plan.subplans[0]
        if (
            self.plan_digest != plan.plan_digest
            or self.subplan_digests != plan.subplan_digests
            or self.expires_at != plan.expires_at
            or self.provider != subplan.provider
            or self.operation is not subplan.operation
            or self.expected_child_egress_count != subplan.expected_child_egress_count
            or self.spend_bounds_by_currency != execution_plan_spend_bounds(plan)
            or self.provenance.workspace_profile_ref != plan.profile_ref
            or self.provenance.account_ref != subplan.account.account_ref
            or self.provenance.auth_profile_ref != subplan.account.auth_profile_ref
            or self.provenance.provider_api_version != subplan.provider_api_version
            or self.provenance.capability_version != subplan.capability_version
        ):
            raise ValueError("retained execution plan facts do not match the exact plan")
        if self.operation is OperationKind.RECOVER:
            # ``SubPlan`` already refuses it, so this can only be reached by a
            # record whose restated operation disagrees with its own plan.
            raise ValueError("RECOVER records an observation and is not a plan operation")
        if (
            self.source_continuity.observed_state != subplan.before
            or self.source_continuity.identity_fingerprint
            != execution_plan_identity_fingerprint(subplan)
        ):
            raise ValueError("retained source continuity does not describe this plan")
        if self.source_continuity.read_finished_at > self.expires_at:
            raise ValueError("a plan cannot expire before the read it was compiled from")
        if self.supersedes_record_digest is not None and (
            self.supersedes_record_digest == self.plan_digest
        ):
            raise ValueError("a plan cannot supersede its own digest")
        self._exact_review_lines(subplan)
        return self

    def _exact_review_lines(self, subplan: SubPlan) -> None:
        """Require the exact lines the approval host will render, in order."""

        derived = derived_execution_review_lines(self.spend_bounds_by_currency)
        try:
            checked = validate_review_lines(
                list(self.review_lines), maximum_lines=_MAXIMUM_REVIEW_LINES, derived=derived
            )
        except (AdhelmError, ValueError):
            raise ValueError("retained review lines are invalid") from None
        if checked != self.review_lines:
            raise ValueError("retained review lines are invalid")
        if self.review_source == "COMPOSITE_SUBPLAN_INTENT" and (
            self.review_lines != derived + composite_subplan_review_lines(subplan)
        ):
            raise ValueError("retained review lines do not restate the subplan intent")


def execution_plan_identity_fingerprint(subplan: SubPlan) -> str:
    """Return the fingerprint of the identity one subplan was compiled against."""

    return digest(
        {
            "domain": _SOURCE_IDENTITY_DOMAIN,
            "provider": subplan.provider,
            "account": subplan.account.model_dump(mode="json"),
        }
    )


def execution_plan_spend_bounds(plan: CompositePlan) -> tuple[ExecutionPlanCurrencyBound, ...]:
    """Return the per-currency maxima, using the S1 ``NOT_SPEND_CAPABLE`` rule.

    An empty result is exactly the approval subject's ``NOT_SPEND_CAPABLE``
    summary.  It is a maximum per currency, never a cross-subplan total: a
    composite has no cumulative claim to make.
    """

    maxima: dict[str, int] = {}
    for subplan in plan.subplans:
        if subplan.operation in NON_SPEND_OPERATIONS and subplan.max_spend.minor_units == 0:
            continue
        currency = subplan.max_spend.currency
        maxima[currency] = max(maxima.get(currency, 0), subplan.max_spend.minor_units)
    return tuple(
        ExecutionPlanCurrencyBound(currency=currency, maximum_minor_units=maxima[currency])
        for currency in sorted(maxima)
    )


def derived_execution_review_lines(
    bounds: Sequence[ExecutionPlanCurrencyBound],
) -> tuple[str, ...]:
    """Return the spend lines the structured summary alone decides.

    These lead the retained list exactly as they lead a
    ``COMPOSITE_EXECUTION_PLAN`` subject, so the sentence a person reads is the
    summary the receipt binds.  ``tests/test_execution_plan_records.py`` pins
    this against :func:`adhelm.approval_protocol.expected_composite_approval_subject`
    byte for byte.
    """

    if not bounds:
        return ("Maximum incremental spend NOT_SPEND_CAPABLE",)
    return tuple(
        f"Maximum incremental spend {bound.maximum_minor_units} minor units {bound.currency}"
        for bound in bounds
    )


def execution_plan_record(
    plan: CompositePlan,
    *,
    review_lines: Sequence[str],
    review_source: ExecutionPlanReviewSource,
    source_continuity: ExecutionPlanSourceContinuity,
    capability_status: str = "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
    supersedes_record_digest: str | None = None,
) -> ExecutionPlanRecord:
    """Reconstruct one complete non-authorizing record from an exact plan.

    ``review_lines`` may be either the caller's own lines or a complete
    previously derived list.  Prepending the derived spend summary is
    idempotent, exactly as it is in the approval builder, so a record can be
    rebuilt from a retained one through this single derivation.
    """

    if type(plan) is not CompositePlan or len(plan.subplans) != _MAXIMUM_SUBPLANS:
        raise _invalid_record()
    subplan = plan.subplans[0]
    bounds = execution_plan_spend_bounds(plan)
    derived = derived_execution_review_lines(bounds)
    supplied = tuple(review_lines)
    lines = supplied if supplied[: len(derived)] == derived else derived + supplied
    try:
        return ExecutionPlanRecord(
            provider=subplan.provider,  # type: ignore[arg-type]
            operation=subplan.operation,
            plan=plan,
            plan_digest=plan.plan_digest,
            subplan_digests=plan.subplan_digests,
            expected_child_egress_count=subplan.expected_child_egress_count,
            review_lines=lines,
            review_source=review_source,
            spend_bounds_by_currency=bounds,
            expires_at=plan.expires_at,
            source_continuity=source_continuity,
            supersedes_record_digest=supersedes_record_digest,
            provenance=ExecutionPlanProvenance(
                workspace_profile_ref=plan.profile_ref,
                account_ref=subplan.account.account_ref,
                auth_profile_ref=subplan.account.auth_profile_ref,
                provider_api_version=subplan.provider_api_version,
                capability_version=subplan.capability_version,
                capability_status=capability_status,  # type: ignore[arg-type]
            ),
        )
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_record() from None


def revalidate_execution_plan_record(value: object) -> ExecutionPlanRecord:
    """Reject incomplete, aliased, or incoherent retained execution-plan facts."""

    try:
        if type(value) is ExecutionPlanRecord:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(ExecutionPlanRecord.model_fields):
            raise ValueError("a complete record is required")
        return validate_json_value(ExecutionPlanRecord, value)
    except Exception:
        raise _invalid_record() from None


__all__ = [
    "EXECUTION_PLAN_RECORD_KIND",
    "NON_SPEND_OPERATIONS",
    "ExecutionPlanCurrencyBound",
    "ExecutionPlanProvenance",
    "ExecutionPlanRecord",
    "ExecutionPlanReviewSource",
    "ExecutionPlanSourceContinuity",
    "derived_execution_review_lines",
    "execution_plan_identity_fingerprint",
    "execution_plan_record",
    "execution_plan_spend_bounds",
    "revalidate_execution_plan_record",
]

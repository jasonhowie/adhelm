"""One provider-agnostic execution-planning session.

The session is the seam between a provider compiler and durable local history.
It performs **no egress of its own**: the caller's configured read session
performs one fresh bounded read and injects it as a
:class:`BoundedExecutionRead`.  The session then builds the ``SubPlan`` and its
one-provider ``CompositePlan`` with the observed ``before`` fingerprint, the
compiler's predicted ``after`` fingerprint, the plan expiry, and the declared
child-request count; derives the review lines through the S1 derivations; and
retains the record with the "durable append, then reload from a verified
checkpoint and compare" discipline the Google F01 planner uses.

Nothing here issues, checks, or implies authorization.  A retained plan is
local history; every kernel check still runs at execution time against a fresh
read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from adhelm.approval_protocol import compiler_review_lines, composite_subplan_review_lines
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_records import (
    ExecutionPlanRecord,
    ExecutionPlanReviewSource,
    ExecutionPlanSourceContinuity,
    execution_plan_identity_fingerprint,
    execution_plan_record,
)
from adhelm.ledger import Ledger
from adhelm.models import MAX_CHILD_EGRESS_COUNT, PROVIDERS
from adhelm.safety import (
    AccountIdentity,
    CompositePlan,
    EgressTarget,
    FieldChange,
    Money,
    OpenAIAccountKeyIdentity,
    OperationKind,
    StateRef,
    SubPlan,
)
from adhelm.saved_execution_plans import load_execution_plan_record, retain_execution_plan_record

DEFAULT_CAPABILITY_STATUS: Final = "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"


@dataclass(frozen=True, slots=True)
class BoundedExecutionRead:
    """One fresh bounded read a configured provider read session performed.

    The session never produces this value.  A caller that cannot supply a
    completed read cannot retain a plan, which is the point: a plan's
    ``before`` fingerprint must be something this workspace actually observed.
    """

    observed_state: StateRef
    read_started_at: datetime
    read_finished_at: datetime
    connector_version: str
    read_consistency: str = "SEQUENTIAL_NONATOMIC"


@dataclass(frozen=True, slots=True)
class CompiledExecutionCandidate:
    """One provider compiler's output, minus the facts only a read supplies.

    ``redacted_review`` is the compiler's existing closed review model when the
    provider has one.  A pause or archive intent has no compiler candidate to
    summarize and leaves it ``None``, which selects the subplan-intent
    derivation instead.
    """

    provider: str
    operation: OperationKind
    subplan_id: str
    account: AccountIdentity | OpenAIAccountKeyIdentity
    provider_api_version: str
    capability_version: str
    after: StateRef
    max_spend: Money
    egress: tuple[EgressTarget, ...]
    recovery_actions: tuple[OperationKind, ...] = ()
    recovery_disposition: str | None = None
    field_changes: tuple[FieldChange, ...] = ()
    expected_child_egress_count: int = 1
    redacted_review: Any = None
    capability_status: str = DEFAULT_CAPABILITY_STATUS


@dataclass(frozen=True, slots=True)
class RetainedExecutionPlan:
    """The durable receipt facts a caller may render, and nothing more."""

    record: ExecutionPlanRecord
    record_digest: str
    plan_digest: str
    subplan_digests: tuple[str, ...]
    checkpoint_digest: str
    recorded_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConfiguredExecutionPlanner(Protocol):
    """The command seam a provider CREATE/ACTIVATE/MAINTAIN ticket implements.

    A configured planner owns exactly what the generic command cannot: that
    provider's validated request model, its credential and configuration
    locking, and its own bounded read session.  It performs the read, builds a
    :class:`CompiledExecutionCandidate`, and calls :func:`plan_execution` with
    the injected :class:`BoundedExecutionRead`.  It returns the durable
    receipt; it never returns an unretained plan.
    """

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan: ...


#: The private registry.  Only :func:`register_execution_planner` writes to it.
_CONFIGURED_EXECUTION_PLANNERS: Final[
    dict[tuple[str, OperationKind], ConfiguredExecutionPlanner]
] = {}

#: Registered configured planners, keyed by ``(provider, operation)``.
#:
#: It is **empty**, and this read-only view is the structural statement of that:
#: no provider compiles a plan through this generic command yet, so
#: ``plan execution create`` fails closed with
#: ``EXECUTION_PLAN_NOT_COMPILABLE`` for every pair rather than reporting a stub
#: success.  A mapping proxy means the emptiness cannot be revoked by an
#: incidental ``dict`` mutation from an unrelated import; a provider ticket must
#: call :func:`register_execution_planner` deliberately, with its own contract,
#: compiler, read session, and evidence.
CONFIGURED_EXECUTION_PLANNERS: Final[
    Mapping[tuple[str, OperationKind], ConfiguredExecutionPlanner]
] = MappingProxyType(_CONFIGURED_EXECUTION_PLANNERS)


def register_execution_planner(
    provider: str, operation: OperationKind, planner: ConfiguredExecutionPlanner
) -> None:
    """Register the one configured planner for one provider operation.

    Registration is deliberate and single: a duplicate pair is refused rather
    than silently replaced, because two planners for one operation would make
    which compiler produced a retained plan depend on import order.
    """

    if (
        type(provider) is not str
        or provider not in PROVIDERS
        or type(operation) is not OperationKind
        or operation is OperationKind.RECOVER
        or not isinstance(planner, ConfiguredExecutionPlanner)
    ):
        raise AdhelmError(
            "INVALID_EXECUTION_PLANNER_REGISTRATION",
            "An execution planner must name one installed provider and one plan operation.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if (provider, operation) in _CONFIGURED_EXECUTION_PLANNERS:
        raise AdhelmError(
            "EXECUTION_PLANNER_ALREADY_REGISTERED",
            "One provider operation has exactly one configured execution planner.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    _CONFIGURED_EXECUTION_PLANNERS[(provider, operation)] = planner


def configured_execution_planner(
    provider: str, operation: OperationKind
) -> ConfiguredExecutionPlanner | None:
    """Return the registered planner for one pair, or ``None`` when there is none."""

    return CONFIGURED_EXECUTION_PLANNERS.get((provider, operation))


def compilable_execution_operations() -> tuple[dict[str, str], ...]:
    """Return the exact registered pairs, sorted, for truthful discovery.

    ``discover`` renders this at call time rather than a hard-coded list, so the
    catalogue can never advertise an operation no planner compiles, or omit one
    a provider ticket registered.
    """

    return tuple(
        {"provider": provider, "operation": operation.value}
        for provider, operation in sorted(
            CONFIGURED_EXECUTION_PLANNERS, key=lambda pair: (pair[0], pair[1].value)
        )
    )


def _invalid_candidate() -> AdhelmError:
    return AdhelmError(
        "INVALID_EXECUTION_PLAN_CANDIDATE",
        "A complete compiled execution plan candidate and bounded read are required.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _checked_read(read: object) -> BoundedExecutionRead:
    if (
        type(read) is not BoundedExecutionRead
        or type(read.observed_state) is not StateRef
        or type(read.read_started_at) is not datetime
        or type(read.read_finished_at) is not datetime
        or read.read_started_at.tzinfo is None
        or read.read_finished_at.tzinfo is None
        or read.read_finished_at < read.read_started_at
        or read.read_consistency != "SEQUENTIAL_NONATOMIC"
        or type(read.connector_version) is not str
        or not 1 <= len(read.connector_version) <= 80
    ):
        raise _invalid_candidate()
    return read


def _checked_candidate(candidate: object) -> CompiledExecutionCandidate:
    if type(candidate) is not CompiledExecutionCandidate or (
        candidate.operation is OperationKind.RECOVER
    ):
        raise _invalid_candidate()
    if (
        type(candidate.expected_child_egress_count) is not int
        or not 1 <= candidate.expected_child_egress_count <= MAX_CHILD_EGRESS_COUNT
    ):
        raise _invalid_candidate()
    return candidate


def compile_execution_plan(
    candidate: CompiledExecutionCandidate,
    *,
    plan_id: str,
    profile_ref: str,
    expires_at: datetime,
    read: BoundedExecutionRead,
) -> CompositePlan:
    """Build one one-provider composite from a candidate and an injected read.

    ``before`` is the observed fingerprint the caller read; the session never
    invents it.  A candidate whose predicted ``after`` equals the observed
    ``before`` is refused, because a plan with no transition has nothing for
    the kernel's drift check to compare.
    """

    checked = _checked_candidate(candidate)
    checked_read = _checked_read(read)
    if checked.after == checked_read.observed_state:
        raise AdhelmError(
            "EXECUTION_PLAN_NO_TRANSITION",
            "A compiled plan must change the observed state it was compiled from.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    # Both optional fields are omitted rather than set: ``SubPlan``
    # distinguishes an unset ``recovery_disposition`` from an explicit one, and
    # drops a default child count from its wire form.  Passing either
    # explicitly would fail its validator or change the subplan digest.
    values: dict[str, Any] = {
        "subplan_id": checked.subplan_id,
        "provider": checked.provider,
        "operation": checked.operation,
        "account": checked.account,
        "provider_api_version": checked.provider_api_version,
        "capability_version": checked.capability_version,
        "before": checked_read.observed_state,
        "after": checked.after,
        "field_changes": checked.field_changes,
        "max_spend": checked.max_spend,
        "egress": checked.egress,
        "recovery_actions": checked.recovery_actions,
        "expires_at": expires_at,
    }
    if checked.recovery_disposition is not None:
        values["recovery_disposition"] = checked.recovery_disposition
    if checked.expected_child_egress_count != 1:
        values["expected_child_egress_count"] = checked.expected_child_egress_count
    try:
        subplan = SubPlan(**values)
        return CompositePlan(
            plan_id=plan_id,
            profile_ref=profile_ref,
            subplans=(subplan,),
            expires_at=expires_at,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_candidate() from None


def execution_plan_review(
    candidate: CompiledExecutionCandidate, subplan: SubPlan
) -> tuple[tuple[str, ...], ExecutionPlanReviewSource]:
    """Derive the exact lines the approval host will render, and their source.

    Both branches are the S1 derivations, unchanged: a provider with a closed
    compiler review renders that model, and a pause or archive intent renders
    the subplan's own bounded intent.
    """

    checked = _checked_candidate(candidate)
    try:
        if checked.redacted_review is None:
            return composite_subplan_review_lines(subplan), "COMPOSITE_SUBPLAN_INTENT"
        return (
            compiler_review_lines(checked.provider, checked.redacted_review),
            "COMPILER_CANDIDATE_REVIEW",
        )
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "EXECUTION_PLAN_REVIEW_NOT_DERIVABLE",
            "The compiled candidate does not produce a bounded renderable review.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Refuse the candidate; a review that does not fit the closed line budget "
                "is never truncated."
            ),
        ) from None


def build_execution_plan_record(
    plan: CompositePlan,
    *,
    candidate: CompiledExecutionCandidate,
    read: BoundedExecutionRead,
    supersedes_record_digest: str | None = None,
) -> ExecutionPlanRecord:
    """Build the retained record, refusing a plan the read does not describe."""

    checked = _checked_candidate(candidate)
    checked_read = _checked_read(read)
    if type(plan) is not CompositePlan or len(plan.subplans) != 1:
        raise _invalid_candidate()
    subplan = plan.subplans[0]
    if subplan.before != checked_read.observed_state or subplan.provider != checked.provider:
        raise _source_continuity_mismatch()
    review_lines, review_source = execution_plan_review(checked, subplan)
    continuity = ExecutionPlanSourceContinuity(
        identity_fingerprint=execution_plan_identity_fingerprint(subplan),
        observed_state=checked_read.observed_state,
        read_started_at=checked_read.read_started_at,
        read_finished_at=checked_read.read_finished_at,
        connector_version=checked_read.connector_version,
    )
    return execution_plan_record(
        plan,
        review_lines=review_lines,
        review_source=review_source,
        source_continuity=continuity,
        capability_status=checked.capability_status,
        supersedes_record_digest=supersedes_record_digest,
    )


def _source_continuity_mismatch() -> AdhelmError:
    return AdhelmError(
        "EXECUTION_PLAN_SOURCE_CONTINUITY_MISMATCH",
        "The compiled plan was not built from the supplied bounded read.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Take a fresh bounded read and compile the plan again from it.",
    )


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "EXECUTION_PLAN_RECEIPT_UNVERIFIED",
        "The execution plan may be retained, but its receipt could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect history verify and plan execution catalog before planning again; "
            "do not repeat an ambiguous plan preparation."
        ),
    )


def _require_read_within_the_retention_window(record: ExecutionPlanRecord, now: datetime) -> None:
    """Bound the retained observation interval against the retention instant.

    Two refusals, both before any durable append:

    * A read that finishes **after** ``now`` did not happen yet, so a record
      claiming it would describe an observation that does not exist.  The GA4
      CREATE family this session generalizes already refuses exactly this
      (``ga4_create_workflow.py`` ``CLOCK_INVALID``), and the generic seam is
      not allowed to be weaker than the family it replaces.
    * A read older than the plan's own remaining lifetime -- ``now -
      read_finished_at > expires_at - now`` -- is refused as well.  The plan
      declares how long its picture of the provider is worth authorizing; an
      observation already staler than that whole window was never worth
      retaining under it.

    A stale-but-in-window read is deliberately **allowed**.  Local retention
    cannot prove the provider still matches, and it must not pretend to: such a
    plan is retained honestly and then refused by the kernel's drift check
    against a fresh read at execution time, which is where that comparison
    belongs.
    """

    finished = record.source_continuity.read_finished_at
    if finished > now:
        raise AdhelmError(
            "CLOCK_INVALID",
            "The retained plan claims a bounded read that finishes after its retention.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Check the local clock and retain the plan with the instant its read "
                "actually completed."
            ),
        )
    if now - finished > record.expires_at - now:
        raise AdhelmError(
            "EXECUTION_PLAN_READ_TOO_STALE",
            "The bounded read is older than the compiled plan's own remaining lifetime.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Take a fresh bounded read and compile the plan again from it.",
        )


def retain_execution_plan(
    ledger: Ledger,
    record: ExecutionPlanRecord,
    *,
    now: datetime,
    before_durable_append: Callable[[], None] | None = None,
) -> RetainedExecutionPlan:
    """Append the record durably, then reload and compare it byte for byte.

    An expired plan, a read that has not happened, and a read older than the
    plan's own remaining lifetime are all refused before the append.  After the
    append, any reload, revalidation, or comparison failure is reported as
    ambiguous retention: a completed append cannot be distinguished from a
    failed one from here, so a caller must inspect local history rather than
    retry.
    """

    if type(record) is not ExecutionPlanRecord:
        raise _invalid_candidate()
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise AdhelmError(
            "CLOCK_INVALID",
            "Plan retention requires an aware instant.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    instant = now.astimezone(UTC)
    if record.expires_at <= instant:
        raise AdhelmError(
            "EXECUTION_PLAN_EXPIRED",
            "The compiled execution plan expired before it could be retained.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile a new plan from a fresh bounded read.",
        )
    _require_read_within_the_retention_window(record, instant)
    appended = retain_execution_plan_record(
        ledger, record, before_durable_append=before_durable_append
    )
    try:
        record_digest = appended["record_digest"]
        reloaded, provenance = load_execution_plan_record(ledger, record_digest)
        if (
            canonical_bytes(reloaded.model_dump(mode="json", warnings="error"))
            != canonical_bytes(record.model_dump(mode="json", warnings="error"))
            or provenance["record_digest"] != record_digest
            or provenance["plan_digest"] != record.plan_digest
        ):
            raise ValueError("retained plan does not match the appended record")
        return RetainedExecutionPlan(
            record=reloaded,
            record_digest=record_digest,
            plan_digest=reloaded.plan_digest,
            subplan_digests=reloaded.subplan_digests,
            checkpoint_digest=provenance["checkpoint_digest"],
            recorded_at=provenance["recorded_at"],
            provenance=provenance,
        )
    except (AdhelmError, KeyError, OSError, TypeError, ValueError):
        raise _retention_unverified() from None


def plan_execution(
    ledger: Ledger,
    candidate: CompiledExecutionCandidate,
    *,
    read: BoundedExecutionRead,
    plan_id: str,
    profile_ref: str,
    expires_at: datetime,
    now: datetime,
    supersedes_record_digest: str | None = None,
    before_durable_append: Callable[[], None] | None = None,
) -> RetainedExecutionPlan:
    """Compile, review, and durably retain one plan from one injected read.

    This is the complete seam a provider ticket calls after its own configured
    read session returns.  It reaches no provider, credential store, or network.
    """

    plan = compile_execution_plan(
        candidate,
        plan_id=plan_id,
        profile_ref=profile_ref,
        expires_at=expires_at,
        read=read,
    )
    record = build_execution_plan_record(
        plan,
        candidate=candidate,
        read=read,
        supersedes_record_digest=supersedes_record_digest,
    )
    return retain_execution_plan(
        ledger, record, now=now, before_durable_append=before_durable_append
    )


__all__ = [
    "CONFIGURED_EXECUTION_PLANNERS",
    "DEFAULT_CAPABILITY_STATUS",
    "BoundedExecutionRead",
    "CompiledExecutionCandidate",
    "ConfiguredExecutionPlanner",
    "RetainedExecutionPlan",
    "build_execution_plan_record",
    "compilable_execution_operations",
    "compile_execution_plan",
    "configured_execution_planner",
    "execution_plan_review",
    "plan_execution",
    "register_execution_planner",
    "retain_execution_plan",
]

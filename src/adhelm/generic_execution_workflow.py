"""One provider-agnostic execution route for retained provider-neutral plans.

This is the generic replacement for the three provider-specific installed
execution workflows (``openai_pause_execution_workflow``,
``ga4_create_execution_workflow``, ``meta_pause_execution_workflow``).  Those
three each load a provider contract's ``PlannedAction``, build that provider's
approval challenge, and construct that provider's adapter inline.  This one
loads the provider-neutral :class:`adhelm.execution_plan_records.ExecutionPlanRecord`
S3 retains, obtains a ``COMPOSITE_EXECUTION_PLAN`` receipt through the
installed bridge, and resolves the adapter for ``(provider, operation)`` from a
registry, so a new provider CREATE, ACTIVATE, or MAINTAIN operation needs an
adapter and a compiler but no fourth execution workflow and no new CLI leaf.

Nothing here is a provider capability.  ``CONFIGURED_EXECUTION_ADAPTERS`` is
**empty**, the installed bridge refuses both provider-neutral approval kinds,
and the ordinary checked-in binding is not installed at all.  A caller supplies
no receipt, challenge, trust pin, key, grant, credential, transport, or
provider request: the retained record digest is the entire input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, NoReturn, Protocol, runtime_checkable

from pydantic import Field, field_validator

from adhelm import release_policy
from adhelm._installed_native_approval_bridge import (
    _INSTALLED_BRIDGE_KINDS,
    _require_compiled_installed_approval_bridge_ready,
    _require_installed_bridge_kind,
)
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import ApprovalChallengeKind
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_records import ExecutionPlanRecord
from adhelm.ledger import Ledger
from adhelm.models import PROVIDERS, StrictModel
from adhelm.release_policy import WRITES_NOT_RELEASED
from adhelm.safety import (
    ChildEgressAdapter,
    CompositePlan,
    DirectGrant,
    DirectGrantSource,
    ExecutionResult,
    GuardedAdapter,
    OperationKind,
    SafetyKernel,
    SubPlan,
    UserApprovalHost,
    attempt_recovery_subject,
)
from adhelm.saved_execution_plans import load_execution_plan_record
from adhelm.strict_json import validate_json_value

#: Fixed protocol labels, never an assertion of a human's identity.
INSTALLED_EXECUTION_LOGICAL_GRANTOR_REF: Final = (
    "logical:adhelm-installed-approval-bridge-composite-execution-plan-v1"
)
INSTALLED_RECOVERY_LOGICAL_GRANTOR_REF: Final = (
    "logical:adhelm-installed-approval-bridge-attempt-recovery-v1"
)

_APPLY_STARTED: Final = "APPLY_STARTED"
_APPROVAL_GRANTED: Final = "APPROVAL_GRANTED"
_RECEIPT_CLAIMED: Final = "APPROVAL_RECEIPT_CLAIMED"
_RECOVERY_OBSERVED: Final = "ATTEMPT_RECOVERY_OBSERVED"
_EXECUTION_PLAN_RETAINED: Final = "EXECUTION_PLAN_RETAINED"
_CHILD_APPLY_STARTED: Final = "CHILD_APPLY_STARTED"


class ExecutionPlanExecutionRequest(StrictModel):
    """The complete input of the generic execute route: one retained record."""

    schema_version: Literal[1] = 1
    plan_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class AttemptRecoveryRequest(StrictModel):
    """The complete input of the recovery route: one durable ``APPLY_STARTED``."""

    schema_version: Literal[1] = 1
    started_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


@dataclass(frozen=True, slots=True)
class AttemptRecoveryOutcome:
    """The observation a recovery recorded, and nothing the record does not say."""

    result: ExecutionResult
    record_digest: str
    started_record_digest: str
    plan_digest: str
    subplan_digest: str
    provider: str
    operation: str
    observed_relation: str
    disposition: str
    actor_ref: str
    attempt_effect_status: str


@dataclass(frozen=True, slots=True)
class ExecutionAdapterCapabilities:
    """What a configured factory declares before any credential is opened.

    This is a *declaration*, answered from the factory's own compiled contract:
    it must not read configuration, open a credential store, or contact a
    provider, because the route asks for it while deciding whether the plan is
    executable at all -- before the approval bridge builds or consumes a
    one-use receipt.  The opened adapter is checked against the declaration
    again before the kernel runs, so a factory that declares a capability it
    does not implement is refused rather than believed.
    """

    #: Whether the factory's adapter implements
    #: :class:`adhelm.safety.ChildEgressAdapter`, which a subplan whose
    #: ``expected_child_egress_count`` exceeds one requires.
    child_egress: bool = False


@runtime_checkable
class ConfiguredExecutionAdapterFactory(Protocol):
    """The seam a provider execution ticket (P03/P04/P05) implements.

    A configured factory owns exactly what the generic route cannot: that
    provider's credential and configuration locking, its fresh authentication
    handoff, its HTTP lifetime, and its :class:`adhelm.safety.GuardedAdapter` (or
    :class:`adhelm.safety.ChildEgressAdapter`) implementation.  It is opened only
    **after** the installed approval bridge has produced an accepted grant for
    the exact retained plan, so an unconfigured or refused approval never
    reaches a credential store or a provider.

    The returned context manager owns the adapter's whole lifetime; the kernel
    executes inside it and it is closed before this route returns.
    """

    def declared_capabilities(self) -> ExecutionAdapterCapabilities: ...

    def open_configured_adapter(
        self,
        directory: Path,
        record: ExecutionPlanRecord,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime],
    ) -> AbstractContextManager[GuardedAdapter]: ...


#: The private registry.  Only :func:`register_execution_adapter` writes to it.
_CONFIGURED_EXECUTION_ADAPTERS: Final[
    dict[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory]
] = {}

#: Registered configured adapters, keyed by ``(provider, operation)``.
#:
#: It is **empty**, and this read-only view is the structural statement of
#: that: no provider executes through this generic command yet, so
#: ``execute plan`` fails closed with ``EXECUTION_ADAPTER_NOT_CONFIGURED``
#: for every pair -- before the approval bridge is asked for anything, so a
#: receipt is never consumed for a plan nothing can execute.  A mapping proxy
#: means the emptiness cannot be revoked by an incidental ``dict`` mutation
#: from an unrelated import; a provider ticket must call
#: :func:`register_execution_adapter` deliberately, with its own contract,
#: credentials, transport, and live evidence.
CONFIGURED_EXECUTION_ADAPTERS: Final[
    Mapping[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory]
] = MappingProxyType(_CONFIGURED_EXECUTION_ADAPTERS)


def register_execution_adapter(
    provider: str, operation: OperationKind, factory: ConfiguredExecutionAdapterFactory
) -> None:
    """Register the one configured adapter factory for one provider operation.

    Registration is deliberate and single: a duplicate pair is refused rather
    than silently replaced, because two factories for one operation would make
    which credentials and transport executed an approved plan depend on import
    order.  ``RECOVER`` is not registrable as its own pair: a recovery reads
    the state of an attempt that some other operation started, and it reuses
    that subplan's adapter.
    """

    if (
        type(provider) is not str
        or provider not in PROVIDERS
        or type(operation) is not OperationKind
        or operation is OperationKind.RECOVER
        or not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "An execution adapter must name one installed provider and one plan operation.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if (provider, operation) in _CONFIGURED_EXECUTION_ADAPTERS:
        raise AdhelmError(
            "EXECUTION_ADAPTER_ALREADY_REGISTERED",
            "One provider operation has exactly one configured execution adapter.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    _CONFIGURED_EXECUTION_ADAPTERS[(provider, operation)] = factory


def configured_execution_adapter(
    provider: str, operation: OperationKind
) -> ConfiguredExecutionAdapterFactory | None:
    """Return the registered factory for one pair, or ``None`` when there is none."""

    return CONFIGURED_EXECUTION_ADAPTERS.get((provider, operation))


def executable_execution_operations() -> tuple[dict[str, str], ...]:
    """Return the exact registered pairs, sorted, for truthful discovery.

    ``discover`` renders this at call time rather than a hard-coded list, so
    the catalogue can never advertise an operation no adapter executes, or omit
    one a provider ticket registered.
    """

    return tuple(
        {"provider": provider, "operation": operation.value}
        for provider, operation in sorted(
            CONFIGURED_EXECUTION_ADAPTERS, key=lambda pair: (pair[0], pair[1].value)
        )
    )


#: The ordered gates an explicit generic execution request passes, with the
#: exact code each produces when it refuses.  The order is the one the caller
#: actually meets: the release-policy gate runs before the command does
#: anything, the CLI's readiness gate runs before the workflow, the bridge tries
#: its own two gates in this order, and the workflow resolves the adapter before
#: the approval exchange.
_REFUSAL_GATES: Final = (
    ("RELEASE_POLICY_WRITES_RELEASED", WRITES_NOT_RELEASED),
    ("COMPILED_APPROVAL_BRIDGE_INSTALLED", "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"),
    ("APPROVAL_KIND_RENDERED_BY_INSTALLED_HOST", "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED"),
    ("CONFIGURED_EXECUTION_ADAPTER", "EXECUTION_ADAPTER_NOT_CONFIGURED"),
)


def installed_execution_refusal_gates() -> tuple[dict[str, Any], ...]:
    """Return the ordered gates and whether each is satisfied right now.

    ``satisfied`` is ``None`` for the compiled-binding gate, and deliberately
    so: deciding it means resolving the binding, and ``discover`` and ``schema``
    are static reads that must not resolve a binding, start a helper, read a
    credential, or contact a provider.  The release-policy gate is never
    satisfied in this build, and the other two are read from a module constant
    and from the adapter registry, neither of which touches anything.
    """

    satisfied: tuple[bool | None, ...] = (
        False,
        None,
        ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN in _INSTALLED_BRIDGE_KINDS,
        bool(CONFIGURED_EXECUTION_ADAPTERS),
    )
    return tuple(
        {"gate": gate, "satisfied": passed, "refusal_code": code}
        for (gate, code), passed in zip(_REFUSAL_GATES, satisfied, strict=True)
    )


def first_execution_refusal_code() -> str | None:
    """Return the code an explicit request refuses with, or ``None``.

    Derived from the gate order rather than restated, so the catalogue can never
    advertise a refusal the caller never reaches -- naming
    ``EXECUTION_ADAPTER_NOT_CONFIGURED`` while a build refuses at the
    release-policy gate three gates earlier is exactly the mismatch this
    removes.

    A gate whose state a static read cannot decide is treated as one that may
    refuse, because skipping past it would name a later code the caller never
    gets to.  In an installed build that renders neither provider-neutral kind
    the earliest bridge refusal is named while the bridge might produce its
    sibling code; both appear in the gate list, both are bridge refusals, and
    both precede any credential.  ``None`` means every decidable gate is
    satisfied and the refusal, if any, comes from the plan itself.
    """

    for gate in installed_execution_refusal_gates():
        if gate["satisfied"] is not True:
            return str(gate["refusal_code"])
    return None


@runtime_checkable
class InstalledCompositeApprovalBridge(Protocol):
    """The private installed-bridge seam for the two provider-neutral kinds.

    The bridge -- not this module -- builds the challenge, runs the native
    exchange, and consumes the receipt, exactly as
    ``_installed_native_approval_bridge`` does for the two direct kinds.  This
    route never sees a receipt, a nonce, a challenge, or a trust pin; it
    receives only the already-recorded acceptance result.
    """

    def request_composite_execution_plan_approval(
        self,
        kernel: SafetyKernel,
        ledger: Ledger,
        *,
        record: ExecutionPlanRecord,
        source_plan_record_digest: str,
        grantor_ref: str,
    ) -> AcceptedNativeApproval: ...

    def request_attempt_recovery_approval(
        self,
        kernel: SafetyKernel,
        ledger: Ledger,
        *,
        record: ExecutionPlanRecord,
        source_plan_record_digest: str,
        attempts: tuple[tuple[SubPlan, str], ...],
        grantor_ref: str,
    ) -> AcceptedNativeApproval: ...


def _refuse_uninstalled_kind(kind: ApprovalChallengeKind) -> NoReturn:
    """Refuse in the ordinary build, in the installed bridge's own order.

    ``NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED`` first, because this checkout has no
    reviewed compiled binding at all; then
    ``NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED``, because even a build that
    installs one still has no signed host that renders a provider-neutral
    subject.  Both gates are the installed bridge's own, both refuse before a
    helper launch, a credential, or a provider request, and neither is a claim
    that the refused kind is unsafe.
    """

    _require_compiled_installed_approval_bridge_ready()
    _require_installed_bridge_kind(kind)
    raise AdhelmError(  # pragma: no cover - the gate above always refuses today
        "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED",
        "This approval kind has no installed native review capability.",
        next_action=(
            "Install a reviewed signed approval host that renders this kind, "
            "then request the review again."
        ),
    )


class _UninstalledCompositeApprovalBridge:
    """The only bridge this checkout ships: one that refuses both kinds."""

    def request_composite_execution_plan_approval(
        self,
        kernel: SafetyKernel,
        ledger: Ledger,
        *,
        record: ExecutionPlanRecord,
        source_plan_record_digest: str,
        grantor_ref: str,
    ) -> AcceptedNativeApproval:
        _refuse_uninstalled_kind(ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN)

    def request_attempt_recovery_approval(
        self,
        kernel: SafetyKernel,
        ledger: Ledger,
        *,
        record: ExecutionPlanRecord,
        source_plan_record_digest: str,
        attempts: tuple[tuple[SubPlan, str], ...],
        grantor_ref: str,
    ) -> AcceptedNativeApproval:
        _refuse_uninstalled_kind(ApprovalChallengeKind.ATTEMPT_RECOVERY)


def _resolve_composite_approval_bridge() -> InstalledCompositeApprovalBridge:
    """Return the installed bridge for the provider-neutral kinds.

    There is one, it refuses, and it is resolved here rather than captured in a
    default argument so an installed product -- or a test -- replaces exactly
    one private seam instead of widening a public signature.
    """

    return _UninstalledCompositeApprovalBridge()


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
            "CLOCK_INVALID", "Generic execution requires an aware local clock."
        ) from None


def _invalid_execution_input() -> AdhelmError:
    return AdhelmError(
        "INVALID_EXECUTION_REQUEST",
        "A complete closed generic execution request is required.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _checked_execution_request(request: object) -> ExecutionPlanExecutionRequest:
    if type(request) is not ExecutionPlanExecutionRequest:
        raise _invalid_execution_input()
    try:
        return validate_json_value(
            ExecutionPlanExecutionRequest, request.model_dump(mode="json", warnings="error")
        )
    except Exception:
        raise _invalid_execution_input() from None


def _checked_recovery_request(request: object) -> AttemptRecoveryRequest:
    if type(request) is not AttemptRecoveryRequest:
        raise _invalid_execution_input()
    try:
        return validate_json_value(
            AttemptRecoveryRequest, request.model_dump(mode="json", warnings="error")
        )
    except Exception:
        raise _invalid_execution_input() from None


def _adapter_not_configured(provider: str, operation: OperationKind) -> AdhelmError:
    """Refuse a pair no installed adapter executes, before any approval work."""

    return AdhelmError(
        "EXECUTION_ADAPTER_NOT_CONFIGURED",
        "NOT_CONFIGURED: no installed adapter executes a "
        f"{provider} {operation.value} execution plan.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Run discover for the installed execution operations; no approval was requested, "
            "no receipt was consumed, and no credential or provider was reached."
        ),
    )


def _adapter_incompatible(provider: str, operation: OperationKind, detail: str) -> AdhelmError:
    """Refuse a registered adapter that cannot execute this exact plan."""

    return AdhelmError(
        "EXECUTION_ADAPTER_INCOMPATIBLE",
        f"The installed {provider} {operation.value} adapter cannot execute this plan: {detail}",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Install an adapter whose declared capabilities cover this plan; no approval was "
            "requested, no receipt was consumed, and no credential or provider was reached."
        ),
    )


def _resolved_adapter_registry(
    supplied: object,
) -> Mapping[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory]:
    """Return the registry a resolution reads, defaulting to the installed one.

    The private seam replaces the whole registry rather than injecting one
    factory, so every path -- production and test -- resolves by the exact
    ``(provider, operation)`` key the retained plan names.  A factory can never
    be supplied for a pair it is not registered under.
    """

    if supplied is None:
        return CONFIGURED_EXECUTION_ADAPTERS
    if not isinstance(supplied, Mapping):
        raise _invalid_execution_input()
    return supplied


def _required_adapter_factory(
    registry: Mapping[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory],
    provider: str,
    operation: OperationKind,
) -> ConfiguredExecutionAdapterFactory:
    factory = registry.get((provider, operation))
    if factory is None:
        raise _adapter_not_configured(provider, operation)
    if not isinstance(factory, ConfiguredExecutionAdapterFactory):
        raise _invalid_execution_input()
    return factory


def _require_compatible_adapter(
    factory: ConfiguredExecutionAdapterFactory, subplan: SubPlan
) -> None:
    """Check the factory's declaration against the plan, before the bridge.

    A subplan whose ``expected_child_egress_count`` exceeds one is executable
    only by a :class:`adhelm.safety.ChildEgressAdapter`; the kernel refuses
    anything else in ``_preflight_one`` with ``ADAPTER_CHILD_EGRESS_REQUIRED``.
    Discovering that inside ``SafetyKernel.execute`` would be too late: the
    one-use receipt is already consumed and its durable claim written, so a
    retry would need a fresh human review of a plan that was never executable.
    """

    try:
        declared = factory.declared_capabilities()
    except Exception:
        raise _adapter_incompatible(
            subplan.provider, subplan.operation, "its capability declaration is unavailable"
        ) from None
    if (
        type(declared) is not ExecutionAdapterCapabilities
        or type(declared.child_egress) is not bool
    ):
        raise _adapter_incompatible(
            subplan.provider, subplan.operation, "its capability declaration is invalid"
        )
    if subplan.expected_child_egress_count > 1 and not declared.child_egress:
        raise _adapter_incompatible(
            subplan.provider,
            subplan.operation,
            "the plan declares "
            f"{subplan.expected_child_egress_count} journaled child requests and the adapter "
            "declares no child egress (ADAPTER_CHILD_EGRESS_REQUIRED)",
        )


def _loaded_record(
    loader: Callable[[Ledger, str], tuple[ExecutionPlanRecord, Mapping[str, Any]]],
    ledger: Ledger,
    record_digest: str,
) -> ExecutionPlanRecord:
    record, provenance = loader(ledger, record_digest)
    if (
        type(record) is not ExecutionPlanRecord
        or not isinstance(provenance, Mapping)
        or provenance.get("record_digest") != record_digest
        or provenance.get("plan_digest") != record.plan_digest
        or len(record.plan.subplans) != 1
    ):
        raise AdhelmError(
            "EXECUTION_PLAN_NOT_RETAINED",
            "The execution plan is not retained in verified local history.",
            next_action="List retained plans with plan execution catalog.",
        )
    return record


def _successor_record_digest(ledger: Ledger, record_digest: str) -> str | None:
    """Return the retained plan that names this one as its predecessor, if any.

    ``validate_execution_plan_history`` keeps the supersession chain linear and
    unforkable, so there is at most one.  The scan reads hash-chain verified
    local history and reaches nothing else.
    """

    for item in ledger.records("changes"):
        if (
            item.get("record_kind") == _EXECUTION_PLAN_RETAINED
            and type(item.get("payload")) is dict
            and item["payload"].get("supersedes_record_digest") == record_digest
            and type(item.get("record_digest")) is str
        ):
            return str(item["record_digest"])
    return None


def _require_current_retained_plan(
    ledger: Ledger, record: ExecutionPlanRecord, record_digest: str, now: datetime
) -> None:
    """Refuse before the bridge what the kernel would refuse after it.

    "Current" includes supersession.  A plan this workspace's own history
    records as replaced expresses an intent the operator compiled again from a
    later bounded read; executing the predecessor is not an authority bypass --
    the approval binds its own digests and the kernel still rejects drift --
    but it applies a decision local history says was superseded, from a strictly
    older read.  The successor is named so the caller can execute it instead.
    """

    if record.provenance.workspace_profile_ref != ledger.profile_ref:
        raise AdhelmError(
            "WORKSPACE_BINDING_REQUIRED",
            "Execution requires the exact local workspace the plan was compiled in.",
        )
    if record.expires_at <= now:
        raise AdhelmError(
            "PLAN_EXPIRED",
            "The retained execution plan has expired.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Compile a new plan from a fresh bounded read.",
        )
    successor = _successor_record_digest(ledger, record_digest)
    if successor is not None:
        raise AdhelmError(
            "EXECUTION_PLAN_SUPERSEDED",
            "The retained execution plan was superseded by a later plan compiled from a "
            f"fresher bounded read: {successor}",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                f"Execute the successor {successor}, or compile a new plan; no approval was "
                "requested, no receipt was consumed, and no credential or provider was reached."
            ),
        )
    if ledger.unresolved_starts() or ledger.unresolved_child_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED",
            "Resolve outstanding execution history before another guarded write.",
            next_action="List unresolved attempts with recover catalog.",
        )


def _required_composite_grant(
    accepted: object, plan: CompositePlan, expected_kind: ApprovalChallengeKind, now: datetime
) -> DirectGrant:
    """Require the exact digest-bound grant this plan's approval must produce."""

    if (
        type(accepted) is not AcceptedNativeApproval
        or accepted.kind is not expected_kind
        or accepted.policy is not None
        or type(accepted.direct_grant) is not DirectGrant
        or accepted.direct_grant.plan_digest != plan.plan_digest
        or accepted.direct_grant.subplan_digests != plan.subplan_digests
    ):
        raise AdhelmError(
            "COMPOSITE_EXECUTION_APPROVAL_REQUIRED",
            "Native approval did not produce the exact grant for this composite plan.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    if accepted.direct_grant.expires_at <= now:
        # The kernel refuses this too, a moment later.  Refusing here keeps the
        # order this function exists to guarantee: the configured adapter --
        # the first step that may reach a credential store or a provider -- is
        # never opened for an authorization that is already dead.
        raise AdhelmError(
            "AUTHORIZATION_EXPIRED",
            "The accepted execution grant expired before the plan could be executed.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Request a new native review; no credential was read and no provider "
                "request was made."
            ),
        )
    return accepted.direct_grant


def _require_matching_adapter(adapter: object, subplan: SubPlan) -> GuardedAdapter:
    if not isinstance(adapter, GuardedAdapter) or adapter.provider != subplan.provider:
        raise _adapter_not_configured(subplan.provider, subplan.operation)
    if subplan.expected_child_egress_count > 1 and not isinstance(adapter, ChildEgressAdapter):
        # The declaration checked before the bridge said otherwise.  The kernel
        # would refuse this too; refusing here names the disagreement instead of
        # reporting a bare capability error from inside the mutation path.
        raise _adapter_incompatible(
            subplan.provider,
            subplan.operation,
            "the opened adapter does not implement the child egress it declared",
        )
    return adapter


def _record_bytes(record: ExecutionPlanRecord) -> bytes:
    return canonical_bytes(record.model_dump(mode="json", warnings="error"))


def execute_retained_execution_plan_with_installed_approval(
    directory: Path,
    request: ExecutionPlanExecutionRequest,
    *,
    loader: Callable[
        [Ledger, str], tuple[ExecutionPlanRecord, Mapping[str, Any]]
    ] = load_execution_plan_record,
    expected_kind: ApprovalChallengeKind = ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN,
    _adapter_registry: Mapping[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory]
    | None = None,
) -> ExecutionResult:
    """Approve one retained provider-neutral plan and execute it through the kernel.

    The order is the whole point of this function:

    1. the retained record from a **verified checkpoint**, then the plan's own
       currency (workspace, expiry, and the unresolved-attempt barrier) -- all
       local reads;
    2. the adapter for ``(provider, operation)``, refused with
       ``EXECUTION_ADAPTER_NOT_CONFIGURED`` **before** the bridge, so a receipt
       is never consumed for a plan nothing can execute;
    3. the installed bridge, which builds and consumes the receipt privately
       and records the claim and the digest-bound grant;
    4. a reload of the same record, compared byte for byte, because approval
       took time this process does not own;
    5. the configured adapter -- the first step that may reach a credential or
       a provider -- opened inside the accepted grant's window;
    6. :meth:`adhelm.safety.SafetyKernel.execute`, which owns every remaining
       check, the durable ``APPLY_STARTED``, the permit, the readback, and the
       recovery facts.

    The keyword seams carry no authority: ``loader`` reads verified local
    history and ``expected_kind`` names one of the two provider-neutral kinds.
    ``_adapter_registry`` is a private test seam that replaces the whole
    registry, never one factory, so every resolution -- production and test --
    is a lookup by the exact ``(provider, operation)`` the retained plan names.
    None of them accepts a receipt, challenge, pin, key, grant, credential, or
    transport, and the CLI passes none of them.
    """

    release_policy.require_writes_released()
    if not isinstance(directory, Path):
        raise _invalid_execution_input()
    if expected_kind is not ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN:
        raise _invalid_execution_input()
    checked = _checked_execution_request(request)
    registry = _resolved_adapter_registry(_adapter_registry)
    ledger = Ledger(directory)
    record = _loaded_record(loader, ledger, checked.plan_record_digest)
    plan = record.plan
    subplan = plan.subplans[0]
    _require_current_retained_plan(ledger, record, checked.plan_record_digest, _checked_now())
    factory = _required_adapter_factory(registry, record.provider, record.operation)
    _require_compatible_adapter(factory, subplan)
    kernel = SafetyKernel(directory, ledger, clock=_checked_now)
    accepted = _resolve_composite_approval_bridge().request_composite_execution_plan_approval(
        kernel,
        ledger,
        record=record,
        source_plan_record_digest=checked.plan_record_digest,
        grantor_ref=INSTALLED_EXECUTION_LOGICAL_GRANTOR_REF,
    )
    grant = _required_composite_grant(accepted, plan, expected_kind, _checked_now())
    if _record_bytes(_loaded_record(loader, ledger, checked.plan_record_digest)) != _record_bytes(
        record
    ):
        raise AdhelmError(
            "PLAN_BINDING_MISMATCH",
            "The retained execution plan changed while it was being approved.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    _require_current_retained_plan(ledger, record, checked.plan_record_digest, _checked_now())
    with factory.open_configured_adapter(directory, record, ledger, clock=_checked_now) as opened:
        adapter = _require_matching_adapter(opened, subplan)
        return kernel.execute(plan, grant, {subplan.provider: adapter})


def _recovery_start_not_found() -> AdhelmError:
    return AdhelmError(
        "ATTEMPT_RECOVERY_START_NOT_FOUND",
        "No unresolved durable attempt start with this record digest is in local history.",
        next_action="List unresolved attempts with recover catalog.",
    )


def _parent_start_digest(ledger: Ledger, child: Mapping[str, Any]) -> str | None:
    """Return the durable ``APPLY_STARTED`` of the attempt a child belongs to."""

    payload = child["payload"]
    for record in ledger.records("changes"):
        if (
            record.get("record_kind") == "CHANGE_EVENT"
            and type(record.get("payload")) is dict
            and record["payload"].get("event_type") == _APPLY_STARTED
            and all(
                record["payload"].get(field) == payload.get(field)
                for field in ("idempotency_ref", "plan_digest", "subplan_digest", "approval_ref")
            )
            and type(record.get("record_digest")) is str
        ):
            return str(record["record_digest"])
    return None


def _refuse_child_start(ledger: Ledger, child: Mapping[str, Any]) -> NoReturn:
    """Refuse a journaled child request with the parent attempt it belongs to.

    ``recover attempt`` acts on a subplan-level attempt: the kernel records one
    observation for the whole approved operation and that release resolves every
    child of it.  A child digest is therefore not a wrong-shaped input but the
    wrong *level*, and saying so with the parent's digest is the difference
    between an actionable diagnostic and a dead end.
    """

    parent = _parent_start_digest(ledger, child)
    raise AdhelmError(
        "ATTEMPT_RECOVERY_CHILD_START_NOT_RECOVERABLE",
        "This is a journaled child request of a larger attempt, not the attempt itself"
        + (f"; its durable attempt start is {parent}" if parent is not None else ""),
        next_action=(
            (f"Recover the attempt {parent}, which releases every child of it.")
            if parent is not None
            else "Inspect history verify; the child's own attempt start is not in local history."
        ),
    )


def _selected_unresolved_start(ledger: Ledger, started_record_digest: str) -> dict[str, Any]:
    """Select exactly one unresolved ``APPLY_STARTED`` record, or refuse."""

    selected = [
        record
        for record in ledger.unresolved_starts()
        if record.get("record_digest") == started_record_digest
        and record.get("record_kind") == "CHANGE_EVENT"
        and type(record.get("payload")) is dict
        and record["payload"].get("event_type") == _APPLY_STARTED
    ]
    if len(selected) != 1:
        children = [
            record
            for record in ledger.unresolved_child_starts()
            if record.get("record_digest") == started_record_digest
            and record.get("record_kind") == _CHILD_APPLY_STARTED
            and type(record.get("payload")) is dict
        ]
        if len(children) == 1:
            _refuse_child_start(ledger, children[0])
        raise _recovery_start_not_found()
    return selected[0]


def _retained_record_for_plan(
    checkpoint_records: Sequence[Mapping[str, Any]], plan_digest: str
) -> ExecutionPlanRecord:
    """Rebuild the exact plan of an unresolved attempt from retained history.

    Only a plan retained as ``EXECUTION_PLAN_RETAINED`` can be recovered here.
    An attempt started from one of the three provider-specific retained
    families has no provider-neutral record to rebuild, and this refuses rather
    than reconstructing a plan from the attempt's own digests -- which would be
    a plan nobody compiled.
    """

    from adhelm.execution_plan_records import revalidate_execution_plan_record

    matching = [
        record["payload"]
        for record in checkpoint_records
        if record.get("record_kind") == _EXECUTION_PLAN_RETAINED
        and type(record.get("payload")) is dict
        and record["payload"].get("plan_digest") == plan_digest
    ]
    if not matching:
        raise AdhelmError(
            "ATTEMPT_RECOVERY_PLAN_NOT_RETAINED",
            "The unresolved attempt's exact plan is not retained as a generic execution plan.",
            next_action=(
                "Recovery requires the provider-neutral retained plan; inspect "
                "plan execution catalog and history status."
            ),
        )
    rebuilt = [revalidate_execution_plan_record(payload) for payload in matching]
    if len({_record_bytes(record) for record in rebuilt}) != 1:
        raise AdhelmError(
            "ATTEMPT_RECOVERY_PLAN_AMBIGUOUS",
            "Local history retains more than one different plan for this digest.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another recovery.",
        )
    return rebuilt[0]


def _authorization_unverified() -> AdhelmError:
    return AdhelmError(
        "ATTEMPT_RECOVERY_AUTHORIZATION_UNVERIFIED",
        "The attempt's original durable authorization could not be rebuilt exactly.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Inspect history status; no approval was requested and nothing was recorded.",
    )


def _reconstructed_direct_grant(
    checkpoint_records: Sequence[Mapping[str, Any]], *, approval_ref: str, plan: CompositePlan
) -> DirectGrant:
    """Rebuild the attempt's own grant from its durable records, and prove it.

    ``record_attempt_recovery`` needs the authorization the attempt started
    under in order to find that exact ``APPLY_STARTED``; it is not the
    authorization that permits the recovery, which is the separate one-use
    recovery grant the bridge produces.  Nothing here is invented: every field
    comes from a durable record, and the rebuild is accepted only when it
    reproduces the ``authorization_digest`` the kernel appended when the grant
    was recorded.  A delegated attempt has no such grant and is refused.
    """

    granted = [
        record["payload"]
        for record in checkpoint_records
        if record.get("record_kind") == "CHANGE_EVENT"
        and type(record.get("payload")) is dict
        and record["payload"].get("event_type") == _APPROVAL_GRANTED
        and record["payload"].get("approval_ref") == approval_ref
        and record["payload"].get("plan_digest") == plan.plan_digest
    ]
    claimed = [
        record["payload"]
        for record in checkpoint_records
        if record.get("record_kind") == _RECEIPT_CLAIMED
        and type(record.get("payload")) is dict
        and record["payload"].get("receipt_id") == approval_ref
    ]
    if len(granted) != 1 or len(claimed) != 1:
        raise _authorization_unverified()
    event, claim = granted[0], claimed[0]
    try:
        evidence = event["evidence_refs"]
        challenge = claim["challenge"]
        configuration = claim["host_configuration"]
        if type(evidence) is not list or len(evidence) != 1:
            raise ValueError("approval evidence")
        grant = DirectGrant(
            authorization_id=approval_ref,
            source=DirectGrantSource.USER_CONTROLLED_HOST,
            grantor_ref=str(event["actor_ref"]),
            host_ref=str(configuration["host_ref"]),
            user_interaction_ref=str(evidence[0]),
            plan_digest=plan.plan_digest,
            subplan_digests=plan.subplan_digests,
            expires_at=_parsed_timestamp(challenge["expires_at"]),
        )
    except AdhelmError:
        raise
    except Exception:
        raise _authorization_unverified() from None
    if digest(grant.model_dump(mode="json")) != event.get("authorization_digest"):
        raise _authorization_unverified()
    return grant


def _parsed_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp")
    return parsed.astimezone(UTC)


def _required_recovery_grant(
    accepted: object,
    plan: CompositePlan,
    attempts: tuple[tuple[SubPlan, str], ...],
    expected_kind: ApprovalChallengeKind,
) -> tuple[DirectGrant, UserApprovalHost]:
    """Require a grant bound to exactly these unresolved attempts, and its host."""

    subjects = tuple(
        attempt_recovery_subject(plan, subplan, started_record_digest=started)
        for subplan, started in attempts
    )
    if (
        type(accepted) is not AcceptedNativeApproval
        or accepted.kind is not expected_kind
        or accepted.policy is not None
        or type(accepted.direct_grant) is not DirectGrant
        or accepted.approval_host is None
        or accepted.direct_grant.plan_digest != digest(list(subjects))
        or accepted.direct_grant.subplan_digests != subjects
    ):
        raise AdhelmError(
            "ATTEMPT_RECOVERY_APPROVAL_REQUIRED",
            "Native approval did not produce the exact grant for these unresolved attempts.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    return accepted.direct_grant, accepted.approval_host


def _recorded_observation(
    ledger: Ledger, *, started_record_digest: str, recovery_approval_ref: str
) -> Mapping[str, Any]:
    selected = [
        record
        for record in ledger.records("changes")
        if record.get("record_kind") == _RECOVERY_OBSERVED
        and type(record.get("payload")) is dict
        and record["payload"].get("started_record_digest") == started_record_digest
        and record["payload"].get("recovery_approval_ref") == recovery_approval_ref
    ]
    if len(selected) != 1:
        raise AdhelmError(
            "ATTEMPT_RECOVERY_RECORD_UNVERIFIED",
            "The recovery may be recorded, but its exact observation could not be reread.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify and history status; do not repeat the recovery.",
        )
    return selected[0]


def record_retained_attempt_recovery_with_installed_approval(
    directory: Path,
    request: AttemptRecoveryRequest,
    *,
    expected_kind: ApprovalChallengeKind = ApprovalChallengeKind.ATTEMPT_RECOVERY,
    _adapter_registry: Mapping[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory]
    | None = None,
) -> AttemptRecoveryOutcome:
    """Record the current state of one unresolved attempt under its own approval.

    This is the command surface the kernel generalization deferred.  It sends
    no provider mutation, attributes nothing, settles nothing, and never
    upgrades the attempt's effect status: it takes a fresh identity check and
    read through the configured adapter, records that observation with an
    ``UNKNOWN`` actor, and releases the global write barrier so one lost
    acknowledgement cannot refuse every later plan on every provider forever.

    The refusal order matches the execute route: local history and the exact
    unresolved attempt first, then the adapter, then the installed bridge, then
    the credential-bearing adapter, then the kernel.
    """

    release_policy.require_writes_released()
    if not isinstance(directory, Path):
        raise _invalid_execution_input()
    if expected_kind is not ApprovalChallengeKind.ATTEMPT_RECOVERY:
        raise _invalid_execution_input()
    checked = _checked_recovery_request(request)
    registry = _resolved_adapter_registry(_adapter_registry)
    ledger = Ledger(directory)
    start = _selected_unresolved_start(ledger, checked.started_record_digest)
    payload = start["payload"]
    checkpoint_records = ledger.checkpoint()["records_by_stream"]["changes"]
    record = _retained_record_for_plan(checkpoint_records, str(payload.get("plan_digest")))
    plan = record.plan
    subplan = plan.subplans[0]
    if (
        subplan.subplan_digest != payload.get("subplan_digest")
        or subplan.provider != start.get("provider")
        or subplan.account.account_ref != start.get("account_ref")
        or plan.plan_id != start.get("run_id")
    ):
        raise _recovery_start_not_found()
    authorization = _reconstructed_direct_grant(
        checkpoint_records, approval_ref=str(payload.get("approval_ref")), plan=plan
    )
    attempts = ((subplan, checked.started_record_digest),)
    factory = _required_adapter_factory(registry, record.provider, record.operation)
    _require_compatible_adapter(factory, subplan)
    kernel = SafetyKernel(directory, ledger, clock=_checked_now)
    accepted = _resolve_composite_approval_bridge().request_attempt_recovery_approval(
        kernel,
        ledger,
        record=record,
        source_plan_record_digest=str(payload.get("plan_digest")),
        attempts=attempts,
        grantor_ref=INSTALLED_RECOVERY_LOGICAL_GRANTOR_REF,
    )
    recovery, host = _required_recovery_grant(accepted, plan, attempts, expected_kind)
    with factory.open_configured_adapter(directory, record, ledger, clock=_checked_now) as opened:
        adapter = _require_matching_adapter(opened, subplan)
        result = kernel.record_attempt_recovery(
            plan, authorization, recovery, host, {subplan.provider: adapter}
        )
    observed = _recorded_observation(
        ledger,
        started_record_digest=checked.started_record_digest,
        recovery_approval_ref=recovery.authorization_id,
    )
    return _recovery_outcome(result, observed, record)


def _recovery_outcome(
    result: object, observed: Mapping[str, Any], record: ExecutionPlanRecord
) -> AttemptRecoveryOutcome:
    try:
        if type(result) is not ExecutionResult or len(result.results) != 1:
            raise ValueError("result")
        payload = observed["payload"]
        outcome = AttemptRecoveryOutcome(
            result=result,
            record_digest=str(observed["record_digest"]),
            started_record_digest=str(payload["started_record_digest"]),
            plan_digest=str(payload["plan_digest"]),
            subplan_digest=str(payload["subplan_digest"]),
            provider=record.provider,
            operation=record.operation.value,
            observed_relation=str(payload["observed_relation"]),
            disposition=str(payload["disposition"]),
            actor_ref=str(payload["actor_ref"]),
            attempt_effect_status=str(payload["attempt_effect_status"]),
        )
    except Exception:
        raise AdhelmError(
            "ATTEMPT_RECOVERY_RECORD_UNVERIFIED",
            "The recovery may be recorded, but its exact observation could not be reread.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify and history status; do not repeat the recovery.",
        ) from None
    if (
        outcome.plan_digest != record.plan_digest
        or outcome.actor_ref != "UNKNOWN"
        or outcome.attempt_effect_status != "AMBIGUOUS_REMOTE_STATE"
        or result.plan_digest != record.plan_digest
    ):
        raise AdhelmError(
            "ATTEMPT_RECOVERY_RECORD_UNVERIFIED",
            "The recorded observation does not describe this exact unresolved attempt.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify and history status; do not repeat the recovery.",
        )
    return outcome


_MAXIMUM_CATALOG_LIMIT: Final = 500


def unresolved_attempt_catalog(
    ledger: Ledger, *, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    """Return the unresolved attempts that block every later guarded write.

    Both levels appear: a subplan attempt whose durable ``APPLY_STARTED`` has
    no resolving record, and a journaled child request of one such attempt.
    ``recoverable`` states only whether ``recover attempt`` can act on that row
    here -- it needs the provider-neutral retained plan -- and never implies
    that recovering it makes a retry safe.
    """

    if (
        type(cursor) is not int
        or type(cursor) is bool
        or cursor < 0
        or type(limit) is not int
        or type(limit) is bool
        or not 1 <= limit <= _MAXIMUM_CATALOG_LIMIT
    ):
        raise AdhelmError(
            "INVALID_ATTEMPT_RECOVERY_SELECTION",
            "Unresolved attempt pagination is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    retained = {
        record["payload"]["plan_digest"]
        for record in ledger.records("changes")
        if record.get("record_kind") == _EXECUTION_PLAN_RETAINED
        and type(record.get("payload")) is dict
        and type(record["payload"].get("plan_digest")) is str
    }
    rows: list[dict[str, Any]] = [
        _catalog_row(record, level="SUBPLAN", retained=retained)
        for record in ledger.unresolved_starts()
    ] + [
        _catalog_row(record, level="CHILD", retained=retained)
        for record in ledger.unresolved_child_starts()
    ]
    rows.sort(key=lambda row: (str(row["recorded_at"]), str(row["started_record_digest"])))
    if cursor > len(rows):
        raise AdhelmError(
            "INVALID_ATTEMPT_RECOVERY_SELECTION",
            "Cursor exceeds the unresolved attempt count.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    page = rows[cursor : cursor + limit]
    return {
        "attempts": page,
        "total_count": len(rows),
        "next_cursor": str(cursor + limit) if cursor + limit < len(rows) else None,
        "history_verification": "HASH_CHAIN_VERIFIED_LOCAL_SCAN",
        "blocking_effect": "BLOCKS_EVERY_LATER_GUARDED_WRITE",
        "attribution": "NO_ACTOR_ATTRIBUTED",
        "recovery_command": "recover attempt",
        "recovery_authorizes_retry": False,
    }


def _catalog_row(record: Mapping[str, Any], *, level: str, retained: set[str]) -> dict[str, Any]:
    payload = record["payload"]
    plan_digest = payload.get("plan_digest")
    return {
        "started_record_digest": record["record_digest"],
        "level": level,
        "child_index": payload.get("child_index") if level == "CHILD" else None,
        "provider": record.get("provider"),
        "operation": payload.get("operation"),
        "plan_digest": plan_digest,
        "subplan_digest": payload.get("subplan_digest"),
        "recorded_at": record.get("recorded_at"),
        "refusal_code": "UNRESOLVED_CHILD_APPLY" if level == "CHILD" else "UNRESOLVED_APPLY",
        "recoverable": level == "SUBPLAN" and plan_digest in retained,
    }


__all__ = [
    "CONFIGURED_EXECUTION_ADAPTERS",
    "AttemptRecoveryOutcome",
    "AttemptRecoveryRequest",
    "ConfiguredExecutionAdapterFactory",
    "ExecutionAdapterCapabilities",
    "ExecutionPlanExecutionRequest",
    "InstalledCompositeApprovalBridge",
    "configured_execution_adapter",
    "executable_execution_operations",
    "execute_retained_execution_plan_with_installed_approval",
    "first_execution_refusal_code",
    "installed_execution_refusal_gates",
    "record_retained_attempt_recovery_with_installed_approval",
    "register_execution_adapter",
    "unresolved_attempt_catalog",
]

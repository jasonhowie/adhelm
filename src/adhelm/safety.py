"""Deterministic mutation safety kernel.

The kernel owns no credentials and imports no provider SDK.  Providers are supplied
as typed adapters, which makes the ordering and failure semantics testable without
network access.  JSONL is the source of truth for authorization consumption and
delegation reservations; a read index must never participate in these decisions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol, cast, runtime_checkable
from uuid import uuid4

import portalocker
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm import release_policy
from adhelm.canonical import digest
from adhelm.egress_permit import EgressPermit, _invalidate_permit, _kernel_permit, _permit_claimed
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import MAX_CHILD_EGRESS_COUNT
from adhelm.privacy import validate_data


class StrictFrozenModel(BaseModel):
    """Strict scalar/value-object schemas used at the mutation boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class OperationKind(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    ASSOCIATE = "ASSOCIATE"
    REPLACE = "REPLACE"
    ACTIVATE = "ACTIVATE"
    PAUSE = "PAUSE"
    ENABLE = "ENABLE"
    ARCHIVE = "ARCHIVE"
    DELETE = "DELETE"
    PUBLISH = "PUBLISH"
    RESTORE = "RESTORE"
    #: Recording the current state after an unattributable attempt.  It is a
    #: grant and policy subject, never a compiled subplan operation: it sends no
    #: mutation and ``SubPlan`` refuses it below.
    RECOVER = "RECOVER"


class EffectStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    APPLIED_VERIFIED = "APPLIED_VERIFIED"
    FAILED_NO_EFFECT = "FAILED_NO_EFFECT"
    PARTIAL_EFFECT = "PARTIAL_EFFECT"
    AMBIGUOUS_REMOTE_STATE = "AMBIGUOUS_REMOTE_STATE"


class ApplyOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


class Money(StrictFrozenModel):
    minor_units: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class StateRef(StrictFrozenModel):
    """An allowlisted remote state fingerprint, never a raw API response."""

    fingerprint: str = Field(min_length=16, max_length=256)
    revision: str | None = Field(default=None, max_length=256)


class EgressTarget(StrictFrozenModel):
    host: str = Field(pattern=r"^[A-Za-z0-9.-]+$")
    purpose: str = Field(min_length=1, max_length=120)


class AccountIdentity(StrictFrozenModel):
    account_ref: str = Field(min_length=1, max_length=160)
    stable_account_id: str = Field(min_length=1, max_length=256)
    auth_profile_ref: str = Field(min_length=1, max_length=160)
    principal_ref: str = Field(min_length=1, max_length=256)
    client_application_ref: str = Field(min_length=1, max_length=256)
    scopes: tuple[str, ...] = Field(min_length=1)

    @field_validator("scopes")
    @classmethod
    def ordered_unique_scopes(cls, scopes: tuple[str, ...]) -> tuple[str, ...]:
        if any(not scope for scope in scopes) or tuple(sorted(set(scopes))) != scopes:
            raise ValueError("scopes must be unique and sorted")
        return scopes


class OpenAIAccountKeyIdentity(StrictFrozenModel):
    """Account-key binding without invented OAuth principal, application or scopes.

    The adapter must prove current account access with the bound credential.
    Its private HMAC binding identifies exact credential material, not a human
    principal or provider permission. This schema alone proves no access.
    """

    identity_kind: Literal["OPENAI_ADS_ACCOUNT_KEY"] = "OPENAI_ADS_ACCOUNT_KEY"
    provider_scope_status: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    account_ref: str = Field(min_length=1, max_length=160)
    stable_account_id: str = Field(min_length=1, max_length=256)
    auth_profile_ref: str = Field(min_length=1, max_length=160)
    credential_binding_ref: str = Field(pattern=r"^credential-binding:sha256:[0-9a-f]{64}$")


class FieldChange(StrictFrozenModel):
    path: str = Field(min_length=1, max_length=256)
    before: StateRef
    after: StateRef


def _subplan_recovery_schema(schema: dict[str, Any]) -> None:
    schema["properties"]["recovery_disposition"] = {
        "type": "string",
        "const": "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION",
    }
    schema.setdefault("allOf", []).append(
        {
            "if": {"required": ["recovery_disposition"]},
            "then": {
                "properties": {
                    "provider": {"const": "ga4_admin"},
                    "operation": {"const": "CREATE"},
                    "recovery_actions": {"maxItems": 0},
                }
            },
            "else": {"properties": {"recovery_actions": {"minItems": 1}}},
        }
    )


class SubPlan(StrictFrozenModel):
    """One provider operation.  Nested values are immutable value objects."""

    model_config = ConfigDict(json_schema_extra=_subplan_recovery_schema)
    subplan_id: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=80)
    operation: OperationKind
    account: AccountIdentity | OpenAIAccountKeyIdentity
    provider_api_version: str = Field(min_length=1, max_length=80)
    capability_version: str = Field(min_length=1, max_length=80)
    before: StateRef
    after: StateRef
    field_changes: tuple[FieldChange, ...] = ()
    max_spend: Money
    egress: tuple[EgressTarget, ...] = Field(min_length=1)
    recovery_actions: tuple[OperationKind, ...]
    recovery_disposition: Literal["RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"] | None = None
    expected_child_egress_count: int = Field(default=1, ge=1, le=MAX_CHILD_EGRESS_COUNT)
    expires_at: datetime

    @model_validator(mode="after")
    def bounded_recovery(self) -> SubPlan:
        if "recovery_disposition" in self.model_fields_set:
            if (
                self.recovery_disposition != "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"
                or self.recovery_actions
                or self.provider != "ga4_admin"
                or self.operation is not OperationKind.CREATE
            ):
                raise ValueError("Only GA4 CREATE supports explicit read-only recovery disposition")
        elif not self.recovery_actions:
            raise ValueError("Recovery actions or an explicitly supported disposition are required")
        return self

    @model_serializer(mode="wrap")
    def preserve_single_request_wire(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Keep the wire form of an ordinary single-request subplan unchanged.

        ``expected_child_egress_count`` is dropped whenever it is the default
        ``1``, so adding multi-step support cannot silently change any retained
        subplan digest, and two semantically identical single-request subplans
        cannot acquire different digests through an explicit default.
        """

        output = handler(self)
        if not isinstance(output, dict):
            raise ValueError("Subplan must serialize as an object")
        if "recovery_disposition" not in self.model_fields_set:
            output.pop("recovery_disposition", None)
        if self.expected_child_egress_count == 1:
            output.pop("expected_child_egress_count", None)
        return output

    @model_validator(mode="after")
    def recover_is_not_a_compiled_operation(self) -> SubPlan:
        if self.operation is OperationKind.RECOVER or OperationKind.RECOVER in (
            self.recovery_actions
        ):
            raise ValueError("RECOVER authorizes recording an observation, not a provider request")
        return self

    @model_validator(mode="after")
    def account_key_kind_matches_provider(self) -> SubPlan:
        if isinstance(self.account, OpenAIAccountKeyIdentity) and self.provider != "openai_ads":
            raise ValueError("OpenAI account-key evidence cannot identify another provider")
        return self

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def subplan_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class CompositePlan(StrictFrozenModel):
    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1, max_length=160)
    profile_ref: str = Field(min_length=1, max_length=160)
    subplans: tuple[SubPlan, ...] = Field(min_length=1)
    expires_at: datetime
    stop_on_non_verified: Literal[True] = True

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("plan schema_version must be the integer 1")
        return value

    @field_validator("stop_on_non_verified", mode="before")
    @classmethod
    def exact_stop_control(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("plan stop_on_non_verified must be true")
        return value

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_order_and_expiry(self) -> CompositePlan:
        ids = tuple(item.subplan_id for item in self.subplans)
        if len(set(ids)) != len(ids):
            raise ValueError("subplan IDs must be unique")
        providers = tuple(item.provider for item in self.subplans)
        if len(set(providers)) != len(providers):
            raise ValueError("a composite plan has one ordered subplan per provider")
        if any(item.expires_at > self.expires_at for item in self.subplans):
            raise ValueError("subplan expiry cannot exceed composite expiry")
        return self

    @property
    def plan_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def subplan_digests(self) -> tuple[str, ...]:
        return tuple(item.subplan_digest for item in self.subplans)


class DirectGrantSource(StrEnum):
    USER_CONTROLLED_HOST = "USER_CONTROLLED_HOST"
    AGENT_ASSERTION = "AGENT_ASSERTION"


class DirectGrant(StrictFrozenModel):
    """A direct grant has a host-mediated user-originated interaction reference."""

    authorization_id: str = Field(min_length=1, max_length=160)
    source: DirectGrantSource
    grantor_ref: str = Field(min_length=1, max_length=256)
    host_ref: str = Field(min_length=1, max_length=256)
    user_interaction_ref: str = Field(min_length=1, max_length=256)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    subplan_digests: tuple[str, ...] = Field(min_length=1)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def direct_origin_is_not_an_agent_assertion(self) -> DirectGrant:
        if self.source is not DirectGrantSource.USER_CONTROLLED_HOST:
            raise ValueError("an agent assertion cannot establish a direct grant")
        if len(set(self.subplan_digests)) != len(self.subplan_digests):
            raise ValueError("subplan digests must be unique")
        return self


class MoneyLimit(StrictFrozenModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    maximum_minor_units: int = Field(ge=0)


class DelegationPolicySource(StrEnum):
    USER_CONTROLLED_CONFIGURATION = "USER_CONTROLLED_CONFIGURATION"
    AGENT_PROPOSAL = "AGENT_PROPOSAL"


class DelegationPolicy(StrictFrozenModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: int = Field(ge=1)
    source: DelegationPolicySource
    grantor_ref: str = Field(min_length=1, max_length=256)
    source_authorization_ref: str = Field(min_length=1, max_length=256)
    configuration_host_ref: str = Field(min_length=1, max_length=256)
    configuration_interaction_ref: str = Field(min_length=1, max_length=256)
    authorized_agent_or_host_ref: str = Field(min_length=1, max_length=256)
    account_stable_ids: tuple[str, ...] = Field(min_length=1)
    allowed_operations: tuple[OperationKind, ...] = Field(min_length=1)
    maximum_change_per_operation: tuple[MoneyLimit, ...] = Field(min_length=1)
    cumulative_change_bounds: tuple[MoneyLimit, ...] = Field(min_length=1)
    effective_from: datetime
    expires_at: datetime
    allowed_egress_hosts: tuple[str, ...] = Field(min_length=1)
    revoked_at: datetime | None = None

    @field_validator("revoked_at")
    @classmethod
    def utc_revocation(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("policy revocation timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def finite_unique_policy_bounds(self) -> DelegationPolicy:
        if self.source is not DelegationPolicySource.USER_CONTROLLED_CONFIGURATION:
            raise ValueError("an agent proposal cannot establish a delegation policy")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.effective_from, self.expires_at)
        ):
            raise ValueError("policy timestamps must be timezone-aware")
        if self.effective_from >= self.expires_at:
            raise ValueError("policy expiry must follow effective time")
        for values in (self.account_stable_ids, self.allowed_egress_hosts):
            if len(set(values)) != len(values):
                raise ValueError("policy values must be unique")
        for limits in (self.maximum_change_per_operation, self.cumulative_change_bounds):
            currencies = tuple(limit.currency for limit in limits)
            if len(set(currencies)) != len(currencies):
                raise ValueError("each policy currency needs one finite limit")
        return self

    @property
    def policy_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class DelegatedGrant(StrictFrozenModel):
    authorization_id: str = Field(min_length=1, max_length=160)
    policy: DelegationPolicy
    issuing_agent_or_host_ref: str = Field(min_length=1, max_length=256)
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


Authorization = DirectGrant | DelegatedGrant


def _revalidate[T: BaseModel](value: object, model: type[T]) -> T:
    """Do not trust model_copy/model_construct or nested prevalidated objects."""
    try:
        if not isinstance(value, model):
            raise ValueError("Unexpected safety value object")
        validated = model.model_validate(value.model_dump(mode="python", warnings=False))
    except (ValidationError, ValueError) as exc:
        raise AdhelmError(
            "INVALID_SAFETY_INPUT",
            "Input violates the closed safety contract.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc
    validate_data(validated.model_dump(mode="json"))
    return validated


def _revalidate_authorization(authorization: Authorization) -> Authorization:
    return (
        _revalidate(authorization, DirectGrant)
        if isinstance(authorization, DirectGrant)
        else _revalidate(authorization, DelegatedGrant)
    )


class AdapterCapabilities(StrictFrozenModel):
    provider_api_version: str
    capability_version: str
    supported_operations: tuple[OperationKind, ...]
    writes_enabled: bool = True


class RemoteIdentity(StrictFrozenModel):
    stable_account_id: str
    auth_profile_ref: str
    principal_ref: str
    client_application_ref: str
    scopes: tuple[str, ...]


class RemoteOpenAIAccountKeyIdentity(StrictFrozenModel):
    """A provider adapter's current account access and exact local key binding."""

    identity_kind: Literal["OPENAI_ADS_ACCOUNT_KEY"] = "OPENAI_ADS_ACCOUNT_KEY"
    provider_scope_status: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    stable_account_id: str = Field(min_length=1, max_length=256)
    auth_profile_ref: str = Field(min_length=1, max_length=160)
    credential_binding_ref: str = Field(pattern=r"^credential-binding:sha256:[0-9a-f]{64}$")


class RemoteState(StrictFrozenModel):
    fingerprint: str
    revision: str | None = None


class ApplyResponse(StrictFrozenModel):
    outcome: ApplyOutcome
    no_effect_proven: bool = False
    opaque_resource_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @field_validator("opaque_resource_refs")
    @classmethod
    def opaque_refs_only(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_refs(refs, kind="opaque")

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_only(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_refs(refs, kind="evidence")

    @model_validator(mode="after")
    def no_effect_claim_has_proof(self) -> ApplyResponse:
        if self.no_effect_proven and (
            self.outcome is not ApplyOutcome.FAILED or not self.evidence_refs
        ):
            raise ValueError("No-effect proof requires a failed response and evidence")
        return self


class Verification(StrictFrozenModel):
    verified: bool
    evidence_refs: tuple[str, ...] = ()

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_only(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_refs(refs, kind="evidence")

    @model_validator(mode="after")
    def proof_for_claimed_verification(self) -> Verification:
        if self.verified and not self.evidence_refs:
            raise ValueError("verified state requires evidence references")
        return self


class Reconciliation(StrictFrozenModel):
    """Provider-aware read-only proof, including asynchronous operation settlement."""

    state: RemoteState
    status: EffectStatus
    settled: bool
    evidence_refs: tuple[str, ...] = ()

    @field_validator("evidence_refs")
    @classmethod
    def evidence_only(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_refs(refs, kind="evidence")

    @model_validator(mode="after")
    def settled_state_has_evidence(self) -> Reconciliation:
        if self.status is EffectStatus.NOT_STARTED:
            raise ValueError("Reconciliation describes observed effects, not execution order")
        if self.settled != (self.status is not EffectStatus.AMBIGUOUS_REMOTE_STATE):
            raise ValueError("Unsettled writes remain ambiguous")
        if self.settled and not self.evidence_refs:
            raise ValueError("Settled effects require evidence")
        return self


@runtime_checkable
class Adapter(Protocol):
    """Provider boundary. Runtime adapters must be explicitly registered by callers."""

    provider: str

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities: ...

    def read_identity(
        self, account_ref: str
    ) -> RemoteIdentity | RemoteOpenAIAccountKeyIdentity: ...

    def read_state(self, plan: SubPlan) -> RemoteState: ...

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse: ...

    def verify(self, plan: SubPlan) -> Verification: ...

    def reconcile(self, plan: SubPlan) -> Reconciliation: ...


@runtime_checkable
class GuardedAdapter(Adapter, Protocol):
    """An adapter that refuses mutation outside a current kernel-issued permit."""

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse: ...


#: A child label names the step in the approved operation, never the resource a
#: provider assigns to it.  Every hyphen-separated word is at least two
#: characters, starts with a letter, and carries at most four consecutive
#: digits, so the identifier shapes a provider assigns cannot be journaled as a
#: label: ``123456789``, ``campaign-88213``, ``act120210000000000``,
#: ``g-abc123def``, and anything containing ``/`` are all refused, while a
#: bounded ordinal such as ``step2`` or ``v2024`` is admitted.  This constrains
#: shape, not meaning.  ``adhelm.ledger`` enforces the same pattern on the record.
_CHILD_LABEL_WORD_PATTERN = r"[a-z](?:(?:[0-9]{0,4}[a-z])+[0-9]{0,4}|[0-9]{1,4})"
_CHILD_LABEL_WORD = re.compile(f"^{_CHILD_LABEL_WORD_PATTERN}$")
_CHILD_LABEL_PATTERN = f"^{_CHILD_LABEL_WORD_PATTERN}(?:-{_CHILD_LABEL_WORD_PATTERN})*$"
_CHILD_LABEL = re.compile(_CHILD_LABEL_PATTERN)
_MAX_CHILD_LABEL_LENGTH = 64
_EXECUTION_ISSUER = object()


def _child_egress_error(code: str, message: str) -> AdhelmError:
    return AdhelmError(
        code,
        message,
        next_action="Reconcile the subplan and compile a new plan before any retry.",
    )


class KernelExecution:
    """Kernel-owned child-egress journal for exactly one subplan attempt.

    A provider hierarchy is often several dependent requests behind one approved
    operation.  Splitting it across subplans would need one authorization per
    request and would still not order them, so the kernel keeps it as one
    subplan and journals each child request instead.

    Children are strictly sequential.  Every child appends and fsyncs a
    ``CHILD_APPLY_STARTED`` record before its one-use permit exists, and a
    ``CHILD_APPLY_RESULT`` once the adapter reports.  A start without a
    resolving result -- an interrupted process, an ambiguous transport, an
    adapter that never reported -- leaves that child unresolved, which blocks
    every later write until reconciliation, exactly like an unresolved
    subplan-level ``APPLY_STARTED``.
    """

    __slots__ = (
        "_active_index",
        "_active_scope",
        "_authorization",
        "_closed",
        "_idempotency_key",
        "_kernel",
        "_lock",
        "_next_index",
        "_plan",
        "_reports",
        "_results",
        "_subplan",
        "_child_claims",
    )

    def __init__(
        self,
        *,
        _issuer: object,
        kernel: SafetyKernel,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        idempotency_key: str,
    ) -> None:
        if _issuer is not _EXECUTION_ISSUER:
            raise TypeError("Only the safety kernel creates a kernel execution")
        self._kernel = kernel
        self._plan = plan
        self._subplan = subplan
        self._authorization = authorization
        self._idempotency_key = idempotency_key
        self._lock = Lock()
        self._next_index = 1
        self._active_index: int | None = None
        self._active_scope: _ChildEgressScope | None = None
        self._closed = False
        self._reports: dict[int, ApplyResponse] = {}
        self._child_claims: dict[int, bool] = {}
        self._results: dict[int, EffectStatus] = {}

    @property
    def subplan_digest(self) -> str:
        return self._subplan.subplan_digest

    @property
    def plan_digest(self) -> str:
        return self._plan.plan_digest

    @property
    def attempt_ref(self) -> str:
        """The subplan attempt reference every child permit is bound under."""

        return self._idempotency_key

    @property
    def expected_child_egress_count(self) -> int:
        return self._subplan.expected_child_egress_count

    @property
    def every_child_applied(self) -> bool:
        """True only when every approved child ran and proved it applied."""

        with self._lock:
            return sorted(self._results) == list(
                range(1, self._subplan.expected_child_egress_count + 1)
            ) and all(status is EffectStatus.APPLIED_VERIFIED for status in self._results.values())

    def begin_child(self, index: int, label: str) -> _ChildEgressScope:
        """Journal one child request and issue its one-use permit.

        ``index`` must equal the next expected child, starting at ``1``, and may
        never exceed the subplan's ``expected_child_egress_count``.  The permit
        is bound to this index, so a permit issued for one child can never be
        replayed as another.  The adapter reports the outcome with
        :meth:`report_child` before leaving the scope; anything else is recorded
        as ambiguous and leaves the child unresolved.

        The returned scope is a context manager rather than a generator so that
        the kernel can force it closed.  An adapter that enters a child scope and
        never exits it must not leave an active permit alive across the kernel's
        post-apply readback.
        """

        return _ChildEgressScope(self, index, label)

    def _open_child(
        self, scope: _ChildEgressScope, index: int, label: str
    ) -> tuple[str, ExitStack, EgressPermit]:
        checked_label = self._claim_child(scope, index, label)
        try:
            # Recheck the plan and authorization window before
            # journaling anything: an expired or revoked grant must not even
            # record an intent to send the next child request.
            self._kernel._check_mutation_window(self._plan, self._authorization)
            self._kernel._append_child_started(
                plan=self._plan,
                subplan=self._subplan,
                authorization=self._authorization,
                idempotency_ref=self._idempotency_key,
                child_index=index,
                child_label=checked_label,
            )
        except Exception:
            with self._lock:
                self._active_index = None
                self._active_scope = None
            raise
        stack = ExitStack()
        try:
            permit = stack.enter_context(
                _kernel_permit(
                    plan_digest=self._plan.plan_digest,
                    subplan_digest=self._subplan.subplan_digest,
                    attempt_ref=self._idempotency_key,
                    child_index=index,
                    check_current=lambda: self._kernel._check_mutation_window(
                        self._plan, self._authorization
                    ),
                )
            )
        except Exception:
            stack.close()
            self._finish_child(index, checked_label)
            raise
        return checked_label, stack, permit

    def report_child(self, index: int, response: ApplyResponse) -> None:
        """Report one child's provider acknowledgement inside its own scope."""

        with self._lock:
            if self._active_index != index:
                raise _child_egress_error(
                    "CHILD_EGRESS_NOT_ACTIVE",
                    "A child result belongs to its own open child egress scope.",
                )
            if index in self._reports:
                raise _child_egress_error(
                    "CHILD_EGRESS_ALREADY_REPORTED",
                    "A journaled child request reports exactly one result.",
                )
        checked = ApplyResponse.model_validate(response.model_dump(mode="python"))
        validate_data(checked.model_dump(mode="json"))
        with self._lock:
            self._reports[index] = checked

    def _claim_child(self, scope: _ChildEgressScope, index: int, label: str) -> str:
        """Claim the single child slot and publish the scope in one atomic step."""

        if (
            type(label) is not str
            or len(label) > _MAX_CHILD_LABEL_LENGTH
            or _CHILD_LABEL.fullmatch(label) is None
            or any(_CHILD_LABEL_WORD.fullmatch(word) is None for word in label.split("-"))
        ):
            raise _child_egress_error(
                "CHILD_EGRESS_LABEL_INVALID",
                "A child egress label names a step, not a provider resource.",
            )
        # Screening a label the kernel is about to journal is cheap and keeps the
        # closed shape from being the only barrier.
        validate_data(label)
        with self._lock:
            if self._closed:
                raise _child_egress_error(
                    "CHILD_EGRESS_CLOSED",
                    "This subplan attempt no longer accepts child egress.",
                )
            if self._active_index is not None:
                raise _child_egress_error(
                    "CHILD_EGRESS_ALREADY_ACTIVE",
                    "One subplan attempt issues one child permit at a time.",
                )
            if type(index) is not int or index != self._next_index:
                raise _child_egress_error(
                    "CHILD_EGRESS_OUT_OF_ORDER",
                    "Child requests are strictly sequential from the next expected index.",
                )
            if index > self._subplan.expected_child_egress_count:
                raise _child_egress_error(
                    "CHILD_EGRESS_COUNT_EXCEEDED",
                    "This subplan approved fewer child requests than were attempted.",
                )
            self._active_index = index
            # Published under the same lock as the claim: a force-close that
            # runs between the two would otherwise miss this scope.
            self._active_scope = scope
        return label

    def _finish_child(self, index: int, label: str) -> None:
        with self._lock:
            report = self._reports.get(index)
            claimed = self._child_claims.get(index, False)
            self._active_index = None
            self._active_scope = None
            self._next_index = index + 1
        status, evidence = _child_effect(report, claimed=claimed)
        with self._lock:
            self._results[index] = status
        self._kernel._append_child_result(
            plan=self._plan,
            subplan=self._subplan,
            authorization=self._authorization,
            idempotency_ref=self._idempotency_key,
            child_index=index,
            child_label=label,
            status=status,
            evidence_refs=evidence,
        )

    def _close(self) -> None:
        """Refuse further children and force any scope the adapter left open.

        This runs before the kernel's post-apply readback, so no child permit
        can survive the adapter's dispatch, and the interrupted child's
        ``CHILD_APPLY_RESULT`` is journaled in order ahead of ``APPLY_RESULT``.
        """

        with self._lock:
            self._closed = True
            scope = self._active_scope
        if scope is not None:
            scope.close()


class _ChildEgressScope:
    """One journaled child request: a durable start, one permit, one result.

    Closing is idempotent and kernel-callable, so the kernel can revoke an
    abandoned child permit and journal its terminal record itself.
    """

    __slots__ = ("_execution", "_index", "_label", "_closed", "_stack", "_permit")

    def __init__(self, execution: KernelExecution, index: int, label: str) -> None:
        self._execution = execution
        self._index = index
        self._label = label
        self._closed = False
        self._stack: ExitStack | None = None
        self._permit: EgressPermit | None = None

    def __enter__(self) -> EgressPermit:
        if self._closed or self._stack is not None:
            raise _child_egress_error(
                "CHILD_EGRESS_SCOPE_REUSED",
                "A journaled child scope is entered exactly once.",
            )
        label, stack, permit = self._execution._open_child(self, self._index, self._label)
        self._label = label
        with self._execution._lock:
            # A force-close between the claim and this assignment would have
            # found no stack to revoke, because the permit did not exist yet.
            # Publishing the stack and rechecking under one lock closes that
            # window: nothing may leave here holding a permit the kernel can no
            # longer revoke.
            abandoned = self._closed or self._execution._closed
            if not abandoned:
                self._stack = stack
                self._permit = permit
        if abandoned:
            self._closed = True
            try:
                stack.close()
            finally:
                self._execution._finish_child(self._index, label)
            raise _child_egress_error(
                "CHILD_EGRESS_CLOSED",
                "This subplan attempt no longer accepts child egress.",
            )
        return permit

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stack, self._stack = self._stack, None
        if stack is None:
            return
        permit, self._permit = self._permit, None
        claimed = permit is not None and _permit_claimed(permit)
        with self._execution._lock:
            self._execution._child_claims[self._index] = claimed
        try:
            # Revoke the permit before the terminal record, so nothing can claim
            # it while the result is being journaled.
            stack.close()
        finally:
            self._execution._finish_child(self._index, self._label)


def _child_effect(
    response: ApplyResponse | None, *, claimed: bool
) -> tuple[EffectStatus, tuple[str, ...]]:
    """Map one child acknowledgement onto a terminal child effect status.

    Only a confirmed and evidenced child resolves its durable start.  A child
    permit that was issued may already have reached the provider, so an
    adapter's own no-effect claim cannot clear its own pre-egress marker any
    more than it can at subplan level: a missing report, a confirmation with no
    evidence, an ambiguous transport, and a claimed no effect all stay
    unresolved until a subplan reconciliation settles the attempt.
    """

    if response is None:
        return EffectStatus.AMBIGUOUS_REMOTE_STATE, ()
    if response.outcome is ApplyOutcome.CONFIRMED and response.evidence_refs and claimed:
        return EffectStatus.APPLIED_VERIFIED, response.evidence_refs
    return EffectStatus.AMBIGUOUS_REMOTE_STATE, response.evidence_refs


@runtime_checkable
class ChildEgressAdapter(GuardedAdapter, Protocol):
    """A guarded adapter whose one approved operation is several requests.

    The kernel supplies a :class:`KernelExecution` instead of a single permit.
    Single-request adapters keep implementing ``apply_guarded`` only.
    """

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse: ...


class AttributableSettlement(StrictFrozenModel):
    """A provider-native proof that binds one exact attempt to its outcome.

    An ordinary reconciliation read shows a state; it does not show which
    request produced it.  Some providers expose a durable operation or
    idempotency resource that does bind the two.  This value carries that proof.
    """

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    subplan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_operation_ref: str = Field(min_length=1, max_length=256)
    settled_status: Literal[EffectStatus.APPLIED_VERIFIED, EffectStatus.FAILED_NO_EFFECT]
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_only(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_refs(refs, kind="evidence")


#: Providers whose adapters may supply an :class:`AttributableSettlement`.
#: It is empty, and adding a provider requires that provider documenting a
#: durable operation or idempotency resource binding a request to its outcome,
#: with reviewed evidence for it.  The gate exists because
#: ``AttributableSettlementProof`` is a ``runtime_checkable`` Protocol: without
#: it, any class that merely defines a method of that name would restore full
#: ``APPLIED_VERIFIED`` attribution to an attempt this kernel already recorded
#: as unattributable.
ATTRIBUTABLE_SETTLEMENT_PROVIDERS: frozenset[str] = frozenset()


@runtime_checkable
class AttributableSettlementProof(Protocol):
    """Optional adapter hook for a provider that can attribute an exact attempt.

    No installed adapter implements this today, and none may until its provider
    documents a durable operation or idempotency resource that binds a request
    to its outcome.  The hook exists so that adding such a provider is a
    provider-side change with its own evidence, rather than a reason to weaken
    the kernel's read-side containment for every other provider.

    Returning ``None`` -- including for an attempt the adapter does not
    recognise -- leaves the attempt unattributable.
    """

    def attributable_settlement(
        self, plan: SubPlan, *, attempt_ref: str
    ) -> AttributableSettlement | None: ...


def _attributable_settlement(
    adapter: Adapter, *, plan: CompositePlan, subplan: SubPlan, attempt_ref: str
) -> AttributableSettlement | None:
    """Accept a settlement proof only when it binds this exact attempt."""

    if (
        subplan.provider not in ATTRIBUTABLE_SETTLEMENT_PROVIDERS
        or adapter.provider not in ATTRIBUTABLE_SETTLEMENT_PROVIDERS
        or not isinstance(adapter, AttributableSettlementProof)
    ):
        return None
    try:
        offered = adapter.attributable_settlement(subplan, attempt_ref=attempt_ref)
    except Exception:
        return None
    if offered is None:
        return None
    try:
        settlement = _revalidate(offered, AttributableSettlement)
    except AdhelmError:
        return None
    if (
        settlement.plan_digest != plan.plan_digest
        or settlement.subplan_digest != subplan.subplan_digest
        or settlement.attempt_ref != attempt_ref
    ):
        return None
    return settlement


@runtime_checkable
class UserApprovalHost(Protocol):
    """Trusted host integration; no command-line flag implements this boundary.

    Proof methods must be bounded local verification and cannot read/acquire
    the ledger: the final proof runs while its append lock is held.
    """

    host_ref: str

    def verifies_direct_grant(self, grant: DirectGrant) -> bool: ...

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool: ...


@runtime_checkable
class KindBoundUserApprovalHost(UserApprovalHost, Protocol):
    """Receipt integrations bind installation and revocation independently.

    The generic parent protocol remains for existing development adapters. An
    installed receipt integration implements these distinct methods and must
    never let an install receipt prove revocation, or the reverse.
    """

    def verifies_policy_installation(self, policy: DelegationPolicy) -> bool: ...

    def verifies_policy_revocation(self, policy: DelegationPolicy) -> bool: ...


def _verifies_policy_intent(
    host: UserApprovalHost, policy: DelegationPolicy, *, revoke: bool
) -> bool:
    """A partial typed integration may never fall back to a generic proof."""
    missing = object()
    try:
        if host.host_ref != policy.configuration_host_ref:
            return False
        installation = getattr(host, "verifies_policy_installation", missing)
        revocation = getattr(host, "verifies_policy_revocation", missing)
        if installation is not missing or revocation is not missing:
            if not callable(installation) or not callable(revocation):
                return False
            verifier = revocation if revoke else installation
            return verifier(policy) is True
        return host.verifies_policy_configuration(policy) is True
    except Exception:
        return False


def _verifies_direct_intent(host: UserApprovalHost, grant: DirectGrant) -> bool:
    try:
        return host.host_ref == grant.host_ref and host.verifies_direct_grant(grant) is True
    except Exception:
        return False


class SubplanResult(StrictFrozenModel):
    provider: str
    subplan_digest: str
    status: EffectStatus
    resource_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    recovery_action: str | None = None
    diagnostic_code: str | None = None


class ExecutionResult(StrictFrozenModel):
    plan_digest: str
    authorization_id: str
    results: tuple[SubplanResult, ...]
    consumed: bool
    recovery_required: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


_SAFE_REF = re.compile(r"^(?:opaque|evidence):sha256:[0-9a-f]{64}$")


def _validated_refs(refs: tuple[str, ...], *, kind: str) -> tuple[str, ...]:
    expected_prefix = f"{kind}:sha256:"
    if len(set(refs)) != len(refs) or any(
        not ref.startswith(expected_prefix) or _SAFE_REF.fullmatch(ref) is None for ref in refs
    ):
        raise ValueError("references must be unique opaque SHA-256 references")
    return refs


#: Operation kinds that observe without writing.  Every kind in the current
#: contract mutates remote state, so this set is empty on purpose: the uniform
#: write rule below then covers every provider and every operation.  A future
#: read-only kind is added here and nowhere else.
READ_ONLY_OPERATIONS: frozenset[OperationKind] = frozenset()


def _is_mutating(subplan: SubPlan) -> bool:
    """Report whether this subplan can change remote state."""

    return subplan.operation not in READ_ONLY_OPERATIONS


def _requires_guarded_egress(subplan: SubPlan) -> bool:
    """Every mutating subplan writes through a permit-bound guarded adapter.

    There is no provider or operation allowlist.  An adapter that cannot accept
    a kernel-issued one-use permit cannot execute a mutation at all, so no
    future CREATE, ACTIVATE, or MAINTAIN operation needs a bespoke kernel branch
    to obtain the same containment.
    """

    return _is_mutating(subplan)


def _requires_attributable_confirmation(subplan: SubPlan) -> bool:
    """Refuse to attribute a non-confirmed mutation to any later read.

    A post-apply read after a non-confirmed acknowledgement cannot prove what
    this invocation caused, in either direction.  A read at the target state
    cannot show that this attempt produced it, and a read still at the approved
    baseline cannot show that the request never reached the provider: an
    eventually consistent list, a lagging replica, or a lost acknowledgement of
    a write that did land all look exactly like that.  The kernel therefore
    reports ``AMBIGUOUS_REMOTE_STATE`` and never derives a no-effect conclusion
    from the adapter's own flag plus the adapter's own read.  Only
    ``reconcile_run``, with a provider-aware settlement proof, may settle such
    an attempt.

    That gap is a property of remote writes, not of Meta or GA4, so it applies
    to every mutating subplan.
    """

    return _is_mutating(subplan)


def _unattributable_attempt_state(
    history: list[dict[str, Any]],
    *,
    plan: CompositePlan,
    authorization: Authorization,
    subplan: SubPlan,
) -> Literal["NO_ATTEMPT", "UNSETTLED", "VERIFIED"]:
    """Classify the exact retained attempt for any mutating subplan.

    A later provider read has no reviewed attribution to the original request.
    In particular, the pre-egress ``APPLY_STARTED`` record cannot show whether a
    process died before or after provider egress. The existing ledger facts bind
    the run, plan, grant and subplan precisely, so this check needs no new
    payload field and never treats one attempt's result as another's.

    This was originally the Meta PAUSE containment. The gap it closes is a
    property of remote writes rather than of one provider, and the uniform write
    rule now converts every non-confirmed acknowledgement to ambiguity for every
    provider, so the same read-side containment applies to every mutating
    subplan. The kernel can therefore durably establish only one exact
    ``APPLIED_VERIFIED`` result followed by its matching verification result;
    it has no no-egress terminal marker for any provider. Every other result,
    duplicate or out-of-order event, or missing result after an exact start is
    ``UNSETTLED`` and must not be upgraded by a later read. A retained verified
    result is distinct: reconciliation may only repeat its exact after-state
    conclusion, never rewrite it as no effect because a later read returned to
    baseline.
    """

    if not _is_mutating(subplan):
        return "NO_ATTEMPT"
    scan = _attempt_events(history, plan=plan, authorization=authorization, subplan=subplan)
    if scan is None:
        return "UNSETTLED"
    start_indexes, result_statuses, verification_statuses = scan
    if not start_indexes:
        return "NO_ATTEMPT" if not result_statuses and not verification_statuses else "UNSETTLED"
    if (
        len(start_indexes) == len(result_statuses) == len(verification_statuses) == 1
        and start_indexes[0] < result_statuses[0][0] < verification_statuses[0][0]
        and result_statuses[0][1] == EffectStatus.APPLIED_VERIFIED.value
        and verification_statuses[0][1] == EffectStatus.APPLIED_VERIFIED.value
        and verification_statuses[0][2]
    ):
        return "VERIFIED"
    return "UNSETTLED"


def _attempt_events(
    history: list[dict[str, Any]],
    *,
    plan: CompositePlan,
    authorization: Authorization,
    subplan: SubPlan,
) -> tuple[list[int], list[tuple[int, str]], list[tuple[int, str, bool]]] | None:
    """Collect this kernel's own durable events for one exact attempt.

    Returns ``None`` when a row identifies the attempt but contradicts its
    retained plan or carries a malformed terminal status. Such a row must not
    disappear from the scan or become an unrelated event merely because its
    digests are valid-shaped.
    """

    attempt = digest({"plan": plan.plan_digest, "subplan": subplan.subplan_digest})
    start_indexes: list[int] = []
    result_statuses: list[tuple[int, str]] = []
    verification_statuses: list[tuple[int, str, bool]] = []
    for index, record in enumerate(history):
        payload = record.get("payload")
        if type(payload) is not dict:
            continue
        matches_attempt = (
            record.get("record_kind") == "CHANGE_EVENT"
            and record.get("provider") == subplan.provider
            and record.get("account_ref") == subplan.account.account_ref
            and record.get("run_id") == plan.plan_id
            and payload.get("operation") == subplan.operation.value
            and payload.get("resource_ref") == subplan.subplan_id
            and payload.get("plan_digest") == plan.plan_digest
            and payload.get("subplan_digest") == subplan.subplan_digest
            and payload.get("approval_ref") == authorization.authorization_id
            and payload.get("idempotency_ref") == attempt
        )
        if not matches_attempt:
            continue
        event_type = payload.get("event_type")
        if event_type not in {"APPLY_STARTED", "APPLY_RESULT", "VERIFICATION_RESULT"}:
            continue
        if (
            payload.get("before_digest") != subplan.before.fingerprint
            or payload.get("after_digest") != subplan.after.fingerprint
        ):
            return None
        if event_type == "APPLY_STARTED":
            start_indexes.append(index)
        else:
            status = payload.get("effect_status")
            if not isinstance(status, str):
                return None
            if event_type == "APPLY_RESULT":
                result_statuses.append((index, status))
            else:
                evidence_refs = payload.get("evidence_refs")
                verification_statuses.append(
                    (index, status, type(evidence_refs) is list and bool(evidence_refs))
                )
    return start_indexes, result_statuses, verification_statuses


def _attempt_locally_terminated(
    history: list[dict[str, Any]],
    *,
    plan: CompositePlan,
    authorization: Authorization,
    subplan: SubPlan,
) -> bool:
    """Report whether this kernel completed and journaled its own attempt protocol.

    An attempt with one ordered ``APPLY_STARTED``, ``APPLY_RESULT`` and
    ``VERIFICATION_RESULT`` ran to the end of the kernel's own sequence: the
    dispatch returned or raised, the post-apply readback was taken, and both
    terminal rows were durably appended. Only attribution is missing, and no
    later read can ever supply it.

    An attempt without that pair is different in kind. The process died
    somewhere inside the write path, so the kernel does not know whether the
    adapter dispatched at all, and a later provider read is not enough to
    release it. That case needs an explicitly authorized operator recovery.
    """

    scan = _attempt_events(history, plan=plan, authorization=authorization, subplan=subplan)
    if scan is None:
        return False
    start_indexes, result_statuses, verification_statuses = scan
    return (
        len(start_indexes) == len(result_statuses) == len(verification_statuses) == 1
        and start_indexes[0] < result_statuses[0][0] < verification_statuses[0][0]
    )


def _attempt_start_record(
    history: list[dict[str, Any]],
    *,
    plan: CompositePlan,
    authorization: Authorization,
    subplan: SubPlan,
) -> dict[str, Any] | None:
    """Return the exact durable ``APPLY_STARTED`` record for one attempt."""

    attempt = digest({"plan": plan.plan_digest, "subplan": subplan.subplan_digest})
    matching = [
        record
        for record in history
        if record.get("record_kind") == "CHANGE_EVENT"
        and record.get("provider") == subplan.provider
        and record.get("account_ref") == subplan.account.account_ref
        and record.get("run_id") == plan.plan_id
        and type(record.get("payload")) is dict
        and record["payload"].get("event_type") == "APPLY_STARTED"
        and record["payload"].get("plan_digest") == plan.plan_digest
        and record["payload"].get("subplan_digest") == subplan.subplan_digest
        and record["payload"].get("approval_ref") == authorization.authorization_id
        and record["payload"].get("idempotency_ref") == attempt
        and record["payload"].get("resource_ref") == subplan.subplan_id
    ]
    return matching[0] if len(matching) == 1 else None


def _required_start_digest(
    history: list[dict[str, Any]],
    *,
    plan: CompositePlan,
    authorization: Authorization,
    subplan: SubPlan,
) -> str:
    start = _attempt_start_record(history, plan=plan, authorization=authorization, subplan=subplan)
    if start is None:
        raise AdhelmError(
            "ATTEMPT_RECOVERY_START_MISSING",
            "Recovery requires the exact durable start of one unresolved attempt.",
        )
    return cast(str, start["record_digest"])


def _observed_relation(subplan: SubPlan, state: RemoteState) -> str:
    if state.fingerprint == subplan.after.fingerprint and state.revision == subplan.after.revision:
        return "AT_TARGET"
    if (
        state.fingerprint == subplan.before.fingerprint
        and state.revision == subplan.before.revision
    ):
        return "AT_BASELINE"
    return "OTHER"


_RECOVERY_DISPOSITION = {
    "AT_BASELINE": "RECOVERED_AT_BASELINE",
    "AT_TARGET": "RECOVERED_AT_TARGET",
    "OTHER": "RECOVERED_DIVERGED",
}


def attempt_recovery_subject(
    plan: CompositePlan, subplan: SubPlan, *, started_record_digest: str
) -> str:
    """Digest the exact unresolved attempt an operator recovery grant must name.

    The subject binds the composite, the subplan, the derived attempt reference,
    and the durable ``APPLY_STARTED`` record itself, so a grant issued for one
    lost acknowledgement can never release a different one.
    """

    return digest(
        {
            "schema_version": 1,
            "subject": "ATTEMPT_RECOVERY",
            "plan_digest": plan.plan_digest,
            "subplan_digest": subplan.subplan_digest,
            "attempt_ref": digest({"plan": plan.plan_digest, "subplan": subplan.subplan_digest}),
            "started_record_digest": started_record_digest,
        }
    )


class SafetyKernel:
    """Executes a compiled composite only after deterministic checks pass."""

    def __init__(self, root: Path, ledger: Any, *, clock: Any = _now) -> None:
        self.root = root
        self.ledger = ledger
        self.clock = clock
        self._lock_dir = root / "coordinator-locks"
        self._lock_dir.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        plan: CompositePlan,
        authorization: Authorization,
        adapters: dict[str, Adapter],
    ) -> ExecutionResult:
        # The release policy is a property of the build: refuse before plan
        # validation, any lock, any journal write, or any adapter call.
        release_policy.require_writes_released()
        plan = _revalidate(plan, CompositePlan)
        authorization = _revalidate_authorization(authorization)
        now = self.clock()
        self._check_expiry(plan, authorization, now)

        with self._coordinator_locks(plan, authorization):
            # Waiting on another process must not make an expired plan executable.
            now = self.clock()
            self._check_expiry(plan, authorization, now)
            self._reject_unresolved_starts()
            self._ensure_durable_authorization(plan, authorization)
            self._authorize_unconsumed(plan, authorization, now)
            self._authorize_policy(plan, authorization, now)
            # A final locked read closes the local race between preflight and egress.
            self._preflight_all(plan, adapters)
            return self._apply_ordered(plan, authorization, adapters)

    def _check_mutation_window(self, plan: CompositePlan, authorization: Authorization) -> None:
        self._check_expiry(plan, authorization, self.clock())

    def record_direct_grant(
        self, plan: CompositePlan, grant: DirectGrant, approval_host: UserApprovalHost
    ) -> None:
        """Durably record a host-verified direct grant before it can be used."""
        plan = _revalidate(plan, CompositePlan)
        grant = _revalidate(grant, DirectGrant)
        self._check_expiry(plan, grant, self.clock())
        if not _verifies_direct_intent(approval_host, grant):
            raise AdhelmError(
                "USER_APPROVAL_HOST_REQUIRED",
                "The configured host could not prove this user-originated approval.",
                exit_code=ExitCode.AUTHORIZATION,
            )

        def before_append() -> None:
            self._check_expiry(plan, grant, self.clock())
            if not _verifies_direct_intent(approval_host, grant):
                raise AdhelmError(
                    "USER_APPROVAL_HOST_REQUIRED",
                    "Direct approval proof is no longer current at its recording handoff.",
                    exit_code=ExitCode.AUTHORIZATION,
                )

        self._append_authorization_event(
            authorization_id=grant.authorization_id,
            actor_ref=grant.grantor_ref,
            plan_ref=plan.plan_id,
            plan_digest=plan.plan_digest,
            subplan_digest=None,
            evidence_refs=[grant.user_interaction_ref],
            policy_id=None,
            authorization_digest=digest(grant.model_dump(mode="json")),
            before_append=before_append,
        )

    def install_delegation_policy(
        self, policy: DelegationPolicy, approval_host: UserApprovalHost
    ) -> None:
        """Record only policy configuration proven by the configured approval host."""
        policy = _revalidate(policy, DelegationPolicy)
        self._check_policy_installation_time(policy)
        if not _verifies_policy_intent(approval_host, policy, revoke=False):
            raise self._policy_proof_error()
        with self._policy_lock(policy.policy_id):
            self._record_policy_transition_locked(policy, approval_host, revoke=False)

    def revoke_delegation_policy(
        self, policy: DelegationPolicy, approval_host: UserApprovalHost
    ) -> None:
        """Revoke the exact latest installed policy, with no provider side effect."""
        policy = _revalidate(policy, DelegationPolicy)
        if not _verifies_policy_intent(approval_host, policy, revoke=True):
            raise self._policy_proof_error()
        with self._policy_lock(policy.policy_id):
            self._record_policy_transition_locked(policy, approval_host, revoke=True)

    @staticmethod
    def _policy_proof_error() -> AdhelmError:
        return AdhelmError(
            "USER_APPROVAL_HOST_REQUIRED",
            "The configured host could not prove this exact current policy operation.",
            exit_code=ExitCode.AUTHORIZATION,
        )

    def _policy_authorization_events(self, policy_id: str) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in self._records()
            if record.get("record_kind") == "CHANGE_EVENT"
            and record["payload"].get("resource_type") == "AUTHORIZATION"
            and record["payload"].get("approval_ref") == f"policy/{policy_id}"
            and record["payload"].get("event_type") in {"APPROVAL_GRANTED", "RECOVERY_RESULT"}
        ]

    def _require_policy_transition(self, policy: DelegationPolicy, *, revoke: bool) -> None:
        """Check canonical policy state while the caller holds its policy lock."""
        if not revoke:
            self._check_policy_installation_time(policy)
            versions = self._policy_versions(policy.policy_id)
            if versions and policy.policy_version <= max(versions):
                raise AdhelmError(
                    "DELEGATION_POLICY_VERSION_REPLAY",
                    "A policy update must have a newer version than durable installations.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            return
        events = self._policy_authorization_events(policy.policy_id)
        if not events or events[-1].get("event_type") != "APPROVAL_GRANTED":
            raise AdhelmError(
                "DELEGATION_NOT_INSTALLED",
                "Revocation requires a currently installed policy that has not been revoked.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        if events[-1].get("plan_digest") != policy.policy_digest or events[-1].get(
            "plan_ref"
        ) != self._policy_plan_ref(policy):
            raise AdhelmError(
                "DELEGATION_POLICY_DRIFT",
                "Revocation must name the exact latest installed policy version and digest.",
                exit_code=ExitCode.AUTHORIZATION,
            )

    def _record_policy_transition_locked(
        self, policy: DelegationPolicy, approval_host: UserApprovalHost, *, revoke: bool
    ) -> None:
        """Internal handoff; receipt acceptance holds this lock across its claim."""
        policy = _revalidate(policy, DelegationPolicy)
        self._require_policy_transition(policy, revoke=revoke)
        if not _verifies_policy_intent(approval_host, policy, revoke=revoke):
            raise self._policy_proof_error()

        def before_append() -> None:
            if not revoke:
                self._check_policy_installation_time(policy)
            if not _verifies_policy_intent(approval_host, policy, revoke=revoke):
                raise self._policy_proof_error()

        self._append_authorization_event(
            authorization_id=self._policy_ref(policy),
            actor_ref=policy.grantor_ref,
            plan_ref=self._policy_plan_ref(policy),
            plan_digest=None if revoke else policy.policy_digest,
            subplan_digest=None,
            evidence_refs=[policy.configuration_interaction_ref],
            policy_id=policy.policy_id,
            event_type="RECOVERY_RESULT" if revoke else "APPROVAL_GRANTED",
            operation="PAUSE" if revoke else "UPDATE",
            before_append=before_append,
        )

    def _check_policy_installation_time(self, policy: DelegationPolicy) -> None:
        # A future effective_from is a valid scheduled policy; execution has
        # its separate current-effective gate. Expired/revoked policies cannot
        # be newly installed, even with a still-current review receipt.
        if policy.expires_at <= self.clock() or policy.revoked_at is not None:
            raise AdhelmError(
                "DELEGATION_POLICY_EXPIRED",
                "Policy installation requires an unexpired, unrevoked policy.",
                exit_code=ExitCode.AUTHORIZATION,
            )

    @staticmethod
    def _policy_ref(policy: DelegationPolicy) -> str:
        return f"policy/{policy.policy_id}"

    @staticmethod
    def _policy_plan_ref(policy: DelegationPolicy) -> str:
        return f"policy/{policy.policy_id}/version/{policy.policy_version}"

    def _policy_versions(self, policy_id: str) -> list[int]:
        prefix = f"policy/{policy_id}/version/"
        versions: list[int] = []
        for payload in self._policy_authorization_events(policy_id):
            plan_ref = payload.get("plan_ref")
            if payload.get("event_type") != "APPROVAL_GRANTED" or not isinstance(plan_ref, str):
                continue
            if plan_ref.startswith(prefix):
                suffix = plan_ref.removeprefix(prefix)
                if suffix.isdecimal():
                    versions.append(int(suffix))
        return versions

    def _ensure_durable_authorization(
        self, plan: CompositePlan, authorization: Authorization
    ) -> None:
        records = self._records()
        if isinstance(authorization, DirectGrant):
            if not any(
                payload.get("event_type") == "APPROVAL_GRANTED"
                and payload.get("approval_ref") == authorization.authorization_id
                and payload.get("plan_digest") == plan.plan_digest
                and payload.get("authorization_digest")
                == digest(authorization.model_dump(mode="json"))
                for payload in (record.get("payload", {}) for record in records)
            ):
                raise AdhelmError(
                    "USER_APPROVAL_HOST_REQUIRED",
                    "No durable user-host approval record covers this plan.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            return
        policy_ref = self._policy_ref(authorization.policy)
        matching = [
            record.get("payload", {})
            for record in records
            if record.get("payload", {}).get("approval_ref") == policy_ref
        ]
        if not matching or matching[-1].get("event_type") != "APPROVAL_GRANTED":
            raise AdhelmError(
                "DELEGATION_NOT_INSTALLED",
                "The current delegation policy is not durably installed.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        if matching[-1].get("plan_digest") != authorization.policy.policy_digest:
            raise AdhelmError(
                "DELEGATION_POLICY_DRIFT",
                "The supplied policy differs from the durable installed policy.",
                exit_code=ExitCode.AUTHORIZATION,
            )

    def _check_expiry(
        self, plan: CompositePlan, authorization: Authorization, now: datetime
    ) -> None:
        if plan.expires_at <= now or any(item.expires_at <= now for item in plan.subplans):
            raise AdhelmError(
                "PLAN_EXPIRED", "The compiled plan has expired.", exit_code=ExitCode.AUTHORIZATION
            )
        if isinstance(authorization, DirectGrant):
            if authorization.expires_at <= now:
                raise AdhelmError(
                    "AUTHORIZATION_EXPIRED",
                    "The direct authorization has expired.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if (
                authorization.plan_digest != plan.plan_digest
                or authorization.subplan_digests != plan.subplan_digests
            ):
                raise AdhelmError(
                    "AUTHORIZATION_PLAN_MISMATCH",
                    "The direct grant does not bind this exact composite.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
        elif (
            authorization.policy.expires_at <= now
            or authorization.policy.effective_from > now
            or (
                authorization.policy.revoked_at is not None
                and authorization.policy.revoked_at <= now
            )
        ):
            raise AdhelmError(
                "DELEGATION_NOT_CURRENT",
                "The delegation policy is not currently effective.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        elif (
            authorization.plan_digest is not None and authorization.plan_digest != plan.plan_digest
        ):
            raise AdhelmError(
                "AUTHORIZATION_PLAN_MISMATCH",
                "The delegated grant binds another plan.",
                exit_code=ExitCode.AUTHORIZATION,
            )

    def _preflight_all(self, plan: CompositePlan, adapters: dict[str, Adapter]) -> None:
        for subplan in plan.subplans:
            adapter = adapters.get(subplan.provider)
            if adapter is None or adapter.provider != subplan.provider:
                raise AdhelmError(
                    "ADAPTER_NOT_CONFIGURED", "A required provider adapter is not configured."
                )
            self._preflight_one(subplan, adapter)

    def _preflight_one(self, subplan: SubPlan, adapter: Adapter) -> None:
        if _requires_guarded_egress(subplan) and not isinstance(adapter, GuardedAdapter):
            raise AdhelmError(
                "ADAPTER_PERMIT_REQUIRED",
                "This mutation requires the guarded adapter interface.",
            )
        if subplan.expected_child_egress_count > 1 and not isinstance(adapter, ChildEgressAdapter):
            raise AdhelmError(
                "ADAPTER_CHILD_EGRESS_REQUIRED",
                "A multi-step subplan requires the journaled child egress interface.",
            )
        capability = adapter.capabilities(subplan)
        if (
            not capability.writes_enabled
            or capability.provider_api_version != subplan.provider_api_version
            or capability.capability_version != subplan.capability_version
            or subplan.operation not in capability.supported_operations
        ):
            raise AdhelmError(
                "UNSUPPORTED_LIVE_CAPABILITY",
                "The installed adapter cannot execute this exact capability.",
            )
        identity = adapter.read_identity(subplan.account.account_ref)
        self._check_identity(subplan.account, identity)
        state = adapter.read_state(subplan)
        if (
            state.fingerprint != subplan.before.fingerprint
            or state.revision != subplan.before.revision
        ):
            raise AdhelmError("REMOTE_DRIFT", "Remote state no longer matches the approved plan.")

    @staticmethod
    def _check_identity(
        expected: AccountIdentity | OpenAIAccountKeyIdentity,
        actual: RemoteIdentity | RemoteOpenAIAccountKeyIdentity,
    ) -> None:
        matches = False
        if isinstance(expected, OpenAIAccountKeyIdentity) and isinstance(
            actual, RemoteOpenAIAccountKeyIdentity
        ):
            # Revalidate the adapter boundary too: an existing model object
            # can contain values inserted through model_copy/model_construct.
            actual = _revalidate(actual, RemoteOpenAIAccountKeyIdentity)
            matches = (
                expected.stable_account_id == actual.stable_account_id
                and expected.auth_profile_ref == actual.auth_profile_ref
                and expected.credential_binding_ref == actual.credential_binding_ref
            )
        elif isinstance(expected, AccountIdentity) and isinstance(actual, RemoteIdentity):
            actual = _revalidate(actual, RemoteIdentity)
            matches = (
                expected.stable_account_id == actual.stable_account_id
                and expected.auth_profile_ref == actual.auth_profile_ref
                and expected.principal_ref == actual.principal_ref
                and expected.client_application_ref == actual.client_application_ref
                and expected.scopes == actual.scopes
            )
        if not matches:
            raise AdhelmError(
                "ACCOUNT_IDENTITY_DRIFT",
                "The active provider identity differs from the approved identity.",
            )

    def _records(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.ledger.records("changes"))

    def _reject_unresolved_starts(self) -> None:
        # Report the innermost unresolved state first: a child request that
        # never recorded its result is more specific than the subplan-level
        # start that contains it.
        if self.ledger.unresolved_child_starts():
            raise AdhelmError(
                "UNRESOLVED_CHILD_APPLY",
                "A prior journaled child request requires reconciliation before another write.",
            )
        if self.ledger.unresolved_starts():
            raise AdhelmError(
                "UNRESOLVED_APPLY",
                "A prior provider mutation requires reconciliation before another write.",
            )
        if self.ledger.unresolved_runs():
            raise AdhelmError(
                "COMPOSITE_RECONCILIATION_REQUIRED",
                "A prior composite requires reconciliation of every selected provider.",
            )

    def _authorize_unconsumed(
        self, plan: CompositePlan, authorization: Authorization, now: datetime
    ) -> None:
        del now
        authorization_id = authorization.authorization_id
        for record in self._records():
            payload = record.get("payload", {})
            if (
                payload.get("event_type") == "APPLY_STARTED"
                and payload.get("approval_ref") == authorization_id
            ):
                raise AdhelmError(
                    "AUTHORIZATION_ALREADY_CONSUMED",
                    "This one-use authorization was already started.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if (
                payload.get("event_type") == "APPLY_STARTED"
                and payload.get("plan_digest") == plan.plan_digest
            ):
                raise AdhelmError(
                    "PLAN_ALREADY_STARTED",
                    "This composite was already started; reconcile and compile a new plan.",
                    exit_code=ExitCode.AUTHORIZATION,
                )

    def _authorize_policy(
        self, plan: CompositePlan, authorization: Authorization, now: datetime
    ) -> None:
        if not isinstance(authorization, DelegatedGrant):
            return
        policy = authorization.policy
        if authorization.issuing_agent_or_host_ref != policy.authorized_agent_or_host_ref:
            raise AdhelmError(
                "DELEGATION_SOURCE_MISMATCH",
                "The caller is not the configured delegated agent or host.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        limits = {
            item.currency: item.maximum_minor_units for item in policy.maximum_change_per_operation
        }
        cumulative = {
            item.currency: item.maximum_minor_units for item in policy.cumulative_change_bounds
        }
        requested: dict[str, int] = {}
        for subplan in plan.subplans:
            if subplan.account.stable_account_id not in policy.account_stable_ids:
                raise AdhelmError(
                    "DELEGATION_ACCOUNT_DENIED",
                    "The policy does not cover this stable account.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if subplan.operation not in policy.allowed_operations:
                raise AdhelmError(
                    "DELEGATION_OPERATION_DENIED",
                    "The policy does not cover this operation.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if any(target.host not in policy.allowed_egress_hosts for target in subplan.egress):
                raise AdhelmError(
                    "DELEGATION_EGRESS_DENIED",
                    "The policy does not allow this egress target.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            amount = subplan.max_spend.minor_units
            currency = subplan.max_spend.currency
            if currency not in limits or amount > limits[currency]:
                raise AdhelmError(
                    "DELEGATION_OPERATION_LIMIT",
                    "The operation exceeds the policy limit.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            requested[currency] = requested.get(currency, 0) + amount
        consumed = self._policy_reservations(policy.policy_id)
        for currency, amount in requested.items():
            if (
                currency not in cumulative
                or consumed.get(currency, 0) + amount > cumulative[currency]
            ):
                raise AdhelmError(
                    "DELEGATION_CUMULATIVE_LIMIT",
                    "The policy cumulative limit would be exceeded.",
                    exit_code=ExitCode.AUTHORIZATION,
                )

    def _policy_reservations(self, policy_id: str) -> dict[str, int]:
        totals: dict[str, int] = {}
        for record in self._records():
            payload = record.get("payload", {})
            if (
                payload.get("event_type") != "APPLY_STARTED"
                or payload.get("policy_id") != policy_id
            ):
                continue
            for item in payload.get("policy_reservation", []):
                currency = item.get("currency")
                amount = item.get("minor_units")
                if isinstance(currency, str) and isinstance(amount, int) and amount >= 0:
                    totals[currency] = totals.get(currency, 0) + amount
        return totals

    def _apply_ordered(
        self, plan: CompositePlan, authorization: Authorization, adapters: dict[str, Adapter]
    ) -> ExecutionResult:
        results: list[SubplanResult] = []
        reservation = self._reservation(plan) if isinstance(authorization, DelegatedGrant) else ()
        for index, subplan in enumerate(plan.subplans):
            adapter = adapters[subplan.provider]
            # Each provider gets a fresh expiry and drift check immediately before
            # its durable APPLY_STARTED record, after earlier providers may mutate.
            try:
                self._preflight_one(subplan, adapter)
                self._check_mutation_window(plan, authorization)
            except Exception as exc:
                if not results:
                    raise
                code = exc.code if isinstance(exc, AdhelmError) else "PROVIDER_PREFLIGHT_FAILED"
                results.extend(
                    SubplanResult(
                        provider=item.provider,
                        subplan_digest=item.subplan_digest,
                        status=EffectStatus.NOT_STARTED,
                        diagnostic_code=code if index == following else "PRIOR_PROVIDER_STOPPED",
                        recovery_action="Reconcile every selected provider and compile a new plan.",
                    )
                    for following, item in enumerate(plan.subplans[index:], start=index)
                )
                break
            if index == 0:
                self._append_run("RUN_STARTED", plan, authorization)
            idempotency_key = digest({"plan": plan.plan_digest, "subplan": subplan.subplan_digest})
            self._append_change(
                event_type="APPLY_STARTED",
                subplan=subplan,
                plan=plan,
                authorization=authorization,
                effect_status="NO_REMOTE_WRITE",
                idempotency_ref=idempotency_key,
                extra={
                    "policy_id": authorization.policy.policy_id
                    if isinstance(authorization, DelegatedGrant)
                    else None,
                    "policy_reservation": list(reservation) if index == 0 else [],
                },
            )
            # The composite-level APPLY_STARTED above always precedes the first
            # egress, including the first journaled child of a multi-step
            # subplan. Permits never exist until their matching append has
            # completed its durable fsync.
            result = self._apply_one(plan, subplan, authorization, adapter, idempotency_key)
            results.append(result)
            if result.status is not EffectStatus.APPLIED_VERIFIED:
                results.extend(
                    SubplanResult(
                        provider=item.provider,
                        subplan_digest=item.subplan_digest,
                        status=EffectStatus.NOT_STARTED,
                        recovery_action="Reconcile every selected provider and compile a new plan.",
                    )
                    for item in plan.subplans[index + 1 :]
                )
                break
        complete = all(r.status is EffectStatus.APPLIED_VERIFIED for r in results)
        if complete:
            self._append_run("RUN_VERIFIED", plan, authorization)
        else:
            results = [
                r.model_copy(
                    update={
                        "recovery_action": "Reconcile all selected providers; compile a new plan."
                    }
                )
                for r in results
            ]
        return ExecutionResult(
            plan_digest=plan.plan_digest,
            authorization_id=authorization.authorization_id,
            results=tuple(results),
            consumed=True,
            recovery_required=not complete,
        )

    def _append_run(
        self,
        event_type: str,
        plan: CompositePlan,
        authorization: Authorization,
        *,
        reconciled_subplan_digest: str | None = None,
        effect_status: str | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> None:
        self.ledger.append(
            "changes",
            "COMPOSITE_RUN",
            {
                "event_type": event_type,
                "plan_digest": plan.plan_digest,
                "approval_ref": authorization.authorization_id,
                "authorization_digest": digest(authorization),
                "participants": [
                    {
                        "provider": p.provider,
                        "account_ref": p.account.account_ref,
                        "resource_ref": p.subplan_id,
                        "subplan_digest": p.subplan_digest,
                    }
                    for p in plan.subplans
                ],
                "reconciled_subplan_digest": reconciled_subplan_digest,
                "effect_status": effect_status,
                "evidence_refs": list(evidence_refs),
            },
            run_id=plan.plan_id,
            source="LOCAL_CLI",
        )

    def record_attempt_recovery(
        self,
        plan: CompositePlan,
        authorization: Authorization,
        recovery: Authorization,
        approval_host: UserApprovalHost,
        adapters: dict[str, Adapter],
    ) -> ExecutionResult:
        """Record the current state of an unresolved attempt under its own grant.

        This is the exit for an attempt this kernel never finished journaling --
        a process that died inside the write path -- where no later read can
        show whether the request was ever dispatched.  It is deliberately not a
        reconciliation: it settles nothing, attributes nothing, and never
        upgrades the attempt's effect status.  It observes the current state
        through a fresh identity check and read, records that observation with
        an ``UNKNOWN`` actor and a bounded observation interval, and releases
        the write barrier so one lost acknowledgement cannot refuse every later
        plan on every provider forever.

        ``recovery`` is a separate grant whose subject is
        :func:`attempt_recovery_subject` for the exact unresolved attempts, so a
        grant issued for one lost acknowledgement can never release another. It
        is one-use: a grant already recorded here is refused.  A delegated grant
        must run under a policy that names ``OperationKind.RECOVER`` explicitly.
        """

        release_policy.require_writes_released()
        plan = _revalidate(plan, CompositePlan)
        authorization = _revalidate_authorization(authorization)
        recovery = _revalidate_authorization(recovery)
        now = self.clock()
        with self._coordinator_locks(plan, authorization):
            history = self._records()
            pending: list[tuple[SubPlan, str]] = []
            for subplan in plan.subplans:
                start = _attempt_start_record(
                    history, plan=plan, authorization=authorization, subplan=subplan
                )
                if start is None:
                    continue
                if any(
                    record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
                    and record["payload"]["started_record_digest"] == start["record_digest"]
                    for record in history
                ):
                    continue
                pending.append((subplan, cast(str, start["record_digest"])))
            if not pending:
                raise AdhelmError(
                    "ATTEMPT_RECOVERY_NOT_REQUIRED",
                    "No unrecorded started attempt of this exact plan needs recovery.",
                )
            subjects = tuple(
                attempt_recovery_subject(plan, subplan, started_record_digest=started)
                for subplan, started in pending
            )
            self._authorize_attempt_recovery(recovery, subjects, approval_host, now)
            results: list[SubplanResult] = []
            for (subplan, _started), subject in zip(pending, subjects, strict=True):
                adapter = adapters.get(subplan.provider)
                if adapter is None or adapter.provider != subplan.provider:
                    raise AdhelmError(
                        "ADAPTER_NOT_CONFIGURED", "A required provider adapter is not configured."
                    )
                # A fresh identity and read under the coordinator lock: the
                # record must describe the state now, not a retained snapshot.
                self._check_identity(
                    subplan.account, adapter.read_identity(subplan.account.account_ref)
                )
                observed = _revalidate(adapter.read_state(subplan), RemoteState)
                self._append_attempt_recovery(
                    plan=plan,
                    subplan=subplan,
                    authorization=authorization,
                    history=history,
                    observed=observed,
                    evidence_refs=(_recovery_evidence(subject, observed),),
                    source="OPERATOR_ATTESTED_OBSERVATION",
                    recovery=recovery,
                    subject_digest=subject,
                )
                results.append(
                    SubplanResult(
                        provider=subplan.provider,
                        subplan_digest=subplan.subplan_digest,
                        status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
                        recovery_action=(
                            "The observed state is recorded; compile a new plan from a fresh "
                            "read before any further mutation."
                        ),
                        diagnostic_code="ATTEMPT_RECOVERY_RECORDED",
                    )
                )
            return ExecutionResult(
                plan_digest=plan.plan_digest,
                authorization_id=recovery.authorization_id,
                results=tuple(results),
                consumed=True,
                recovery_required=bool(self.ledger.unresolved_runs()),
            )

    def _authorize_attempt_recovery(
        self,
        recovery: Authorization,
        subjects: tuple[str, ...],
        approval_host: UserApprovalHost,
        now: datetime,
    ) -> None:
        """Bind a recovery grant to the exact attempts it may release."""

        subject_digest = digest(list(subjects))
        if isinstance(recovery, DirectGrant):
            if recovery.expires_at <= now:
                raise AdhelmError(
                    "AUTHORIZATION_EXPIRED",
                    "The recovery authorization has expired.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if recovery.plan_digest != subject_digest or recovery.subplan_digests != subjects:
                raise AdhelmError(
                    "ATTEMPT_RECOVERY_SUBJECT_MISMATCH",
                    "The recovery grant does not name these exact unresolved attempts.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if not _verifies_direct_intent(approval_host, recovery):
                raise AdhelmError(
                    "USER_APPROVAL_HOST_REQUIRED",
                    "The configured host could not prove this recovery approval.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
        else:
            policy = recovery.policy
            if recovery.issuing_agent_or_host_ref != policy.authorized_agent_or_host_ref:
                raise AdhelmError(
                    "DELEGATION_SOURCE_MISMATCH",
                    "The caller is not the configured delegated agent or host.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if (
                policy.expires_at <= now
                or policy.effective_from > now
                or (policy.revoked_at is not None and policy.revoked_at <= now)
            ):
                raise AdhelmError(
                    "DELEGATION_NOT_CURRENT",
                    "The delegation policy is not currently effective.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if OperationKind.RECOVER not in policy.allowed_operations:
                raise AdhelmError(
                    "DELEGATION_OPERATION_DENIED",
                    "The policy does not cover recording an attempt recovery.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            if recovery.plan_digest != subject_digest:
                raise AdhelmError(
                    "ATTEMPT_RECOVERY_SUBJECT_MISMATCH",
                    "The recovery grant does not name these exact unresolved attempts.",
                    exit_code=ExitCode.AUTHORIZATION,
                )
            self._require_installed_policy(policy)
        for record in self._records():
            payload = record.get("payload", {})
            if (
                record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
                and payload.get("recovery_approval_ref") == recovery.authorization_id
            ):
                raise AdhelmError(
                    "AUTHORIZATION_ALREADY_CONSUMED",
                    "This one-use recovery authorization was already recorded.",
                    exit_code=ExitCode.AUTHORIZATION,
                )

    def _require_installed_policy(self, policy: DelegationPolicy) -> None:
        matching = [
            record.get("payload", {})
            for record in self._records()
            if record.get("payload", {}).get("approval_ref") == self._policy_ref(policy)
        ]
        if not matching or matching[-1].get("event_type") != "APPROVAL_GRANTED":
            raise AdhelmError(
                "DELEGATION_NOT_INSTALLED",
                "The current delegation policy is not durably installed.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        if matching[-1].get("plan_digest") != policy.policy_digest:
            raise AdhelmError(
                "DELEGATION_POLICY_DRIFT",
                "The supplied policy differs from the durable installed policy.",
                exit_code=ExitCode.AUTHORIZATION,
            )

    def reconcile_run(
        self,
        plan: CompositePlan,
        authorization: Authorization,
        adapters: dict[str, Adapter],
    ) -> ExecutionResult:
        """Read-only reconciliation of an exact retained run; expired write grants are acceptable.

        The adapter must prove settled provider-native operation state. An unchanged
        eventually consistent snapshot alone is insufficient evidence of no effect.
        """
        plan = _revalidate(plan, CompositePlan)
        authorization = _revalidate_authorization(authorization)
        with self._coordinator_locks(plan, authorization):

            def names_this_run(record: dict[str, Any]) -> bool:
                payload = record["payload"]
                return bool(
                    payload["plan_digest"] == plan.plan_digest
                    and payload["approval_ref"] == authorization.authorization_id
                    and payload["authorization_digest"] == digest(authorization)
                    and record["run_id"] == plan.plan_id
                )

            runs = [r for r in self.ledger.unresolved_runs() if names_this_run(r)]
            history = self._records()
            # Reconciliation is a read.  Repeating it after the barrier has
            # already been released must return the same provider-aware answer
            # instead of failing, and must not append a second set of records.
            barrier_open = len(runs) == 1
            if not barrier_open and not any(
                r["record_kind"] == "COMPOSITE_RUN"
                and r["payload"]["event_type"] == "RUN_STARTED"
                and names_this_run(r)
                for r in history
            ):
                raise AdhelmError(
                    "RECONCILIATION_RUN_MISMATCH",
                    "Reconciliation requires the exact retained plan and authorization.",
                )
            if len(runs) > 1:
                raise AdhelmError(
                    "RECONCILIATION_RUN_MISMATCH",
                    "Reconciliation requires the exact retained plan and authorization.",
                )
            started = {
                r["payload"].get("subplan_digest")
                for r in history
                if r["record_kind"] == "CHANGE_EVENT"
                and r["payload"].get("event_type") == "APPLY_STARTED"
                and r["payload"].get("plan_digest") == plan.plan_digest
                and r["payload"].get("approval_ref") == authorization.authorization_id
            }
            approvals = [
                r
                for r in history
                if r["record_kind"] == "CHANGE_EVENT"
                and r["payload"].get("event_type") == "APPROVAL_GRANTED"
                and r["payload"].get("approval_ref") == authorization.authorization_id
            ]
            results: list[SubplanResult] = []
            for subplan in plan.subplans:
                recovered = False
                contradicted = False
                observed: RemoteState | None = None
                observed_evidence: tuple[str, ...] = ()
                try:
                    adapter = adapters[subplan.provider]
                    if adapter.provider != subplan.provider:
                        raise AdhelmError("ADAPTER_IDENTITY_MISMATCH", "Provider identity differs.")
                    self._check_identity(
                        subplan.account, adapter.read_identity(subplan.account.account_ref)
                    )
                    proof = adapter.reconcile(subplan)
                    proof = Reconciliation.model_validate(proof.model_dump(mode="python"))
                    validate_data(proof.model_dump(mode="json"))
                    attempt_ref = digest(
                        {"plan": plan.plan_digest, "subplan": subplan.subplan_digest}
                    )
                    attempt_state = _unattributable_attempt_state(
                        history, plan=plan, authorization=authorization, subplan=subplan
                    )
                    settlement = (
                        _attributable_settlement(
                            adapter, plan=plan, subplan=subplan, attempt_ref=attempt_ref
                        )
                        if attempt_state == "UNSETTLED"
                        else None
                    )
                    if settlement is not None and settlement.settled_status is proof.status:
                        # The provider bound this exact attempt to this outcome,
                        # so the attempt is attributable after all. No installed
                        # adapter supplies such a proof today.
                        attempt_state = "VERIFIED" if proof.settled else "UNSETTLED"
                    if (
                        attempt_state == "UNSETTLED"
                        and proof.settled
                        and proof.evidence_refs
                        and _attempt_locally_terminated(
                            history, plan=plan, authorization=authorization, subplan=subplan
                        )
                    ):
                        # This kernel completed and journaled its own attempt
                        # protocol; only attribution is missing, and no read can
                        # supply it. Release the write barrier without settling
                        # the attempt; the reported status below stays ambiguous.
                        # An interrupted attempt has no such journalled terminal
                        # pair and stays blocked until an authorized operator
                        # recovery.
                        #
                        # The record must describe an observation, not a claim,
                        # so it takes a fresh identity check and read here --
                        # the same standard `record_attempt_recovery` meets --
                        # instead of copying the adapter's proof state through.
                        recovered = True
                        self._check_identity(
                            subplan.account, adapter.read_identity(subplan.account.account_ref)
                        )
                        observed = _revalidate(adapter.read_state(subplan), RemoteState)
                        observed_evidence = tuple(
                            dict.fromkeys(
                                (
                                    _recovery_evidence(
                                        attempt_recovery_subject(
                                            plan,
                                            subplan,
                                            started_record_digest=_required_start_digest(
                                                history,
                                                plan=plan,
                                                authorization=authorization,
                                                subplan=subplan,
                                            ),
                                        ),
                                        observed,
                                    ),
                                    *proof.evidence_refs,
                                )
                            )
                        )
                        if (
                            observed.fingerprint != proof.state.fingerprint
                            or observed.revision != proof.state.revision
                        ):
                            # The adapter's settled proof and the adapter's own
                            # current read disagree. Record what was read, and
                            # say plainly that the proof did not match it.
                            contradicted = True
                    if attempt_state == "UNSETTLED":
                        # A later provider read can show either baseline or
                        # target configuration, but cannot attribute that state
                        # to this exact started attempt. Preserve it rather than
                        # clearing a one-use mutation attempt or making a retry
                        # safe. Only an adapter that proves settled
                        # provider-native operation state reaches the branches
                        # below.
                        proof = Reconciliation(
                            state=proof.state,
                            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
                            settled=False,
                            evidence_refs=proof.evidence_refs,
                        )
                    elif attempt_state == "VERIFIED" and (
                        proof.status is not EffectStatus.APPLIED_VERIFIED
                        or proof.state.fingerprint != subplan.after.fingerprint
                        or proof.state.revision != subplan.after.revision
                    ):
                        # A composite can remain open if a different provider
                        # stopped it. Do not let a contradictory later read
                        # rewrite this exact durable verified attempt as no
                        # effect; it is not evidence about the original egress.
                        proof = Reconciliation(
                            state=proof.state,
                            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
                            settled=False,
                            evidence_refs=proof.evidence_refs,
                        )
                    if proof.status is EffectStatus.APPLIED_VERIFIED and (
                        proof.state.fingerprint != subplan.after.fingerprint
                        or proof.state.revision != subplan.after.revision
                    ):
                        raise AdhelmError(
                            "RECONCILIATION_STATE_MISMATCH", "Verified state differs."
                        )
                    if proof.status is EffectStatus.FAILED_NO_EFFECT and (
                        proof.state.fingerprint != subplan.before.fingerprint
                        or proof.state.revision != subplan.before.revision
                    ):
                        raise AdhelmError(
                            "RECONCILIATION_STATE_MISMATCH", "No-effect state differs."
                        )
                except Exception:
                    results.append(
                        SubplanResult(
                            provider=subplan.provider,
                            subplan_digest=subplan.subplan_digest,
                            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
                            recovery_action="Provider-aware reconciliation remains required.",
                            diagnostic_code="RECONCILIATION_UNPROVEN",
                        )
                    )
                    continue
                if recovered and observed is not None and barrier_open:
                    self._append_attempt_recovery(
                        plan=plan,
                        subplan=subplan,
                        authorization=authorization,
                        history=history,
                        observed=observed,
                        evidence_refs=observed_evidence,
                        source="PROVIDER_SETTLEMENT_PROOF",
                    )
                if proof.settled:
                    if (
                        subplan.subplan_digest not in started
                        and proof.status is not EffectStatus.FAILED_NO_EFFECT
                    ):
                        proof = proof.model_copy(update={"status": EffectStatus.PARTIAL_EFFECT})
                    if barrier_open:
                        # A repeat read of an already released run reports the
                        # same provider-aware answer and appends nothing.
                        self._append_reconciliation(
                            plan=plan,
                            subplan=subplan,
                            authorization=authorization,
                            proof=proof,
                            external=subplan.subplan_digest not in started,
                            approvals=approvals,
                        )
                results.append(
                    SubplanResult(
                        provider=subplan.provider,
                        subplan_digest=subplan.subplan_digest,
                        status=proof.status,
                        evidence_refs=proof.evidence_refs,
                        recovery_action="Compile a new plan for any further mutation."
                        if proof.settled
                        else "Provider-aware reconciliation remains required.",
                        diagnostic_code="RECONCILIATION_PROOF_CONTRADICTS_READ"
                        if contradicted
                        else None,
                    )
                )
            return ExecutionResult(
                plan_digest=plan.plan_digest,
                authorization_id=authorization.authorization_id,
                results=tuple(results),
                consumed=True,
                recovery_required=bool(self.ledger.unresolved_runs()),
            )

    @staticmethod
    def _reservation(plan: CompositePlan) -> tuple[dict[str, int | str], ...]:
        totals: dict[str, int] = {}
        for subplan in plan.subplans:
            totals[subplan.max_spend.currency] = (
                totals.get(subplan.max_spend.currency, 0) + subplan.max_spend.minor_units
            )
        return tuple(
            {"currency": key, "minor_units": value} for key, value in sorted(totals.items())
        )

    def _dispatch(
        self,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        adapter: Adapter,
        idempotency_key: str,
    ) -> tuple[ApplyResponse, bool]:
        """Run the adapter's write behind exactly one permit-bound egress path.

        The second value reports whether the adapter actually claimed its
        permit; a confirmed acknowledgement without a claim is not attributable
        to the guarded seam and is downgraded by the caller.
        """

        # The adapter receives only the typed, locally screened plan surface.
        validate_data(subplan.model_dump(mode="json"))
        if subplan.expected_child_egress_count > 1:
            if not isinstance(adapter, ChildEgressAdapter):
                raise AdhelmError(
                    "ADAPTER_CHILD_EGRESS_REQUIRED",
                    "A multi-step subplan requires the journaled child egress interface.",
                )
            return (
                self._apply_children(plan, subplan, authorization, adapter, idempotency_key),
                True,
            )
        if isinstance(adapter, GuardedAdapter):
            with _kernel_permit(
                plan_digest=plan.plan_digest,
                subplan_digest=subplan.subplan_digest,
                attempt_ref=idempotency_key,
                check_current=lambda: self._check_mutation_window(plan, authorization),
            ) as permit:
                # Retained contexts become invalid immediately when the
                # adapter call returns or raises; validation and readback do
                # not extend the one-use mutation permit's lifetime.
                try:
                    response = adapter.apply_guarded(
                        subplan, idempotency_key=idempotency_key, permit=permit
                    )
                    return response, _permit_claimed(permit)
                finally:
                    _invalidate_permit(permit)
        # Only a future read-only operation kind reaches this branch; preflight
        # refuses an unguarded adapter for every mutating subplan.
        return adapter.apply(subplan, idempotency_key=idempotency_key), True

    def _apply_children(
        self,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        adapter: ChildEgressAdapter,
        idempotency_key: str,
    ) -> ApplyResponse:
        execution = KernelExecution(
            _issuer=_EXECUTION_ISSUER,
            kernel=self,
            plan=plan,
            subplan=subplan,
            authorization=authorization,
            idempotency_key=idempotency_key,
        )
        try:
            response = adapter.apply_children(
                subplan, idempotency_key=idempotency_key, execution=execution
            )
        finally:
            execution._close()
        if not execution.every_child_applied:
            # A subplan whose approved children did not all prove they applied
            # has no attributable overall acknowledgement, whatever the adapter
            # returned. Its unresolved children already block any retry.
            return ApplyResponse(outcome=ApplyOutcome.AMBIGUOUS)
        return response

    def _apply_one(
        self,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        adapter: Adapter,
        idempotency_key: str,
    ) -> SubplanResult:
        response: ApplyResponse | None = None
        try:
            response, permit_claimed = self._dispatch(
                plan, subplan, authorization, adapter, idempotency_key
            )
            response = ApplyResponse.model_validate(response.model_dump(mode="python"))
            validate_data(response.model_dump(mode="json"))
        except Exception:  # Adapter exceptions carry no trusted remote-state conclusion.
            response = ApplyResponse(outcome=ApplyOutcome.AMBIGUOUS)
            permit_claimed = False
        egress_not_claimed = response.outcome is ApplyOutcome.CONFIRMED and not permit_claimed
        if egress_not_claimed:
            # A confirmed acknowledgement whose permit was never consumed gives
            # no evidence that the write went through the guarded seam.
            response = ApplyResponse(
                outcome=ApplyOutcome.AMBIGUOUS,
                opaque_resource_refs=response.opaque_resource_refs,
                evidence_refs=response.evidence_refs,
            )

        try:
            after = adapter.read_state(subplan)
        except Exception:
            after = None
        diagnostic_code: str | None = None
        if (
            _requires_attributable_confirmation(subplan)
            and response.outcome is not ApplyOutcome.CONFIRMED
        ):
            # No read taken by this caller can attribute a non-confirmed
            # mutation, whatever the provider or operation. An adapter's own
            # no-effect claim plus its own unchanged read is exactly the pair
            # `reconcile_run` already refuses to treat as proof, so it never
            # upgrades this result either.
            if egress_not_claimed:
                diagnostic_code = "GUARDED_EGRESS_NOT_CLAIMED"
            elif response.no_effect_proven:
                diagnostic_code = "NO_EFFECT_CLAIM_NOT_INDEPENDENTLY_PROVEN"
            status = EffectStatus.AMBIGUOUS_REMOTE_STATE
            verification = Verification(verified=False)
        elif after is None:
            status = EffectStatus.AMBIGUOUS_REMOTE_STATE
            verification = Verification(verified=False)
        elif (
            after.fingerprint == subplan.after.fingerprint
            and after.revision == subplan.after.revision
        ):
            try:
                candidate = adapter.verify(subplan)
                if candidate.verified and not candidate.evidence_refs:
                    diagnostic_code = "VERIFICATION_EVIDENCE_REQUIRED"
                    verification = Verification(verified=False)
                else:
                    verification = candidate
                verification = Verification.model_validate(verification.model_dump(mode="python"))
                validate_data(verification.model_dump(mode="json"))
            except Exception:
                verification = Verification(verified=False)
            status = (
                EffectStatus.APPLIED_VERIFIED
                if verification.verified
                else EffectStatus.PARTIAL_EFFECT
            )
        elif (
            after.fingerprint == subplan.before.fingerprint
            and after.revision == subplan.before.revision
        ):
            # A confirmed acknowledgement whose readback never left the baseline
            # contradicts itself. Reconciliation, not this caller's read, settles
            # which of the two observations was stale.
            verification = Verification(verified=False)
            status = EffectStatus.AMBIGUOUS_REMOTE_STATE
        else:
            verification = Verification(verified=False)
            status = EffectStatus.PARTIAL_EFFECT
        refs = response.opaque_resource_refs if response is not None else ()
        evidence = tuple(
            dict.fromkeys(
                (response.evidence_refs if response is not None else ())
                + verification.evidence_refs
            )
        )
        self._append_change(
            event_type="APPLY_RESULT",
            subplan=subplan,
            plan=plan,
            authorization=authorization,
            effect_status=status.value,
            idempotency_ref=idempotency_key,
            extra={"evidence_refs": list(response.evidence_refs)},
        )
        self._append_change(
            event_type="VERIFICATION_RESULT",
            subplan=subplan,
            plan=plan,
            authorization=authorization,
            effect_status=status.value,
            idempotency_ref=idempotency_key,
            extra={"evidence_refs": list(verification.evidence_refs)},
        )
        recovery = (
            None
            if status is EffectStatus.APPLIED_VERIFIED
            else "Reconcile remote state; compile a new plan before any retry."
        )
        return SubplanResult(
            provider=subplan.provider,
            subplan_digest=subplan.subplan_digest,
            status=status,
            resource_refs=refs,
            evidence_refs=evidence,
            recovery_action=recovery,
            diagnostic_code=diagnostic_code,
        )

    def _append_change(
        self,
        *,
        event_type: str,
        subplan: SubPlan,
        plan: CompositePlan,
        authorization: Authorization,
        effect_status: str,
        idempotency_ref: str,
        extra: dict[str, Any],
    ) -> None:
        payload: dict[str, Any] = {
            "event_type": event_type,
            "operation": subplan.operation.value,
            "origin": "LOCAL_CLI",
            "actor_ref": None,
            "resource_type": "PLAN",
            "resource_ref": subplan.subplan_id,
            "parent_resource_refs": [],
            "recommendation_ref": None,
            "plan_ref": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "subplan_digest": subplan.subplan_digest,
            "approval_ref": authorization.authorization_id,
            "idempotency_ref": idempotency_ref,
            "before_snapshot_ref": None,
            "before_digest": subplan.before.fingerprint,
            "after_snapshot_ref": None,
            "after_digest": subplan.after.fingerprint,
            "field_deltas": [],
            "effect_status": effect_status,
            "spend_impact_minor": subplan.max_spend.minor_units,
            "currency": subplan.max_spend.currency,
            "provider_api_version": subplan.provider_api_version,
            "connector_version": subplan.capability_version,
            "evidence_refs": [],
            "superseded_record_ref": None,
            "replacement_record_ref": None,
            "supersession_reason": None,
            **extra,
        }
        validate_data(payload)
        self.ledger.append(
            "changes",
            "CHANGE_EVENT",
            payload,
            provider=subplan.provider,
            account_ref=subplan.account.account_ref,
            run_id=plan.plan_id,
            source="LOCAL_CLI",
        )

    def _child_payload(
        self,
        *,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        idempotency_ref: str,
        child_index: int,
        child_label: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation": subplan.operation.value,
            "resource_ref": subplan.subplan_id,
            "plan_ref": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "subplan_digest": subplan.subplan_digest,
            "approval_ref": authorization.authorization_id,
            "idempotency_ref": idempotency_ref,
            "child_index": child_index,
            "expected_child_egress_count": subplan.expected_child_egress_count,
            "child_label": child_label,
        }

    def _append_child_started(
        self,
        *,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        idempotency_ref: str,
        child_index: int,
        child_label: str,
    ) -> None:
        """Durably journal one child request before its permit can exist."""

        payload = self._child_payload(
            plan=plan,
            subplan=subplan,
            authorization=authorization,
            idempotency_ref=idempotency_ref,
            child_index=child_index,
            child_label=child_label,
        )
        hosts = sorted({target.host for target in subplan.egress})
        payload["effect_status"] = "NO_REMOTE_WRITE"
        payload["intent"] = {
            "egress_hosts": hosts,
            # The approved allowlist, not the request this child will send. The
            # kernel does not know which allowlisted target an adapter will use
            # and must not invent one.
            "egress_allowlist_digest": digest(
                [
                    {"host": target.host, "purpose": target.purpose}
                    for target in sorted(subplan.egress, key=lambda item: (item.host, item.purpose))
                ]
            ),
            "intent_digest": digest(
                {
                    "schema_version": 1,
                    "plan_digest": plan.plan_digest,
                    "subplan_digest": subplan.subplan_digest,
                    "attempt_ref": idempotency_ref,
                    "child_index": child_index,
                    "child_label": child_label,
                }
            ),
        }
        validate_data(payload)
        self.ledger.append(
            "changes",
            "CHILD_APPLY_STARTED",
            payload,
            provider=subplan.provider,
            account_ref=subplan.account.account_ref,
            run_id=plan.plan_id,
            source="LOCAL_CLI",
        )

    def _append_child_result(
        self,
        *,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        idempotency_ref: str,
        child_index: int,
        child_label: str,
        status: EffectStatus,
        evidence_refs: tuple[str, ...],
    ) -> None:
        payload = self._child_payload(
            plan=plan,
            subplan=subplan,
            authorization=authorization,
            idempotency_ref=idempotency_ref,
            child_index=child_index,
            child_label=child_label,
        )
        payload["effect_status"] = status.value
        payload["evidence_refs"] = list(evidence_refs)
        validate_data(payload)
        self.ledger.append(
            "changes",
            "CHILD_APPLY_RESULT",
            payload,
            provider=subplan.provider,
            account_ref=subplan.account.account_ref,
            run_id=plan.plan_id,
            source="LOCAL_CLI",
        )

    def _append_reconciliation(
        self,
        *,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        proof: Reconciliation,
        external: bool,
        approvals: list[dict[str, Any]],
    ) -> None:
        """Journal one settled provider-aware reconciliation of an exact attempt."""

        if external and proof.status is EffectStatus.PARTIAL_EFFECT:
            self._append_change(
                event_type="REMOTE_CHANGE_OBSERVED",
                subplan=subplan,
                plan=plan,
                authorization=authorization,
                effect_status="EXTERNAL_CHANGE_DETECTED",
                idempotency_ref="observation:" + digest(proof),
                extra={
                    "origin": "UNKNOWN",
                    "actor_ref": None,
                    "after_digest": proof.state.fingerprint,
                    "evidence_refs": list(proof.evidence_refs),
                    "observation_interval_start": approvals[-1]["recorded_at"]
                    if approvals
                    else None,
                    "observation_interval_end": self.clock().isoformat(),
                },
            )
        attempt = digest({"plan": plan.plan_digest, "subplan": subplan.subplan_digest})
        self._append_change(
            event_type="RECONCILIATION_RESULT",
            subplan=subplan,
            plan=plan,
            authorization=authorization,
            effect_status=proof.status.value,
            idempotency_ref=attempt,
            extra={
                "evidence_refs": list(proof.evidence_refs),
                "after_digest": proof.state.fingerprint,
            },
        )
        self._append_run(
            "PROVIDER_RECONCILED",
            plan,
            authorization,
            reconciled_subplan_digest=subplan.subplan_digest,
            effect_status=proof.status.value,
            evidence_refs=proof.evidence_refs,
        )

    def _append_attempt_recovery(
        self,
        *,
        plan: CompositePlan,
        subplan: SubPlan,
        authorization: Authorization,
        history: list[dict[str, Any]],
        observed: RemoteState,
        evidence_refs: tuple[str, ...],
        source: str,
        recovery: Authorization | None = None,
        subject_digest: str | None = None,
    ) -> str:
        """Record the current state after an unattributable attempt, and only that.

        The record names no actor and never restates the attempt as applied.
        It exists so that one lost acknowledgement cannot refuse every later
        plan on every provider forever.
        """

        start = _attempt_start_record(
            history, plan=plan, authorization=authorization, subplan=subplan
        )
        if start is None:
            raise AdhelmError(
                "ATTEMPT_RECOVERY_START_MISSING",
                "Recovery requires the exact durable start of one unresolved attempt.",
            )
        relation = _observed_relation(subplan, observed)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "operation": subplan.operation.value,
            "resource_ref": subplan.subplan_id,
            "plan_ref": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "subplan_digest": subplan.subplan_digest,
            "approval_ref": authorization.authorization_id,
            "idempotency_ref": start["payload"]["idempotency_ref"],
            "started_record_digest": start["record_digest"],
            "recovery_source": source,
            "recovery_approval_ref": recovery.authorization_id if recovery is not None else None,
            "recovery_authorization_digest": digest(recovery) if recovery is not None else None,
            "recovery_subject_digest": subject_digest,
            "observed_state_fingerprint": observed.fingerprint,
            "observed_state_revision": observed.revision,
            "observed_relation": relation,
            "disposition": _RECOVERY_DISPOSITION[relation],
            "actor_ref": "UNKNOWN",
            "attempt_effect_status": "AMBIGUOUS_REMOTE_STATE",
            "observation_interval_start": start["recorded_at"],
            "evidence_refs": list(evidence_refs),
        }
        validate_data(payload)
        self.ledger.append(
            "changes",
            "ATTEMPT_RECOVERY_OBSERVED",
            payload,
            provider=subplan.provider,
            account_ref=subplan.account.account_ref,
            run_id=plan.plan_id,
            source="LOCAL_CLI",
        )
        return cast(str, start["record_digest"])

    def _append_authorization_event(
        self,
        *,
        authorization_id: str,
        actor_ref: str,
        plan_ref: str,
        plan_digest: str | None,
        subplan_digest: str | None,
        evidence_refs: list[str],
        policy_id: str | None,
        authorization_digest: str | None = None,
        event_type: str = "APPROVAL_GRANTED",
        operation: str = "UPDATE",
        before_append: Callable[[], None] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event_type": event_type,
            "operation": operation,
            "origin": "LOCAL_CLI",
            "actor_ref": actor_ref,
            "resource_type": "AUTHORIZATION",
            "resource_ref": authorization_id,
            "parent_resource_refs": [],
            "recommendation_ref": None,
            "plan_ref": plan_ref,
            "plan_digest": plan_digest,
            "subplan_digest": subplan_digest,
            "approval_ref": authorization_id,
            "authorization_digest": authorization_digest,
            "idempotency_ref": None,
            "before_snapshot_ref": None,
            "before_digest": None,
            "after_snapshot_ref": None,
            "after_digest": None,
            "field_deltas": [],
            "effect_status": "NO_REMOTE_WRITE",
            "spend_impact_minor": None,
            "currency": None,
            "provider_api_version": None,
            "connector_version": None,
            "evidence_refs": evidence_refs,
            "superseded_record_ref": None,
            "replacement_record_ref": None,
            "supersession_reason": None,
        }
        # Policy reservations belong exclusively to APPLY_STARTED.  Installation
        # and revocation bind the policy through their durable approval reference.
        del policy_id
        validate_data(payload)
        self.ledger.append(
            "changes",
            "CHANGE_EVENT",
            payload,
            source="LOCAL_CLI",
            _before_durable_append=before_append,
        )

    def _coordinator_locks(self, plan: CompositePlan, authorization: Authorization) -> ExitStack:
        # Stable identities prevent aliases and policy allowances from bypassing ordering.
        names = {f"account-{item.account.stable_account_id}" for item in plan.subplans}
        if isinstance(authorization, DelegatedGrant):
            names.add(f"policy-{authorization.policy.policy_id}")
        else:
            names.add(f"grant-{authorization.authorization_id}")
        stack = ExitStack()
        try:
            for name in sorted(names):
                safe_name = digest(name)[:32]
                lock = portalocker.Lock(self._lock_dir / f"{safe_name}.lock", timeout=5)
                stack.enter_context(cast(AbstractContextManager[Any], lock))
        except portalocker.exceptions.LockException as exc:
            stack.close()
            raise AdhelmError(
                "COORDINATOR_BUSY", "Another local operation holds an overlapping coordinator lock."
            ) from exc
        return stack

    def _policy_lock(self, policy_id: str) -> ExitStack:
        stack = ExitStack()
        try:
            safe_name = digest(f"policy-{policy_id}")[:32]
            lock = portalocker.Lock(self._lock_dir / f"{safe_name}.lock", timeout=5)
            stack.enter_context(cast(AbstractContextManager[Any], lock))
        except portalocker.exceptions.LockException as exc:
            stack.close()
            raise AdhelmError(
                "COORDINATOR_BUSY",
                "Another local operation holds this delegation policy lock.",
            ) from exc
        return stack


def _recovery_evidence(subject: str, observed: RemoteState) -> str:
    """A local, opaque reference to exactly what this recovery observed."""

    return "evidence:sha256:" + digest(
        {
            "kind": "attempt_recovery_observation",
            "subject": subject,
            "fingerprint": observed.fingerprint,
            "revision": observed.revision,
        }
    )


def new_attempt_recovery_grant(
    *,
    authorization_id: str | None = None,
    subjects: tuple[str, ...],
    grantor_ref: str,
    host_ref: str,
    user_interaction_ref: str,
    expires_at: datetime,
) -> DirectGrant:
    """Build the one-use grant that authorizes recording exact attempt recoveries.

    Host integrations call this only after their documented user-originated
    approval, exactly as for :func:`new_direct_grant`.  The subjects come from
    :func:`attempt_recovery_subject`; they bind the durable ``APPLY_STARTED``
    records this grant may release and nothing else.
    """

    if not subjects or len(set(subjects)) != len(subjects):
        raise AdhelmError(
            "INVALID_SAFETY_INPUT",
            "A recovery grant names at least one distinct unresolved attempt.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return DirectGrant(
        authorization_id=authorization_id or str(uuid4()),
        source=DirectGrantSource.USER_CONTROLLED_HOST,
        grantor_ref=grantor_ref,
        host_ref=host_ref,
        user_interaction_ref=user_interaction_ref,
        plan_digest=digest(list(subjects)),
        subplan_digests=subjects,
        expires_at=expires_at,
    )


def new_direct_grant(
    *,
    authorization_id: str | None = None,
    plan: CompositePlan,
    grantor_ref: str,
    host_ref: str,
    user_interaction_ref: str,
    expires_at: datetime,
) -> DirectGrant:
    """Host integrations call this only after their documented user-originated approval."""

    return DirectGrant(
        authorization_id=authorization_id or str(uuid4()),
        source=DirectGrantSource.USER_CONTROLLED_HOST,
        grantor_ref=grantor_ref,
        host_ref=host_ref,
        user_interaction_ref=user_interaction_ref,
        plan_digest=plan.plan_digest,
        subplan_digests=plan.subplan_digests,
        expires_at=expires_at,
    )

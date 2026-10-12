from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    DelegatedGrant,
    DelegationPolicy,
    DelegationPolicySource,
    DirectGrant,
    DirectGrantSource,
    EffectStatus,
    EgressTarget,
    GuardedAdapter,
    Money,
    MoneyLimit,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    SafetyKernel,
    StateRef,
    SubPlan,
    Verification,
    new_direct_grant,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

NOW = datetime(2030, 1, 1, tzinfo=UTC)
BEFORE = "a" * 64
AFTER = "b" * 64
OPAQUE_REF = "opaque:sha256:" + "c" * 64
EVIDENCE_REF = "evidence:sha256:" + "d" * 64


class FakeApprovalHost:
    host_ref = "local-trusted-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return grant.user_interaction_ref == "approved-interaction"

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool:
        return policy.source_authorization_ref == "approved-policy"


class FakeAdapter:
    provider = "google_ads"

    def __init__(
        self,
        *,
        state: str = BEFORE,
        apply_outcome: ApplyOutcome = ApplyOutcome.CONFIRMED,
        post_apply_state: str | None = None,
        fail_post_apply_read: bool = False,
    ) -> None:
        self.state = state
        self.apply_outcome = apply_outcome
        self.post_apply_state = post_apply_state
        self.fail_post_apply_read = fail_post_apply_read
        self.apply_calls = 0
        self.ledger: Ledger | None = None

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_api_version="v1",
            capability_version="cap-v1",
            supported_operations=(OperationKind.CREATE, OperationKind.ACTIVATE),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        assert account_ref == "alias-account"
        return RemoteIdentity(
            stable_account_id="stable-account",
            auth_profile_ref="edit-profile",
            principal_ref="principal-1",
            client_application_ref="client-1",
            scopes=("ads.write",),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        if self.fail_post_apply_read and self.apply_calls:
            raise ConnectionError("synthetic post-write transport loss")
        return RemoteState(fingerprint=self.state, revision="r1")

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        self.apply_calls += 1
        assert self.ledger is not None
        assert any(
            item["payload"].get("event_type") == "APPLY_STARTED"
            for item in self.ledger.records("changes")
        )
        if self.apply_outcome is ApplyOutcome.CONFIRMED:
            self.state = self.post_apply_state or AFTER
        return ApplyResponse(outcome=self.apply_outcome, opaque_resource_refs=(OPAQUE_REF,))

    def verify(self, plan: SubPlan) -> Verification:
        return Verification(verified=True, evidence_refs=(EVIDENCE_REF,))

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        settled = self.state in {BEFORE, AFTER}
        return Reconciliation(
            state=RemoteState(fingerprint=self.state, revision="r1"),
            status=EffectStatus.APPLIED_VERIFIED
            if self.state == AFTER
            else EffectStatus.FAILED_NO_EFFECT
            if self.state == BEFORE
            else EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=settled,
            evidence_refs=(EVIDENCE_REF,) if settled else (),
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        """Every mutating subplan now reaches its provider through this seam.

        The generalized kernel rule is not a Meta/GA4 provider allowlist, so an
        ordinary Google Ads fake claims the kernel's one-use permit exactly the
        way a real provider adapter does.
        """

        permit.consume(
            plan_digest=self._started_plan_digest(plan, idempotency_key),
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        return self.apply(plan, idempotency_key=idempotency_key)

    def _started_plan_digest(self, plan: SubPlan, idempotency_key: str) -> str:
        assert self.ledger is not None
        for record in self.ledger.records("changes"):
            payload = record["payload"]
            if (
                payload.get("event_type") == "APPLY_STARTED"
                and payload.get("subplan_digest") == plan.subplan_digest
                and payload.get("idempotency_ref") == idempotency_key
            ):
                return str(payload["plan_digest"])
        raise AssertionError("guarded egress requires the kernel's durable start")


def subplan(*, operation: OperationKind = OperationKind.CREATE, spend: int = 100) -> SubPlan:
    return SubPlan(
        subplan_id="google-create",
        provider="google_ads",
        operation=operation,
        account=AccountIdentity(
            account_ref="alias-account",
            stable_account_id="stable-account",
            auth_profile_ref="edit-profile",
            principal_ref="principal-1",
            client_application_ref="client-1",
            scopes=("ads.write",),
        ),
        provider_api_version="v1",
        capability_version="cap-v1",
        before=StateRef(fingerprint=BEFORE, revision="r1"),
        after=StateRef(fingerprint=AFTER, revision="r1"),
        max_spend=Money(minor_units=spend, currency="USD"),
        egress=(EgressTarget(host="api.google.test", purpose="paused campaign creation"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=NOW + timedelta(hours=1),
    )


def plan(*, operation: OperationKind = OperationKind.CREATE, spend: int = 100) -> CompositePlan:
    return CompositePlan(
        plan_id="run-plan",
        profile_ref="test-profile",
        subplans=(subplan(operation=operation, spend=spend),),
        expires_at=NOW + timedelta(hours=1),
    )


def kernel(tmp_path: Path) -> tuple[SafetyKernel, Ledger]:
    ledger = Ledger(tmp_path, profile_ref="test-profile")
    return SafetyKernel(tmp_path, ledger, clock=lambda: NOW), ledger


def direct_grant(compiled: CompositePlan) -> DirectGrant:
    return new_direct_grant(
        authorization_id="direct-1",
        plan=compiled,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=NOW + timedelta(minutes=10),
    )


def test_direct_execution_is_host_recorded_and_fsync_started_before_egress(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger

    result = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert adapter.apply_calls == 1
    events = [
        record["payload"]["event_type"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
    ]
    assert events == ["APPROVAL_GRANTED", "APPLY_STARTED", "APPLY_RESULT", "VERIFICATION_RESULT"]


def test_agent_assertion_and_unrecorded_direct_grant_cannot_authorize(tmp_path: Path) -> None:
    compiled = plan()
    with pytest.raises(ValidationError):
        DirectGrant(
            authorization_id="bad",
            source=DirectGrantSource.AGENT_ASSERTION,
            grantor_ref="agent",
            host_ref="agent",
            user_interaction_ref="claim",
            plan_digest=compiled.plan_digest,
            subplan_digests=compiled.subplan_digests,
            expires_at=NOW + timedelta(minutes=1),
        )
    instance, ledger = kernel(tmp_path)
    adapter = FakeAdapter()
    adapter.ledger = ledger
    with pytest.raises(AdhelmError, match="No durable"):
        instance.execute(compiled, direct_grant(compiled), {adapter.provider: adapter})
    assert adapter.apply_calls == 0


def test_expiry_and_drift_fail_before_consumption_or_egress(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = FakeAdapter(state="c" * 64)
    adapter.ledger = ledger
    with pytest.raises(AdhelmError, match="Remote state"):
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0
    assert [
        record["payload"]["event_type"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
    ] == ["APPROVAL_GRANTED"]


def test_expired_plan_fails_without_a_provider_read_or_authorization_consumption(
    tmp_path: Path,
) -> None:
    instance, ledger = kernel(tmp_path)
    original = plan()
    expired_at = NOW - timedelta(seconds=1)
    expired = original.model_copy(
        update={
            "expires_at": expired_at,
            "subplans": tuple(
                item.model_copy(update={"expires_at": expired_at}) for item in original.subplans
            ),
        }
    )
    adapter = FakeAdapter()
    adapter.ledger = ledger
    with pytest.raises(AdhelmError, match="expired"):
        instance.execute(expired, direct_grant(plan()), {adapter.provider: adapter})
    assert adapter.apply_calls == 0
    assert not [
        record for record in ledger.records("changes") if record["record_kind"] == "CHANGE_EVENT"
    ]


def test_direct_grant_is_one_use_even_after_success(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger
    instance.execute(compiled, grant, {adapter.provider: adapter})
    with pytest.raises(AdhelmError, match="already started"):
        instance.execute(compiled, grant, {adapter.provider: adapter})


def test_ambiguous_attempt_consumes_the_original_grant_without_blind_retry(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    ambiguous = FakeAdapter(fail_post_apply_read=True)
    ambiguous.ledger = ledger

    outcome = instance.execute(compiled, grant, {ambiguous.provider: ambiguous})

    assert outcome.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    retry = FakeAdapter()
    retry.ledger = ledger
    with pytest.raises(AdhelmError, match="reconciliation"):
        instance.execute(compiled, grant, {retry.provider: retry})
    assert retry.apply_calls == 0


def test_invalid_adapter_references_are_not_propagated(tmp_path: Path) -> None:
    class LeakyAdapter(FakeAdapter):
        def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
            self.apply_calls += 1
            self.state = AFTER
            return ApplyResponse.model_construct(
                outcome=ApplyOutcome.CONFIRMED,
                opaque_resource_refs=("person@example.com",),
                evidence_refs=("raw provider response",),
            )

    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = LeakyAdapter()
    adapter.ledger = ledger

    outcome = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert outcome.results[0].resource_refs == ()
    assert all("@" not in ref and " " not in ref for ref in outcome.results[0].evidence_refs)


def test_claimed_verification_without_evidence_is_not_verified(tmp_path: Path) -> None:
    class UnprovenAdapter(FakeAdapter):
        def verify(self, plan: SubPlan) -> Verification:
            return Verification.model_construct(verified=True, evidence_refs=())

    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = UnprovenAdapter()
    adapter.ledger = ledger

    outcome = instance.execute(compiled, grant, {adapter.provider: adapter})

    assert outcome.results[0].status is EffectStatus.PARTIAL_EFFECT
    assert outcome.results[0].diagnostic_code == "VERIFICATION_EVIDENCE_REQUIRED"


def test_later_subplan_is_reread_for_drift_after_an_earlier_provider_writes(
    tmp_path: Path,
) -> None:
    class MetaAdapter(FakeAdapter):
        provider = "meta_ads"

    meta = MetaAdapter()

    class GoogleChangesMeta(FakeAdapter):
        def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
            result = super().apply(plan, idempotency_key=idempotency_key)
            meta.state = "e" * 64
            return result

    instance, ledger = kernel(tmp_path)
    google = subplan()
    meta_subplan = google.model_copy(update={"subplan_id": "meta-create", "provider": "meta_ads"})
    compiled = CompositePlan(
        plan_id="two-provider-plan",
        profile_ref="test-profile",
        subplans=(google, meta_subplan),
        expires_at=NOW + timedelta(hours=1),
    )
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    google_adapter = GoogleChangesMeta()
    google_adapter.ledger = ledger
    meta.ledger = ledger

    outcome = instance.execute(
        compiled,
        grant,
        {google_adapter.provider: google_adapter, meta.provider: meta},
    )
    assert outcome.recovery_required
    assert outcome.results[1].diagnostic_code == "REMOTE_DRIFT"

    assert google_adapter.apply_calls == 1
    assert meta.apply_calls == 0
    starts = [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
        and record["payload"]["event_type"] == "APPLY_STARTED"
    ]
    assert len(starts) == 1


def test_plan_expiring_while_waiting_for_coordinator_lock_never_applies(tmp_path: Path) -> None:
    clock_values = iter((NOW, NOW, NOW + timedelta(hours=2)))
    ledger = Ledger(tmp_path, profile_ref="test-profile")
    instance = SafetyKernel(tmp_path, ledger, clock=lambda: next(clock_values))
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger

    with pytest.raises(AdhelmError, match="expired"):
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0


def delegated(
    compiled: CompositePlan, *, allowed: tuple[OperationKind, ...] = (OperationKind.CREATE,)
) -> DelegatedGrant:
    policy = DelegationPolicy(
        policy_id="policy-1",
        policy_version=1,
        source=DelegationPolicySource.USER_CONTROLLED_CONFIGURATION,
        grantor_ref="marketer",
        source_authorization_ref="approved-policy",
        configuration_host_ref="local-trusted-host",
        configuration_interaction_ref="approved-policy",
        authorized_agent_or_host_ref="selected-agent",
        account_stable_ids=("stable-account",),
        allowed_operations=allowed,
        maximum_change_per_operation=(MoneyLimit(currency="USD", maximum_minor_units=100),),
        cumulative_change_bounds=(MoneyLimit(currency="USD", maximum_minor_units=100),),
        effective_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=1),
        allowed_egress_hosts=("api.google.test",),
    )
    return DelegatedGrant(
        authorization_id="delegated-1",
        policy=policy,
        issuing_agent_or_host_ref="selected-agent",
        plan_digest=compiled.plan_digest,
    )


def test_agent_proposed_delegation_policy_is_rejected_by_the_schema() -> None:
    policy = delegated(plan()).policy
    proposed = policy.model_copy(update={"source": DelegationPolicySource.AGENT_PROPOSAL})
    with pytest.raises(ValidationError, match="agent proposal"):
        DelegationPolicy(**proposed.model_dump())


def test_delegated_limit_egress_and_restart_are_ledger_enforced(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan(spend=100)
    grant = delegated(compiled)
    instance.install_delegation_policy(grant.policy, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger
    instance.execute(compiled, grant, {adapter.provider: adapter})

    next_plan = plan(spend=1).model_copy(update={"plan_id": "second-plan"})
    next_grant = delegated(next_plan)
    next_grant = next_grant.model_copy(
        update={"authorization_id": "delegated-2", "plan_digest": next_plan.plan_digest}
    )
    restarted = SafetyKernel(tmp_path, ledger, clock=lambda: NOW)
    with pytest.raises(AdhelmError, match="cumulative"):
        restarted.execute(next_plan, next_grant, {adapter.provider: adapter})


def test_newer_policy_version_invalidates_the_old_durable_policy(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    old_grant = delegated(compiled)
    instance.install_delegation_policy(old_grant.policy, FakeApprovalHost())
    newer_policy = old_grant.policy.model_copy(update={"policy_version": 2})
    instance.install_delegation_policy(newer_policy, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger

    with pytest.raises(AdhelmError, match="differs"):
        instance.execute(compiled, old_grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0


def test_concurrent_policy_reservations_allow_only_one_cumulative_spend(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    first_plan = plan(spend=60)
    first_grant = delegated(first_plan)
    instance.install_delegation_policy(first_grant.policy, FakeApprovalHost())
    second_plan = first_plan.model_copy(update={"plan_id": "another-plan"})
    second_grant = DelegatedGrant(
        authorization_id="delegated-2",
        policy=first_grant.policy,
        issuing_agent_or_host_ref="selected-agent",
        plan_digest=second_plan.plan_digest,
    )

    def invoke(compiled: CompositePlan, grant: DelegatedGrant) -> str:
        adapter = FakeAdapter()
        adapter.ledger = ledger
        try:
            instance.execute(compiled, grant, {adapter.provider: adapter})
        except AdhelmError as exc:
            return exc.code
        return "OK"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            pool.map(
                lambda item: invoke(*item),
                ((first_plan, first_grant), (second_plan, second_grant)),
            )
        )
    assert outcomes == ["DELEGATION_CUMULATIVE_LIMIT", "OK"]


def test_activation_needs_explicit_delegation_and_never_reaches_adapter(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan(operation=OperationKind.ACTIVATE)
    grant = delegated(compiled, allowed=(OperationKind.CREATE,))
    instance.install_delegation_policy(grant.policy, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger
    with pytest.raises(AdhelmError, match="operation"):
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 0


def test_delegated_egress_outside_the_user_policy_never_reaches_adapter(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = delegated(compiled)
    denied_policy = grant.policy.model_copy(update={"allowed_egress_hosts": ("other.example",)})
    denied = grant.model_copy(update={"policy": denied_policy})
    instance.install_delegation_policy(denied_policy, FakeApprovalHost())
    adapter = FakeAdapter()
    adapter.ledger = ledger
    with pytest.raises(AdhelmError, match="egress"):
        instance.execute(compiled, denied, {adapter.provider: adapter})
    assert adapter.apply_calls == 0


def test_concurrent_direct_attempts_have_one_winner(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())

    def invoke() -> str:
        adapter = FakeAdapter()
        adapter.ledger = ledger
        try:
            instance.execute(compiled, grant, {adapter.provider: adapter})
        except AdhelmError as exc:
            return exc.code
        return "OK"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _: invoke(), range(2)))
    assert outcomes == ["AUTHORIZATION_ALREADY_CONSUMED", "OK"]


def test_every_mutating_operation_kind_needs_a_guarded_permit_bound_adapter(
    tmp_path: Path,
) -> None:
    """The write rule is uniform: there is no provider or operation allowlist.

    This replaces the previous expectation that only Meta PAUSE and GA4 CREATE
    were required to reach their provider through a kernel-issued permit.
    """

    class _Legacy:
        """The pre-generalization adapter surface: no ``apply_guarded``."""

        provider = "google_ads"

        def __init__(self, operation: OperationKind) -> None:
            self.operation = operation
            self.apply_calls = 0

        def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
            del plan
            return AdapterCapabilities(
                provider_api_version="v1",
                capability_version="cap-v1",
                supported_operations=(self.operation,),
            )

        def read_identity(self, account_ref: str) -> RemoteIdentity:
            del account_ref
            raise AssertionError("an unguarded adapter is refused before any provider read")

        def read_state(self, plan: SubPlan) -> RemoteState:
            del plan
            raise AssertionError("an unguarded adapter is refused before any provider read")

        def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
            del plan, idempotency_key
            self.apply_calls += 1
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED)

        def verify(self, plan: SubPlan) -> Verification:
            del plan
            return Verification(verified=False)

        def reconcile(self, plan: SubPlan) -> Reconciliation:
            del plan
            raise AssertionError("unused")

    from adhelm.safety import READ_ONLY_OPERATIONS

    assert READ_ONLY_OPERATIONS == frozenset()
    # RECOVER authorizes recording an observation, never a compiled subplan, so
    # it is the one kind a plan cannot carry at all.
    with pytest.raises(ValidationError, match="RECOVER"):
        subplan(operation=OperationKind.RECOVER)
    for operation in (kind for kind in OperationKind if kind is not OperationKind.RECOVER):
        instance, ledger = kernel(tmp_path / operation.value)
        compiled = plan(operation=operation)
        grant = direct_grant(compiled)
        instance.record_direct_grant(compiled, grant, FakeApprovalHost())
        adapter = _Legacy(operation)
        with pytest.raises(AdhelmError) as error:
            instance.execute(compiled, grant, {adapter.provider: adapter})
        assert error.value.code == "ADAPTER_PERMIT_REQUIRED"
        assert adapter.apply_calls == 0
        assert ledger.unresolved_starts() == []


def test_guarded_adapter_must_also_support_reconciliation(tmp_path: Path) -> None:
    """A provider that can write must be able to prove what happened."""

    class _NoReconcile(FakeAdapter):
        reconcile = None  # type: ignore[assignment]

    instance, ledger = kernel(tmp_path)
    compiled = plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = _NoReconcile()
    adapter.ledger = ledger
    assert not isinstance(adapter, GuardedAdapter)

    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})

    assert error.value.code == "ADAPTER_PERMIT_REQUIRED"

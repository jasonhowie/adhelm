"""Independent fake-only regressions for GA4 key-event CREATE kernel safeguards.

These tests use the already closed GA4 CREATE fixture and in-memory fakes only.
They cover the kernel boundary after authorization, without resolving an OAuth
bundle, opening a provider transport, or invoking a native approval host.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from test_ga4_admin_execution_observation import _KEY
from test_ga4_admin_key_event_create_adapter import _CreateScenario, _install_adapter_http
from test_ga4_create_contract import _NOW
from test_ga4_create_workflow import _setup
from test_saved_ga4_key_event_candidates import _definition

from adhelm.canonical import digest
from adhelm.egress_permit import _kernel_permit
from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_execution_observation import observe_ga4_admin_edit_execution_inventory
from adhelm.ga4_create_execution_sources import validate_current_ga4_create_sources
from adhelm.ga4_create_plan_history import validate_ga4_create_start_history
from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
from adhelm.ledger import Ledger
from adhelm.providers import ga4_admin_key_event_create_adapter as adapter_module
from adhelm.providers.ga4_admin_key_event_create_adapter import _configured_ga4_create_adapter
from adhelm.safety import (
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    DirectGrant,
    EffectStatus,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    SafetyKernel,
    SubPlan,
    Verification,
    new_direct_grant,
)
from adhelm.saved_conversion_contracts import retain_conversion_contract
from adhelm.saved_ga4_admin_edit_inventories import retain_ga4_admin_edit_inventory
from adhelm.saved_ga4_key_event_candidates import retain_ga4_key_event_candidate

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_EVIDENCE = "evidence:sha256:" + "e" * 64


class _ApprovalHost:
    host_ref = "ultra-review-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return grant.user_interaction_ref == "ultra-review-interaction"

    def verifies_policy_configuration(self, _policy: object) -> bool:
        return False


class _BaseGa4Adapter:
    provider = "ga4_admin"

    def __init__(self, plan: CompositePlan) -> None:
        self._subplan = plan.subplans[0]
        self._state = self._subplan.before
        self.apply_calls = 0
        self.guarded_calls = 0
        self.verify_calls = 0
        self.capability_calls = 0
        self.identity_calls = 0
        self.read_calls = 0

    def capabilities(self, subplan: SubPlan) -> AdapterCapabilities:
        self.capability_calls += 1
        assert subplan == self._subplan
        return AdapterCapabilities(
            provider_api_version=self._subplan.provider_api_version,
            capability_version=self._subplan.capability_version,
            supported_operations=(self._subplan.operation,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        self.identity_calls += 1
        account = self._subplan.account
        assert account_ref == account.account_ref
        return RemoteIdentity(
            stable_account_id=account.stable_account_id,
            auth_profile_ref=account.auth_profile_ref,
            principal_ref=account.principal_ref,
            client_application_ref=account.client_application_ref,
            scopes=account.scopes,
        )

    def read_state(self, subplan: SubPlan) -> RemoteState:
        self.read_calls += 1
        assert subplan == self._subplan
        return RemoteState(fingerprint=self._state.fingerprint, revision=self._state.revision)

    def apply(self, _subplan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del idempotency_key
        self.apply_calls += 1
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED)

    def verify(self, _subplan: SubPlan) -> Verification:
        self.verify_calls += 1
        # This stands for a stale or forged success assertion.  A GA4 CREATE
        # ambiguity must not even consult it.
        return Verification(verified=True, evidence_refs=(_EVIDENCE,))

    def reconcile(self, _subplan: SubPlan) -> Reconciliation:
        return Reconciliation(
            state=RemoteState(
                fingerprint=self._state.fingerprint,
                revision=self._state.revision,
            ),
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )


class _AmbiguousGuardedGa4Adapter(_BaseGa4Adapter):
    def __init__(self, plan: CompositePlan, *, outcome: ApplyOutcome) -> None:
        super().__init__(plan)
        self._outcome = outcome

    def apply_guarded(
        self, subplan: SubPlan, *, idempotency_key: str, permit: object
    ) -> ApplyResponse:
        del idempotency_key
        assert subplan == self._subplan
        # The test cannot forge the real permit; its existence here proves the
        # kernel reached the guarded call only after durable APPLY_STARTED.
        assert permit is not None
        self.guarded_calls += 1
        self._state = self._subplan.after
        return ApplyResponse(
            outcome=self._outcome,
            no_effect_proven=self._outcome is ApplyOutcome.FAILED,
            evidence_refs=(_EVIDENCE,) if self._outcome is ApplyOutcome.FAILED else (),
        )


def _kernel_with_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SafetyKernel, Ledger, CompositePlan, DirectGrant]:
    request, ledger, _store, _profile, _config, _current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    plan = retained.action.plan
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: _NOW)
    grant = new_direct_grant(
        authorization_id="ultra-review-direct-grant",
        plan=plan,
        grantor_ref="ultra-review-grantor",
        host_ref=_ApprovalHost.host_ref,
        user_interaction_ref="ultra-review-interaction",
        expires_at=_NOW + timedelta(minutes=4),
    )
    kernel.record_direct_grant(plan, grant, _ApprovalHost())
    return kernel, ledger, plan, grant


@pytest.mark.parametrize("outcome", [ApplyOutcome.AMBIGUOUS, ApplyOutcome.FAILED])
def test_nonconfirmed_ga4_create_with_matching_readback_cannot_use_stale_verify_to_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: ApplyOutcome
) -> None:
    kernel, ledger, plan, grant = _kernel_with_grant(tmp_path, monkeypatch)
    adapter = _AmbiguousGuardedGa4Adapter(plan, outcome=outcome)

    result = kernel.execute(plan, grant, {adapter.provider: adapter})

    assert result.recovery_required is True
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.guarded_calls == 1
    assert adapter.apply_calls == 0
    assert adapter.verify_calls == 0
    events = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
    ]
    assert [event["event_type"] for event in events][-3:] == [
        "APPLY_STARTED",
        "APPLY_RESULT",
        "VERIFICATION_RESULT",
    ]
    assert all(event["effect_status"] == "AMBIGUOUS_REMOTE_STATE" for event in events[-2:])


def test_non_guarded_ga4_create_rejects_before_preflight_reads_run_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, plan, grant = _kernel_with_grant(tmp_path, monkeypatch)
    adapter = _BaseGa4Adapter(plan)
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as raised:
        kernel.execute(plan, grant, {adapter.provider: adapter})

    assert raised.value.code == "ADAPTER_PERMIT_REQUIRED"
    assert adapter.capability_calls == 0
    assert adapter.identity_calls == 0
    assert adapter.read_calls == 0
    assert adapter.apply_calls == 0
    assert ledger.checkpoint()["checkpoint_digest"] == before


def _start_envelope(plan: CompositePlan) -> dict[str, object]:
    subplan = plan.subplans[0]
    from adhelm.canonical import digest

    return {
        "record_kind": "CHANGE_EVENT",
        "provider": "ga4_admin",
        "account_ref": subplan.account.account_ref,
        "run_id": plan.plan_id,
        "source": "LOCAL_CLI",
        "payload": {
            "event_type": "APPLY_STARTED",
            "operation": "CREATE",
            "origin": "LOCAL_CLI",
            "resource_type": "PLAN",
            "resource_ref": subplan.subplan_id,
            "plan_ref": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "subplan_digest": subplan.subplan_digest,
            "before_digest": subplan.before.fingerprint,
            "after_digest": subplan.after.fingerprint,
            "effect_status": "NO_REMOTE_WRITE",
            "spend_impact_minor": 0,
            "currency": subplan.max_spend.currency,
            "provider_api_version": subplan.provider_api_version,
            "connector_version": subplan.capability_version,
            "idempotency_ref": digest(
                {"plan": plan.plan_digest, "subplan": subplan.subplan_digest}
            ),
        },
    }


def test_start_history_rejects_duplicate_retained_plan_digest_before_accepting_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehashed/physical duplicate cannot make a start's plan source ambiguous."""

    request, ledger, _store, _profile, _config, _current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    records = ledger.checkpoint()["records_by_stream"]["changes"]
    plan_record = next(
        record for record in records if record["record_kind"] == "GA4_KEY_EVENT_CREATE_PLAN"
    )
    duplicate = deepcopy(plan_record)

    with pytest.raises(AdhelmError) as raised:
        validate_ga4_create_start_history(
            [*records, duplicate, _start_envelope(retained.action.plan)]
        )

    assert raised.value.code == "GA4_CREATE_START_SOURCE_CONFLICT"


@pytest.mark.parametrize("newer_source", ["conversion_contract", "edit_observation"])
def test_start_history_rechecks_the_latest_relevant_source_at_durable_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newer_source: str
) -> None:
    """A plan cannot turn an obsolete contract or EDIT read into a new write."""

    request, ledger, _store, profile, config, current, _calls, contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    if newer_source == "conversion_contract":
        retain_conversion_contract(
            ledger,
            _definition(
                version=2,
                supersedes=contract.content_digest,
                effective_from=current[0] - timedelta(days=1),
            ),
        )
    else:
        current[0] += timedelta(seconds=1)
        observed = observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id=retained.action.execution_binding.target.web_stream_client_ref,
            fingerprint_key=_KEY,
            expected_configuration_digest=digest(config),
        )
        retained_observation = Ga4AdminEditInventoryObservation(
            profile_ref=profile.profile_id,
            configuration_digest=digest(config),
            profile_configuration_digest=digest(profile),
            read_started_at=observed.read_started_at,
            read_finished_at=observed.read_finished_at,
            inventory=observed.inventory,
            inventory_digest=observed.inventory_digest,
            execution_observation=observed,
        )
        retain_ga4_admin_edit_inventory(
            ledger,
            retained_observation,
            fingerprint_key=_KEY,
        )

    with pytest.raises(AdhelmError) as raised:
        validate_ga4_create_start_history(
            [
                *ledger.checkpoint()["records_by_stream"]["changes"],
                _start_envelope(retained.action.plan),
            ]
        )

    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CONFLICT"


def test_current_source_check_requires_one_exact_retained_plan_not_a_matching_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An action is not bound merely because one of several equal records matches."""

    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    checkpoint = ledger.checkpoint()
    records = checkpoint["records_by_stream"]["changes"]
    plan_record = next(
        record for record in records if record["record_kind"] == "GA4_KEY_EVENT_CREATE_PLAN"
    )
    duplicate_checkpoint = deepcopy(checkpoint)
    duplicate_checkpoint["records_by_stream"]["changes"].append(deepcopy(plan_record))
    monkeypatch.setattr(ledger, "checkpoint", lambda: duplicate_checkpoint)

    with pytest.raises(AdhelmError) as raised:
        validate_current_ga4_create_sources(
            ledger,
            retained.action,
            fresh=request.fresh,
            fingerprint_key=_KEY,
            now=current[0],
        )

    assert raised.value.code == "GA4_CREATE_PLAN_NOT_RETAINED"


def test_later_parallel_candidate_does_not_invalidate_exact_selected_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidates are immutable alternatives, so only the selected one is pinned."""

    request, ledger, _store, _profile, _config, current, _calls, contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    retain_ga4_key_event_candidate(
        ledger,
        request.fresh,
        retained_contract=contract,
        retained_contract_record_digest=(retained.action.selection_binding.contract_record_digest),
    )
    records = ledger.checkpoint()["records_by_stream"]["changes"]

    # A later candidate is not a superseding source. The exact original
    # candidate, latest conversion definition, and selected EDIT observation
    # remain the sources that the CREATE start must bind.
    validate_ga4_create_start_history([*records, _start_envelope(retained.action.plan)])
    validate_current_ga4_create_sources(
        ledger,
        retained.action,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        now=current[0],
    )


def test_durable_start_append_rechecks_latest_contract_under_changes_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale plan cannot bypass the start hook by reaching Ledger.append directly."""

    request, ledger, _store, _profile, _config, current, _calls, contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    retain_conversion_contract(
        ledger,
        _definition(
            version=2,
            supersedes=contract.content_digest,
            effective_from=current[0] - timedelta(days=1),
        ),
    )
    start = _start_envelope(retained.action.plan)
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as raised:
        ledger.append(
            "changes",
            "CHANGE_EVENT",
            start["payload"],  # type: ignore[arg-type]
            provider="ga4_admin",
            account_ref=retained.action.plan.subplans[0].account.account_ref,
            run_id=retained.action.plan.plan_id,
            source="LOCAL_CLI",
        )

    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CONFLICT"
    assert ledger.checkpoint()["checkpoint_digest"] == before


def test_configured_guarded_adapter_requires_durable_start_before_provider_reads_or_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid in-memory permit alone cannot authorize the fixed CREATE adapter."""

    request, ledger, _store, _profile, _config, current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    scenario = _CreateScenario()
    _install_adapter_http(monkeypatch, scenario)
    monkeypatch.setattr(adapter_module, "export_key", lambda: _KEY)
    plan = retained.action.plan.subplans[0]

    with _configured_ga4_create_adapter(
        tmp_path,
        retained.action,
        ledger,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        clock=lambda: current[0],
    ) as adapter:
        with _kernel_permit(
            plan_digest=retained.action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref="ultra-no-start",
            check_current=lambda: None,
        ) as permit:
            with pytest.raises(AdhelmError) as raised:
                adapter.apply_guarded(plan, idempotency_key="ultra-no-start", permit=permit)

    assert raised.value.code == "KERNEL_START_REQUIRED"
    assert scenario.calls == []
    assert scenario.posts == 0

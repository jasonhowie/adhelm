"""Kernel-only containment for the fixed Meta campaign PAUSE action.

These tests use M40 actions, locally signed synthetic M42 receipts, and fake
adapters.  They do not construct a Meta provider client, make HTTP requests,
or add an execution entry point.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalHostConfiguration,
    build_meta_campaign_pause_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    DirectGrant,
    EffectStatus,
    EgressPermit,
    EgressTarget,
    Money,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    SafetyKernel,
    StateRef,
    SubPlan,
    Verification,
    _unattributable_attempt_state,
)
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1", signing_key_ref="approval-key:synthetic-meta-kernel"
)
_OPAQUE = "opaque:sha256:" + "a" * 64
_EVIDENCE = "evidence:sha256:" + "b" * 64


def _action(workspace: str) -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref=workspace,
        account_binding=MetaCampaignPauseAccountBinding(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone="UTC",
            currency="USD",
            account_status="1",
        ),
        execution_binding=MetaCampaignPauseExecutionBinding(
            profile_ref="meta-edit",
            client_application_ref="555555",
            principal_ref="meta_token_principal_v1." + "c" * 64,
            credential_binding_ref="credential-binding:sha256:" + "d" * 64,
            configuration_digest="e" * 64,
            profile_configuration_digest="f" * 64,
            inspected_at=_NOW - timedelta(seconds=3),
            expires_at=_NOW + timedelta(hours=1),
            data_access_expires_at=_NOW + timedelta(hours=2),
        ),
        observation_binding=MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654",
                campaign_client_ref="987654",
                account_client_ref="123456",
                configured_status="ACTIVE",
                effective_status="WITH_ISSUES",
            ),
            read_started_at=_NOW - timedelta(seconds=2),
            read_finished_at=_NOW - timedelta(seconds=1),
        ),
        expires_at=_NOW + timedelta(minutes=10),
    )


def _challenge(action: MetaCampaignPausePlannedAction) -> ApprovalChallenge:
    return build_meta_campaign_pause_challenge(
        action,
        grantor_ref="synthetic-customer-grantor",
        configuration=_CONFIGURATION,
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
        expires_at=_NOW + timedelta(minutes=4),
    )


def _signed_receipt(challenge: ApprovalChallenge, key: ec.EllipticCurvePrivateKey) -> bytes:
    signing = receipt_signing_payload(
        challenge,
        configuration=_CONFIGURATION,
        receipt_id="approval-receipt:" + "K" * 43,
        issued_at=_NOW,
    )
    payload = parse_json(signing.canonical_unsigned_receipt)
    assert type(payload) is dict
    signature = key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    payload["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(payload)


def _signed_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SafetyKernel, Ledger, MetaCampaignPausePlannedAction, DirectGrant]:
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _NOW.isoformat().replace("+00:00", "Z"))
    ledger = Ledger(tmp_path)
    action = _action(ledger.profile_ref)
    record_digest = retain_meta_campaign_pause_plan(ledger, action)["record_digest"]
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: _NOW)
    accepted = accept_approval_receipt(
        kernel,
        ledger,
        receipt=_signed_receipt(_challenge(action), key),
        expected_challenge=_challenge(action),
        trust=PinnedApprovalTrust(_CONFIGURATION, public, ledger.profile_ref),
        source_plan_record_digest=record_digest,
    )
    assert accepted.direct_grant is not None
    return kernel, ledger, action, accepted.direct_grant


class _MetaAdapterBase:
    provider = "meta_ads"

    def __init__(self, action: MetaCampaignPausePlannedAction, *, ledger: Ledger) -> None:
        self._action = action
        self._ledger = ledger
        self._state = RemoteState(
            fingerprint=action.plan.subplans[0].before.fingerprint,
            revision=action.plan.subplans[0].before.revision,
        )
        self.capability_calls = 0
        self.identity_calls = 0
        self.read_state_calls = 0
        self.apply_calls = 0
        self.verify_calls = 0
        self.reconcile_calls = 0

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        self.capability_calls += 1
        return AdapterCapabilities(
            provider_api_version=plan.provider_api_version,
            capability_version=plan.capability_version,
            supported_operations=(OperationKind.PAUSE,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        self.identity_calls += 1
        account = self._action.plan.subplans[0].account
        assert isinstance(account, AccountIdentity)
        assert account_ref == account.account_ref
        return RemoteIdentity(
            stable_account_id=account.stable_account_id,
            auth_profile_ref=account.auth_profile_ref,
            principal_ref=account.principal_ref,
            client_application_ref=account.client_application_ref,
            scopes=account.scopes,
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        self.read_state_calls += 1
        assert plan == self._action.plan.subplans[0]
        return self._state

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        pytest.fail("Meta PAUSE must never use the unguarded adapter method")

    def verify(self, plan: SubPlan) -> Verification:
        self.verify_calls += 1
        assert plan == self._action.plan.subplans[0]
        return Verification(verified=True, evidence_refs=(_EVIDENCE,))

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self.reconcile_calls += 1
        assert plan == self._action.plan.subplans[0]
        return Reconciliation(
            state=self._state,
            status=EffectStatus.APPLIED_VERIFIED,
            settled=True,
            evidence_refs=(_EVIDENCE,),
        )


class _GuardedMetaAdapter(_MetaAdapterBase):
    def __init__(
        self,
        action: MetaCampaignPausePlannedAction,
        *,
        ledger: Ledger,
        outcome: ApplyOutcome,
        state_after_apply: bool,
    ) -> None:
        super().__init__(action, ledger=ledger)
        self._outcome = outcome
        self._state_after_apply = state_after_apply

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        self.apply_calls += 1
        assert any(
            row["payload"].get("event_type") == "APPLY_STARTED"
            for row in self._ledger.records("changes")
        )
        permit.consume(
            plan_digest=self._action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        if self._state_after_apply:
            self._state = RemoteState(
                fingerprint=plan.after.fingerprint, revision=plan.after.revision
            )
        return ApplyResponse(
            outcome=self._outcome,
            opaque_resource_refs=(_OPAQUE,),
            evidence_refs=(_EVIDENCE,),
        )


class _UnguardedMetaAdapter(_MetaAdapterBase):
    """Deliberately has no `apply_guarded` member for the protocol check."""


class _FirstPreflightAdapter(_MetaAdapterBase):
    """A non-Meta first subplan used only to prove all-plan preflight ordering."""

    provider = "google_ads"

    def read_state(self, plan: SubPlan) -> RemoteState:
        self.read_state_calls += 1
        return RemoteState(fingerprint=plan.before.fingerprint, revision=plan.before.revision)


class _CrashAfterMetaStart(BaseException):
    """Synthetic process-loss boundary after durable start and possible egress."""


class _CrashAfterStartMetaAdapter(_GuardedMetaAdapter):
    def __init__(
        self,
        action: MetaCampaignPausePlannedAction,
        *,
        ledger: Ledger,
        state_after_crash: bool,
    ) -> None:
        super().__init__(
            action,
            ledger=ledger,
            outcome=ApplyOutcome.CONFIRMED,
            state_after_apply=state_after_crash,
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        self.apply_calls += 1
        permit.consume(
            plan_digest=self._action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        if self._state_after_apply:
            self._state = RemoteState(
                fingerprint=plan.after.fingerprint, revision=plan.after.revision
            )
        raise _CrashAfterMetaStart()


class _ContradictoryReconcileMetaAdapter(_GuardedMetaAdapter):
    """A later baseline claim cannot overwrite a durable verified Meta result."""

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self.reconcile_calls += 1
        assert plan == self._action.plan.subplans[0]
        return Reconciliation(
            state=self._state,
            status=EffectStatus.FAILED_NO_EFFECT,
            settled=True,
            evidence_refs=(_EVIDENCE,),
        )


def test_meta_pause_requires_a_guarded_adapter_before_any_adapter_interaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _UnguardedMetaAdapter(action, ledger=ledger)

    with pytest.raises(AdhelmError) as refused:
        kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert refused.value.code == "ADAPTER_PERMIT_REQUIRED"
    assert (
        adapter.capability_calls,
        adapter.identity_calls,
        adapter.read_state_calls,
        adapter.apply_calls,
    ) == (0, 0, 0, 0)
    assert not any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )


def test_full_preflight_rejects_later_unguarded_meta_before_any_first_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, _grant = _signed_material(tmp_path, monkeypatch)
    meta = _UnguardedMetaAdapter(action, ledger=ledger)
    first = _FirstPreflightAdapter(action, ledger=ledger)
    template = action.plan.subplans[0]
    first_subplan = SubPlan(
        subplan_id="preflight-first",
        provider="google_ads",
        operation=OperationKind.PAUSE,
        account=template.account,
        provider_api_version=template.provider_api_version,
        capability_version=template.capability_version,
        before=StateRef(fingerprint=template.before.fingerprint, revision=template.before.revision),
        after=StateRef(fingerprint=template.after.fingerprint, revision=template.after.revision),
        max_spend=Money(minor_units=0, currency="USD"),
        egress=(EgressTarget(host="graph.facebook.com", purpose="synthetic preflight only"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=template.expires_at,
    )
    multi = CompositePlan(
        plan_id="meta-preflight-only",
        profile_ref=action.plan.profile_ref,
        subplans=(first_subplan, template),
        expires_at=action.plan.expires_at,
    )

    with pytest.raises(AdhelmError) as refused:
        kernel._preflight_all(multi, {"google_ads": first, "meta_ads": meta})  # noqa: SLF001

    assert refused.value.code == "ADAPTER_PERMIT_REQUIRED"
    assert first.apply_calls == 0
    assert (
        meta.capability_calls,
        meta.identity_calls,
        meta.read_state_calls,
        meta.apply_calls,
    ) == (
        0,
        0,
        0,
        0,
    )
    assert not any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )


def test_signed_meta_grant_with_confirmed_guarded_fixture_can_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.recovery_required is False
    assert (adapter.apply_calls, adapter.verify_calls) == (1, 1)
    assert not ledger.unresolved_starts()


def test_ambiguous_meta_pause_never_becomes_verified_from_later_matching_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.AMBIGUOUS, state_after_apply=True
    )

    initial = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert initial.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert initial.recovery_required is True
    assert adapter.verify_calls == 0
    assert ledger.unresolved_starts()

    # Reconciliation records the current state and releases the global write
    # barrier (ticket S2, see docs/implementation/kernel-generalization.md).
    # It never settles or attributes this attempt: no RECONCILIATION_RESULT is
    # written, the status stays ambiguous, and the exact plan stays one-use.
    for _ in range(2):
        reconciled = kernel.reconcile_run(action.plan, grant, {adapter.provider: adapter})
        assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.reconcile_calls == 2
    assert not any(
        row["payload"].get("event_type") == "RECONCILIATION_RESULT"
        and row["payload"].get("subplan_digest") == action.plan.subplans[0].subplan_digest
        for row in ledger.records("changes")
    )
    recoveries = [
        row["payload"]
        for row in ledger.records("changes")
        if row["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert [row["attempt_effect_status"] for row in recoveries] == ["AMBIGUOUS_REMOTE_STATE"]
    assert recoveries[0]["actor_ref"] == "UNKNOWN"
    assert ledger.unresolved_starts() == []

    with pytest.raises(AdhelmError) as retry:
        kernel.execute(action.plan, grant, {adapter.provider: adapter})
    assert retry.value.code in {"AUTHORIZATION_ALREADY_CONSUMED", "PLAN_ALREADY_STARTED"}
    assert adapter.apply_calls == 1


def test_meta_pause_durable_verified_result_cannot_be_rewritten_by_later_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _ContradictoryReconcileMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )
    append_run = kernel._append_run  # noqa: SLF001

    def crash_before_run_verified(event_type: str, *args: object, **kwargs: object) -> None:
        if event_type == "RUN_VERIFIED":
            raise _CrashAfterMetaStart()
        append_run(event_type, *args, **kwargs)

    monkeypatch.setattr(kernel, "_append_run", crash_before_run_verified)

    with pytest.raises(_CrashAfterMetaStart):
        kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert any(
        row["payload"].get("event_type") == "APPLY_RESULT"
        and row["payload"].get("effect_status") == EffectStatus.APPLIED_VERIFIED.value
        for row in ledger.records("changes")
    )
    assert not ledger.unresolved_starts()
    assert ledger.unresolved_runs()
    baseline = action.plan.subplans[0].before
    adapter._state = RemoteState(fingerprint=baseline.fingerprint, revision=baseline.revision)

    reconciled = kernel.reconcile_run(action.plan, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciled.recovery_required is True
    assert adapter.reconcile_calls == 1
    assert not ledger.unresolved_starts()
    assert ledger.unresolved_runs()
    assert not any(
        row["payload"].get("event_type") == "RECONCILIATION_RESULT"
        and row["payload"].get("subplan_digest") == action.plan.subplans[0].subplan_digest
        for row in ledger.records("changes")
    )


def test_duplicate_exact_meta_apply_result_is_unsettled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    history = ledger.records("changes")
    history.append(
        next(row for row in history if row["payload"].get("event_type") == "APPLY_RESULT")
    )

    assert (
        _unattributable_attempt_state(  # noqa: SLF001
            history, plan=action.plan, authorization=grant, subplan=action.plan.subplans[0]
        )
        == "UNSETTLED"
    )


def test_sparse_meta_verification_result_is_unsettled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )
    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    history = ledger.records("changes")
    verification_index = next(
        index
        for index, row in enumerate(history)
        if row["payload"].get("event_type") == "VERIFICATION_RESULT"
    )
    verification = history[verification_index]
    history[verification_index] = {
        **verification,
        "payload": {**verification["payload"], "evidence_refs": []},
    }

    assert (
        _unattributable_attempt_state(  # noqa: SLF001
            history, plan=action.plan, authorization=grant, subplan=action.plan.subplans[0]
        )
        == "UNSETTLED"
    )


@pytest.mark.parametrize(
    ("event_type", "digest_field"),
    [
        ("APPLY_STARTED", "before_digest"),
        ("APPLY_RESULT", "after_digest"),
        ("VERIFICATION_RESULT", "before_digest"),
    ],
)
def test_meta_event_with_mismatched_planned_fingerprint_is_unsettled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_type: str, digest_field: str
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )
    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    history = ledger.records("changes")
    event_index = next(
        index for index, row in enumerate(history) if row["payload"].get("event_type") == event_type
    )
    event = history[event_index]
    expected = event["payload"][digest_field]
    assert isinstance(expected, str)
    replacement = ("0" if expected != "0" * len(expected) else "1") * len(expected)
    history[event_index] = {
        **event,
        "payload": {**event["payload"], digest_field: replacement},
    }

    assert (
        _unattributable_attempt_state(  # noqa: SLF001
            history, plan=action.plan, authorization=grant, subplan=action.plan.subplans[0]
        )
        == "UNSETTLED"
    )


def test_valid_meta_sequence_plus_wrong_fingerprint_duplicate_is_unsettled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )
    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    history = ledger.records("changes")
    terminal = next(row for row in history if row["payload"].get("event_type") == "APPLY_RESULT")
    expected = terminal["payload"]["after_digest"]
    assert isinstance(expected, str)
    replacement = ("0" if expected != "0" * len(expected) else "1") * len(expected)
    history.append(
        {
            **terminal,
            "payload": {**terminal["payload"], "after_digest": replacement},
        }
    )

    assert (
        _unattributable_attempt_state(  # noqa: SLF001
            history, plan=action.plan, authorization=grant, subplan=action.plan.subplans[0]
        )
        == "UNSETTLED"
    )


@pytest.mark.parametrize(
    "history_shape", ["repeated-start", "result-before-start", "result-without-start"]
)
def test_incoherent_meta_attempt_order_is_unsettled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history_shape: str
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )
    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    history = ledger.records("changes")
    start = next(row for row in history if row["payload"].get("event_type") == "APPLY_STARTED")
    apply_result = next(
        row for row in history if row["payload"].get("event_type") == "APPLY_RESULT"
    )
    if history_shape == "repeated-start":
        history.insert(history.index(start) + 1, start)
    elif history_shape == "result-before-start":
        history.remove(apply_result)
        history.insert(history.index(start), apply_result)
    else:
        history.remove(start)

    assert (
        _unattributable_attempt_state(  # noqa: SLF001
            history, plan=action.plan, authorization=grant, subplan=action.plan.subplans[0]
        )
        == "UNSETTLED"
    )


def test_meta_result_without_durable_verification_remains_unsettled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _GuardedMetaAdapter(
        action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
    )
    append_change = kernel._append_change  # noqa: SLF001

    def crash_before_verification(*, event_type: str, **kwargs: object) -> None:
        if event_type == "VERIFICATION_RESULT":
            raise _CrashAfterMetaStart()
        append_change(event_type=event_type, **kwargs)

    monkeypatch.setattr(kernel, "_append_change", crash_before_verification)

    with pytest.raises(_CrashAfterMetaStart):
        kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert any(
        row["payload"].get("event_type") == "APPLY_RESULT"
        and row["payload"].get("effect_status") == EffectStatus.APPLIED_VERIFIED.value
        for row in ledger.records("changes")
    )
    assert not any(
        row["payload"].get("event_type") == "VERIFICATION_RESULT"
        for row in ledger.records("changes")
    )
    assert ledger.unresolved_starts()

    reconciled = kernel.reconcile_run(action.plan, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciled.recovery_required is True
    assert ledger.unresolved_starts()


@pytest.mark.parametrize("state_after_crash", [False, True], ids=["baseline", "paused"])
def test_started_meta_pause_without_result_never_settles_from_later_campaign_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_after_crash: bool
) -> None:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _CrashAfterStartMetaAdapter(
        action, ledger=ledger, state_after_crash=state_after_crash
    )

    with pytest.raises(_CrashAfterMetaStart):
        kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )
    assert not any(
        row["payload"].get("event_type") == "APPLY_RESULT" for row in ledger.records("changes")
    )
    assert ledger.unresolved_starts()

    reconciled = kernel.reconcile_run(action.plan, grant, {adapter.provider: adapter})

    assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciled.recovery_required is True
    assert adapter.reconcile_calls == 1
    assert ledger.unresolved_starts()
    assert not any(
        row["payload"].get("event_type") == "RECONCILIATION_RESULT"
        and row["payload"].get("subplan_digest") == action.plan.subplans[0].subplan_digest
        for row in ledger.records("changes")
    )

"""Hermetic recovery regressions found during the independent safety review."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm.audit import AuditPolicy, audit_report
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Money as ReportMoney
from adhelm.reporting import (
    ConversionBreakdownRow,
    NormalizedReport,
    ReportEnvelope,
)
from adhelm.reporting import (
    PerformanceRow as ReportPerformanceRow,
)
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    DirectGrant,
    EffectStatus,
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
    new_direct_grant,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

NOW = datetime(2030, 1, 1, tzinfo=UTC)
BEFORE = "a" * 64
AFTER = "b" * 64
DRIFTED = "c" * 64
EVIDENCE = "evidence:sha256:" + "d" * 64
AUDIT_SOURCE_DIGEST = "e" * 64
AUDIT_OBSERVED = datetime(2030, 1, 1, 12, tzinfo=UTC)


class _ApprovalHost:
    host_ref = "review-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        del grant
        return True

    def verifies_policy_configuration(self, policy: object) -> bool:
        del policy
        return True


class _Adapter:
    def __init__(
        self,
        *,
        provider: str,
        stable_account_id: str,
        state: str = BEFORE,
        post_apply_state: str = AFTER,
        after_apply: Callable[[], None] | None = None,
        reported_stable_account_id: str | None = None,
        reconciliation_status: EffectStatus | None = None,
        reconciliation_settled: bool | None = None,
    ) -> None:
        self.provider = provider
        self.stable_account_id = stable_account_id
        self.state = state
        self.post_apply_state = post_apply_state
        self.after_apply = after_apply
        self.reported_stable_account_id = reported_stable_account_id
        self.reconciliation_status = reconciliation_status
        self.reconciliation_settled = reconciliation_settled
        self.apply_calls = 0
        # Set by a test that expects a CONFIRMED acknowledgement to count: the
        # kernel now treats a confirmed result whose permit was never claimed as
        # ambiguous, exactly as a real adapter that bypassed the guarded seam.
        self.plan_digest: str | None = None

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        del plan
        return AdapterCapabilities(
            provider_api_version="v1",
            capability_version="cap-v1",
            supported_operations=(OperationKind.CREATE,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        assert account_ref == f"alias-{self.provider}"
        return RemoteIdentity(
            stable_account_id=self.reported_stable_account_id or self.stable_account_id,
            auth_profile_ref="edit-profile",
            principal_ref="principal",
            client_application_ref="client",
            scopes=("ads.write",),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        del plan
        return RemoteState(fingerprint=self.state, revision="revision-1")

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        self.apply_calls += 1
        self.state = self.post_apply_state
        if self.after_apply is not None:
            self.after_apply()
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED)

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        """The uniform write rule routes every provider through a guarded seam.

        These recovery regressions are about composite ordering and barriers.
        The permit-claim protocol itself is covered by ``test_egress_permit`` and
        by the real guarded provider adapters, so this fake only proves that the
        kernel refuses to reach any provider without issuing one.
        """

        assert permit is not None
        if self.plan_digest is not None:
            permit.consume(
                plan_digest=self.plan_digest,
                subplan_digest=plan.subplan_digest,
                attempt_ref=idempotency_key,
            )
        return self.apply(plan, idempotency_key=idempotency_key)

    def verify(self, plan: SubPlan) -> Verification:
        del plan
        return Verification(verified=True, evidence_refs=(EVIDENCE,))

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        del plan
        status = self.reconciliation_status
        if status is None:
            status = (
                EffectStatus.APPLIED_VERIFIED
                if self.state == AFTER
                else EffectStatus.FAILED_NO_EFFECT
                if self.state == BEFORE
                else EffectStatus.PARTIAL_EFFECT
            )
        settled = self.reconciliation_settled
        if settled is None:
            settled = status is not EffectStatus.AMBIGUOUS_REMOTE_STATE
        evidence = (EVIDENCE,) if settled else ()
        return Reconciliation(
            state=RemoteState(fingerprint=self.state, revision="revision-1"),
            status=status,
            settled=settled,
            evidence_refs=evidence,
        )


class _EventuallyConsistentAmbiguousAdapter(_Adapter):
    """The request may apply later, while its first provider read is still stale."""

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        self.apply_calls += 1
        # Deliberately retain BEFORE for the immediate re-read.  A real provider
        # can expose this window after a timeout or an accepted asynchronous write.
        return ApplyResponse(outcome=ApplyOutcome.AMBIGUOUS)

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        del plan
        return Reconciliation(
            state=RemoteState(fingerprint=self.state, revision="revision-1"),
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )


def _subplan(
    *,
    provider: str,
    stable_account_id: str,
    subplan_id: str,
    before: str = BEFORE,
    after: str = AFTER,
) -> SubPlan:
    return SubPlan(
        subplan_id=subplan_id,
        provider=provider,
        operation=OperationKind.CREATE,
        account=AccountIdentity(
            account_ref=f"alias-{provider}",
            stable_account_id=stable_account_id,
            auth_profile_ref="edit-profile",
            principal_ref="principal",
            client_application_ref="client",
            scopes=("ads.write",),
        ),
        provider_api_version="v1",
        capability_version="cap-v1",
        before=StateRef(fingerprint=before, revision="revision-1"),
        after=StateRef(fingerprint=after, revision="revision-1"),
        max_spend=Money(minor_units=100, currency="USD"),
        egress=(EgressTarget(host="api.provider.test", purpose="paused creation"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=NOW + timedelta(hours=1),
    )


def _plan(plan_id: str, *subplans: SubPlan) -> CompositePlan:
    return CompositePlan(
        plan_id=plan_id,
        profile_ref="review-profile",
        subplans=subplans,
        expires_at=NOW + timedelta(hours=1),
    )


def _grant(
    plan: CompositePlan,
    authorization_id: str,
    *,
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> DirectGrant:
    return new_direct_grant(
        authorization_id=authorization_id,
        plan=plan,
        grantor_ref="marketer",
        host_ref="review-host",
        user_interaction_ref="approved-review",
        expires_at=expires_at,
    )


def _kernel(tmp_path: Path) -> tuple[SafetyKernel, Ledger]:
    ledger = Ledger(tmp_path, profile_ref="review-profile")
    return SafetyKernel(tmp_path, ledger, clock=lambda: NOW), ledger


def _audit_envelope() -> ReportEnvelope:
    return ReportEnvelope(
        report_id="audit-report",
        provider="google_ads",
        source_account_ref="audit-account",
        query_contract_hash="f" * 64,
        window_start=date(2029, 12, 30),
        window_end=date(2029, 12, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="DECLARED_PROVIDER_MODEL",
        attribution_window="7d_click",
        observed_at=AUDIT_OBSERVED,
        data_quality_statuses=["COMPLETE"],
        provider_api_version="v1",
        connector_version="review",
    )


def _audit_policy(*, maximum_cpc: ReportMoney | None = None) -> AuditPolicy:
    return AuditPolicy(
        maximum_observation_age_seconds=86_400,
        maximum_cpc=maximum_cpc,
        minimum_impressions=None,
        require_conversion_evidence=True,
    )


def test_ambiguous_write_with_an_immediately_unchanged_read_is_not_no_effect(
    tmp_path: Path,
) -> None:
    """A stale post-write read cannot prove that a timed-out request had no effect."""
    kernel, ledger = _kernel(tmp_path)
    subplan = _subplan(
        provider="google_ads", stable_account_id="stable-google", subplan_id="google-create"
    )
    compiled = _plan("eventual-consistency", subplan)
    grant = _grant(compiled, "grant-eventual")
    kernel.record_direct_grant(compiled, grant, _ApprovalHost())
    adapter = _EventuallyConsistentAmbiguousAdapter(
        provider="google_ads", stable_account_id="stable-google"
    )

    result = kernel.execute(compiled, grant, {adapter.provider: adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    apply_results = [
        record["payload"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
        and record["payload"]["event_type"] == "APPLY_RESULT"
    ]
    assert apply_results[-1]["effect_status"] == EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert ledger.unresolved_starts()


def test_partial_run_reconciles_every_selected_provider_after_restart_with_expired_grant(
    tmp_path: Path,
) -> None:
    """A cross-provider partial run needs a durable all-provider recovery barrier."""
    kernel, ledger = _kernel(tmp_path)
    meta = _Adapter(provider="meta_ads", stable_account_id="stable-meta")
    google = _Adapter(
        provider="google_ads",
        stable_account_id="stable-google",
        after_apply=lambda: setattr(meta, "state", DRIFTED),
    )
    first = _plan(
        "partial-after-google",
        _subplan(
            provider="google_ads", stable_account_id="stable-google", subplan_id="google-create"
        ),
        _subplan(provider="meta_ads", stable_account_id="stable-meta", subplan_id="meta-create"),
    )
    google.plan_digest = meta.plan_digest = first.plan_digest
    first_grant = _grant(first, "grant-partial")
    kernel.record_direct_grant(first, first_grant, _ApprovalHost())

    # The initial all-provider preflight passes.  Google then verifies, but its
    # side effect makes Meta drift before Meta's locked pre-egress check.
    outcome = kernel.execute(first, first_grant, {google.provider: google, meta.provider: meta})
    assert [result.status for result in outcome.results] == [
        EffectStatus.APPLIED_VERIFIED,
        EffectStatus.NOT_STARTED,
    ]
    assert outcome.results[1].diagnostic_code == "REMOTE_DRIFT"
    assert outcome.recovery_required
    assert google.apply_calls == 1
    assert meta.apply_calls == 0

    # Reads must still be possible after the exact write grant has expired and
    # after a process restart, because otherwise an expired partial run wedges
    # the local profile forever.
    restarted = SafetyKernel(tmp_path, ledger, clock=lambda: NOW + timedelta(minutes=20))
    reconciliation = restarted.reconcile_run(
        first, first_grant, {google.provider: google, meta.provider: meta}
    )
    assert [result.status for result in reconciliation.results] == [
        EffectStatus.APPLIED_VERIFIED,
        EffectStatus.PARTIAL_EFFECT,
    ]
    assert reconciliation.recovery_required is False
    assert not ledger.unresolved_runs()

    fresh = _plan(
        "must-wait-for-partial-reconciliation",
        _subplan(
            provider="linkedin_ads",
            stable_account_id="stable-linkedin",
            subplan_id="linkedin-create",
        ),
    )
    fresh_grant = _grant(fresh, "grant-fresh", expires_at=NOW + timedelta(minutes=30))
    restarted.record_direct_grant(fresh, fresh_grant, _ApprovalHost())
    fresh_adapter = _Adapter(provider="linkedin_ads", stable_account_id="stable-linkedin")

    restarted.execute(fresh, fresh_grant, {fresh_adapter.provider: fresh_adapter})
    assert fresh_adapter.apply_calls == 1


def test_incomplete_reconciliation_keeps_the_composite_barrier_across_restart(
    tmp_path: Path,
) -> None:
    """An unsettled provider cannot be hidden by a settled sibling's proof."""
    kernel, ledger = _kernel(tmp_path)
    google = _EventuallyConsistentAmbiguousAdapter(
        provider="google_ads", stable_account_id="stable-google"
    )
    meta = _Adapter(provider="meta_ads", stable_account_id="stable-meta")
    first = _plan(
        "ambiguous-google",
        _subplan(
            provider="google_ads", stable_account_id="stable-google", subplan_id="google-create"
        ),
        _subplan(provider="meta_ads", stable_account_id="stable-meta", subplan_id="meta-create"),
    )
    first_grant = _grant(first, "grant-ambiguous")
    kernel.record_direct_grant(first, first_grant, _ApprovalHost())

    outcome = kernel.execute(first, first_grant, {google.provider: google, meta.provider: meta})
    assert outcome.recovery_required
    assert meta.apply_calls == 0

    restarted = SafetyKernel(tmp_path, ledger, clock=lambda: NOW + timedelta(minutes=20))
    reconciliation = restarted.reconcile_run(
        first, first_grant, {google.provider: google, meta.provider: meta}
    )
    assert reconciliation.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciliation.recovery_required
    assert ledger.unresolved_runs()

    fresh = _plan(
        "blocked-by-unsettled-provider",
        _subplan(
            provider="linkedin_ads",
            stable_account_id="stable-linkedin",
            subplan_id="linkedin-create",
        ),
    )
    fresh_grant = _grant(fresh, "grant-blocked", expires_at=NOW + timedelta(minutes=30))
    restarted.record_direct_grant(fresh, fresh_grant, _ApprovalHost())
    fresh_adapter = _Adapter(provider="linkedin_ads", stable_account_id="stable-linkedin")

    with pytest.raises(AdhelmError, match="(?i)reconcil"):
        restarted.execute(fresh, fresh_grant, {fresh_adapter.provider: fresh_adapter})
    assert fresh_adapter.apply_calls == 0


def test_reconciliation_identity_mismatch_cannot_clear_a_composite_barrier(tmp_path: Path) -> None:
    """A fresh adapter must prove the same account identity before a run can close."""
    kernel, ledger = _kernel(tmp_path)
    google = _EventuallyConsistentAmbiguousAdapter(
        provider="google_ads", stable_account_id="stable-google"
    )
    first = _plan(
        "identity-mismatch",
        _subplan(
            provider="google_ads", stable_account_id="stable-google", subplan_id="google-create"
        ),
    )
    grant = _grant(first, "grant-identity")
    kernel.record_direct_grant(first, grant, _ApprovalHost())
    kernel.execute(first, grant, {google.provider: google})

    wrong_identity = _Adapter(
        provider="google_ads",
        stable_account_id="stable-google",
        reported_stable_account_id="different-account",
    )
    restarted = SafetyKernel(tmp_path, ledger, clock=lambda: NOW + timedelta(minutes=20))
    reconciliation = restarted.reconcile_run(
        first, grant, {wrong_identity.provider: wrong_identity}
    )

    assert reconciliation.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciliation.results[0].diagnostic_code == "RECONCILIATION_UNPROVEN"
    assert reconciliation.recovery_required
    assert ledger.unresolved_runs()


def test_reconciliation_never_credits_an_unstarted_provider_as_applied(tmp_path: Path) -> None:
    """A matching state alone does not prove that this run caused an unstarted change."""
    kernel, ledger = _kernel(tmp_path)
    meta = _Adapter(provider="meta_ads", stable_account_id="stable-meta")
    google = _Adapter(
        provider="google_ads",
        stable_account_id="stable-google",
        post_apply_state=DRIFTED,
        after_apply=lambda: setattr(meta, "state", AFTER),
    )
    first = _plan(
        "external-meta-after-google",
        _subplan(
            provider="google_ads", stable_account_id="stable-google", subplan_id="google-create"
        ),
        _subplan(provider="meta_ads", stable_account_id="stable-meta", subplan_id="meta-create"),
    )
    google.plan_digest = meta.plan_digest = first.plan_digest
    grant = _grant(first, "grant-external-meta")
    kernel.record_direct_grant(first, grant, _ApprovalHost())

    outcome = kernel.execute(first, grant, {google.provider: google, meta.provider: meta})
    assert [result.status for result in outcome.results] == [
        EffectStatus.PARTIAL_EFFECT,
        EffectStatus.NOT_STARTED,
    ]
    assert meta.apply_calls == 0

    restarted = SafetyKernel(tmp_path, ledger, clock=lambda: NOW + timedelta(minutes=20))
    reconciliation = restarted.reconcile_run(
        first, grant, {google.provider: google, meta.provider: meta}
    )

    assert reconciliation.results[1].status is not EffectStatus.APPLIED_VERIFIED
    local_credit = [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
        and record["provider"] == "meta_ads"
        and record["payload"]["event_type"] == "RECONCILIATION_RESULT"
        and record["payload"]["effect_status"] == "APPLIED_VERIFIED"
    ]
    assert local_credit == []


def test_ambiguous_subplan_never_continues_to_later_provider_egress(tmp_path: Path) -> None:
    """An execution-policy flag cannot permit writes after an ambiguous attempt."""
    kernel, _ = _kernel(tmp_path)
    first_subplan = _subplan(
        provider="google_ads", stable_account_id="stable-google", subplan_id="google-create"
    )
    later_subplan = _subplan(
        provider="meta_ads", stable_account_id="stable-meta", subplan_id="meta-create"
    )
    try:
        compiled = CompositePlan(
            plan_id="continue-after-ambiguous",
            profile_ref="review-profile",
            subplans=(first_subplan, later_subplan),
            expires_at=NOW + timedelta(hours=1),
            stop_on_non_verified=False,
        )
    except ValidationError:
        # Rejecting the unsafe execution policy before egress is also conformant.
        return
    grant = _grant(compiled, "grant-continue")
    kernel.record_direct_grant(compiled, grant, _ApprovalHost())
    ambiguous = _EventuallyConsistentAmbiguousAdapter(
        provider="google_ads", stable_account_id="stable-google"
    )
    later = _Adapter(provider="meta_ads", stable_account_id="stable-meta")

    result = kernel.execute(compiled, grant, {ambiguous.provider: ambiguous, later.provider: later})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert later.apply_calls == 0
    assert result.results[1].status is EffectStatus.NOT_STARTED


def test_ga4_key_event_count_cannot_satisfy_native_conversion_evidence_policy() -> None:
    """GA4 aggregate counts do not prove a provider-native conversion mapping or receipt."""
    report = NormalizedReport(
        envelope=_audit_envelope(),
        conversion_rows=[
            ConversionBreakdownRow(
                report_id="audit-report",
                conversion_source="GA4",
                conversion_action_ref="ga4.key-event.purchase",
                conversion_kind="KEY_EVENT",
                attribution_model="GA4_DATA_API",
                conversion_count=7,
                data_quality_statuses=["COMPLETE"],
            )
        ],
    )

    result = audit_report(
        report,
        source_record_digest=AUDIT_SOURCE_DIGEST,
        policy=_audit_policy(),
        as_of=AUDIT_OBSERVED,
    )

    assert "CONVERSION_EVIDENCE_MISSING" in {finding.finding_type for finding in result.findings}
    coverage = {item.dimension: item.status for item in result.coverage}
    assert coverage["NATIVE_CONVERSION"] != "CHECKED"


def test_google_ads_cpc_boundary_uses_exact_retained_micros() -> None:
    """A rounded cents display must not hide a source-micro-unit threshold breach."""
    row = ReportPerformanceRow(
        report_id="audit-report",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="GOOGLE_ADS_CAMPAIGN",
        resource_ref="google_ads.campaign.1",
        campaign_ref="google_ads.campaign.1",
        impressions=100,
        clicks=2,
        spend=ReportMoney(minor_units=2, currency="USD"),
        data_quality_statuses=["COMPLETE"],
        provider_extensions={
            "google_ads_cost_micros": "20001",
            "google_ads_rounding": "HALF_EVEN_TO_MINOR",
        },
    )
    report = NormalizedReport(envelope=_audit_envelope(), performance_rows=[row])

    result = audit_report(
        report,
        source_record_digest=AUDIT_SOURCE_DIGEST,
        policy=_audit_policy(maximum_cpc=ReportMoney(minor_units=1, currency="USD")),
        as_of=AUDIT_OBSERVED,
    )

    assert "CPC_ABOVE_DECLARED_MAXIMUM" in {finding.finding_type for finding in result.findings}

"""Regression probes for safety properties found during independent review."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Money as ReportMoney
from adhelm.projections import _redact_payload, export_verified
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope
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
from adhelm.transport import SafeHttpClient

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

BASE = datetime(2030, 1, 1, tzinfo=UTC)
BEFORE = "a" * 64
AFTER = "b" * 64
EVIDENCE = "evidence:sha256:" + "d" * 64


class _ApprovalHost:
    host_ref = "trusted-local-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return True

    def verifies_policy_configuration(self, policy: object) -> bool:
        del policy
        return True


class _Adapter:
    provider = "google_ads"

    def __init__(
        self, *, ambiguous_after_apply: bool = False, response_ref: str | None = None
    ) -> None:
        self.ambiguous_after_apply = ambiguous_after_apply
        self.response_ref = response_ref
        self.state = BEFORE
        self.apply_calls = 0

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        del plan
        return AdapterCapabilities(
            provider_api_version="v1",
            capability_version="cap-v1",
            supported_operations=(OperationKind.CREATE,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        assert account_ref == "account-alias"
        return RemoteIdentity(
            stable_account_id="stable-account",
            auth_profile_ref="edit-profile",
            principal_ref="principal",
            client_application_ref="client",
            scopes=("ads.write",),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        del plan
        if self.ambiguous_after_apply and self.apply_calls:
            raise ConnectionError("post-write read is unavailable")
        return RemoteState(fingerprint=self.state, revision="revision-1")

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        self.apply_calls += 1
        if not self.ambiguous_after_apply:
            self.state = AFTER
        refs = () if self.response_ref is None else (self.response_ref,)
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, opaque_resource_refs=refs)

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        """No provider reaches egress without a kernel-issued permit."""

        assert permit is not None
        return self.apply(plan, idempotency_key=idempotency_key)

    def verify(self, plan: SubPlan) -> Verification:
        del plan
        return Verification(verified=True)

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        del plan
        settled = not self.ambiguous_after_apply
        return Reconciliation(
            state=RemoteState(fingerprint=self.state, revision="revision-1"),
            status=EffectStatus.APPLIED_VERIFIED
            if self.state == AFTER
            else EffectStatus.FAILED_NO_EFFECT
            if settled
            else EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=settled,
            evidence_refs=(EVIDENCE,) if settled else (),
        )


def _plan(*, plan_id: str = "plan-one") -> CompositePlan:
    subplan = SubPlan(
        subplan_id="google-create",
        provider="google_ads",
        operation=OperationKind.CREATE,
        account=AccountIdentity(
            account_ref="account-alias",
            stable_account_id="stable-account",
            auth_profile_ref="edit-profile",
            principal_ref="principal",
            client_application_ref="client",
            scopes=("ads.write",),
        ),
        provider_api_version="v1",
        capability_version="cap-v1",
        before=StateRef(fingerprint=BEFORE, revision="revision-1"),
        after=StateRef(fingerprint=AFTER, revision="revision-1"),
        max_spend=Money(minor_units=100, currency="USD"),
        egress=(EgressTarget(host="api.google.test", purpose="paused creation"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=BASE + timedelta(hours=1),
    )
    return CompositePlan(
        plan_id=plan_id,
        profile_ref="review-profile",
        subplans=(subplan,),
        expires_at=BASE + timedelta(hours=1),
    )


def _grant(plan: CompositePlan, authorization_id: str, *, expires_at: datetime) -> DirectGrant:
    return new_direct_grant(
        authorization_id=authorization_id,
        plan=plan,
        grantor_ref="marketer",
        host_ref="trusted-local-host",
        user_interaction_ref="reviewed-interaction",
        expires_at=expires_at,
    )


def _kernel(tmp_path: Path, now: datetime) -> tuple[SafetyKernel, Ledger]:
    ledger = Ledger(tmp_path, profile_ref="review-profile")
    return SafetyKernel(tmp_path, ledger, clock=lambda: now), ledger


def test_durable_direct_grant_cannot_be_extended_by_reconstructed_input(tmp_path: Path) -> None:
    """The durable approval's expiry, not caller input, controls later execution."""
    compiled = _plan()
    initial, ledger = _kernel(tmp_path, BASE)
    recorded = _grant(compiled, "direct-expiry", expires_at=BASE + timedelta(minutes=1))
    initial.record_direct_grant(compiled, recorded, _ApprovalHost())

    reconstructed = recorded.model_copy(update={"expires_at": BASE + timedelta(days=1)})
    later = SafetyKernel(tmp_path, ledger, clock=lambda: BASE + timedelta(minutes=2))
    adapter = _Adapter()

    with pytest.raises(AdhelmError):
        later.execute(compiled, reconstructed, {adapter.provider: adapter})
    assert adapter.apply_calls == 0


def test_ambiguous_attempt_blocks_a_new_grant_until_reconciliation(tmp_path: Path) -> None:
    """Changing a grant or plan ID cannot turn an ambiguous write into a retry."""
    first_plan = _plan(plan_id="first-plan")
    kernel, ledger = _kernel(tmp_path, BASE)
    first_grant = _grant(first_plan, "direct-first", expires_at=BASE + timedelta(minutes=10))
    kernel.record_direct_grant(first_plan, first_grant, _ApprovalHost())
    ambiguous = _Adapter(ambiguous_after_apply=True)

    first_result = kernel.execute(first_plan, first_grant, {ambiguous.provider: ambiguous})
    assert first_result.results[0].status.value == "AMBIGUOUS_REMOTE_STATE"

    next_plan = _plan(plan_id="second-plan")
    next_grant = _grant(next_plan, "direct-second", expires_at=BASE + timedelta(minutes=10))
    kernel.record_direct_grant(next_plan, next_grant, _ApprovalHost())
    retry = _Adapter()

    with pytest.raises(AdhelmError):
        kernel.execute(next_plan, next_grant, {retry.provider: retry})
    assert retry.apply_calls == 0
    assert ledger.unresolved_starts()


def test_adapter_response_cannot_return_sensitive_resource_reference(tmp_path: Path) -> None:
    """Untrusted provider-returned strings must be rejected or redacted before output."""
    compiled = _plan()
    kernel, _ = _kernel(tmp_path, BASE)
    grant = _grant(compiled, "direct-response", expires_at=BASE + timedelta(minutes=10))
    kernel.record_direct_grant(compiled, grant, _ApprovalHost())
    adapter = _Adapter(response_ref="person@example.com")

    try:
        result = kernel.execute(compiled, grant, {adapter.provider: adapter})
    except AdhelmError:
        return
    assert "person@example.com" not in str(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "kwargs",
    [{"json": {"email": "person@example.com"}}, {"params": {"email": "person@example.com"}}],
)
def test_transport_rejects_sensitive_request_data_before_egress(kwargs: dict[str, Any]) -> None:
    """Host allowlisting cannot make arbitrary body/query data safe to transmit."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    client = SafeHttpClient(httpx.Client(transport=httpx.MockTransport(handler)), {"provider.test"})
    with pytest.raises(AdhelmError):
        client.request("POST", "https://provider.test/fixed-operation", mutation=True, **kwargs)
    assert called is False


def test_export_rejects_a_symlinked_destination_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    """A user-selected artifact destination cannot silently traverse a symlink."""
    ledger = Ledger(tmp_path / "profile", profile_ref="review-profile")
    target = tmp_path / "unrelated-target"
    target.mkdir()
    destination = tmp_path / "export-link"
    destination.symlink_to(target, target_is_directory=True)

    with pytest.raises(AdhelmError):
        export_verified(ledger, destination, opaque_key=b"x" * 32)

    assert list(target.iterdir()) == []


def test_ledger_rejects_a_symlinked_history_root_before_creating_records(tmp_path: Path) -> None:
    """Canonical history must not silently traverse an attacker-controlled link."""
    target = tmp_path / "unrelated-history-target"
    target.mkdir()
    (tmp_path / "ledger").symlink_to(target, target_is_directory=True)
    ledger = Ledger(tmp_path)

    with pytest.raises(AdhelmError) as raised:
        ledger.append(
            "changes",
            "CHANGE_EVENT",
            {
                "event_type": "PLAN_CREATED",
                "operation": "CREATE",
                "origin": "LOCAL_CLI",
                "resource_type": "CAMPAIGN",
                "resource_ref": "campaign-one",
                "effect_status": "NO_REMOTE_WRITE",
                "idempotency_ref": "attempt-one",
            },
        )

    assert raised.value.code == "UNSAFE_LEDGER_PATH"
    assert list(target.iterdir()) == []


def test_export_redacts_realtime_event_dimension_references() -> None:
    """A GA4 event partition cannot bypass keyed export pseudonymization."""
    raw = {
        "performance_rows": [
            {
                "dimension_type": "GA4_EVENT",
                "dimension_value": "ga4.event.signup",
            }
        ]
    }

    redacted = _redact_payload(raw, b"r" * 32, False)
    value = redacted["performance_rows"][0]["dimension_value"]
    assert value.startswith("opaque:")
    assert "signup" not in str(redacted)


def test_campaign_aggregate_cannot_claim_a_single_creative_version() -> None:
    """Campaign totals cannot acquire a creative identity through report metadata."""
    with pytest.raises(ValidationError):
        PerformanceRow(
            report_id="report-one",
            resource_level="CAMPAIGN",
            metric_grain="CAMPAIGN",
            resource_type="CAMPAIGN",
            resource_ref="campaign-one",
            creative_version_id="creative:sha256:one",
            impressions=10,
        )


def test_normalized_report_rejects_mixed_currency_totals() -> None:
    """A report must not silently combine money that needs an FX conversion."""
    envelope = ReportEnvelope(
        report_id="report-one",
        provider="ga4",
        source_account_ref="account-one",
        query_contract_hash="c" * 64,
        window_start=date(2030, 1, 1),
        window_end=date(2030, 1, 1),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="GA4_DATA_API",
        observed_at=BASE,
        provider_api_version="v1beta",
        connector_version="review",
    )
    row = PerformanceRow(
        report_id="report-one",
        resource_level="ACCOUNT",
        metric_grain="ACCOUNT",
        resource_type="ACCOUNT",
        resource_ref="account-one",
        spend=ReportMoney(minor_units=1, currency="EUR"),
    )
    with pytest.raises(ValidationError):
        NormalizedReport(envelope=envelope, performance_rows=[row])

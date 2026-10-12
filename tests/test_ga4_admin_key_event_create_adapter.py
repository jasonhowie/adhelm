"""Fake-HTTP coverage for the internal guarded GA4 key-event CREATE adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_ga4_create_workflow import _setup

from adhelm import ga4_create_workflow as workflow
from adhelm.egress_permit import EgressPermit, _kernel_permit
from adhelm.errors import AdhelmError
from adhelm.providers import ga4_admin_key_event_create_adapter as adapter_module
from adhelm.providers.ga4_admin_key_event_create_adapter import (
    Ga4AdminKeyEventCreateGuardedAdapter,
    _configured_ga4_create_adapter,
)
from adhelm.safety import ApplyOutcome, DirectGrant, EffectStatus, SafetyKernel, new_direct_grant

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_NOW = datetime(2031, 1, 1, tzinfo=UTC)
_KEY = b"m24-fingerprint-key-for-fake-observation!"
_ACCESS = "m24-private-oauth-access"


class _ApprovalHost:
    host_ref = "fixture-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return grant.user_interaction_ref == "fixture-interaction"

    def verifies_policy_configuration(self, policy: object) -> bool:
        del policy
        return False


class _CreateScenario:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = [
            {
                "name": "properties/123/keyEvents/700",
                "eventName": "m24_private_event_0",
                "custom": True,
                "deletable": True,
                "countingMethod": "ONCE_PER_EVENT",
            },
            {
                "name": "properties/123/keyEvents/701",
                "eventName": "m24_private_event_1",
                "custom": True,
                "deletable": True,
                "countingMethod": "ONCE_PER_EVENT",
            },
        ]
        self.calls: list[httpx.Request] = []
        self.posts = 0
        self.created: dict[str, object] | None = None
        self.mode = "ok"

    def _property(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "properties/123",
                "timeZone": "UTC",
                "currencyCode": "USD",
                "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "analyticsadmin.googleapis.com"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        if request.method == "GET" and request.url.path == "/v1beta/properties/123":
            return self._property()
        if request.method == "GET" and request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(
                200,
                json={"name": "properties/123/dataStreams/456", "type": "WEB_DATA_STREAM"},
            )
        if request.method == "GET" and request.url.path == "/v1alpha/properties/123/keyEvents":
            assert dict(request.url.params)["pageSize"] == "200"
            return httpx.Response(200, json={"keyEvents": self.events})
        if request.method == "POST":
            assert request.url.path == "/v1alpha/properties/123/keyEvents"
            self.posts += 1
            if self.mode == "lost":
                raise httpx.ConnectError("fixture lost response", request=request)
            if self.mode == "failure":
                return httpx.Response(500)
            body = json.loads(request.content)
            assert set(body) <= {"eventName", "countingMethod", "defaultValue"}
            self.created = {
                "name": "properties/123/keyEvents/702",
                "eventName": body["eventName"],
                "custom": True,
                "deletable": True,
                "countingMethod": body["countingMethod"],
                **({"defaultValue": body["defaultValue"]} if "defaultValue" in body else {}),
            }
            self.events = [*self.events, self.created]
            return httpx.Response(200, json=self.created)
        if request.method == "GET" and request.url.path == "/v1alpha/properties/123/keyEvents/702":
            assert self.created is not None
            return httpx.Response(200, json=self.created)
        pytest.fail(f"unexpected request {request.method} {request.url.path}")


def _install_adapter_http(monkeypatch: pytest.MonkeyPatch, scenario: _CreateScenario) -> None:
    # The planning fixture temporarily patches ``httpx.Client`` in another
    # module.  Reach the concrete class so this adapter test owns its own fake
    # transport rather than inheriting that unrelated read fixture.
    actual_client = httpx._client.Client  # pyright: ignore[reportPrivateUsage]

    def factory(*, trust_env: bool, follow_redirects: bool) -> httpx.Client:
        assert trust_env is False and follow_redirects is False
        return actual_client(
            transport=httpx.MockTransport(scenario.handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(adapter_module.httpx, "Client", factory)


def _prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, Any, Any, list[datetime]]:
    request, ledger, _, _, _, current, _, _ = _setup(tmp_path, monkeypatch)
    result = workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    monkeypatch.setattr(adapter_module, "export_key", lambda: _KEY)
    monkeypatch.setattr(adapter_module, "_now", lambda: current[0])
    return request, ledger, result.action, current, current


def test_guarded_create_runs_fixed_post_returned_get_and_full_after_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, action, _, current = _prepared(tmp_path, monkeypatch)
    scenario = _CreateScenario()
    _install_adapter_http(monkeypatch, scenario)

    with _configured_ga4_create_adapter(
        tmp_path,
        action,
        ledger,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        clock=lambda: current[0],
    ) as adapter:
        plan = action.plan.subplans[0]
        assert isinstance(adapter, Ga4AdminKeyEventCreateGuardedAdapter)
        assert adapter.capabilities(plan).writes_enabled is True
        assert adapter.read_identity(plan.account.account_ref).stable_account_id == "123"
        before = adapter.read_state(plan)
        assert (before.fingerprint, before.revision) == (
            plan.before.fingerprint,
            plan.before.revision,
        )
        with _kernel_permit(
            plan_digest=action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref="fixture-attempt",
            check_current=lambda: None,
        ) as permit:
            # The adapter refuses a hand-created start. Add only the minimal
            # test seam marker because SafetyKernel owns durable starts.
            monkeypatch.setattr(adapter, "_kernel_started", lambda _plan: True)
            response = adapter.apply_guarded(plan, idempotency_key="fixture-attempt", permit=permit)
        assert response.outcome is ApplyOutcome.CONFIRMED
        after = adapter.read_state(plan)
        assert (after.fingerprint, after.revision) == (plan.after.fingerprint, plan.after.revision)
        assert adapter.verify(plan).verified is True
    assert scenario.posts == 1
    assert [request.method for request in scenario.calls].count("POST") == 1
    assert _ACCESS not in repr(adapter)


def test_safety_kernel_uses_durable_start_and_real_one_use_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, action, _, current = _prepared(tmp_path, monkeypatch)
    scenario = _CreateScenario()
    _install_adapter_http(monkeypatch, scenario)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: current[0])
    grant = new_direct_grant(
        authorization_id="ga4-create-fixture-grant",
        plan=action.plan,
        grantor_ref="fixture-owner",
        host_ref="fixture-host",
        user_interaction_ref="fixture-interaction",
        expires_at=current[0] + timedelta(minutes=1),
    )
    kernel.record_direct_grant(action.plan, grant, _ApprovalHost())
    with _configured_ga4_create_adapter(
        tmp_path,
        action,
        ledger,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        clock=lambda: current[0],
    ) as adapter:
        result = kernel.execute(action.plan, grant, {"ga4_admin": adapter})
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert scenario.posts == 1
    assert any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )


def test_direct_apply_or_forged_permit_cannot_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, action, _, current = _prepared(tmp_path, monkeypatch)
    scenario = _CreateScenario()
    _install_adapter_http(monkeypatch, scenario)
    with _configured_ga4_create_adapter(
        tmp_path,
        action,
        ledger,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        clock=lambda: current[0],
    ) as adapter:
        plan = action.plan.subplans[0]
        with pytest.raises(AdhelmError) as direct:
            adapter.apply(plan, idempotency_key="local")
        assert direct.value.code == "PERMIT_REQUIRED"
        with pytest.raises(AdhelmError) as guarded:
            adapter.apply_guarded(plan, idempotency_key="fake", permit=object.__new__(EgressPermit))
        assert guarded.value.code == "KERNEL_START_REQUIRED"
    assert scenario.posts == 0


@pytest.mark.parametrize("mode", ["lost", "failure"])
def test_mutation_loss_or_5xx_never_retries_and_reconciliation_stays_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    request, ledger, action, _, current = _prepared(tmp_path, monkeypatch)
    scenario = _CreateScenario()
    scenario.mode = mode
    _install_adapter_http(monkeypatch, scenario)
    with _configured_ga4_create_adapter(
        tmp_path,
        action,
        ledger,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        clock=lambda: current[0],
    ) as adapter:
        plan = action.plan.subplans[0]
        monkeypatch.setattr(adapter, "_kernel_started", lambda _plan: True)
        with _kernel_permit(
            plan_digest=action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref="fixture-attempt",
            check_current=lambda: None,
        ) as permit:
            with pytest.raises(AdhelmError) as error:
                adapter.apply_guarded(plan, idempotency_key="fixture-attempt", permit=permit)
        assert error.value.code == "AMBIGUOUS_REMOTE_STATE"
        reconciliation = adapter.reconcile(plan)
        assert reconciliation.status is EffectStatus.AMBIGUOUS_REMOTE_STATE
        assert reconciliation.settled is False
    assert scenario.posts == 1


def test_runtime_current_source_check_blocks_egress_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, action, _, current = _prepared(tmp_path, monkeypatch)
    scenario = _CreateScenario()
    _install_adapter_http(monkeypatch, scenario)
    calls = 0
    original = adapter_module._validate_current_sources

    def changes(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise AdhelmError("GA4_CREATE_FRESH_INPUT_MISMATCH", "fixed source mismatch")
        original(*args, **kwargs)

    monkeypatch.setattr(adapter_module, "_validate_current_sources", changes)
    with _configured_ga4_create_adapter(
        tmp_path,
        action,
        ledger,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        clock=lambda: current[0],
    ) as adapter:
        plan = action.plan.subplans[0]
        monkeypatch.setattr(adapter, "_kernel_started", lambda _plan: True)
        with _kernel_permit(
            plan_digest=action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref="fixture-attempt",
            check_current=lambda: None,
        ) as permit:
            with pytest.raises(AdhelmError) as error:
                adapter.apply_guarded(plan, idempotency_key="fixture-attempt", permit=permit)
    assert error.value.code == "GA4_CREATE_FRESH_INPUT_MISMATCH"
    assert scenario.posts == 0

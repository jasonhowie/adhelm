from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import ValidationError
from test_openai_ads import _account, _campaign

from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.projections import _redact_payload
from adhelm.providers.openai_ads import OpenAIAdsClient
from adhelm.providers.openai_pause import (
    OpenAIPauseAdapter,
    OpenAIPauseCredentials,
    OpenAIPausePlannedAction,
    execute_openai_campaign_pause,
    plan_openai_campaign_pause,
)
from adhelm.safety import DirectGrant, EffectStatus, SafetyKernel, new_direct_grant
from adhelm.transport import SafeHttpClient

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

NOW = datetime(2030, 1, 1, tzinfo=UTC)
_KEY = b"fixture-private-binding-key-32-bytes!"


class _Host:
    host_ref = "fixture-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return grant.user_interaction_ref == "fixture-interaction"

    def verifies_policy_configuration(self, policy: object) -> bool:
        del policy
        return False


class _Scenario:
    def __init__(
        self,
        *,
        post: Literal["ok", "lost", "malformed", "failure"] = "ok",
        after_drift: bool = False,
    ) -> None:
        self.status = "active"
        self.post = post
        self.after_drift = after_drift
        self.posts = 0
        self.requests: list[str] = []
        self.account = _account()
        self.campaign = _campaign("campaign_fixture")

    def campaign_payload(self) -> dict[str, Any]:
        item = dict(self.campaign)
        item["status"] = self.status
        return item

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=self.account)
        if request.url.path == "/v1/campaigns":
            item = self.campaign_payload()
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [item],
                    "first_id": "campaign_fixture",
                    "last_id": "campaign_fixture",
                    "has_more": False,
                },
            )
        if request.url.path == "/v1/campaigns/campaign_fixture" and request.method == "GET":
            return httpx.Response(200, json=self.campaign_payload())
        assert request.url.path == "/v1/campaigns/campaign_fixture/pause"
        assert request.method == "POST"
        self.posts += 1
        if self.post == "lost":
            raise httpx.ConnectError("fixture loss", request=request)
        if self.post == "failure":
            return httpx.Response(500)
        self.status = "paused"
        if self.post == "malformed":
            return httpx.Response(200, json={"not": "a campaign"})
        response = self.campaign_payload()
        if self.after_drift:
            self.campaign["bidding_type"] = "cost_per_click"
        return httpx.Response(200, json=response)

    def transport(self) -> SafeHttpClient:
        return SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(self.handler)),
            {"api.ads.openai.com"},
            read_attempts=1,
        )


def _credentials(
    *, profile: str = "profile", secret: str = "fixture-key-not-in-results"
) -> OpenAIPauseCredentials:
    return OpenAIPauseCredentials(profile_ref=profile, secret=secret, binding_key=_KEY)


def _action(
    scenario: _Scenario,
    credentials: OpenAIPauseCredentials | None = None,
    *,
    expires_at: datetime = NOW + timedelta(minutes=10),
    planning_now: datetime = NOW,
) -> tuple[Any, OpenAIPauseCredentials]:
    value = credentials or _credentials()
    transport = scenario.transport()
    return (
        plan_openai_campaign_pause(
            client=OpenAIAdsClient(transport, lambda: value.secret),
            credentials=value,
            expected_account_client_ref="account_fixture",
            campaign_client_ref="campaign_fixture",
            expires_at=expires_at,
            clock=lambda: planning_now,
        ),
        value,
    )


def _kernel(tmp_path: Path, action: Any) -> tuple[SafetyKernel, Ledger, DirectGrant]:
    ledger = Ledger(tmp_path, profile_ref="profile")
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: NOW)
    grant = new_direct_grant(
        authorization_id="pause-grant",
        plan=action.plan,
        grantor_ref="owner",
        host_ref="fixture-host",
        user_interaction_ref="fixture-interaction",
        expires_at=NOW + timedelta(minutes=5),
    )
    kernel.record_direct_grant(action.plan, grant, _Host())
    return kernel, ledger, grant


def test_pause_runs_through_kernel_with_durable_start_and_independent_readback(
    tmp_path: Path,
) -> None:
    scenario = _Scenario()
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    transport = scenario.transport()

    def supplier() -> OpenAIPauseCredentials:
        return credentials

    adapter = OpenAIPauseAdapter(action, transport, supplier, ledger)
    result = execute_openai_campaign_pause(
        kernel=kernel, action=action, authorization=grant, adapter=adapter
    )
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert scenario.posts == 1
    assert any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )
    assert scenario.requests.count("GET /v1/campaigns/campaign_fixture") >= 3
    assert credentials.secret not in repr(result)
    assert credentials.secret not in repr(adapter)
    # Ledger resource refs and projected plan fields cannot contain the native
    # ID; field paths are fixed structural text rather than interpolated IDs.
    assert "campaign_fixture" not in str(ledger.records("changes"))
    projection = _redact_payload(action.plan.model_dump(mode="json"), b"x" * 32, False)
    assert "campaign_fixture" not in str(projection)
    assert [change.path for change in action.plan.subplans[0].field_changes] == [
        "campaign/identity",
        "campaign/status",
    ]


@pytest.mark.parametrize("post", ["failure", "lost", "malformed"])
def test_ambiguous_post_outcomes_have_one_post_no_retry_and_stay_unresolved(
    tmp_path: Path, post: Literal["failure", "lost", "malformed"]
) -> None:
    scenario = _Scenario(post=post)
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    adapter = OpenAIPauseAdapter(action, scenario.transport(), lambda: credentials, ledger)
    result = kernel.execute(action.plan, grant, {"openai_ads": adapter})
    assert scenario.posts == 1
    assert result.results[0].status in {
        EffectStatus.AMBIGUOUS_REMOTE_STATE,
        EffectStatus.PARTIAL_EFFECT,
    }
    assert result.recovery_required is True
    reconciliation = adapter.reconcile(action.plan.subplans[0])
    assert reconciliation.status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciliation.settled is False


def test_direct_apply_and_forged_permit_cannot_post(tmp_path: Path) -> None:
    scenario = _Scenario()
    action, credentials = _action(scenario)
    ledger = Ledger(tmp_path, profile_ref="profile")
    adapter = OpenAIPauseAdapter(action, scenario.transport(), lambda: credentials, ledger)
    with pytest.raises(AdhelmError) as direct:
        adapter.apply(action.plan.subplans[0], idempotency_key="local-only")
    assert direct.value.code == "PERMIT_REQUIRED"
    fake = object.__new__(EgressPermit)
    with pytest.raises(AdhelmError) as guarded:
        adapter.apply_guarded(action.plan.subplans[0], idempotency_key="fake", permit=fake)
    assert guarded.value.code == "KERNEL_START_REQUIRED"
    assert scenario.posts == 0


@pytest.mark.parametrize(
    "mutation",
    [
        ("account", "stable_account_id", "another-account"),
        ("account", "auth_profile_ref", "another-profile"),
        ("account", "credential_binding_ref", "credential-binding:sha256:" + "a" * 64),
        ("subplans", "provider_api_version", "v9"),
        ("subplans", "capability_version", "other-contract"),
        ("subplans", "egress", [{"host": "example.invalid", "purpose": "redirect"}]),
    ],
)
def test_rehashed_cross_binding_tampering_is_rejected(mutation: tuple[str, str, Any]) -> None:
    action, _ = _action(_Scenario())
    payload = action.model_dump(mode="python")
    parent, field, value = mutation
    if parent == "account":
        payload["plan"]["subplans"][0]["account"][field] = value
    else:
        payload["plan"]["subplans"][0][field] = value
    with pytest.raises(ValidationError):
        OpenAIPausePlannedAction.model_validate(payload)


def test_profile_key_account_and_baseline_drift_all_fail_before_post(tmp_path: Path) -> None:
    for changed in ("profile", "key", "account", "campaign"):
        scenario = _Scenario()
        action, credentials = _action(scenario)
        kernel, ledger, grant = _kernel(tmp_path / changed, action)
        if changed == "account":
            scenario.account["id"] = "other_account"
        if changed == "campaign":
            scenario.campaign["budget"] = {"lifetime_spend_limit_micros": 999}

        def supplier(
            expected_change: str = changed,
            stable_credentials: OpenAIPauseCredentials = credentials,
        ) -> OpenAIPauseCredentials:
            if expected_change == "profile":
                return _credentials(profile="other-profile")
            if expected_change == "key":
                return _credentials(secret="rotated-fixture-key")
            return stable_credentials

        adapter = OpenAIPauseAdapter(action, scenario.transport(), supplier, ledger)
        with pytest.raises(AdhelmError):
            kernel.execute(action.plan, grant, {"openai_ads": adapter})
        assert scenario.posts == 0


def test_rotation_between_current_read_and_egress_prevents_post(tmp_path: Path) -> None:
    scenario = _Scenario()
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    calls = 0

    def supplier() -> OpenAIPauseCredentials:
        nonlocal calls
        calls += 1
        # The immediate preflight reads are stable. The second supplier call
        # inside apply_guarded is the close-to-egress rotation check.
        if calls >= 6:
            return _credentials(secret="rotated-fixture-key")
        return credentials

    adapter = OpenAIPauseAdapter(action, scenario.transport(), supplier, ledger)
    result = kernel.execute(action.plan, grant, {"openai_ads": adapter})
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert scenario.posts == 0


def test_after_readback_drift_cannot_verify(tmp_path: Path) -> None:
    scenario = _Scenario(after_drift=True)
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    result = kernel.execute(
        action.plan,
        grant,
        {
            "openai_ads": OpenAIPauseAdapter(
                action, scenario.transport(), lambda: credentials, ledger
            )
        },
    )
    assert scenario.posts == 1
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE


@pytest.mark.parametrize("field", ["bidding_type", "budget", "start_time", "status"])
def test_incomplete_or_ineligible_baselines_fail_during_planning(field: str) -> None:
    scenario = _Scenario()
    if field == "status":
        scenario.status = "paused"
    else:
        scenario.campaign[field] = None
    with pytest.raises(AdhelmError):
        _action(scenario)
    assert scenario.posts == 0


def test_expired_plan_and_secret_errors_do_not_reach_post_or_output(tmp_path: Path) -> None:
    scenario = _Scenario()
    credentials = _credentials(secret="secret-never-output")
    action, _ = _action(
        scenario,
        credentials,
        expires_at=NOW - timedelta(seconds=1),
        planning_now=NOW - timedelta(seconds=2),
    )
    with pytest.raises(AdhelmError) as error:
        _kernel(tmp_path, action)
    assert credentials.secret not in str(error.value)
    assert scenario.posts == 0

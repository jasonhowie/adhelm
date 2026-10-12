"""Adversarial regressions for the fixed OpenAI campaign-pause adapter."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_openai_pause import NOW, _action, _credentials, _kernel, _Scenario

from adhelm.errors import AdhelmError
from adhelm.providers.openai_ads import OpenAIAdsClient
from adhelm.providers.openai_pause import (
    OpenAIPauseAdapter,
    OpenAIPausePlannedAction,
    _build_plan,
    execute_openai_campaign_pause,
    plan_openai_campaign_pause,
)
from adhelm.safety import EffectStatus

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _RedirectScenario(_Scenario):
    def __init__(self) -> None:
        super().__init__()
        self.redirect_requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.ads.openai.com":
            self.redirect_requests.append(str(request.url))
            return httpx.Response(500)
        if request.url.path == "/v1/campaigns/campaign_fixture/pause":
            self.requests.append(f"{request.method} {request.url.path}")
            self.posts += 1
            return httpx.Response(
                307,
                headers={"location": "https://other.invalid/v1/campaigns/other/pause"},
            )
        return super().handler(request)


class _CrossResourceScenario(_Scenario):
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/campaigns/campaign_fixture/pause":
            self.requests.append(f"{request.method} {request.url.path}")
            self.posts += 1
            self.status = "paused"
            payload = self.campaign_payload()
            payload["id"] = "other_campaign"
            return httpx.Response(200, json=payload)
        return super().handler(request)


class _SensitiveResponseScenario(_Scenario):
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/campaigns/campaign_fixture/pause":
            self.requests.append(f"{request.method} {request.url.path}")
            self.posts += 1
            payload = self.campaign_payload()
            payload["description"] = "person@example.invalid"
            return httpx.Response(200, json=payload)
        return super().handler(request)


class _MetadataRotationScenario(_Scenario):
    def __init__(self, field: str, value: str) -> None:
        super().__init__()
        self._field, self._value, self._account_reads = field, value, 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            self._account_reads += 1
            # Planning plus both kernel preflights already completed. The
            # sixth account GET begins guarded apply after APPLY_STARTED.
            if self._account_reads >= 6:
                self.account[self._field] = self._value
        return super().handler(request)


@pytest.mark.parametrize(
    ("campaign_client_ref", "maximum_inventory_records", "expires_at"),
    [
        ("campaign/escape", 500, NOW + timedelta(minutes=10)),
        ("campaign_fixture", True, NOW + timedelta(minutes=10)),
        ("campaign_fixture", 5_001, NOW + timedelta(minutes=10)),
        ("campaign_fixture", 500, datetime(2030, 1, 1)),
    ],
)
def test_invalid_public_pause_inputs_fail_before_any_provider_read(
    campaign_client_ref: object,
    maximum_inventory_records: object,
    expires_at: object,
) -> None:
    scenario = _Scenario()
    credentials = _credentials()
    client = OpenAIAdsClient(scenario.transport(), lambda: credentials.secret)

    with pytest.raises(AdhelmError) as error:
        plan_openai_campaign_pause(
            client=client,
            credentials=credentials,
            expected_account_client_ref="account_fixture",
            campaign_client_ref=campaign_client_ref,  # type: ignore[arg-type]
            expires_at=expires_at,  # type: ignore[arg-type]
            maximum_inventory_records=maximum_inventory_records,  # type: ignore[arg-type]
        )

    assert error.value.code == "INVALID_INPUT"
    assert scenario.requests == []


def test_expired_pause_plan_fails_before_any_provider_read() -> None:
    scenario = _Scenario()
    credentials = _credentials()
    client = OpenAIAdsClient(scenario.transport(), lambda: credentials.secret)

    with pytest.raises(AdhelmError) as error:
        plan_openai_campaign_pause(
            client=client,
            credentials=credentials,
            expected_account_client_ref="account_fixture",
            campaign_client_ref="campaign_fixture",
            expires_at=NOW - timedelta(seconds=1),
            clock=lambda: NOW,
        )

    assert error.value.code == "PLAN_EXPIRED"
    assert scenario.requests == []


def test_public_wrapper_requires_the_actual_safety_kernel(tmp_path: Path) -> None:
    scenario = _Scenario()
    action, credentials = _action(scenario)
    adapter = OpenAIPauseAdapter(
        action,
        scenario.transport(),
        lambda: credentials,
        _kernel(tmp_path, action)[1],
    )

    class ImitationKernel:
        called = False

        def execute(self, *args: Any, **kwargs: Any) -> object:
            self.called = True
            return object()

    imitation = ImitationKernel()
    with pytest.raises(AdhelmError) as error:
        execute_openai_campaign_pause(
            kernel=imitation,  # type: ignore[arg-type]
            action=action,
            authorization=object(),
            adapter=adapter,
        )

    assert error.value.code == "SAFETY_KERNEL_REQUIRED"
    assert imitation.called is False
    assert scenario.posts == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("timezone", "UTC"), ("currency_code", "EUR"), ("status", "paused")],
)
def test_metadata_rotation_during_guarded_prepost_read_blocks_egress(
    tmp_path: Path, field: str, value: str
) -> None:
    scenario = _MetadataRotationScenario(field, value)
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    adapter = OpenAIPauseAdapter(action, scenario.transport(), lambda: credentials, ledger)

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert scenario.posts == 0
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("campaign_ref", "openai_ads.campaign.other_campaign"),
        ("bidding_type", "clicks"),
        ("lifetime_spend_limit_micros", "9999999"),
        ("start_at", "ADVANCE_START"),
        ("end_at", "SET_END"),
    ],
)
def test_self_consistent_rehashed_baseline_still_must_match_current_source(
    tmp_path: Path, field: str, value: str
) -> None:
    scenario = _Scenario()
    original, credentials = _action(scenario)
    replacement: object = value
    if value == "ADVANCE_START":
        replacement = original.binding.campaign.start_at + timedelta(seconds=1)
    elif value == "SET_END":
        replacement = original.binding.campaign.start_at + timedelta(days=1)
    baseline = original.binding.campaign.model_copy(update={field: replacement})
    binding = original.binding.model_copy(update={"campaign": baseline})
    forged = OpenAIPausePlannedAction(
        binding=binding,
        plan=_build_plan(binding, expires_at=original.plan.expires_at),
    )
    kernel, ledger, grant = _kernel(tmp_path, forged)
    adapter = OpenAIPauseAdapter(forged, scenario.transport(), lambda: credentials, ledger)

    with pytest.raises(AdhelmError) as error:
        kernel.execute(forged.plan, grant, {adapter.provider: adapter})

    assert error.value.code == "REMOTE_DRIFT"
    assert scenario.posts == 0
    assert not any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_ref", "other_account"),
        ("account_client_ref", "other_account"),
        ("profile_ref", "other_profile"),
        ("credential_binding_ref", "credential-binding:sha256:" + "a" * 64),
        ("account_timezone", "UTC"),
        ("account_currency", "EUR"),
    ],
)
def test_self_consistent_rehashed_account_binding_still_must_match_current_source(
    tmp_path: Path, field: str, value: str
) -> None:
    scenario = _Scenario()
    original, credentials = _action(scenario)
    binding = original.binding.model_copy(update={field: value})
    forged = OpenAIPausePlannedAction(
        binding=binding,
        plan=_build_plan(binding, expires_at=original.plan.expires_at),
    )
    kernel, ledger, grant = _kernel(tmp_path, forged)
    adapter = OpenAIPauseAdapter(forged, scenario.transport(), lambda: credentials, ledger)

    with pytest.raises(AdhelmError):
        kernel.execute(forged.plan, grant, {adapter.provider: adapter})

    assert scenario.posts == 0
    assert not any(
        row["payload"].get("event_type") == "APPLY_STARTED" for row in ledger.records("changes")
    )


def test_adapter_plan_expiry_is_checked_immediately_before_permit_consumption(
    tmp_path: Path,
) -> None:
    scenario = _Scenario()
    action, credentials = _action(scenario, expires_at=NOW + timedelta(minutes=5))
    kernel, ledger, grant = _kernel(tmp_path, action)
    adapter = OpenAIPauseAdapter(
        action,
        scenario.transport(),
        lambda: credentials,
        ledger,
        clock=lambda: NOW + timedelta(minutes=6),
    )

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert scenario.posts == 0
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert ledger.unresolved_starts()


def test_cross_resource_pause_response_cannot_become_verified(tmp_path: Path) -> None:
    scenario = _CrossResourceScenario()
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    adapter = OpenAIPauseAdapter(action, scenario.transport(), lambda: credentials, ledger)

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert scenario.posts == 1
    # The uniform write rule applies the attribution gap to every provider, not
    # only Meta PAUSE and GA4 CREATE. A rejected cross-resource acknowledgement
    # is not attributable to this invocation, so a matching later read cannot
    # settle it as a partial effect.
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.verify(action.plan.subplans[0]).verified is False
    assert "other_campaign" not in str(ledger.records("changes"))


def test_post_redirect_is_not_followed_or_retried(tmp_path: Path) -> None:
    scenario = _RedirectScenario()
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    adapter = OpenAIPauseAdapter(action, scenario.transport(), lambda: credentials, ledger)

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert scenario.posts == 1
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert scenario.requests.count("POST /v1/campaigns/campaign_fixture/pause") == 1
    assert scenario.redirect_requests == []
    assert "other.invalid" not in str(ledger.records("changes"))


def test_sensitive_pause_response_is_sanitized_before_history(tmp_path: Path) -> None:
    scenario = _SensitiveResponseScenario()
    action, credentials = _action(scenario)
    kernel, ledger, grant = _kernel(tmp_path, action)
    adapter = OpenAIPauseAdapter(action, scenario.transport(), lambda: credentials, ledger)

    result = kernel.execute(action.plan, grant, {adapter.provider: adapter})

    assert scenario.posts == 1
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert "person@example.invalid" not in str(ledger.records("changes"))

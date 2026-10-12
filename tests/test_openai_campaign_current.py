from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from test_openai_ads import _account, _campaign

from adhelm.errors import AdhelmError
from adhelm.providers.openai_ads import (
    OpenAIAdsClient,
    OpenAIAdsIdentity,
    OpenAIAdsInventoryQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport, *, attempts: int = 1) -> OpenAIAdsClient:
    return OpenAIAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler), {"api.ads.openai.com"}, read_attempts=attempts
        ),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )


def _session(client: OpenAIAdsClient) -> OpenAIAdsIdentity:
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )
    return identity


def test_get_campaign_is_fixed_current_read_after_same_session_inventory() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_campaign("campaign_fixture")],
                    "first_id": "campaign_fixture",
                    "last_id": "campaign_fixture",
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/campaigns/campaign_fixture"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
        current = _campaign("campaign_fixture")
        current["ad_account_id"] = "account_fixture"
        current["status"] = "paused"
        return httpx.Response(200, json=current)

    client = _client(httpx.MockTransport(handler))
    identity = _session(client)
    result = client.get_campaign(identity, "campaign_fixture")

    assert result.account_ref == identity.account_ref
    assert result.campaign_client_ref == "campaign_fixture"
    assert result.status == "paused"
    assert result.lifetime_spend_limit_micros == "10000000"
    assert result.start_at is not None and result.end_at is None
    assert [request.url.path for request in requests] == [
        "/v1/ad_account",
        "/v1/campaigns",
        "/v1/campaigns/campaign_fixture",
    ]
    assert "fixture-token-not-for-output" not in repr(result)


def test_get_campaign_rejects_unbound_session_identity_and_returned_identity_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_campaign("campaign_fixture")],
                    "first_id": "campaign_fixture",
                    "last_id": "campaign_fixture",
                    "has_more": False,
                },
            )
        response = _campaign("campaign_other")
        response["ad_account_id"] = "account_other"
        return httpx.Response(200, json=response)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    with pytest.raises(AdhelmError, match="bounded discovery session"):
        client.get_campaign(identity, "campaign_fixture")

    identity = _session(client)
    with pytest.raises(AdhelmError, match="discovered account"):
        client.get_campaign(identity, "campaign_fixture")
    forged = identity.model_copy(update={"account_client_ref": "account_other"})
    with pytest.raises(AdhelmError, match="discovered account identity"):
        client.get_campaign(forged, "campaign_fixture")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda campaign: campaign.update({"id": "../bad"}),
        lambda campaign: campaign.update({"budget": {"lifetime_spend_limit_micros": True}}),
        lambda campaign: campaign.update({"unknown": "unsupported"}),
    ],
)
def test_get_campaign_rejects_malformed_current_state(mutate: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_campaign("campaign_fixture")],
                    "first_id": "campaign_fixture",
                    "last_id": "campaign_fixture",
                    "has_more": False,
                },
            )
        campaign = _campaign("campaign_fixture")
        mutate(campaign)
        return httpx.Response(200, json=campaign)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError, match="invalid|unsupported|different campaign|lifetime spend"):
        client.get_campaign(_session(client), "campaign_fixture")


def test_get_campaign_uses_bounded_read_retries_only() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_campaign("campaign_fixture")],
                    "first_id": "campaign_fixture",
                    "last_id": "campaign_fixture",
                    "has_more": False,
                },
            )
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=_campaign("campaign_fixture"))

    client = _client(httpx.MockTransport(handler), attempts=3)
    assert client.get_campaign(_session(client), "campaign_fixture").status == "paused"
    assert calls == 3

"""Adversarial current-campaign regressions for the fixed OpenAI Ads read."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import pytest
from test_openai_ads import _account, _ad_group, _campaign

from adhelm.errors import AdhelmError
from adhelm.providers.openai_ads import (
    OpenAIAdsAdGroup,
    OpenAIAdsCampaign,
    OpenAIAdsClient,
    OpenAIAdsIdentity,
    OpenAIAdsInventoryQuery,
)
from adhelm.transport import SafeHttpClient


def _client(
    handler: httpx.MockTransport,
    *,
    token_getter: Any = lambda: "fixture-token-not-for-output",
) -> OpenAIAdsClient:
    return OpenAIAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"api.ads.openai.com"}, read_attempts=1),
        token_getter,
        now=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )


def _discover_campaign_and_group(
    client: OpenAIAdsClient,
) -> tuple[OpenAIAdsIdentity, OpenAIAdsInventoryQuery, OpenAIAdsCampaign, OpenAIAdsAdGroup]:
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    query = OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2)
    campaign = client.list_campaigns(query, identity=identity)[0]
    group = client.list_ad_groups(query, identity=identity, campaign=campaign)[0]
    return identity, query, campaign, group


@pytest.mark.parametrize("failure", ("transport", "schema", "resource"))
def test_current_read_failure_invalidates_the_parent_and_dependent_child_cache(
    failure: Literal["transport", "schema", "resource"],
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
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
        if request.url.path == "/v1/ad_groups":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_ad_group("group_fixture")],
                    "first_id": "group_fixture",
                    "last_id": "group_fixture",
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/campaigns/campaign_fixture"
        if failure == "transport":
            return httpx.Response(503)
        campaign = _campaign("campaign_fixture")
        if failure == "schema":
            campaign["unknown"] = "unsupported"
        else:
            campaign["id"] = "campaign_other"
        return httpx.Response(200, json=campaign)

    client = _client(httpx.MockTransport(handler))
    identity, query, campaign, group = _discover_campaign_and_group(client)

    with pytest.raises(AdhelmError):
        client.get_campaign(identity, "campaign_fixture")
    with pytest.raises(AdhelmError) as campaign_error:
        client.list_ad_groups(query, identity=identity, campaign=campaign)
    with pytest.raises(AdhelmError) as group_error:
        client.list_ads(query, identity=identity, ad_group=group)

    assert campaign_error.value.code == "PARENT_NOT_DISCOVERED"
    assert group_error.value.code == "PARENT_NOT_DISCOVERED"
    assert calls == [
        "/v1/ad_account",
        "/v1/campaigns",
        "/v1/ad_groups",
        "/v1/campaigns/campaign_fixture",
    ]


def test_failed_campaign_inventory_refresh_cannot_leave_an_old_parent_current() -> None:
    calls: list[str] = []
    campaign_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal campaign_reads
        calls.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/campaigns"
        campaign_reads += 1
        if campaign_reads == 1:
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
        return httpx.Response(200, json={"object": "wrong", "data": []})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    query = OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2)
    client.list_campaigns(query, identity=identity)
    with pytest.raises(AdhelmError):
        client.list_campaigns(query, identity=identity)
    with pytest.raises(AdhelmError) as raised:
        client.get_campaign(identity, "campaign_fixture")

    assert raised.value.code == "PARENT_NOT_DISCOVERED"
    assert calls == ["/v1/ad_account", "/v1/campaigns", "/v1/campaigns"]


@pytest.mark.parametrize(
    "query",
    (
        lambda account_ref: OpenAIAdsInventoryQuery(account_ref=account_ref).model_copy(
            update={"page_size": 501, "maximum_records": 501}
        ),
        lambda account_ref: OpenAIAdsInventoryQuery.model_construct(
            account_ref=account_ref,
            page_size=1.0,
            maximum_records=1,
            order="desc",
        ),
    ),
)
def test_constructed_or_copied_query_fails_before_token_or_inventory_http(query: Any) -> None:
    token_reads = 0
    requests: list[str] = []

    def token() -> str:
        nonlocal token_reads
        token_reads += 1
        return "fixture-token-not-for-output"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.path == "/v1/ad_account"
        return httpx.Response(200, json=_account())

    client = _client(httpx.MockTransport(handler), token_getter=token)
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(query(identity.account_ref), identity=identity)

    assert raised.value.code == "INVALID_INPUT"
    assert token_reads == 1
    assert requests == ["/v1/ad_account"]


def test_malformed_expected_account_clears_session_before_token_or_http() -> None:
    token_reads = 0
    requests: list[str] = []

    def token() -> str:
        nonlocal token_reads
        token_reads += 1
        return "fixture-token-not-for-output"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.path == "/v1/ad_account"
        return httpx.Response(200, json=_account())

    client = _client(httpx.MockTransport(handler), token_getter=token)
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    with pytest.raises(AdhelmError) as invalid:
        client.discover_identity(expected_account_client_ref="../wrong")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            OpenAIAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )

    assert invalid.value.code == "INVALID_INPUT"
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert token_reads == 1
    assert requests == ["/v1/ad_account"]


def test_malformed_current_campaign_id_fails_before_current_read_http() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/campaigns"
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

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )
    with pytest.raises(AdhelmError) as raised:
        client.get_campaign(identity, "../wrong")

    assert raised.value.code == "INVALID_INPUT"
    assert requests == ["/v1/ad_account", "/v1/campaigns"]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda campaign: campaign.pop("status"),
        lambda campaign: campaign.update({"budget": {"lifetime_spend_limit_micros": 10_000_000.0}}),
        lambda campaign: campaign.update({"budget": {"lifetime_spend_limit_micros": "10000000"}}),
        lambda campaign: campaign.update({"start_time": True}),
    ),
)
def test_current_read_rejects_missing_status_and_noninteger_native_wire_fields(
    mutate: Any,
) -> None:
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
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )
    with pytest.raises(AdhelmError) as raised:
        client.get_campaign(identity, "campaign_fixture")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_current_read_omits_copy_and_rejects_sensitive_payload_without_echoing_it() -> None:
    sensitive = "person@example.com"

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
        campaign["name"] = sensitive
        return httpx.Response(200, json=campaign)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )
    with pytest.raises(AdhelmError) as raised:
        client.get_campaign(identity, "campaign_fixture")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert sensitive not in raised.value.message


def test_current_read_drops_non_sensitive_copy_from_the_safe_inventory_model() -> None:
    private_copy = "fixture campaign copy that is not part of the inventory contract"

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
        campaign["name"] = private_copy
        campaign["description"] = private_copy
        return httpx.Response(200, json=campaign)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )
    result = client.get_campaign(identity, "campaign_fixture")

    rendered = result.model_dump_json()
    assert private_copy not in rendered
    assert "name" not in rendered and "description" not in rendered

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.openai_ads import (
    OpenAIAdsAdGroup,
    OpenAIAdsCampaign,
    OpenAIAdsClient,
    OpenAIAdsHierarchyInventoryQuery,
    OpenAIAdsIdentity,
    OpenAIAdsInventoryQuery,
    OpenAIAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport) -> OpenAIAdsClient:
    transport = SafeHttpClient(
        httpx.Client(transport=handler), {"api.ads.openai.com"}, read_attempts=1
    )
    return OpenAIAdsClient(
        transport,
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _account() -> dict[str, Any]:
    return {
        "id": "account_fixture",
        "name": "Fixture account",
        "url": "https://example.invalid",
        "preview_url": None,
        "status": "active",
        "timezone": "America/Chicago",
        "currency_code": "USD",
        "review": {"status": "approved"},
    }


def _campaign(campaign_id: str) -> dict[str, Any]:
    return {
        "id": campaign_id,
        "created_at": 1_788_260_800,
        "status": "paused",
        "bidding_type": "impressions",
        "budget": {"lifetime_spend_limit_micros": 10_000_000},
        "conversion_event_setting_ids": [],
        "description": "Fixture",
        "end_time": None,
        "mode": None,
        "name": "Fixture campaign",
        "start_time": 1_788_260_800,
        "targeting": {},
        "updated_at": 1_788_260_800,
    }


def _ad_group(ad_group_id: str) -> dict[str, Any]:
    return {
        "id": ad_group_id,
        "created_at": 1_788_260_800,
        "updated_at": 1_788_260_800,
        "name": "Fixture group",
        "description": "Fixture description",
        "context_hints": ["Fixture context"],
        "status": "paused",
        "bidding_config": {"billing_event_type": "impression", "max_bid_micros": 125_000},
    }


def _ad(ad_id: str) -> dict[str, Any]:
    return {
        "id": ad_id,
        "name": "Fixture ad",
        "created_at": 1_788_260_800,
        "updated_at": 1_788_260_800,
        "creative": {
            "type": "chat_card",
            "title": "Fixture title",
            "body": "Fixture body",
            "file_id": "file_fixture",
            "image_url": "https://example.invalid/image.png",
            "target_url": "https://example.invalid/landing",
        },
        "status": "paused",
        "review_status": "approved",
    }


def test_identity_inventory_pagination_and_campaign_report() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            if request.url.params.get("after") is None:
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [_campaign("campaign_first")],
                        "first_id": "campaign_first",
                        "last_id": "page_one",
                        "has_more": True,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_campaign("campaign_second")],
                    "first_id": "campaign_second",
                    "last_id": "campaign_second",
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/ad_account/insights"
        assert request.url.params.get("aggregation_level") == "campaign"
        assert request.url.params.get_list("fields[]") == [
            "metadata.readable_time",
            "campaign.id",
            "campaign.impressions",
            "campaign.clicks",
            "campaign.spend",
        ]
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "count": 1,
                    "data": [
                        {
                            "id": "row_one",
                            "start_time": 1_788_307_200,
                            "end_time": 1_788_393_600,
                            "readable_time": "2026-09-01",
                            "campaign_id": "campaign_first",
                            "impressions": 10,
                            "clicks": 2,
                            "spend": 1.25,
                        }
                    ],
                    "first_id": "row_one",
                    "last_id": "row_one",
                    "has_more": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "object": "list",
                "count": 1,
                "data": [
                    {
                        "id": "row_two",
                        "start_time": 1_788_393_600,
                        "end_time": 1_788_480_000,
                        "readable_time": "2026-09-02",
                        "campaign_id": "campaign_first",
                        "impressions": 4,
                        "clicks": 1,
                        "spend": 0.75,
                    }
                ],
                "first_id": "row_two",
                "last_id": "row_two",
                "has_more": False,
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    inventory = client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, page_size=1, maximum_records=2),
        identity=identity,
    )
    report = client.run_campaign_report(
        OpenAIAdsReportQuery(
            account_ref=identity.account_ref,
            report_id="report.fixture",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 2),
            source_timezone="America/Chicago",
            page_size=1,
            maximum_rows=2,
        ),
        identity=identity,
    )
    assert [campaign.campaign_client_ref for campaign in inventory] == [
        "campaign_first",
        "campaign_second",
    ]
    assert [row.spend.minor_units if row.spend else None for row in report.performance_rows] == [
        125,
        75,
    ]
    assert all(row.metric_grain == "CAMPAIGN" for row in report.performance_rows)
    assert len(requests) == 5


def test_hierarchy_inventory_and_daily_ad_report_are_typed_and_paginated() -> None:
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
        if request.url.path == "/v1/ad_groups":
            assert request.url.params["campaign_id"] == "campaign_fixture"
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
        if request.url.path == "/v1/ads":
            assert request.url.params["ad_group_id"] == "group_fixture"
            if request.url.params.get("after") is None:
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [_ad("ad_first")],
                        "first_id": "ad_first",
                        "last_id": "ad_first",
                        "has_more": True,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_ad("ad_second")],
                    "first_id": "ad_second",
                    "last_id": "ad_second",
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/ad_account/insights"
        assert request.url.params["aggregation_level"] == "ad"
        assert request.url.params.get_list("fields[]") == [
            "metadata.readable_time",
            "campaign.id",
            "ad.id",
            "ad.impressions",
            "ad.clicks",
            "ad.spend",
        ]
        if request.url.params.get("after") is None:
            payload = {
                "id": "ad_row_one",
                "start_time": 1_788_307_200,
                "end_time": 1_788_393_600,
                "readable_time": "2026-09-01",
                "campaign_id": "campaign_fixture",
                "ad_id": "ad_first",
                "impressions": 10,
                "clicks": 2,
                "spend": 1.25,
            }
            has_more = True
        else:
            payload = {
                "id": "ad_row_two",
                "start_time": 1_788_393_600,
                "end_time": 1_788_480_000,
                "readable_time": "2026-09-02",
                "campaign_id": "campaign_fixture",
                "ad_id": "ad_second",
                "impressions": 4,
                "clicks": 1,
                "spend": 0.75,
            }
            has_more = False
        return httpx.Response(
            200,
            json={
                "object": "list",
                "count": 1,
                "data": [payload],
                "first_id": payload["id"],
                "last_id": payload["id"],
                "has_more": has_more,
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(
            account_ref=identity.account_ref, page_size=1, maximum_records=4
        ),
        identity=identity,
    )
    report = client.run_ad_report(
        OpenAIAdsReportQuery(
            level="AD",
            account_ref=identity.account_ref,
            report_id="report.fixture",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 2),
            source_timezone="America/Chicago",
            page_size=1,
            maximum_rows=2,
        ),
        identity=identity,
    )
    assert hierarchy.campaigns[0].account_ref == identity.account_ref
    assert hierarchy.ad_groups[0].campaign_ref == hierarchy.campaigns[0].campaign_ref
    assert {ad.ad_group_ref for ad in hierarchy.ads} == {hierarchy.ad_groups[0].ad_group_ref}
    assert [ad.ad_client_ref for ad in hierarchy.ads] == ["ad_first", "ad_second"]
    assert [row.resource_ref for row in report.performance_rows] == [
        "openai_ads.ad.ad_first",
        "openai_ads.ad.ad_second",
    ]
    assert all(row.metric_grain == "AD" for row in report.performance_rows)
    assert all(row.parent_resource_ref is None for row in report.performance_rows)
    assert all(row.creative_version_id is None for row in report.performance_rows)
    assert all(
        row.data_quality_statuses == ["CREATIVE_NOT_EXPOSED", "NOT_AVAILABLE_FROM_PROVIDER"]
        for row in report.performance_rows
    )
    assert [row.spend.minor_units if row.spend else None for row in report.performance_rows] == [
        125,
        75,
    ]
    assert len(requests) == 7


def test_unsafe_report_shape_fails_before_transport() -> None:
    with pytest.raises(ValidationError):
        OpenAIAdsReportQuery(
            account_ref="account.fixture",
            report_id="report.fixture",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 2),
            window_end=date(2026, 9, 1),
            source_timezone="UTC",
        )


def test_unknown_response_field_is_rejected_without_token_output() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={**_account(), "unexpected": "fixture-token-not-for-output"}
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity()
    assert calls == 1
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "fixture-token" not in raised.value.message


def test_duplicate_response_keys_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"id":"account_one","id":"account_two"}')

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity()
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_identity_mismatch_stops_report_before_egress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_account())

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            OpenAIAdsReportQuery(
                account_ref="different.account",
                report_id="report.fixture",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 1),
                source_timezone="America/Chicago",
            ),
            identity=identity,
        )
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert calls == 1


def test_future_report_window_stops_before_insights_egress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_account())

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            OpenAIAdsReportQuery(
                account_ref=identity.account_ref,
                report_id="report.fixture",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 5),
                window_end=date(2026, 9, 5),
                source_timezone="America/Chicago",
            ),
            identity=identity,
        )
    assert raised.value.code == "INVALID_REPORT_WINDOW"
    assert calls == 1


def test_non_usd_identity_stops_report_before_insights_egress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        account = _account()
        account["currency_code"] = "JPY"
        return httpx.Response(200, json=account)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            OpenAIAdsReportQuery(
                account_ref=identity.account_ref,
                report_id="report.fixture",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 1),
                source_timezone="America/Chicago",
            ),
            identity=identity,
        )
    assert raised.value.code == "UNSUPPORTED_CURRENCY"
    assert calls == 1


@pytest.mark.parametrize("forged", ["campaign", "ad_group", "ancestor", "account"])
def test_direct_hierarchy_models_cannot_relabel_native_parent_provenance(forged: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/ad_account"
        return httpx.Response(200, json=_account())

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    if forged == "account":
        identity = OpenAIAdsIdentity.model_validate(
            {**identity.model_dump(), "account_ref": "openai_ads.account.other"}
        )
    query = OpenAIAdsInventoryQuery(account_ref=identity.account_ref)
    campaign = OpenAIAdsCampaign(
        account_ref=identity.account_ref,
        campaign_ref="openai_ads.campaign.other"
        if forged == "campaign"
        else "openai_ads.campaign.one",
        campaign_client_ref="one",
        status="paused",
    )
    group = OpenAIAdsAdGroup(
        account_ref=identity.account_ref,
        campaign_ref="openai_ads.campaign.other" if forged == "ancestor" else campaign.campaign_ref,
        campaign_client_ref="one",
        ad_group_ref="openai_ads.ad_group.other"
        if forged == "ad_group"
        else "openai_ads.ad_group.one",
        ad_group_client_ref="one",
        status="paused",
        billing_event_type="impression",
    )
    with pytest.raises(AdhelmError) as caught:
        if forged == "account":
            client.list_campaigns(query, identity=identity)
        elif forged == "campaign":
            client.list_ad_groups(query, identity=identity, campaign=campaign)
        else:
            client.list_ads(query, identity=identity, ad_group=group)
    assert caught.value.code == "IDENTITY_MISMATCH"
    assert calls == 1

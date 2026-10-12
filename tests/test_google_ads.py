from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.google_ads import (
    GoogleAdsClient,
    GoogleAdsInventoryQuery,
    GoogleAdsReadConfig,
    GoogleAdsReportQuery,
)
from adhelm.transport import SafeHttpClient

CONFIG = GoogleAdsReadConfig(
    account_ref="account.google.1",
    customer_client_ref="1234567890",
    login_customer_client_ref="0987654321",
)


def _response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _identity() -> dict[str, object]:
    return {
        "results": [{"customer": {"id": "1234567890", "currencyCode": "USD", "timeZone": "UTC"}}]
    }


def _client(transport: httpx.BaseTransport, *, now: datetime | None = None) -> GoogleAdsClient:
    return GoogleAdsClient(
        SafeHttpClient(
            httpx.Client(transport=transport),
            {"googleads.googleapis.com"},
            sleep=lambda _: None,
        ),
        lambda: "access-fixture-token",
        lambda: "developer-fixture-token",
        now=lambda: now or datetime(2026, 9, 4, 12, tzinfo=UTC),
    )


def test_fixed_identity_inventory_and_campaign_report_paginate() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v25/customers/1234567890/googleAds:search"
        assert request.headers["authorization"] == "Bearer access-fixture-token"
        assert request.headers["developer-token"] == "developer-fixture-token"
        assert request.headers["login-customer-id"] == "0987654321"
        body = json.loads(request.content)
        gaql = body["query"]
        assert gaql.endswith(" PARAMETERS omit_unselected_resource_names = true")
        assert body["searchSettings"] == {"returnTotalResultsCount": True}
        assert set(body) <= {"query", "searchSettings", "pageToken"}
        assert "pageSize" not in body
        if "customer.currency_code" in gaql:
            return _response(_identity())
        if "campaign_budget.amount_micros" in gaql:
            if body.get("pageToken") is None:
                return _response(
                    {
                        "results": [
                            {
                                "campaign": {
                                    "id": "11",
                                    "status": "ENABLED",
                                    "advertisingChannelType": "SEARCH",
                                    "startDate": "2026-01-01",
                                    "endDate": "",
                                    "biddingStrategyType": "MAXIMIZE_CONVERSIONS",
                                },
                                "campaignBudget": {"amountMicros": "1000000"},
                            }
                        ],
                        "nextPageToken": "campaign-page-2",
                        "totalResultsCount": "2",
                    }
                )
            assert body["pageToken"] == "campaign-page-2"
            return _response(
                {
                    "results": [
                        {
                            "campaign": {
                                "id": "12",
                                "status": "PAUSED",
                                "advertisingChannelType": "DISPLAY",
                                "startDate": "2026-02-01",
                                "endDate": "2026-03-01",
                                "biddingStrategyType": "MANUAL_CPC",
                            },
                            "campaignBudget": {"amountMicros": "2000000"},
                        }
                    ],
                    "totalResultsCount": "2",
                }
            )
        if "campaign_criterion.type = 'AD_SCHEDULE'" in gaql:
            return _response({"results": []})
        if "ad_group.cpc_bid_micros" in gaql:
            return _response(
                {
                    "results": [
                        {
                            "campaign": {"id": "11"},
                            "adGroup": {
                                "id": "21",
                                "status": "ENABLED",
                                "type": "SEARCH_STANDARD",
                                "cpcBidMicros": "1500000",
                            },
                        }
                    ]
                }
            )
        if "ad_group_ad.ad.type" in gaql:
            return _response(
                {
                    "results": [
                        {
                            "campaign": {"id": "11"},
                            "adGroup": {"id": "21"},
                            "adGroupAd": {
                                "ad": {"id": "31", "type": "RESPONSIVE_SEARCH_AD"},
                                "status": "ENABLED",
                            },
                        }
                    ]
                }
            )
        assert "metrics.cost_micros" in gaql
        assert "FROM campaign" in gaql
        assert "'2026-09-01'" in gaql
        return _response(
            {
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "segments": {"date": "2026-09-01"},
                        "metrics": {"impressions": "5", "clicks": "2", "costMicros": "5001"},
                    }
                ]
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    report = client.run_daily_report(
        CONFIG,
        GoogleAdsReportQuery(
            account_ref="account.google.1",
            report_id="report.google.campaign",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 1),
            source_timezone="UTC",
        ),
        identity=identity,
    )

    assert [campaign.campaign_client_ref for campaign in inventory.campaigns] == ["11", "12"]
    assert inventory.campaigns[0].budget is not None
    assert inventory.campaigns[0].budget.minor_units == 100
    assert inventory.ad_groups[0].bid_amount is not None
    assert inventory.ads[0].creative_capture_status == "CREATIVE_NOT_EXPOSED"
    row = report.performance_rows[0]
    assert row.metric_grain == "CAMPAIGN"
    assert row.dimension_value == "2026-09-01"
    assert row.spend is not None and row.spend.minor_units == 1
    assert row.provider_extensions == {
        "google_ads_cost_micros": "5001",
        "google_ads_rounding": "HALF_EVEN_TO_MINOR",
    }
    assert report.envelope.data_fresh_through is None
    assert report.envelope.attribution_model == "NOT_AVAILABLE_FROM_PROVIDER"
    assert report.envelope.data_quality_statuses == ["NOT_AVAILABLE_FROM_PROVIDER"]
    assert len(calls) == 7


def test_native_ad_daily_report_keeps_ad_grain_and_marks_creative_unexposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "customer.currency_code" in body["query"]:
            return _response(_identity())
        assert "FROM ad_group_ad" in body["query"]
        return _response(
            {
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "adGroup": {"id": "21"},
                        "adGroupAd": {"ad": {"id": "31"}},
                        "segments": {"date": "2026-09-01"},
                        "metrics": {"impressions": "9", "clicks": "3", "costMicros": "5000"},
                    }
                ]
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    report = client.run_daily_report(
        CONFIG,
        GoogleAdsReportQuery(
            account_ref="account.google.1",
            report_id="report.google.ad",
            query_contract_hash="b" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 1),
            source_timezone="UTC",
            level="AD",
        ),
        identity=identity,
    )
    row = report.performance_rows[0]
    assert row.resource_level == "AD"
    assert row.metric_grain == "AD"
    assert row.parent_resource_ref == "google_ads.ad_group.21"
    assert row.data_quality_statuses == ["COMPLETE", "CREATIVE_NOT_EXPOSED"]
    assert row.spend is not None and row.spend.minor_units == 0
    assert row.provider_extensions == {
        "google_ads_cost_micros": "5000",
        "google_ads_rounding": "HALF_EVEN_TO_MINOR",
    }


def test_unsafe_input_and_future_or_currency_report_stop_before_report_egress() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "customer.currency_code" in json.loads(request.content)["query"]
        return _response(_identity())

    with pytest.raises(ValidationError):
        GoogleAdsReadConfig(account_ref="account.google.1", customer_client_ref="1-234567890")
    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as future:
        client.run_daily_report(
            CONFIG,
            GoogleAdsReportQuery(
                account_ref="account.google.1",
                report_id="report.future",
                query_contract_hash="c" * 64,
                window_start=date(2026, 9, 5),
                window_end=date(2026, 9, 5),
                source_timezone="UTC",
            ),
            identity=identity,
        )
    assert future.value.code == "INVALID_REPORT_WINDOW"
    with pytest.raises(AdhelmError) as currency:
        client.run_daily_report(
            CONFIG,
            GoogleAdsReportQuery(
                account_ref="account.google.1",
                report_id="report.currency",
                query_contract_hash="d" * 64,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 1),
                source_timezone="UTC",
                currency="CAD",
            ),
            identity=identity,
        )
    assert currency.value.code == "CURRENCY_MISMATCH"
    assert len(calls) == 1


def test_malformed_nested_response_and_duplicate_json_fail_closed_without_secret() -> None:
    phase = "identity"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal phase
        if phase == "identity":
            phase = "inventory"
            return _response(_identity())
        return _response(
            {
                "results": [
                    {
                        "campaign": {
                            "id": "11",
                            "status": "ENABLED",
                            "advertisingChannelType": "SEARCH",
                            "startDate": "",
                            "endDate": "",
                            "biddingStrategyType": "MANUAL_CPC",
                            "name": "do-not-accept",
                        },
                        "campaignBudget": {"amountMicros": "1000000"},
                    }
                ]
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as malformed:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert malformed.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "do-not-accept" not in malformed.value.message

    def duplicate(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"results":[],"results":[]}',
            headers={"content-type": "application/json"},
        )

    with pytest.raises(AdhelmError) as invalid_json:
        _client(httpx.MockTransport(duplicate)).discover_identity(CONFIG)
    assert invalid_json.value.code == "PROVIDER_RESPONSE_INVALID"


def test_inventory_budget_and_bid_reject_non_lossless_micros() -> None:
    phase = "identity"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal phase
        if phase == "identity":
            phase = "budget"
            return _response(_identity())
        assert "campaign_budget.amount_micros" in json.loads(request.content)["query"]
        return _response(
            {
                "results": [
                    {
                        "campaign": {
                            "id": "11",
                            "status": "ENABLED",
                            "advertisingChannelType": "SEARCH",
                            "startDate": "",
                            "endDate": "",
                            "biddingStrategyType": "MANUAL_CPC",
                        },
                        "campaignBudget": {"amountMicros": "5001"},
                    }
                ]
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_report_query_rejects_more_than_367_inclusive_days_before_egress() -> None:
    with pytest.raises(ValidationError, match="367 days"):
        GoogleAdsReportQuery(
            account_ref="account.google.1",
            report_id="report.too-wide",
            query_contract_hash="f" * 64,
            window_start=date(2025, 1, 1),
            window_end=date(2026, 1, 3),
            source_timezone="UTC",
        )
    accepted = GoogleAdsReportQuery(
        account_ref="account.google.1",
        report_id="report.max-wide",
        query_contract_hash="f" * 64,
        window_start=date(2025, 1, 1),
        window_end=date(2026, 1, 2),
        source_timezone="UTC",
    )
    assert (accepted.window_end - accepted.window_start).days == 366

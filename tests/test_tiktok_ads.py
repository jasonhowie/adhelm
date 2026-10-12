from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.tiktok_ads import (
    TikTokAdsClient,
    TikTokAdsInventoryQuery,
    TikTokAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport) -> TikTokAdsClient:
    return TikTokAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler), {"business-api.tiktok.com"}, read_attempts=1
        ),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _response(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "message": "OK", "request_id": "request-1", "data": data}


def _page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "list": items,
        "page_info": {"page": 1, "page_size": 500, "total_number": len(items), "total_page": 1},
    }


def _report_query(account_ref: str) -> TikTokAdsReportQuery:
    return TikTokAdsReportQuery(
        account_ref=account_ref,
        report_id="report:fixture",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone="UTC",
    )


def test_identity_inventory_and_fixed_daily_campaign_and_ad_reports() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["access-token"] == "fixture-token-not-for-output"
        if request.url.path.endswith("/advertiser/info/"):
            assert request.url.params["advertiser_ids"] == '["123"]'
            return httpx.Response(
                200,
                json=_response(
                    {
                        "list": [
                            {
                                "advertiser_id": "123",
                                "currency": "USD",
                                "timezone": "UTC",
                                "status": "STATUS_ENABLE",
                            }
                        ]
                    }
                ),
            )
        if request.url.path.endswith("/campaign/get/"):
            return httpx.Response(
                200,
                json=_response(
                    _page(
                        [
                            {
                                "campaign_id": "11",
                                "operation_status": "ENABLE",
                                "objective_type": "TRAFFIC",
                                "budget_mode": "BUDGET_MODE_DAY",
                                "budget": "5.00",
                            }
                        ]
                    )
                ),
            )
        if request.url.path.endswith("/adgroup/get/"):
            return httpx.Response(
                200,
                json=_response(
                    _page(
                        [
                            {
                                "adgroup_id": "12",
                                "campaign_id": "11",
                                "operation_status": "ENABLE",
                                "budget_mode": "BUDGET_MODE_DAY",
                                "budget": "5.00",
                                "bid_type": "BID_TYPE_NO_BID",
                                "bid_price": "0.50",
                            }
                        ]
                    )
                ),
            )
        if request.url.path.endswith("/ad/get/"):
            return httpx.Response(
                200,
                json=_response(
                    _page(
                        [
                            {
                                "ad_id": "13",
                                "adgroup_id": "12",
                                "campaign_id": "11",
                                "operation_status": "ENABLE",
                            }
                        ]
                    )
                ),
            )
        assert request.url.path.endswith("/report/integrated/get/")
        assert request.url.params["service_type"] == "AUCTION"
        assert request.url.params["report_type"] == "BASIC"
        if request.url.params["data_level"] == "AUCTION_CAMPAIGN":
            assert request.url.params["dimensions"] == '["stat_time_day","campaign_id"]'
            row = {
                "dimensions": {"stat_time_day": "2026-09-01", "campaign_id": "11"},
                "metrics": {"impressions": "10", "clicks": "2", "spend": "1.25"},
            }
        else:
            assert request.url.params["data_level"] == "AUCTION_AD"
            row = {
                "dimensions": {
                    "stat_time_day": "2026-09-02",
                    "ad_id": "13",
                },
                "metrics": {"impressions": "4", "clicks": "1", "spend": "0.75"},
            }
        return httpx.Response(200, json=_response(_page([row])))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123", expected_account_client_ref="123")
    query = TikTokAdsInventoryQuery(account_ref=identity.account_ref)
    campaign = client.list_campaigns(query, identity=identity)[0]
    adgroup = client.list_adgroups(query, identity=identity)[0]
    ad = client.list_ads(query, identity=identity)[0]
    campaign_report = client.run_campaign_report(
        _report_query(identity.account_ref), identity=identity
    )
    ad_report = client.run_ad_report(
        _report_query(identity.account_ref).model_copy(update={"level": "AD"}), identity=identity
    )

    assert campaign.budget and campaign.budget.minor_units == 500
    assert campaign.budget_type == "BUDGET_MODE_DAY"
    assert adgroup.campaign_ref == campaign.campaign_ref
    assert ad.adgroup_ref == adgroup.adgroup_ref
    assert campaign_report.performance_rows[0].metric_grain == "CAMPAIGN"
    assert (
        campaign_report.performance_rows[0].spend
        and campaign_report.performance_rows[0].spend.minor_units == 125
    )
    assert "CREATIVE_NOT_EXPOSED" in ad_report.performance_rows[0].data_quality_statuses
    assert len(requests) == 6


def test_rejects_non_exact_decimal_money() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(
                200,
                json=_response(
                    {
                        "list": [
                            {
                                "advertiser_id": "123",
                                "currency": "USD",
                                "timezone": "UTC",
                                "status": "STATUS_ENABLE",
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(
            200,
            json=_response(
                _page(
                    [
                        {
                            "campaign_id": "11",
                            "operation_status": "ENABLE",
                            "objective_type": "TRAFFIC",
                            "budget_mode": "BUDGET_MODE_DAY",
                            "budget": 5.001,
                        }
                    ]
                )
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError, match="unsupported precision"):
        client.list_campaigns(
            TikTokAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )


def test_unknown_response_field_and_incomplete_page_fail_closed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(
                200,
                json=_response(
                    {
                        "list": [
                            {
                                "advertiser_id": "123",
                                "currency": "USD",
                                "timezone": "UTC",
                                "status": "STATUS_ENABLE",
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(
            200,
            json=_response(
                _page(
                    [
                        {
                            "campaign_id": "11",
                            "operation_status": "ENABLE",
                            "objective_type": "TRAFFIC",
                            "budget_mode": "BUDGET_MODE_DAY",
                            "budget": "5.00",
                            "campaign_name": "must-not-be-accepted",
                        }
                    ]
                )
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError, match="unsupported field"):
        client.list_campaigns(
            TikTokAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert calls == 2


def test_invalid_identity_or_unbounded_window_fail_before_egress() -> None:
    with pytest.raises(ValidationError):
        TikTokAdsReportQuery(
            account_ref="tiktok_ads.advertiser.1",
            report_id="report:fixture",
            query_contract_hash="a" * 64,
            window_start=date(2025, 1, 1),
            window_end=date(2026, 9, 4),
            source_timezone="UTC",
        )

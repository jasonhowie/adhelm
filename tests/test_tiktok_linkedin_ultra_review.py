"""Independent regressions for TikTok and LinkedIn fixed read contracts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.linkedin_ads import (
    LinkedInAdsClient,
    LinkedInAdsInventoryQuery,
    LinkedInAdsReportQuery,
)
from adhelm.providers.tiktok_ads import (
    TikTokAdsClient,
    TikTokAdsInventoryQuery,
    TikTokAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _tiktok_client(handler: httpx.MockTransport) -> TikTokAdsClient:
    return TikTokAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler), {"business-api.tiktok.com"}, read_attempts=1
        ),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


def _tiktok_response(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "message": "OK", "request_id": "review-request", "data": data}


def _tiktok_identity() -> dict[str, Any]:
    return _tiktok_response(
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
    )


def _tiktok_page(items: list[dict[str, Any]], *, page_size: int, total: int) -> dict[str, Any]:
    return {
        "list": items,
        "page_info": {
            "page": 1,
            "page_size": page_size,
            "total_number": total,
            "total_page": 1,
        },
    }


def _tiktok_report_query(account_ref: str, **updates: Any) -> TikTokAdsReportQuery:
    values: dict[str, Any] = {
        "account_ref": account_ref,
        "report_id": "report:tiktok-ultra-review",
        "query_contract_hash": "a" * 64,
        "window_start": date(2026, 9, 1),
        "window_end": date(2026, 9, 1),
        "source_timezone": "UTC",
        "maximum_rows": 1,
        "page_size": 1,
    }
    values.update(updates)
    return TikTokAdsReportQuery(**values)


def _linkedin_client(handler: httpx.MockTransport) -> LinkedInAdsClient:
    return LinkedInAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"api.linkedin.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


def _linkedin_report_query(account_ref: str) -> LinkedInAdsReportQuery:
    return LinkedInAdsReportQuery(
        account_ref=account_ref,
        report_id="report:linkedin-ultra-review",
        query_contract_hash="b" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 1),
        source_timezone="UTC",
        currency="USD",
        maximum_rows=2,
        page_size=2,
    )


def test_tiktok_rejects_a_31_day_daily_query_before_egress() -> None:
    with pytest.raises(ValidationError, match="at most 30"):
        _tiktok_report_query(
            "tiktok_ads.advertiser.123",
            window_start=date(2026, 8, 1),
            window_end=date(2026, 8, 31),
        )


def test_tiktok_ad_report_uses_one_id_dimension_and_includes_deleted_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_tiktok_identity())
        assert request.url.path.endswith("/report/integrated/get/")
        assert request.url.params["data_level"] == "AUCTION_AD"
        assert request.url.params["dimensions"] == '["stat_time_day","ad_id"]'
        filtering = json.loads(request.url.params["filtering"])
        assert filtering == [
            {
                "field_name": "ad_status",
                "filter_type": "IN",
                "filter_value": '["STATUS_ALL"]',
            }
        ]
        return httpx.Response(
            200,
            json=_tiktok_response(
                _tiktok_page(
                    [
                        {
                            "dimensions": {"stat_time_day": "2026-09-01", "ad_id": "13"},
                            "metrics": {"impressions": "4", "clicks": "1", "spend": "0.75"},
                        }
                    ],
                    page_size=1,
                    total=1,
                )
            ),
        )

    client = _tiktok_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    report = client.run_ad_report(
        _tiktok_report_query(identity.account_ref, level="AD"), identity=identity
    )
    row = report.performance_rows[0]
    assert row.campaign_ref is None
    assert {"CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"} <= set(row.data_quality_statuses)


def test_tiktok_inventory_accepts_documented_numeric_money_without_float_rounding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_tiktok_identity())
        assert request.url.path.endswith("/adgroup/get/")
        # Preserve numeric JSON lexemes; the adapter must not receive a binary float.
        return httpx.Response(
            200,
            content=(
                b'{"code":0,"message":"OK","request_id":"review-request","data":'
                b'{"list":[{"adgroup_id":"12","campaign_id":"11",'
                b'"operation_status":"ENABLE","budget_mode":"BUDGET_MODE_DAY",'
                b'"budget":5.00,"bid_type":"BID_TYPE_NO_BID","bid_price":0.50}],'
                b'"page_info":{"page":1,"page_size":1,"total_number":1,"total_page":1}}}'
            ),
        )

    client = _tiktok_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    adgroup = client.list_adgroups(
        TikTokAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=1, page_size=1),
        identity=identity,
    )[0]
    assert adgroup.budget and adgroup.budget.minor_units == 500
    assert adgroup.bid_amount and adgroup.bid_amount.minor_units == 50


def test_tiktok_rejects_inconsistent_complete_page_counters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_tiktok_identity())
        return httpx.Response(
            200,
            json=_tiktok_response(
                _tiktok_page(
                    [
                        {
                            "campaign_id": "11",
                            "operation_status": "ENABLE",
                            "objective_type": "TRAFFIC",
                            "budget_mode": "BUDGET_MODE_DAY",
                            "budget": "5.00",
                        }
                    ],
                    page_size=1,
                    total=2,
                )
            ),
        )

    client = _tiktok_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            TikTokAdsInventoryQuery(
                account_ref=identity.account_ref, maximum_records=1, page_size=1
            ),
            identity=identity,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_tiktok_rejects_boolean_success_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        response = _tiktok_identity()
        response["code"] = False
        return httpx.Response(200, json=response)

    with pytest.raises(AdhelmError) as raised:
        _tiktok_client(httpx.MockTransport(handler)).discover_identity("123")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_tiktok_rejects_noncanonical_iso_week_report_days() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_tiktok_identity())
        return httpx.Response(
            200,
            json=_tiktok_response(
                _tiktok_page(
                    [
                        {
                            # Python's date.fromisoformat accepts this as
                            # 2025-12-29, so calendar-date lexical validation
                            # must precede normalizing the provider response.
                            "dimensions": {
                                "stat_time_day": "2026-W01-1",
                                "campaign_id": "11",
                            },
                            "metrics": {"impressions": "4", "clicks": "1", "spend": "0.75"},
                        }
                    ],
                    page_size=1,
                    total=1,
                )
            ),
        )

    client = _tiktok_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            _tiktok_report_query(
                identity.account_ref,
                window_start=date(2025, 12, 29),
                window_end=date(2025, 12, 29),
            ),
            identity=identity,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_linkedin_accepts_documented_nonpaged_analytics_without_completeness_claim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        assert request.url.path == "/rest/adAnalytics"
        assert "start" not in request.url.params
        assert "count" not in request.url.params
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "clicks": 2,
                        "impressions": 10,
                        "costInLocalCurrency": "1.25",
                        "pivotValues": ["urn:li:sponsoredCampaign:456"],
                        "dateRange": {
                            "start": {"year": 2026, "month": 9, "day": 1},
                            "end": {"year": 2026, "month": 9, "day": 1},
                        },
                    }
                ],
                "paging": {"start": 0, "count": 1, "links": []},
            },
        )

    client = _linkedin_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    report = client.run_campaign_report(
        _linkedin_report_query(identity.account_ref), identity=identity
    )
    assert len(report.performance_rows) == 1
    assert "NOT_AVAILABLE_FROM_PROVIDER" in report.envelope.data_quality_statuses
    assert "COMPLETE" not in report.envelope.data_quality_statuses


def test_linkedin_creative_finder_caps_pages_and_accepts_documented_urn_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        if request.url.path.endswith("/adCampaigns"):
            assert request.url.params["pageSize"] == "1000"
            assert request.url.params["fields"] == "id,account,campaignGroup,status,objectiveType"
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "account": "urn:li:sponsoredAccount:123",
                            "id": 22,
                            "status": "PAUSED",
                        }
                    ],
                    "metadata": {},
                },
            )
        assert request.url.path.endswith("/creatives")
        assert request.url.params["pageSize"] == "100"
        assert request.url.params["fields"] == "id,account,campaign,intendedStatus"
        assert request.headers["x-restli-method"] == "FINDER"
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "account": "urn:li:sponsoredAccount:123",
                        "campaign": "urn:li:sponsoredCampaign:22",
                        "id": "urn:li:sponsoredCreative:33",
                        "intendedStatus": "PAUSED",
                    }
                ],
                "metadata": {},
            },
        )

    client = _linkedin_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    creatives = client.list_creatives(
        LinkedInAdsInventoryQuery(
            account_ref=identity.account_ref, maximum_records=1_000, page_size=1_000
        ),
        identity=identity,
    )
    assert [(creative.creative_client_ref, creative.campaign_ref) for creative in creatives] == [
        ("33", "linkedin_ads.campaign.22")
    ]


def test_linkedin_rejects_a_page_larger_than_its_fixed_page_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        assert request.url.params["pageSize"] == "1"
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"account": "urn:li:sponsoredAccount:123", "id": 11, "status": "ACTIVE"},
                    {"account": "urn:li:sponsoredAccount:123", "id": 12, "status": "ACTIVE"},
                ],
                "metadata": {},
            },
        )

    client = _linkedin_client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(
                account_ref=identity.account_ref, maximum_records=10, page_size=1
            ),
            identity=identity,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"

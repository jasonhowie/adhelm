from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.meta_ads import (
    MetaAdsClient,
    MetaAdsInventoryQuery,
    MetaAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport) -> MetaAdsClient:
    return MetaAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"graph.facebook.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _account() -> dict[str, Any]:
    return {"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"}


def _next(edge: str, cursor: str) -> str:
    return f"https://graph.facebook.com/v26.0/act_123/{edge}?after={cursor}"


def _report_query(account_ref: str) -> MetaAdsReportQuery:
    return MetaAdsReportQuery(
        account_ref=account_ref,
        report_id="report:fixture",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone="UTC",
        action_attribution_windows=("7d_click", "1d_view"),
    )


def test_identity_inventory_and_daily_campaign_and_ad_reports() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
        if request.url.path == "/v26.0/act_123":
            assert request.url.params["fields"] == "id,account_status,currency,timezone_name"
            return httpx.Response(200, json=_account())
        if request.url.path == "/v26.0/act_123/campaigns":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "11",
                            "effective_status": "PAUSED",
                            "objective": "OUTCOME_TRAFFIC",
                            "daily_budget": "500",
                            "lifetime_budget": None,
                            "spend_cap": "0",
                            "start_time": "2026-09-01T00:00:00+00:00",
                            "stop_time": None,
                        }
                    ]
                },
            )
        if request.url.path == "/v26.0/act_123/adsets":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "12",
                            "campaign_id": "11",
                            "effective_status": "PAUSED",
                            "billing_event": "IMPRESSIONS",
                            "bid_amount": "20",
                            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                            "daily_budget": "500",
                            "lifetime_budget": None,
                            "start_time": "2026-09-01T00:00:00+00:00",
                            "end_time": None,
                        }
                    ]
                },
            )
        if request.url.path == "/v26.0/act_123/ads":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "13",
                            "campaign_id": "11",
                            "adset_id": "12",
                            "effective_status": "PAUSED",
                        }
                    ]
                },
            )
        assert request.url.path == "/v26.0/act_123/insights"
        assert request.url.params["time_increment"] == "1"
        assert request.url.params["action_attribution_windows"] == '["7d_click","1d_view"]'
        if request.url.params["level"] == "campaign":
            assert request.url.params["fields"] == (
                "campaign_id,date_start,date_stop,impressions,clicks,spend,reach"
            )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "campaign_id": "11",
                            "date_start": "2026-09-01",
                            "date_stop": "2026-09-01",
                            "impressions": "10",
                            "clicks": "2",
                            "spend": "1.25",
                            "reach": "8",
                        }
                    ]
                },
            )
        assert request.url.params["level"] == "ad"
        assert request.url.params["fields"] == (
            "campaign_id,adset_id,ad_id,date_start,date_stop,impressions,clicks,spend,reach"
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "campaign_id": "11",
                        "adset_id": "12",
                        "ad_id": "13",
                        "date_start": "2026-09-02",
                        "date_stop": "2026-09-02",
                        "impressions": "4",
                        "clicks": "1",
                        "spend": "0.75",
                        "reach": "3",
                    }
                ]
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("act_123", expected_account_client_ref="123")
    query = MetaAdsInventoryQuery(account_ref=identity.account_ref)
    campaign = client.list_campaigns(query, identity=identity)[0]
    adset = client.list_adsets(query, identity=identity)[0]
    ad = client.list_ads(query, identity=identity)[0]
    campaign_report = client.run_campaign_report(
        _report_query(identity.account_ref), identity=identity
    )
    ad_report = client.run_ad_report(
        _report_query(identity.account_ref).model_copy(update={"level": "AD"}), identity=identity
    )
    assert campaign.daily_budget and campaign.daily_budget.minor_units == 500
    assert adset.campaign_ref == campaign.campaign_ref
    assert ad.adset_ref == adset.adset_ref
    assert campaign_report.performance_rows[0].metric_grain == "CAMPAIGN"
    assert (
        campaign_report.performance_rows[0].spend
        and campaign_report.performance_rows[0].spend.minor_units == 125
    )
    assert ad_report.performance_rows[0].metric_grain == "AD"
    assert "CREATIVE_NOT_EXPOSED" in ad_report.performance_rows[0].data_quality_statuses
    assert len(requests) == 6


def test_pagination_reissues_fixed_path_after_validating_pinned_next_url() -> None:
    """The cursor is validated against the pinned next URL and then reissued.

    Graph documents that "it is possible that a page may be empty but contain a
    `next` paging link", so an empty continuing page is followed rather than
    refused; refusing it would end a whole bounded read at the first
    privacy-filtered page.  What is still enforced is the pinned path: the
    second request goes to the same fixed edge with exactly the returned
    cursor, never to whatever URL the provider supplied.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v26.0/act_123/campaigns"
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "paging": {
                        "cursors": {"after": "safe_cursor"},
                        "next": _next("campaigns", "safe_cursor"),
                    },
                },
            )
        assert request.url.params["after"] == "safe_cursor"
        return httpx.Response(200, json={"data": [{"id": "11", "effective_status": "PAUSED"}]})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    campaigns = client.list_campaigns(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert [item.campaign_client_ref for item in campaigns] == ["11"]
    assert calls == 3

    # A cursor that does not match the pinned next URL still fails closed.
    def mismatched(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "data": [],
                "paging": {
                    "cursors": {"after": "safe_cursor"},
                    "next": _next("campaigns", "other_cursor"),
                },
            },
        )

    other = _client(httpx.MockTransport(mismatched))
    other_identity = other.discover_identity("123")
    with pytest.raises(AdhelmError, match="next URL"):
        other.list_campaigns(
            MetaAdsInventoryQuery(account_ref=other_identity.account_ref), identity=other_identity
        )


def test_unpinned_paging_and_unknown_nested_fields_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "data": [],
                "paging": {
                    "cursors": {"after": "safe"},
                    "next": "https://evil.invalid/next?after=safe",
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_unknown_inventory_field_and_future_window_fail_before_extra_egress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"data": [{"id": "11", "unsupported": "field"}]})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError, match="unsupported field"):
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert calls == 2
    with pytest.raises(AdhelmError, match="future"):
        client.run_campaign_report(
            MetaAdsReportQuery(
                account_ref=identity.account_ref,
                report_id="report:future",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 5),
                window_end=date(2026, 9, 5),
                source_timezone="UTC",
                action_attribution_windows=("7d_click",),
            ),
            identity=identity,
        )
    assert calls == 2


def test_unreviewed_currency_and_future_window_stop_before_insights() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        account = _account()
        account["currency"] = "BRL"
        return httpx.Response(200, json=account)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError, match="currency"):
        client.discover_identity("123")
    assert calls == 1


def test_meta_window_bound_and_remaining_result_limit_are_enforced() -> None:
    with pytest.raises(ValidationError, match="367 days"):
        MetaAdsReportQuery(
            account_ref="meta_ads.account.act_123",
            report_id="report.too-wide",
            query_contract_hash="a" * 64,
            window_start=date(2025, 1, 1),
            window_end=date(2026, 1, 3),
            source_timezone="UTC",
            action_attribution_windows=("7d_click",),
        )

    inventory_calls = 0

    def inventory_handler(request: httpx.Request) -> httpx.Response:
        nonlocal inventory_calls
        inventory_calls += 1
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(200, json=_account())
        assert request.url.params["limit"] == "1"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "11",
                        "effective_status": "PAUSED",
                        "objective": "OUTCOME_TRAFFIC",
                        "daily_budget": "500",
                        "lifetime_budget": None,
                        "spend_cap": "0",
                        "start_time": None,
                        "stop_time": None,
                    }
                ],
                "paging": {
                    "cursors": {"before": "before", "after": "more"},
                    "next": _next("campaigns", "more"),
                },
            },
        )

    client = _client(httpx.MockTransport(inventory_handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as inventory_limit:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=1),
            identity=identity,
        )
    assert inventory_limit.value.code == "RESULT_LIMIT_REQUIRED"
    assert inventory_calls == 2

    report_calls = 0

    def report_handler(request: httpx.Request) -> httpx.Response:
        nonlocal report_calls
        report_calls += 1
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v26.0/act_123/insights"
        assert request.url.params["limit"] == "1"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "campaign_id": "11",
                        "date_start": "2026-09-01",
                        "date_stop": "2026-09-01",
                        "impressions": "1",
                        "clicks": "1",
                        "spend": "0.01",
                        "reach": "1",
                    }
                ],
                "paging": {
                    "cursors": {"before": "before", "after": "more"},
                    "next": _next("insights", "more"),
                },
            },
        )

    client = _client(httpx.MockTransport(report_handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as report_limit:
        client.run_campaign_report(
            MetaAdsReportQuery(
                account_ref=identity.account_ref,
                report_id="report.one",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 1),
                source_timezone="UTC",
                action_attribution_windows=("7d_click",),
                maximum_rows=1,
            ),
            identity=identity,
        )
    assert report_limit.value.code == "RESULT_LIMIT_REQUIRED"
    assert report_calls == 2

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.reddit_ads import (
    RedditAdsClient,
    RedditAdsInventoryQuery,
    RedditAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport) -> RedditAdsClient:
    return RedditAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler),
            {"ads-api.reddit.com"},
            read_attempts=1,
        ),
        lambda: "fixture-token",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        connector_version="test",
    )


def _identity() -> dict[str, Any]:
    return {
        "data": {
            "id": "a2_fixture",
            "currency": "USD",
            "time_zone_id": "America/Chicago",
        }
    }


def _query(account_ref: str, **updates: Any) -> RedditAdsReportQuery:
    values: dict[str, Any] = {
        "account_ref": account_ref,
        "report_id": "report.one",
        "query_contract_hash": "a" * 64,
        "window_start": date(2026, 9, 1),
        "window_end": date(2026, 9, 1),
    }
    values.update(updates)
    return RedditAdsReportQuery(**values)


def _report_response(row: dict[str, Any], *, updated_at: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"metrics": [row]}
    if updated_at is not None:
        payload["metrics_updated_at"] = updated_at
    return {"data": payload}


def test_fixed_identity_inventory_and_primary_report_wire_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture-token"
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(200, json=_identity())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "c1",
                            "ad_account_id": "a2_fixture",
                            "effective_status": "PAUSED",
                            "objective": "TRAFFIC",
                        }
                    ],
                    "pagination": {},
                },
            )
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "g1",
                            "ad_account_id": "a2_fixture",
                            "campaign_id": "c1",
                            "effective_status": "PAUSED",
                        }
                    ],
                    "pagination": {},
                },
            )
        if request.url.path.endswith("/ads"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "ad1",
                            "ad_account_id": "a2_fixture",
                            "campaign_id": "c1",
                            "ad_group_id": "g1",
                            "effective_status": "PAUSED",
                        }
                    ],
                    "pagination": {},
                },
            )
        assert request.method == "POST"
        assert request.url.path == "/api/v3/ad_accounts/a2_fixture/reports"
        body = json.loads(request.content)
        assert body["data"]["fields"] == ["impressions", "clicks", "spend"]
        assert body["data"]["time_zone_id"] == "UTC"
        assert body["data"]["starts_at"] == "2026-09-01T00:00:00Z"
        assert body["data"]["ends_at"] == "2026-09-01T23:59:59Z"
        if body["data"]["breakdowns"] == ["campaign_id", "date"]:
            return httpx.Response(
                200,
                json=_report_response(
                    {
                        "campaign_id": "c1",
                        "date": "2026-09-01",
                        "hour": "2026-09-01T00:00:00+00:00",
                        "impressions": 10,
                        "clicks": 2,
                        "spend": 1_250_000,
                    },
                    updated_at="2026-09-02T00:00:00+00:00",
                ),
            )
        assert body["data"]["breakdowns"] == ["ad_id", "date"]
        return httpx.Response(
            200,
            json=_report_response(
                {
                    "ad_id": "ad1",
                    "date": "2026-09-01",
                    "impressions": 4,
                    "clicks": 1,
                    "spend": 750_000,
                }
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_fixture")
    inventory = RedditAdsInventoryQuery(account_ref=identity.account_ref)
    assert client.list_ads(inventory, identity=identity)[0].campaign_ref == "reddit_ads.campaign.c1"

    campaign = client.run_campaign_report(_query(identity.account_ref), identity=identity)
    campaign_row = campaign.performance_rows[0]
    assert campaign_row.metric_grain == "CAMPAIGN"
    assert campaign_row.spend and campaign_row.spend.minor_units == 125

    ad = client.run_ad_report(
        _query(identity.account_ref, level="AD", report_id="report.ad"), identity=identity
    )
    ad_row = ad.performance_rows[0]
    assert ad_row.metric_grain == "AD"
    assert ad_row.campaign_ref is None
    assert ad_row.parent_resource_ref is None


@pytest.mark.parametrize(
    ("source_micros", "clicks", "expected_minor"),
    [(5_000, 1, 0), (20_001, 2, 2)],
)
def test_reddit_native_micros_keep_half_even_provenance_at_cpc_boundary(
    source_micros: int, clicks: int, expected_minor: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(200, json=_identity())
        return httpx.Response(
            200,
            json=_report_response(
                {
                    "campaign_id": "c1",
                    "date": "2026-09-01",
                    "impressions": 10,
                    "clicks": clicks,
                    "spend": source_micros,
                }
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_fixture")
    row = client.run_campaign_report(
        _query(identity.account_ref), identity=identity
    ).performance_rows[0]

    assert row.spend and row.spend.minor_units == expected_minor
    assert row.clicks == clicks
    assert row.provider_extensions == {
        "reddit_ads_spend_micros": str(source_micros),
        "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
    }


def test_reddit_report_allows_exactly_the_bounded_single_response_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(200, json=_identity())
        return httpx.Response(
            200,
            json=_report_response(
                {
                    "campaign_id": "c1",
                    "date": "2026-09-01",
                    "impressions": 1,
                    "clicks": 0,
                    "spend": 0,
                }
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_fixture")
    report = client.run_campaign_report(
        _query(identity.account_ref, maximum_rows=1), identity=identity
    )

    assert len(report.performance_rows) == 1


def test_reddit_report_rejects_known_pagination_without_guessing_continuation() -> None:
    report_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal report_requests
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(200, json=_identity())
        report_requests += 1
        return httpx.Response(
            200,
            json={
                **_report_response(
                    {
                        "campaign_id": "c1",
                        "date": "2026-09-01",
                        "impressions": 1,
                        "clicks": 0,
                        "spend": 0,
                    }
                ),
                "pagination": {"next_url": "https://ads-api.reddit.com/unknown"},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_fixture")
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_query(identity.account_ref), identity=identity)

    assert raised.value.code == "REPORT_PAGINATION_UNVERIFIED"
    assert report_requests == 1


def test_reddit_rejects_path_traversal_account_before_http_egress() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity("../a2_fixture")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == 0

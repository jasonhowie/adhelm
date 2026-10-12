"""Independent regressions for Reddit Ads fixed read contracts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.reddit_ads import (
    RedditAdsClient,
    RedditAdsIdentity,
    RedditAdsInventoryQuery,
    RedditAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport) -> RedditAdsClient:
    return RedditAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"ads-api.reddit.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


def _identity_response() -> dict[str, object]:
    return {"data": {"id": "a2_review", "currency": "USD", "time_zone_id": "UTC"}}


def _query(account_ref: str, **updates: object) -> RedditAdsReportQuery:
    values: dict[str, object] = {
        "account_ref": account_ref,
        "report_id": "report:reddit-ultra-review",
        "query_contract_hash": "d" * 64,
        "window_start": date(2026, 9, 1),
        "window_end": date(2026, 9, 1),
        "maximum_rows": 10,
    }
    values.update(updates)
    return RedditAdsReportQuery(**values)


def test_reddit_campaign_report_uses_documented_date_and_nested_metrics_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/ad_accounts/a2_review":
            return httpx.Response(200, json=_identity_response())
        assert request.method == "POST"
        assert request.url.path == "/api/v3/ad_accounts/a2_review/reports"
        body = json.loads(request.content)
        assert body["data"]["breakdowns"] == ["campaign_id", "date"]
        assert body["data"]["fields"] == ["impressions", "clicks", "spend"]
        assert body["data"]["starts_at"] == "2026-09-01T00:00:00Z"
        return httpx.Response(
            200,
            json={
                "data": {
                    "metrics": [
                        {
                            "campaign_id": "c1",
                            "date": "2026-09-01",
                            # The official example includes this field even
                            # for a daily report. It is permitted then dropped.
                            "hour": "2026-09-01T00:00:00+00:00",
                            "impressions": 10,
                            "clicks": 2,
                            "spend": 1_250_000,
                        }
                    ],
                    "metrics_updated_at": "2026-09-02T01:02:03.123000+00:00",
                }
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_review")
    report = client.run_campaign_report(_query(identity.account_ref), identity=identity)
    row = report.performance_rows[0]
    assert row.dimension_value == "2026-09-01"
    assert row.resource_ref == "reddit_ads.campaign.c1"
    assert row.spend and row.spend.minor_units == 125
    assert "COMPLETE" not in report.envelope.data_quality_statuses


def test_reddit_ad_report_uses_native_ad_date_grain_without_fabricated_parents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/ad_accounts/a2_review":
            return httpx.Response(200, json=_identity_response())
        body = json.loads(request.content)
        assert body["data"]["breakdowns"] == ["ad_id", "date"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "metrics": [
                        {
                            "ad_id": "ad1",
                            "date": "2026-09-01",
                            "hour": "2026-09-01T00:00:00+00:00",
                            "impressions": 10,
                            "clicks": 2,
                            "spend": 1_250_000,
                        }
                    ],
                    "metrics_updated_at": "2026-09-02T01:02:03.123000+00:00",
                }
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_review")
    report = client.run_ad_report(_query(identity.account_ref, level="AD"), identity=identity)
    row = report.performance_rows[0]
    assert row.resource_ref == "reddit_ads.ad.ad1"
    assert row.campaign_ref is None
    assert row.parent_resource_ref is None
    assert {"CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"} <= set(row.data_quality_statuses)


def test_reddit_report_continuation_fails_explicitly_until_its_method_is_reviewed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/api/v3/ad_accounts/a2_review":
            return httpx.Response(200, json=_identity_response())
        return httpx.Response(
            200,
            json={
                "data": {"metrics": [], "metrics_updated_at": "2026-09-02T01:02:03+00:00"},
                "pagination": {
                    "next_url": (
                        "https://ads-api.reddit.com/api/v3/ad_accounts/a2_review/reports?after=x"
                    )
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_review")
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_query(identity.account_ref), identity=identity)
    assert raised.value.code == "REPORT_PAGINATION_UNVERIFIED"
    assert calls == 2


@pytest.mark.parametrize(
    ("spend", "expected_minor", "error"),
    [
        (9_223_372_036_854_775_807, 922_337_203_685_478, None),
        (9_223_372_036_854_775_808, None, "PROVIDER_RESPONSE_INVALID"),
    ],
)
def test_reddit_spend_micros_preserves_the_signed_int64_contract(
    spend: int, expected_minor: int | None, error: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/ad_accounts/a2_review":
            return httpx.Response(200, json=_identity_response())
        return httpx.Response(
            200,
            json={
                "data": {
                    "metrics": [
                        {
                            "campaign_id": "c1",
                            "date": "2026-09-01",
                            "impressions": 10,
                            "clicks": 2,
                            "spend": spend,
                        }
                    ],
                    "metrics_updated_at": "2026-09-02T01:02:03+00:00",
                }
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_review")
    if error is not None:
        with pytest.raises(AdhelmError) as raised:
            client.run_campaign_report(_query(identity.account_ref), identity=identity)
        assert raised.value.code == error
        return
    report = client.run_campaign_report(_query(identity.account_ref), identity=identity)
    assert report.performance_rows[0].spend
    assert report.performance_rows[0].spend.minor_units == expected_minor


def test_reddit_query_model_does_not_invent_a_24_month_request_limit() -> None:
    # Delivery retention is checked against the connector observation date at
    # execution. It is not a provider-documented maximum request duration.
    query = _query(
        "reddit_ads.account.a2_review",
        window_start=date(2023, 9, 1),
        window_end=date(2026, 9, 1),
    )
    assert query.window_start == date(2023, 9, 1)


def test_reddit_list_cursor_preserves_encoded_repeated_query_bytes() -> None:
    calls = 0
    cursor = b"after=a%2Bb&scope=one%20two&scope=one%20two"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/api/v3/ad_accounts/a2_review":
            return httpx.Response(200, json=_identity_response())
        if calls == 2:
            assert request.url.query == b""
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "c1",
                            "ad_account_id": "a2_review",
                            "effective_status": "PAUSED",
                        }
                    ],
                    "pagination": {
                        "next_url": (
                            "https://ads-api.reddit.com/api/v3/ad_accounts/a2_review/"
                            "campaigns?after=a%2Bb&scope=one%20two&scope=one%20two"
                        )
                    },
                },
            )
        assert request.url.path == "/api/v3/ad_accounts/a2_review/campaigns"
        assert request.url.query == cursor
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c2",
                        "ad_account_id": "a2_review",
                        "effective_status": "PAUSED",
                    }
                ],
                "pagination": {},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_review")
    campaigns = client.list_campaigns(
        RedditAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )
    assert [campaign.campaign_client_ref for campaign in campaigns] == ["c1", "c2"]
    assert calls == 3


def test_reddit_follows_documented_api_root_pagination_cursor() -> None:
    """Reddit documents this root-path cursor shape for a paginated list."""

    calls = 0
    cursor = b"page.token=nextToken"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/api/v3/ad_accounts/a2_review":
            return httpx.Response(200, json=_identity_response())
        if calls == 2:
            assert request.url.path == "/api/v3/ad_accounts/a2_review/campaigns"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "c1",
                            "ad_account_id": "a2_review",
                            "effective_status": "PAUSED",
                        }
                    ],
                    "pagination": {
                        # Official Add Adaptive Time Zone Parameters example:
                        # `/api/v3?page.token=nextToken`.
                        "next_url": "https://ads-api.reddit.com/api/v3?page.token=nextToken"
                    },
                },
            )
        assert request.url.path == "/api/v3"
        assert request.url.query == cursor
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c2",
                        "ad_account_id": "a2_review",
                        "effective_status": "PAUSED",
                    }
                ],
                "pagination": {},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("a2_review")
    campaigns = client.list_campaigns(
        RedditAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=2),
        identity=identity,
    )

    assert [campaign.campaign_client_ref for campaign in campaigns] == ["c1", "c2"]
    assert calls == 3


def test_opaque_cursor_cannot_be_reused_for_a_mutation() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        {"ads-api.reddit.com"},
        read_attempts=1,
    )
    with pytest.raises(AdhelmError) as raised:
        transport.request(
            "POST",
            "https://ads-api.reddit.com/api/v3/ad_accounts/a2_review/campaigns",
            mutation=True,
            opaque_read_query=b"after=opaque%2Bcursor",
        )
    assert raised.value.code == "INVALID_CURSOR_CONTEXT"
    assert not called


@pytest.mark.parametrize("operation", ["inventory", "report"])
def test_reddit_revalidates_direct_identity_before_account_path_egress(operation: str) -> None:
    """A typed identity is still untrusted at an adapter's public boundary."""

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = _client(httpx.MockTransport(handler))
    # SafeRef permits this string, but it is not a Reddit resource ID.  Without
    # an adapter-boundary check, httpx normalizes it outside /ad_accounts/{id}.
    identity = RedditAdsIdentity(
        account_ref="reddit_ads.account.forged",
        account_client_ref="../x",
        currency="USD",
    )
    if operation == "inventory":
        with pytest.raises(AdhelmError):
            client.list_campaigns(
                RedditAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
    else:
        with pytest.raises(AdhelmError):
            client.run_campaign_report(_query(identity.account_ref), identity=identity)

    assert calls == 0

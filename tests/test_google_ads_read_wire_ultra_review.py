from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.google_ads import (
    GoogleAdsClient,
    GoogleAdsInventoryQuery,
    GoogleAdsReadConfig,
    GoogleAdsReportQuery,
)
from adhelm.transport import SafeHttpClient

_CONFIG = GoogleAdsReadConfig(
    account_ref="account.google.wire",
    customer_client_ref="1234567890",
    login_customer_client_ref="0987654321",
)
_NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
_RESOURCE_NAME_PARAMETER = "PARAMETERS omit_unselected_resource_names = true"


def _client(handler: httpx.MockTransport) -> GoogleAdsClient:
    return GoogleAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler, trust_env=False),
            {"googleads.googleapis.com"},
            sleep=lambda _seconds: None,
        ),
        lambda: "wire-access-token",
        now=lambda: _NOW,
    )


def _response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _identity_payload(*, metadata: dict[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "results": [
            {
                "customer": {
                    "id": "1234567890",
                    "currencyCode": "USD",
                    "timeZone": "UTC",
                }
            }
        ],
        "totalResultsCount": "1",
    }
    if metadata is not None:
        payload.update(metadata)
    return payload


def _campaign_inventory_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {
                    "id": "11",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                    "startDateTime": "2026-01-01 00:00:00",
                    "endDateTime": "",
                    "biddingStrategyType": "MANUAL_CPC",
                },
                "campaignBudget": {"amountMicros": "1000000"},
            }
        ],
        "totalResultsCount": "1",
    }


def _ad_group_inventory_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {"id": "11"},
                "adGroup": {
                    "id": "21",
                    "status": "ENABLED",
                    "type": "SEARCH_STANDARD",
                    "cpcBidMicros": "1000000",
                },
            }
        ],
        "totalResultsCount": "1",
    }


def _ad_inventory_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {"id": "11"},
                "adGroup": {"id": "21"},
                "adGroupAd": {
                    "ad": {"id": "31", "type": "RESPONSIVE_SEARCH_AD"},
                    "status": "ENABLED",
                },
            }
        ],
        "totalResultsCount": "1",
    }


def _report_payload(
    *, ad_level: bool, metadata: dict[str, object] | None = None
) -> dict[str, object]:
    row: dict[str, object] = {
        "campaign": {"id": "11"},
        "segments": {"date": "2026-09-01"},
        "metrics": {"impressions": "5", "clicks": "2", "costMicros": "5000"},
    }
    if ad_level:
        row.update(
            {
                "adGroup": {"id": "21"},
                "adGroupAd": {"ad": {"id": "31"}},
            }
        )
    payload: dict[str, object] = {"results": [row], "totalResultsCount": "1"}
    if metadata is not None:
        payload.update(metadata)
    return payload


def _campaign_report() -> GoogleAdsReportQuery:
    return GoogleAdsReportQuery(
        account_ref=_CONFIG.account_ref,
        report_id="report.google.wire.campaign",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 1),
        source_timezone="UTC",
    )


def _ad_report() -> GoogleAdsReportQuery:
    return GoogleAdsReportQuery(
        account_ref=_CONFIG.account_ref,
        report_id="report.google.wire.ad",
        query_contract_hash="b" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 1),
        source_timezone="UTC",
        level="AD",
    )


def _assert_fixed_request(request: httpx.Request) -> str:
    assert request.method == "POST"
    assert request.url == httpx.URL(
        "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
    )
    assert request.headers["authorization"] == "Bearer wire-access-token"
    assert "developer-token" not in request.headers
    assert request.headers["login-customer-id"] == "0987654321"
    body = json.loads(request.content)
    assert set(body) == {"query", "searchSettings"}
    assert body["searchSettings"] == {"returnTotalResultsCount": True}
    assert "returnTotalResultsCount" not in {key for key in body if key != "searchSettings"}
    assert "pageSize" not in body
    query = body["query"]
    assert type(query) is str
    assert query.endswith(_RESOURCE_NAME_PARAMETER)
    return query


def test_all_fixed_v25_reads_suppress_implicit_resource_names() -> None:
    observed_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = _assert_fixed_request(request)
        observed_queries.append(query)
        if "FROM customer" in query:
            return _response(_identity_payload())
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return _response({"results": []})
        if "campaign_budget.amount_micros" in query:
            return _response(_campaign_inventory_payload())
        if "ad_group.cpc_bid_micros" in query:
            return _response(_ad_group_inventory_payload())
        if "ad_group_ad.ad.type" in query:
            return _response(_ad_inventory_payload())
        if "FROM ad_group_ad" in query:
            return _response(_report_payload(ad_level=True))
        assert "FROM campaign" in query
        return _response(_report_payload(ad_level=False))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(_CONFIG)
    client.inventory(
        _CONFIG,
        GoogleAdsInventoryQuery(account_ref=_CONFIG.account_ref),
        identity=identity,
    )
    client.run_daily_report(_CONFIG, _campaign_report(), identity=identity)
    client.run_daily_report(_CONFIG, _ad_report(), identity=identity)

    # Seven fixed reads: identity, campaign inventory, the campaign
    # ad-schedule criterion presence read, ad-group inventory, ad
    # inventory, and the two delivery reports.
    assert len(observed_queries) == 7
    assert any("campaign_criterion.type = 'AD_SCHEDULE'" in query for query in observed_queries)
    assert any("FROM customer" in query for query in observed_queries)
    assert any("campaign_budget.amount_micros" in query for query in observed_queries)
    assert any("ad_group.cpc_bid_micros" in query for query in observed_queries)
    assert any("ad_group_ad.ad.type" in query for query in observed_queries)
    assert any(
        "FROM campaign" in query and "metrics.cost_micros" in query for query in observed_queries
    )
    assert any(
        "FROM ad_group_ad" in query and "metrics.cost_micros" in query for query in observed_queries
    )


def test_documented_search_metadata_is_validated_discarded_and_not_retained() -> None:
    metadata: dict[str, object] = {
        "queryResourceConsumption": "7",
        "metricAttributes": [
            {
                "name": "metrics.fixture_metadata",
                "attributes": [{"key": "fixture-key", "value": "fixture-value"}],
            },
            {"name": "metrics.empty_attributes"},
            {
                "name": "metrics.default_attribute_value",
                "attributes": [{"key": "fixture-empty-value"}],
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        query = _assert_fixed_request(request)
        if "FROM customer" in query:
            return _response(_identity_payload(metadata=metadata))
        assert "metrics.cost_micros" in query
        return _response(_report_payload(ad_level=False, metadata=metadata))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(_CONFIG)
    report = client.run_daily_report(_CONFIG, _campaign_report(), identity=identity)

    rendered = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        "queryResourceConsumption",
        "metricAttributes",
        "fixture_metadata",
        "fixture-key",
        "fixture-value",
        "fixture-empty-value",
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "metadata",
    [
        {"queryResourceConsumption": 7},
        {"queryResourceConsumption": "00"},
        {"queryResourceConsumption": "9223372036854775808"},
        {"metricAttributes": "not-a-list"},
        {"metricAttributes": [{}]},
        {
            "metricAttributes": [
                {
                    "name": "metrics.fixture",
                    "attributes": [{"key": "k", "value": "v", "unexpected": "x"}],
                }
            ]
        },
        {"metricAttributes": [{"name": f"metrics.fixture_{index}"} for index in range(33)]},
        {
            "metricAttributes": [
                {
                    "name": "metrics.fixture",
                    "attributes": [{"key": "k", "value": "v"}] * 17,
                }
            ]
        },
    ],
)
def test_invalid_documented_search_metadata_fails_closed(metadata: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_fixed_request(request)
        return _response(_identity_payload(metadata=metadata))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity(_CONFIG)

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "fixture" not in raised.value.message


def test_unexpected_implicit_resource_name_fails_closed_under_the_fixed_query_contract() -> None:
    payload = _identity_payload()
    customer = payload["results"]
    assert type(customer) is list
    row = customer[0]
    assert type(row) is dict
    observed = row["customer"]
    assert type(observed) is dict
    observed["resourceName"] = "customers/1234567890"

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_fixed_request(request)
        return _response(payload)

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity(_CONFIG)

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"

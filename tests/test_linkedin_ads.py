from datetime import UTC, date, datetime
from decimal import localcontext

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.linkedin_ads import (
    LinkedInAdsClient,
    LinkedInAdsInventoryQuery,
    LinkedInAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _client(handler: httpx.MockTransport) -> LinkedInAdsClient:
    return LinkedInAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"api.linkedin.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _query(account_ref: str, *, level: str = "CAMPAIGN") -> LinkedInAdsReportQuery:
    return LinkedInAdsReportQuery(
        level=level,
        account_ref=account_ref,
        report_id="report:linkedin-fixture",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone="UTC",
        currency="USD",
        maximum_rows=3,
        page_size=3,
    )


def _analytics(pivot: str) -> dict[str, object]:
    urn_type = "sponsoredCampaign" if pivot == "CAMPAIGN" else "sponsoredCreative"
    return {
        "elements": [
            {
                "clicks": 2,
                "impressions": 10,
                "costInLocalCurrency": "1.25",
                "pivotValues": [f"urn:li:{urn_type}:456"],
                "dateRange": {
                    "start": {"year": 2026, "month": 9, "day": 1},
                    "end": {"year": 2026, "month": 9, "day": 1},
                },
            }
        ],
        "paging": {"start": 0, "count": 10, "links": []},
    }


def test_identity_and_fixed_campaign_and_native_ad_reports() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
        assert request.headers["linkedin-version"] == "202608"
        assert request.headers["x-restli-protocol-version"] == "2.0.0"
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "currency": "USD",
                    "status": "ACTIVE",
                    "notifiedOnNewFeaturesEnabled": True,
                },
            )
        assert request.url.path == "/rest/adAnalytics"
        assert request.url.params["q"] == "analytics"
        assert request.url.params["accounts"] == "List(urn:li:sponsoredAccount:123)"
        assert request.url.params["timeGranularity"] == "DAILY"
        assert request.url.params["dateRange"] == (
            "(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:2))"
        )
        assert request.url.params["fields"] == (
            "clicks,impressions,costInLocalCurrency,pivotValues,dateRange"
        )
        assert "start" not in request.url.params and "count" not in request.url.params
        assert request.url.query == (
            "q=analytics"
            f"&pivot={request.url.params['pivot']}"
            "&timeGranularity=DAILY"
            "&dateRange=(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:2))"
            "&accounts=List(urn%3Ali%3AsponsoredAccount%3A123)"
            "&fields=clicks,impressions,costInLocalCurrency,pivotValues,dateRange"
        ).encode("ascii")
        return httpx.Response(200, json=_analytics(request.url.params["pivot"]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123", expected_account_client_ref="123")
    campaign = client.run_campaign_report(_query(identity.account_ref), identity=identity)
    ad = client.run_ad_report(_query(identity.account_ref, level="AD"), identity=identity)
    assert identity.timezone is None
    assert campaign.envelope.source_timezone == "UTC"
    assert campaign.envelope.data_quality_statuses == ["NOT_AVAILABLE_FROM_PROVIDER"]
    assert campaign.performance_rows[0].metric_grain == "CAMPAIGN"
    assert (
        campaign.performance_rows[0].spend and campaign.performance_rows[0].spend.minor_units == 125
    )
    assert campaign.performance_rows[0].provider_extensions == {
        "linkedin_ads_cost_in_local_currency": "1.25",
        "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
    }
    assert ad.performance_rows[0].metric_grain == "AD"
    assert ad.performance_rows[0].resource_type == "LINKEDIN_SPONSORED_CREATIVE"
    assert ad.performance_rows[0].creative_version_id is None
    assert ad.performance_rows[0].campaign_ref is None
    assert set(ad.performance_rows[0].data_quality_statuses) == {
        "CREATIVE_NOT_EXPOSED",
        "COMBINATION_UNKNOWN",
    }
    assert len(requests) == 3


def test_bounded_hierarchy_inventory_uses_fixed_account_scoped_searches() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        assert request.url.params["sortOrder"] == "ASCENDING"
        expected_page_size = (
            "2" if request.url.path.endswith(("adCampaignGroups", "adCampaigns")) else "2"
        )
        assert request.url.params["pageSize"] == expected_page_size
        if request.url.path.endswith("/adCampaignGroups"):
            assert request.url.params["q"] == "search"
            assert "ACTIVE" in request.url.params["search"]
            assert request.url.params["fields"] == "id,account,status"
            assert request.url.query == (
                "q=search"
                "&search=(status:(values:List(ACTIVE,ARCHIVED,CANCELED,DRAFT,PAUSED,"
                "PENDING_DELETION,REMOVED)))"
                "&sortOrder=ASCENDING&fields=id,account,status&pageSize=2"
            ).encode("ascii")
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "account": "urn:li:sponsoredAccount:123",
                            "id": 11,
                            "status": "ACTIVE",
                        }
                    ],
                    "metadata": {},
                    "paging": {"start": 0, "count": 1, "links": []},
                },
            )
        if request.url.path.endswith("/adCampaigns"):
            assert request.url.params["q"] == "search"
            assert "COMPLETED" in request.url.params["search"]
            assert request.url.params["fields"] == "id,account,campaignGroup,status,objectiveType"
            assert request.url.query == (
                "q=search"
                "&search=(status:(values:List(ACTIVE,PAUSED,ARCHIVED,COMPLETED,CANCELED,"
                "DRAFT,PENDING_DELETION,REMOVED)))"
                "&sortOrder=ASCENDING&fields=id,account,campaignGroup,status,objectiveType"
                "&pageSize=2"
            ).encode("ascii")
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "account": "urn:li:sponsoredAccount:123",
                            "campaignGroup": "urn:li:sponsoredCampaignGroup:11",
                            "id": 22,
                            "status": "PAUSED",
                            "objectiveType": "WEBSITE_VISITS",
                        }
                    ],
                    "metadata": {},
                    "paging": {"start": 0, "count": 1, "links": []},
                },
            )
        assert request.url.path.endswith("/creatives")
        assert request.url.params["q"] == "criteria"
        assert request.headers["x-restli-method"] == "FINDER"
        assert request.url.params["pageSize"] == "2"
        assert request.url.params["campaigns"] == "List(urn:li:sponsoredCampaign:22)"
        assert request.url.params["fields"] == "id,account,campaign,intendedStatus"
        assert request.url.query == (
            "q=criteria&campaigns=List(urn%3Ali%3AsponsoredCampaign%3A22)"
            "&sortOrder=ASCENDING&fields=id,account,campaign,intendedStatus&pageSize=2"
        ).encode("ascii")
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
                "paging": {"start": 0, "count": 1, "links": []},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    query = LinkedInAdsInventoryQuery(
        account_ref=identity.account_ref, maximum_records=2, page_size=2
    )
    groups = client.list_campaign_groups(query, identity=identity)
    campaigns = client.list_campaigns(query, identity=identity)
    creatives = client.list_creatives(query, identity=identity)
    assert groups[0].campaign_group_ref == "linkedin_ads.campaign_group.11"
    assert campaigns[0].campaign_group_ref == groups[0].campaign_group_ref
    assert creatives[0].campaign_ref == campaigns[0].campaign_ref
    assert len(requests) == 5


def test_inventory_rejects_unprojected_campaign_group_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        assert request.url.params["fields"] == "id,account,status"
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "account": "urn:li:sponsoredAccount:123",
                        "buyingType": "AUCTION",
                        "id": 11,
                        "status": "ACTIVE",
                    }
                ],
                "metadata": {},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_inventory_rejects_nonempty_legacy_paging_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"account": "urn:li:sponsoredAccount:123", "id": 11, "status": "ACTIVE"}
                ],
                "metadata": {},
                "paging": {"start": 0, "count": 1, "links": ["untrusted-next"]},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_inventory_rejects_repeated_page_token() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD"})
        if calls == 3:
            assert request.url.query.endswith(b"pageToken=repeated%2Btoken%2F%28%29")
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"account": "urn:li:sponsoredAccount:123", "id": 11, "status": "ACTIVE"}
                ],
                "metadata": {"nextPageToken": "repeated+token/()"},
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError, match="pagination"):
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=3),
            identity=identity,
        )
    assert calls == 3


@pytest.mark.parametrize(
    ("native_cost", "expected_minor"),
    [("0.005", 0), ("0.010001", 1), ("0.015", 2)],
)
def test_fractional_linkedin_cost_retains_exact_decimal(
    native_cost: str, expected_minor: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": "123", "currency": "USD"})
        response = _analytics("CAMPAIGN")
        response["elements"][0]["costInLocalCurrency"] = native_cost  # type: ignore[index]
        return httpx.Response(200, json=response)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with localcontext() as ambient_context:
        ambient_context.prec = 2
        row = client.run_campaign_report(
            _query(identity.account_ref), identity=identity
        ).performance_rows[0]
    assert row.spend and row.spend.minor_units == expected_minor
    assert row.provider_extensions == {
        "linkedin_ads_cost_in_local_currency": native_cost,
        "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
    }


def test_json_float_money_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": "123", "currency": "USD"})
        response = _analytics("CAMPAIGN")
        response["elements"][0]["costInLocalCurrency"] = 1.25  # type: ignore[index]
        return httpx.Response(200, json=response)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError, match="analytics cost"):
        client.run_campaign_report(_query(identity.account_ref), identity=identity)


def test_nonzero_analytics_offset_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": "123", "currency": "USD"})
        response = _analytics("CAMPAIGN")
        response["paging"]["start"] = 1  # type: ignore[index]
        return httpx.Response(200, json=response)

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    with pytest.raises(AdhelmError, match="paging"):
        client.run_campaign_report(_query(identity.account_ref), identity=identity)


def test_invalid_window_is_rejected_before_egress() -> None:
    with pytest.raises(ValidationError, match="at most 367 days"):
        LinkedInAdsReportQuery(
            account_ref="linkedin_ads.account.123",
            report_id="report:too-long",
            query_contract_hash="a" * 64,
            window_start=date(2025, 1, 1),
            window_end=date(2026, 1, 3),
        )


def test_future_window_fails_after_identity_without_report_egress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": 123, "currency": "USD"})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123")
    future = _query(identity.account_ref).model_copy(
        update={"window_start": date(2026, 9, 5), "window_end": date(2026, 9, 5)}
    )
    with pytest.raises(AdhelmError, match="future"):
        client.run_campaign_report(future, identity=identity)
    assert calls == 1

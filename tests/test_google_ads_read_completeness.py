# ruff: noqa: E501
"""Fixture-only tests for the additive Google Ads read completeness surface.

Nothing here performs a live Google Ads request.  Every case drives the fixed
v25 adapter through `httpx.MockTransport`, so the pagination, quota, creative
redaction and disclosure behaviour proved here is fixture behaviour and is not
a live conformance claim.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.google_ads import (
    GOOGLE_ADS_API_VERSION,
    GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVELS,
    GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE,
    GOOGLE_ADS_READ_SURFACES,
    GoogleAdsClient,
    GoogleAdsInventoryQuery,
    GoogleAdsReadConfig,
    GoogleAdsReportQuery,
    classify_google_ads_transport_error,
)
from adhelm.transport import SafeHttpClient

CONFIG = GoogleAdsReadConfig(
    account_ref="account.google.1",
    customer_client_ref="1234567890",
    login_customer_client_ref="0987654321",
)
ACCESS_TOKEN = "access-fixture-token"
# Deliberately hostile advertising copy: a legitimate Google Ads headline may
# contain a phone number or an address, and the fixed read must neither retain
# it nor fail on it.
HOSTILE_HEADLINE = {"text": "Call 555-123-4567 for a free quote"}
HOSTILE_DESCRIPTION = {"text": "Email sales@example.com about our account team"}
HOSTILE_FINAL_URL = "https://example.com/account/checkout?utm_source=google"


def _response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _client(
    transport: httpx.BaseTransport, *, read_attempts: int = 1, now: datetime | None = None
) -> GoogleAdsClient:
    return GoogleAdsClient(
        SafeHttpClient(
            httpx.Client(transport=transport),
            {"googleads.googleapis.com"},
            read_attempts=read_attempts,
            sleep=lambda _: None,
        ),
        lambda: ACCESS_TOKEN,
        now=lambda: now or datetime(2026, 9, 4, 12, tzinfo=UTC),
    )


def _identity(*, test_account: bool | None = None, total: str | None = "1") -> dict[str, object]:
    customer: dict[str, object] = {
        "id": "1234567890",
        "currencyCode": "USD",
        "timeZone": "UTC",
    }
    if test_account is not None:
        customer["testAccount"] = test_account
    payload: dict[str, object] = {"results": [{"customer": customer}]}
    if total is not None:
        payload["totalResultsCount"] = total
    return payload


def _campaign_row(*, budget: bool = True) -> dict[str, object]:
    row: dict[str, object] = {
        "campaign": {
            "id": "11",
            "status": "ENABLED",
            "advertisingChannelType": "SEARCH",
            "startDateTime": "2026-01-01 00:00:00",
            "endDateTime": "",
            "biddingStrategyType": "MANUAL_CPC",
            "networkSettings": {
                "targetGoogleSearch": True,
                "targetSearchNetwork": True,
                "targetContentNetwork": True,
                "targetPartnerSearchNetwork": False,
            },
            "geoTargetTypeSetting": {
                "positiveGeoTargetType": "PRESENCE_OR_INTEREST",
                "negativeGeoTargetType": "PRESENCE",
            },
        }
    }
    if budget:
        row["campaignBudget"] = {
            "amountMicros": "5000000",
            "period": "DAILY",
            "deliveryMethod": "STANDARD",
            "explicitlyShared": True,
        }
    return row


def _ad_row() -> dict[str, object]:
    return {
        "campaign": {"id": "11"},
        "adGroup": {"id": "21"},
        "adGroupAd": {
            "status": "ENABLED",
            "policySummary": {"approvalStatus": "APPROVED_LIMITED", "reviewStatus": "REVIEWED"},
            "ad": {
                "id": "31",
                "type": "RESPONSIVE_SEARCH_AD",
                "finalUrls": [HOSTILE_FINAL_URL],
                "responsiveSearchAd": {
                    "headlines": [HOSTILE_HEADLINE, HOSTILE_HEADLINE, HOSTILE_HEADLINE],
                    "descriptions": [HOSTILE_DESCRIPTION],
                },
            },
        },
    }


def _inventory_handler(
    *,
    totals: bool = True,
    test_account: bool | None = False,
    scheduled: bool = True,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        assert json.loads(request.content)["searchSettings"] == {"returnTotalResultsCount": True}

        def page(rows: list[dict[str, object]]) -> httpx.Response:
            payload: dict[str, object] = {"results": rows}
            if totals:
                payload["totalResultsCount"] = str(len(rows))
            return _response(payload)

        if "FROM customer" in query:
            return _response(_identity(test_account=test_account, total="1" if totals else None))
        if "campaign_budget.amount_micros" in query:
            return page([_campaign_row()])
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return page(
                [
                    {
                        "campaign": {"id": "11"},
                        "campaignCriterion": {"criterionId": "500", "type": "AD_SCHEDULE"},
                    }
                ]
                if scheduled
                else []
            )
        if "ad_group.cpc_bid_micros" in query:
            return page(
                [
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
            )
        assert "ad_group_ad.ad.responsive_search_ad.headlines" in query
        return page([_ad_row()])

    return httpx.MockTransport(handler)


# --- the single version constant ---------------------------------------------


def test_one_constant_names_the_api_version_for_every_fixed_read() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _response(_identity())

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    assert paths == [f"/{GOOGLE_ADS_API_VERSION}/customers/1234567890/googleAds:search"]
    assert identity.provider_api_version == GOOGLE_ADS_API_VERSION


# --- pagination completeness --------------------------------------------------


def test_a_declared_total_result_count_proves_the_read_and_marks_the_report_complete() -> None:
    client = _client(_inventory_handler())
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    assert inventory.pagination_completeness == "PROVEN_COMPLETE"

    def report_handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "FROM customer" in query:
            return _response(_identity())
        return _response(
            {
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "segments": {"date": "2026-09-01"},
                        "metrics": {"impressions": "5", "clicks": "2", "costMicros": "5001"},
                    }
                ],
                "totalResultsCount": "1",
            }
        )

    report_client = _client(httpx.MockTransport(report_handler))
    report_identity = report_client.discover_identity(CONFIG)
    report = report_client.run_daily_report(
        CONFIG,
        GoogleAdsReportQuery(
            account_ref="account.google.1",
            report_id="report.google.campaign",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 1),
            source_timezone="UTC",
        ),
        identity=report_identity,
    )
    assert report.envelope.data_quality_statuses == ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"]
    # Micros are preserved exactly beside the rounded display amount.
    row = report.performance_rows[0]
    assert row.provider_extensions["google_ads_cost_micros"] == "5001"
    assert row.spend is not None and row.spend.minor_units == 1


def test_an_undeclared_total_result_count_is_reported_as_incomplete_not_assumed() -> None:
    client = _client(_inventory_handler(totals=False))
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    assert inventory.pagination_completeness == "INCOMPLETE_TOTAL_NOT_DECLARED"

    def report_handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "FROM customer" in query:
            return _response(_identity(total=None))
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

    report_client = _client(httpx.MockTransport(report_handler))
    report_identity = report_client.discover_identity(CONFIG)
    report = report_client.run_daily_report(
        CONFIG,
        GoogleAdsReportQuery(
            account_ref="account.google.1",
            report_id="report.google.campaign",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 1),
            source_timezone="UTC",
        ),
        identity=report_identity,
    )
    assert report.envelope.data_quality_statuses == ["NOT_AVAILABLE_FROM_PROVIDER"]


def test_a_page_token_walk_continues_and_a_changed_total_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = body["query"]
        if "FROM customer" in query:
            return _response(_identity())
        if body.get("pageToken") is None:
            return _response(
                {
                    "results": [_campaign_row()],
                    "nextPageToken": "page-2",
                    "totalResultsCount": "2",
                }
            )
        return _response({"results": [_campaign_row()], "totalResultsCount": "3"})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "REPORT_CHANGED_DURING_PAGINATION"


# --- quota classification -----------------------------------------------------


def test_a_quota_refusal_is_a_precondition_failure_with_a_bounded_retry_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"}, json={"error": {}})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity(CONFIG)
    error = raised.value
    assert error.code == "GOOGLE_ADS_QUOTA_EXHAUSTED"
    assert error.exit_code == int(ExitCode.PRECONDITION) == 3
    assert error.retry_after_seconds == 30
    assert "30 second(s)" in (error.next_action or "")
    assert "RESOURCE_EXHAUSTED" in (error.next_action or "")
    # The access level the allowance depends on is disclosed as unknown.
    assert "AdHelm cannot observe" in (error.next_action or "")
    rendered = json.dumps({"message": error.message, "next_action": error.next_action})
    assert ACCESS_TOKEN not in rendered


def test_a_quota_refusal_without_a_retry_after_header_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {}})

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity(CONFIG)
    assert "no whole-second Retry-After hint" in (raised.value.next_action or "")


@pytest.mark.parametrize("status", [401, 403])
def test_an_access_refusal_is_an_authorization_failure_naming_the_status_command(
    status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {}})

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity(CONFIG)
    error = raised.value
    assert error.code == "GOOGLE_ADS_ACCESS_NOT_PERMITTED"
    assert error.exit_code == int(ExitCode.AUTHORIZATION) == 4
    assert "auth google-oauth status" in (error.next_action or "")


def test_an_unclassified_refusal_is_returned_unchanged() -> None:
    original = AdhelmError("PROVIDER_REQUEST_FAILED", "Provider rejected the fixed operation.")
    assert classify_google_ads_transport_error(original) is original


# --- creative redaction -------------------------------------------------------


def test_ad_copy_and_destination_values_become_counts_and_never_reach_a_model() -> None:
    client = _client(_inventory_handler())
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    ad = inventory.ads[0]
    assert ad.responsive_search_ad_returned is True
    assert ad.headline_count == 3
    assert ad.description_count == 1
    assert ad.final_url_count == 1
    assert ad.policy_approval_status == "APPROVED_LIMITED"
    assert ad.policy_review_status == "REVIEWED"
    assert ad.creative_capture_status == "CREATIVE_NOT_EXPOSED"
    rendered = json.dumps(inventory.model_dump(mode="json"))
    for secret in (
        HOSTILE_HEADLINE["text"],
        HOSTILE_DESCRIPTION["text"],
        HOSTILE_FINAL_URL,
        "555-123-4567",
        "sales@example.com",
    ):
        assert secret not in rendered


def test_an_ad_without_a_responsive_creative_reports_no_component_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "FROM customer" in query:
            return _response(_identity())
        if "campaign_budget.amount_micros" in query:
            return _response({"results": [_campaign_row()], "totalResultsCount": "1"})
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return _response({"results": [], "totalResultsCount": "0"})
        if "ad_group.cpc_bid_micros" in query:
            return _response({"results": [], "totalResultsCount": "0"})
        return _response(
            {
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "adGroup": {"id": "21"},
                        "adGroupAd": {
                            "status": "PAUSED",
                            "ad": {"id": "31", "type": "EXPANDED_TEXT_AD"},
                        },
                    }
                ],
                "totalResultsCount": "1",
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    ad = inventory.ads[0]
    assert ad.responsive_search_ad_returned is False
    assert ad.headline_count is None and ad.description_count is None
    assert ad.final_url_count == 0
    assert ad.policy_approval_status is None


def test_a_component_collection_beyond_the_local_bound_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "FROM customer" in query:
            return _response(_identity())
        if "campaign_budget.amount_micros" in query:
            return _response({"results": [_campaign_row()], "totalResultsCount": "1"})
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return _response({"results": [], "totalResultsCount": "0"})
        if "ad_group.cpc_bid_micros" in query:
            return _response({"results": [], "totalResultsCount": "0"})
        row = _ad_row()
        row["adGroupAd"]["ad"]["responsiveSearchAd"]["headlines"] = [HOSTILE_HEADLINE] * 100  # type: ignore[index]
        return _response({"results": [row], "totalResultsCount": "1"})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert HOSTILE_HEADLINE["text"] not in str(raised.value.message)


# --- campaign settings and disclosures ---------------------------------------


def test_campaign_settings_are_read_as_presence_and_enum_facts() -> None:
    client = _client(_inventory_handler())
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    campaign = inventory.campaigns[0]
    assert campaign.budget is not None and campaign.budget.minor_units == 500
    assert campaign.budget_period == "DAILY"
    assert campaign.budget_delivery_method == "STANDARD"
    assert campaign.budget_explicitly_shared is True
    assert campaign.target_content_network is True
    assert campaign.target_partner_search_network is False
    assert campaign.positive_geo_target_type == "PRESENCE_OR_INTEREST"
    assert campaign.negative_geo_target_type == "PRESENCE"
    assert campaign.ad_schedule_present is True


def test_a_campaign_with_no_ad_schedule_criterion_records_that_absence() -> None:
    client = _client(_inventory_handler(scheduled=False))
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    assert inventory.campaigns[0].ad_schedule_present is False


def test_an_ad_schedule_for_a_campaign_the_same_read_never_returned_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "FROM customer" in query:
            return _response(_identity())
        if "campaign_budget.amount_micros" in query:
            return _response({"results": [_campaign_row()], "totalResultsCount": "1"})
        assert "campaign_criterion.type = 'AD_SCHEDULE'" in query
        return _response(
            {
                "results": [
                    {
                        "campaign": {"id": "404"},
                        "campaignCriterion": {"criterionId": "500", "type": "AD_SCHEDULE"},
                    }
                ],
                "totalResultsCount": "1",
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_the_cloud_project_access_level_is_disclosed_as_not_observable() -> None:
    client = _client(_inventory_handler(test_account=True))
    identity = client.discover_identity(CONFIG)
    assert identity.cloud_project_access_level == GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE
    assert identity.cloud_project_access_level not in GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVELS
    assert identity.test_account is True
    assert GOOGLE_ADS_READ_SURFACES["CLOUD_PROJECT_ACCESS_LEVEL"] == "NOT_IN_REVIEWED_FIXED_READ"


def test_a_customer_that_returns_no_test_account_flag_is_not_assumed_production() -> None:
    client = _client(_inventory_handler(test_account=None))
    assert client.discover_identity(CONFIG).test_account is None

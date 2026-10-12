"""TikTok read completeness, rate-limit classification, and retained state shape.

Every case is offline: `httpx.MockTransport` through the ordinary
`SafeHttpClient` boundary, and a local ledger.  No live TikTok request is made.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.inventory import _tiktok_snapshots
from adhelm.providers.tiktok_ads import (
    TIKTOK_ADS_API_HOST,
    TIKTOK_ADS_API_VERSION,
    TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
    TIKTOK_ADS_RATE_LIMIT_BUSINESS_CODES,
    TIKTOK_ADS_READ_SURFACES,
    TIKTOK_ADS_REVIEWED_OPERATION_STATUSES,
    TIKTOK_ADS_STATE_SHAPE_PREFIX,
    TIKTOK_ADS_STATE_SHAPE_STATUS,
    TIKTOK_ADS_STATE_SHAPE_VERSION,
    TikTokAdsAd,
    TikTokAdsAdGroup,
    TikTokAdsCampaign,
    TikTokAdsClient,
    TikTokAdsIdentity,
    TikTokAdsInventoryQuery,
    classify_tiktok_ads_business_code,
    classify_tiktok_ads_transport_error,
    tiktok_ads_declares_serving_intent,
)
from adhelm.transport import SafeHttpClient

ADVERTISER = "123"


def _client(handler: httpx.MockTransport) -> TikTokAdsClient:
    return TikTokAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {TIKTOK_ADS_API_HOST}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "message": "OK", "request_id": "request-1", "data": data}


def _identity_body() -> dict[str, Any]:
    return _envelope(
        {
            "list": [
                {
                    "advertiser_id": ADVERTISER,
                    "currency": "USD",
                    "timezone": "UTC",
                    "status": "STATUS_ENABLE",
                }
            ]
        }
    )


def _page(items: list[dict[str, Any]], *, page: int, page_size: int, total: int) -> dict[str, Any]:
    total_pages = (total + page_size - 1) // page_size if total else 1
    return _envelope(
        {
            "list": items,
            "page_info": {
                "page": page,
                "page_size": page_size,
                "total_number": total,
                "total_page": total_pages,
            },
        }
    )


def _identity() -> TikTokAdsIdentity:
    return TikTokAdsIdentity(
        account_ref=f"tiktok_ads.advertiser.{ADVERTISER}",
        advertiser_client_ref=ADVERTISER,
        timezone="UTC",
        currency="USD",
        status="STATUS_ENABLE",
    )


# ------------------------------------------------------- one API version pin


def test_the_api_version_and_host_are_each_one_constant() -> None:
    assert TIKTOK_ADS_API_VERSION == "v1.3"
    assert TIKTOK_ADS_API_HOST == "business-api.tiktok.com"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_identity_body())

    _client(httpx.MockTransport(handler)).discover_identity(ADVERTISER)

    assert requests[0].url.host == TIKTOK_ADS_API_HOST
    assert requests[0].url.path.startswith(f"/open_api/{TIKTOK_ADS_API_VERSION}/")


def test_no_second_host_or_version_literal_exists_in_the_adapter() -> None:
    from pathlib import Path

    import adhelm.providers.tiktok_ads as module

    source = Path(module.__file__).read_text()
    # Exactly one host literal; every other use reads the constant.
    assert source.count('"business-api.tiktok.com"') == 1
    # The version appears once as a value; the remaining occurrences are the
    # `Literal["v1.3"]` type annotations that pin it, which is the point.
    assert source.count('= "v1.3"') == 1
    assert source.count('Literal["v1.3"]') == 3


# ------------------------------------------------------------- pagination


def test_a_complete_page_walk_consumes_every_numbered_page() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_identity_body())
        page = int(request.url.params["page"])
        calls.append(page)
        items = [{"campaign_id": str(100 + page), "operation_status": "ENABLE"}]
        return httpx.Response(200, json=_page(items, page=page, page_size=1, total=2))

    client = _client(httpx.MockTransport(handler))
    campaigns = client.list_campaigns(
        TikTokAdsInventoryQuery(
            account_ref=_identity().account_ref, maximum_records=10, page_size=1
        ),
        identity=_identity(),
    )

    assert calls == [1, 2]
    assert len(campaigns) == 2


def test_a_page_without_a_declared_total_fails_closed_rather_than_returning_a_partial_read() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_identity_body())
        body = _page([], page=1, page_size=500, total=0)
        # A read that cannot prove its own total is refused; there is no path
        # that returns rows without the proof, which is why a retained snapshot
        # records `TIKTOK_PAGINATION_TOTAL_DECLARED`.
        del body["data"]["page_info"]["total_number"]
        return httpx.Response(200, json=body)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as error:
        client.list_campaigns(
            TikTokAdsInventoryQuery(account_ref=_identity().account_ref),
            identity=_identity(),
        )

    assert error.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_declared_total_beyond_the_configured_bound_is_reported_as_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_identity_body())
        return httpx.Response(
            200,
            json=_page(
                [{"campaign_id": "101", "operation_status": "ENABLE"}],
                page=1,
                page_size=1,
                total=5,
            ),
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as error:
        client.list_campaigns(
            TikTokAdsInventoryQuery(
                account_ref=_identity().account_ref, maximum_records=2, page_size=1
            ),
            identity=_identity(),
        )

    assert error.value.code == "RESULT_LIMIT_REQUIRED"


def test_a_shifting_declared_total_between_pages_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_identity_body())
        page = int(request.url.params["page"])
        total = 2 if page == 1 else 3
        return httpx.Response(
            200,
            json=_page(
                [{"campaign_id": str(100 + page), "operation_status": "ENABLE"}],
                page=page,
                page_size=1,
                total=total,
            ),
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as error:
        client.list_campaigns(
            TikTokAdsInventoryQuery(
                account_ref=_identity().account_ref, maximum_records=10, page_size=1
            ),
            identity=_identity(),
        )

    assert error.value.code == "PROVIDER_RESPONSE_INVALID"


# ----------------------------------------------- rate-limit classification


def test_a_rate_limit_status_reaches_the_caller_through_the_client() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"}, json={"code": 40100})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ADVERTISER)

    assert error.value.code == "TIKTOK_ADS_RATE_LIMITED"
    assert error.value.exit_code == 3
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 30
    assert "1 request/second" in error.value.next_action


@pytest.mark.parametrize("status", [401, 403])
def test_a_denial_reaches_the_caller_with_the_documented_exit_code(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": 40001, "message": "leaked-secret-value"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ADVERTISER)

    assert error.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert error.value.exit_code == 4
    assert "leaked-secret-value" not in error.value.message + error.value.next_action
    assert "tiktok-token status" in error.value.next_action


def test_a_business_error_code_never_reaches_a_diagnostic() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 40002,
                "message": "advertiser 987654321 is not authorized for app leaked-app-id",
                "request_id": "request-1",
                "data": {},
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ADVERTISER)

    assert error.value.code == "PROVIDER_REQUEST_FAILED"
    text = error.value.message + error.value.next_action
    assert "leaked-app-id" not in text
    assert "987654321" not in text


def test_no_business_code_is_relabelled_into_a_rate_limit_it_was_not_shown_to_be() -> None:
    # TikTok publishes no rate-limit code vocabulary this build could fetch, so
    # the map is deliberately empty and classification uses the HTTP status.
    assert TIKTOK_ADS_RATE_LIMIT_BUSINESS_CODES == {}
    assert classify_tiktok_ads_business_code(40100).code == "PROVIDER_REQUEST_FAILED"
    assert classify_tiktok_ads_business_code(True).code == "PROVIDER_RESPONSE_INVALID"
    assert classify_tiktok_ads_business_code("40100").code == "PROVIDER_RESPONSE_INVALID"


def test_an_unclassified_transport_status_is_returned_unchanged() -> None:
    original = AdhelmError("PROVIDER_REQUEST_FAILED", "x", status_code=500)
    assert classify_tiktok_ads_transport_error(original) is original


# ------------------------------------------------------------ read surfaces


def test_every_dimension_surface_named_by_the_rule_table_exists_in_the_read_map() -> None:
    from adhelm.audits.tiktok_ads import _DIMENSION_SURFACES

    for surfaces in _DIMENSION_SURFACES.values():
        for surface in surfaces:
            assert surface in TIKTOK_ADS_READ_SURFACES, surface


def test_the_unproven_surfaces_are_declared_unread_rather_than_requested() -> None:
    # TikTok's reference portal renders through JavaScript and the
    # TikTok-maintained SDK documents `fields` as a bare `list[str]`, so no
    # returnable field name for these is proven and none is requested.
    for surface in (
        "AD_GROUP_SCHEDULE",
        "AD_GROUP_TARGETING_PRESENCE",
        "AD_GROUP_PLACEMENT",
        "AD_CREATIVE_ASSOCIATION",
        "AD_CREATIVE_VIDEO_PRESENCE",
        "AD_DESTINATION_URL_VALUE",
        "SECONDARY_STATUS",
        "AD_REVIEW_STATE",
    ):
        assert TIKTOK_ADS_READ_SURFACES[surface] == "NOT_IN_REVIEWED_FIXED_READ", surface
    for surface in (
        "OBJECTIVE_ENUMERATION",
        "BID_TYPE_ENUMERATION",
        "ACCOUNT_STATUS_ENUMERATION",
        "ACCOUNT_PERMISSION_SCOPE",
        "APP_APPROVAL_STATE",
        "RATE_LIMIT_BUSINESS_CODES",
    ):
        assert TIKTOK_ADS_READ_SURFACES[surface] == "NOT_DOCUMENTED", surface


def test_the_fixed_read_requests_only_the_reviewed_projection() -> None:
    import json

    requested: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_identity_body())
        requested.append(json.loads(request.url.params["fields"]))
        return httpx.Response(200, json=_page([], page=1, page_size=500, total=0))

    client = _client(httpx.MockTransport(handler))
    query = TikTokAdsInventoryQuery(account_ref=_identity().account_ref)
    client.list_campaigns(query, identity=_identity())
    client.list_adgroups(query, identity=_identity())
    client.list_ads(query, identity=_identity())

    assert requested == [
        ["campaign_id", "operation_status", "objective_type", "budget_mode", "budget"],
        [
            "adgroup_id",
            "campaign_id",
            "operation_status",
            "budget_mode",
            "budget",
            "bid_type",
            "bid_price",
        ],
        ["ad_id", "adgroup_id", "campaign_id", "operation_status"],
    ]
    # No unproven field name is ever requested.
    for fields in requested:
        for forbidden in (
            "schedule_type",
            "schedule_start_time",
            "location_ids",
            "age_groups",
            "placement_type",
            "secondary_status",
            "video_id",
            "ad_text",
            "landing_page_url",
            "creative_material_mode",
        ):
            assert forbidden not in fields


# ---------------------------------------------------------- retained state


def _snapshots() -> list[dict[str, Any]]:
    return _tiktok_snapshots(
        _identity(),
        [
            TikTokAdsCampaign(
                campaign_ref="tiktok_ads.campaign.11",
                campaign_client_ref="11",
                status="ENABLE",
                objective="TRAFFIC",
                budget_type="BUDGET_MODE_DAY",
                budget=None,
            )
        ],
        [
            TikTokAdsAdGroup(
                adgroup_ref="tiktok_ads.adgroup.12",
                adgroup_client_ref="12",
                campaign_ref="tiktok_ads.campaign.11",
                status="ENABLE",
                budget_type="BUDGET_MODE_INFINITE",
                budget=None,
                bid_type="BID_TYPE_NO_BID",
                bid_amount=None,
            )
        ],
        [
            TikTokAdsAd(
                ad_ref="tiktok_ads.ad.13",
                ad_client_ref="13",
                campaign_ref="tiktok_ads.campaign.11",
                adgroup_ref="tiktok_ads.adgroup.12",
                status="ENABLE",
            )
        ],
    )


def test_every_tiktok_snapshot_records_the_connector_state_shape_version() -> None:
    for snapshot in _snapshots():
        versions = [
            status.removeprefix(TIKTOK_ADS_STATE_SHAPE_PREFIX)
            for status in snapshot["data_quality_statuses"]
            if status.startswith(TIKTOK_ADS_STATE_SHAPE_PREFIX)
        ]
        assert versions == [TIKTOK_ADS_STATE_SHAPE_VERSION]
        # The version never enters normalized_state, so recording it creates no
        # drift of its own.
        assert TIKTOK_ADS_STATE_SHAPE_STATUS not in snapshot["normalized_state"].values()
        assert not any(key.startswith("state_shape") for key in snapshot["normalized_state"])


def test_the_account_snapshot_records_the_proven_declared_total() -> None:
    account = next(item for item in _snapshots() if item["resource_type"] == "ACCOUNT")
    assert TIKTOK_ADS_PAGINATION_TOTAL_DECLARED in account["data_quality_statuses"]


def test_presence_and_enum_facts_are_stated_in_both_directions() -> None:
    snapshots = _snapshots()
    campaign = next(item for item in snapshots if item["resource_type"] == "CAMPAIGN")
    ad_group = next(item for item in snapshots if item["resource_type"] == "AD_GROUP")
    ad = next(item for item in snapshots if item["resource_type"] == "AD")

    assert "TIKTOK_OBJECTIVE_RETURNED" in campaign["data_quality_statuses"]
    assert "TIKTOK_BUDGET_MODE_DAY" in campaign["data_quality_statuses"]
    assert "TIKTOK_BUDGET_AMOUNT_ABSENT" in campaign["data_quality_statuses"]
    assert "TIKTOK_OPERATION_STATUS_REVIEWED" in campaign["data_quality_statuses"]
    assert "TIKTOK_BUDGET_MODE_INFINITE" in ad_group["data_quality_statuses"]
    assert "TIKTOK_BID_TYPE_RETURNED" in ad_group["data_quality_statuses"]
    assert "TIKTOK_BID_PRICE_ABSENT" in ad_group["data_quality_statuses"]
    assert "TIKTOK_OPERATION_STATUS_REVIEWED" in ad["data_quality_statuses"]
    # The pre-existing budget statuses are unchanged.
    assert "BUDGET_MODE_INFINITE" in ad_group["data_quality_statuses"]


def test_no_targeting_creative_or_destination_value_reaches_a_retained_snapshot() -> None:
    for snapshot in _snapshots():
        for key in snapshot["normalized_state"]:
            assert key not in {
                "targeting",
                "location_ids",
                "age_groups",
                "gender",
                "placement_type",
                "video_id",
                "identity_id",
                "ad_text",
                "call_to_action",
                "landing_page_url",
            }


def test_an_unreviewed_operation_status_is_recorded_as_unreviewed() -> None:
    campaign = TikTokAdsCampaign(
        campaign_ref="tiktok_ads.campaign.11",
        campaign_client_ref="11",
        status="SOMETHING_ELSE",
        objective=None,
        budget_type="BUDGET_MODE_DAY",
        budget=None,
    )
    snapshots = _tiktok_snapshots(_identity(), [campaign], [], [])
    retained = next(item for item in snapshots if item["resource_type"] == "CAMPAIGN")

    assert "TIKTOK_OPERATION_STATUS_NOT_IN_REVIEWED_ENUM" in retained["data_quality_statuses"]
    assert "TIKTOK_OBJECTIVE_NOT_RETURNED" in retained["data_quality_statuses"]


def test_serving_intent_is_the_one_reviewed_status_that_declares_it() -> None:
    assert TIKTOK_ADS_REVIEWED_OPERATION_STATUSES == ("ENABLE", "DISABLE")
    assert tiktok_ads_declares_serving_intent("ENABLE") is True
    assert tiktok_ads_declares_serving_intent("enable") is True
    assert tiktok_ads_declares_serving_intent("DISABLE") is False
    assert tiktok_ads_declares_serving_intent(None) is False


def test_the_reviewed_budget_modes_are_the_three_the_read_accepts() -> None:
    from adhelm.inventory import _tiktok_budget_state

    expected_budget_type = {"BUDGET_MODE_DAY": "DAILY", "BUDGET_MODE_TOTAL": "LIFETIME"}
    for mode in ("BUDGET_MODE_DAY", "BUDGET_MODE_INFINITE", "BUDGET_MODE_TOTAL"):
        state, statuses = _tiktok_budget_state(mode, None)
        assert state["tiktok_ads_budget_mode"] == mode
        if mode == "BUDGET_MODE_INFINITE":
            assert statuses == ["BUDGET_MODE_INFINITE"]
            assert "budget_type" not in state
        else:
            assert state["budget_type"] == expected_budget_type[mode]
    with pytest.raises(AdhelmError) as error:
        _tiktok_budget_state("BUDGET_MODE_INVENTED", None)
    assert error.value.code == "UNSUPPORTED_BUDGET_MODE"


def test_the_report_envelope_records_freshness_and_attribution_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(200, json=_identity_body())
        return httpx.Response(
            200,
            json=_page(
                [
                    {
                        "dimensions": {"stat_time_day": "2026-09-01", "campaign_id": "11"},
                        "metrics": {"impressions": "10", "clicks": "1", "spend": "1.50"},
                    }
                ],
                page=1,
                page_size=500,
                total=1,
            ),
        )

    from adhelm.providers.tiktok_ads import TikTokAdsReportQuery

    client = _client(httpx.MockTransport(handler))
    report = client.run_campaign_report(
        TikTokAdsReportQuery(
            account_ref=_identity().account_ref,
            report_id="report:fixture",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 2),
            source_timezone="UTC",
        ),
        identity=_identity(),
    )

    assert report.envelope.data_fresh_through is None
    assert report.envelope.attribution_model == "NOT_AVAILABLE_FROM_PROVIDER"
    assert report.envelope.attribution_window is None
    assert report.envelope.provider_api_version == TIKTOK_ADS_API_VERSION

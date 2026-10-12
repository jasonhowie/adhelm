# ruff: noqa: E501
"""Fixture-only OpenAI Ads read completeness, quota, and refusal classification.

Every case uses `httpx.MockTransport`.  No test performs a live OpenAI request,
and no case reconciles against the OpenAI Ads interface.
"""

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.openai_ads import (
    OPENAI_ADS_ACCOUNT_REQUESTS_PER_MINUTE,
    OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED,
    OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
    OPENAI_ADS_API_HOST,
    OPENAI_ADS_API_VERSION,
    OPENAI_ADS_CONTEXT_HINTS_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_BODY_PRESENT,
    OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
    OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_IMAGE_URL_ABSENT,
    OPENAI_ADS_CREATIVE_IMAGE_URL_PRESENT,
    OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
    OPENAI_ADS_CREATIVE_TITLE_PRESENT,
    OPENAI_ADS_ENDPOINT_REQUESTS_PER_MINUTE,
    OPENAI_ADS_READ_SURFACES,
    OPENAI_ADS_TARGETING_DECLARED,
    OpenAIAdsClient,
    OpenAIAdsHierarchyInventoryQuery,
    OpenAIAdsInventoryQuery,
    OpenAIAdsReportQuery,
    openai_ads_ad_group_quality_statuses,
    openai_ads_ad_quality_statuses,
    openai_ads_campaign_quality_statuses,
)
from adhelm.providers.openai_conversions import (
    OpenAIAdsConversionSettingsClient,
    OpenAIAdsConversionSettingsQuery,
)
from adhelm.transport import SafeHttpClient

TOKEN = "fixture-token-not-for-output"
CONTRACT = "c" * 64


def _client(handler: httpx.MockTransport, *, read_attempts: int = 1) -> OpenAIAdsClient:
    transport = SafeHttpClient(
        httpx.Client(transport=handler),
        {OPENAI_ADS_API_HOST},
        read_attempts=read_attempts,
        sleep=lambda _seconds: None,
    )
    return OpenAIAdsClient(
        transport,
        lambda: TOKEN,
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _account() -> dict[str, Any]:
    return {
        "id": "account_fixture",
        "name": "Fixture account",
        "url": "https://example.invalid",
        "preview_url": None,
        "status": "active",
        "timezone": "UTC",
        "currency_code": "USD",
        "review": {"status": "approved"},
    }


def _campaign(campaign_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": campaign_id,
        "created_at": 1_788_260_800,
        "status": "paused",
        "bidding_type": "impressions",
        "budget": {"lifetime_spend_limit_micros": 10_000_000},
        "conversion_event_setting_ids": [],
        "description": "Fixture",
        "end_time": None,
        "mode": None,
        "name": "Fixture campaign",
        "start_time": 1_788_260_800,
        "targeting": {},
        "updated_at": 1_788_260_800,
        **overrides,
    }


def _ad_group(ad_group_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": ad_group_id,
        "created_at": 1_788_260_800,
        "updated_at": 1_788_260_800,
        "name": "Fixture group",
        "description": "Fixture description",
        "context_hints": ["Fixture context"],
        "status": "paused",
        "bidding_config": {"billing_event_type": "impression", "max_bid_micros": 125_000},
        **overrides,
    }


def _ad(ad_id: str, creative: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return {
        "id": ad_id,
        "name": "Fixture ad",
        "created_at": 1_788_260_800,
        "updated_at": 1_788_260_800,
        "creative": creative
        if creative is not None
        else {
            "type": "chat_card",
            "title": "Fixture title",
            "body": "Fixture body",
            "file_id": "file_fixture",
            "image_url": "https://example.invalid/image.png",
            "target_url": "https://example.invalid/landing",
        },
        "status": "paused",
        "review_status": "approved",
        **overrides,
    }


def _page(items: list[dict[str, Any]], *, has_more: bool = False) -> dict[str, Any]:
    return {
        "object": "list",
        "data": items,
        "first_id": items[0]["id"] if items else None,
        "last_id": items[-1]["id"] if items else None,
        "has_more": has_more,
    }


def _insight(day: str, campaign_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": f"{campaign_id}:{day}",
        "start_time": 1_788_220_800,
        "end_time": 1_788_307_200,
        "readable_time": day,
        "campaign_id": campaign_id,
        "campaign_name": "Fixture campaign",
        "impressions": 10,
        "clicks": 1,
        "spend": 1.25,
        "timezone": "UTC",
        **overrides,
    }


def _insight_page(items: list[dict[str, Any]], *, has_more: bool = False) -> dict[str, Any]:
    return {
        "object": "list",
        "data": items,
        "count": len(items),
        "first_id": items[0]["id"] if items else None,
        "last_id": items[-1]["id"] if items else None,
        "has_more": has_more,
    }


def _report_query(**overrides: Any) -> OpenAIAdsReportQuery:
    return OpenAIAdsReportQuery(
        level="CAMPAIGN",
        account_ref="openai_ads.account.account_fixture",
        report_id="report:openai",
        query_contract_hash=CONTRACT,
        window_start=date(2026, 8, 30),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        currency="USD",
        **overrides,
    )


# --- one pinned version and one documented quota model ------------------------


def test_the_api_version_is_pinned_in_one_shared_constant() -> None:
    from adhelm.providers import openai_conversions

    assert OPENAI_ADS_API_VERSION == "v1"
    assert openai_conversions._API_VERSION is OPENAI_ADS_API_VERSION
    assert openai_conversions._HOST is OPENAI_ADS_API_HOST


def test_every_fixed_read_sends_the_pinned_version_path() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json=_page([_campaign("campaign_first")]))
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(200, json=_page([_ad_group("group_first")]))
        return httpx.Response(200, json=_page([_ad("ad_first")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert paths and all(path.startswith(f"/{OPENAI_ADS_API_VERSION}/") for path in paths)


def test_the_documented_quota_model_is_recorded_as_exact_numbers() -> None:
    assert OPENAI_ADS_ENDPOINT_REQUESTS_PER_MINUTE == 600
    assert OPENAI_ADS_ACCOUNT_REQUESTS_PER_MINUTE == 1_200


def test_the_read_surface_map_never_claims_an_unread_surface() -> None:
    for surface in ("PLACEMENT", "NATIVE_CONVERSION_METRICS", "REPORT_FRESHNESS"):
        assert OPENAI_ADS_READ_SURFACES[surface] in {
            "NOT_IN_REVIEWED_FIXED_READ",
            "NOT_DOCUMENTED",
        }
    assert OPENAI_ADS_READ_SURFACES["AD_CREATIVE_COPY_TEXT"] == "NOT_IN_REVIEWED_FIXED_READ"
    assert (
        OPENAI_ADS_READ_SURFACES["AD_CREATIVE_COPY_PRESENCE"]
        == "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"
    )


# --- pagination is consumed to completion or fails explicitly -----------------


def test_hierarchy_pagination_consumes_every_documented_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            if after is None:
                return httpx.Response(200, json=_page([_campaign("campaign_a")], has_more=True))
            return httpx.Response(200, json=_page([_campaign("campaign_b")]))
        if request.url.path.endswith("/ad_groups"):
            parent = request.url.params["campaign_id"]
            if after is None:
                return httpx.Response(
                    200, json=_page([_ad_group(f"group_{parent}_a")], has_more=True)
                )
            return httpx.Response(200, json=_page([_ad_group(f"group_{parent}_b")]))
        parent_group = request.url.params["ad_group_id"]
        return httpx.Response(200, json=_page([_ad(f"ad_{parent_group}")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert [item.campaign_client_ref for item in hierarchy.campaigns] == [
        "campaign_a",
        "campaign_b",
    ]
    assert len(hierarchy.ad_groups) == 4


def test_an_unfinished_page_run_fails_closed_instead_of_returning_a_partial_read() -> None:
    """Ten pages that never clear `has_more` produce an explicit pagination error."""

    served: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        served.append(1)
        return httpx.Response(
            200, json=_page([_campaign(f"campaign_{len(served)}")], has_more=True)
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    with pytest.raises(AdhelmError) as error:
        client.list_campaigns(
            OpenAIAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert error.value.code == "PAGINATION_LIMIT"
    # Exactly the ten bounded pages, and no partial inventory was returned.
    assert len(served) == 10


def test_a_result_beyond_the_configured_bound_fails_before_a_partial_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json=_insight_page(
                [
                    _insight("2026-08-30", "campaign_a"),
                    _insight("2026-08-31", "campaign_a"),
                ]
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(_report_query(maximum_rows=1, page_size=1), identity=identity)
    assert error.value.code in {"RESULT_LIMIT_REQUIRED", "PROVIDER_RESPONSE_INVALID"}


def test_a_completed_report_declares_the_complete_quality_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json=_insight_page([_insight("2026-08-30", "campaign_a")], has_more=True),
            )
        return httpx.Response(200, json=_insight_page([_insight("2026-08-31", "campaign_a")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    report = client.run_campaign_report(_report_query(), identity=identity)
    assert list(report.envelope.data_quality_statuses) == ["COMPLETE"]
    assert len(report.performance_rows) == 2


# --- refusal classification ---------------------------------------------------


def _refusing_client(status: int, headers: dict[str, str] | None = None) -> OpenAIAdsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers=headers,
            json={"error": {"message": "SENSITIVE-BODY-VALUE", "code": "leak"}},
        )

    return _client(httpx.MockTransport(handler))


def test_an_invalid_or_expired_key_is_an_authorization_failure_with_exit_code_four() -> None:
    with pytest.raises(AdhelmError) as error:
        _refusing_client(401).discover_identity()
    assert error.value.code == "OPENAI_ADS_KEY_INVALID_OR_EXPIRED"
    assert error.value.exit_code == int(ExitCode.AUTHORIZATION)
    assert error.value.status_code == 401


def test_a_forbidden_surface_is_an_authorization_failure_naming_ads_manager() -> None:
    with pytest.raises(AdhelmError) as error:
        _refusing_client(403).discover_identity()
    assert error.value.code == "OPENAI_ADS_ACCESS_NOT_PERMITTED"
    assert error.value.exit_code == int(ExitCode.AUTHORIZATION)
    assert "Ads Manager" in error.value.next_action


def test_a_rate_limit_is_a_precondition_failure_that_discloses_retry_after() -> None:
    with pytest.raises(AdhelmError) as error:
        _refusing_client(429, {"retry-after": "42"}).discover_identity()
    assert error.value.code == "OPENAI_ADS_RATE_LIMITED"
    assert error.value.exit_code == int(ExitCode.PRECONDITION)
    assert error.value.retry_after_seconds == 42
    assert "42 second(s)" in error.value.next_action
    assert "600 requests per minute per endpoint" in error.value.next_action
    assert "1200 per minute overall" in error.value.next_action
    assert "enforced by ad account and by IP address" in error.value.next_action


def test_a_rate_limit_without_a_usable_hint_says_so_rather_than_guessing() -> None:
    with pytest.raises(AdhelmError) as error:
        _refusing_client(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}).discover_identity()
    assert error.value.retry_after_seconds is None
    assert "no whole-second Retry-After hint" in error.value.next_action


def test_a_hostile_retry_after_is_clamped_to_the_disclosed_bound() -> None:
    with pytest.raises(AdhelmError) as error:
        _refusing_client(429, {"retry-after": "99999999"}).discover_identity()
    assert error.value.retry_after_seconds == 3_600


def test_no_refusal_diagnostic_leaks_any_part_of_the_response_body() -> None:
    for status in (400, 401, 403, 404, 429, 500):
        with pytest.raises(AdhelmError) as error:
            _refusing_client(status, {"retry-after": "3"}).discover_identity()
        text = f"{error.value.code}{error.value.message}{error.value.next_action}"
        assert "SENSITIVE-BODY-VALUE" not in text
        assert "leak" not in text
        assert TOKEN not in text


def test_an_unclassified_status_is_re_raised_unchanged() -> None:
    with pytest.raises(AdhelmError) as error:
        _refusing_client(400).discover_identity()
    assert error.value.code == "PROVIDER_REQUEST_FAILED"
    assert error.value.status_code == 400


def test_a_rate_limit_is_retried_within_the_transport_bound_before_it_is_classified() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "slow down"})
        return httpx.Response(200, json=_account())

    client = _client(httpx.MockTransport(handler), read_attempts=3)
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    assert identity.account_client_ref == "account_fixture"
    assert len(attempts) == 2


# --- feature-gated conversion surface ----------------------------------------


def _conversion_client(handler: httpx.MockTransport) -> OpenAIAdsConversionSettingsClient:
    transport = SafeHttpClient(
        httpx.Client(transport=handler), {OPENAI_ADS_API_HOST}, read_attempts=1
    )
    return OpenAIAdsConversionSettingsClient(
        transport,
        lambda: TOKEN,
        identity_session_verifier=lambda _identity: True,
    )


def _identity() -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_account())

    return _client(httpx.MockTransport(handler)).discover_identity(
        expected_account_client_ref="account_fixture"
    )


@pytest.mark.parametrize("status", [403, 404])
def test_a_feature_gated_conversion_surface_is_a_typed_not_enabled_outcome(status: int) -> None:
    identity = _identity()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "not enabled"})

    availability = _conversion_client(httpx.MockTransport(handler)).read_event_settings(
        OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
    )
    assert availability.status == "NOT_ENABLED_FOR_ACCOUNT"
    assert availability.observed_http_status == status
    assert availability.settings == ()


def test_an_unusable_key_on_the_conversion_surface_stays_an_authorization_failure() -> None:
    identity = _identity()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(AdhelmError) as error:
        _conversion_client(httpx.MockTransport(handler)).read_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert error.value.code == "OPENAI_ADS_KEY_INVALID_OR_EXPIRED"
    assert error.value.exit_code == int(ExitCode.AUTHORIZATION)


def test_a_rate_limited_conversion_surface_is_not_reported_as_a_disabled_feature() -> None:
    identity = _identity()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    with pytest.raises(AdhelmError) as error:
        _conversion_client(httpx.MockTransport(handler)).read_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert error.value.code == "OPENAI_ADS_RATE_LIMITED"


def test_an_available_conversion_surface_returns_its_settings_and_no_refusal_status() -> None:
    identity = _identity()
    setting = {
        "id": "conv_1",
        "name": "Fixture",
        "event_type": "lead_created",
        "custom_event_name": None,
        "attribution_window_days": 30,
        "ad_account_id": "account_fixture",
        "source_ids": ["src_1"],
        "sources": [{"id": "src_1", "name": "Fixture source"}],
        "campaigns": [],
        "archived": False,
        "version": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([setting]))

    availability = _conversion_client(httpx.MockTransport(handler)).read_event_settings(
        OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
    )
    assert availability.status == "AVAILABLE"
    assert availability.observed_http_status is None
    assert len(availability.settings) == 1


# --- additive audit-coverage read fields --------------------------------------


def test_the_ad_read_retains_association_and_presence_but_no_copy_or_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json=_page([_campaign("campaign_a")]))
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(200, json=_page([_ad_group("group_a")]))
        return httpx.Response(200, json=_page([_ad("ad_a")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    ad = hierarchy.ads[0]
    assert ad.creative_file_client_ref == "file_fixture"
    assert ad.creative_title_present and ad.creative_body_present
    assert ad.creative_target_url_present and ad.creative_image_url_present
    serialized = ad.model_dump_json()
    assert "Fixture title" not in serialized
    assert "Fixture body" not in serialized
    assert "example.invalid" not in serialized


def test_campaign_and_ad_group_settings_are_presence_and_count_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json=_page(
                    [
                        _campaign(
                            "campaign_a",
                            targeting={"locations": {"include": [1, 2]}},
                            conversion_event_setting_ids=["conv_1", "conv_2"],
                        )
                    ]
                ),
            )
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(200, json=_page([_ad_group("group_a")]))
        return httpx.Response(200, json=_page([_ad("ad_a")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    campaign = hierarchy.campaigns[0]
    assert campaign.targeting_declared is True
    assert campaign.conversion_event_setting_count == 2
    assert "locations" not in campaign.model_dump_json()
    assert hierarchy.ad_groups[0].context_hint_count == 1
    assert "Fixture context" not in hierarchy.ad_groups[0].model_dump_json()


def test_an_empty_targeting_object_is_recorded_as_undeclared() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json=_page([_campaign("campaign_a")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    campaigns = client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert campaigns[0].targeting_declared is False
    assert campaigns[0].conversion_event_setting_count == 0
    assert openai_ads_campaign_quality_statuses(campaigns[0]) == (
        "TARGETING_NOT_DECLARED",
        OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
    )


def test_a_malformed_targeting_or_association_field_fails_closed() -> None:
    for overrides in ({"targeting": "everyone"}, {"conversion_event_setting_ids": "conv_1"}):

        def handler(
            request: httpx.Request, overrides: dict[str, Any] = overrides
        ) -> httpx.Response:
            if request.url.path.endswith("/ad_account"):
                return httpx.Response(200, json=_account())
            return httpx.Response(200, json=_page([_campaign("campaign_a", **overrides)]))

        client = _client(httpx.MockTransport(handler))
        identity = client.discover_identity(expected_account_client_ref="account_fixture")
        with pytest.raises(AdhelmError) as error:
            client.list_campaigns(
                OpenAIAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
        assert error.value.code == "PROVIDER_RESPONSE_INVALID"


def test_the_quality_vocabulary_states_every_fact_in_both_directions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json=_page([_campaign("campaign_a", targeting={"a": 1})]))
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(200, json=_page([_ad_group("group_a")]))
        return httpx.Response(200, json=_page([_ad("ad_a")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert openai_ads_campaign_quality_statuses(hierarchy.campaigns[0])[0] == (
        OPENAI_ADS_TARGETING_DECLARED
    )
    assert openai_ads_ad_group_quality_statuses(hierarchy.ad_groups[0]) == (
        OPENAI_ADS_CONTEXT_HINTS_DECLARED,
    )
    assert openai_ads_ad_quality_statuses(hierarchy.ads[0]) == (
        OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
        OPENAI_ADS_CREATIVE_TITLE_PRESENT,
        OPENAI_ADS_CREATIVE_BODY_PRESENT,
        OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
        OPENAI_ADS_CREATIVE_IMAGE_URL_PRESENT,
        OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
    )


def test_an_incomplete_chat_card_records_the_absent_facts_explicitly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json=_page([_campaign("campaign_a")]))
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(200, json=_page([_ad_group("group_a")]))
        return httpx.Response(
            200,
            json=_page(
                [
                    _ad(
                        "ad_a",
                        creative={
                            "type": "chat_card",
                            "title": None,
                            "body": None,
                            "file_id": None,
                            "image_url": None,
                            "target_url": None,
                        },
                        review_status=None,
                    )
                ]
            ),
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    statuses = openai_ads_ad_quality_statuses(hierarchy.ads[0])
    assert OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED in statuses
    assert OPENAI_ADS_CREATIVE_IMAGE_URL_ABSENT in statuses
    assert OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED in statuses


# --- native money regression --------------------------------------------------


def test_native_budget_and_bid_micros_are_preserved_without_rounding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json=_page(
                    [
                        _campaign(
                            "campaign_a",
                            budget={"lifetime_spend_limit_micros": 9_223_372_036_854_775_807},
                        )
                    ]
                ),
            )
        if request.url.path.endswith("/ad_groups"):
            return httpx.Response(
                200,
                json=_page(
                    [
                        _ad_group(
                            "group_a",
                            bidding_config={
                                "billing_event_type": "impression",
                                "max_bid_micros": 125_001,
                            },
                        )
                    ]
                ),
            )
        return httpx.Response(200, json=_page([_ad("ad_a")]))

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity(expected_account_client_ref="account_fixture")
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert hierarchy.campaigns[0].lifetime_spend_limit_micros == "9223372036854775807"
    assert hierarchy.ad_groups[0].max_bid_micros == "125001"

    from adhelm.inventory import _openai_minor, _openai_money_quality, _openai_snapshots

    # A subminor amount is never rounded down into a minor-unit projection.
    assert _openai_minor("125001", "USD") is None
    assert _openai_money_quality("125001", "USD") == ["SUBMINOR_AMOUNT_RETAINED_AS_NATIVE_MICROS"]
    snapshots = _openai_snapshots(
        identity,
        list(hierarchy.campaigns),
        list(hierarchy.ad_groups),
        list(hierarchy.ads),
    )
    group = next(item for item in snapshots if item["resource_type"] == "AD_GROUP")
    assert group["normalized_state"]["openai_ads_max_bid_micros"] == "125001"
    assert "bid_amount_minor" not in group["normalized_state"]
    assert "SUBMINOR_AMOUNT_RETAINED_AS_NATIVE_MICROS" in group["data_quality_statuses"]
    # The additive audit statuses are appended, never replacing the money ones.
    assert OPENAI_ADS_CONTEXT_HINTS_DECLARED in group["data_quality_statuses"]

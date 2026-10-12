# ruff: noqa: E501
"""Meta Ads read completeness, classification, and presence-only facts.

Every case is hermetic: `httpx.MockTransport` only, no live Graph request, no
credential store, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.meta_ads import (
    META_ADS_API_HOST,
    META_ADS_API_VERSION,
    META_ADS_ATTRIBUTION_MODEL,
    META_ADS_BID_STRATEGY_NOT_RETURNED,
    META_ADS_BID_STRATEGY_RETURNED,
    META_ADS_BUDGET_MODE_DAILY,
    META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,
    META_ADS_BUDGET_MODE_LIFETIME,
    META_ADS_BUDGET_MODE_NOT_RETURNED,
    META_ADS_BUYING_TYPE_NOT_REVIEWED,
    META_ADS_BUYING_TYPE_RESERVED,
    META_ADS_BUYING_TYPE_RETURNED,
    META_ADS_CREATIVE_ASSOCIATED,
    META_ADS_CREATIVE_IMAGE_HASH_ABSENT,
    META_ADS_CREATIVE_IMAGE_HASH_PRESENT,
    META_ADS_CREATIVE_LINK_ABSENT,
    META_ADS_CREATIVE_LINK_PRESENT,
    META_ADS_CREATIVE_NOT_ASSOCIATED,
    META_ADS_RATE_LIMIT_ERROR_CODES,
    META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED,
    META_ADS_READ_SURFACES,
    META_ADS_REPORT_COMPLETE_STATUS,
    META_ADS_SUPPORTED_ATTRIBUTION_WINDOWS,
    META_ADS_TARGETING_AGE_ABSENT,
    META_ADS_TARGETING_AGE_PRESENT,
    META_ADS_TARGETING_DECLARED,
    META_ADS_TARGETING_GEO_ABSENT,
    META_ADS_TARGETING_GEO_PRESENT,
    META_ADS_TARGETING_INTEREST_ABSENT,
    META_ADS_TARGETING_INTEREST_PRESENT,
    META_ADS_TARGETING_NOT_DECLARED,
    MetaAdsClient,
    MetaAdsInventoryQuery,
    MetaAdsReportQuery,
    classify_meta_ads_transport_error,
    meta_ads_ad_quality_statuses,
    meta_ads_ad_set_quality_statuses,
    meta_ads_campaign_quality_statuses,
    meta_ads_declares_serving_intent,
    meta_ads_rate_limit_hint,
)
from adhelm.transport import SafeHttpClient

_BODY_SECRET = "operator-only-provider-explanation"


def _client(handler: httpx.MockTransport, *, attempts: int = 1) -> MetaAdsClient:
    return MetaAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler), {META_ADS_API_HOST}, read_attempts=attempts
        ),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _account() -> dict[str, Any]:
    return {"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"}


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


def _identity(client: MetaAdsClient) -> Any:
    return client.discover_identity("act_123", expected_account_client_ref="123")


# ---------------------------------------------------------------- version pin


def test_the_graph_version_is_pinned_in_one_shared_constant() -> None:
    import adhelm.providers.meta_ads as module

    assert META_ADS_API_VERSION == "v26.0"
    assert module._API_VERSION is META_ADS_API_VERSION
    assert module._HOST is META_ADS_API_HOST


def test_every_fixed_read_sends_the_pinned_version_path() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/insights"):
            return httpx.Response(200, json={"data": []})
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"data": []})

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    query = MetaAdsInventoryQuery(account_ref=identity.account_ref)
    client.list_campaigns(query, identity=identity)
    client.list_adsets(query, identity=identity)
    client.list_ads(query, identity=identity)
    client.run_campaign_report(_report_query(identity.account_ref), identity=identity)
    assert paths
    assert all(path.startswith(f"/{META_ADS_API_VERSION}/") for path in paths)


def test_the_read_surface_map_never_claims_an_unread_surface() -> None:
    """A presence-only or unread surface may never be marked as a full read."""
    import adhelm.providers.meta_ads as module

    requested = ",".join(
        (module._ACCOUNT_FIELDS, module._CAMPAIGN_FIELDS, module._ADSET_FIELDS, module._AD_FIELDS)
    )
    # The three fields that would carry ad copy are never requested at all.
    for forbidden in ("object_story_spec", "title", "body", "asset_feed_spec"):
        assert forbidden not in requested
    assert META_ADS_READ_SURFACES["AD_CREATIVE_COPY_TEXT"] == "NOT_IN_REVIEWED_FIXED_READ"
    assert META_ADS_READ_SURFACES["AD_SET_PLACEMENT"] == "NOT_IN_REVIEWED_FIXED_READ"
    assert META_ADS_READ_SURFACES["PIXEL_AND_EVENT_HEALTH"] == "NOT_IN_REVIEWED_FIXED_READ"
    assert META_ADS_READ_SURFACES["NATIVE_CONVERSION_METRICS"] == "NOT_IN_REVIEWED_FIXED_READ"
    assert META_ADS_READ_SURFACES["REPORT_FRESHNESS"] == "NOT_IN_REVIEWED_FIXED_READ"
    # Everything the map calls presence-only is requested but never retained
    # as a value; everything it calls a full read is actually requested.
    assert "targeting" in module._ADSET_FIELDS
    assert "image_hash" in module._AD_FIELDS
    assert "buying_type" in module._CAMPAIGN_FIELDS
    assert META_ADS_READ_SURFACES["AD_SET_TARGETING_PRESENCE"] == (
        "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"
    )
    assert set(META_ADS_READ_SURFACES.values()) <= {
        "IMPLEMENTED_FIXED_READ",
        "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
        "NOT_IN_REVIEWED_FIXED_READ",
        "NOT_DOCUMENTED",
    }


def test_the_supported_attribution_windows_match_the_query_contract() -> None:
    from typing import get_args

    field = MetaAdsReportQuery.model_fields["action_attribution_windows"]
    declared = get_args(get_args(field.annotation)[0])
    assert set(declared) == set(META_ADS_SUPPORTED_ATTRIBUTION_WINDOWS)


# ----------------------------------------------------------------- pagination


def test_cursor_pagination_consumes_every_documented_page() -> None:
    pages = {
        None: {
            "data": [{"id": "11", "effective_status": "ACTIVE"}],
            "paging": {
                "cursors": {"after": "cursor-one"},
                "next": f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/act_123/campaigns?after=cursor-one",
            },
        },
        "cursor-one": {"data": [{"id": "12", "effective_status": "PAUSED"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        after = request.url.params.get("after")
        return httpx.Response(200, json=pages[after])

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    campaigns = client.list_campaigns(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert [item.campaign_client_ref for item in campaigns] == ["11", "12"]


def test_a_repeated_cursor_fails_closed_instead_of_looping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "data": [{"id": "11", "effective_status": "ACTIVE"}],
                "paging": {
                    "cursors": {"after": "same"},
                    "next": f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/act_123/campaigns?after=same",
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code in {"PROVIDER_RESPONSE_INVALID", "RESULT_LIMIT_REQUIRED"}


def test_a_continuing_page_at_the_configured_bound_fails_before_a_partial_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "data": [{"id": "11", "effective_status": "ACTIVE"}],
                "paging": {
                    "cursors": {"after": "next-cursor"},
                    "next": f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/act_123/campaigns?after=next-cursor",
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=1, page_size=1),
            identity=identity,
        )
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"


def test_a_completed_report_declares_the_complete_quality_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
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

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    report = client.run_campaign_report(_report_query(identity.account_ref), identity=identity)
    assert META_ADS_REPORT_COMPLETE_STATUS in report.envelope.data_quality_statuses
    assert report.envelope.attribution_model == META_ADS_ATTRIBUTION_MODEL
    assert report.envelope.attribution_window == "7d_click,1d_view"
    assert report.envelope.data_fresh_through is None
    # Native minor units in the account's own currency, computed exactly from
    # the reviewed per-currency exponent and never rounded.
    row = report.performance_rows[0]
    assert row.spend is not None and row.spend.minor_units == 125
    assert row.spend.currency == "USD"


# ------------------------------------------------------------- classification


@pytest.mark.parametrize("code", sorted(META_ADS_RATE_LIMIT_ERROR_CODES))
def test_a_documented_graph_rate_limit_code_is_a_precondition_failure(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "error": {
                    "message": _BODY_SECRET,
                    "type": "OAuthException",
                    "code": code,
                    "error_subcode": 2_446_079,
                    "fbtrace_id": _BODY_SECRET,
                }
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    error = raised.value
    assert error.code == "META_ADS_RATE_LIMITED"
    assert error.exit_code == 3
    assert META_ADS_RATE_LIMIT_ERROR_CODES[code] in error.next_action
    assert _BODY_SECRET not in error.message
    assert _BODY_SECRET not in error.next_action


def test_an_undocumented_graph_error_code_is_not_relabelled_as_a_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"error": {"code": 100, "message": _BODY_SECRET}})

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_REQUEST_FAILED"
    assert _BODY_SECRET not in raised.value.message


def test_a_non_integer_graph_error_code_is_an_invalid_response_not_a_guess() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"error": {"code": "4"}})

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_an_http_rate_limit_status_is_classified_as_a_precondition_failure() -> None:
    error = AdhelmError("PROVIDER_REQUEST_FAILED", "x", status_code=429, retry_after_seconds=30)
    classified = classify_meta_ads_transport_error(error)
    assert classified.code == "META_ADS_RATE_LIMITED"
    assert classified.exit_code == 3
    assert classified.status_code == 429
    assert classified.retry_after_seconds == 30


@pytest.mark.parametrize("status", [401, 403])
def test_a_denied_status_is_an_authorization_failure_with_exit_code_four(status: int) -> None:
    error = AdhelmError("PROVIDER_ACCESS_REQUIRED", "x", status_code=status)
    classified = classify_meta_ads_transport_error(error)
    assert classified.code == "PROVIDER_ACCESS_REQUIRED"
    assert classified.exit_code == 4
    # The authorization diagnostic never asserts an app-review outcome.
    assert "not documented locally" in classified.next_action


def test_an_unclassified_status_is_returned_unchanged() -> None:
    error = AdhelmError("PROVIDER_REQUEST_FAILED", "x", status_code=500)
    assert classify_meta_ads_transport_error(error) is error


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED),
        ("", META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED),
        # The documented header is a JSON object in practice.  A structured
        # value is deliberately reported as not disclosed rather than parsed.
        (
            '{"123":[{"estimated_time_to_regain_access":11}]}',
            META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED,
        ),
        # `str.isdigit` accepts this superscript; `int` does not.  The ASCII
        # bound stops it before a parse failure.
        ("²", META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED),
        ("٣٤", META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED),
        ("11", 11),
        ("999999999999999", META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED),
        ("99999", 1_440),
    ],
)
def test_the_business_use_case_hint_is_bounded_ascii_decimal_or_not_disclosed(
    header: str | None, expected: int | str
) -> None:
    assert meta_ads_rate_limit_hint(header) == expected


def test_a_rate_limit_without_a_usable_hint_says_so_rather_than_guessing() -> None:
    from adhelm.providers.meta_ads import classify_meta_ads_graph_error

    silent = classify_meta_ads_graph_error(4)
    disclosed = classify_meta_ads_graph_error(4, business_use_case_usage="7")
    assert "no bounded whole-minute regain estimate" in silent.next_action
    assert "estimated 7 minute(s)" in disclosed.next_action


def test_the_shared_transport_discloses_no_business_use_case_header_to_this_build() -> None:
    """The header is structurally unavailable, so the hint is always undisclosed here.

    `SafeHttpClient.request` rebuilds every response with a fixed content type
    and drops provider headers, and it raises before reading a body for any
    status at or above 400.  This is why the read contract records the hint as
    `NOT_DISCLOSED` rather than claiming a bound it cannot observe.
    """
    seen: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_account(),
            headers={
                "x-business-use-case-usage": '{"123":[{"estimated_time_to_regain_access":9}]}'
            },
        )

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), {META_ADS_API_HOST}, read_attempts=1
    )
    seen.append(transport.request("GET", f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/x"))
    assert "x-business-use-case-usage" not in seen[0].headers
    assert meta_ads_rate_limit_hint(seen[0].headers.get("x-business-use-case-usage")) == (
        META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED
    )


# ------------------------------------------------------------- presence facts


def _inventory_handler(adset: dict[str, Any], ad: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/adsets"):
            return httpx.Response(200, json={"data": [adset]})
        return httpx.Response(200, json={"data": [ad]})

    return httpx.MockTransport(handler)


def test_targeting_is_read_as_presence_and_never_as_a_value() -> None:
    adset = {
        "id": "12",
        "campaign_id": "11",
        "effective_status": "ACTIVE",
        "bid_strategy": "COST_CAP",
        "targeting": {
            "geo_locations": {"countries": ["US"]},
            "age_min": 25,
            "interests": [{"id": "6003", "name": "Cycling"}],
            "publisher_platforms": ["facebook"],
        },
    }
    client = _client(_inventory_handler(adset, {}))
    identity = _identity(client)
    parsed = client.list_adsets(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )[0]
    assert parsed.targeting_declared
    assert parsed.targeting_geo_present
    assert parsed.targeting_age_present
    assert parsed.targeting_interest_present
    # No targeting value survives into the model or its serialization.
    serialized = parsed.model_dump_json()
    for value in ("US", "Cycling", "6003", "facebook", "geo_locations"):
        assert value not in serialized
    statuses = meta_ads_ad_set_quality_statuses(parsed)
    assert META_ADS_TARGETING_DECLARED in statuses
    assert META_ADS_TARGETING_GEO_PRESENT in statuses
    assert META_ADS_TARGETING_AGE_PRESENT in statuses
    assert META_ADS_TARGETING_INTEREST_PRESENT in statuses


def test_an_absent_or_empty_targeting_object_is_recorded_as_undeclared() -> None:
    for targeting in ({}, None):
        adset = {"id": "12", "campaign_id": "11", "effective_status": "PAUSED"}
        if targeting is not None:
            adset["targeting"] = targeting
        client = _client(_inventory_handler(adset, {}))
        identity = _identity(client)
        parsed = client.list_adsets(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )[0]
        statuses = meta_ads_ad_set_quality_statuses(parsed)
        assert META_ADS_TARGETING_NOT_DECLARED in statuses
        assert META_ADS_TARGETING_GEO_ABSENT in statuses
        assert META_ADS_TARGETING_AGE_ABSENT in statuses
        assert META_ADS_TARGETING_INTEREST_ABSENT in statuses
        assert META_ADS_BUDGET_MODE_NOT_RETURNED in statuses
        assert META_ADS_BID_STRATEGY_NOT_RETURNED in statuses


def test_a_malformed_targeting_object_fails_closed() -> None:
    adset = {"id": "12", "campaign_id": "11", "effective_status": "ACTIVE", "targeting": ["us"]}
    client = _client(_inventory_handler(adset, {}))
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="targeting"):
        client.list_adsets(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )


def test_a_person_level_targeting_value_still_fails_the_whole_read_closed() -> None:
    """An uploaded audience really can carry a person-level value.

    The whole-read screen keeps that failing closed, and the refusal is a local
    screening decision with a next action rather than a bare provider error.
    """
    adset = {
        "id": "12",
        "campaign_id": "11",
        "effective_status": "ACTIVE",
        "targeting": {"geo_locations": {"custom_audiences": ["person@example.com"]}},
    }
    client = _client(_inventory_handler(adset, {}))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_adsets(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "META_ADS_RESPONSE_WITHHELD_BY_LOCAL_PRIVACY_SCREEN"
    assert raised.value.exit_code == 3
    assert "local screening decision, not a Meta error" in raised.value.next_action
    # The matched value is never named, quoted, or hashed.
    assert "person@example.com" not in raised.value.message + raised.value.next_action


def test_a_screened_creative_value_withholds_one_fact_and_not_the_whole_read() -> None:
    """One advertiser landing page must not take a whole account offline."""
    from adhelm.providers.meta_ads import (
        META_ADS_CREATIVE_LINK_PRESENT,
        META_ADS_CREATIVE_VALUE_NOT_WITHHELD,
        META_ADS_CREATIVE_VALUE_WITHHELD,
    )

    ad = {
        "id": "13",
        "campaign_id": "11",
        "adset_id": "12",
        "effective_status": "ACTIVE",
        "creative": {"id": "900", "link_url": "https://shop.example.com/lp?c=sales@example.com"},
    }
    client = _client(_inventory_handler({}, ad))
    identity = _identity(client)
    parsed = client.list_ads(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )[0]
    assert parsed.creative_link_present
    assert parsed.creative_screened_value_withheld
    statuses = meta_ads_ad_quality_statuses(parsed)
    assert META_ADS_CREATIVE_LINK_PRESENT in statuses
    assert META_ADS_CREATIVE_VALUE_WITHHELD in statuses
    assert META_ADS_CREATIVE_VALUE_NOT_WITHHELD not in statuses
    # The value itself is withheld either way.
    assert "sales@example.com" not in parsed.model_dump_json()


def test_the_ad_read_retains_creative_presence_but_no_hash_or_url() -> None:
    ad = {
        "id": "13",
        "campaign_id": "11",
        "adset_id": "12",
        "effective_status": "ACTIVE",
        "creative": {
            "id": "900",
            "image_hash": "abc123hash",
            "object_url": "https://example.invalid/landing",
        },
    }
    client = _client(_inventory_handler({}, ad))
    identity = _identity(client)
    parsed = client.list_ads(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )[0]
    assert parsed.creative_associated
    assert parsed.creative_image_hash_present
    assert parsed.creative_link_present
    serialized = parsed.model_dump_json()
    for value in ("abc123hash", "example.invalid", "landing", "900"):
        assert value not in serialized
    statuses = meta_ads_ad_quality_statuses(parsed)
    assert META_ADS_CREATIVE_ASSOCIATED in statuses
    assert META_ADS_CREATIVE_IMAGE_HASH_PRESENT in statuses
    assert META_ADS_CREATIVE_LINK_PRESENT in statuses
    assert not parsed.creative_screened_value_withheld


def test_an_ad_with_no_creative_records_every_absent_fact_explicitly() -> None:
    ad = {"id": "13", "campaign_id": "11", "adset_id": "12", "effective_status": "DISAPPROVED"}
    client = _client(_inventory_handler({}, ad))
    identity = _identity(client)
    parsed = client.list_ads(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )[0]
    statuses = meta_ads_ad_quality_statuses(parsed)
    assert META_ADS_CREATIVE_NOT_ASSOCIATED in statuses
    assert META_ADS_CREATIVE_IMAGE_HASH_ABSENT in statuses
    assert META_ADS_CREATIVE_LINK_ABSENT in statuses
    assert "META_AD_REVIEW_BLOCKED" in statuses


def test_an_unrequested_creative_field_fails_closed() -> None:
    ad = {
        "id": "13",
        "campaign_id": "11",
        "adset_id": "12",
        "effective_status": "ACTIVE",
        "creative": {"id": "900", "body": "buy now"},
    }
    client = _client(_inventory_handler({}, ad))
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="unsupported field"):
        client.list_ads(MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity)


def test_native_budget_minor_units_are_preserved_without_rounding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "11",
                        "effective_status": "ACTIVE",
                        "objective": "OUTCOME_TRAFFIC",
                        "buying_type": "AUCTION",
                        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                        "daily_budget": "1",
                        "lifetime_budget": "9007199254740991",
                        "spend_cap": "0",
                    }
                ]
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    campaign = client.list_campaigns(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )[0]
    assert campaign.daily_budget is not None and campaign.daily_budget.minor_units == 1
    assert campaign.lifetime_budget is not None
    assert campaign.lifetime_budget.minor_units == 9_007_199_254_740_991
    assert campaign.spend_cap is not None and campaign.spend_cap.minor_units == 0
    statuses = meta_ads_campaign_quality_statuses(campaign)
    assert META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME in statuses
    assert META_ADS_BUYING_TYPE_RETURNED in statuses
    assert META_ADS_BID_STRATEGY_RETURNED in statuses


@pytest.mark.parametrize("field", ["daily_budget", "lifetime_budget", "spend_cap"])
@pytest.mark.parametrize("value", ["1" * 17, "1" * 5000], ids=["17-digits", "5000-digits"])
def test_an_over_long_budget_is_a_response_refusal_not_a_crash(field: str, value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "11",
                        "effective_status": "ACTIVE",
                        "objective": "OUTCOME_TRAFFIC",
                        "buying_type": "AUCTION",
                        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                        field: value,
                    }
                ]
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_the_quality_vocabulary_states_every_fact_in_both_directions() -> None:
    """No absent status may have to be read as either "present" or "absent"."""
    import adhelm.providers.meta_ads as module

    pairs = [
        ("META_ADS_OBJECTIVE_RETURNED", "META_ADS_OBJECTIVE_NOT_RETURNED"),
        ("META_ADS_BUYING_TYPE_RETURNED", "META_ADS_BUYING_TYPE_NOT_RETURNED"),
        ("META_ADS_BID_STRATEGY_RETURNED", "META_ADS_BID_STRATEGY_NOT_RETURNED"),
        ("META_ADS_TARGETING_DECLARED", "META_ADS_TARGETING_NOT_DECLARED"),
        ("META_ADS_TARGETING_GEO_PRESENT", "META_ADS_TARGETING_GEO_ABSENT"),
        ("META_ADS_TARGETING_AGE_PRESENT", "META_ADS_TARGETING_AGE_ABSENT"),
        ("META_ADS_TARGETING_INTEREST_PRESENT", "META_ADS_TARGETING_INTEREST_ABSENT"),
        ("META_ADS_SCHEDULE_END_PRESENT", "META_ADS_SCHEDULE_END_ABSENT"),
        ("META_ADS_CREATIVE_ASSOCIATED", "META_ADS_CREATIVE_NOT_ASSOCIATED"),
        ("META_ADS_CREATIVE_IMAGE_HASH_PRESENT", "META_ADS_CREATIVE_IMAGE_HASH_ABSENT"),
        ("META_ADS_CREATIVE_LINK_PRESENT", "META_ADS_CREATIVE_LINK_ABSENT"),
    ]
    for present, absent in pairs:
        assert getattr(module, present) != getattr(module, absent)
    assert {
        META_ADS_BUDGET_MODE_DAILY,
        META_ADS_BUDGET_MODE_LIFETIME,
        META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,
        META_ADS_BUDGET_MODE_NOT_RETURNED,
    } == {module.meta_ads_budget_mode_status(*pair) for pair in _BUDGET_PAIRS()}


def _BUDGET_PAIRS() -> list[tuple[Any, Any]]:
    from adhelm.models import Money

    amount = Money(minor_units=1, currency="USD")
    return [(amount, amount), (amount, None), (None, amount), (None, None)]


def test_a_reserved_buying_type_and_an_unreviewed_one_are_distinguished() -> None:
    from adhelm.providers.meta_ads import MetaAdsCampaign

    reserved = MetaAdsCampaign(
        campaign_ref="meta_ads.campaign.11",
        campaign_client_ref="11",
        status="ACTIVE",
        buying_type="RESERVED",
    )
    unknown = MetaAdsCampaign(
        campaign_ref="meta_ads.campaign.12",
        campaign_client_ref="12",
        status="ACTIVE",
        buying_type="FIXED_CPM",
    )
    assert META_ADS_BUYING_TYPE_RESERVED in meta_ads_campaign_quality_statuses(reserved)
    assert META_ADS_BUYING_TYPE_NOT_REVIEWED in meta_ads_campaign_quality_statuses(unknown)
    assert META_ADS_BUYING_TYPE_RESERVED not in meta_ads_campaign_quality_statuses(unknown)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ACTIVE", True),
        ("active", True),
        ("PAUSED", False),
        ("CAMPAIGN_PAUSED", False),
        (None, False),
    ],
)
def test_serving_intent_reads_the_documented_effective_status(
    status: str | None, expected: bool
) -> None:
    assert meta_ads_declares_serving_intent(status) is expected


def test_a_numeric_zero_targeting_bound_is_present_not_absent() -> None:
    """Emptiness is a container/string property, never a numeric one.

    Reading a falsy scalar as an absence would report a targeting gap the
    provider did not return.
    """
    adset = {
        "id": "12",
        "campaign_id": "11",
        "effective_status": "ACTIVE",
        "targeting": {"age_min": 0, "geo_locations": {}, "interests": []},
    }
    client = _client(_inventory_handler(adset, {}))
    identity = _identity(client)
    parsed = client.list_adsets(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )[0]
    assert parsed.targeting_declared
    assert parsed.targeting_age_present
    assert not parsed.targeting_geo_present
    assert not parsed.targeting_interest_present


def test_a_malformed_creative_presence_field_fails_closed() -> None:
    ad = {
        "id": "13",
        "campaign_id": "11",
        "adset_id": "12",
        "effective_status": "ACTIVE",
        "creative": {"id": "900", "image_hash": 17},
    }
    client = _client(_inventory_handler({}, ad))
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="ad creative is invalid"):
        client.list_ads(MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity)


# ============================================================ O2.2 read gaps
#
# Identity, grammar, daily-window boundaries, repeated rows, freshness and the
# null / zero / unavailable distinction.  See "Read completeness review (O2.2)"
# in docs/research/meta-ads-read-contract.md.


def _insight(
    campaign: str = "11",
    day: str = "2026-09-01",
    *,
    ad: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "campaign_id": campaign,
        "date_start": day,
        "date_stop": day,
        "impressions": "10",
        "clicks": "2",
        "spend": "1.25",
        "reach": "8",
    }
    if ad is not None:
        row["adset_id"] = "21"
        row["ad_id"] = ad
    row.update(overrides)
    return row


def _insights_client(
    pages: list[dict[str, Any]],
    *,
    account: dict[str, Any] | None = None,
    now: datetime = datetime(2026, 9, 4, 12, tzinfo=UTC),
    requests: list[httpx.Request] | None = None,
) -> MetaAdsClient:
    """A client whose insights edge serves `pages` in order, then stops."""
    served = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=account or _account())
        return httpx.Response(200, json=next(served))

    return MetaAdsClient(
        SafeHttpClient(httpx.Client(transport=httpx.MockTransport(handler)), {META_ADS_API_HOST}),
        lambda: "fixture-token-not-for-output",
        now=lambda: now,
        connector_version="test",
    )


def _insight_page(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(rows)}


def _continuing(after: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "paging": {
            "cursors": {"after": after},
            "next": f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/act_123/insights?after={after}",
        },
    }


def _daily_query(
    identity: Any,
    start: date = date(2026, 9, 1),
    end: date = date(2026, 9, 2),
    *,
    level: str = "CAMPAIGN",
    **overrides: Any,
) -> MetaAdsReportQuery:
    return MetaAdsReportQuery(
        level=level,  # type: ignore[arg-type]
        account_ref=identity.account_ref,
        report_id="report:fixture",
        query_contract_hash="a" * 64,
        window_start=start,
        window_end=end,
        source_timezone=identity.timezone,
        action_attribution_windows=("7d_click",),
        **overrides,
    )


# ------------------------------------------------------------------- identity


def test_the_returned_account_must_be_the_requested_account() -> None:
    client = _insights_client([], account={**_account(), "id": "act_999"})
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("act_123")
    assert raised.value.code == "IDENTITY_MISMATCH"


def test_a_profile_account_that_differs_from_the_requested_account_is_refused() -> None:
    client = _insights_client([])
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("act_123", expected_account_client_ref="act_124")
    assert raised.value.code == "IDENTITY_MISMATCH"


@pytest.mark.parametrize(
    "timezone", ["../etc/localtime", "America/", "/UTC", "Not/AZone", "America", "Etc"]
)
def test_a_malformed_account_timezone_is_a_response_refusal_not_a_crash(timezone: str) -> None:
    client = _insights_client([], account={**_account(), "timezone_name": timezone})
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("act_123")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "timezone" in raised.value.message
    assert "site-packages" not in raised.value.message


def test_a_report_for_another_account_stops_before_any_insights_request() -> None:
    requests: list[httpx.Request] = []
    client = _insights_client([_insight_page()], requests=requests)
    identity = _identity(client)
    foreign = identity.model_copy(update={"account_ref": "meta_ads.account.act_999"})
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_daily_query(identity), identity=foreign)
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"source_timezone": "Europe/Paris"}, "TIMEZONE_MISMATCH"),
        ({"currency": "EUR"}, "CURRENCY_MISMATCH"),
        ({"level": "AD"}, "QUERY_GRAIN_MISMATCH"),
    ],
)
def test_a_report_query_that_disagrees_with_the_identity_stops_before_egress(
    update: dict[str, Any], code: str
) -> None:
    requests: list[httpx.Request] = []
    client = _insights_client([_insight_page()], requests=requests)
    identity = _identity(client)
    query = _daily_query(identity).model_copy(update=update)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(query, identity=identity)
    assert raised.value.code == code
    assert len(requests) == 1


# -------------------------------------------------- strict numeric and day grammar


@pytest.mark.parametrize("value", ["٣", "１２", "0123", "12a", "", " 12", "-1"])
def test_a_lookalike_or_malformed_identifier_never_reaches_a_reference(value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"data": [{"id": value, "effective_status": "ACTIVE"}]})

    client = _client(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="campaign ID is invalid"):
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )


@pytest.mark.parametrize("metric", ["impressions", "clicks", "reach"])
@pytest.mark.parametrize("value", ["٣", "1.0", "1e3", "-1", " 1", "+1", "1_0", 7, True])
def test_a_count_that_is_not_an_ascii_digit_string_refuses_the_response(
    metric: str, value: Any
) -> None:
    client = _insights_client([_insight_page(_insight(**{metric: value}))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="is invalid"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


@pytest.mark.parametrize("metric", ["impressions", "clicks", "reach"])
@pytest.mark.parametrize("value", ["1" * 17, "1" * 5000], ids=["17-digits", "5000-digits"])
def test_an_over_long_count_is_a_response_refusal_not_a_crash(metric: str, value: str) -> None:
    client = _insights_client([_insight_page(_insight(**{metric: value}))])
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_daily_query(identity), identity=identity)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "value",
    [
        "1e2",
        "1_0",
        "٣",
        "１２",
        " 1",
        "1 ",
        "-0",
        "-1",
        "+1",
        "NaN",
        "Infinity",
        "1.",
        ".5",
        "01",
        "",
    ],
)
def test_spend_outside_the_plain_decimal_grammar_refuses_the_response(value: str) -> None:
    client = _insights_client([_insight_page(_insight(spend=value))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="insight spend is invalid"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


def test_spend_with_precision_the_currency_cannot_hold_is_refused_not_rounded() -> None:
    client = _insights_client([_insight_page(_insight(spend="1.255"))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="unsupported precision"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


@pytest.mark.parametrize("spend", ["1.0000000000000000000000000001", "0." + "0" * 30 + "1"])
def test_spend_beyond_the_decimal_context_is_refused_not_rounded(spend: str) -> None:
    client = _insights_client([_insight_page(_insight(spend=spend))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="unsupported precision"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


@pytest.mark.parametrize("spend", ["1" * 40 + ".01", "9007199254740991", "90071992547409.92"])
def test_spend_beyond_the_numeric_bound_is_refused_as_a_magnitude(spend: str) -> None:
    client = _insights_client([_insight_page(_insight(spend=spend))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="exceeds the numeric bound"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


def test_spend_failing_both_precision_and_magnitude_is_refused_as_precision() -> None:
    client = _insights_client([_insight_page(_insight(spend="9007199254740991.001"))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="unsupported precision"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


def test_spend_longer_than_eighty_characters_is_invalid_before_any_magnitude_check() -> None:
    client = _insights_client([_insight_page(_insight(spend="1" * 81))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="insight spend is invalid"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


def test_spend_at_the_numeric_bound_is_accepted_exactly() -> None:
    client = _insights_client([_insight_page(_insight(spend="90071992547409.91"))])
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    spend = report.performance_rows[0].spend
    assert spend is not None
    assert (spend.minor_units, spend.currency) == (9_007_199_254_740_991, "USD")


def test_spend_with_only_trailing_zero_precision_is_kept_exactly() -> None:
    client = _insights_client([_insight_page(_insight(spend="1.2500000000000000000000000000000"))])
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    spend = report.performance_rows[0].spend
    assert spend is not None
    assert (spend.minor_units, spend.currency) == (125, "USD")


def test_spend_converts_through_the_account_currency_exponent() -> None:
    account = {**_account(), "currency": "JPY"}
    client = _insights_client(
        [_insight_page(_insight(spend="1500"), _insight("12", spend="0.0"))], account=account
    )
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    spends = [row.spend for row in report.performance_rows]
    assert [(m.minor_units, m.currency) for m in spends if m is not None] == [
        (1500, "JPY"),
        (0, "JPY"),
    ]
    jpy_fraction = _insights_client([_insight_page(_insight(spend="1500.5"))], account=account)
    with pytest.raises(AdhelmError, match="unsupported precision"):
        jpy_fraction.run_campaign_report(_daily_query(identity), identity=_identity(jpy_fraction))


@pytest.mark.parametrize(
    "day", ["20260901", "2026-W36-2", "2026-9-1", "٢٠٢٦-٠٩-٠١", "2026-09-01T00"]
)
def test_an_insight_day_must_be_the_documented_calendar_date_format(day: str) -> None:
    client = _insights_client([_insight_page(_insight(day=day))])
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_daily_query(identity), identity=identity)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "row",
    [
        _insight(day="2026-08-31"),
        _insight(day="2026-09-03"),
        _insight(date_stop="2026-09-02"),
    ],
)
def test_an_insight_outside_the_window_or_spanning_days_is_refused(row: dict[str, Any]) -> None:
    client = _insights_client([_insight_page(row)])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="outside the requested daily window"):
        client.run_campaign_report(_daily_query(identity), identity=identity)


# ------------------------------------------------------- null, zero, unavailable


def test_a_zero_metric_is_retained_as_zero() -> None:
    client = _insights_client(
        [_insight_page(_insight(impressions="0", clicks="0", spend="0", reach="0"))]
    )
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    row = report.performance_rows[0]
    assert (row.impressions, row.clicks, row.reach) == (0, 0, 0)
    assert row.spend is not None and row.spend.minor_units == 0


@pytest.mark.parametrize("metric", ["impressions", "clicks", "spend", "reach"])
def test_a_missing_or_null_metric_is_refused_and_never_read_as_zero(metric: str) -> None:
    absent = _insight()
    del absent[metric]
    for row in (absent, _insight(**{metric: None})):
        client = _insights_client([_insight_page(row)])
        identity = _identity(client)
        with pytest.raises(AdhelmError, match=f"insight {metric} is invalid"):
            client.run_campaign_report(_daily_query(identity), identity=identity)


def test_a_window_with_no_delivery_returns_no_rows_rather_than_zero_rows() -> None:
    client = _insights_client([_insight_page()])
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    assert report.performance_rows == []


# ---------------------------------------------------------- repeated resource days


def test_a_resource_day_repeated_on_a_later_page_is_refused() -> None:
    client = _insights_client(
        [_continuing("c1", [_insight()]), _insight_page(_insight(impressions="99"))]
    )
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="repeated a resource day") as raised:
        client.run_campaign_report(_daily_query(identity), identity=identity)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_an_ad_day_repeated_within_one_page_is_refused() -> None:
    client = _insights_client([_insight_page(_insight(ad="31"), _insight(ad="31"))])
    identity = _identity(client)
    with pytest.raises(AdhelmError, match="repeated a resource day"):
        client.run_ad_report(_daily_query(identity, level="AD"), identity=identity)


def test_distinct_resources_and_distinct_days_are_not_repeats() -> None:
    client = _insights_client(
        [
            _continuing("c1", [_insight("11", "2026-09-01"), _insight("12", "2026-09-01")]),
            _insight_page(_insight("11", "2026-09-02")),
        ]
    )
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    assert [(r.resource_ref, r.dimension_value) for r in report.performance_rows] == [
        ("meta_ads.campaign.11", "2026-09-01"),
        ("meta_ads.campaign.12", "2026-09-01"),
        ("meta_ads.campaign.11", "2026-09-02"),
    ]


def test_a_report_follows_the_cursor_and_reissues_the_same_fixed_request() -> None:
    requests: list[httpx.Request] = []
    client = _insights_client(
        [_continuing("c1", [_insight("11")]), _insight_page(_insight("12"))], requests=requests
    )
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    assert len(report.performance_rows) == 2
    first, second = (r.url.params for r in requests[1:])
    assert "after" not in first
    assert second["after"] == "c1"
    for key in ("fields", "level", "time_increment", "time_range", "action_attribution_windows"):
        assert first[key] == second[key]


def test_a_report_at_its_row_bound_names_window_narrowing_as_the_remedy() -> None:
    client = _insights_client([_continuing("c1", [_insight("11"), _insight("12")])])
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            _daily_query(identity, maximum_rows=2, page_size=2), identity=identity
        )
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"
    assert "Narrow the report window" in raised.value.next_action


# ------------------------------------------------- daily boundary and freshness


def test_a_window_ending_before_the_account_day_carries_no_pending_status() -> None:
    client = _insights_client([_insight_page(_insight(day="2026-09-02"))])
    identity = _identity(client)
    report = client.run_campaign_report(_daily_query(identity), identity=identity)
    assert report.envelope.data_quality_statuses == ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"]
    assert report.performance_rows[0].data_quality_statuses == ["COMPLETE"]


def test_the_open_account_day_is_pending_while_settled_days_stay_complete() -> None:
    client = _insights_client(
        [_insight_page(_insight(day="2026-09-03"), _insight(day="2026-09-04"))],
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    identity = _identity(client)
    report = client.run_campaign_report(
        _daily_query(identity, date(2026, 9, 3), date(2026, 9, 4)), identity=identity
    )
    assert report.envelope.data_quality_statuses == [
        "COMPLETE",
        "PROCESSING_PENDING",
        "NOT_AVAILABLE_FROM_PROVIDER",
    ]
    assert report.envelope.data_fresh_through is None
    settled, open_day = report.performance_rows
    assert settled.data_quality_statuses == ["COMPLETE"]
    assert open_day.data_quality_statuses == ["PROCESSING_PENDING"]


def test_an_open_ad_day_keeps_the_creative_exposure_caveats() -> None:
    client = _insights_client([_insight_page(_insight(day="2026-09-04", ad="31"))])
    identity = _identity(client)
    report = client.run_ad_report(
        _daily_query(identity, date(2026, 9, 4), date(2026, 9, 4), level="AD"), identity=identity
    )
    assert report.performance_rows[0].data_quality_statuses == [
        "PROCESSING_PENDING",
        "CREATIVE_NOT_EXPOSED",
        "COMBINATION_UNKNOWN",
    ]


def test_the_account_calendar_day_decides_the_future_window_boundary() -> None:
    # East: 2026-09-04 23:30 UTC is already 2026-09-05 in UTC+14, so a window
    # ending on the 5th is the account's open day.  The UTC day would refuse it.
    now = datetime(2026, 9, 4, 23, 30, tzinfo=UTC)
    east = {**_account(), "timezone_name": "Pacific/Kiritimati"}
    client = _insights_client([_insight_page(_insight(day="2026-09-05"))], account=east, now=now)
    identity = _identity(client)
    report = client.run_campaign_report(
        _daily_query(identity, date(2026, 9, 5), date(2026, 9, 5)), identity=identity
    )
    assert "PROCESSING_PENDING" in report.envelope.data_quality_statuses

    # West: 2026-09-05 03:00 UTC is still 2026-09-04 20:00 in Los Angeles.  The
    # UTC day (the 5th) would allow a window ending on the 5th and would call
    # the 4th a closed day.
    west_now = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    west = {**_account(), "timezone_name": "America/Los_Angeles"}
    requests: list[httpx.Request] = []
    client = _insights_client(
        [_insight_page(_insight(day="2026-09-04"))], account=west, now=west_now, requests=requests
    )
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            _daily_query(identity, date(2026, 9, 4), date(2026, 9, 5)), identity=identity
        )
    assert raised.value.code == "INVALID_REPORT_WINDOW"
    assert len(requests) == 1
    report = client.run_campaign_report(
        _daily_query(identity, date(2026, 9, 4), date(2026, 9, 4)), identity=identity
    )
    assert "PROCESSING_PENDING" in report.envelope.data_quality_statuses
    assert "PROCESSING_PENDING" in report.performance_rows[0].data_quality_statuses


def test_the_request_asks_for_the_exact_window_and_one_day_increments() -> None:
    requests: list[httpx.Request] = []
    client = _insights_client([_insight_page()], requests=requests)
    identity = _identity(client)
    client.run_campaign_report(_daily_query(identity), identity=identity)
    params = requests[1].url.params
    assert params["time_range"] == '{"since":"2026-09-01","until":"2026-09-02"}'
    assert params["time_increment"] == "1"
    assert params["level"] == "campaign"
    assert params["action_attribution_windows"] == '["7d_click"]'

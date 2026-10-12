"""LinkedIn Ads fixed-read completeness: identity, pagination, quota, money, dates, nulls.

Every case is hermetic: `httpx.MockTransport` only, no live LinkedIn request, no
credential store, no network.  The cases pin what the fixed read promises and, as
importantly, what it refuses to promise: the Ad Analytics finder documents no
pagination, so no report here is ever marked complete.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import adhelm.inventory as inventory
from adhelm import workflows
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.linkedin_ads import (
    LINKEDIN_ADS_API_HOST,
    LINKEDIN_ADS_IDENTITY_SCOPE,
    LINKEDIN_ADS_READ_SURFACES,
    LINKEDIN_ADS_REPORTING_SCOPE,
    LinkedInAdsCampaign,
    LinkedInAdsClient,
    LinkedInAdsIdentity,
    LinkedInAdsInventoryQuery,
    LinkedInAdsReportQuery,
    classify_linkedin_ads_transport_error,
)
from adhelm.transport import SafeHttpClient

_BODY_SECRET = "operator-only-provider-explanation"
_NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)
_ACCOUNT_URN = "urn:li:sponsoredAccount:123"
Handler = Callable[[httpx.Request], httpx.Response]


def _client(
    handler: Handler,
    *,
    attempts: int = 1,
    now: Callable[[], datetime] = lambda: _NOW,
    sleeps: list[float] | None = None,
) -> LinkedInAdsClient:
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        {LINKEDIN_ADS_API_HOST},
        read_attempts=attempts,
        sleep=(sleeps.append if sleeps is not None else (lambda _seconds: None)),
    )
    return LinkedInAdsClient(
        transport, lambda: "fixture-token-not-for-output", now=now, connector_version="test"
    )


def _account(**overrides: Any) -> dict[str, Any]:
    return {"id": 123, "currency": "USD", "status": "ACTIVE", **overrides}


def _identity(client: LinkedInAdsClient) -> LinkedInAdsIdentity:
    return client.discover_identity("123", expected_account_client_ref="123")


def _day(day: int, *, month: int = 9, year: int = 2026) -> dict[str, int]:
    return {"year": year, "month": month, "day": day}


def _row(
    day: int = 1,
    *,
    pivot: str = "urn:li:sponsoredCampaign:456",
    clicks: Any = 2,
    impressions: Any = 10,
    cost: Any = "1.25",
    month: int = 9,
    year: int = 2026,
) -> dict[str, Any]:
    value = _day(day, month=month, year=year)
    return {
        "clicks": clicks,
        "impressions": impressions,
        "costInLocalCurrency": cost,
        "pivotValues": [pivot],
        "dateRange": {"start": value, "end": value},
    }


def _analytics(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"elements": list(rows), "paging": {"start": 0, "count": len(rows), "links": []}}


def _query(
    account_ref: str,
    *,
    level: str = "CAMPAIGN",
    start: date = date(2026, 9, 1),
    end: date = date(2026, 9, 2),
    maximum_rows: int = 5,
) -> LinkedInAdsReportQuery:
    return LinkedInAdsReportQuery(
        level=level,  # type: ignore[arg-type]
        account_ref=account_ref,
        report_id="report:linkedin-completeness",
        query_contract_hash="a" * 64,
        window_start=start,
        window_end=end,
        maximum_rows=maximum_rows,
        page_size=maximum_rows,
    )


def _serve(
    analytics: dict[str, Any] | Handler | None = None,
    *,
    account: dict[str, Any] | None = None,
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=account or _account())
        assert request.url.path == "/rest/adAnalytics"
        if callable(analytics):
            return analytics(request)
        return httpx.Response(200, json=analytics if analytics is not None else _analytics())

    return handler


def _report(
    analytics: dict[str, Any] | Handler | None = None,
    *,
    level: str = "CAMPAIGN",
    account: dict[str, Any] | None = None,
    **query: Any,
) -> Any:
    client = _client(_serve(analytics, account=account))
    identity = _identity(client)
    request = _query(identity.account_ref, level=level, **query)
    return (
        client.run_campaign_report(request, identity=identity)
        if level == "CAMPAIGN"
        else client.run_ad_report(request, identity=identity)
    )


def _group(identifier: int, *, account: str = _ACCOUNT_URN) -> dict[str, Any]:
    return {"account": account, "id": identifier, "status": "ACTIVE"}


def _page(elements: list[dict[str, Any]], token: str | None = None) -> httpx.Response:
    metadata = {"nextPageToken": token} if token is not None else {}
    return httpx.Response(200, json={"elements": elements, "metadata": metadata})


def _inventory_handler(pages: Callable[[httpx.Request], httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=_account())
        return pages(request)

    return handler


# ----------------------------------------------------------- version and host


def test_every_fixed_read_is_a_pinned_get_to_the_rest_host() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=_account())
        if request.url.path == "/rest/adAnalytics":
            return httpx.Response(200, json=_analytics())
        if request.url.path.endswith("/adAccounts/123/adCampaigns"):
            return _page([{**_group(22), "campaignGroup": "urn:li:sponsoredCampaignGroup:11"}])
        if request.url.path.endswith("/creatives"):
            return _page([])
        return _page([_group(11)])

    client = _client(handler)
    identity = _identity(client)
    query = LinkedInAdsInventoryQuery(account_ref=identity.account_ref)
    client.list_campaign_groups(query, identity=identity)
    campaigns = client.list_campaigns(query, identity=identity)
    client.list_creatives(query, identity=identity, campaigns=campaigns)
    client.run_campaign_report(_query(identity.account_ref), identity=identity)
    client.run_ad_report(_query(identity.account_ref, level="AD"), identity=identity)

    assert len(seen) == 6
    for request in seen:
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "api.linkedin.com"
        assert request.url.path.startswith("/rest/")
        assert request.headers["linkedin-version"] == "202608"
        assert request.headers["x-restli-protocol-version"] == "2.0.0"
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
    assert identity.provider_api_version == "202608"
    assert _report().envelope.provider_api_version == "202608"


def test_the_client_requires_its_fixed_host_and_has_no_write_surface() -> None:
    wrong = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        {"example.com"},
    )
    with pytest.raises(AdhelmError) as raised:
        LinkedInAdsClient(wrong, lambda: "token")
    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    public = {name for name in dir(LinkedInAdsClient) if not name.startswith("_")}
    assert public == {
        "discover_identity",
        "list_campaign_groups",
        "list_campaigns",
        "list_creatives",
        "run_ad_report",
        "run_campaign_report",
    }


def test_the_read_surface_table_never_claims_pagination_or_unread_fields() -> None:
    assert LINKEDIN_ADS_READ_SURFACES["ANALYTICS_PAGINATION"] == "NOT_DOCUMENTED"
    for name in ("CAMPAIGN_DELIVERY_REPORT", "CREATIVE_DELIVERY_REPORT"):
        assert LINKEDIN_ADS_READ_SURFACES[name] == "IMPLEMENTED_FIXED_READ_BOUNDED_NON_EXHAUSTIVE"
    for name in ("CAMPAIGN_BID_AND_BUDGET", "CREATIVE_CONTENT", "NATIVE_CONVERSION_METRICS"):
        assert LINKEDIN_ADS_READ_SURFACES[name] == "NOT_IN_REVIEWED_FIXED_READ"


# --------------------------------------------------------------------- identity


@pytest.mark.parametrize("configured", ["", "urn:li:sponsoredAccount:123", "12a", "-1", "１２３"])
def test_a_malformed_configured_account_never_reaches_the_wire(configured: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_account())

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity(configured)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == []


def test_an_account_beyond_the_int64_bound_is_refused_locally() -> None:
    with pytest.raises(AdhelmError) as raised:
        _client(_serve()).discover_identity("9223372036854775808")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_an_identity_for_another_account_than_requested_is_a_mismatch() -> None:
    client = _client(_serve(account=_account(id=999)))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("123")
    assert raised.value.code == "IDENTITY_MISMATCH"


def test_an_identity_that_differs_from_the_profile_expectation_is_a_mismatch() -> None:
    with pytest.raises(AdhelmError) as raised:
        _client(_serve()).discover_identity("123", expected_account_client_ref="124")
    assert raised.value.code == "IDENTITY_MISMATCH"


@pytest.mark.parametrize("currency", [None, "", "usd", "US", "USDX", 840])
def test_a_missing_or_malformed_currency_is_never_defaulted(currency: Any) -> None:
    account = {"id": 123, "currency": currency}
    with pytest.raises(AdhelmError) as raised:
        _client(_serve(account=account)).discover_identity("123")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_an_undocumented_account_field_fails_the_read_closed() -> None:
    with pytest.raises(AdhelmError) as raised:
        _client(_serve(account=_account(billingContactEmail="x"))).discover_identity("123")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_an_absent_account_status_stays_null_and_no_timezone_is_invented() -> None:
    identity = _client(_serve(account={"id": 123, "currency": "USD"})).discover_identity("123")
    assert identity.status is None
    assert identity.timezone is None
    assert identity.account_ref == "linkedin_ads.account.123"


def test_a_typed_identity_for_another_account_cannot_drive_a_report() -> None:
    client = _client(_serve())
    identity = _identity(client)
    foreign = identity.model_copy(update={"account_client_ref": "124"})
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_query(identity.account_ref), identity=foreign)
    assert raised.value.code == "IDENTITY_MISMATCH"
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_query("linkedin_ads.account.124"), identity=identity)
    assert raised.value.code == "IDENTITY_MISMATCH"


def test_a_report_method_must_match_the_query_grain() -> None:
    client = _client(_serve())
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_ad_report(_query(identity.account_ref), identity=identity)
    assert raised.value.code == "QUERY_GRAIN_MISMATCH"


# ------------------------------------------------------- quota and access refusal


def test_a_rate_limit_is_a_precondition_failure_that_discloses_the_bounded_hint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, text=_BODY_SECRET)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")
    error = raised.value
    assert error.code == "LINKEDIN_ADS_RATE_LIMITED"
    assert error.exit_code == 3
    assert error.status_code == 429
    assert error.retry_after_seconds == 30
    assert "30 second(s)" in error.next_action
    assert _BODY_SECRET not in f"{error} {error.next_action}"


@pytest.mark.parametrize("hint", [None, "soon", "-5", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_rate_limit_without_a_whole_second_hint_says_so(hint: str | None) -> None:
    headers = {} if hint is None else {"Retry-After": hint}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")
    assert raised.value.code == "LINKEDIN_ADS_RATE_LIMITED"
    assert raised.value.retry_after_seconds is None
    assert "no whole-second Retry-After hint" in raised.value.next_action


def test_a_hostile_retry_hint_is_clamped_rather_than_echoed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "999999999"})

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")
    assert raised.value.retry_after_seconds == 3_600


@pytest.mark.parametrize("surface", ["inventory", "analytics"])
def test_every_fixed_read_surface_classifies_a_rate_limit(surface: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=_account())
        return httpx.Response(429, headers={"Retry-After": "2"})

    client = _client(handler)
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        if surface == "inventory":
            client.list_campaign_groups(
                LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
        else:
            client.run_campaign_report(_query(identity.account_ref), identity=identity)
    assert raised.value.code == "LINKEDIN_ADS_RATE_LIMITED"
    assert raised.value.retry_after_seconds == 2


def test_the_shared_transport_retries_a_rate_limit_once_per_attempt_then_recovers() -> None:
    answers = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=_account()),
        ]
    )
    sleeps: list[float] = []
    identity = _client(lambda _request: next(answers), attempts=2, sleeps=sleeps).discover_identity(
        "123"
    )
    assert identity.account_client_ref == "123"
    assert sleeps == [7.0]


def test_an_exhausted_retry_budget_surfaces_the_classified_rate_limit() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    sleeps: list[float] = []
    with pytest.raises(AdhelmError) as raised:
        _client(handler, attempts=3, sleeps=sleeps).discover_identity("123")
    assert raised.value.code == "LINKEDIN_ADS_RATE_LIMITED"
    assert calls == 3 and sleeps == [1.0, 1.0]


@pytest.mark.parametrize("status", [401, 403])
def test_an_inventory_refusal_names_the_account_read_permission_and_local_status(
    status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=_account())
        return httpx.Response(status, text=_BODY_SECRET)

    client = _client(handler)
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    error = raised.value
    assert error.code == "PROVIDER_ACCESS_REQUIRED"
    assert error.exit_code == 4
    assert error.status_code == status
    assert LINKEDIN_ADS_IDENTITY_SCOPE in error.next_action
    assert "auth linkedin-token status --profile" in error.next_action
    assert _BODY_SECRET not in f"{error} {error.next_action}"


@pytest.mark.parametrize("status", [401, 403])
def test_an_analytics_refusal_names_the_reporting_permission(status: int) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=_BODY_SECRET)

    client = _client(_serve(refuse))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_query(identity.account_ref), identity=identity)
    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert LINKEDIN_ADS_REPORTING_SCOPE in raised.value.next_action
    assert _BODY_SECRET not in f"{raised.value} {raised.value.next_action}"


def test_a_recorded_scope_identity_refusal_keeps_its_lifecycle_specific_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123", granted_scopes=(LINKEDIN_ADS_IDENTITY_SCOPE,))
    assert raised.value.code == "LINKEDIN_ADS_AUTHORIZATION_REQUIRED"


def test_an_identity_refusal_without_recorded_scopes_gets_the_permission_naming_action() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=_BODY_SECRET)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")
    error = raised.value
    assert error.code == "PROVIDER_ACCESS_REQUIRED"
    assert error.exit_code == 4
    assert error.status_code == 401
    assert LINKEDIN_ADS_IDENTITY_SCOPE in error.next_action
    assert "auth linkedin-token status --profile" in error.next_action
    assert _BODY_SECRET not in f"{error} {error.next_action}"


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_an_unclassified_status_is_not_relabelled_as_quota_or_access(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=_BODY_SECRET)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")
    assert raised.value.code == "PROVIDER_REQUEST_FAILED"
    assert raised.value.status_code == status
    assert _BODY_SECRET not in str(raised.value)


def test_the_classifier_returns_a_non_http_failure_unchanged() -> None:
    original = AdhelmError("PROVIDER_UNAVAILABLE", "Provider read transport failed.")
    assert (
        classify_linkedin_ads_transport_error(original, permission=LINKEDIN_ADS_IDENTITY_SCOPE)
        is original
    )


# ------------------------------------------------------------------- pagination


def test_a_multi_page_inventory_follows_only_the_returned_cursor_in_order() -> None:
    requests: list[httpx.Request] = []

    def pages(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        token = request.url.params.get("pageToken")
        if token is None:
            return _page([_group(11), _group(12)], "next+page/1=")
        if token == "next+page/1=":
            return _page([_group(13)], "last")
        assert token == "last"
        return _page([_group(14)])

    client = _client(_inventory_handler(pages))
    identity = _identity(client)
    groups = client.list_campaign_groups(
        LinkedInAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=4, page_size=2),
        identity=identity,
    )
    assert [group.campaign_group_client_ref for group in groups] == ["11", "12", "13", "14"]
    # The last page asks only for what the bound still allows.
    assert [request.url.params["pageSize"] for request in requests] == ["2", "2", "1"]
    assert requests[1].url.query.endswith(b"&pageToken=next%2Bpage%2F1%3D")
    assert b"pageToken" not in requests[0].url.query


def test_an_inventory_that_exactly_fills_its_bound_is_complete_only_without_a_cursor() -> None:
    client = _client(_inventory_handler(lambda _request: _page([_group(11), _group(12)])))
    identity = _identity(client)
    query = LinkedInAdsInventoryQuery(
        account_ref=identity.account_ref, maximum_records=2, page_size=2
    )
    assert len(client.list_campaign_groups(query, identity=identity)) == 2

    client = _client(_inventory_handler(lambda _request: _page([_group(11), _group(12)], "more")))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(query, identity=identity)
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"


def test_an_endless_cursor_chain_stops_at_the_ten_page_limit() -> None:
    counter = iter(range(1, 1_000))

    def pages(_request: httpx.Request) -> httpx.Response:
        number = next(counter)
        return _page([_group(number)], f"token-{number}")

    client = _client(_inventory_handler(pages))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(
                account_ref=identity.account_ref, maximum_records=100, page_size=10
            ),
            identity=identity,
        )
    assert raised.value.code == "PAGINATION_LIMIT"
    assert next(counter) == 11


def test_an_empty_page_that_still_offers_a_cursor_is_invalid() -> None:
    client = _client(_inventory_handler(lambda _request: _page([], "token")))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("token", ["", "has space", "tab\there", "x" * 4097, 7, ["a"]])
def test_a_malformed_continuation_token_is_never_followed(token: Any) -> None:
    calls = 0

    def pages(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"elements": [_group(11)], "metadata": {"nextPageToken": token}}
        )

    client = _client(_inventory_handler(pages))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == 1


def test_an_inventory_page_without_its_cursor_metadata_is_invalid() -> None:
    client = _client(
        _inventory_handler(lambda _request: httpx.Response(200, json={"elements": [_group(11)]}))
    )
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_inventory_bounds_cannot_exceed_ten_pages() -> None:
    with pytest.raises(ValidationError, match="ten-page limit"):
        LinkedInAdsInventoryQuery(
            account_ref="linkedin_ads.account.123", maximum_records=101, page_size=10
        )


def test_inventory_from_another_account_or_parent_is_an_identity_mismatch() -> None:
    foreign = _client(
        _inventory_handler(
            lambda _request: _page([_group(11, account="urn:li:sponsoredAccount:999")])
        )
    )
    identity = _identity(foreign)
    with pytest.raises(AdhelmError) as raised:
        foreign.list_campaign_groups(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "IDENTITY_MISMATCH"

    def creatives(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/adCampaigns"):
            return _page([{**_group(22), "campaignGroup": "urn:li:sponsoredCampaignGroup:11"}])
        return _page(
            [
                {
                    "account": _ACCOUNT_URN,
                    "campaign": "urn:li:sponsoredCampaign:23",
                    "id": "urn:li:sponsoredCreative:33",
                    "intendedStatus": "ACTIVE",
                }
            ]
        )

    client = _client(_inventory_handler(creatives))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_creatives(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "IDENTITY_MISMATCH"


def test_creative_scoping_reuses_the_campaigns_the_caller_already_read() -> None:
    paths: list[str] = []

    def pages(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/adCampaigns"):
            return _page(
                [
                    {**_group(22), "campaignGroup": "urn:li:sponsoredCampaignGroup:11"},
                    {**_group(23), "campaignGroup": "urn:li:sponsoredCampaignGroup:11"},
                ]
            )
        urn = request.url.params["campaigns"]
        return _page(
            [
                {
                    "account": _ACCOUNT_URN,
                    "campaign": urn.removeprefix("List(").removesuffix(")"),
                    "id": "urn:li:sponsoredCreative:33",
                }
            ]
        )

    client = _client(_inventory_handler(pages))
    identity = _identity(client)
    query = LinkedInAdsInventoryQuery(account_ref=identity.account_ref)
    campaigns = client.list_campaigns(query, identity=identity)
    paths.clear()
    creatives = client.list_creatives(query, identity=identity, campaigns=campaigns[:1])
    assert [path.rsplit("/", 1)[-1] for path in paths] == ["creatives"]
    assert [creative.campaign_ref for creative in creatives] == ["linkedin_ads.campaign.22"]
    assert creatives[0].status is None

    paths.clear()
    assert client.list_creatives(query, identity=identity, campaigns=[]) == []
    assert paths == []


def test_a_supplied_campaign_whose_reference_differs_from_its_id_is_refused() -> None:
    client = _client(_inventory_handler(lambda _request: _page([])))
    identity = _identity(client)
    forged = LinkedInAdsCampaign(
        campaign_ref="linkedin_ads.campaign.22",
        campaign_client_ref="23",
        status="ACTIVE",
    )
    with pytest.raises(AdhelmError) as raised:
        client.list_creatives(
            LinkedInAdsInventoryQuery(account_ref=identity.account_ref),
            identity=identity,
            campaigns=[forged],
        )
    assert raised.value.code == "IDENTITY_MISMATCH"


def test_creatives_share_one_complete_result_bound_across_campaigns() -> None:
    creative_requests = 0

    def pages(request: httpx.Request) -> httpx.Response:
        nonlocal creative_requests
        if request.url.path.endswith("/adCampaigns"):
            return _page(
                [
                    {**_group(number), "campaignGroup": "urn:li:sponsoredCampaignGroup:11"}
                    for number in (22, 23, 24)
                ]
            )
        creative_requests += 1
        urn = request.url.params["campaigns"].removeprefix("List(").removesuffix(")")
        return _page(
            [{"account": _ACCOUNT_URN, "campaign": urn, "id": "urn:li:sponsoredCreative:33"}]
        )

    client = _client(_inventory_handler(pages))
    identity = _identity(client)
    campaigns = client.list_campaigns(
        LinkedInAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=3, page_size=3),
        identity=identity,
    )
    with pytest.raises(AdhelmError) as raised:
        client.list_creatives(
            LinkedInAdsInventoryQuery(
                account_ref=identity.account_ref, maximum_records=2, page_size=2
            ),
            identity=identity,
            campaigns=campaigns,
        )
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"
    # It refuses before asking about a campaign it has no room to retain.
    assert creative_requests == 2


# ------------------------------------------------- analytics: completeness claims


def test_an_empty_analytics_response_yields_no_rows_and_never_a_zero_row() -> None:
    report = _report(_analytics())
    assert report.performance_rows == []
    assert "COMPLETE" not in report.envelope.data_quality_statuses
    assert report.envelope.data_quality_statuses == ["NOT_AVAILABLE_FROM_PROVIDER"]
    assert report.envelope.data_fresh_through is None


def test_a_report_is_never_marked_complete_at_any_size() -> None:
    report = _report(_analytics(_row(1), _row(2)), maximum_rows=3)
    assert len(report.performance_rows) == 2
    assert "COMPLETE" not in report.envelope.data_quality_statuses
    assert report.envelope.attribution_model == "NOT_AVAILABLE_FROM_PROVIDER"
    assert report.envelope.attribution_window is None
    assert all("COMPLETE" not in row.data_quality_statuses for row in report.performance_rows)


def test_a_response_at_the_one_response_bound_is_refused_with_a_narrowing_action() -> None:
    assert len(_report(_analytics(_row(1)), maximum_rows=2).performance_rows) == 1
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(_row(1), _row(2)), maximum_rows=2)
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"
    assert "Narrow the report window" in raised.value.next_action


def test_the_report_request_never_carries_undocumented_paging_controls() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_analytics(_row(1)))

    _report(record)
    names = {part.split("=", 1)[0] for part in seen[0].url.query.decode("ascii").split("&")}
    assert names == {"q", "pivot", "timeGranularity", "dateRange", "accounts", "fields"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda page: page.update(paging={"start": 0, "count": 1, "links": [], "total": 99}),
        lambda page: page.update(total=99),
        lambda page: page.update(metadata={"nextPageToken": "x"}),
        lambda page: page.pop("paging"),
        lambda page: page.update(elements="none"),
        lambda page: page["paging"].update(count=None),
        lambda page: page["paging"].update(links="next"),
        lambda page: page["paging"].update(start=1),
    ],
)
def test_an_undocumented_or_malformed_paging_envelope_fails_the_report_closed(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    page = _analytics(_row(1))
    mutation(page)
    with pytest.raises(AdhelmError) as raised:
        _report(page)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("level", "pivot"),
    [
        ("CAMPAIGN", "urn:li:sponsoredCampaign:456"),
        ("AD", "urn:li:sponsoredCreative:789"),
    ],
)
@pytest.mark.parametrize(
    "repeat",
    [
        pytest.param({"clicks": 5}, id="same-cost-different-clicks"),
        pytest.param({"cost": "2.00"}, id="different-cost"),
        pytest.param({"cost": "2.00", "clicks": 5, "impressions": 99}, id="all-metrics-differ"),
    ],
)
def test_a_repeated_resource_day_is_refused_instead_of_double_counted(
    level: str, pivot: str, repeat: dict[str, Any]
) -> None:
    # A repeat with a different cost is not caught by the shared report validator
    # (its key includes the cost), so only the adapter check stops the double count.
    with pytest.raises(AdhelmError) as raised:
        _report(
            _analytics(_row(1, pivot=pivot), _row(1, pivot=pivot, **repeat)),
            level=level,
            maximum_rows=5,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "repeated a resource day" in str(raised.value)


def test_distinct_days_and_distinct_resources_are_not_duplicates() -> None:
    report = _report(
        _analytics(
            _row(1),
            _row(2),
            _row(1, pivot="urn:li:sponsoredCampaign:457"),
        ),
        maximum_rows=5,
    )
    assert [(row.resource_ref, row.dimension_value) for row in report.performance_rows] == [
        ("linkedin_ads.campaign.456", "2026-09-01"),
        ("linkedin_ads.campaign.456", "2026-09-02"),
        ("linkedin_ads.campaign.457", "2026-09-01"),
    ]


def test_creative_pivot_rows_stay_ad_grain_with_exposure_caveats() -> None:
    report = _report(
        _analytics(_row(1, pivot="urn:li:sponsoredCreative:789")), level="AD", maximum_rows=3
    )
    row = report.performance_rows[0]
    assert (row.resource_level, row.metric_grain) == ("AD", "AD")
    assert row.resource_ref == "linkedin_ads.creative.789"
    assert row.campaign_ref is None and row.creative_version_id is None
    assert set(row.data_quality_statuses) == {"CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"}


@pytest.mark.parametrize(
    ("level", "pivot"),
    [
        ("CAMPAIGN", "urn:li:sponsoredCreative:789"),
        ("AD", "urn:li:sponsoredCampaign:456"),
        ("CAMPAIGN", "urn:li:sponsoredAccount:123"),
        ("CAMPAIGN", "456"),
    ],
)
def test_a_pivot_of_the_wrong_resource_kind_is_refused(level: str, pivot: str) -> None:
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(_row(1, pivot=pivot)), level=level)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_row_must_carry_exactly_one_pivot_value() -> None:
    row = _row(1)
    row["pivotValues"] = ["urn:li:sponsoredCampaign:456", "urn:li:sponsoredCampaign:457"]
    with pytest.raises(AdhelmError):
        _report(_analytics(row))
    row["pivotValues"] = []
    with pytest.raises(AdhelmError):
        _report(_analytics(row))


# ------------------------------------------------ null, zero and unavailable facts


def test_zero_delivery_is_retained_as_zero_and_unrequested_metrics_stay_null() -> None:
    row = _report(_analytics(_row(1, clicks=0, impressions=0, cost="0"))).performance_rows[0]
    assert (row.impressions, row.clicks) == (0, 0)
    assert row.spend is not None and row.spend.minor_units == 0
    assert row.provider_extensions["linkedin_ads_cost_in_local_currency"] == "0"
    # The fixed request never asks for these, so they are unavailable, not zero.
    assert row.reach is None
    assert row.video_views is None
    assert row.engagements is None
    assert row.provider_reported_results is None


@pytest.mark.parametrize("field", ["clicks", "impressions", "costInLocalCurrency"])
def test_an_absent_or_null_metric_fails_the_report_instead_of_becoming_zero(field: str) -> None:
    absent = _row(1)
    del absent[field]
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(absent))
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    null = _row(1)
    null[field] = None
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(null))
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("value", [-1, 1.5, "7", True, 9_007_199_254_740_992])
def test_a_count_must_be_a_bounded_nonnegative_integer(value: Any) -> None:
    for field in ("impressions", "clicks"):
        with pytest.raises(AdhelmError) as raised:
            _report(_analytics(_row(1, **{field: value})))
        assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_an_undocumented_row_field_fails_the_report_closed() -> None:
    row = _row(1)
    row["approximateUniqueImpressions"] = 3
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(row))
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


# ---------------------------------------------------------------- money and units


@pytest.mark.parametrize(
    ("native", "minor"),
    [
        ("0", 0),
        ("0.0049", 0),
        ("0.005", 0),
        ("0.0051", 1),
        ("0.015", 2),
        ("0.025", 2),
        ("0.035", 4),
        ("12.3400", 1234),
        ("90071992547409.91", 9_007_199_254_740_991),
    ],
)
def test_usd_cost_projects_to_cents_half_even_and_retains_the_exact_decimal(
    native: str, minor: int
) -> None:
    row = _report(_analytics(_row(1, cost=native))).performance_rows[0]
    assert row.spend is not None
    assert (row.spend.minor_units, row.spend.currency) == (minor, "USD")
    assert row.provider_extensions == {
        "linkedin_ads_cost_in_local_currency": native,
        "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
    }


@pytest.mark.parametrize(
    "native",
    ["", "1,25", "1e3", "1E+2", "-1", "+1", ".5", "1.", " 1", "1 ", "１２", "NaN", "Infinity"]
    + ["90071992547409.92", "9" * 81],
)
def test_a_malformed_or_out_of_bound_cost_string_fails_closed(native: str) -> None:
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(_row(1, cost=native)))
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_non_usd_account_is_refused_rather_than_projected_with_a_guessed_exponent() -> None:
    with pytest.raises(AdhelmError) as raised:
        _report(
            _analytics(_row(1)),
            account={"id": 123, "currency": "JPY"},
        )
    assert raised.value.code == "UNSUPPORTED_CURRENCY"


def test_a_query_currency_that_differs_from_the_account_is_refused_before_egress() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_account())

    client = _client(handler)
    identity = _identity(client)
    query = _query(identity.account_ref).model_copy(update={"currency": "EUR"})
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(query, identity=identity)
    assert raised.value.code == "CURRENCY_MISMATCH"
    assert calls == ["/rest/adAccounts/123"]


# --------------------------------------------------- timezone and date boundaries


def test_a_window_ending_on_the_current_utc_day_marks_the_open_day_pending() -> None:
    report = _report(_analytics(_row(4)), start=date(2026, 9, 3), end=date(2026, 9, 4))
    assert report.envelope.data_quality_statuses == [
        "PROCESSING_PENDING",
        "NOT_AVAILABLE_FROM_PROVIDER",
    ]
    assert report.envelope.data_fresh_through is None
    assert report.envelope.window_end == date(2026, 9, 4)


def test_a_window_that_ended_before_the_current_utc_day_is_not_marked_pending() -> None:
    report = _report(_analytics(_row(3)), start=date(2026, 9, 3), end=date(2026, 9, 3))
    assert report.envelope.data_quality_statuses == ["NOT_AVAILABLE_FROM_PROVIDER"]


def test_the_utc_day_not_the_clock_offset_decides_the_boundary() -> None:
    chicago = timezone(timedelta(hours=-5))
    late_evening = datetime(2026, 9, 4, 23, 30, tzinfo=chicago)  # 2026-09-05 04:30 UTC
    handler = _serve(_analytics(_row(5)))
    client = _client(handler, now=lambda: late_evening)
    identity = _identity(client)
    today = client.run_campaign_report(
        _query(identity.account_ref, start=date(2026, 9, 5), end=date(2026, 9, 5)),
        identity=identity,
    )
    assert today.envelope.observed_at == datetime(2026, 9, 5, 4, 30, tzinfo=UTC)
    assert today.envelope.data_quality_statuses[0] == "PROCESSING_PENDING"
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(
            _query(identity.account_ref, start=date(2026, 9, 5), end=date(2026, 9, 6)),
            identity=identity,
        )
    assert raised.value.code == "INVALID_REPORT_WINDOW"


def test_an_unaware_connector_clock_is_refused() -> None:
    client = _client(_serve(), now=lambda: datetime(2026, 9, 4, 12))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.run_campaign_report(_query(identity.account_ref), identity=identity)
    assert raised.value.code == "TIME_SOURCE_INVALID"


def test_the_request_encodes_the_inclusive_utc_dates_across_a_year_boundary() -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["dateRange"])
        return httpx.Response(
            200,
            json=_analytics(_row(31, month=12, year=2025), _row(1, month=1, year=2026)),
        )

    report = _report(record, start=date(2025, 12, 31), end=date(2026, 1, 1))
    assert seen == ["(start:(year:2025,month:12,day:31),end:(year:2026,month:1,day:1))"]
    assert [row.dimension_value for row in report.performance_rows] == ["2025-12-31", "2026-01-01"]
    assert report.envelope.source_timezone == report.envelope.report_timezone == "UTC"


def test_a_leap_day_is_a_valid_row_day() -> None:
    clock = datetime(2028, 3, 5, 12, tzinfo=UTC)
    client = _client(_serve(_analytics(_row(29, month=2, year=2028))), now=lambda: clock)
    identity = _identity(client)
    report = client.run_campaign_report(
        _query(identity.account_ref, start=date(2028, 2, 28), end=date(2028, 3, 1)),
        identity=identity,
    )
    assert report.performance_rows[0].dimension_value == "2028-02-29"


def test_a_row_dated_outside_the_requested_window_is_refused() -> None:
    for day in (31, 3):
        month = 8 if day == 31 else 9
        with pytest.raises(AdhelmError) as raised:
            _report(_analytics(_row(day, month=month)))
        assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_row_that_spans_more_than_one_day_is_not_a_daily_row() -> None:
    row = _row(1)
    row["dateRange"]["end"] = _day(2)
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(row))
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "date_range",
    [
        None,
        {},
        {"start": _day(1)},
        {"start": _day(1), "end": _day(1), "timeZone": "UTC"},
        {
            "start": {"year": 2026, "month": 2, "day": 30},
            "end": {"year": 2026, "month": 2, "day": 30},
        },
        {
            "start": {"year": "2026", "month": 9, "day": 1},
            "end": {"year": 2026, "month": 9, "day": 1},
        },
        {"start": {"year": 2026, "month": 9, "day": 1, "hour": 4}, "end": _day(1)},
    ],
)
def test_a_missing_or_malformed_row_date_range_fails_closed(date_range: Any) -> None:
    row = _row(1)
    row["dateRange"] = date_range
    with pytest.raises(AdhelmError) as raised:
        _report(_analytics(row))
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_the_longest_documented_window_is_accepted_and_one_day_more_is_not() -> None:
    longest = _query("linkedin_ads.account.123", start=date(2025, 9, 3), end=date(2026, 9, 4))
    assert (longest.window_end - longest.window_start).days == 366
    with pytest.raises(ValidationError, match="at most 367 days"):
        _query("linkedin_ads.account.123", start=date(2025, 9, 2), end=date(2026, 9, 4))
    with pytest.raises(ValidationError, match="must not precede"):
        _query("linkedin_ads.account.123", start=date(2026, 9, 2), end=date(2026, 9, 1))


def test_a_report_timezone_other_than_utc_is_not_representable() -> None:
    with pytest.raises(ValidationError):
        LinkedInAdsReportQuery(
            account_ref="linkedin_ads.account.123",
            report_id="report:tz",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 1),
            source_timezone="America/Chicago",  # type: ignore[arg-type]
        )


# ------------------------------------------------------------- end-to-end surfaces

runner = CliRunner()


def _profile() -> Profile:
    return Profile(
        profile_id="linkedin_ads.read",
        provider="linkedin_ads",
        account_ref="123",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="oauth.fixture",
    )


def test_a_rate_limited_report_command_retains_nothing_and_exits_with_the_precondition_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    query_file = tmp_path / "linkedin-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "123",
                "report_id": "report.linkedin.rate-limited",
                "query_contract_hash": "d" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "level": "CAMPAIGN",
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=_account())
        return httpx.Response(429, headers={"Retry-After": "0"}, text=_BODY_SECRET)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(workflows, "credential", lambda *_a, **_k: "oauth-fixture")
    monkeypatch.setattr(workflows, "export_key", lambda: b"r" * 32)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "report",
            "linkedin-ads",
            "linkedin_ads.read",
            "--input",
            str(query_file),
        ],
    )

    assert result.exit_code == 3, result.stdout
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == "LINKEDIN_ADS_RATE_LIMITED"
    assert _BODY_SECRET not in result.stdout
    assert not [
        record
        for record in Ledger(tmp_path).records("performance")
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
    ]


def test_inventory_reads_each_campaign_set_once_and_scopes_creatives_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(profiles=[_profile().model_copy(update={"profile_id": "linkedin_ads-read"})]),
    )
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/adCampaignGroups"):
            return _page([_group(1)])
        if request.url.path.endswith("/adCampaigns"):
            return _page([{**_group(2), "campaignGroup": "urn:li:sponsoredCampaignGroup:1"}])
        return _page(
            [
                {
                    "account": _ACCOUNT_URN,
                    "campaign": "urn:li:sponsoredCampaign:2",
                    "id": "urn:li:sponsoredCreative:3",
                }
            ]
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(inventory, "credential", lambda *_a, **_k: "fixture-read-token")
    monkeypatch.setattr(inventory, "export_key", lambda: b"k" * 32)

    result = inventory.run_inventory(tmp_path, "linkedin_ads", "linkedin_ads-read")

    assert paths == ["123", "adCampaignGroups", "adCampaigns", "creatives"]
    assert result["resource_counts"]["AD"] == 1

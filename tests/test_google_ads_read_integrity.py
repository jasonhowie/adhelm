"""Fixture-only tests for Google Ads read identity, boundary and integrity behavior.

Nothing here performs a live Google Ads request.  Every case drives the fixed
v25 adapter through ``httpx.MockTransport``.  The cases cover the read-completeness
exit list for the Google Ads fixed read: identity, pagination, money in minor
units, customer time-zone and date boundaries, freshness, and the distinction
between a zero, a missing and an absent value.  They prove fixture behavior and
make no live conformance claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.google_ads import (
    GoogleAdsClient,
    GoogleAdsIdentity,
    GoogleAdsInventoryQuery,
    GoogleAdsReadConfig,
    GoogleAdsReportQuery,
)
from adhelm.reporting import NormalizedReport
from adhelm.transport import SafeHttpClient

CONFIG = GoogleAdsReadConfig(account_ref="account.google.1", customer_client_ref="1234567890")
LOGIN_CONFIG = GoogleAdsReadConfig(
    account_ref="account.google.1",
    customer_client_ref="1234567890",
    login_customer_client_ref="0987654321",
)
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    now: datetime = NOW,
    clock: Callable[[], datetime] | None = None,
) -> GoogleAdsClient:
    return GoogleAdsClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            {"googleads.googleapis.com"},
            sleep=lambda _: None,
        ),
        lambda: "access-fixture-token",
        now=clock if clock is not None else (lambda: now),
    )


def _customer(**overrides: object) -> dict[str, object]:
    customer: dict[str, object] = {"id": "1234567890", "currencyCode": "USD", "timeZone": "UTC"}
    customer.update(overrides)
    return customer


def _identity_page(*customers: dict[str, object]) -> dict[str, object]:
    return {"results": [{"customer": item} for item in customers]}


def _serve(
    pages: list[dict[str, object]],
    *,
    customer: dict[str, object] | None = None,
    bodies: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the identity query from ``customer`` and every other query from ``pages``."""
    remaining = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if bodies is not None:
            bodies.append(body)
        if "FROM customer" in body["query"]:
            return httpx.Response(200, json=_identity_page(customer or _customer()))
        assert remaining, "the adapter asked for more pages than the fixture serves"
        return httpx.Response(200, json=remaining.pop(0))

    return handler


def _day_row(
    day: str,
    *,
    campaign: str = "11",
    impressions: object = "9",
    clicks: object = "3",
    cost: object = "5000",
) -> dict[str, object]:
    return {
        "campaign": {"id": campaign},
        "segments": {"date": day},
        "metrics": {"impressions": impressions, "clicks": clicks, "costMicros": cost},
    }


def _ad_day_row(day: str, *, ad: str = "31") -> dict[str, object]:
    return {
        "campaign": {"id": "11"},
        "adGroup": {"id": "21"},
        "adGroupAd": {"ad": {"id": ad}},
        "segments": {"date": day},
        "metrics": {"impressions": "9", "clicks": "3", "costMicros": "5000"},
    }


def _report_query(
    *,
    start: date = date(2026, 9, 1),
    end: date = date(2026, 9, 1),
    timezone: str = "UTC",
    level: str = "CAMPAIGN",
    maximum_rows: int = 10_000,
) -> GoogleAdsReportQuery:
    return GoogleAdsReportQuery(
        account_ref="account.google.1",
        report_id="report.google.integrity",
        query_contract_hash="a" * 64,
        window_start=start,
        window_end=end,
        source_timezone=timezone,
        level=level,  # type: ignore[arg-type]
        maximum_rows=maximum_rows,
    )


def _report(
    pages: list[dict[str, object]],
    *,
    customer: dict[str, object] | None = None,
    now: datetime = NOW,
    **query: Any,
) -> NormalizedReport:
    query.setdefault("timezone", str((customer or {}).get("timeZone", "UTC")))
    client = _client(_serve(pages, customer=customer), now=now)
    identity = client.discover_identity(CONFIG)
    return client.run_daily_report(CONFIG, _report_query(**query), identity=identity)


def _refusal(
    pages: list[dict[str, object]], *, customer: dict[str, object] | None = None, **query: Any
) -> AdhelmError:
    with pytest.raises(AdhelmError) as raised:
        _report(pages, customer=customer, **query)
    return raised.value


def _identity_of(customer: dict[str, object]) -> GoogleAdsIdentity:
    return _client(_serve([], customer=customer)).discover_identity(CONFIG)


# ------------------------------------------------------------------- identity


def test_the_configured_login_customer_header_is_sent_only_when_configured() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=_identity_page(_customer()))

    client = _client(handler)
    client.discover_identity(LOGIN_CONFIG)
    client.discover_identity(CONFIG)
    assert seen[0]["login-customer-id"] == "0987654321"
    assert "login-customer-id" not in seen[1]
    assert all("developer-token" not in h for h in seen)


def test_a_customer_other_than_the_configured_one_is_refused_before_any_further_read() -> None:
    bodies: list[dict[str, Any]] = []
    client = _client(_serve([], customer=_customer(id="1234567891"), bodies=bodies))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity(CONFIG)
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert len(bodies) == 1


@pytest.mark.parametrize("count", [0, 2, 3, 50])
def test_an_identity_read_must_return_exactly_one_customer(count: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_identity_page(*[_customer()] * count))

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity(CONFIG)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "one customer" in raised.value.message


def test_a_continuing_identity_response_is_an_invalid_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = _identity_page(_customer())
        page["nextPageToken"] = f"t{len(requests) - 1}"
        return httpx.Response(200, json=page)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity(CONFIG)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "one customer" in raised.value.message
    assert len(requests) == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "１２３４５６７８９０"},
        {"id": 1234567890},
        {"id": None},
        {"currencyCode": "usd"},
        {"currencyCode": "US"},
        {"currencyCode": 840},
        {"timeZone": "Not/AZone"},
        {"timeZone": ""},
        {"testAccount": "true"},
        {"descriptiveName": "Do not accept this"},
    ],
)
def test_a_malformed_customer_identity_is_refused_without_echoing_the_value(
    overrides: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_identity_page(_customer(**overrides)))

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity(CONFIG)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "Do not accept this" not in raised.value.message


def test_a_test_account_flag_of_false_is_distinct_from_one_not_returned() -> None:
    assert _identity_of(_customer(testAccount=False)).test_account is False
    assert _identity_of(_customer()).test_account is None


@pytest.mark.parametrize("bad_id", ["011", "0011", "９", "9223372036854775808", "-1", "1.0", " 11"])
def test_an_entity_identifier_must_be_a_canonical_int64_string(bad_id: str) -> None:
    campaign = _campaign_inventory_row(bad_id)

    client = _client(_serve([{"results": [campaign]}]))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def _campaign_inventory_row(
    campaign_id: str = "11", **campaign: object
) -> dict[str, dict[str, object]]:
    fields: dict[str, object] = {
        "id": campaign_id,
        "status": "ENABLED",
        "advertisingChannelType": "SEARCH",
        "startDateTime": "2026-01-01 00:00:00",
        "endDateTime": "",
        "biddingStrategyType": "MANUAL_CPC",
    }
    fields.update(campaign)
    return {"campaign": fields, "campaignBudget": {"amountMicros": "5000000"}}


# ------------------------------------------------------------------ pagination


def _numbered_page(token: str | None, *, rows: int = 1, total: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {
        "results": [_day_row("2026-09-01", campaign=str(i)) for i in range(rows)]
    }
    if token is not None:
        page["nextPageToken"] = token
    if total is not None:
        page["totalResultsCount"] = total
    return page


def test_a_repeated_page_token_is_refused_as_no_safe_progress() -> None:
    error = _refusal([_numbered_page("t1"), _numbered_page("t1")])
    assert error.code == "PROVIDER_RESPONSE_INVALID"
    assert "no safe progress" in error.message


def test_a_continuing_page_with_no_rows_is_refused_as_no_safe_progress() -> None:
    error = _refusal([_numbered_page("t1"), {"results": [], "nextPageToken": "t2"}])
    assert error.code == "PROVIDER_RESPONSE_INVALID"
    assert "no safe progress" in error.message


def test_a_walk_beyond_ten_pages_fails_and_names_how_to_narrow_it() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [_numbered_page(f"t{index}") for index in range(11)]
    client = _client(_serve(pages, bodies=bodies))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.run_daily_report(CONFIG, _report_query(), identity=identity)
    assert raised.value.code == "PAGINATION_LIMIT"
    assert "shorten its window" in raised.value.next_action
    # One identity request plus exactly ten report pages; the eleventh is never asked for.
    assert len(bodies) == 11
    assert [body.get("pageToken") for body in bodies[1:]] == [None, *[f"t{i}" for i in range(9)]]


def test_a_result_beyond_the_caller_bound_is_refused_not_truncated() -> None:
    error = _refusal([_numbered_page(None, rows=3)], maximum_rows=2)
    assert error.code == "RESULT_LIMIT_REQUIRED"
    assert "shorten the report window" in error.next_action
    assert "maximum_rows" in error.next_action
    assert "never truncated" in error.next_action


def test_an_inventory_beyond_the_caller_bound_names_the_inventory_option() -> None:
    client = _client(
        _serve([{"results": [_campaign_inventory_row("11"), _campaign_inventory_row("12")]}])
    )
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG,
            GoogleAdsInventoryQuery(account_ref="account.google.1", maximum_records=1),
            identity=identity,
        )
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"
    assert "--maximum-records" in raised.value.next_action


def test_a_result_exactly_at_the_caller_bound_is_read_whole() -> None:
    report = _report([_numbered_page(None, rows=2, total="2")], maximum_rows=2)
    assert len(report.performance_rows) == 2
    assert "COMPLETE" in report.envelope.data_quality_statuses


def test_a_declared_total_that_the_walk_did_not_reach_is_refused() -> None:
    error = _refusal([_numbered_page(None, rows=2, total="3")])
    assert error.code == "PROVIDER_RESPONSE_INVALID"
    assert "incomplete" in error.message


def test_a_page_token_is_sent_back_unchanged_and_no_page_size_is_ever_sent() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [_numbered_page("opaque-token-1", total="2"), _numbered_page(None, total="2")]
    pages[1]["results"] = [_day_row("2026-09-01", campaign="99")]
    client = _client(_serve(pages, bodies=bodies))
    identity = client.discover_identity(CONFIG)
    client.run_daily_report(CONFIG, _report_query(), identity=identity)
    first, second = bodies[1], bodies[2]
    assert "pageToken" not in first
    assert second["pageToken"] == "opaque-token-1"
    assert second["query"] == first["query"]
    assert all("pageSize" not in body for body in bodies)


# --------------------------------------------------------- money in minor units


@pytest.mark.parametrize(
    ("micros", "cents"),
    [
        ("0", 0),
        ("4999", 0),
        ("5000", 0),
        ("5001", 1),
        ("14999", 1),
        ("15000", 2),
        ("25000", 2),
        ("25001", 3),
        ("9223372036854775807", 922337203685478),
    ],
)
def test_cost_micros_round_half_even_to_cents_and_keep_the_exact_source(
    micros: str, cents: int
) -> None:
    report = _report([{"results": [_day_row("2026-09-01", cost=micros)]}])
    row = report.performance_rows[0]
    assert row.spend is not None
    assert (row.spend.minor_units, row.spend.currency) == (cents, "USD")
    assert row.provider_extensions["google_ads_cost_micros"] == micros
    assert row.provider_extensions["google_ads_rounding"] == "HALF_EVEN_TO_MINOR"


@pytest.mark.parametrize(
    "cost",
    ["-5", "5.5", "1e3", "005000", "５０００", " 5000", "", "9223372036854775808", None, 5000],
)
def test_a_cost_that_is_not_a_canonical_int64_string_is_refused(cost: object) -> None:
    error = _refusal([{"results": [_day_row("2026-09-01", cost=cost)]}])
    assert error.code == "PROVIDER_RESPONSE_INVALID"


def test_a_non_usd_customer_report_is_refused_before_any_report_request() -> None:
    bodies: list[dict[str, Any]] = []
    client = _client(_serve([], customer=_customer(currencyCode="EUR"), bodies=bodies))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.run_daily_report(CONFIG, _report_query(), identity=identity)
    assert raised.value.code == "UNSUPPORTED_CURRENCY"
    assert len(bodies) == 1


def test_a_non_usd_customer_inventory_with_a_budget_is_refused_rather_than_misstated() -> None:
    client = _client(
        _serve([{"results": [_campaign_inventory_row()]}], customer=_customer(currencyCode="EUR"))
    )
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "UNSUPPORTED_CURRENCY"


def test_a_report_currency_other_than_the_customers_is_refused_before_egress() -> None:
    bodies: list[dict[str, Any]] = []
    client = _client(_serve([], bodies=bodies))
    identity = client.discover_identity(CONFIG)
    query = _report_query().model_copy(update={"currency": "EUR"})
    with pytest.raises(AdhelmError) as raised:
        client.run_daily_report(CONFIG, query, identity=identity)
    assert raised.value.code == "CURRENCY_MISMATCH"
    assert len(bodies) == 1


# ------------------------------------------------ time zone and date boundaries


def test_a_report_timezone_other_than_the_customers_is_refused_before_egress() -> None:
    bodies: list[dict[str, Any]] = []
    customer = _customer(timeZone="America/Los_Angeles")
    client = _client(_serve([], customer=customer, bodies=bodies))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.run_daily_report(CONFIG, _report_query(timezone="UTC"), identity=identity)
    assert raised.value.code == "TIMEZONE_MISMATCH"
    assert len(bodies) == 1


def test_a_customer_behind_utc_may_not_read_a_day_that_has_not_begun_for_it() -> None:
    # 2026-09-05 03:00 UTC is still 2026-09-04 20:00 in Los Angeles.
    customer = _customer(timeZone="America/Los_Angeles")
    moment = datetime(2026, 9, 5, 3, tzinfo=UTC)
    today = _report(
        [{"results": []}],
        customer=customer,
        now=moment,
        start=date(2026, 9, 4),
        end=date(2026, 9, 4),
    )
    assert "PROCESSING_PENDING" in today.envelope.data_quality_statuses
    error = _refusal(
        [{"results": []}],
        customer=customer,
        now=moment,
        start=date(2026, 9, 5),
        end=date(2026, 9, 5),
    )
    assert error.code == "INVALID_REPORT_WINDOW"


def test_a_read_that_crosses_customer_local_midnight_keeps_its_last_day_pending() -> None:
    # The window's last day was still accruing when its rows were read, even
    # though the clock has passed midnight by the time the walk ends.
    instants = iter(
        [datetime(2026, 9, 4, 23, 59, tzinfo=UTC), datetime(2026, 9, 5, 0, 1, tzinfo=UTC)]
    )
    client = _client(
        _serve([{"results": [_day_row("2026-09-04")], "totalResultsCount": "1"}]),
        clock=lambda: next(instants),
    )
    identity = client.discover_identity(CONFIG)
    report = client.run_daily_report(
        CONFIG,
        _report_query(start=date(2026, 9, 4), end=date(2026, 9, 4)),
        identity=identity,
    )
    assert report.envelope.observed_at == datetime(2026, 9, 5, 0, 1, tzinfo=UTC)
    assert "PROCESSING_PENDING" in report.envelope.data_quality_statuses
    assert report.performance_rows[0].data_quality_statuses == ["PROCESSING_PENDING"]


def test_a_customer_ahead_of_utc_may_read_its_own_current_day() -> None:
    # 2026-09-04 20:00 UTC is already 2026-09-05 08:00 in Auckland.
    customer = _customer(timeZone="Pacific/Auckland")
    moment = datetime(2026, 9, 4, 20, tzinfo=UTC)
    report = _report(
        [{"results": [_day_row("2026-09-05")]}],
        customer=customer,
        now=moment,
        start=date(2026, 9, 5),
        end=date(2026, 9, 5),
    )
    assert report.envelope.observed_at == moment
    assert report.envelope.window_end == date(2026, 9, 5)
    assert "PROCESSING_PENDING" in report.envelope.data_quality_statuses
    assert report.performance_rows[0].data_quality_statuses == ["PROCESSING_PENDING"]
    error = _refusal(
        [{"results": []}],
        customer=customer,
        now=moment,
        start=date(2026, 9, 5),
        end=date(2026, 9, 6),
    )
    assert error.code == "INVALID_REPORT_WINDOW"


def test_an_unaware_connector_clock_is_refused() -> None:
    error = _refusal([{"results": []}], now=datetime(2026, 9, 4, 12))
    assert error.code == "CLOCK_INVALID"


@pytest.mark.parametrize(
    "day",
    ["20260901", "2026-W36-2", "２０２６-０９-０１", "2026-9-1", "2026-09-01T00:00", "2026-13-01"],
)
def test_a_report_date_must_be_the_documented_calendar_spelling(day: str) -> None:
    error = _refusal([{"results": [_day_row(day)]}])
    assert error.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("day", ["2026-08-31", "2026-09-02"])
def test_a_row_outside_the_requested_window_refuses_the_whole_response(day: str) -> None:
    error = _refusal([{"results": [_day_row("2026-09-01"), _day_row(day)]}])
    assert error.code == "PROVIDER_RESPONSE_INVALID"
    assert "outside the query" in error.message


def test_a_row_with_no_date_is_refused_rather_than_placed_in_the_window() -> None:
    row = _day_row("2026-09-01")
    row["segments"] = {}
    assert _refusal([{"results": [row]}]).code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "dates",
    [
        {"startDateTime": "20260101"},
        {"startDateTime": "2026-W01-1"},
        {"endDateTime": "２０２６-０１-０１ 23:59:59"},
        {"startDateTime": "2026-03-01 00:00:00", "endDateTime": "2026-02-01 23:59:59"},
    ],
)
def test_campaign_schedule_dates_follow_the_same_spelling_and_order_rules(
    dates: dict[str, str],
) -> None:
    client = _client(_serve([{"results": [_campaign_inventory_row(**dates)]}]))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


# ------------------------------------------------------------------- freshness


def test_a_window_ending_on_the_customer_local_current_day_marks_that_day_pending() -> None:
    pages: list[dict[str, object]] = [
        {
            "results": [
                _day_row("2026-09-03"),
                _day_row("2026-09-04"),
            ],
            "totalResultsCount": "2",
        }
    ]
    report = _report(pages, start=date(2026, 9, 3), end=date(2026, 9, 4))
    assert report.envelope.data_quality_statuses == [
        "COMPLETE",
        "PROCESSING_PENDING",
        "NOT_AVAILABLE_FROM_PROVIDER",
    ]
    assert report.envelope.data_fresh_through is None
    by_day = {row.dimension_value: row.data_quality_statuses for row in report.performance_rows}
    assert by_day == {"2026-09-03": ["COMPLETE"], "2026-09-04": ["PROCESSING_PENDING"]}


def test_a_pending_window_without_a_declared_total_is_not_marked_complete() -> None:
    report = _report(
        [{"results": [_day_row("2026-09-04")]}], start=date(2026, 9, 4), end=date(2026, 9, 4)
    )
    assert report.envelope.data_quality_statuses == [
        "PROCESSING_PENDING",
        "NOT_AVAILABLE_FROM_PROVIDER",
    ]


def test_a_window_that_closed_before_the_current_day_is_not_marked_pending() -> None:
    report = _report(
        [{"results": [_day_row("2026-09-03")], "totalResultsCount": "1"}],
        start=date(2026, 9, 3),
        end=date(2026, 9, 3),
    )
    assert report.envelope.data_quality_statuses == ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"]
    assert report.performance_rows[0].data_quality_statuses == ["COMPLETE"]


def test_the_open_day_of_an_ad_report_keeps_the_unexposed_creative_status() -> None:
    report = _report(
        [{"results": [_ad_day_row("2026-09-04")]}],
        start=date(2026, 9, 4),
        end=date(2026, 9, 4),
        level="AD",
    )
    assert report.performance_rows[0].data_quality_statuses == [
        "PROCESSING_PENDING",
        "CREATIVE_NOT_EXPOSED",
    ]


# ----------------------------------------------- zero, missing and absent values


def test_a_zero_metric_row_is_retained_as_zero() -> None:
    report = _report([{"results": [_day_row("2026-09-01", impressions="0", clicks="0", cost="0")]}])
    row = report.performance_rows[0]
    assert (row.impressions, row.clicks) == (0, 0)
    assert row.spend is not None and row.spend.minor_units == 0
    assert row.provider_extensions["google_ads_cost_micros"] == "0"


@pytest.mark.parametrize("metric", ["impressions", "clicks", "costMicros"])
@pytest.mark.parametrize("value", ["missing", None])
def test_a_missing_or_null_metric_is_refused_never_read_as_zero(metric: str, value: None) -> None:
    row = _day_row("2026-09-01")
    metrics = row["metrics"]
    assert isinstance(metrics, dict)
    if value == "missing":
        del metrics[metric]
    else:
        metrics[metric] = None
    assert _refusal([{"results": [row]}]).code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("value", ["-1", "1.0", "007", "１２", "9007199254740992", 7, True])
@pytest.mark.parametrize("metric", ["impressions", "clicks"])
def test_a_count_that_is_not_a_canonical_bounded_integer_string_is_refused(
    metric: str, value: object
) -> None:
    overrides: dict[str, Any] = {metric: value}
    row = _day_row("2026-09-01", **overrides)
    assert _refusal([{"results": [row]}]).code == "PROVIDER_RESPONSE_INVALID"


def test_a_day_without_a_row_is_absent_not_zero_filled() -> None:
    report = _report(
        [{"results": [_day_row("2026-09-01"), _day_row("2026-09-03")]}],
        start=date(2026, 9, 1),
        end=date(2026, 9, 3),
    )
    assert [row.dimension_value for row in report.performance_rows] == ["2026-09-01", "2026-09-03"]


def test_an_empty_result_is_no_rows_and_is_complete_only_when_the_total_was_declared() -> None:
    declared = _report([{"totalResultsCount": "0"}])
    assert declared.performance_rows == []
    assert declared.envelope.data_quality_statuses == ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"]
    undeclared = _report([{}])
    assert undeclared.performance_rows == []
    assert undeclared.envelope.data_quality_statuses == ["NOT_AVAILABLE_FROM_PROVIDER"]


def test_a_budget_of_zero_is_retained_and_distinct_from_no_budget() -> None:
    zero = _campaign_inventory_row("11")
    zero["campaignBudget"] = {"amountMicros": "0"}
    absent = _campaign_inventory_row("12")
    del absent["campaignBudget"]
    client = _client(
        _serve(
            [
                {"results": [zero, absent], "totalResultsCount": "2"},
                {"results": []},
                {"results": []},
                {"results": []},
            ]
        )
    )
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    budgets = {item.campaign_client_ref: item.budget for item in inventory.campaigns}
    assert budgets["11"] is not None and budgets["11"].minor_units == 0
    assert budgets["12"] is None


def test_an_omitted_network_setting_is_unknown_and_an_explicit_false_is_false() -> None:
    explicit = _campaign_inventory_row(
        "11",
        networkSettings={
            "targetGoogleSearch": True,
            "targetSearchNetwork": False,
            "targetContentNetwork": False,
        },
    )
    silent = _campaign_inventory_row("12")
    client = _client(
        _serve(
            [
                {"results": [explicit, silent]},
                {"results": []},
                {"results": []},
                {"results": []},
            ]
        )
    )
    identity = client.discover_identity(CONFIG)
    inventory = client.inventory(
        CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
    )
    by_id = {item.campaign_client_ref: item for item in inventory.campaigns}
    assert by_id["11"].target_search_network is False
    assert by_id["11"].target_partner_search_network is None
    assert by_id["12"].target_google_search is None


@pytest.mark.parametrize("amount", ["005000000", "５００００００", "-5000000", "5000000.0"])
def test_a_budget_that_is_not_a_canonical_int64_string_is_refused(amount: str) -> None:
    row = _campaign_inventory_row()
    row["campaignBudget"] = {"amountMicros": amount}
    client = _client(_serve([{"results": [row]}]))
    identity = client.discover_identity(CONFIG)
    with pytest.raises(AdhelmError) as raised:
        client.inventory(
            CONFIG, GoogleAdsInventoryQuery(account_ref="account.google.1"), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


# ------------------------------------------------------------ repeated resources


def test_one_campaign_day_returned_twice_in_a_page_is_refused() -> None:
    error = _refusal([{"results": [_day_row("2026-09-01"), _day_row("2026-09-01", cost="7000")]}])
    assert error.code == "PROVIDER_RESPONSE_INVALID"
    assert "repeated a resource day" in error.message


def test_one_campaign_day_returned_on_two_pages_is_refused() -> None:
    error = _refusal(
        [
            {"results": [_day_row("2026-09-01")], "nextPageToken": "next"},
            {"results": [_day_row("2026-09-01")]},
        ]
    )
    assert error.code == "PROVIDER_RESPONSE_INVALID"
    assert "repeated a resource day" in error.message


def test_one_ad_day_returned_twice_is_refused_but_distinct_ads_and_days_are_not() -> None:
    error = _refusal(
        [{"results": [_ad_day_row("2026-09-01"), _ad_day_row("2026-09-01")]}], level="AD"
    )
    assert "repeated a resource day" in error.message
    report = _report(
        [
            {
                "results": [
                    _ad_day_row("2026-09-01", ad="31"),
                    _ad_day_row("2026-09-01", ad="32"),
                    _ad_day_row("2026-09-02", ad="31"),
                ]
            }
        ],
        start=date(2026, 9, 1),
        end=date(2026, 9, 2),
        level="AD",
    )
    assert len(report.performance_rows) == 3

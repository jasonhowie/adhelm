"""Reddit Ads read completeness: continuation, quota, denial, repeats and retention.

Fixture-only: every case drives the real adapter through `httpx.MockTransport`.
No Reddit request is made.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import adhelm.inventory as inventory
from adhelm.audits.reddit_ads import _DIMENSION_SURFACES
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.reddit_ads import (
    REDDIT_ADS_READ_SURFACES,
    RedditAdsClient,
    RedditAdsInventoryQuery,
    RedditAdsReportQuery,
)
from adhelm.transport import SafeHttpClient

ACCOUNT = "a2_fixture"
Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, *, attempts: int = 1) -> RedditAdsClient:
    return RedditAdsClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            {"ads-api.reddit.com"},
            read_attempts=attempts,
            sleep=lambda _seconds: None,
        ),
        lambda: "fixture-token",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        connector_version="test",
    )


def _identity_json() -> dict[str, Any]:
    return {"data": {"id": ACCOUNT, "currency": "USD", "time_zone_id": "UTC"}}


def _report_query(account_ref: str, **updates: Any) -> RedditAdsReportQuery:
    values: dict[str, Any] = {
        "account_ref": account_ref,
        "report_id": "report.one",
        "query_contract_hash": "a" * 64,
        "window_start": date(2026, 9, 1),
        "window_end": date(2026, 9, 2),
    }
    values.update(updates)
    return RedditAdsReportQuery(**values)


def _row(day: str, campaign: str = "c1", **updates: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "campaign_id": campaign,
        "date": day,
        "impressions": 10,
        "clicks": 1,
        "spend": 1_000_000,
    }
    row.update(updates)
    return row


def _reporting(rows: list[dict[str, Any]], **extra: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(200, json=_identity_json())
        return httpx.Response(200, json={"data": {"metrics": rows}, **extra})

    return handler


# --- report continuation -----------------------------------------------------


@pytest.mark.parametrize(
    "pagination",
    [
        {"next_url": "https://ads-api.reddit.com/api/v3/ad_accounts/a2_fixture/reports?x=1"},
        {"next_url": "https://elsewhere.example/reports?x=1"},
        {"next_url": "relative/path"},
        {"next_url": ""},
    ],
)
def test_any_signalled_report_continuation_fails_closed_with_an_actionable_diagnostic(
    pagination: dict[str, str],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _reporting([_row("2026-09-01")], pagination=pagination)(request)

    client = _client(handler)
    identity = client.discover_identity(ACCOUNT)
    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(_report_query(identity.account_ref), identity=identity)

    assert error.value.code == "REPORT_PAGINATION_UNVERIFIED"
    # The diagnostic tells the agent how to get one response; it does not
    # return the first page as if it were the report.
    assert "Narrow the report window" in error.value.next_action
    # Identity plus exactly one report request: no continuation was guessed,
    # whatever host or shape the provider's URL had.
    assert [request.method for request in requests] == ["GET", "POST"]


def test_an_absent_or_empty_report_pagination_object_is_one_complete_response() -> None:
    for extra in ({}, {"pagination": {}}, {"pagination": {"next_url": None}}):
        client = _client(_reporting([_row("2026-09-01")], **extra))
        identity = client.discover_identity(ACCOUNT)
        report = client.run_campaign_report(
            _report_query(identity.account_ref, window_end=date(2026, 9, 1)), identity=identity
        )
        assert len(report.performance_rows) == 1


def test_the_report_is_never_labeled_fresh_complete_or_attributed() -> None:
    client = _client(_reporting([_row("2026-09-01")]))
    identity = client.discover_identity(ACCOUNT)

    report = client.run_campaign_report(
        _report_query(identity.account_ref, window_end=date(2026, 9, 1)), identity=identity
    )

    envelope = report.envelope
    assert envelope.data_fresh_through is None
    assert envelope.attribution_model == "NOT_AVAILABLE_FROM_PROVIDER"
    assert "PROCESSING_PENDING" in envelope.data_quality_statuses
    assert "COMPLETE" not in envelope.data_quality_statuses
    assert envelope.source_timezone == envelope.report_timezone == "UTC"


# --- repeated rows ------------------------------------------------------------


def test_a_repeated_resource_and_day_never_reaches_a_total() -> None:
    client = _client(_reporting([_row("2026-09-01"), _row("2026-09-01")]))
    identity = client.discover_identity(ACCOUNT)

    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(_report_query(identity.account_ref), identity=identity)

    assert error.value.code == "PROVIDER_RESPONSE_INVALID"


def test_distinct_resource_and_day_pairs_are_all_kept() -> None:
    rows = [_row("2026-09-01"), _row("2026-09-02"), _row("2026-09-01", campaign="c2")]
    client = _client(_reporting(rows))
    identity = client.discover_identity(ACCOUNT)

    report = client.run_campaign_report(_report_query(identity.account_ref), identity=identity)

    assert len(report.performance_rows) == 3


def test_a_row_outside_the_requested_window_is_refused() -> None:
    client = _client(_reporting([_row("2026-09-03")]))
    identity = client.discover_identity(ACCOUNT)

    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(_report_query(identity.account_ref), identity=identity)

    assert error.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("field", ["impressions", "clicks", "spend"])
@pytest.mark.parametrize("value", [None, -1, 1.5, "7", True])
def test_a_missing_or_malformed_metric_is_refused_rather_than_read_as_zero(
    field: str, value: object
) -> None:
    row = _row("2026-09-01", **{field: value})
    client = _client(_reporting([row]))
    identity = client.discover_identity(ACCOUNT)

    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(
            _report_query(identity.account_ref, window_end=date(2026, 9, 1)), identity=identity
        )

    assert error.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_zero_metric_is_a_real_zero() -> None:
    row = _row("2026-09-01", impressions=0, clicks=0, spend=0)
    client = _client(_reporting([row]))
    identity = client.discover_identity(ACCOUNT)

    report = client.run_campaign_report(
        _report_query(identity.account_ref, window_end=date(2026, 9, 1)), identity=identity
    )

    result = report.performance_rows[0]
    assert (result.impressions, result.clicks) == (0, 0)
    assert result.spend is not None and result.spend.minor_units == 0
    assert result.provider_extensions["reddit_ads_spend_micros"] == "0"


# --- quota and denial ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "recovery"),
    [(401, "auth reddit-token refresh"), (403, "a refresh will not change this")],
)
def test_a_denial_keeps_its_code_and_names_the_recovery_that_fits_its_status(
    status: int, recovery: str
) -> None:
    client = _client(lambda _r: httpx.Response(status, json={"message": "no"}))

    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ACCOUNT)

    assert error.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert error.value.status_code == status
    assert error.value.exit_code == 3
    assert recovery in error.value.next_action
    if status == 401:
        # A 401 must not send an EXPLICIT_CI profile into a refresh that always
        # fails for it: the CI clause names who owns that token's replacement.
        assert "OS_STORE profile enrolled with `auth reddit-token enroll`" in (
            error.value.next_action
        )
        assert "EXPLICIT_CI token" in error.value.next_action
        assert "must replace it" in error.value.next_action


def test_a_report_denial_is_explained_like_an_identity_denial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(200, json=_identity_json())
        return httpx.Response(401)

    client = _client(handler)
    identity = client.discover_identity(ACCOUNT)

    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(_report_query(identity.account_ref), identity=identity)

    assert error.value.status_code == 401
    assert "auth reddit-token refresh" in error.value.next_action


def test_an_error_that_is_not_a_denial_passes_through_unchanged() -> None:
    client = _client(lambda _r: httpx.Response(404))

    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ACCOUNT)

    assert error.value.code == "PROVIDER_REQUEST_FAILED"
    assert error.value.status_code == 404
    assert "reddit-token" not in error.value.next_action


def test_a_transport_failure_keeps_its_exception_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)

    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ACCOUNT)

    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert isinstance(error.value.__cause__, httpx.ConnectError)


def test_a_report_transport_failure_keeps_its_exception_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(200, json=_identity_json())
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    identity = client.discover_identity(ACCOUNT)

    with pytest.raises(AdhelmError) as error:
        client.run_campaign_report(_report_query(identity.account_ref), identity=identity)

    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert isinstance(error.value.__cause__, httpx.ConnectError)


def test_a_replaced_denial_drops_the_original_chain() -> None:
    client = _client(lambda _r: httpx.Response(401))

    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ACCOUNT)

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_a_rate_limit_is_retried_within_the_bound_and_then_reaches_the_caller() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "7"}, json={"message": "slow down"})

    client = _client(handler, attempts=3)

    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ACCOUNT)

    assert calls == 3
    assert error.value.code == "PROVIDER_REQUEST_FAILED"
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 7


def test_a_rate_limit_that_clears_inside_the_bound_is_a_successful_read() -> None:
    responses = iter([httpx.Response(429), httpx.Response(200, json=_identity_json())])
    client = _client(lambda _r: next(responses), attempts=3)

    assert client.discover_identity(ACCOUNT).account_client_ref == ACCOUNT


def test_a_report_read_is_a_non_mutating_post_and_retries_like_a_read() -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(200, json=_identity_json())
        posts += 1
        if posts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"metrics": [_row("2026-09-01")]}})

    client = _client(handler, attempts=3)
    identity = client.discover_identity(ACCOUNT)

    report = client.run_campaign_report(
        _report_query(identity.account_ref, window_end=date(2026, 9, 1)), identity=identity
    )

    assert posts == 2
    assert len(report.performance_rows) == 1


def test_a_redirect_from_the_api_host_is_never_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    client = _client(handler)

    with pytest.raises(AdhelmError) as error:
        client.discover_identity(ACCOUNT)

    assert error.value.code == "UNAPPROVED_EGRESS"
    assert len(seen) == 1


# --- list pagination ----------------------------------------------------------


def _list_handler(pages: dict[str, dict[str, Any]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(200, json=_identity_json())
        key = request.url.query.decode() if request.url.query else ""
        return httpx.Response(200, json=pages[key])

    return handler


def _campaign(identifier: str) -> dict[str, Any]:
    return {"id": identifier, "ad_account_id": ACCOUNT, "effective_status": "ACTIVE"}


def test_a_list_walk_follows_the_provider_cursor_to_the_end() -> None:
    base = f"https://ads-api.reddit.com/api/v3/ad_accounts/{ACCOUNT}/campaigns"
    pages = {
        "": {"data": [_campaign("c1")], "pagination": {"next_url": f"{base}?page.token=two"}},
        "page.token=two": {"data": [_campaign("c2")], "pagination": {"next_url": None}},
    }
    client = _client(_list_handler(pages))
    identity = client.discover_identity(ACCOUNT)

    campaigns = client.list_campaigns(
        RedditAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )

    assert [item.campaign_client_ref for item in campaigns] == ["c1", "c2"]


def test_a_list_that_cannot_finish_inside_the_bound_fails_instead_of_truncating() -> None:
    base = f"https://ads-api.reddit.com/api/v3/ad_accounts/{ACCOUNT}/campaigns"
    pages = {
        "": {"data": [_campaign("c1")], "pagination": {"next_url": f"{base}?page.token=two"}},
        "page.token=two": {"data": [_campaign("c2")], "pagination": {}},
    }
    client = _client(_list_handler(pages))
    identity = client.discover_identity(ACCOUNT)

    with pytest.raises(AdhelmError) as error:
        client.list_campaigns(
            RedditAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=1),
            identity=identity,
        )

    assert error.value.code == "RESULT_LIMIT_REQUIRED"


# --- the read-surface declaration --------------------------------------------


def test_every_surface_the_audit_depends_on_is_declared() -> None:
    declared = set(REDDIT_ADS_READ_SURFACES)
    assert {
        surface for surfaces in _DIMENSION_SURFACES.values() for surface in surfaces
    } <= declared


def test_no_surface_claims_more_than_the_adapter_returns() -> None:
    returned = {
        "ACCOUNT_IDENTITY",
        "ACCOUNT_TIMEZONE",
        "CAMPAIGN_INVENTORY",
        "CAMPAIGN_OBJECTIVE",
        "AD_GROUP_INVENTORY",
        "AD_INVENTORY",
        "CAMPAIGN_DELIVERY_REPORT",
        "AD_DELIVERY_REPORT",
    }
    implemented = {
        name
        for name, status in REDDIT_ADS_READ_SURFACES.items()
        if status.startswith("IMPLEMENTED")
    }
    assert implemented == returned


# --- retention ----------------------------------------------------------------


def test_inventory_retains_status_objective_and_parents_but_never_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter accepts `name` so the read does not fail; it must never be kept."""
    save_configuration(
        tmp_path,
        Configuration(
            profiles=[
                Profile(
                    profile_id="reddit_ads-read",
                    provider="reddit_ads",
                    account_ref=ACCOUNT,
                    principal_ref="principal.fixture",
                    client_ref="client.fixture",
                    scopes=["adsread"],
                    purpose="READ",
                    currency="USD",
                    timezone="UTC",
                    credential_ref="fixture-read-token",
                )
            ]
        ),
    )
    secret = "Confidential Launch Campaign Name"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": ACCOUNT,
                        "currency": "USD",
                        "time_zone_id": "UTC",
                        "name": secret,
                        "business_id": "b1",
                    }
                },
            )
        named = {"name": secret, "ad_account_id": ACCOUNT, "effective_status": "ACTIVE"}
        if request.url.path.endswith("/campaigns"):
            item = {"id": "c1", "objective": "TRAFFIC", **named}
        elif request.url.path.endswith("/ad_groups"):
            item = {"id": "g1", "campaign_id": "c1", **named}
        else:
            item = {"id": "ad1", "campaign_id": "c1", "ad_group_id": "g1", **named}
        return httpx.Response(200, json={"data": [item], "pagination": {}})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(inventory, "credential", lambda *_a, **_k: "fixture-read-token")
    monkeypatch.setattr(inventory, "export_key", lambda: b"k" * 32)

    inventory.run_inventory(tmp_path, "reddit_ads", "reddit_ads-read")

    records = Ledger(tmp_path).records("changes")
    assert secret not in json.dumps(records)
    snapshots = {
        record["payload"]["resource_type"]: record["payload"]
        for record in records
        if record["record_kind"] == "RESOURCE_SNAPSHOT"
    }
    assert snapshots["CAMPAIGN"]["normalized_state"] == {
        "status": "ACTIVE",
        "objective": "TRAFFIC",
        "currency": "USD",
    }
    assert snapshots["AD"]["parent_resource_refs"] == [
        "reddit_ads.campaign.c1",
        "reddit_ads.ad_group.g1",
    ]

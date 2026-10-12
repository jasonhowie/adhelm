# ruff: noqa: E501
"""`pilot run` for every released provider, end to end, against fake providers.

`test_pilot_run.py` proves the command's rules on Reddit, GA4 and GTM. This module
runs the other five advertising providers through the same command, each with the
profile and credential type it really uses (a Google OAuth grant, a Meta account
token bundle, an OpenAI account key), and then holds all eight to the same guarantees.
Every request is served by `httpx.MockTransport`; every credential and the Meta
protected bundle are fixtures. Nothing reaches a provider, the operating-system
credential store or owner data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_ga4_data_aggregates import _ga4_profile
from test_meta_token_read_workflows import _INSPECTOR, _NOW, _SUBJECT, _Runtime
from test_pilot_run import (
    _COMMIT,
    _KEY,
    _RUNNER,
    _TOKEN,
    Environment,
    _edit_report_input,
    _forbidden_strings,
    _install,
    _record,
    _untouched,
    _write_inputs,
    forbid_provider_access,
    ga4,  # noqa: F401  (fixture)
    gtm,  # noqa: F401  (fixture)
    reddit,  # noqa: F401  (fixture)
)

import adhelm.credentials
import adhelm.inventory as inventory
import adhelm.pilot_workflow as pilot_workflow
from adhelm import _meta_ads_token_session as meta_sessions
from adhelm import meta_token_credentials, meta_token_inspection, workflows
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Configuration, OpenAIAccountKeyProfile, Profile
from adhelm.pilot_records import RELEASE_PROVIDERS, PilotRecord, pilot_steps
from adhelm.strict_json import validate_json_input

_REAL_CLIENT = httpx.Client
_ADS = (
    "google_ads",
    "meta_ads",
    "linkedin_ads",
    "tiktok_ads",
    "openai_ads",
)


def _profile(
    provider: str, account: str, *, scopes: list[str] | None = None, **extra: Any
) -> Profile:
    return Profile(
        profile_id=f"{provider}-read",
        provider=provider,  # type: ignore[arg-type]
        account_ref=account,
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=scopes or ["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="pilot-fixture-credential",
        **extra,
    )


def _prepare(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    profile: Any,
    query: dict[str, Any],
    handler: Callable[[httpx.Request], httpx.Response],
    identifiers: list[str],
) -> Environment:
    save_configuration(root, Configuration(profiles=[profile, _ga4_profile()]))
    options = _write_inputs(root, query)
    environment = Environment(root, provider, profile.profile_id, options)
    environment.identifiers = identifiers
    _install(monkeypatch, environment, handler)
    return environment


def _report_query(account: str, **extra: Any) -> dict[str, Any]:
    return {
        "account_ref": account,
        "report_id": "report.pilot.campaign",
        "query_contract_hash": "e" * 64,
        "source_timezone": "UTC",
        "currency": "USD",
        "level": "CAMPAIGN",
        **extra,
    }


# --- Google Ads: OAuth token only (developer tokens were retired 2026-09-09) ------------------------------


def _google_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v25/customers/1234567890/googleAds:search"
    query = json.loads(request.content)["query"]
    if "customer.currency_code" in query:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"customer": {"id": "1234567890", "currencyCode": "USD", "timeZone": "UTC"}}
                ]
            },
        )
    if "campaign_budget.amount_micros" in query:
        return httpx.Response(
            200,
            json={
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
                ]
            },
        )
    if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
        return httpx.Response(200, json={"results": []})
    if "ad_group.cpc_bid_micros" in query:
        return httpx.Response(
            200,
            json={
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
                ]
            },
        )
    if "ad_group_ad.ad.type" in query:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "adGroup": {"id": "21"},
                        "adGroupAd": {
                            "ad": {"id": "31", "type": "RESPONSIVE_SEARCH_AD"},
                            "status": "ENABLED",
                        },
                    }
                ]
            },
        )
    assert "FROM campaign" in query and "metrics.cost_micros" in query
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "campaign": {"id": "11"},
                    "segments": {"date": "2026-09-01"},
                    "metrics": {"impressions": "5", "clicks": "2", "costMicros": "5001"},
                }
            ]
        },
    )


@pytest.fixture
def google_ads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    profile = _profile(
        "google_ads",
        "1234567890",
        login_customer_ref="0987654321",
    )
    return _prepare(
        tmp_path,
        monkeypatch,
        "google_ads",
        profile,
        _report_query("1234567890"),
        _google_handler,
        ["1234567890", "0987654321"],
    )


# --- LinkedIn Ads ------------------------------------------------------------------------------


def _linkedin_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/rest/adAccounts/456":
        return httpx.Response(200, json={"id": 456, "currency": "USD", "status": "ACTIVE"})
    if path.endswith("/adCampaignGroups"):
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"account": "urn:li:sponsoredAccount:456", "id": 1, "status": "ACTIVE"}
                ],
                "metadata": {},
            },
        )
    if path.endswith("/adCampaigns"):
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "account": "urn:li:sponsoredAccount:456",
                        "id": 2,
                        "campaignGroup": "urn:li:sponsoredCampaignGroup:1",
                        "status": "ACTIVE",
                        "objectiveType": "WEBSITE_VISITS",
                    }
                ],
                "metadata": {},
            },
        )
    if path.endswith("/creatives"):
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "account": "urn:li:sponsoredAccount:456",
                        "campaign": "urn:li:sponsoredCampaign:2",
                        "id": "urn:li:sponsoredCreative:3",
                        "intendedStatus": "ACTIVE",
                    }
                ],
                "metadata": {},
            },
        )
    assert path == "/rest/adAnalytics"
    day = {"year": 2026, "month": 9, "day": 1}
    return httpx.Response(
        200,
        json={
            "elements": [
                {
                    "clicks": 2,
                    "impressions": 10,
                    "costInLocalCurrency": "1.25",
                    "pivotValues": ["urn:li:sponsoredCampaign:2"],
                    "dateRange": {"start": day, "end": day},
                }
            ],
            "paging": {"start": 0, "count": 1, "links": []},
        },
    )


@pytest.fixture
def linkedin_ads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    return _prepare(
        tmp_path,
        monkeypatch,
        "linkedin_ads",
        _profile("linkedin_ads", "456"),
        _report_query("456"),
        _linkedin_handler,
        ["456", "urn:li:sponsoredAccount", "WEBSITE_VISITS"],
    )


# --- TikTok Ads ------------------------------------------------------------------------------------


def _tiktok_page(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "code": 0,
        "message": "OK",
        "request_id": "fixture-request",
        "data": {
            "list": items,
            "page_info": {"page": 1, "page_size": 500, "total_number": len(items), "total_page": 1},
        },
    }


def _tiktok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/advertiser/info/"):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "request_id": "fixture-request",
                "data": {
                    "list": [
                        {
                            "advertiser_id": "123",
                            "currency": "USD",
                            "timezone": "UTC",
                            "status": "STATUS_ENABLE",
                        }
                    ]
                },
            },
        )
    if path.endswith("/campaign/get/"):
        return httpx.Response(
            200,
            json=_tiktok_page(
                [
                    {
                        "campaign_id": "11",
                        "operation_status": "ENABLE",
                        "objective_type": "TRAFFIC",
                        "budget_mode": "BUDGET_MODE_DAY",
                        "budget": "5.00",
                    }
                ]
            ),
        )
    if path.endswith("/adgroup/get/"):
        return httpx.Response(
            200,
            json=_tiktok_page(
                [
                    {
                        "adgroup_id": "12",
                        "campaign_id": "11",
                        "operation_status": "ENABLE",
                        "budget_mode": "BUDGET_MODE_INFINITE",
                        "budget": None,
                        "bid_type": "BID_TYPE_NO_BID",
                        "bid_price": None,
                    }
                ]
            ),
        )
    if path.endswith("/ad/get/"):
        return httpx.Response(
            200,
            json=_tiktok_page(
                [
                    {
                        "ad_id": "13",
                        "adgroup_id": "12",
                        "campaign_id": "11",
                        "operation_status": "ENABLE",
                    }
                ]
            ),
        )
    assert path.endswith("/report/integrated/get/")
    return httpx.Response(
        200,
        content=(
            b'{"code":0,"message":"OK","request_id":"fixture-request","data":'
            b'{"list":[{"dimensions":{"stat_time_day":"2026-09-01","campaign_id":"11"},'
            b'"metrics":{"impressions":"4","clicks":"1","spend":0.75}}],'
            b'"page_info":{"page":1,"page_size":500,"total_number":1,"total_page":1}}}'
        ),
    )


@pytest.fixture
def tiktok_ads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    return _prepare(
        tmp_path,
        monkeypatch,
        "tiktok_ads",
        _profile("tiktok_ads", "123"),
        _report_query("123"),
        _tiktok_handler,
        ["123"],
    )


# --- OpenAI Ads: an account-key profile -----------------------------------------------------------


def _openai_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/ad_account":
        return httpx.Response(
            200,
            json={
                "id": "account_fixture",
                "name": "ignored locally",
                "status": "active",
                "timezone": "UTC",
                "currency_code": "USD",
            },
        )
    if path == "/v1/ad_account/insights":
        return httpx.Response(
            200,
            json={
                "object": "list",
                "count": 1,
                "data": [
                    {
                        "id": "row_fixture",
                        "readable_time": "2026-09-01",
                        "start_time": 1788220800,
                        "end_time": 1788307200,
                        "campaign_id": "campaign_fixture",
                        "impressions": 2,
                        "clicks": 1,
                        "spend": 1.25,
                    }
                ],
                "has_more": False,
                "first_id": "row_fixture",
                "last_id": "row_fixture",
            },
        )
    if path == "/v1/ad_groups":
        data = {
            "id": "group_fixture",
            "status": "paused",
            "bidding_config": {"billing_event_type": "impression", "max_bid_micros": 125_000},
        }
    elif path == "/v1/ads":
        data = {
            "id": "ad_fixture",
            "status": "paused",
            "creative": {"type": "chat_card", "title": "not persisted", "file_id": "file_fixture"},
        }
    else:
        assert path == "/v1/campaigns"
        data = {
            "id": "campaign_fixture",
            "created_at": 1_788_260_800,
            "status": "paused",
            "bidding_type": "impressions",
            "budget": {},
            "conversion_event_setting_ids": [],
            "description": "not persisted",
            "end_time": None,
            "mode": None,
            "name": "not persisted",
            "start_time": 1_788_260_800,
            "targeting": {},
            "updated_at": 1_788_260_800,
        }
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [data],
            "first_id": data["id"],
            "last_id": data["id"],
            "has_more": False,
        },
    )


@pytest.fixture
def openai_ads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    profile = OpenAIAccountKeyProfile(
        profile_kind="OPENAI_ADS_ACCOUNT_KEY",
        profile_id="openai_ads-read",
        account_ref="account_fixture",
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="pilot-fixture-key",
    )
    return _prepare(
        tmp_path,
        monkeypatch,
        "openai_ads",
        profile,
        _report_query("account_fixture"),
        _openai_handler,
        ["account_fixture", "campaign_fixture", "group_fixture", "ad_fixture", "file_fixture"],
    )


# --- Meta Ads: a protected account-token bundle, not the generic credential path -------------------


@pytest.fixture
def meta_ads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    runtime = _Runtime(tmp_path)
    environment = Environment(tmp_path, "meta_ads", "meta-reader", [])
    environment.identifiers = ["12345", "67890", "99999", "meta-reader", _SUBJECT, _INSPECTOR]

    def serve(request: httpx.Request) -> httpx.Response:
        environment.requests.append(request)
        return runtime.handle(request)

    monkeypatch.setattr(meta_token_credentials, "_credential_backend", lambda: runtime.store)
    monkeypatch.setattr(meta_token_inspection, "_now", lambda: runtime.now)
    monkeypatch.setattr(meta_sessions, "_now", lambda: runtime.now)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_CLIENT(transport=httpx.MockTransport(serve), **kwargs),
    )

    def generic_credential(*_args: object, **_kwargs: object) -> str:
        pytest.fail("the generic credential resolver must not see a Meta token bundle")

    for module in (inventory, workflows):
        monkeypatch.setattr(module, "export_key", lambda: _KEY)
        monkeypatch.setattr(module, "credential", generic_credential)
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)
    enroll_meta_token_profile(
        tmp_path,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "profile_id": "meta-reader",
                "account_ref": "12345",
                "client_application_ref": "67890",
                "currency": "USD",
                "timezone": "UTC",
                "purpose": "READ",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    configuration = load_configuration(tmp_path)
    save_configuration(
        tmp_path,
        Configuration(profiles=[*configuration.profiles, _ga4_profile()]),
        replace=True,
    )
    environment.options = _write_inputs(
        tmp_path,
        _report_query("12345", action_attribution_windows=["7d_click", "1d_view"]),
        # The fixture's clock is 2026-09-06, and a report window may not end after today.
        ("2026-08-31", "2026-09-06"),
    )
    assert runtime.now == _NOW
    return environment


# --- all eight providers ---------------------------------------------------------------------------------


#: The core pilot tests name their Reddit fixture for the provider's short name.
_FIXTURE = {"reddit_ads": "reddit"}


def _environment(request: pytest.FixtureRequest, provider: str) -> Environment:
    environment: Environment = request.getfixturevalue(_FIXTURE.get(provider, provider))
    return environment


@pytest.fixture(params=RELEASE_PROVIDERS)
def pilot(request: pytest.FixtureRequest) -> Environment:
    return _environment(request, request.param)


def test_every_released_provider_is_covered_here_or_in_the_core_pilot_tests() -> None:
    assert set(RELEASE_PROVIDERS) == {"reddit_ads", "ga4", "gtm", *_ADS}


def test_a_pilot_runs_every_step_for_each_provider_and_writes_one_schema_valid_record(
    pilot: Environment,
) -> None:
    code, envelope = pilot.run()

    assert code == 0, envelope
    assert envelope["outcome"] == "PILOT_RECORDED"
    record = _record(pilot)
    assert record.provider == pilot.provider
    assert [(step.step, step.command) for step in record.steps] == list(pilot_steps(pilot.provider))
    assert record.audit_coverage
    assert (record.report_window is None) == (pilot.provider == "gtm")
    assert record.ui_comparison == "MATCH" and record.source_commit == _COMMIT
    # The file validates against the installed schema and nothing else is in it.
    validate_json_input(PilotRecord, pilot.output.read_bytes())
    assert set(json.loads(pilot.output.read_text())) == set(PilotRecord.model_fields)
    assert envelope["provider_labels"][0]["provider"] == pilot.provider
    assert envelope["data"]["remote_mutations"] is False


def _without_hex(text: str) -> str:
    return re.sub(r"[0-9a-f]{40,}", "", text)


def test_the_record_and_the_output_hold_no_identifier_name_token_or_payload_for_each_provider(
    pilot: Environment,
) -> None:
    result = _RUNNER.invoke(app, pilot.arguments())
    assert result.exit_code == 0, result.output
    text = pilot.output.read_text()
    shown = result.output.replace(str(pilot.output), "")
    forbidden = [
        _TOKEN,
        "principal.fixture",
        "client.fixture",
        "pilot-fixture-credential",
        "not persisted",
        str(Path.home()),
        str(pilot.root),
        f"{pilot.provider}-read",
        *pilot.identifiers,
        *(_forbidden_strings(pilot) if pilot.provider in {"reddit_ads", "ga4", "gtm"} else []),
    ]
    for fragment in forbidden:
        pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(fragment) + r"(?![0-9A-Za-z])")
        assert pattern.search(_without_hex(text)) is None, fragment
        # The command output names the new file's path and nothing else of the root.
        if fragment != str(pilot.root):
            assert pattern.search(_without_hex(shown)) is None, fragment
    assert _record(pilot).account_pseudonym.startswith("opaque:")


def test_each_providers_pilot_sends_only_reads_to_its_own_hosts(pilot: Environment) -> None:
    assert pilot.run()[0] == 0

    if pilot.provider != "gtm":  # the GTM fixture replaces its workflow seam, not the transport
        assert pilot.requests, "a pilot reads the provider"
    hosts = {request.url.host for request in pilot.requests}
    expected = {
        "google_ads": {"googleads.googleapis.com"},
        "meta_ads": {"graph.facebook.com"},
        "linkedin_ads": {"api.linkedin.com"},
        "tiktok_ads": {"business-api.tiktok.com"},
        "openai_ads": {"api.ads.openai.com"},
        "reddit_ads": {"ads-api.reddit.com"},
        "ga4": {"analyticsadmin.googleapis.com", "analyticsdata.googleapis.com"},
        "gtm": {"tagmanager.googleapis.com"},
    }[pilot.provider]
    assert hosts <= expected, hosts
    for request in pilot.requests:
        # The only POSTs are the read queries Google Ads and GA4 and Reddit send as POST.
        if request.method == "POST":
            assert request.url.path.endswith((":search", ":runReport", "/reports")), request.url
        else:
            assert request.method == "GET", request.method


# --- each provider refuses what the report command would, before any step ---------------------------------


_REPORTING = [name for name in RELEASE_PROVIDERS if name != "gtm"]


@pytest.fixture(params=_REPORTING)
def reporting(request: pytest.FixtureRequest) -> Environment:
    return _environment(request, request.param)


def test_an_unknown_report_field_is_refused_before_any_step_for_each_provider(
    reporting: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_provider_access(monkeypatch)
    _edit_report_input(reporting, unexpected_field="x")

    code, envelope = reporting.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    _untouched(reporting)


def test_a_report_for_another_account_is_refused_before_any_step_for_each_provider(
    reporting: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_provider_access(monkeypatch)
    _edit_report_input(reporting, account_ref="somebody_else")

    code, envelope = reporting.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "IDENTITY_MISMATCH"
    _untouched(reporting)


def test_a_window_other_than_seven_days_is_refused_before_any_step_for_each_provider(
    reporting: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_provider_access(monkeypatch)
    _edit_report_input(reporting, window_end="2026-09-08")

    code, envelope = reporting.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_WINDOW"
    _untouched(reporting)


def test_a_non_read_profile_cannot_run_a_pilot_for_any_provider(
    pilot: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_provider_access(monkeypatch)
    configuration = load_configuration(pilot.root)
    profiles = [
        item.model_copy(update={"purpose": "EDIT"}) if item.profile_id == pilot.profile else item
        for item in configuration.profiles
    ]
    save_configuration(pilot.root, Configuration(profiles=profiles), replace=True)

    code, envelope = pilot.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "READ_PROFILE_REQUIRED"
    _untouched(pilot)


# --- the profile- and option-dependent report checks run before any step -----------------------------------


def _replace_profile(environment: Environment, **changes: Any) -> None:
    configuration = load_configuration(environment.root)
    profiles = [
        item.model_copy(update=changes) if item.profile_id == environment.profile else item
        for item in configuration.profiles
    ]
    save_configuration(environment.root, Configuration(profiles=profiles), replace=True)


@pytest.mark.parametrize(
    "changes",
    [{"source_timezone": "America/New_York"}, {"currency": "EUR"}],
    ids=["source-timezone", "currency"],
)
def test_a_meta_report_that_differs_from_the_account_declaration_is_refused_before_any_step(
    changes: dict[str, str], meta_ads: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit_report_input(meta_ads, **changes)
    forbid_provider_access(monkeypatch)

    code, envelope = meta_ads.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "IDENTITY_MISMATCH"
    assert meta_ads.requests == []
    _untouched(meta_ads)


def test_a_linkedin_profile_that_is_not_utc_is_refused_before_any_step(
    linkedin_ads: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replace_profile(linkedin_ads, timezone="America/Chicago")
    forbid_provider_access(monkeypatch)

    code, envelope = linkedin_ads.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "TIMEZONE_MISMATCH"
    assert linkedin_ads.requests == [] and linkedin_ads.credential_reads == []
    _untouched(linkedin_ads)


def test_a_bad_window_is_refused_before_a_linkedin_timezone_mismatch(
    linkedin_ads: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seven-day window is checked first, before the report preconditions."""
    _replace_profile(linkedin_ads, timezone="America/Chicago")
    _edit_report_input(linkedin_ads, window_end="2026-09-08")
    forbid_provider_access(monkeypatch)

    code, envelope = linkedin_ads.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_WINDOW"
    assert linkedin_ads.requests == [] and linkedin_ads.credential_reads == []
    _untouched(linkedin_ads)


def test_a_bad_window_is_refused_for_a_google_ads_pilot_before_any_access(
    google_ads: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit_report_input(google_ads, window_end="2026-09-08")
    forbid_provider_access(monkeypatch)

    code, envelope = google_ads.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_WINDOW"
    assert google_ads.requests == [] and google_ads.credential_reads == []
    _untouched(google_ads)


def test_the_report_command_and_the_pilot_run_one_shared_precondition_function(
    reporting: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both refuse through `require_report_preconditions`, so they cannot drift apart."""
    calls: list[str] = []

    def refuse(*_args: object, **_kwargs: object) -> Any:
        calls.append("called")
        raise AdhelmError("SHARED_PRECONDITION_PROBE", "probe", exit_code=3)

    monkeypatch.setattr(workflows, "require_report_preconditions", refuse)
    monkeypatch.setattr(pilot_workflow, "require_report_preconditions", refuse)
    forbid_provider_access(monkeypatch)

    code, envelope = reporting.run()
    assert (code, envelope["diagnostics"][0]["code"]) == (3, "SHARED_PRECONDITION_PROBE")
    _untouched(reporting)

    report = _RUNNER.invoke(
        app,
        [
            "--data-dir",
            str(reporting.root),
            "report",
            reporting.provider.replace("_", "-"),
            reporting.profile,
            *(["456"] if reporting.provider == "ga4" else []),
            "--input",
            str(reporting.root / "report-input.json"),
        ],
    )
    assert report.exit_code == 3
    assert json.loads(report.output)["diagnostics"][0]["code"] == "SHARED_PRECONDITION_PROBE"
    assert calls == ["called", "called"]


@pytest.mark.parametrize("provider", ["linkedin_ads", "openai_ads", "tiktok_ads", "meta_ads"])
def test_the_audit_of_a_retained_report_joins_the_natively_keyed_inventory(
    provider: str, request: pytest.FixtureRequest
) -> None:
    """The report names the account canonically; its inventory is keyed by the native id.

    `retained_account_key` joins the two spellings. If it did not, the audit of the
    report this pilot just retained would find no inventory and would not cover STRUCTURE.
    """
    environment = _environment(request, provider)
    assert environment.run()[0] == 0

    coverage = {item.dimension: item.status for item in _record(environment).audit_coverage}
    assert coverage["STRUCTURE"] == "COVERED"

"""Hermetic contracts for bounded OpenAI Ads conversion-setting inventory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.openai_ads import OpenAIAdsIdentity
from adhelm.providers.openai_conversions import (
    OpenAIAdsConversionSettingsClient,
    OpenAIAdsConversionSettingsQuery,
)
from adhelm.transport import SafeHttpClient


def _identity() -> OpenAIAdsIdentity:
    return OpenAIAdsIdentity(
        account_ref="openai_ads.account.adacct_fixture",
        account_client_ref="adacct_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )


def _setting(
    setting_id: str,
    *,
    event_type: str = "order_created",
    custom_event_name: str | None = None,
    ad_account_id: str = "adacct_fixture",
    campaigns: list[object] | None = None,
) -> dict[str, Any]:
    return {
        "id": setting_id,
        "name": "Fixture conversion name discarded before output",
        "event_type": event_type,
        "custom_event_name": custom_event_name,
        "attribution_window_days": 30,
        "ad_account_id": ad_account_id,
        "source_ids": ["clidsrc_fixture"],
        "sources": [{"id": "clidsrc_fixture", "name": "Fixture source name discarded"}],
        "campaigns": [] if campaigns is None else campaigns,
        "archived": False,
        "version": 1,
    }


def _page(
    settings: list[dict[str, Any]],
    *,
    has_more: bool = False,
) -> dict[str, Any]:
    return {
        "object": "list",
        "data": settings,
        "first_id": settings[0]["id"] if settings else None,
        "last_id": settings[-1]["id"] if settings else None,
        "has_more": has_more,
    }


def _client(
    handler: httpx.MockTransport,
    identity: OpenAIAdsIdentity,
    *,
    identity_session_verifier: Callable[[OpenAIAdsIdentity], bool] | None = None,
    token_getter: Callable[[], str] | None = None,
) -> OpenAIAdsConversionSettingsClient:
    return OpenAIAdsConversionSettingsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"api.ads.openai.com"}, read_attempts=1),
        token_getter or (lambda: "fixture-token-not-for-output"),
        identity_session_verifier=(
            identity_session_verifier or (lambda candidate: candidate is identity)
        ),
    )


def test_list_event_settings_is_current_account_bound_paginated_and_scalar_only() -> None:
    identity = _identity()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/conversions/event_settings"
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
        assert request.url.params["limit"] == "1"
        assert request.url.params["order"] == "desc"
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json=_page(
                    [_setting("ces_first", campaigns=[{"id": "campaign_transient"}])],
                    has_more=True,
                ),
            )
        assert request.url.params["after"] == "ces_first"
        return httpx.Response(200, json=_page([_setting("ces_second")]))

    settings = _client(httpx.MockTransport(handler), identity).list_event_settings(
        OpenAIAdsConversionSettingsQuery(
            account_ref=identity.account_ref,
            maximum_records=2,
            page_size=1,
        ),
        identity=identity,
    )

    assert [setting.conversion_setting_client_ref for setting in settings] == [
        "ces_first",
        "ces_second",
    ]
    assert settings[0].campaign_count == 1
    assert settings[0].status == "NOT_ARCHIVED"
    serialized = [setting.model_dump_json() for setting in settings]
    assert all("Fixture" not in row for row in serialized)
    assert all("clidsrc" not in row for row in serialized)
    assert all("campaign_transient" not in row for row in serialized)
    assert [request.url.path for request in requests] == [
        "/v1/conversions/event_settings",
        "/v1/conversions/event_settings",
    ]


def test_identity_session_verifier_stops_forged_identity_before_token_or_http() -> None:
    discovered = _identity()
    forged = OpenAIAdsIdentity.model_validate(discovered.model_dump())
    calls = 0
    token_reads = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    def token_getter() -> str:
        nonlocal token_reads
        token_reads += 1
        return "fixture-token-not-for-output"

    client = _client(httpx.MockTransport(handler), discovered, token_getter=token_getter)
    with pytest.raises(AdhelmError) as raised:
        client.list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=forged.account_ref), identity=forged
        )
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert calls == 0
    assert token_reads == 0


def test_forged_canonical_account_binding_stops_before_verifier_token_or_http() -> None:
    calls = 0
    verifier_calls = 0
    token_reads = 0
    identity = OpenAIAdsIdentity(
        account_ref="openai_ads.account.claimed",
        account_client_ref="adacct_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    def verifier(_candidate: OpenAIAdsIdentity) -> bool:
        nonlocal verifier_calls
        verifier_calls += 1
        return True

    def token_getter() -> str:
        nonlocal token_reads
        token_reads += 1
        return "fixture-token-not-for-output"

    with pytest.raises(AdhelmError) as raised:
        _client(
            httpx.MockTransport(handler),
            identity,
            identity_session_verifier=verifier,
            token_getter=token_getter,
        ).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert calls == verifier_calls == token_reads == 0


def test_response_account_mismatch_fails_closed() -> None:
    identity = _identity()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_page([_setting("ces_fixture", ad_account_id="other")]))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert calls == 1


@pytest.mark.parametrize(
    "event_type",
    [
        "app_installed",
        "app_opened",
        "appointment_scheduled",
        "checkout_started",
        "contents_viewed",
        "items_added",
        "lead_created",
        "order_created",
        "page_viewed",
        "registration_completed",
        "subscription_created",
        "trial_started",
    ],
)
def test_all_documented_standard_event_types_are_accepted(event_type: str) -> None:
    identity = _identity()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([_setting("ces_fixture", event_type=event_type)]))

    settings = _client(httpx.MockTransport(handler), identity).list_event_settings(
        OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
    )
    assert settings[0].event_type == event_type


@pytest.mark.parametrize(
    ("event_type", "custom_event_name"),
    [("custom", "unreviewed_custom"), ("unknown_event", None), ("order_created", "unexpected")],
)
def test_custom_or_unsupported_event_variants_are_rejected(
    event_type: str, custom_event_name: str | None
) -> None:
    identity = _identity()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page(
                [
                    _setting(
                        "ces_fixture",
                        event_type=event_type,
                        custom_event_name=custom_event_name,
                    )
                ]
            ),
        )

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "UNSUPPORTED_CONVERSION_VARIANT"


@pytest.mark.parametrize("window", [29, 31, True, 30.0, "30"])
def test_only_documented_integer_30_day_window_is_accepted(window: object) -> None:
    identity = _identity()

    def handler(_request: httpx.Request) -> httpx.Response:
        setting = _setting("ces_fixture")
        setting["attribution_window_days"] = window
        return httpx.Response(200, json=_page([setting]))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "UNSUPPORTED_CONVERSION_VARIANT"


def test_unknown_or_event_payload_response_fields_are_rejected_without_payload_output() -> None:
    identity = _identity()
    payload_marker = "event-payload-must-not-appear"

    def handler(_request: httpx.Request) -> httpx.Response:
        setting = _setting("ces_fixture")
        setting["event_data_json"] = payload_marker
        return httpx.Response(200, json=_page([setting]))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert payload_marker not in raised.value.message


def test_bad_native_ids_and_source_binding_fail_closed() -> None:
    identity = _identity()

    def handler(_request: httpx.Request) -> httpx.Response:
        setting = _setting("ces/../../escape")
        setting["sources"] = [{"id": "other_source", "name": "Discarded source"}]
        return httpx.Response(200, json=_page([setting]))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_result_cap_stops_without_partial_return_or_next_page_request() -> None:
    identity = _identity()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json=_page([_setting("ces_fixture")], has_more=True))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(
                account_ref=identity.account_ref,
                maximum_records=1,
                page_size=1,
            ),
            identity=identity,
        )
    assert raised.value.code == "RESULT_LIMIT_REQUIRED"
    assert calls == 1


def test_redirects_are_rejected_and_no_recent_event_stream_is_requested() -> None:
    identity = _identity()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(302, headers={"location": "https://example.invalid/"})

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler), identity).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "UNAPPROVED_EGRESS"
    assert paths == ["/v1/conversions/event_settings"]
    assert "/v1/conversions/events" not in paths


def test_query_requires_documented_page_bounds() -> None:
    with pytest.raises(ValidationError):
        OpenAIAdsConversionSettingsQuery(
            account_ref="openai_ads.account.adacct_fixture", page_size=501
        )
    with pytest.raises(ValidationError):
        OpenAIAdsConversionSettingsQuery(
            account_ref="openai_ads.account.adacct_fixture",
            maximum_records=101,
            page_size=10,
        )

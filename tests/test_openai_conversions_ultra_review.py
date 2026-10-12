"""Adversarial boundary checks for the fixed OpenAI conversion-settings read."""

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


def _client(
    handler: httpx.MockTransport,
    identity: OpenAIAdsIdentity,
    *,
    token_getter: Callable[[], str],
) -> OpenAIAdsConversionSettingsClient:
    return OpenAIAdsConversionSettingsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"api.ads.openai.com"}, read_attempts=1),
        token_getter,
        identity_session_verifier=lambda candidate: candidate is identity,
    )


def test_bypassed_query_cannot_send_an_oversized_or_undeclared_request() -> None:
    identity = _identity()
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

    bypassed = OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref).model_copy(
        update={"page_size": 501, "maximum_records": 501, "order": "not-documented"}
    )

    with pytest.raises(ValidationError):
        _client(
            httpx.MockTransport(handler), identity, token_getter=token_getter
        ).list_event_settings(bypassed, identity=identity)
    assert calls == token_reads == 0


def test_empty_response_missing_closed_pagination_fields_is_not_a_verified_absence() -> None:
    identity = _identity()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/conversions/event_settings"
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    with pytest.raises(AdhelmError) as raised:
        _client(
            httpx.MockTransport(handler),
            identity,
            token_getter=lambda: "fixture-token-not-for-output",
        ).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_client_refuses_a_transport_that_can_buffer_above_its_response_contract() -> None:
    identity = _identity()
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
        {"api.ads.openai.com"},
        maximum_response_bytes=8_388_609,
        read_attempts=1,
    )

    with pytest.raises(AdhelmError) as raised:
        OpenAIAdsConversionSettingsClient(
            transport,
            lambda: "fixture-token-not-for-output",
            identity_session_verifier=lambda candidate: candidate is identity,
        )
    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"


def test_rejected_response_data_never_echoes_nested_customer_text() -> None:
    identity = _identity()
    marker = "customer@example.test"

    def handler(_request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = {
            "object": "list",
            "data": [],
            "first_id": None,
            "last_id": None,
            "has_more": False,
            "unexpected": {"customer_note": marker},
        }
        return httpx.Response(200, json=payload)

    with pytest.raises(AdhelmError) as raised:
        _client(
            httpx.MockTransport(handler),
            identity,
            token_getter=lambda: "fixture-token-not-for-output",
        ).list_event_settings(
            OpenAIAdsConversionSettingsQuery(account_ref=identity.account_ref), identity=identity
        )
    assert raised.value.code == "SENSITIVE_DATA_RISK"
    assert marker not in raised.value.message

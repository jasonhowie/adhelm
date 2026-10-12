"""Fake-only tests for fixed Google Ads v25 EDIT-session observations."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsCampaignState,
    GoogleAdsCampaignStateReader,
    GoogleAdsEditIdentity,
    _GoogleAdsEditTarget,
)
from adhelm.transport import SafeHttpClient

_CUSTOMER = "1234567890"
_LOGIN = "0987654321"
_TOKEN = "fixture-google-oauth-bearer"
_DEVELOPER = "fixture-google-developer-token"


def _target() -> _GoogleAdsEditTarget:
    return _GoogleAdsEditTarget(
        customer_client_ref=_CUSTOMER,
        login_customer_client_ref=_LOGIN,
        declared_currency="USD",
        declared_timezone="UTC",
    )


def _headers() -> dict[str, str]:
    return {
        "authorization": f"Bearer {_TOKEN}",
        "developer-token": _DEVELOPER,
        "login-customer-id": _LOGIN,
    }


def _reader(
    handler: Callable[[httpx.Request], httpx.Response],
    getter: Callable[[], dict[str, str]] = _headers,
    *,
    read_attempts: int = 1,
    maximum_response_bytes: int = 65_536,
) -> GoogleAdsCampaignStateReader:
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    return GoogleAdsCampaignStateReader(
        SafeHttpClient(
            client,
            {"googleads.googleapis.com"},
            read_attempts=read_attempts,
            maximum_response_bytes=maximum_response_bytes,
        ),
        getter,
        _target(),
    )


def _identity_response(
    *, status: object = "CLOSED", extra: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "results": [
            {
                "customer": {
                    "id": _CUSTOMER,
                    "resourceName": f"customers/{_CUSTOMER}",
                    "currencyCode": "USD",
                    "timeZone": "UTC",
                    "status": status,
                }
            }
        ],
        "totalResultsCount": "1",
        "fieldMask": "customer.id,customer.currency_code,customer.time_zone,customer.status",
        "queryResourceConsumption": "1",
    }
    if extra:
        payload.update(extra)
    return payload


def _campaign_response(
    *, campaign: object = "42", status: object = "ENABLED", extra: dict[str, object] | None = None
) -> dict[str, object]:
    resource = f"customers/{_CUSTOMER}/campaigns/{campaign}" if type(campaign) is str else "invalid"
    payload: dict[str, object] = {
        "results": [{"campaign": {"id": campaign, "resourceName": resource, "status": status}}],
        "totalResultsCount": "1",
        "fieldMask": "campaign.id,campaign.resource_name,campaign.status",
        "queryResourceConsumption": "1",
    }
    if extra:
        payload.update(extra)
    return payload


def _identity() -> GoogleAdsEditIdentity:
    return GoogleAdsEditIdentity(
        customer_ref=f"google_ads.customer.{_CUSTOMER}",
        customer_client_ref=_CUSTOMER,
        currency="USD",
        timezone="UTC",
        status="CLOSED",
    )


def test_identity_and_campaign_use_exact_one_attempt_fixed_v25_searches() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == (
            f"https://googleads.googleapis.com/v25/customers/{_CUSTOMER}/googleAds:search"
        )
        assert request.headers["authorization"] == f"Bearer {_TOKEN}"
        assert request.headers["developer-token"] == _DEVELOPER
        assert request.headers["login-customer-id"] == _LOGIN
        body = json.loads(request.content)
        assert body["searchSettings"] == {"returnTotalResultsCount": True}
        assert set(body) == {"query", "searchSettings"}
        if "FROM customer" in body["query"]:
            return httpx.Response(200, json=_identity_response())
        assert body["query"] == (
            "SELECT campaign.id, campaign.resource_name, campaign.status FROM campaign "
            "WHERE campaign.id = 42"
        )
        return httpx.Response(200, json=_campaign_response())

    reader = _reader(handler)
    identity = reader.discover_identity()
    state = reader.read_campaign_state(identity, "42")

    assert identity.model_dump(mode="json") == {
        "customer_ref": f"google_ads.customer.{_CUSTOMER}",
        "customer_client_ref": _CUSTOMER,
        "currency": "USD",
        "timezone": "UTC",
        "status": "CLOSED",
        "provider_api_version": "v25",
    }
    assert state.model_dump(mode="json") == {
        "campaign_ref": f"google_ads.campaign.{_CUSTOMER}.42",
        "campaign_client_ref": "42",
        "customer_ref": f"google_ads.customer.{_CUSTOMER}",
        "customer_client_ref": _CUSTOMER,
        "status": "ENABLED",
        "provider_api_version": "v25",
    }
    assert len(requests) == 2


@pytest.mark.parametrize("status", ["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"])
def test_customer_status_is_observed_without_local_eligibility_gate(status: str) -> None:
    observed = _reader(
        lambda _request: httpx.Response(200, json=_identity_response(status=status))
    ).discover_identity()

    assert observed.status == status


@pytest.mark.parametrize(
    "campaign_id", ["0", "01", "9223372036854775808", "1/../2", "１２", "", 42]
)
def test_invalid_campaign_id_stops_before_credentials_or_post(campaign_id: object) -> None:
    callbacks = 0
    requests = 0

    def getter() -> dict[str, str]:
        nonlocal callbacks
        callbacks += 1
        return _headers()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, getter).read_campaign_state(_identity(), campaign_id)  # type: ignore[arg-type]

    assert raised.value.code == "INVALID_INPUT"
    assert callbacks == requests == 0


@pytest.mark.parametrize(
    "identity",
    [
        _identity().model_copy(update={"customer_client_ref": "0000000000"}),
        _identity().model_copy(update={"customer_ref": "google_ads.customer.0000000000"}),
        _identity().model_copy(update={"currency": "EUR"}),
        _identity().model_copy(update={"timezone": "America/Chicago"}),
        _identity().model_copy(update={"status": "UNKNOWN"}),
    ],
)
def test_constructed_or_mismatched_identity_stops_before_credentials_or_post(
    identity: GoogleAdsEditIdentity,
) -> None:
    callbacks = 0
    requests = 0

    def getter() -> dict[str, str]:
        nonlocal callbacks
        callbacks += 1
        return _headers()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, getter).read_campaign_state(identity, "42")

    assert raised.value.code == "IDENTITY_BINDING_INVALID"
    assert callbacks == requests == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [], "totalResultsCount": "0"},
        _identity_response(extra={"nextPageToken": "unexpected-page"}),
        _identity_response(extra={"summaryRow": {"customer": {}}}),
        _identity_response(extra={"metricAttributes": [{"foo": "bar"}]}),
        _identity_response(extra={"unknown": True}),
        {"results": _identity_response()["results"], "totalResultsCount": 1},
        {
            "results": _identity_response()["results"] * 2,
            "totalResultsCount": "2",
        },
    ],
)
def test_non_single_or_unsupported_search_envelopes_fail_closed(payload: dict[str, object]) -> None:
    with pytest.raises(AdhelmError) as raised:
        _reader(lambda _request: httpx.Response(200, json=payload)).discover_identity()

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("status", "campaign"),
    [
        ("UNKNOWN", "42"),
        ("UNSPECIFIED", "42"),
        ("PAUSED", "0"),
        ("PAUSED", "9223372036854775808"),
    ],
)
def test_unknown_or_invalid_campaign_wire_values_fail_closed(
    status: object, campaign: object
) -> None:
    with pytest.raises(AdhelmError) as raised:
        _reader(
            lambda _request: httpx.Response(
                200, json=_campaign_response(campaign=campaign, status=status)
            )
        ).read_campaign_state(_identity(), "42")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("operation", ("identity", "campaign"))
def test_selected_resource_name_must_be_non_null_and_exact(operation: str) -> None:
    if operation == "identity":
        payload = _identity_response()
        payload["results"][0]["customer"]["resourceName"] = None  # type: ignore[index]
    else:
        payload = _campaign_response()
        payload["results"][0]["campaign"]["resourceName"] = None  # type: ignore[index]

    with pytest.raises(AdhelmError) as raised:
        reader = _reader(lambda _request: httpx.Response(200, json=payload))
        if operation == "identity":
            reader.discover_identity()
        else:
            reader.read_campaign_state(_identity(), "42")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_callback_mutated_cookie_state_is_rejected_before_the_post() -> None:
    requests = 0
    reader_holder: list[GoogleAdsCampaignStateReader] = []

    def getter() -> dict[str, str]:
        transport = reader_holder[0]._transport
        transport.client.cookies.set("unexpected", "state")
        return _headers()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_identity_response())

    reader = _reader(handler, getter)
    reader_holder.append(reader)
    with pytest.raises(AdhelmError) as raised:
        reader.discover_identity()

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert requests == 0


def test_cookie_from_the_initial_response_blocks_the_later_campaign_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"set-cookie": "unexpected=state; Path=/; Secure"},
                json=_identity_response(),
            )
        pytest.fail("campaign POST must not follow an ambient cookie")

    reader = _reader(handler)
    identity = reader.discover_identity()

    with pytest.raises(AdhelmError) as raised:
        reader.read_campaign_state(identity, "42")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert len(requests) == 1


def test_secret_bearing_callback_error_is_normalized() -> None:
    def getter() -> dict[str, str]:
        raise AdhelmError("ARBITRARY", "Bearer fixture-secret-should-never-escape")

    with pytest.raises(AdhelmError) as raised:
        _reader(lambda _request: pytest.fail("no HTTP"), getter).discover_identity()

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert "fixture-secret" not in raised.value.message


def test_models_are_frozen_and_bind_their_native_references() -> None:
    with pytest.raises(ValidationError):
        GoogleAdsCampaignState(
            campaign_ref=f"google_ads.campaign.{_CUSTOMER}.42",
            campaign_client_ref="042",
            customer_ref=f"google_ads.customer.{_CUSTOMER}",
            customer_client_ref=_CUSTOMER,
            status="PAUSED",
        )
    with pytest.raises(ValidationError):
        GoogleAdsCampaignState(
            campaign_ref="google_ads.campaign.other.42",
            campaign_client_ref="42",
            customer_ref=f"google_ads.customer.{_CUSTOMER}",
            customer_client_ref=_CUSTOMER,
            status="PAUSED",
        )

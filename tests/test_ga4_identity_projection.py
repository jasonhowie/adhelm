"""Closed partial-response regressions for legacy GA4 identity discovery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.ga4 import Ga4DataClient
from adhelm.transport import SafeHttpClient

_PROPERTY_PATH = "/v1beta/properties/123"
_STREAMS_PATH = "/v1beta/properties/123/dataStreams"
_PROPERTY_FIELDS = "name,timeZone,currencyCode"
_STREAM_FIELDS = "dataStreams(name,type),nextPageToken"


def _property(**changes: object) -> dict[str, object]:
    return {
        "name": "properties/123",
        "timeZone": "America/Chicago",
        "currencyCode": "USD",
        **changes,
    }


def _stream(
    stream_id: str, stream_type: str = "WEB_DATA_STREAM", **changes: object
) -> dict[str, object]:
    return {
        "name": f"properties/123/dataStreams/{stream_id}",
        "type": stream_type,
        **changes,
    }


def _page(
    streams: list[dict[str, object]], *, next_token: str | None = None, **changes: object
) -> dict[str, object]:
    return {
        "dataStreams": streams,
        **({"nextPageToken": next_token} if next_token is not None else {}),
        **changes,
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    client_params: dict[str, str] | None = None,
    token_getter: Callable[[], str] = lambda: "fixture-token",
) -> Ga4DataClient:
    return Ga4DataClient(
        SafeHttpClient(
            httpx.Client(
                transport=httpx.MockTransport(handler), params=client_params, trust_env=False
            ),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        token_getter,
        now=lambda: datetime(2026, 9, 5, 12, tzinfo=UTC),
        connector_version="identity-projection-review",
    )


def test_identity_uses_exact_partial_masks_and_preserves_the_existing_wire_shape() -> None:
    calls: list[tuple[str, str, tuple[tuple[str, str], ...], bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.path,
                tuple(request.url.params.multi_items()),
                request.content,
                request.headers["authorization"],
            )
        )
        assert request.url.host == "analyticsadmin.googleapis.com"
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=_property())
        assert request.url.path == _STREAMS_PATH
        if request.url.params.get("pageToken") is None:
            return httpx.Response(
                200, json=_page([_stream("100", "ANDROID_APP_DATA_STREAM")], next_token="page-1")
            )
        assert request.url.params["pageToken"] == "page-1"
        return httpx.Response(200, json=_page([_stream("456")]))

    identity = _client(handler).discover_identity("123", expected_web_stream_id="456")

    assert calls == [
        (
            "GET",
            _PROPERTY_PATH,
            (("fields", _PROPERTY_FIELDS),),
            b"",
            "Bearer fixture-token",
        ),
        (
            "GET",
            _STREAMS_PATH,
            (("pageSize", "200"), ("fields", _STREAM_FIELDS)),
            b"",
            "Bearer fixture-token",
        ),
        (
            "GET",
            _STREAMS_PATH,
            (("pageSize", "200"), ("fields", _STREAM_FIELDS), ("pageToken", "page-1")),
            b"",
            "Bearer fixture-token",
        ),
    ]
    assert identity.model_dump(mode="json") == {
        "schema_version": 1,
        "property_ref": "ga4.property.123",
        "property_client_ref": "123",
        "web_stream_ref": "ga4.stream.456",
        "web_stream_client_ref": "456",
        "timezone": "America/Chicago",
        "currency": "USD",
        "admin_api_version": "v1beta",
    }


@pytest.mark.parametrize(
    ("stage", "response", "private"),
    [
        (
            "property",
            _property(displayName="private customer property"),
            "private customer property",
        ),
        (
            "streams",
            _page([_stream("456", displayName="private customer stream")]),
            "private customer stream",
        ),
        (
            "streams",
            _page([_stream("456")], unexpected="private outer list field"),
            "private outer list field",
        ),
    ],
)
def test_unrequested_projected_fields_fail_closed_without_reflection(
    stage: str, response: dict[str, object], private: str
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=response if stage == "property" else _property())
        return httpx.Response(200, json=response)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123", expected_web_stream_id="456")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert private not in str(raised.value)
    assert calls == ([_PROPERTY_PATH] if stage == "property" else [_PROPERTY_PATH, _STREAMS_PATH])


def test_full_web_stream_response_with_default_uri_remains_privately_rejected() -> None:
    private_uri = "https://customer.example/private?email=person@example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=_property())
        return httpx.Response(
            200,
            json=_page([_stream("456", webStreamData={"defaultUri": private_uri})]),
        )

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123", expected_web_stream_id="456")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert private_uri not in str(raised.value)


@pytest.mark.parametrize(
    ("stage", "response"),
    [
        ("property", _property(timeZone=True)),
        ("property", _property(currencyCode=0)),
        ("streams", _page([_stream("456", type=1)])),
        ("streams", _page([_stream("456", "DATA_STREAM_TYPE_UNSPECIFIED")])),
    ],
)
def test_noncanonical_projected_scalars_and_stream_kinds_fail_closed(
    stage: str, response: dict[str, object]
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=response if stage == "property" else _property())
        return httpx.Response(200, json=response)

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123", expected_web_stream_id="456")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == ([_PROPERTY_PATH] if stage == "property" else [_PROPERTY_PATH, _STREAMS_PATH])


def test_duplicate_stream_resource_across_pages_fails_before_identity_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=_property())
        if request.url.params.get("pageToken") is None:
            return httpx.Response(200, json=_page([_stream("456")], next_token="next"))
        return httpx.Response(200, json=_page([_stream("456")]))

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_repeated_or_empty_pagination_cannot_make_unsafe_progress() -> None:
    state = {"calls": 0}

    def repeated_token(request: httpx.Request) -> httpx.Response:
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=_property())
        state["calls"] += 1
        return httpx.Response(
            200,
            json=_page(
                [_stream(str(100 + state["calls"]), "ANDROID_APP_DATA_STREAM")], next_token="cycle"
            ),
        )

    with pytest.raises(AdhelmError) as repeated:
        _client(repeated_token).discover_identity("123")
    assert repeated.value.code == "PROVIDER_RESPONSE_INVALID"
    assert state["calls"] == 2

    def empty_page(request: httpx.Request) -> httpx.Response:
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=_property())
        return httpx.Response(200, json=_page([], next_token="next"))

    with pytest.raises(AdhelmError) as empty:
        _client(empty_page).discover_identity("123")
    assert empty.value.code == "PROVIDER_RESPONSE_INVALID"


def test_identity_stream_page_bound_is_closed() -> None:
    page_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        if request.url.path == _PROPERTY_PATH:
            return httpx.Response(200, json=_property())
        page_calls += 1
        start = (page_calls - 1) * 200 + 1
        page = [
            _stream(str(stream_id), "ANDROID_APP_DATA_STREAM")
            for stream_id in range(start, start + 200)
        ]
        return httpx.Response(200, json=_page(page, next_token=f"page-{page_calls}"))

    with pytest.raises(AdhelmError) as raised:
        _client(handler).discover_identity("123")

    assert raised.value.code == "PAGINATION_LIMIT"
    assert page_calls == 5


def test_default_client_query_parameters_are_rejected_before_token_or_http() -> None:
    token_reads: list[str] = []
    egresses: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        egresses.append(request.url.path)
        raise AssertionError("identity request escaped")

    with pytest.raises(AdhelmError) as raised:
        _client(
            handler,
            client_params={"unexpected": "injected"},
            token_getter=lambda: token_reads.append("token") or "fixture-token",
        ).discover_identity("123", expected_web_stream_id="456")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert token_reads == egresses == []

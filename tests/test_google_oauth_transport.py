from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from google.auth import exceptions as google_auth_exceptions

from adhelm import google_oauth_transport as oauth_transport
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_CERTS_ENDPOINT,
    GOOGLE_OAUTH_TOKEN_ENDPOINT,
    GoogleOAuthTokenDispatchState,
    GoogleOAuthTransportError,
)
from adhelm.google_oauth_transport import (
    FixedGoogleOAuthTransport,
    GoogleOAuthCertificateRequest,
    is_fixed_google_oauth_endpoint,
)


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        return None


def test_fixed_transport_posts_exact_urlencoded_form_without_generic_transport() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            content=b'{"access_token":"unused"}',
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    with FixedGoogleOAuthTransport(client) as transport:
        response = transport.post_token_form(
            (
                ("client_id", "client.apps.googleusercontent.com"),
                ("client_secret", "private-secret"),
                ("grant_type", "refresh_token"),
                ("refresh_token", "private-refresh"),
            )
        )
    assert response == b'{"access_token":"unused"}'
    assert len(observed) == 1
    request = observed[0]
    assert request.method == "POST"
    assert str(request.url) == GOOGLE_OAUTH_TOKEN_ENDPOINT
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["accept"] == "application/json"
    assert request.content == (
        b"client_id=client.apps.googleusercontent.com&client_secret=private-secret&"
        b"grant_type=refresh_token&refresh_token=private-refresh"
    )


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (200, "text/html", b"<html>not-json</html>"),
        (302, "application/json", b"{}"),
    ],
)
def test_fixed_token_transport_never_accepts_html_or_redirect(
    status: int, content_type: str, body: bytes
) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"content-type": content_type}, content=body)
        ),
        trust_env=False,
    )
    with FixedGoogleOAuthTransport(client) as transport:
        with pytest.raises(GoogleOAuthTransportError) as raised:
            transport.post_token_form(
                (
                    ("client_id", "client.apps.googleusercontent.com"),
                    ("grant_type", "refresh_token"),
                )
            )
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.UNCERTAIN
    assert "private" not in str(raised.value)


def test_certificate_request_permits_only_google_auth_default_get() -> None:
    class FakeCertificates:
        def __init__(self) -> None:
            self.calls = 0

        def get_google_oauth_certificates(self) -> bytes:
            self.calls += 1
            return json.dumps({"key": "certificate"}).encode("utf-8")

    fake = FakeCertificates()
    request = GoogleOAuthCertificateRequest(fake)
    response = request(GOOGLE_OAUTH_CERTS_ENDPOINT, method="GET")
    assert response.status == 200
    assert response.data == b'{"key": "certificate"}'
    assert fake.calls == 1
    with pytest.raises(google_auth_exceptions.TransportError):
        request("https://www.googleapis.com/oauth2/v3/certs", method="GET")
    with pytest.raises(google_auth_exceptions.TransportError):
        request(GOOGLE_OAUTH_CERTS_ENDPOINT, method="POST", body=b"x")


def test_fixed_endpoint_guard_rejects_lookalikes_and_query_routing() -> None:
    assert is_fixed_google_oauth_endpoint(GOOGLE_OAUTH_TOKEN_ENDPOINT)
    assert is_fixed_google_oauth_endpoint(GOOGLE_OAUTH_CERTS_ENDPOINT, certificates=True)
    assert not is_fixed_google_oauth_endpoint("https://oauth2.googleapis.com/token?next=other")
    assert not is_fixed_google_oauth_endpoint("https://oauth2.googleapis.com.evil/token")


def test_token_slow_stream_exhausts_whole_response_budget_after_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Start, headers, first chunk, then a second timely socket read whose
    # cumulative elapsed time is past the private whole-response budget.
    samples = iter((0.0, 0.1, 15.0, 30.0))
    monkeypatch.setattr(oauth_transport, "_monotonic", lambda: next(samples))
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_ChunkStream((b'{"access":', b'"unused"}')),
            )
        ),
        trust_env=False,
    )
    with FixedGoogleOAuthTransport(client) as transport:
        with pytest.raises(GoogleOAuthTransportError) as raised:
            transport.post_token_form(
                (
                    ("client_id", "client.apps.googleusercontent.com"),
                    ("grant_type", "refresh_token"),
                )
            )
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.UNCERTAIN


def test_certificate_slow_stream_exhausts_whole_response_budget_without_token_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = iter((100.0, 100.1, 130.0))
    monkeypatch.setattr(oauth_transport, "_monotonic", lambda: next(samples))
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_ChunkStream((b"{}",)),
            )
        ),
        trust_env=False,
    )
    with FixedGoogleOAuthTransport(client) as transport:
        with pytest.raises(GoogleOAuthTransportError) as raised:
            transport.get_google_oauth_certificates()
    assert raised.value.code == "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE"
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.NOT_DISPATCHED


@pytest.mark.parametrize("invalid_clock", [float("nan"), -1.0])
def test_token_response_clock_failure_after_post_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, invalid_clock: float
) -> None:
    samples = iter((10.0, 11.0, invalid_clock))
    monkeypatch.setattr(oauth_transport, "_monotonic", lambda: next(samples))
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_ChunkStream((b"{}",)),
            )
        ),
        trust_env=False,
    )
    with FixedGoogleOAuthTransport(client) as transport:
        with pytest.raises(GoogleOAuthTransportError) as raised:
            transport.post_token_form(
                (
                    ("client_id", "client.apps.googleusercontent.com"),
                    ("grant_type", "refresh_token"),
                )
            )
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.UNCERTAIN


def test_invalid_monotonic_clock_before_token_dispatch_never_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    monkeypatch.setattr(oauth_transport, "_monotonic", lambda: float("inf"))
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    with FixedGoogleOAuthTransport(client) as transport:
        with pytest.raises(GoogleOAuthTransportError) as raised:
            transport.post_token_form(
                (
                    ("client_id", "client.apps.googleusercontent.com"),
                    ("grant_type", "refresh_token"),
                )
            )
    assert calls == 0
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.NOT_DISPATCHED

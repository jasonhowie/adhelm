"""Malformed provider headers cannot reach the network or escape as raw codec errors."""

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.transport import SafeHttpClient


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer fixture\x7f"},
        {"authorization": "Bearer fixture\u0100"},
        {"authorization": "Bearer fixture\r\ninjected: yes"},
        {"bad name": "value"},
        {"authorization": "Bearer first", "Authorization": "Bearer second"},
        {"authorization": b"Bearer fixture"},
        {"authorization": "x" * 16_513},
        {7: "value"},
    ],
)
def test_bad_explicit_headers_fail_before_http(headers: dict) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("Malformed headers reached HTTP")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = SafeHttpClient(client, {"example.invalid"})
        with pytest.raises(AdhelmError) as raised:
            transport.request("GET", "https://example.invalid/fixed", headers=headers)
    assert raised.value.code == "INVALID_REQUEST_HEADERS"
    assert "fixture" not in str(raised.value)
    assert calls == []


def test_documented_ascii_header_values_pass_unchanged() -> None:
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers)
        return httpx.Response(200, json={"ok": True})

    headers = {
        "authorization": "Bearer fixture-value",
        "content-type": "application/json; charset=utf-8",
        "LinkedIn-Version": "202608",
        "x-restli-protocol-version": "2.0.0",
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = SafeHttpClient(client, {"example.invalid"}).request(
            "GET",
            "https://example.invalid/fixed",
            headers=headers,
        )
    assert response.status_code == 200
    assert all(observed[0][key] == value for key, value in headers.items())

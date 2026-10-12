"""Actual HTTPX serialization and refusal checks for fixed mutation forms."""

from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.transport import SafeHttpClient


def test_form_is_encoded_in_body_and_write_timeout_is_never_retried() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == "https://provider.test/v26.0/123"
        assert request.content == b"status=PAUSED"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        raise httpx.ReadTimeout("provider-secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = SafeHttpClient(client, {"provider.test"}, sleep=lambda _: pytest.fail("retry"))
        with pytest.raises(AdhelmError) as error:
            transport.request(
                "POST",
                "https://provider.test/v26.0/123",
                headers={"authorization": "Bearer synthetic-token"},
                form={"status": "PAUSED"},
                mutation=True,
            )
    assert error.value.code == "AMBIGUOUS_REMOTE_STATE"
    assert "provider-secret" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "GET"},
        {"method": "PATCH"},
        {"mutation": False},
        {"mutation": 1},
        {"json": {}},
        {"params": {}},
        {"opaque_read_query": b"after=next"},
        {"form": {}},
        {"form": [("status", "PAUSED")]},
        {"form": {"status": ["PAUSED"]}},
        {"form": {"status": True}},
        {"form": {"status": "PAUSED\n"}},
        {"form": {"status": "é"}},
        {"form": {"status": "x" * 4097}},
        {"form": {"bad key": "PAUSED"}},
        {"form": {"x" * 129: "PAUSED"}},
        {"form": {f"field{i}": "x" for i in range(33)}},
        {"form": {f"field{i}": "x" * 4096 for i in range(4)}},
        {"headers": {"Content-Type": "application/json"}},
        {"headers": {"Content-Length": "0"}},
        {"headers": {"Transfer-Encoding": "chunked"}},
    ],
)
def test_malformed_or_mixed_form_refused_before_http(overrides: dict[str, Any]) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = SafeHttpClient(client, {"provider.test"})
        kwargs: dict[str, Any] = {"form": {"status": "PAUSED"}, "mutation": True}
        kwargs.update(overrides)
        method = kwargs.pop("method", "POST")
        with pytest.raises(AdhelmError) as error:
            transport.request(method, "https://provider.test/change", **kwargs)
    assert error.value.code == "INVALID_REQUEST_FORM"
    assert not calls


@pytest.mark.parametrize(
    "defaults",
    [
        {"params": {"access_token": "synthetic-token"}},
        {"headers": {"Content-Type": "text/plain"}},
        {"headers": {"Content-Length": "0"}},
        {"headers": {"Transfer-Encoding": "chunked"}},
    ],
)
def test_form_rejects_hidden_query_and_body_headers(defaults: dict[str, Any]) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle), **defaults) as client:
        with pytest.raises(AdhelmError) as error:
            SafeHttpClient(client, {"provider.test"}).request(
                "POST", "https://provider.test/change", form={"status": "PAUSED"}, mutation=True
            )
    assert error.value.code == "INVALID_REQUEST_FORM"
    assert not calls


def test_form_privacy_scan_and_bounded_response_remain_enforced() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"12345")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = SafeHttpClient(client, {"provider.test"}, maximum_response_bytes=4)
        with pytest.raises(AdhelmError) as error:
            transport.request(
                "POST",
                "https://provider.test/change",
                form={"email": "test@example.test"},
                mutation=True,
            )
        assert error.value.code == "SENSITIVE_DATA_RISK"
        assert not calls
        with pytest.raises(AdhelmError) as error:
            transport.request(
                "POST", "https://provider.test/change", form={"status": "PAUSED"}, mutation=True
            )
        assert error.value.code == "RESPONSE_TOO_LARGE"
    assert len(calls) == 1

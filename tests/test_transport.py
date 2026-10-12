import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.transport import SafeHttpClient, _retry_after_seconds


def test_egress_denied_before_request_and_redirect_not_followed() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.test/"})

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), {"provider.test"}
    )
    for url in [
        "https://evil.test/",
        "http://provider.test/",
        "https://provider.test/?token=secret",
    ]:
        with pytest.raises(AdhelmError, match="outside"):
            transport.request("GET", url)
    assert not calls
    with pytest.raises(AdhelmError, match="redirects"):
        transport.request("GET", "https://provider.test/read")
    assert len(calls) == 1


def test_write_timeout_is_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("raw secret may be in the request", request=request)

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), {"provider.test"}
    )
    with pytest.raises(AdhelmError) as error:
        transport.request("POST", "https://provider.test/change", mutation=True)
    assert error.value.code == "AMBIGUOUS_REMOTE_STATE"
    assert "secret" not in str(error.value)
    assert len(calls) == 1


def test_read_retries_are_bounded_and_retry_after_capped() -> None:
    calls = []
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "999999"})
        return httpx.Response(200, json={"rows": []})

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), {"provider.test"}, sleep=delays.append
    )
    assert transport.request("POST", "https://provider.test/report", json={}).json() == {"rows": []}
    assert len(calls) == 3
    assert delays == [10.0, 10.0]


def test_a_retry_after_header_int_rejects_falls_back_to_the_fixed_delay() -> None:
    """`str.isdigit` is not the set `int` and `float` accept.

    `U+00B2` is a legal latin-1 header byte that `isdigit` calls a digit and
    `int` rejects, so reading it without an ASCII bound raised an unsanitized
    `ValueError` out of the retry path instead of retrying.
    """
    calls: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            return httpx.Response(429, headers=[(b"retry-after", b"\xb2")])
        return httpx.Response(200, json={"rows": []})

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), {"provider.test"}, sleep=delays.append
    )
    assert transport.request("GET", "https://provider.test/report").json() == {"rows": []}
    assert delays == [1.0]


def test_only_an_ascii_decimal_retry_after_value_is_read_as_a_hint() -> None:
    """The parser's contract, stated directly on the parser.

    `str.isdigit` accepts values `int` rejects, and `str.isdecimal` accepts
    non-ASCII digits `int` would silently convert -- a value the caller never
    wrote in the digits it expected.  Only the ASCII delay-seconds form is a
    hint; everything else reads exactly like an absent header.
    """
    assert _retry_after_seconds("30") == 30
    assert _retry_after_seconds("99999999") == 3_600
    for value in ("\u00b2", "\u0663\u0660", "-1", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT", "", None):
        assert _retry_after_seconds(value) is None


def test_oversized_response_is_rejected() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"abc"))
    )
    with pytest.raises(AdhelmError) as error:
        SafeHttpClient(client, {"provider.test"}, maximum_response_bytes=2).request(
            "GET", "https://provider.test/report"
        )
    assert error.value.code == "RESPONSE_TOO_LARGE"

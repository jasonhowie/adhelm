"""Independent fake-only adversarial coverage for the Google OAuth browser boundary."""

from __future__ import annotations

import base64
import inspect
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from adhelm import google_oauth_browser as browser
from adhelm import google_oauth_protocol as protocol
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GoogleOAuthAuthorizationAttempt,
    GoogleOAuthTarget,
    consume_google_oauth_callback,
)

_ISSUED_AT = datetime(2030, 1, 1, tzinfo=UTC)


def _base64url(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


def _attempt(
    redirect_uri: str, *, expires_at: datetime | None = None
) -> GoogleOAuthAuthorizationAttempt:
    return GoogleOAuthAuthorizationAttempt(
        target=GoogleOAuthTarget(
            client_ref="review-client.apps.googleusercontent.com",
            api_scope="https://www.googleapis.com/auth/analytics.readonly",
        ),
        redirect_uri=redirect_uri,
        state=_base64url(1),
        nonce=_base64url(2),
        code_verifier="A" * 64,
        issued_at=_ISSUED_AT,
        expires_at=expires_at or _ISSUED_AT + timedelta(minutes=10),
    )


class _FakeConnection:
    def __init__(self, request: bytes, *, after_reply: Callable[[], None] | None = None) -> None:
        self._request = request
        self._after_reply = after_reply
        self.closed = False
        self.responses: list[bytes] = []
        self.timeouts: list[float] = []

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, _: int) -> bytes:
        request, self._request = self._request, b""
        return request

    def sendall(self, response: bytes) -> None:
        self.responses.append(response)
        if self._after_reply is not None:
            self._after_reply()


class _FakeListener:
    def __init__(self, accepted: list[tuple[_FakeConnection, tuple[str, int]]]) -> None:
        self._accepted = accepted
        self.bind_calls: list[tuple[str, int]] = []
        self.listen_calls: list[int] = []
        self.timeout_calls: list[float] = []
        self.accept_calls = 0
        self.closed = False

    def bind(self, address: tuple[str, int]) -> None:
        self.bind_calls.append(address)

    def listen(self, backlog: int) -> None:
        self.listen_calls.append(backlog)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 43123)

    def settimeout(self, value: float) -> None:
        self.timeout_calls.append(value)

    def accept(self) -> tuple[_FakeConnection, tuple[str, int]]:
        self.accept_calls += 1
        if not self._accepted:
            raise AssertionError("fake listener accepted beyond the supplied bounded requests")
        return self._accepted.pop(0)

    def close(self) -> None:
        self.closed = True


def _install_fake_listener(
    monkeypatch: pytest.MonkeyPatch,
    listener: _FakeListener,
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def factory(*args: object) -> _FakeListener:
        calls.append(args)
        return listener

    monkeypatch.setattr(browser.socket, "socket", factory)
    monkeypatch.setattr(browser.secrets, "token_urlsafe", lambda _: "A" * 32)
    return calls


def _http_request(target: str, host: str, *, extra_headers: str = "", body: bytes = b"") -> bytes:
    return f"GET {target} HTTP/1.1\r\nHost: {host}\r\n{extra_headers}\r\n".encode("ascii") + body


@pytest.mark.parametrize(
    "clock",
    [
        lambda: True,
        lambda: float("nan"),
        lambda: float("inf"),
        lambda: -float("inf"),
        lambda: sys.float_info.max,
        lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE-CLOCK-FAILURE")),
    ],
    ids=("bool", "nan", "positive-infinity", "negative-infinity", "rounded-deadline", "raises"),
)
def test_loopback_rejects_invalid_clock_before_creating_fake_socket(
    monkeypatch: pytest.MonkeyPatch, clock: object
) -> None:
    listener = _FakeListener([])
    socket_calls = _install_fake_listener(monkeypatch, listener)

    with pytest.raises(AdhelmError) as raised:
        browser.GoogleOAuthLoopbackReceiver(clock=clock)  # type: ignore[arg-type]

    assert raised.value.code == "GOOGLE_OAUTH_CLOCK_INVALID"
    assert "PRIVATE-CLOCK-FAILURE" not in str(raised.value)
    assert socket_calls == []
    assert not listener.closed


def test_loopback_rejects_decreasing_clock_before_accept_and_closes_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _FakeListener([])
    _install_fake_listener(monkeypatch, listener)
    samples = iter((1.0, 0.5))
    receiver = browser.GoogleOAuthLoopbackReceiver(clock=lambda: next(samples))

    with pytest.raises(AdhelmError) as raised:
        receiver.receive_query()

    assert raised.value.code == "GOOGLE_OAUTH_CALLBACK_EXPIRED"
    assert listener.accept_calls == 0
    assert listener.closed


@pytest.mark.parametrize(
    "random_callback",
    [
        lambda _: "A" * 31,
        lambda _: "!" * 32,
        lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE-ENTROPY-FAILURE")),
    ],
    ids=("short", "non-url-safe", "raises"),
)
def test_loopback_rejects_bad_callback_randomness_before_fake_socket(
    monkeypatch: pytest.MonkeyPatch, random_callback: Callable[[int], str]
) -> None:
    listener = _FakeListener([])
    socket_calls: list[tuple[object, ...]] = []

    def factory(*args: object) -> _FakeListener:
        socket_calls.append(args)
        return listener

    monkeypatch.setattr(browser.socket, "socket", factory)
    monkeypatch.setattr(browser.secrets, "token_urlsafe", random_callback)

    with pytest.raises(AdhelmError) as raised:
        browser.GoogleOAuthLoopbackReceiver(clock=lambda: 1.0)

    assert raised.value.code == "GOOGLE_OAUTH_RANDOMNESS_UNAVAILABLE"
    assert "PRIVATE-ENTROPY-FAILURE" not in str(raised.value)
    assert socket_calls == []
    assert not listener.closed


def test_browser_requires_current_revalidated_attempt_before_fake_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher_calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.setenv("BROWSER", "/private/attacker/browser")
    monkeypatch.setenv("PATH", "/private/attacker/bin")

    def launcher(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        launcher_calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(browser.subprocess, "run", launcher)
    monkeypatch.setattr(protocol, "_utc_now", lambda: _ISSUED_AT + timedelta(seconds=1))
    attempt = _attempt("http://127.0.0.1:43123/oauth/callback/" + "A" * 32)

    browser.open_system_browser(attempt)

    assert "now" not in inspect.signature(browser.open_system_browser).parameters
    assert "now" not in inspect.signature(protocol.google_oauth_authorization_url).parameters
    assert len(launcher_calls) == 1
    arguments, kwargs = launcher_calls[0]
    assert arguments[0:2] == ["/usr/bin/open", "--"]
    assert arguments[2].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "PRIVATE" not in arguments[2]
    assert kwargs["shell"] is False
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
    assert kwargs["timeout"] == 10


def test_browser_rejects_forged_or_expired_attempt_without_fake_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(browser.subprocess, "run", lambda *_args, **_kwargs: calls.append(object()))
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    valid = _attempt("http://127.0.0.1:43123/oauth/callback/" + "A" * 32)
    forged = object.__new__(GoogleOAuthAuthorizationAttempt)
    for name, value in (
        ("target", valid.target),
        ("redirect_uri", valid.redirect_uri),
        ("state", "not-a-32-byte-state"),
        ("nonce", valid.nonce),
        ("code_verifier", valid.code_verifier),
        ("issued_at", valid.issued_at),
        ("expires_at", valid.expires_at),
    ):
        object.__setattr__(forged, name, value)

    with pytest.raises(AdhelmError) as forged_error:
        browser.open_system_browser(forged)
    assert forged_error.value.code == "GOOGLE_OAUTH_AUTHORIZATION_INVALID"
    assert calls == []

    monkeypatch.setattr(protocol, "_utc_now", lambda: valid.expires_at)
    with pytest.raises(AdhelmError) as expired_error:
        browser.open_system_browser(valid)
    assert expired_error.value.code == "GOOGLE_OAUTH_AUTHORIZATION_EXPIRED"
    assert calls == []


def test_callback_rejects_http_smuggling_forms_without_reflecting_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _FakeListener([])
    _install_fake_listener(monkeypatch, listener)
    receiver = browser.GoogleOAuthLoopbackReceiver(clock=lambda: 1.0)
    attempt = _attempt(receiver.redirect_uri)
    private_code = "PRIVATE-CALLBACK-CODE"
    invalid_requests = [
        _http_request(f"{receiver._path}?state=wrong#", receiver._host),
        _http_request(f"{receiver._path}?state=wrong", receiver._host + ":80"),
        _http_request(
            f"{receiver._path}?state=wrong",
            receiver._host,
            extra_headers="Origin: https://accounts.google.com\r\n",
        ),
        _http_request(
            f"{receiver._path}?state=wrong",
            receiver._host,
            extra_headers="Content-Length: 1\r\n",
            body=b"x",
        ),
    ]
    valid_query = f"code={private_code}&state={attempt.state}"
    valid_request = _http_request(f"{receiver._path}?{valid_query}", receiver._host)
    connections = [_FakeConnection(request) for request in invalid_requests]
    accepted = [(connection, ("127.0.0.1", 50000)) for connection in connections]
    valid_connection = _FakeConnection(valid_request)
    accepted.append((valid_connection, ("127.0.0.1", 50001)))
    listener._accepted.extend(accepted)

    query = receiver.receive_query()

    assert query == valid_query
    assert listener.closed and receiver._socket is None
    for connection in connections:
        assert connection.closed
        assert connection.responses and b"400 Bad Request" in connection.responses[0]
        assert private_code.encode() not in connection.responses[0]
    assert valid_connection.responses and b"200 OK" in valid_connection.responses[0]
    assert private_code.encode() not in valid_connection.responses[0]
    assert attempt.state.encode() not in valid_connection.responses[0]
    with pytest.raises(AdhelmError) as consumed:
        receiver.receive_query()
    assert consumed.value.code == "GOOGLE_OAUTH_CALLBACK_CONSUMED"


def test_callback_http_success_does_not_treat_duplicate_state_as_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _FakeListener([])
    _install_fake_listener(monkeypatch, listener)
    receiver = browser.GoogleOAuthLoopbackReceiver(clock=lambda: 1.0)
    attempt = _attempt(receiver.redirect_uri)
    private_code = "PRIVATE-CALLBACK-CODE"
    query = f"code={private_code}&state={attempt.state}&state=attacker"
    connection = _FakeConnection(_http_request(f"{receiver._path}?{query}", receiver._host))
    listener._accepted.append((connection, ("127.0.0.1", 50000)))
    monkeypatch.setattr(protocol, "_utc_now", lambda: _ISSUED_AT + timedelta(seconds=1))

    raw_query = receiver.receive_query()
    with pytest.raises(AdhelmError) as invalid_callback:
        consume_google_oauth_callback(attempt, raw_query)

    assert raw_query == query
    assert invalid_callback.value.code == "GOOGLE_OAUTH_CALLBACK_INVALID"
    assert private_code not in str(invalid_callback.value)
    assert connection.responses and b"200 OK" in connection.responses[0]
    assert private_code.encode() not in connection.responses[0]


def test_callback_rechecks_deadline_after_reply_before_returning_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _FakeListener([])
    _install_fake_listener(monkeypatch, listener)
    clock = [299.0]
    receiver = browser.GoogleOAuthLoopbackReceiver(clock=lambda: clock[0])
    attempt = _attempt(receiver.redirect_uri)
    query = f"code=PRIVATE-CALLBACK-CODE&state={attempt.state}"
    connection = _FakeConnection(
        _http_request(f"{receiver._path}?{query}", receiver._host),
        after_reply=lambda: clock.__setitem__(0, 599.0),
    )
    listener._accepted.append((connection, ("127.0.0.1", 50000)))

    with pytest.raises(AdhelmError) as expired:
        receiver.receive_query()

    assert expired.value.code == "GOOGLE_OAUTH_CALLBACK_EXPIRED"
    assert listener.closed
    assert connection.responses and b"200 OK" in connection.responses[0]

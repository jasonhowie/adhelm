"""Explicit system-browser handoff and a bounded loopback-only OAuth callback.

No listener, process, or browser is created by import. Callback bytes and the
authorization URL are never logged, reflected, or returned as CLI output.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from types import TracebackType
from urllib.parse import urlsplit

from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GoogleOAuthAuthorizationAttempt,
    google_oauth_authorization_url,
)

_MAX_HEADERS = 16_384
_MAX_REQUESTS = 16
_TOTAL_SECONDS = 300
_RESPONSE_BODY = b"Return to the local CLI to check the authorization result."


def open_system_browser(attempt: GoogleOAuthAuthorizationAttempt) -> None:
    """Open only a revalidated private authorization attempt using an OS launcher."""
    authorization_url = google_oauth_authorization_url(attempt)
    try:
        parsed = urlsplit(authorization_url)
    except ValueError:
        raise AdhelmError(
            "GOOGLE_OAUTH_BROWSER_URL_INVALID", "Invalid authorization handoff."
        ) from None
    if (
        type(authorization_url) is not str
        or len(authorization_url) > 16_384
        or parsed.scheme != "https"
        or parsed.netloc != "accounts.google.com"
        or parsed.path != "/o/oauth2/v2/auth"
        or not parsed.query
        or "#" in authorization_url
        or any(ord(character) < 33 or ord(character) > 126 for character in authorization_url)
    ):
        raise AdhelmError("GOOGLE_OAUTH_BROWSER_URL_INVALID", "Invalid authorization handoff.")
    try:
        if sys.platform == "win32":
            os.startfile(authorization_url)
            return
        environment = {"PATH": "/usr/bin:/bin"}
        arguments: list[str]
        if sys.platform == "darwin":
            arguments = ["/usr/bin/open", "--", authorization_url]
        elif sys.platform.startswith("linux"):
            # Desktop session routing is needed, but executable selection and
            # shell/browser injection environment variables are never forwarded.
            for name in (
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
                "XDG_CURRENT_DESKTOP",
                "DESKTOP_SESSION",
            ):
                if name in os.environ:
                    environment[name] = os.environ[name]
            arguments = ["/usr/bin/xdg-open", authorization_url]
        else:
            raise _browser_unavailable()
        result = subprocess.run(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise _browser_unavailable()
    except Exception:
        raise _browser_unavailable() from None


def _browser_unavailable() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_BROWSER_UNAVAILABLE",
        "The operating-system browser handoff did not complete.",
        exit_code=4,
        next_action="Use a supported local desktop session and start a deliberate new enrollment.",
    )


class GoogleOAuthLoopbackReceiver:
    """A single-use local listener; HTTP validity does not establish OAuth identity."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = _sample_clock(clock)
        if type(self._started) not in {int, float} or not math.isfinite(self._started):
            raise AdhelmError("GOOGLE_OAUTH_CLOCK_INVALID", "A trusted local clock is unavailable.")
        self._deadline = self._started + _TOTAL_SECONDS
        if not math.isfinite(self._deadline) or self._deadline <= self._started:
            raise AdhelmError("GOOGLE_OAUTH_CLOCK_INVALID", "A trusted local clock is unavailable.")
        self._last_clock = self._started
        try:
            callback_nonce = secrets.token_urlsafe(24)
            if (
                type(callback_nonce) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{32}", callback_nonce) is None
            ):
                raise ValueError("Invalid callback randomness")
        except Exception:
            raise AdhelmError(
                "GOOGLE_OAUTH_RANDOMNESS_UNAVAILABLE",
                "Private callback randomness is unavailable.",
                exit_code=4,
            ) from None
        self._path = "/oauth/callback/" + callback_nonce
        self._used = False
        self._socket: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket = listener
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid loopback port")
            self._host = f"127.0.0.1:{port}"
            self.redirect_uri = f"http://{self._host}{self._path}"
        except Exception:
            self.close()
            raise AdhelmError(
                "GOOGLE_OAUTH_LOOPBACK_UNAVAILABLE",
                "A private loopback callback could not be opened.",
                exit_code=4,
            ) from None

    def __enter__(self) -> GoogleOAuthLoopbackReceiver:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        listener, self._socket = self._socket, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _now(self) -> float:
        now = _sample_clock(self._clock)
        if (
            type(now) not in {int, float}
            or not math.isfinite(now)
            or not self._last_clock <= now < self._deadline
        ):
            raise AdhelmError(
                "GOOGLE_OAUTH_CALLBACK_EXPIRED",
                "The bounded browser authorization window ended.",
                exit_code=4,
            )
        self._last_clock = now
        return now

    def _remaining(self) -> float:
        return self._deadline - self._now()

    def require_unexpired(self) -> None:
        """Recheck the same local deadline after callback receipt and before exchange."""
        self._remaining()

    def receive_query(self) -> str:
        """Consume one HTTP callback; caller must then verify state/code/nonce."""
        if self._used or self._socket is None:
            raise AdhelmError("GOOGLE_OAUTH_CALLBACK_CONSUMED", "The callback is already closed.")
        self._used = True
        try:
            for _ in range(_MAX_REQUESTS):
                self._socket.settimeout(self._remaining())
                try:
                    connection, peer = self._socket.accept()
                except TimeoutError:
                    raise AdhelmError(
                        "GOOGLE_OAUTH_CALLBACK_EXPIRED",
                        "The bounded browser authorization window ended.",
                        exit_code=4,
                    ) from None
                with connection:
                    if peer[0] != "127.0.0.1":
                        continue
                    try:
                        query = self._read_request(connection)
                    except (ValueError, OSError):
                        self._reply(connection, accepted=False)
                        continue
                    self._remaining()
                    self._reply(connection, accepted=True)
                    self._remaining()
                    return query
            raise AdhelmError(
                "GOOGLE_OAUTH_CALLBACK_REJECTED",
                "No valid callback arrived within the bounded request allowance.",
                exit_code=4,
            )
        except AdhelmError:
            raise
        except OSError:
            raise AdhelmError(
                "GOOGLE_OAUTH_CALLBACK_UNAVAILABLE", "The loopback callback failed.", exit_code=4
            ) from None
        finally:
            # Close before any token exchange. A code is never received twice.
            self.close()

    def _read_request(self, connection: socket.socket) -> str:
        request = bytearray()
        connection_deadline = min(self._now() + 5.0, self._deadline)
        while b"\r\n\r\n" not in request:
            remaining = min(connection_deadline - self._now(), self._remaining())
            if remaining <= 0 or len(request) >= _MAX_HEADERS:
                raise ValueError("Request bound exceeded")
            connection.settimeout(remaining)
            chunk = connection.recv(min(4096, _MAX_HEADERS - len(request)))
            if not chunk:
                raise ValueError("Incomplete request")
            request.extend(chunk)
        header, body = bytes(request).split(b"\r\n\r\n", 1)
        if body:
            raise ValueError("Request body is forbidden")
        lines = header.decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
        if method != "GET" or version != "HTTP/1.1" or len(target) > 8192:
            raise ValueError("Unsupported request line")
        if any(ord(c) < 33 or ord(c) > 126 for c in target):
            raise ValueError("Invalid target")
        parsed = urlsplit(target)
        if (
            not target.startswith("/")
            or parsed.scheme
            or parsed.netloc
            or parsed.path != self._path
            or "#" in target
            or not parsed.query
        ):
            raise ValueError("Incorrect callback target")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or not name or not all(c.isalnum() or c == "-" for c in name):
                raise ValueError("Invalid header")
            normalized = name.lower()
            if normalized in headers or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Invalid or duplicate header")
            headers[normalized] = value.strip(" ")
        if (
            headers.get("host") != self._host
            or "transfer-encoding" in headers
            or headers.get("content-length", "0") != "0"
            or "origin" in headers
        ):
            raise ValueError("Unsupported callback headers")
        return parsed.query

    @staticmethod
    def _reply(connection: socket.socket, *, accepted: bool) -> None:
        status = b"200 OK" if accepted else b"400 Bad Request"
        response = (
            b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
            b"Content-Security-Policy: default-src 'none'\r\nConnection: close\r\n"
            b"Content-Length: " + str(len(_RESPONSE_BODY)).encode() + b"\r\n\r\n" + _RESPONSE_BODY
        )
        try:
            connection.sendall(response)
        except OSError:
            pass


def _sample_clock(clock: Callable[[], float]) -> float:
    try:
        value = clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("Invalid clock sample")
        return value
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_CLOCK_INVALID", "A trusted local clock is unavailable."
        ) from None

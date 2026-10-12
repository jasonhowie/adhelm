"""Fixed, secret-safe HTTP boundary for Google desktop OAuth.

This module is deliberately separate from :mod:`adhelm.transport`.  OAuth token
forms contain refresh tokens and, for some installed client registrations, a
client secret.  The regular provider transport correctly rejects such fields
at its generic privacy boundary.  Nothing here is a general HTTP client: it
can issue only the one token POST and the one certificate GET used by the
maintained ``google-auth`` verifier.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Final, Self
from urllib.parse import urlencode, urlsplit

import httpx

from adhelm.errors import ExitCode
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_CERTS_ENDPOINT,
    GOOGLE_OAUTH_TOKEN_ENDPOINT,
    GoogleOAuthCertificateTransport,
    GoogleOAuthTokenDispatchState,
    GoogleOAuthTransportError,
)

_TOKEN_HOST: Final = "oauth2.googleapis.com"
_CERTS_HOST: Final = "www.googleapis.com"
_REQUEST_TIMEOUT_SECONDS: Final = 30.0
# ``httpx`` applies this timeout to an individual blocking socket read.  Keep
# a separate elapsed budget so a peer cannot retain the credential lock by
# slowly sending many individually timely chunks.
_RESPONSE_ELAPSED_BUDGET_SECONDS: Final = 30.0
_MAX_TOKEN_RESPONSE_BYTES: Final = 65_536
_MAX_CERTIFICATE_RESPONSE_BYTES: Final = 1_048_576
_MAX_FORM_VALUE_BYTES: Final = 16_384
_TOKEN_FORM_KEYS: Final = frozenset(
    {
        "client_id",
        "client_secret",
        "code",
        "code_verifier",
        "grant_type",
        "redirect_uri",
        "refresh_token",
    }
)


class GoogleOAuthCertificateResponse:
    """The minimal response shape expected by ``google.auth.transport.Request``."""

    def __init__(self, *, status: int, headers: Mapping[str, str], data: bytes) -> None:
        self._status = status
        self._headers = dict(headers)
        self._data = data

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def data(self) -> bytes:
        return self._data


class GoogleOAuthCertificateRequest:
    """A fixed URL GET adapter for ``google.oauth2.id_token``.

    The verifier may not select an endpoint, method, body, or transport.  Any
    deviation is converted to a normal google-auth transport failure, which
    the protocol sanitizes before it reaches product output.
    """

    def __init__(self, transport: GoogleOAuthCertificateTransport) -> None:
        self._transport = transport

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: object,
    ) -> GoogleOAuthCertificateResponse:
        del timeout
        if (
            type(url) is not str
            or url != GOOGLE_OAUTH_CERTS_ENDPOINT
            or method != "GET"
            or body not in (None, b"")
            or headers not in (None, {})
            or kwargs
        ):
            raise _certificate_transport_error(
                "Google OAuth certificate request is outside the fixed contract."
            )
        try:
            data = self._transport.get_google_oauth_certificates()
        except GoogleOAuthTransportError:
            raise _certificate_transport_error("Google OAuth certificate request failed.") from None
        except Exception:
            raise _certificate_transport_error("Google OAuth certificate request failed.") from None
        if type(data) is not bytes:
            raise _certificate_transport_error("Google OAuth certificate response is invalid.")
        return GoogleOAuthCertificateResponse(status=200, headers={}, data=data)


class FixedGoogleOAuthTransport(AbstractContextManager["FixedGoogleOAuthTransport"]):
    """One explicitly constructed fixed-host OAuth transport session.

    ``open()`` is the production constructor.  Direct construction is a
    fake-client test seam only; callers must not treat a supplied ``httpx``
    client as an independently validated production transport.
    """

    def __init__(self, client: httpx.Client) -> None:
        if type(client) is not httpx.Client:
            raise GoogleOAuthTransportError(
                "GOOGLE_OAUTH_TRANSPORT_INVALID",
                "Google OAuth transport is unavailable.",
                dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
                exit_code=ExitCode.INVALID_INPUT,
            )
        self._client = client

    @classmethod
    def open(cls) -> Self:
        """Create the sole production client shape: no environment or redirects."""
        return cls(httpx.Client(trust_env=False, follow_redirects=False))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        checked = _checked_token_form(fields)
        encoded = urlencode(dict(checked)).encode("ascii")
        deadline = _new_response_elapsed_budget(token_operation=True)
        try:
            with self._client.stream(
                "POST",
                GOOGLE_OAUTH_TOKEN_ENDPOINT,
                content=encoded,
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                },
                follow_redirects=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                deadline.checkpoint()
                if 300 <= response.status_code < 400:
                    raise _uncertain_token_error()
                if response.status_code in {400, 401, 403}:
                    raise GoogleOAuthTransportError(
                        "GOOGLE_OAUTH_TOKEN_REJECTED",
                        "Google OAuth rejected the local credential exchange.",
                        dispatch_state=GoogleOAuthTokenDispatchState.DEFINITE_RESPONSE,
                    )
                if response.status_code >= 500:
                    raise _uncertain_token_error()
                if response.status_code != 200:
                    raise GoogleOAuthTransportError(
                        "GOOGLE_OAUTH_TOKEN_REJECTED",
                        "Google OAuth rejected the local credential exchange.",
                        dispatch_state=GoogleOAuthTokenDispatchState.DEFINITE_RESPONSE,
                    )
                if not _json_content_type(response.headers.get("content-type")):
                    raise _uncertain_token_error()
                return _bounded_response_body(
                    response,
                    maximum_bytes=_MAX_TOKEN_RESPONSE_BYTES,
                    token_operation=True,
                    deadline=deadline,
                )
        except GoogleOAuthTransportError:
            raise
        except httpx.HTTPError:
            raise _uncertain_token_error() from None
        except Exception:
            raise _uncertain_token_error() from None

    def get_google_oauth_certificates(self) -> bytes:
        deadline = _new_response_elapsed_budget(token_operation=False)
        try:
            with self._client.stream(
                "GET",
                GOOGLE_OAUTH_CERTS_ENDPOINT,
                headers={"accept": "application/json"},
                follow_redirects=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                deadline.checkpoint()
                if response.status_code != 200:
                    raise GoogleOAuthTransportError(
                        "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE",
                        "Google identity verification certificates are unavailable.",
                        dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
                    )
                if not _json_content_type(response.headers.get("content-type")):
                    raise GoogleOAuthTransportError(
                        "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE",
                        "Google identity verification certificates are unavailable.",
                        dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
                    )
                return _bounded_response_body(
                    response,
                    maximum_bytes=_MAX_CERTIFICATE_RESPONSE_BYTES,
                    token_operation=False,
                    deadline=deadline,
                )
        except GoogleOAuthTransportError:
            raise
        except httpx.HTTPError:
            raise GoogleOAuthTransportError(
                "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE",
                "Google identity verification certificates are unavailable.",
                dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
            ) from None
        except Exception:
            raise GoogleOAuthTransportError(
                "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE",
                "Google identity verification certificates are unavailable.",
                dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
            ) from None


def _checked_token_form(fields: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    if type(fields) is not tuple or not 1 <= len(fields) <= 7:
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_TOKEN_FORM_INVALID",
            "Google OAuth request cannot be prepared.",
            dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
            exit_code=ExitCode.INVALID_INPUT,
        )
    names: set[str] = set()
    checked: list[tuple[str, str]] = []
    for item in fields:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            or item[0] not in _TOKEN_FORM_KEYS
            or item[0] in names
            or not 1 <= len(item[1].encode("utf-8")) <= _MAX_FORM_VALUE_BYTES
            or any(ord(character) < 32 for character in item[1])
        ):
            raise GoogleOAuthTransportError(
                "GOOGLE_OAUTH_TOKEN_FORM_INVALID",
                "Google OAuth request cannot be prepared.",
                dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
                exit_code=ExitCode.INVALID_INPUT,
            )
        names.add(item[0])
        checked.append(item)
    return tuple(checked)


def _bounded_response_body(
    response: httpx.Response,
    *,
    maximum_bytes: int,
    token_operation: bool,
    deadline: _ResponseElapsedBudget,
) -> bytes:
    data = bytearray()
    for chunk in response.iter_bytes():
        if len(data) + len(chunk) > maximum_bytes:
            if token_operation:
                raise _uncertain_token_error()
            raise GoogleOAuthTransportError(
                "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE",
                "Google identity verification certificates are unavailable.",
                dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
            )
        data.extend(chunk)
        deadline.checkpoint()
    deadline.checkpoint()
    return bytes(data)


class _ResponseElapsedBudget:
    """Private monotonic deadline for one fixed response body."""

    def __init__(self, *, started_at: float, token_operation: bool) -> None:
        self._started_at = started_at
        self._last_checked_at = started_at
        self._token_operation = token_operation

    def checkpoint(self) -> None:
        try:
            current = _checked_monotonic()
        except Exception:
            raise _elapsed_budget_failure(token_operation=self._token_operation) from None
        if (
            current < self._last_checked_at
            or current - self._started_at >= _RESPONSE_ELAPSED_BUDGET_SECONDS
        ):
            raise _elapsed_budget_failure(token_operation=self._token_operation)
        self._last_checked_at = current


def _new_response_elapsed_budget(*, token_operation: bool) -> _ResponseElapsedBudget:
    """Start before fixed dispatch, while a clock failure is still pre-egress."""
    try:
        started_at = _checked_monotonic()
    except Exception:
        if token_operation:
            raise GoogleOAuthTransportError(
                "GOOGLE_OAUTH_TRANSPORT_CLOCK_UNAVAILABLE",
                (
                    "Google OAuth token exchange cannot start with the local transport "
                    "clock unavailable."
                ),
                dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
                exit_code=ExitCode.PRECONDITION,
            ) from None
        raise _certificate_unavailable_error() from None
    return _ResponseElapsedBudget(started_at=started_at, token_operation=token_operation)


def _monotonic() -> float:
    """Private fake-only seam for whole-response budget regressions."""
    return time.monotonic()


def _checked_monotonic() -> float:
    value = _monotonic()
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("monotonic clock is invalid")
    return float(value)


def _elapsed_budget_failure(*, token_operation: bool) -> GoogleOAuthTransportError:
    if token_operation:
        # The POST was initiated before every checkpoint, so a deadline or
        # clock failure here cannot safely permit an automatic retry.
        return _uncertain_token_error()
    return _certificate_unavailable_error()


def _certificate_unavailable_error() -> GoogleOAuthTransportError:
    return GoogleOAuthTransportError(
        "GOOGLE_OAUTH_CERTIFICATES_UNAVAILABLE",
        "Google identity verification certificates are unavailable.",
        dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
    )


def _json_content_type(value: object) -> bool:
    if type(value) is not str or len(value) > 256 or any(ord(c) < 32 for c in value):
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def _uncertain_token_error() -> GoogleOAuthTransportError:
    return GoogleOAuthTransportError(
        "GOOGLE_OAUTH_TOKEN_OUTCOME_UNCERTAIN",
        "Google OAuth token exchange outcome is uncertain; reconnect before another attempt.",
        dispatch_state=GoogleOAuthTokenDispatchState.UNCERTAIN,
        exit_code=ExitCode.AMBIGUOUS,
    )


def _certificate_transport_error(message: str) -> Exception:
    """Defer google-auth import until OIDC verification is actually requested."""
    from google.auth import exceptions as google_auth_exceptions

    return google_auth_exceptions.TransportError(message)  # type: ignore[no-untyped-call]


def is_fixed_google_oauth_endpoint(value: str, *, certificates: bool = False) -> bool:
    """Internal testable endpoint guard; it never accepts caller-selected URLs."""
    expected = GOOGLE_OAUTH_CERTS_ENDPOINT if certificates else GOOGLE_OAUTH_TOKEN_ENDPOINT
    expected_host = _CERTS_HOST if certificates else _TOKEN_HOST
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        value == expected
        and parsed.scheme == "https"
        and parsed.hostname == expected_host
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )

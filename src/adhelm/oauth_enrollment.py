"""Provider-parameterized OAuth authorization-code enrollment.

The mechanics here are generalized from the fixed Google desktop OAuth modules
(``google_oauth_protocol``, ``google_oauth_browser``, ``google_oauth_transport``)
so a second provider can reuse the same loopback redirect listener, state nonce,
optional PKCE, token exchange and refresh grant without copying them.  The
Google modules keep their own specialized implementation: their OIDC identity
verification, exact two-scope contract and protected bundle lifecycle are not
generalized here, and this module does not change Google behavior.

A provider contract declares how the client authenticates at the token endpoint.
``FORM_SECRET`` sends the client ID and secret in the form body.  ``BASIC_SECRET``
sends them as the HTTP Basic user name and password (``-u '<client ID>:<secret>'``)
and keeps both out of the body.  Both are confidential-client forms: every
contract here needs the customer application's secret.

Nothing in this module logs, echoes, serializes or returns a client secret,
authorization code, access token or refresh token.  Secret-bearing values are
private frozen dataclasses with no public serialization method, and every
provider HTTP call is a fixed-endpoint POST issued by :class:`FixedOAuthTokenTransport`.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Final, Literal, Never, Protocol, Self, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from adhelm.errors import AdhelmError, ExitCode

_SCOPE_TEXT: Final = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_SECRET_TEXT: Final = re.compile(r"^[\x21-\x7e]{1,16384}$")
_AUTH_CODE: Final = re.compile(r"^[\x21-\x7e]{1,4096}$")
_BASE64URL: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_PKCE_VERIFIER: Final = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CALLBACK_PATH: Final = re.compile(r"^/[A-Za-z0-9_/-]{1,120}$")
_PROVIDER_KEY: Final = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_PROMPT_LABEL: Final = re.compile(r"^[A-Za-z0-9 ()/-]{1,60}: $")
_USER_AGENT: Final = re.compile(r"^[\x21-\x7e]{1,120}$")

_STATE_BYTES: Final = 32
_PKCE_RANDOM_BYTES: Final = 64
_FLOW_LIFETIME: Final = timedelta(minutes=10)
_MAX_TOKEN_RESPONSE_BYTES: Final = 65_536
_MAX_CALLBACK_QUERY_BYTES: Final = 8_192
_MAX_FORM_VALUE_BYTES: Final = 16_384
_MAX_SCOPE_TEXT_BYTES: Final = 512
_MAX_SCOPES: Final = 16
_REQUEST_TIMEOUT_SECONDS: Final = 30.0
_RESPONSE_ELAPSED_BUDGET_SECONDS: Final = 30.0
_MAX_SECRET_CHARS: Final = 16_384

_LOOPBACK_MAX_HEADER_BYTES: Final = 16_384
_LOOPBACK_MAX_REQUESTS: Final = 16
_LOOPBACK_TOTAL_SECONDS: Final = 300
_LOOPBACK_RESPONSE_BODY: Final = b"Return to the local CLI to check the authorization result."

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
_TOKEN_RESPONSE_KEYS: Final = frozenset(
    {
        "access_token",
        "expires_in",
        "token_type",
        "scope",
        "refresh_token",
        "refresh_token_expires_in",
    }
)
_CALLBACK_KEYS: Final = frozenset({"code", "state", "error", "error_description", "error_uri"})

RefreshStatus = Literal["GRANTED", "REFRESH_NOT_GRANTED"]
ClientAuthentication = Literal["FORM_SECRET", "BASIC_SECRET"]


class OAuthTokenDispatchState(StrEnum):
    """Whether a token request may have reached the provider."""

    NOT_DISPATCHED = "NOT_DISPATCHED"
    DEFINITE_RESPONSE = "DEFINITE_RESPONSE"
    UNCERTAIN = "UNCERTAIN"


class OAuthTransportError(AdhelmError):
    """Sanitized token-endpoint error carrying only its dispatch classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        dispatch_state: OAuthTokenDispatchState,
        exit_code: int = ExitCode.AUTHORIZATION,
        next_action: str = "Re-enroll the configured local profile deliberately.",
    ) -> None:
        super().__init__(code, message, exit_code=exit_code, next_action=next_action)
        self.dispatch_state = dispatch_state


class OAuthTokenTransport(Protocol):
    """Opaque token-form seam used by this module and fake-only tests."""

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes: ...


@dataclass(frozen=True, slots=True)
class OAuthProviderContract:
    """One provider's fixed, reviewed authorization-code wire contract.

    Every field is public non-secret capability data.  A provider that does not
    document PKCE gets ``pkce_method="NONE"``; this module never sends an
    undocumented parameter to make a flow look uniform.
    """

    provider: str
    authorization_endpoint: str
    token_endpoint: str
    supported_scopes: frozenset[str]
    pkce_method: Literal["S256", "NONE"] = "NONE"
    refresh_grant_documented: bool = True
    authorization_parameters: tuple[tuple[str, str], ...] = ()
    scope_request_separator: str = " "
    scope_response_separators: tuple[str, ...] = (" ",)
    maximum_access_token_lifetime_seconds: int = 86_400
    maximum_refresh_token_lifetime_seconds: int = 315_576_000
    client_authentication: ClientAuthentication = "FORM_SECRET"
    user_agent: str | None = None

    def __post_init__(self) -> None:
        if type(self.provider) is not str or _PROVIDER_KEY.fullmatch(self.provider) is None:
            raise ValueError("OAuth provider key is invalid")
        _validate_https_endpoint(self.authorization_endpoint)
        _validate_https_endpoint(self.token_endpoint)
        if (
            type(self.supported_scopes) is not frozenset
            or not 1 <= len(self.supported_scopes) <= _MAX_SCOPES
            or any(_SCOPE_TEXT.fullmatch(scope) is None for scope in self.supported_scopes)
        ):
            raise ValueError("OAuth supported scopes are invalid")
        if self.pkce_method not in {"S256", "NONE"}:
            raise ValueError("OAuth PKCE method is unsupported")
        if type(self.refresh_grant_documented) is not bool:
            raise ValueError("OAuth refresh-grant support flag is invalid")
        if type(self.authorization_parameters) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            or not item[0]
            or not item[1]
            for item in self.authorization_parameters
        ):
            raise ValueError("OAuth authorization parameters are invalid")
        reserved = {
            "response_type",
            "client_id",
            "redirect_uri",
            "scope",
            "state",
            "code_challenge",
            "code_challenge_method",
        }
        names = [name for name, _ in self.authorization_parameters]
        if len(names) != len(set(names)) or reserved & set(names):
            raise ValueError("OAuth authorization parameters are invalid")
        if self.scope_request_separator not in {" ", ","}:
            raise ValueError("OAuth scope request separator is unsupported")
        if (
            type(self.scope_response_separators) is not tuple
            or not self.scope_response_separators
            or any(item not in {" ", ","} for item in self.scope_response_separators)
        ):
            raise ValueError("OAuth scope response separators are unsupported")
        if (
            type(self.maximum_access_token_lifetime_seconds) is not int
            or not 1 <= self.maximum_access_token_lifetime_seconds <= 315_576_000
            or type(self.maximum_refresh_token_lifetime_seconds) is not int
            or not 1 <= self.maximum_refresh_token_lifetime_seconds <= 315_576_000
        ):
            raise ValueError("OAuth token lifetime bounds are invalid")
        if self.client_authentication not in {"FORM_SECRET", "BASIC_SECRET"}:
            raise ValueError("OAuth client authentication method is unsupported")
        if self.user_agent is not None and (
            type(self.user_agent) is not str or _USER_AGENT.fullmatch(self.user_agent) is None
        ):
            raise ValueError("OAuth user agent is invalid")

    @property
    def token_host(self) -> str:
        host = urlsplit(self.token_endpoint).hostname
        if type(host) is not str:
            raise ValueError("OAuth token endpoint host is invalid")
        return host

    @property
    def authorization_host(self) -> str:
        host = urlsplit(self.authorization_endpoint).hostname
        if type(host) is not str:
            raise ValueError("OAuth authorization endpoint host is invalid")
        return host


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationTarget:
    """The one public, non-secret target selected from a validated profile."""

    contract: OAuthProviderContract
    client_ref: str
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.contract) is not OAuthProviderContract:
            raise ValueError("OAuth provider contract is invalid")
        if (
            type(self.client_ref) is not str
            or not 1 <= len(self.client_ref) <= 200
            or any(not 33 <= ord(character) <= 126 for character in self.client_ref)
        ):
            raise ValueError("OAuth client reference is invalid")
        if self.contract.client_authentication == "BASIC_SECRET" and ":" in self.client_ref:
            # HTTP Basic splits the credential at its first colon, so a colon in
            # the ID would move the split.  Refuse it here, before any browser
            # handoff, rather than only when the token request is built.
            raise ValueError("OAuth client reference cannot contain a colon for HTTP Basic")
        if (
            type(self.scopes) is not tuple
            or not 1 <= len(self.scopes) <= _MAX_SCOPES
            or len(set(self.scopes)) != len(self.scopes)
            or any(type(scope) is not str for scope in self.scopes)
            or not set(self.scopes) <= self.contract.supported_scopes
            or list(self.scopes) != sorted(self.scopes)
        ):
            raise ValueError("OAuth requested scopes are invalid")


@dataclass(frozen=True, slots=True, repr=False)
class OAuthAuthorizationAttempt:
    """One in-memory state-bound (and optionally PKCE-bound) authorization attempt."""

    target: OAuthAuthorizationTarget
    redirect_uri: str
    state: str
    code_verifier: str | None
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.target) is not OAuthAuthorizationTarget:
            raise ValueError("OAuth authorization target is invalid")
        _validate_loopback_redirect_uri(self.redirect_uri)
        _decode_exact_base64url(self.state, expected_bytes=_STATE_BYTES)
        if self.target.contract.pkce_method == "S256":
            if type(self.code_verifier) is not str or (
                _PKCE_VERIFIER.fullmatch(self.code_verifier) is None
            ):
                raise ValueError("PKCE verifier is invalid")
        elif self.code_verifier is not None:
            raise ValueError("PKCE verifier is not supported by this provider contract")
        issued_at = _checked_time(self.issued_at)
        expires_at = _checked_time(self.expires_at)
        if expires_at != issued_at + _FLOW_LIFETIME:
            raise ValueError("OAuth authorization attempt lifetime is invalid")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)

    def __repr__(self) -> str:
        return "<OAuthAuthorizationAttempt redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("OAuth authorization attempts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("OAuth authorization attempts cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class OAuthCallbackCode:
    """A callback code bound to one in-memory attempt state."""

    code: str
    state: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _AUTH_CODE.fullmatch(self.code) is None:
            raise ValueError("OAuth authorization code is invalid")
        _decode_exact_base64url(self.state, expected_bytes=_STATE_BYTES)

    def __repr__(self) -> str:
        return "<OAuthCallbackCode redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("OAuth callback codes cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("OAuth callback codes cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class OAuthInitialGrant:
    """One verified initial token exchange; never serialize this object publicly."""

    target: OAuthAuthorizationTarget
    access_token: str
    access_token_expires_at: datetime
    refresh_token: str | None
    refresh_token_expires_at: datetime | None
    refresh_status: RefreshStatus
    granted_scopes: tuple[str, ...]
    unrequested_granted_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.target) is not OAuthAuthorizationTarget:
            raise ValueError("OAuth authorization target is invalid")
        _validate_secret_text(self.access_token)
        access_token_expires_at = _checked_time(self.access_token_expires_at)
        _validate_unrequested_scopes(self.unrequested_granted_scopes, self.target)
        _validate_refresh_pair(
            self.refresh_token, self.refresh_token_expires_at, self.refresh_status
        )
        refresh_token_expires_at = (
            None
            if self.refresh_token_expires_at is None
            else _checked_time(self.refresh_token_expires_at)
        )
        _validate_granted_scopes(self.granted_scopes, self.target)
        object.__setattr__(self, "access_token_expires_at", access_token_expires_at)
        object.__setattr__(self, "refresh_token_expires_at", refresh_token_expires_at)

    def __repr__(self) -> str:
        return "<OAuthInitialGrant redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("OAuth grants cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("OAuth grants cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class OAuthRefreshGrant:
    """One normalized refresh response; it establishes no new identity."""

    target: OAuthAuthorizationTarget
    access_token: str
    access_token_expires_at: datetime
    granted_scopes: tuple[str, ...] | None
    rotated_refresh_token: str | None
    rotated_refresh_token_expires_at: datetime | None
    unrequested_granted_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.target) is not OAuthAuthorizationTarget:
            raise ValueError("OAuth authorization target is invalid")
        _validate_secret_text(self.access_token)
        access_token_expires_at = _checked_time(self.access_token_expires_at)
        _validate_unrequested_scopes(self.unrequested_granted_scopes, self.target)
        if self.granted_scopes is not None:
            _validate_granted_scopes(self.granted_scopes, self.target)
        if self.rotated_refresh_token is not None:
            _validate_secret_text(self.rotated_refresh_token)
        if self.rotated_refresh_token is None and self.rotated_refresh_token_expires_at is not None:
            raise ValueError("OAuth rotated refresh-token expiry is invalid")
        rotated_expires_at = (
            None
            if self.rotated_refresh_token_expires_at is None
            else _checked_time(self.rotated_refresh_token_expires_at)
        )
        object.__setattr__(self, "access_token_expires_at", access_token_expires_at)
        object.__setattr__(self, "rotated_refresh_token_expires_at", rotated_expires_at)

    def __repr__(self) -> str:
        return "<OAuthRefreshGrant redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("OAuth grants cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("OAuth grants cannot be serialized")


def start_authorization(
    target: OAuthAuthorizationTarget, redirect_uri: str
) -> OAuthAuthorizationAttempt:
    """Create one secure authorization attempt after the loopback listener binds.

    The caller must bind its IPv4 loopback listener first and pass the actual
    callback URL.  This helper never launches a browser or emits the URL.
    """
    try:
        checked = _checked_target(target)
        _validate_loopback_redirect_uri(redirect_uri)
        issued_at = _current_time()
        verifier = (
            _random_base64url(_PKCE_RANDOM_BYTES)
            if checked.contract.pkce_method == "S256"
            else None
        )
        return OAuthAuthorizationAttempt(
            target=checked,
            redirect_uri=redirect_uri,
            state=_random_base64url(_STATE_BYTES),
            code_verifier=verifier,
            issued_at=issued_at,
            expires_at=issued_at + _FLOW_LIFETIME,
        )
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "OAUTH_AUTHORIZATION_INVALID",
            "The local OAuth authorization cannot be prepared.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def authorization_url(attempt: OAuthAuthorizationAttempt) -> str:
    """Revalidate one current attempt before handing its exact URL to the browser.

    This is the sole browser-facing URL constructor.  Browser code accepts the
    typed attempt, never an arbitrary URL string.
    """
    checked = _checked_attempt(attempt)
    if _current_time() >= checked.expires_at:
        raise _authorization_expired()
    contract = checked.target.contract
    parameters: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", checked.target.client_ref),
        ("redirect_uri", checked.redirect_uri),
        ("scope", contract.scope_request_separator.join(checked.target.scopes)),
        ("state", checked.state),
    ]
    if contract.pkce_method == "S256":
        if type(checked.code_verifier) is not str:
            raise _authorization_expired()
        parameters.append(("code_challenge", pkce_s256_challenge(checked.code_verifier)))
        parameters.append(("code_challenge_method", "S256"))
    parameters.extend(contract.authorization_parameters)
    return f"{contract.authorization_endpoint}?{urlencode(parameters)}"


def consume_callback(attempt: OAuthAuthorizationAttempt, raw_query: str) -> OAuthCallbackCode:
    """Validate one raw loopback query without exposing provider callback data."""
    checked = _checked_attempt(attempt)
    if _current_time() >= checked.expires_at:
        raise _authorization_expired()
    fields = _parse_callback_query(raw_query)
    state = fields.get("state")
    if type(state) is not str or not hmac.compare_digest(state, checked.state):
        raise AdhelmError(
            "OAUTH_CALLBACK_STATE_MISMATCH",
            "The OAuth callback did not match the local authorization attempt.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local authorization; do not reuse this callback.",
        )
    if "error" in fields:
        raise AdhelmError(
            "OAUTH_AUTHORIZATION_DENIED",
            "The provider authorization did not return a usable local grant.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Review the requested local authorization and retry deliberately.",
        )
    code = fields.get("code")
    if type(code) is not str or _AUTH_CODE.fullmatch(code) is None:
        raise _callback_invalid()
    return OAuthCallbackCode(code=code, state=checked.state)


def exchange_authorization_code(
    attempt: OAuthAuthorizationAttempt,
    callback: OAuthCallbackCode,
    *,
    token_transport: OAuthTokenTransport,
    client_secret: str | None = None,
) -> OAuthInitialGrant:
    """Exchange one callback code for a token response.

    Every contract authenticates the client with the customer application's
    secret, in the form or as the HTTP Basic password (the transport decides
    which), so a missing or malformed secret is refused before any request.
    """
    checked_attempt = _checked_attempt(attempt)
    checked_callback = _checked_callback(callback, checked_attempt)
    current = _current_time()
    if current >= checked_attempt.expires_at:
        raise _authorization_expired()
    form: list[tuple[str, str]] = [
        ("client_id", checked_attempt.target.client_ref),
        *_client_secret_field(client_secret),
        ("code", checked_callback.code),
        ("grant_type", "authorization_code"),
        ("redirect_uri", checked_attempt.redirect_uri),
    ]
    if checked_attempt.target.contract.pkce_method == "S256":
        if type(checked_attempt.code_verifier) is not str:
            raise _authorization_expired()
        form.append(("code_verifier", checked_attempt.code_verifier))
    raw = _post_token_form(token_transport, tuple(sorted(form)))
    parsed = _parse_token_response(raw, checked_attempt.target, initial=True)
    granted = parsed.granted_scopes
    if granted is None:
        # The provider did not echo a scope. Requested scopes were consented to
        # as a set, so record exactly what was requested rather than inventing
        # a broader grant.
        granted = checked_attempt.target.scopes
    refresh_status: RefreshStatus = (
        "GRANTED" if parsed.refresh_token is not None else "REFRESH_NOT_GRANTED"
    )
    return OAuthInitialGrant(
        target=checked_attempt.target,
        access_token=parsed.access_token,
        access_token_expires_at=current + timedelta(seconds=parsed.expires_in),
        refresh_token=parsed.refresh_token,
        refresh_token_expires_at=(
            None
            if parsed.refresh_token_expires_in is None
            else current + timedelta(seconds=parsed.refresh_token_expires_in)
        ),
        refresh_status=refresh_status,
        granted_scopes=granted,
        unrequested_granted_scopes=parsed.unrequested_scopes,
    )


def refresh_access_token(
    target: OAuthAuthorizationTarget,
    *,
    refresh_token: str,
    client_secret: str | None = None,
    token_transport: OAuthTokenTransport,
) -> OAuthRefreshGrant:
    """Exchange a stored refresh token for a new access token.

    The caller must have released every credential-store lock before calling
    this function; it performs provider egress.
    """
    checked = _checked_target(target)
    if not checked.contract.refresh_grant_documented:
        raise AdhelmError(
            "OAUTH_REFRESH_GRANT_NOT_DOCUMENTED",
            "This provider contract documents no refresh grant.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Re-enroll the configured local profile through the browser flow.",
        )
    _validate_secret_text(refresh_token)
    current = _current_time()
    form: tuple[tuple[str, str], ...] = (
        ("client_id", checked.client_ref),
        *_client_secret_field(client_secret),
        ("grant_type", "refresh_token"),
        ("refresh_token", refresh_token),
    )
    raw = _post_token_form(token_transport, form)
    parsed = _parse_token_response(raw, checked, initial=False)
    return OAuthRefreshGrant(
        target=checked,
        access_token=parsed.access_token,
        access_token_expires_at=current + timedelta(seconds=parsed.expires_in),
        granted_scopes=parsed.granted_scopes,
        rotated_refresh_token=parsed.refresh_token,
        rotated_refresh_token_expires_at=(
            None
            if parsed.refresh_token_expires_in is None
            else current + timedelta(seconds=parsed.refresh_token_expires_in)
        ),
        unrequested_granted_scopes=parsed.unrequested_scopes,
    )


def _client_secret_field(client_secret: str | None) -> tuple[tuple[str, str], ...]:
    """Return the form field that carries the application secret, which every contract needs."""
    if type(client_secret) is not str:
        raise ValueError("OAuth secret text is invalid")
    _validate_secret_text(client_secret)
    return (("client_secret", client_secret),)


def pkce_s256_challenge(code_verifier: str) -> str:
    """Derive the exact S256 challenge for one verifier."""
    if type(code_verifier) is not str or _PKCE_VERIFIER.fullmatch(code_verifier) is None:
        raise ValueError("PKCE verifier is invalid")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def prompt_hidden_secret(
    label: str,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    getpass_function: Callable[..., str] = getpass.getpass,
) -> str:
    """Prompt without echo only when getpass cannot fall back to stdin.

    This mirrors the shared credential-setup prompt rules for provider modules
    that need their own labels: both streams must be terminals, a
    ``GetPassWarning`` is a failed prompt, and no value is ever echoed.
    """
    if type(label) is not str or _PROMPT_LABEL.fullmatch(label) is None:
        raise AdhelmError(
            "SECRET_PROMPT_UNAVAILABLE",
            "The local hidden credential prompt is unavailable.",
            next_action="Use a supported local terminal credential prompt.",
        )
    input_stream = stdin or sys.stdin
    output_stream = stderr or sys.stderr
    if not input_stream.isatty() or not output_stream.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "This local step requires an interactive terminal.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Run locally in a terminal; do not provide a secret in chat.",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass_function(label, stream=output_stream)
    except Exception:
        raise AdhelmError(
            "SECRET_PROMPT_UNAVAILABLE",
            "The local hidden credential prompt is unavailable.",
            next_action="Use a supported local terminal credential prompt.",
        ) from None
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_SECRET_CHARS
        or _SECRET_TEXT.fullmatch(value) is None
    ):
        raise AdhelmError(
            "SECRET_VALUE_INVALID",
            "The entered value is empty, oversized, or contains control characters.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Re-enter the value from the provider application settings.",
        )
    return value


def open_system_browser(attempt: OAuthAuthorizationAttempt) -> None:
    """Open only a revalidated private authorization attempt using an OS launcher."""
    url = authorization_url(attempt)
    expected_host = attempt.target.contract.authorization_host
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise AdhelmError("OAUTH_BROWSER_URL_INVALID", "Invalid authorization handoff.") from None
    if (
        len(url) > 16_384
        or parsed.scheme != "https"
        or parsed.hostname != expected_host
        or not parsed.query
        or "#" in url
        or any(ord(character) < 33 or ord(character) > 126 for character in url)
    ):
        raise AdhelmError("OAUTH_BROWSER_URL_INVALID", "Invalid authorization handoff.")
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined,unused-ignore]
            return
        environment = {"PATH": "/usr/bin:/bin"}
        arguments: list[str]
        if sys.platform == "darwin":
            arguments = ["/usr/bin/open", "--", url]
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
            arguments = ["/usr/bin/xdg-open", url]
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


class LoopbackRedirectReceiver:
    """A single-use local listener; HTTP validity does not establish OAuth identity.

    ``port`` is explicit for providers that require an exactly pre-registered
    redirect URI.  ``path`` defaults to a private single-use nonce path; a
    provider that must pre-register a fixed path supplies it here.
    """

    def __init__(
        self,
        *,
        port: int = 0,
        path: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(port) is not bool and type(port) is int and 0 <= port <= 65_535:
            requested_port = port
        else:
            raise AdhelmError(
                "OAUTH_LOOPBACK_PORT_INVALID",
                "The requested local callback port is invalid.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        self._clock = clock
        self._started = _sample_clock(clock)
        self._deadline = self._started + _LOOPBACK_TOTAL_SECONDS
        if not math.isfinite(self._deadline) or self._deadline <= self._started:
            raise AdhelmError("OAUTH_CLOCK_INVALID", "A trusted local clock is unavailable.")
        self._last_clock = self._started
        if path is None:
            try:
                nonce = secrets.token_urlsafe(24)
                if type(nonce) is not str or re.fullmatch(r"[A-Za-z0-9_-]{32}", nonce) is None:
                    raise ValueError("Invalid callback randomness")
            except Exception:
                raise AdhelmError(
                    "OAUTH_RANDOMNESS_UNAVAILABLE",
                    "Private callback randomness is unavailable.",
                    exit_code=ExitCode.PRECONDITION,
                ) from None
            resolved_path = "/oauth/callback/" + nonce
        else:
            if type(path) is not str or _CALLBACK_PATH.fullmatch(path) is None:
                raise AdhelmError(
                    "OAUTH_LOOPBACK_PATH_INVALID",
                    "The requested local callback path is invalid.",
                    exit_code=ExitCode.INVALID_INPUT,
                )
            resolved_path = path
        self._path = resolved_path
        self._used = False
        self._socket: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket = listener
            listener.bind(("127.0.0.1", requested_port))
            listener.listen(1)
            bound_port = listener.getsockname()[1]
            if type(bound_port) is not int or not 1 <= bound_port <= 65_535:
                raise ValueError("Invalid loopback port")
            self._host = f"127.0.0.1:{bound_port}"
            self.redirect_uri = f"http://{self._host}{self._path}"
        except Exception:
            self.close()
            raise AdhelmError(
                "OAUTH_LOOPBACK_UNAVAILABLE",
                "A private loopback callback could not be opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Free the registered local callback port and retry deliberately.",
            ) from None

    def __enter__(self) -> LoopbackRedirectReceiver:
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
        if not self._last_clock <= now < self._deadline:
            raise _callback_expired()
        self._last_clock = now
        return now

    def _remaining(self) -> float:
        return self._deadline - self._now()

    def require_unexpired(self) -> None:
        """Recheck the same local deadline after callback receipt and before exchange."""
        self._remaining()

    def receive_query(self) -> str:
        """Consume one HTTP callback; the caller must then verify state and code."""
        if self._used or self._socket is None:
            raise AdhelmError("OAUTH_CALLBACK_CONSUMED", "The callback is already closed.")
        self._used = True
        try:
            for _ in range(_LOOPBACK_MAX_REQUESTS):
                self._socket.settimeout(self._remaining())
                try:
                    connection, peer = self._socket.accept()
                except TimeoutError:
                    raise _callback_expired() from None
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
                "OAUTH_CALLBACK_REJECTED",
                "No valid callback arrived within the bounded request allowance.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        except AdhelmError:
            raise
        except OSError:
            raise AdhelmError(
                "OAUTH_CALLBACK_UNAVAILABLE",
                "The loopback callback failed.",
                exit_code=ExitCode.AUTHORIZATION,
            ) from None
        finally:
            # Close before any token exchange. A code is never received twice.
            self.close()

    def _read_request(self, connection: socket.socket) -> str:
        request = bytearray()
        connection_deadline = min(self._now() + 5.0, self._deadline)
        while b"\r\n\r\n" not in request:
            remaining = min(connection_deadline - self._now(), self._remaining())
            if remaining <= 0 or len(request) >= _LOOPBACK_MAX_HEADER_BYTES:
                raise ValueError("Request bound exceeded")
            connection.settimeout(remaining)
            chunk = connection.recv(min(4096, _LOOPBACK_MAX_HEADER_BYTES - len(request)))
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
            b"Content-Length: "
            + str(len(_LOOPBACK_RESPONSE_BODY)).encode()
            + b"\r\n\r\n"
            + _LOOPBACK_RESPONSE_BODY
        )
        try:
            connection.sendall(response)
        except OSError:
            pass


class FixedOAuthTokenTransport(AbstractContextManager["FixedOAuthTokenTransport"]):
    """One explicitly constructed fixed-endpoint OAuth token transport session.

    This is deliberately separate from :class:`adhelm.transport.SafeHttpClient`.
    A token form carries ``client_secret`` and ``refresh_token`` field names,
    which the shared provider transport correctly rejects at its generic privacy
    boundary.  Nothing here is a general HTTP client: it can issue only the one
    token POST named by its contract, with no redirects and no ambient
    environment configuration.

    ``open()`` is the production constructor.  Direct construction is a
    fake-client test seam only.
    """

    def __init__(self, contract: OAuthProviderContract, client: httpx.Client) -> None:
        if type(contract) is not OAuthProviderContract or type(client) is not httpx.Client:
            raise OAuthTransportError(
                "OAUTH_TRANSPORT_INVALID",
                "The local OAuth transport is unavailable.",
                dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
                exit_code=ExitCode.INVALID_INPUT,
            )
        self._contract = contract
        self._client = client

    @classmethod
    def open(cls, contract: OAuthProviderContract) -> Self:
        """Create the sole production client shape: no environment or redirects."""
        return cls(contract, httpx.Client(trust_env=False, follow_redirects=False))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        checked = _checked_token_form(fields)
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
        }
        if self._contract.user_agent is not None:
            headers["user-agent"] = self._contract.user_agent
        if self._contract.client_authentication == "BASIC_SECRET":
            # The client ID and secret are the Basic user name and password, so
            # they travel in the header only and never in the form body.  A colon
            # in the user name would move the split (RFC 7617 section 2).
            form = dict(checked)
            client_id = form.pop("client_id", None)
            client_secret = form.pop("client_secret", None)
            if client_id is None or client_secret is None or ":" in client_id:
                raise _token_form_invalid()
            userinfo = f"{client_id}:{client_secret}".encode()
            headers["authorization"] = "Basic " + base64.b64encode(userinfo).decode("ascii")
            checked = tuple(form.items())
        encoded = urlencode(dict(checked)).encode("ascii")
        deadline = _ResponseElapsedBudget(started_at=_checked_monotonic())
        try:
            with self._client.stream(
                "POST",
                self._contract.token_endpoint,
                content=encoded,
                headers=headers,
                follow_redirects=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                deadline.checkpoint()
                if 300 <= response.status_code < 400:
                    raise _uncertain_token_error()
                if response.status_code in {400, 401, 403}:
                    raise OAuthTransportError(
                        "OAUTH_TOKEN_REJECTED",
                        "The provider rejected the local credential exchange.",
                        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
                    )
                if response.status_code != 200:
                    raise _uncertain_token_error()
                if not _json_content_type(response.headers.get("content-type")):
                    raise _uncertain_token_error()
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > _MAX_TOKEN_RESPONSE_BYTES:
                        raise _uncertain_token_error()
                    data.extend(chunk)
                    deadline.checkpoint()
                deadline.checkpoint()
                return bytes(data)
        except OAuthTransportError:
            raise
        except Exception:
            raise _uncertain_token_error() from None


class _ResponseElapsedBudget:
    """Private monotonic deadline for one fixed token response."""

    def __init__(self, *, started_at: float) -> None:
        self._started_at = started_at
        self._last_checked_at = started_at

    def checkpoint(self) -> None:
        try:
            current = _checked_monotonic()
        except Exception:
            raise _uncertain_token_error() from None
        if (
            current < self._last_checked_at
            or current - self._started_at >= _RESPONSE_ELAPSED_BUDGET_SECONDS
        ):
            raise _uncertain_token_error()
        self._last_checked_at = current


@dataclass(frozen=True, slots=True, repr=False)
class _TokenResponse:
    access_token: str
    expires_in: int
    refresh_token: str | None
    refresh_token_expires_in: int | None
    granted_scopes: tuple[str, ...] | None
    unrequested_scopes: tuple[str, ...] = ()


def _post_token_form(transport: OAuthTokenTransport, fields: tuple[tuple[str, str], ...]) -> bytes:
    try:
        response = transport.post_token_form(fields)
    except OAuthTransportError:
        raise
    except Exception:
        raise _uncertain_token_error() from None
    if type(response) is not bytes or len(response) > _MAX_TOKEN_RESPONSE_BYTES:
        raise _uncertain_token_error()
    return response


def _parse_token_response(
    raw: bytes, target: OAuthAuthorizationTarget, *, initial: bool
) -> _TokenResponse:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_TOKEN_RESPONSE_BYTES:
        raise _unsupported_token_response()
    from adhelm.canonical import parse_json

    try:
        data = parse_json(raw, maximum_bytes=_MAX_TOKEN_RESPONSE_BYTES, decimal_numbers=True)
    except (AdhelmError, ValueError, UnicodeDecodeError):
        raise _unsupported_token_response() from None
    if type(data) is not dict or not all(type(key) is str for key in data):
        raise _unsupported_token_response()
    if "error" in data and "access_token" not in data:
        # RFC 6749 section 5.2 error object. The provider answered definitively
        # and granted nothing, so this is an authorization failure rather than
        # an ambiguous outcome that might have changed remote or local state.
        raise _token_error_response()
    if not {"access_token", "expires_in"}.issubset(data) or not set(data) <= _TOKEN_RESPONSE_KEYS:
        raise _unsupported_token_response()
    # RFC 6749 section 7.1: the token type is case-insensitive, and Reddit
    # documents a lower-case "bearer".
    if "token_type" in data and (
        type(data["token_type"]) is not str or data["token_type"].lower() != "bearer"
    ):
        raise _unsupported_token_response()
    contract = target.contract
    access_token = _response_secret(data, "access_token")
    expires_in = data.get("expires_in")
    if (
        type(expires_in) is not int
        or not 1 <= expires_in <= contract.maximum_access_token_lifetime_seconds
    ):
        raise _unsupported_token_response()
    refresh_token = None if "refresh_token" not in data else _response_secret(data, "refresh_token")
    refresh_expires_in: int | None = None
    if "refresh_token_expires_in" in data:
        value = data["refresh_token_expires_in"]
        if (
            type(value) is not int
            or not 1 <= value <= contract.maximum_refresh_token_lifetime_seconds
        ):
            raise _unsupported_token_response()
        refresh_expires_in = value
    if refresh_token is None and refresh_expires_in is not None:
        raise _unsupported_token_response()
    if refresh_token is not None and not contract.refresh_grant_documented:
        raise _unsupported_token_response()
    returned = None if "scope" not in data else _parse_scope_text(data["scope"], target)
    if initial and returned is not None and not set(target.scopes) <= set(returned):
        raise AdhelmError(
            "OAUTH_SCOPE_NOT_GRANTED",
            "The provider granted fewer permissions than the local profile requires.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Confirm the requested permissions on the provider application, "
                "then start a new local authorization."
            ),
        )
    # A provider may echo permissions this request never asked for, because the
    # member consented to them through some other application flow.  Record only
    # the requested subset as the enrolled contract and report the excess
    # separately; a wider echo must never silently widen local authority.
    granted = returned
    unrequested: tuple[str, ...] = ()
    if returned is not None:
        granted = tuple(sorted(set(returned) & set(target.scopes)))
        unrequested = tuple(sorted(set(returned) - set(target.scopes)))
        if not granted:
            raise AdhelmError(
                "OAUTH_SCOPE_NOT_GRANTED",
                "The provider granted none of the requested permissions.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Confirm the requested permissions on the provider application, "
                    "then start a new local authorization."
                ),
            )
    return _TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=refresh_token,
        refresh_token_expires_in=refresh_expires_in,
        granted_scopes=granted,
        unrequested_scopes=unrequested,
    )


def _response_secret(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if type(value) is not str or _SECRET_TEXT.fullmatch(value) is None:
        raise _unsupported_token_response()
    return value


def _parse_scope_text(value: object, target: OAuthAuthorizationTarget) -> tuple[str, ...]:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_SCOPE_TEXT_BYTES:
        raise _unsupported_token_response()
    tokens = [value]
    for separator in target.contract.scope_response_separators:
        tokens = [part for token in tokens for part in token.split(separator)]
    if (
        not tokens
        or len(tokens) > _MAX_SCOPES
        or any(not item for item in tokens)
        or len(set(tokens)) != len(tokens)
        or not set(tokens) <= target.contract.supported_scopes
    ):
        raise _unsupported_token_response()
    return tuple(sorted(tokens))


def _validate_granted_scopes(value: object, target: OAuthAuthorizationTarget) -> None:
    if (
        type(value) is not tuple
        or not 1 <= len(value) <= _MAX_SCOPES
        or any(type(scope) is not str for scope in value)
        or len(set(value)) != len(value)
        or not set(value) <= target.contract.supported_scopes
        or list(value) != sorted(value)
    ):
        raise ValueError("OAuth granted scopes are invalid")


def _validate_unrequested_scopes(value: object, target: OAuthAuthorizationTarget) -> None:
    if (
        type(value) is not tuple
        or len(value) > _MAX_SCOPES
        or any(type(scope) is not str for scope in value)
        or len(set(value)) != len(value)
        or not set(value) <= target.contract.supported_scopes
        or set(value) & set(target.scopes)
        or list(value) != sorted(value)
    ):
        raise ValueError("OAuth unrequested granted scopes are invalid")


def _validate_refresh_pair(
    refresh_token: object, refresh_token_expires_at: object, refresh_status: object
) -> None:
    if refresh_status not in {"GRANTED", "REFRESH_NOT_GRANTED"}:
        raise ValueError("OAuth refresh status is invalid")
    if refresh_status == "GRANTED":
        _validate_secret_text(refresh_token)
        return
    if refresh_token is not None or refresh_token_expires_at is not None:
        raise ValueError("OAuth refresh status does not match the returned refresh token")


def _checked_token_form(fields: object) -> tuple[tuple[str, str], ...]:
    if type(fields) is not tuple or not 1 <= len(fields) <= 7:
        raise _token_form_invalid()
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
            raise _token_form_invalid()
        names.add(item[0])
        checked.append(item)
    return tuple(checked)


def _parse_callback_query(raw_query: object) -> dict[str, str]:
    if (
        type(raw_query) is not str
        or not raw_query
        or len(raw_query.encode("utf-8")) > _MAX_CALLBACK_QUERY_BYTES
        or any(ord(character) < 32 or ord(character) > 126 for character in raw_query)
    ):
        raise _callback_invalid()
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=6)
    except ValueError:
        raise _callback_invalid() from None
    result: dict[str, str] = {}
    for key, value in pairs:
        if (
            key not in _CALLBACK_KEYS
            or key in result
            or type(value) is not str
            or len(value.encode("utf-8")) > 4_096
            or any(ord(character) < 32 for character in value)
        ):
            raise _callback_invalid()
        result[key] = value
    if "state" not in result or ("code" in result) == ("error" in result):
        raise _callback_invalid()
    return result


def _checked_target(value: object) -> OAuthAuthorizationTarget:
    if type(value) is not OAuthAuthorizationTarget:
        raise ValueError("OAuth authorization target is invalid")
    return OAuthAuthorizationTarget(
        contract=value.contract, client_ref=value.client_ref, scopes=value.scopes
    )


def _checked_attempt(value: object) -> OAuthAuthorizationAttempt:
    if type(value) is not OAuthAuthorizationAttempt:
        raise ValueError("OAuth authorization attempt is invalid")
    return OAuthAuthorizationAttempt(
        target=_checked_target(value.target),
        redirect_uri=value.redirect_uri,
        state=value.state,
        code_verifier=value.code_verifier,
        issued_at=value.issued_at,
        expires_at=value.expires_at,
    )


def _checked_callback(value: object, attempt: OAuthAuthorizationAttempt) -> OAuthCallbackCode:
    if type(value) is not OAuthCallbackCode:
        raise ValueError("OAuth callback code is invalid")
    checked = OAuthCallbackCode(code=value.code, state=value.state)
    if not hmac.compare_digest(checked.state, attempt.state):
        raise AdhelmError(
            "OAUTH_CALLBACK_STATE_MISMATCH",
            "The OAuth callback did not match the local authorization attempt.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local authorization; do not reuse this callback.",
        )
    return checked


def _validate_https_endpoint(value: object) -> None:
    if type(value) is not str or not 1 <= len(value) <= 512:
        raise ValueError("OAuth endpoint is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("OAuth endpoint is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname != parsed.hostname.lower()
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\\" in value
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("OAuth endpoint is invalid")


def _validate_loopback_redirect_uri(value: object) -> None:
    if type(value) is not str or len(value) > 256:
        raise ValueError("OAuth redirect URI is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("OAuth redirect URI is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or _CALLBACK_PATH.fullmatch(parsed.path) is None
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or value != f"http://127.0.0.1:{port}{parsed.path}"
    ):
        raise ValueError("OAuth redirect URI is invalid")


def _validate_secret_text(value: object) -> None:
    if type(value) is not str or _SECRET_TEXT.fullmatch(value) is None:
        raise ValueError("OAuth secret text is invalid")


def _decode_exact_base64url(value: object, *, expected_bytes: int) -> bytes:
    if type(value) is not str or _BASE64URL.fullmatch(value) is None:
        raise ValueError("base64url value is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except Exception:
        raise ValueError("base64url value is invalid") from None
    encoded = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if encoded != value or len(decoded) != expected_bytes:
        raise ValueError("base64url value is invalid")
    return decoded


def _secure_random_bytes(size: int) -> bytes:
    """Private randomness seam; callers cannot supply predictable OAuth state."""
    return secrets.token_bytes(size)


def _random_base64url(size: int) -> str:
    try:
        value = _secure_random_bytes(size)
    except Exception:
        raise AdhelmError(
            "OAUTH_RANDOMNESS_UNAVAILABLE",
            "Secure local OAuth randomness is unavailable.",
            exit_code=ExitCode.PRECONDITION,
        ) from None
    if type(value) is not bytes or len(value) != size:
        raise AdhelmError(
            "OAUTH_RANDOMNESS_UNAVAILABLE",
            "Secure local OAuth randomness is unavailable.",
            exit_code=ExitCode.PRECONDITION,
        )
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _checked_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OAuth time is invalid")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    """Private clock seam; production callers cannot select OAuth time."""
    return datetime.now(UTC)


def _current_time() -> datetime:
    try:
        return _checked_time(_utc_now())
    except Exception:
        raise AdhelmError(
            "OAUTH_CLOCK_INVALID",
            "The local OAuth lifecycle requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _monotonic() -> float:
    """Private fake-only seam for whole-response budget regressions."""
    return time.monotonic()


def _checked_monotonic() -> float:
    value = _monotonic()
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("monotonic clock is invalid")
    return float(value)


def _sample_clock(clock: Callable[[], float]) -> float:
    try:
        value = clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("Invalid clock sample")
        return float(value)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError("OAUTH_CLOCK_INVALID", "A trusted local clock is unavailable.") from None


def _json_content_type(value: object) -> bool:
    if type(value) is not str or len(value) > 256 or any(ord(c) < 32 for c in value):
        return False
    return value.split(";", 1)[0].strip().lower() == "application/json"


def _browser_unavailable() -> AdhelmError:
    return AdhelmError(
        "OAUTH_BROWSER_UNAVAILABLE",
        "The operating-system browser handoff did not complete.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Use a supported local desktop session and start a deliberate new attempt.",
    )


def _callback_expired() -> AdhelmError:
    return AdhelmError(
        "OAUTH_CALLBACK_EXPIRED",
        "The bounded browser authorization window ended.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Start a new local authorization.",
    )


def _callback_invalid() -> AdhelmError:
    return AdhelmError(
        "OAUTH_CALLBACK_INVALID",
        "The OAuth callback is invalid.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Start a new local authorization.",
    )


def _authorization_expired() -> AdhelmError:
    return AdhelmError(
        "OAUTH_AUTHORIZATION_EXPIRED",
        "The local OAuth authorization expired before it completed.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Start a new local authorization.",
    )


def _token_form_invalid() -> OAuthTransportError:
    return OAuthTransportError(
        "OAUTH_TOKEN_FORM_INVALID",
        "The OAuth token request cannot be prepared.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
        exit_code=ExitCode.INVALID_INPUT,
    )


def _uncertain_token_error() -> OAuthTransportError:
    return OAuthTransportError(
        "OAUTH_TOKEN_OUTCOME_UNCERTAIN",
        "The OAuth token exchange outcome is uncertain; inspect status before another attempt.",
        dispatch_state=OAuthTokenDispatchState.UNCERTAIN,
        exit_code=ExitCode.AMBIGUOUS,
    )


def _token_error_response() -> OAuthTransportError:
    return OAuthTransportError(
        "OAUTH_TOKEN_ERROR_RESPONSE",
        "The provider returned an OAuth error instead of a token; nothing was granted.",
        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Start a new local authorization; do not retry the same code or token.",
    )


def _unsupported_token_response() -> OAuthTransportError:
    return OAuthTransportError(
        "OAUTH_TOKEN_RESPONSE_UNSUPPORTED",
        "The OAuth token response cannot be safely accepted.",
        dispatch_state=OAuthTokenDispatchState.UNCERTAIN,
        exit_code=ExitCode.AMBIGUOUS,
    )

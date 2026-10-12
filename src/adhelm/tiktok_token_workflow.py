"""Explicit local TikTok Ads app auth-code enrollment; no advertising operations.

Enrollment performs exactly one provider request — the documented
``POST /open_api/v1.3/oauth2/access_token/`` — and it is written so that no
credential mutation lock is held across it: the credential lifecycle module
opens and closes each locked section, and the network step happens between them.

The exchange deliberately does **not** reuse
:func:`adhelm.oauth_enrollment.exchange_authorization_code`.  That function builds
and parses an RFC 6749 form grant, and TikTok's exchange is a JSON body of
exactly ``app_id``, ``secret`` and ``auth_code`` returning a
``{code,message,request_id,data}`` envelope with no ``grant_type``,
``redirect_uri``, PKCE parameter, or refresh grant at all.  The reusable
module's non-wire seams are reused unchanged:
:func:`~adhelm.oauth_enrollment.prompt_hidden_secret` for the hidden prompts, and
:class:`~adhelm.oauth_enrollment.OAuthTransportError` with
:class:`~adhelm.oauth_enrollment.OAuthTokenDispatchState` so a refusal still
carries only its dispatch classification and never a response body.

There is no loopback listener and no browser handoff.  TikTok shows each
application its own authorization URL in the My Apps console and publishes no
constructable template on any page this build could fetch, so the operator
obtains the one-hour single-use authorization code themselves and pastes it into
a hidden prompt.  This product never constructs, opens, or emits an
authorization URL.
"""

from __future__ import annotations

import json
import math
import sys
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Never, Protocol, Self

import httpx

from adhelm.canonical import parse_json
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, validate_configuration
from adhelm.oauth_enrollment import (
    OAuthTokenDispatchState,
    OAuthTransportError,
    prompt_hidden_secret,
)
from adhelm.tiktok_token_credentials import (
    TikTokExpirySource,
    TikTokTokenGrant,
    TikTokTokenLifecycleResult,
    TikTokTokenStatus,
    install_tiktok_token_grant,
    new_identity_salt,
    prepare_tiktok_token_enrollment,
    require_tiktok_token_refresh_route,
    tiktok_token_status,
)
from adhelm.tiktok_token_profiles import (
    TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED,
    TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED,
    TIKTOK_MAXIMUM_ACCESS_TOKEN_LIFETIME_SECONDS,
    TIKTOK_OAUTH_TOKEN_PATH,
    TIKTOK_TOKEN_ENVIRONMENT_HOSTS,
    TikTokTokenEnvironment,
    checked_advertiser_id,
    checked_app_ref,
    select_tiktok_ads_profile,
    tiktok_environment_host,
)

_APP_ID_PROMPT: Final = "TikTok app ID: "
_APP_SECRET_PROMPT: Final = "TikTok app secret: "
_AUTH_CODE_PROMPT: Final = "TikTok one-hour auth code: "
_REQUEST_TIMEOUT_SECONDS: Final = 30.0
_RESPONSE_ELAPSED_BUDGET_SECONDS: Final = 30.0
_MAX_TOKEN_RESPONSE_BYTES: Final = 262_144
# The whole documented request body.  Nothing else is ever sent.
_TOKEN_BODY_KEYS: Final[frozenset[str]] = frozenset({"app_id", "auth_code", "secret"})
# The documented envelope.  `data` is an untyped object in the SDK model, so the
# payload keys below are accepted where present and never required.
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"code", "data", "message", "request_id"})
_DATA_KEYS: Final[frozenset[str]] = frozenset(
    {"access_token", "advertiser_ids", "scope", "expires_in", "token_expires_in"}
)
_MAX_ADVERTISERS: Final = 512
# The closed set of origins this transport may ever address, derived from the
# same closed environment map `tiktok_environment_host` resolves.  The pinning
# is therefore structural rather than a property of one call site remembering
# to resolve the host first: `FixedOAuthTokenTransport` gets the same guarantee
# from a contract endpoint that already passed `_validate_https_endpoint`, and
# `FixedGoogleOAuthTransport` from a module constant.  A host outside this set
# -- including one carrying userinfo, where `https://{host}` would resolve to
# whatever follows the `@` -- is refused before the app secret and the one-hour
# authorization code could reach it.
_PINNED_TOKEN_HOSTS: Final[frozenset[str]] = frozenset(TIKTOK_TOKEN_ENVIRONMENT_HOSTS.values())


class TikTokTokenTransport(Protocol):
    """Opaque JSON token seam used by this module and fake-only tests."""

    def post_token_body(self, fields: tuple[tuple[str, str], ...]) -> bytes: ...


class FixedTikTokTokenTransport(AbstractContextManager["FixedTikTokTokenTransport"]):
    """One explicitly constructed fixed-endpoint TikTok token transport session.

    This is deliberately separate from :class:`adhelm.transport.SafeHttpClient`.
    The body carries a ``secret`` and an ``auth_code`` field name, which the
    shared provider transport correctly rejects at its generic privacy boundary.
    Nothing here is a general HTTP client: it can issue only the one token POST
    at one of the closed ``_PINNED_TOKEN_HOSTS``, with no redirects and no
    ambient environment configuration.  Every constructor refuses a host outside
    that set before anything is dispatched, so the pinning cannot be bypassed by
    a caller that skipped :func:`tiktok_environment_host`.

    ``open()`` is the production constructor.  Direct construction is a
    fake-client test seam only.
    """

    def __init__(self, host: str, client: httpx.Client) -> None:
        checked = _pinned_token_host(host)
        if type(client) is not httpx.Client:
            raise _transport_invalid()
        self._endpoint = f"https://{checked}{TIKTOK_OAUTH_TOKEN_PATH}"
        self._client = client

    @classmethod
    def open(cls, host: str) -> Self:
        """Create the sole production client shape: no environment or redirects.

        The host is checked before any client exists, so an unpinned host opens
        no socket, leaves no client to close, and dispatches nothing.
        """
        checked = _pinned_token_host(host)
        return cls(checked, httpx.Client(trust_env=False, follow_redirects=False))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def post_token_body(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        checked = _checked_token_body(fields)
        encoded = json.dumps(dict(checked), separators=(",", ":")).encode("utf-8")
        started = _checked_monotonic()
        try:
            with self._client.stream(
                "POST",
                self._endpoint,
                content=encoded,
                headers={"accept": "application/json", "content-type": "application/json"},
                follow_redirects=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                _checkpoint(started)
                if 300 <= response.status_code < 400:
                    raise _uncertain_token_error()
                if response.status_code in {400, 401, 403}:
                    raise OAuthTransportError(
                        "TIKTOK_TOKEN_REJECTED",
                        "TikTok rejected the local credential exchange.",
                        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
                        next_action=(
                            "Obtain a new authorization code from the application's own "
                            "authorization URL; a TikTok auth code is single-use and expires "
                            "one hour after it is issued."
                        ),
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
                    _checkpoint(started)
                _checkpoint(started)
                return bytes(data)
        except OAuthTransportError:
            raise
        except Exception:
            raise _uncertain_token_error() from None


def enroll_tiktok_token(
    directory: Path,
    profile_id: str,
    *,
    environment: TikTokTokenEnvironment,
    interactive: bool = False,
) -> TikTokTokenLifecycleResult:
    """Exchange one operator-supplied TikTok authorization code for this profile.

    The operator obtains the one-hour single-use code from the application's own
    authorization URL in TikTok's My Apps console.  The app ID, the app secret
    and the code are all collected through hidden prompts and none of them is
    ever echoed, logged, or returned.
    """
    _require_interactive(interactive)
    host = tiktok_environment_host(environment)
    configuration = _configuration(directory)
    profile = select_tiktok_ads_profile(configuration, profile_id)
    checked_advertiser_id(profile.account_ref)
    slot = prepare_tiktok_token_enrollment(directory, profile_id, environment)
    app_ref = checked_app_ref(_prompt_app_id())
    app_secret = _prompt_app_secret()
    auth_code = _prompt_auth_code()
    with _open_token_transport(host) as transport:
        raw = _post_token_body(
            transport,
            (
                ("app_id", app_ref),
                ("auth_code", auth_code),
                ("secret", app_secret),
            ),
        )
    grant = _parse_exchange(raw, app_ref=app_ref, environment=environment)
    return install_tiktok_token_grant(
        directory,
        slot,
        grant,
        app_secret=app_secret,
        identity_salt=new_identity_salt(),
    )


def refresh_tiktok_token(directory: Path, profile_id: str) -> Never:
    """Always refuse: TikTok documents no advertiser refresh route.

    It never returns: the refusal is raised after a local status read only.
    """
    require_tiktok_token_refresh_route(directory, profile_id)


def tiktok_token_local_status(directory: Path, profile_id: str) -> TikTokTokenStatus:
    """Return bounded local status; this makes no TikTok request."""
    return tiktok_token_status(directory, profile_id)


def _parse_exchange(
    raw: bytes, *, app_ref: str, environment: TikTokTokenEnvironment
) -> TikTokTokenGrant:
    """Parse exactly the documented envelope; nothing else is accepted.

    `message` and `request_id` are validated for shape and never read into a
    diagnostic, so no provider text can reach local output.
    """
    if type(raw) is not bytes or not raw or len(raw) > _MAX_TOKEN_RESPONSE_BYTES:
        raise _unsupported_response()
    try:
        data = parse_json(raw, maximum_bytes=_MAX_TOKEN_RESPONSE_BYTES)
    except (AdhelmError, ValueError, UnicodeDecodeError):
        raise _unsupported_response() from None
    if type(data) is not dict or not set(data) <= _ENVELOPE_KEYS:
        raise _unsupported_response()
    code = data.get("code")
    if type(code) is not int or isinstance(code, bool):
        raise _unsupported_response()
    if code != 0:
        raise OAuthTransportError(
            "TIKTOK_TOKEN_ERROR_RESPONSE",
            "TikTok returned a business error instead of a token; nothing was granted.",
            dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Obtain a new authorization code and enroll again; a TikTok auth code is "
                "single-use and expires one hour after it is issued. No response text is "
                "recorded locally."
            ),
        )
    payload = data.get("data")
    if type(payload) is not dict or not set(payload) <= _DATA_KEYS:
        raise _unsupported_response()
    access_token = payload.get("access_token")
    if (
        type(access_token) is not str
        or not 1 <= len(access_token) <= 16_384
        or any(ord(character) < 33 or ord(character) > 126 for character in access_token)
    ):
        raise _unsupported_response()
    expires_at, source = _expiry(payload)
    advertiser_ids = _advertiser_ids(payload)
    return TikTokTokenGrant(
        app_ref=app_ref,
        environment=environment,
        access_token=access_token,
        access_token_expires_at=expires_at,
        expiry_source=source,
        advertiser_ids=advertiser_ids,
        # Only whether a permission field was present.  TikTok publishes no
        # scope vocabulary this build could fetch, so its value is never read,
        # stored, or reported.
        scope_field_returned="scope" in payload,
    )


def _expiry(payload: dict[str, Any]) -> tuple[datetime | None, TikTokExpirySource]:
    """Track an expiry only when the response actually declares a bounded one.

    No first-party source documents an expiry for the advertiser access token,
    so an absent lifetime is missing information and is recorded as
    `NOT_DOCUMENTED_LOCALLY` rather than as "does not expire".

    Both lifetime field names are `UNVERIFIED` in
    `docs/research/tiktok-oauth-contract.md`: the TikTok-maintained SDK proves
    the envelope, not `data`.  Neither name therefore arbitrates the other, so a
    response declaring two *different* bounded lifetimes is refused rather than
    reduced to whichever key this parser happens to read first -- that would
    record a `PROVIDER_DECLARED` expiry the same response contradicts.  Two
    equal declarations agree and are accepted.
    """
    declared: set[int] = set()
    for key in ("expires_in", "token_expires_in"):
        value = payload.get(key)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or type(value) is not int
            or not 1 <= value <= TIKTOK_MAXIMUM_ACCESS_TOKEN_LIFETIME_SECONDS
        ):
            raise _unsupported_response()
        declared.add(value)
    if not declared:
        return None, TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED
    if len(declared) != 1:
        raise _unsupported_response()
    return (
        _now() + timedelta(seconds=declared.pop()),
        TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED,
    )


def _advertiser_ids(payload: dict[str, Any]) -> tuple[str, ...] | None:
    """Read the granted advertiser list when the response carries one.

    The field name is recorded by this repository's 2026-09-04 provider-access
    review and by no page this build could fetch, so its absence is reported as
    an unproven binding rather than silently treated as an empty grant.

    A *returned* empty list is the opposite case: the provider answered, and its
    answer is that this grant carries no advertiser.  That is a definite fact
    about a definite response, so it is refused as
    `TIKTOK_TOKEN_NO_ADVERTISERS_GRANTED` rather than reported as an
    uninterpretable shape.
    """
    value = payload.get("advertiser_ids")
    if value is None:
        return None
    if type(value) is not list or len(value) > _MAX_ADVERTISERS:
        raise _unsupported_response()
    if not value:
        raise _no_advertisers_granted()
    checked: list[str] = []
    for item in value:
        try:
            checked.append(checked_advertiser_id(item))
        except AdhelmError:
            raise _unsupported_response() from None
    if len(set(checked)) != len(checked):
        raise _unsupported_response()
    return tuple(checked)


def _post_token_body(transport: TikTokTokenTransport, fields: tuple[tuple[str, str], ...]) -> bytes:
    try:
        response = transport.post_token_body(fields)
    except OAuthTransportError:
        raise
    except Exception:
        raise _uncertain_token_error() from None
    if type(response) is not bytes or len(response) > _MAX_TOKEN_RESPONSE_BYTES:
        raise _uncertain_token_error()
    return response


def _checked_token_body(fields: object) -> tuple[tuple[str, str], ...]:
    if type(fields) is not tuple or len(fields) != len(_TOKEN_BODY_KEYS):
        raise _body_invalid()
    names: set[str] = set()
    checked: list[tuple[str, str]] = []
    for item in fields:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            or item[0] not in _TOKEN_BODY_KEYS
            or item[0] in names
            or not 1 <= len(item[1].encode("utf-8")) <= 16_384
            or any(ord(character) < 32 for character in item[1])
        ):
            raise _body_invalid()
        names.add(item[0])
        checked.append(item)
    if names != set(_TOKEN_BODY_KEYS):
        raise _body_invalid()
    return tuple(checked)


def _open_token_transport(host: str) -> FixedTikTokTokenTransport:
    """Production token transport seam; tests supply a fake client instead."""
    return FixedTikTokTokenTransport.open(host)


def _prompt_app_id() -> str:
    """Collect the customer application's ID without echo."""
    return prompt_hidden_secret(_APP_ID_PROMPT)


def _prompt_app_secret() -> str:
    """Collect the customer application's secret without echo."""
    return prompt_hidden_secret(_APP_SECRET_PROMPT)


def _prompt_auth_code() -> str:
    """Collect the one-hour single-use authorization code without echo."""
    return prompt_hidden_secret(_AUTH_CODE_PROMPT)


def _require_interactive(interactive: object) -> None:
    if interactive is not True:
        raise AdhelmError(
            "EXPLICIT_LOCAL_ENROLLMENT_REQUIRED",
            "TikTok enrollment requires an explicitly requested local credential handoff.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Run locally with --interactive; keep app IDs, secrets and authorization "
                "codes out of chat."
            ),
        )
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "TikTok enrollment requires a local interactive terminal.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Run enrollment in a local terminal and paste the authorization code from the "
                "application's own authorization URL."
            ),
        )


def _configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _now() -> datetime:
    """Private clock seam; production callers cannot select the enrollment time."""
    value = datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdhelmError(
            "TIKTOK_TOKEN_CLOCK_INVALID",
            "The TikTok token lifecycle requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        )
    return value


def _monotonic() -> float:
    """Private fake-only seam for whole-response budget regressions."""
    return time.monotonic()


def _checked_monotonic() -> float:
    value = _monotonic()
    if type(value) not in {int, float} or not math.isfinite(value):
        raise _uncertain_token_error()
    return float(value)


def _checkpoint(started: float) -> None:
    current = _checked_monotonic()
    if current < started or current - started >= _RESPONSE_ELAPSED_BUDGET_SECONDS:
        raise _uncertain_token_error()


def _json_content_type(value: object) -> bool:
    if type(value) is not str or len(value) > 256 or any(ord(c) < 32 for c in value):
        return False
    return value.split(";", 1)[0].strip().lower() == "application/json"


def _pinned_token_host(host: object) -> str:
    """Return the host only when the closed environment map contains it.

    A host this build never proved is refused here rather than interpolated
    into an endpoint, so no request carrying the application secret or the
    one-hour authorization code can be addressed to it.
    """
    if type(host) is not str or host not in _PINNED_TOKEN_HOSTS:
        raise OAuthTransportError(
            "TIKTOK_TOKEN_TRANSPORT_HOST_NOT_PINNED",
            "The TikTok token transport accepts only its pinned provider host.",
            dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Resolve the host with `tiktok_environment_host`; the TikTok token endpoint "
                "is pinned to the closed environment map and is never taken from a caller."
            ),
        )
    return host


def _transport_invalid() -> OAuthTransportError:
    return OAuthTransportError(
        "TIKTOK_TOKEN_TRANSPORT_INVALID",
        "The local TikTok token transport is unavailable.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
        exit_code=ExitCode.INVALID_INPUT,
    )


def _body_invalid() -> OAuthTransportError:
    return OAuthTransportError(
        "TIKTOK_TOKEN_BODY_INVALID",
        "The TikTok token request cannot be prepared.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start the TikTok enrollment again from its own command.",
    )


def _uncertain_token_error() -> OAuthTransportError:
    return OAuthTransportError(
        "TIKTOK_TOKEN_OUTCOME_UNCERTAIN",
        "The TikTok token exchange outcome is uncertain; inspect status before another attempt.",
        dispatch_state=OAuthTokenDispatchState.UNCERTAIN,
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Run `auth tiktok-token status --profile <id>`. The authorization code may already "
            "have been consumed; obtain a new one rather than retrying the same code."
        ),
    )


def _no_advertisers_granted() -> OAuthTransportError:
    """A definite provider answer: the exchange granted no advertiser at all.

    Unlike an absent list, an empty one is not missing information, so it is
    reported as an authorization outcome rather than as an unsupported shape.
    The single-use authorization code was consumed by the request that returned
    it, so the caller still blocks the prepared enrollment state.
    """
    return OAuthTransportError(
        "TIKTOK_TOKEN_NO_ADVERTISERS_GRANTED",
        "TikTok returned an empty advertiser list; this grant reaches no advertiser account.",
        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
        exit_code=ExitCode.AUTHORIZATION,
        next_action=(
            "Grant the application access to the advertiser account in TikTok Business Center, "
            "then enroll again with a new authorization code; the previous code is now spent."
        ),
    )


def _unsupported_response() -> OAuthTransportError:
    return OAuthTransportError(
        "TIKTOK_TOKEN_RESPONSE_UNSUPPORTED",
        "The TikTok token response cannot be safely accepted.",
        dispatch_state=OAuthTokenDispatchState.UNCERTAIN,
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Run `auth tiktok-token status --profile <id>` before another attempt; the "
            "authorization code may already have been consumed."
        ),
    )


__all__ = [
    "FixedTikTokTokenTransport",
    "TikTokTokenTransport",
    "enroll_tiktok_token",
    "refresh_tiktok_token",
    "tiktok_token_local_status",
]

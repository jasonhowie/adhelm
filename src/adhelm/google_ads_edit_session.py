"""Private sealed Google Ads EDIT observation session.

The context manager in this module is the only M54 entry point.  It proves a
customer identity once, then permits only one fixed campaign-state read at a
time.  It has no mutation, plan, ledger, retention, generic credential, URL,
query, transport, or serialization surface.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Final, Never

import httpx

from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import digest
from adhelm.config import ConfigurationTransaction, configuration_transaction
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, Profile, validate_configuration
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsCampaignState,
    GoogleAdsCampaignStateReader,
    GoogleAdsEditIdentity,
    _GoogleAdsEditTarget,
)
from adhelm.transport import SafeHttpClient

__all__ = ("GoogleAdsEditSession", "configured_google_ads_edit_session")

_HOST: Final = "googleads.googleapis.com"
_MAX_RESPONSE_BYTES: Final = 65_536
_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_SAFE_REF: Final = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$", re.ASCII)
_CONTINUITY_DOMAIN: Final = b"adhelm/google-ads/edit-session/continuity/v1\x00"
_SESSION_ISSUER = object()
_RLOCK_TYPE: Final = type(RLock())


def _now() -> datetime:
    """Private trusted-clock seam for deterministic fake-only tests."""

    return datetime.now(UTC)


def _google_ads_http_client() -> httpx.Client:
    """Create the sole clean client used by the fixed v25 search reader."""

    return httpx.Client(trust_env=False)


@dataclass(frozen=True, slots=True, repr=False)
class _GoogleAdsEditSessionContext:
    _directory: Path
    _profile_id: str
    _configuration_digest: str
    _profile_digest: str
    _target: _GoogleAdsEditTarget
    _continuity_key: bytes
    _continuity_marker: bytes
    _observed_at: datetime
    _expires_at: datetime

    def __repr__(self) -> str:
        return "<_GoogleAdsEditSessionContext redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads EDIT session contexts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads EDIT session contexts cannot be serialized")


class GoogleAdsEditSession:
    """An active fixed Google Ads EDIT observation session with no write route."""

    __slots__ = ("_client", "_context", "_identity", "_issuer", "_lock", "_closed", "_reader")

    def __init__(
        self,
        *,
        client: httpx.Client,
        context: _GoogleAdsEditSessionContext,
        issuer: object,
    ) -> None:
        if issuer is not _SESSION_ISSUER:
            raise TypeError("Google Ads EDIT sessions are factory-issued only")
        self._client = client
        self._context = context
        self._issuer = issuer
        self._identity: GoogleAdsEditIdentity | None = None
        self._lock = RLock()
        self._closed = False
        self._reader = GoogleAdsCampaignStateReader(
            SafeHttpClient(
                client,
                {_HOST},
                maximum_response_bytes=_MAX_RESPONSE_BYTES,
                read_attempts=1,
            ),
            self._headers_for_fixed_request,
            context._target,
        )

    def __repr__(self) -> str:
        return "<GoogleAdsEditSession redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads EDIT sessions cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads EDIT sessions cannot be serialized")

    @property
    def identity(self) -> GoogleAdsEditIdentity:
        """Return the initial configured customer observation without credentials."""

        with self._checked_lock():
            self._require_open()
            identity = self._identity
        if type(identity) is not GoogleAdsEditIdentity:
            raise _session_invalid()
        try:
            return GoogleAdsEditIdentity.model_validate(
                identity.model_dump(mode="python", warnings="error")
            )
        except Exception:
            raise _session_invalid() from None

    def read_campaign_state(self, campaign_id: str) -> GoogleAdsCampaignState:
        """Perform exactly one sealed campaign-state search after local continuity checks."""

        with self._checked_lock():
            self._require_open()
            reader = self._reader
            identity = self._identity
            if (
                type(reader) is not GoogleAdsCampaignStateReader
                or type(identity) is not GoogleAdsEditIdentity
            ):
                raise _session_invalid()
            return reader.read_campaign_state(identity, campaign_id)

    def _prove_identity(self) -> None:
        with self._checked_lock():
            self._require_open()
            reader = self._reader
            if type(reader) is not GoogleAdsCampaignStateReader:
                raise _session_invalid()
            identity = reader.discover_identity()
            self._require_open()
            self._identity = identity

    def _headers_for_fixed_request(self) -> dict[str, str]:
        """Resolve and compare both protected values immediately before one POST."""

        with self._checked_lock():
            self._require_open()
            context = self._checked_context()
        with configuration_transaction(context._directory) as transaction:
            profile = _current_profile(transaction, context)
            snapshot = oauth_credentials._checked_google_ads_edit_execution_snapshot(
                oauth_credentials._resolve_google_ads_edit_execution_snapshot(profile), profile
            )
            try:
                _verify_snapshot_continuity(snapshot, context)
                headers = _headers_from_snapshot(snapshot, context._target)
                transaction.require_unchanged(context._configuration_digest)
                return headers
            finally:
                # Do not retain a protected snapshot in this callback after its
                # fixed request headers have been constructed.
                del snapshot

    def _checked_context(self) -> _GoogleAdsEditSessionContext:
        try:
            issuer = self._issuer
            context = self._context
        except Exception:
            raise _session_invalid() from None
        if issuer is not _SESSION_ISSUER or type(context) is not _GoogleAdsEditSessionContext:
            raise _session_invalid()
        try:
            if (
                not isinstance(context._directory, Path)
                or type(context._profile_id) is not str
                or _SAFE_REF.fullmatch(context._profile_id) is None
                or type(context._configuration_digest) is not str
                or _DIGEST.fullmatch(context._configuration_digest) is None
                or type(context._profile_digest) is not str
                or _DIGEST.fullmatch(context._profile_digest) is None
                or type(context._continuity_key) is not bytes
                or len(context._continuity_key) != 32
                or type(context._continuity_marker) is not bytes
                or len(context._continuity_marker) != 32
                or type(context._observed_at) is not datetime
                or context._observed_at.tzinfo is None
                or context._observed_at.utcoffset() is None
                or type(context._expires_at) is not datetime
                or context._expires_at.tzinfo is None
                or context._expires_at.utcoffset() is None
                or context._observed_at.astimezone(UTC) >= context._expires_at.astimezone(UTC)
            ):
                raise ValueError("context")
            _checked_target(context._target)
        except Exception:
            raise _session_invalid() from None
        return context

    def _checked_lock(self) -> RLock:
        if type(self) is not GoogleAdsEditSession:
            raise _session_invalid()
        try:
            lock = self._lock
        except Exception:
            raise _session_invalid() from None
        if type(lock) is not _RLOCK_TYPE:
            raise _session_invalid()
        return lock

    def _require_open(self) -> None:
        try:
            closed = self._closed
        except Exception:
            raise _session_invalid() from None
        if closed is not False:
            raise AdhelmError(
                "GOOGLE_ADS_EDIT_SESSION_CLOSED",
                "The Google Ads EDIT observation session is closed.",
                exit_code=ExitCode.PRECONDITION,
            )

    def _close(self) -> None:
        with self._checked_lock():
            if self._closed:
                return
            self._closed = True
            try:
                self._client.close()
            except Exception:
                # Closing an in-memory read session must not mask a provider or
                # validation result already in flight.
                pass


@contextmanager
def configured_google_ads_edit_session(
    directory: Path,
    profile_id: str,
    expected_configuration_digest: str | None = None,
) -> Iterator[GoogleAdsEditSession]:
    """Open one configured, read-only Google Ads EDIT observation session.

    The optional digest is private composition protection.  A malformed value
    fails before protected storage; a valid but stale value fails before the
    OAuth slot or any v25 request.
    """

    root = _checked_directory(directory)
    selected_id = _checked_profile_id(profile_id)
    expected = _checked_expected_digest(expected_configuration_digest)
    with configuration_transaction(root) as transaction:
        configuration = _checked_configuration(transaction.read())
        configuration_digest = digest(configuration)
        if expected is not None and not hmac.compare_digest(expected, configuration_digest):
            raise _configuration_changed()
        profile = _selected_profile(configuration, selected_id)
        snapshot = oauth_credentials._checked_google_ads_edit_execution_snapshot(
            oauth_credentials._resolve_google_ads_edit_execution_snapshot(profile), profile
        )
        try:
            context = _new_context(root, configuration_digest, profile, snapshot)
            transaction.require_unchanged(configuration_digest)
        finally:
            del snapshot
    with _google_ads_http_client() as client:
        session = GoogleAdsEditSession(client=client, context=context, issuer=_SESSION_ISSUER)
        try:
            session._prove_identity()
            yield session
        finally:
            session._close()


def _new_context(
    directory: Path,
    configuration_digest: str,
    profile: Profile,
    snapshot: oauth_credentials._GoogleAdsEditExecutionSnapshot,
) -> _GoogleAdsEditSessionContext:
    """Detach only the nonsecret target and keyed protected-value continuity fact."""

    checked_profile = _checked_profile(profile)
    checked_snapshot = oauth_credentials._checked_google_ads_edit_execution_snapshot(
        snapshot, checked_profile
    )
    try:
        currency = checked_profile.currency
        if type(currency) is not str:
            raise ValueError("currency")
        target = _GoogleAdsEditTarget(
            customer_client_ref=checked_profile.account_ref,
            login_customer_client_ref=checked_profile.login_customer_ref,
            declared_currency=currency,
            declared_timezone=checked_profile.timezone,
        )
        observed_at = _checked_now()
        expires_at = _utc_time(checked_snapshot._access_token_expires_at)
        if observed_at >= expires_at:
            raise ValueError("expiry")
        marker_key = secrets.token_bytes(32)
        marker = _continuity_marker(
            marker_key,
            configuration_digest,
            checked_profile.profile_id,
            target,
            checked_snapshot,
        )
        return _GoogleAdsEditSessionContext(
            _directory=directory,
            _profile_id=checked_profile.profile_id,
            _configuration_digest=configuration_digest,
            _profile_digest=digest(checked_profile),
            _target=target,
            _continuity_key=marker_key,
            _continuity_marker=marker,
            _observed_at=observed_at,
            _expires_at=expires_at,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _session_invalid() from None


def _current_profile(
    transaction: ConfigurationTransaction, context: _GoogleAdsEditSessionContext
) -> Profile:
    configuration = _checked_configuration(
        transaction.require_unchanged(context._configuration_digest)
    )
    profile = _selected_profile(configuration, context._profile_id)
    if not hmac.compare_digest(digest(profile), context._profile_digest):
        raise _credential_changed()
    target = context._target
    if (
        profile.account_ref != target.customer_client_ref
        or profile.login_customer_ref != target.login_customer_client_ref
        or profile.currency != target.declared_currency
        or profile.timezone != target.declared_timezone
    ):
        raise _credential_changed()
    return profile


def _verify_snapshot_continuity(
    snapshot: oauth_credentials._GoogleAdsEditExecutionSnapshot,
    context: _GoogleAdsEditSessionContext,
) -> None:
    """Require exact config/profile/both protected values and finite expiry."""

    now = _checked_now()
    if now < context._observed_at.astimezone(UTC):
        raise _clock_invalid()
    if now >= context._expires_at.astimezone(UTC):
        raise _token_expired()
    try:
        marker = _continuity_marker(
            context._continuity_key,
            context._configuration_digest,
            context._profile_id,
            context._target,
            snapshot,
        )
        resolved_at = _utc_time(snapshot._resolved_at)
        expires_at = _utc_time(snapshot._access_token_expires_at)
        if resolved_at > now or expires_at != context._expires_at.astimezone(UTC):
            raise ValueError("snapshot time")
        if not hmac.compare_digest(marker, context._continuity_marker):
            raise ValueError("marker")
    except AdhelmError:
        raise
    except Exception:
        raise _credential_changed() from None


def _continuity_marker(
    key: bytes,
    configuration_digest: str,
    profile_id: str,
    target: _GoogleAdsEditTarget,
    snapshot: oauth_credentials._GoogleAdsEditExecutionSnapshot,
) -> bytes:
    try:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("key")
        _checked_target(target)
        segments = (
            _CONTINUITY_DOMAIN,
            configuration_digest.encode("ascii"),
            profile_id.encode("ascii"),
            target.customer_client_ref.encode("ascii"),
            (target.login_customer_client_ref or "").encode("ascii"),
            target.declared_currency.encode("ascii"),
            target.declared_timezone.encode("utf-8"),
            snapshot.profile_digest.encode("ascii"),
            snapshot.account_ref.encode("ascii"),
            snapshot.principal_ref.encode("ascii"),
            snapshot.client_ref.encode("ascii"),
            snapshot.api_scope.encode("ascii"),
            snapshot.credential_ref.encode("ascii"),
            snapshot._bearer_token.encode("ascii"),
            _utc_time(snapshot._access_token_expires_at).isoformat().encode("ascii"),
        )
        return hmac.new(key, b"\x00".join(segments), hashlib.sha256).digest()
    except Exception:
        raise _session_invalid() from None


def _headers_from_snapshot(
    snapshot: oauth_credentials._GoogleAdsEditExecutionSnapshot, target: _GoogleAdsEditTarget
) -> dict[str, str]:
    try:
        headers = {
            "authorization": f"Bearer {snapshot._bearer_token}",
        }
        if target.login_customer_client_ref is not None:
            headers["login-customer-id"] = target.login_customer_client_ref
        return headers
    except Exception:
        raise _credential_changed() from None


def _checked_directory(value: object) -> Path:
    if not isinstance(value, Path):
        raise _session_invalid()
    try:
        return value.resolve()
    except Exception:
        raise _session_invalid() from None


def _checked_profile_id(value: object) -> str:
    if type(value) is not str or _SAFE_REF.fullmatch(value) is None:
        raise _session_invalid()
    return value


def _checked_expected_digest(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _session_invalid()
    return value


def _checked_configuration(value: object) -> Configuration:
    try:
        return validate_configuration(value)
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation for the Google Ads EDIT session.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _selected_profile(configuration: Configuration, profile_id: str) -> Profile:
    try:
        matches = [
            profile for profile in configuration.profiles if profile.profile_id == profile_id
        ]
        if len(matches) != 1 or type(matches[0]) is not Profile:
            raise ValueError("profile")
        return _checked_profile(matches[0])
    except AdhelmError:
        raise
    except Exception:
        raise _profile_required() from None


def _checked_profile(value: object) -> Profile:
    try:
        if type(value) is not Profile:
            raise ValueError("profile")
        return oauth_credentials._checked_google_ads_edit_profile(value)
    except AdhelmError:
        raise
    except Exception:
        raise _profile_required() from None


def _checked_target(value: object) -> _GoogleAdsEditTarget:
    if type(value) is not _GoogleAdsEditTarget:
        raise ValueError("target")
    return _GoogleAdsEditTarget.model_validate(value.model_dump(mode="python", warnings="error"))


def _checked_now() -> datetime:
    try:
        return _utc_time(_now())
    except Exception:
        raise _clock_invalid() from None


def _utc_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time")
    return value.astimezone(UTC)


def _session_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_SESSION_INVALID",
        "Google Ads EDIT observation session input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
        "A configured Google Ads OAuth EDIT profile is required.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Configure and authorize the selected Google Ads EDIT profile locally.",
    )


def _configuration_changed() -> AdhelmError:
    return AdhelmError(
        "CONFIGURATION_CHANGED",
        "Local configuration changed during the Google Ads EDIT observation.",
        next_action="Review the current configuration before another fixed observation.",
    )


def _credential_changed() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED",
        "Local Google Ads EDIT credentials changed during the fixed observation.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
    )


def _token_expired() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_TOKEN_EXPIRED",
        "The local Google Ads EDIT OAuth token is no longer usable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
    )


def _clock_invalid() -> AdhelmError:
    return AdhelmError(
        "CLOCK_INVALID",
        "A trusted local clock is unavailable for the Google Ads EDIT observation.",
        exit_code=ExitCode.AUTHORIZATION,
    )

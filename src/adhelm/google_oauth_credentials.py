"""Protected local credential lifecycle for the bounded Google desktop OAuth flow.

This module stores a private, canonical credential bundle in a dedicated
OS-store service, using the configured primary credential reference and the
same local lock namespace as ordinary credentials. It does not expose the
bundle as a Pydantic model, configuration, ledger record, result payload, or
exception detail.
"""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock, get_ident
from typing import Literal, Never, Protocol, cast

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthRefreshGrant,
    GoogleOAuthTarget,
    GoogleOAuthTokenDispatchState,
    GoogleOAuthTransportError,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
    is_google_oauth_client_id,
    is_google_oauth_operational_principal_ref,
    revalidate_google_oauth_initial_grant,
    revalidate_google_oauth_refresh_grant,
)
from adhelm.models import (
    GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
    Configuration,
    Profile,
    is_reserved_credential_reference,
    validate_configuration,
    validate_configured_profile,
)

# OAuth bundles deliberately live under their own fixed credential-store
# service. A profile later interpreted as ``OS_STORE`` must never retrieve a
# sealed bundle and pass its JSON as a bearer credential. The reference lock
# namespace remains shared with ordinary OS-store credentials.
_SERVICE = "adhelm.google-oauth-v1"
_MAX_BUNDLE_BYTES = 65_536
_MAX_SECRET_BYTES = 16_384
_CACHE_REFRESH_SKEW = timedelta(seconds=60)
_SAFE_REFERENCE = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?\+00:00$"
)

_BundleState = Literal["READY", "REFRESH_STARTED", "RECONNECT_REQUIRED"]
# An opaque preflight snapshot is issued only by the local slot-read helpers.
# This prevents a constructed lookalike from becoming a write capability.
_SLOT_ISSUER = object()
_SLOT_HMAC_KEY: bytes | None = None
_SLOT_HMAC_KEY_LOCK = Lock()
# Private execution snapshots may cross only into the fixed local binding
# resolver. This is an in-process misuse guard, not a same-user Python trust
# boundary or an authorization credential.
_EXECUTION_SNAPSHOT_ISSUER = object()
# Google Ads PAUSE planning has a second, fixed, protected slot beyond the
# configured OAuth bundle.  It is deliberately a
# process-local issuer guard only: the OS-store value itself supplies the
# durable HMAC key, while this marker prevents a constructed Python object from
# crossing the fixed lifecycle boundary in ordinary callers.
_GOOGLE_ADS_PAUSE_SNAPSHOT_ISSUER = object()
_GOOGLE_ADS_PAUSE_BINDING_DOMAIN = b"adhelm/google-ads/pause/credential-binding/v1\x00"
_GOOGLE_ADS_PAUSE_BINDING_KEY_HEX = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_GOOGLE_ADS_PAUSE_BINDING_REF = re.compile(r"^credential-binding:sha256:[0-9a-f]{64}$", re.ASCII)


class _CredentialStore(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthInstallSlot:
    """Opaque pre-browser snapshot for exactly one protected OS-store slot.

    The expected stored value is intentionally private and is compared only
    under the reference lock immediately before an install/reconnect write.
    Callers must not serialize or retain this object outside their one local
    enrollment attempt.
    """

    _reference: str
    _expected_value_hmac: str
    _replace: bool
    _configuration_digest: str | None
    _profile_id: str | None
    _issuer: object


@dataclass(frozen=True, slots=True)
class GoogleOAuthCredentialResult:
    """Nonsecret lifecycle outcome safe for an allowlisted caller result."""

    provider: Literal["google_ads", "ga4", "ga4_admin", "gtm"]
    slot: Literal["PRIMARY"] = "PRIMARY"
    state: Literal["PRESENT"] = "PRESENT"
    operation: Literal["INSTALL", "RECONNECT"] = "INSTALL"


@dataclass(frozen=True, slots=True, repr=False)
class _GoogleOAuthCredentialBundle:
    state: _BundleState
    profile_digest: str
    provider: str
    purpose: str
    account_ref: str
    principal_ref: str
    client_ref: str
    api_scope: str
    canonical_issuer: str
    raw_subject: str
    requested_scopes: tuple[str, str]
    returned_scopes: tuple[str, ...]
    client_secret: str | None
    refresh_token: str
    refresh_token_expires_at: datetime | None
    access_token: str
    access_token_expires_at: datetime
    refresh_started_at: datetime | None

    def _data(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "access_token_expires_at": _time_text(self.access_token_expires_at),
            "account_ref": self.account_ref,
            "api_scope": self.api_scope,
            "canonical_issuer": self.canonical_issuer,
            "client_ref": self.client_ref,
            "client_secret": self.client_secret,
            "principal_ref": self.principal_ref,
            "profile_digest": self.profile_digest,
            "provider": self.provider,
            "purpose": self.purpose,
            "raw_subject": self.raw_subject,
            "refresh_started_at": (
                None if self.refresh_started_at is None else _time_text(self.refresh_started_at)
            ),
            "refresh_token": self.refresh_token,
            "refresh_token_expires_at": (
                None
                if self.refresh_token_expires_at is None
                else _time_text(self.refresh_token_expires_at)
            ),
            "requested_scopes": list(self.requested_scopes),
            "returned_scopes": list(self.returned_scopes),
            "schema_version": 1,
            "state": self.state,
        }

    def _encoded(self) -> str:
        try:
            return canonical_bytes(self._data()).decode("utf-8")
        except Exception:
            raise _invalid_bundle() from None


class _GoogleOAuthExecutionSnapshot:
    """One private bearer and its sealed, nonsecret bundle association.

    This is deliberately not a public result or serializer surface.  It omits
    the raw OIDC subject, refresh token, client secret, and bundle state.  The
    new execution-binding module consumes it immediately after this lifecycle
    has read, validated, and (when required) refreshed one protected bundle
    under one credential-reference lock.
    """

    _bearer_token: str
    profile_digest: str
    provider: Literal["google_ads", "ga4", "ga4_admin", "gtm"]
    purpose: Literal["READ", "EDIT"]
    account_ref: str
    principal_ref: str
    client_ref: str
    api_scope: str
    _access_token_expires_at: datetime
    _resolved_at: datetime
    _issuer: object

    __slots__ = (
        "_bearer_token",
        "profile_digest",
        "provider",
        "purpose",
        "account_ref",
        "principal_ref",
        "client_ref",
        "api_scope",
        "_access_token_expires_at",
        "_resolved_at",
        "_issuer",
    )

    def __init__(
        self,
        *,
        _bearer_token: str,
        profile_digest: str,
        provider: Literal["google_ads", "ga4", "ga4_admin", "gtm"],
        purpose: Literal["READ", "EDIT"],
        account_ref: str,
        principal_ref: str,
        client_ref: str,
        api_scope: str,
        _access_token_expires_at: datetime,
        _resolved_at: datetime,
        _issuer: object,
    ) -> None:
        if _issuer is not _EXECUTION_SNAPSHOT_ISSUER:
            raise TypeError("Google OAuth execution snapshots are lifecycle-issued only")
        object.__setattr__(self, "_bearer_token", _bearer_token)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "account_ref", account_ref)
        object.__setattr__(self, "principal_ref", principal_ref)
        object.__setattr__(self, "client_ref", client_ref)
        object.__setattr__(self, "api_scope", api_scope)
        object.__setattr__(self, "_access_token_expires_at", _access_token_expires_at)
        object.__setattr__(self, "_resolved_at", _resolved_at)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Google OAuth execution snapshots are immutable")

    def __repr__(self) -> str:
        return "<_GoogleOAuthExecutionSnapshot redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google OAuth execution snapshots cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google OAuth execution snapshots cannot be serialized")


class _GoogleAdsEditExecutionSnapshot:
    """Private Google Ads EDIT association for one fixed session.

    The OAuth bundle remains in the OAuth-only credential service.  Google
    retired developer tokens on 2026-09-09, so no second credential joins it.
    It has no serializer and is consumed immediately by the fixed Google Ads
    EDIT session, which keeps only a keyed continuity marker.
    """

    _bearer_token: str
    profile_digest: str
    provider: Literal["google_ads"]
    purpose: Literal["EDIT"]
    account_ref: str
    principal_ref: str
    client_ref: str
    api_scope: Literal["https://www.googleapis.com/auth/adwords"]
    credential_ref: str
    login_customer_ref: str | None
    currency: str
    timezone: str
    _access_token_expires_at: datetime
    _resolved_at: datetime
    _issuer: object

    __slots__ = (
        "_bearer_token",
        "profile_digest",
        "provider",
        "purpose",
        "account_ref",
        "principal_ref",
        "client_ref",
        "api_scope",
        "credential_ref",
        "login_customer_ref",
        "currency",
        "timezone",
        "_access_token_expires_at",
        "_resolved_at",
        "_issuer",
    )

    def __init__(
        self,
        *,
        _bearer_token: str,
        profile_digest: str,
        provider: Literal["google_ads"],
        purpose: Literal["EDIT"],
        account_ref: str,
        principal_ref: str,
        client_ref: str,
        api_scope: Literal["https://www.googleapis.com/auth/adwords"],
        credential_ref: str,
        login_customer_ref: str | None,
        currency: str,
        timezone: str,
        _access_token_expires_at: datetime,
        _resolved_at: datetime,
        _issuer: object,
    ) -> None:
        if _issuer is not _EXECUTION_SNAPSHOT_ISSUER:
            raise TypeError("Google Ads EDIT execution snapshots are lifecycle-issued only")
        for name, value in (
            ("_bearer_token", _bearer_token),
            ("profile_digest", profile_digest),
            ("provider", provider),
            ("purpose", purpose),
            ("account_ref", account_ref),
            ("principal_ref", principal_ref),
            ("client_ref", client_ref),
            ("api_scope", api_scope),
            ("credential_ref", credential_ref),
            ("login_customer_ref", login_customer_ref),
            ("currency", currency),
            ("timezone", timezone),
            ("_access_token_expires_at", _access_token_expires_at),
            ("_resolved_at", _resolved_at),
            ("_issuer", _issuer),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> Never:
        del name, value
        raise AttributeError("Google Ads EDIT execution snapshots are immutable")

    def __repr__(self) -> str:
        return "<_GoogleAdsEditExecutionSnapshot redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads EDIT execution snapshots cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads EDIT execution snapshots cannot be serialized")


class _GoogleAdsPauseProtectedSnapshot:
    """Private two-slot association for one future Google Ads PAUSE plan.

    The public M55 action receives only the nonsecret execution facts exposed
    here.  The bearer, complete OAuth bundle association, and
    fixed private binding key remain inside this lifecycle module and the
    future fixed planning session.  A snapshot is not a credential, provider
    permission, or execution authorization.
    """

    _bearer_token: str
    profile_ref: str
    profile_configuration_digest: str
    account_client_ref: str
    currency: str
    timezone: str
    client_application_ref: str
    principal_ref: str
    required_scope: Literal["https://www.googleapis.com/auth/adwords"]
    login_customer_client_ref: str | None
    credential_binding_ref: str
    configuration_digest: str
    _resolved_at: datetime
    _expires_at: datetime
    _issuer: object

    __slots__ = (
        "_bearer_token",
        "profile_ref",
        "profile_configuration_digest",
        "account_client_ref",
        "currency",
        "timezone",
        "client_application_ref",
        "principal_ref",
        "required_scope",
        "login_customer_client_ref",
        "credential_binding_ref",
        "configuration_digest",
        "_resolved_at",
        "_expires_at",
        "_issuer",
    )

    def __init__(
        self,
        *,
        _bearer_token: str,
        profile_ref: str,
        profile_configuration_digest: str,
        account_client_ref: str,
        currency: str,
        timezone: str,
        client_application_ref: str,
        principal_ref: str,
        required_scope: Literal["https://www.googleapis.com/auth/adwords"],
        login_customer_client_ref: str | None,
        credential_binding_ref: str,
        configuration_digest: str,
        _resolved_at: datetime,
        _expires_at: datetime,
        _issuer: object,
    ) -> None:
        if _issuer is not _GOOGLE_ADS_PAUSE_SNAPSHOT_ISSUER:
            raise TypeError("Google Ads PAUSE snapshots are lifecycle-issued only")
        for name, value in (
            ("_bearer_token", _bearer_token),
            ("profile_ref", profile_ref),
            ("profile_configuration_digest", profile_configuration_digest),
            ("account_client_ref", account_client_ref),
            ("currency", currency),
            ("timezone", timezone),
            ("client_application_ref", client_application_ref),
            ("principal_ref", principal_ref),
            ("required_scope", required_scope),
            ("login_customer_client_ref", login_customer_client_ref),
            ("credential_binding_ref", credential_binding_ref),
            ("configuration_digest", configuration_digest),
            ("_resolved_at", _resolved_at),
            ("_expires_at", _expires_at),
            ("_issuer", _issuer),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> Never:
        del name, value
        raise AttributeError("Google Ads PAUSE snapshots are immutable")

    def __repr__(self) -> str:
        return "<_GoogleAdsPauseProtectedSnapshot redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads PAUSE snapshots cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads PAUSE snapshots cannot be serialized")


def prepare_google_oauth_slot(credential_ref: str, *, replace: bool) -> GoogleOAuthInstallSlot:
    """Capture one hidden slot value before a browser authorization begins.

    New enrollment requires an empty slot.  Reconnect captures either a present
    or absent slot, so an explicitly configured profile can recover after a
    prior local install failure or deliberate local removal.  The returned
    object binds a later install to the exact value seen here.
    """
    reference = _checked_reference(credential_ref)
    with credential_mutation_lock(reference):
        existing = _read_slot(_credential_backend(), reference)
    if not replace and existing is not None:
        raise _credential_already_exists()
    return GoogleOAuthInstallSlot(
        _reference=reference,
        _expected_value_hmac=_slot_value_hmac(reference, existing),
        _replace=replace,
        _configuration_digest=None,
        _profile_id=None,
        _issuer=_SLOT_ISSUER,
    )


def prepare_google_oauth_install(
    configuration: Configuration, profile_id: str, *, replace: bool
) -> GoogleOAuthInstallSlot:
    """Capture a reconnect slot and its exact validated configuration binding."""
    validated = _validated_configuration(configuration)
    _validate_configuration_references(validated)
    profile = _selected_google_oauth_profile(validated, profile_id)
    slot = prepare_google_oauth_slot(profile.credential_ref, replace=replace)
    return GoogleOAuthInstallSlot(
        _reference=slot._reference,
        _expected_value_hmac=slot._expected_value_hmac,
        _replace=slot._replace,
        _configuration_digest=digest(validated),
        _profile_id=profile.profile_id,
        _issuer=_SLOT_ISSUER,
    )


def install_google_oauth_grant(
    configuration: Configuration,
    profile_id: str,
    grant: GoogleOAuthInitialGrant,
    *,
    client_secret: str | None,
    expected_slot: GoogleOAuthInstallSlot,
    replace: bool = False,
) -> GoogleOAuthCredentialResult:
    """Seal one verified initial/reconnect grant after exact slot revalidation.

    This function has no browser, callback, configuration-write, or provider
    resource operation.  It writes only the private local bundle after the
    caller has independently finalized the selected profile configuration.
    """
    validated = _validated_configuration(configuration)
    _validate_configuration_references(validated)
    profile = _selected_google_oauth_profile(validated, profile_id)
    checked_grant = revalidate_google_oauth_initial_grant(grant)
    checked_slot = _checked_install_slot(expected_slot, profile, validated, replace=replace)
    _validate_initial_grant_binding(profile, checked_grant)
    _validate_client_secret(client_secret)

    with credential_mutation_lock(profile.credential_ref):
        backend = _credential_backend()
        current = _read_slot(backend, profile.credential_ref)
        if not hmac.compare_digest(
            _slot_value_hmac(profile.credential_ref, current), checked_slot._expected_value_hmac
        ):
            raise _concurrent_change()
        if not replace and current is not None:
            raise _concurrent_change()
        before_write_now = _checked_now()
        bundle = _bundle_from_initial(profile, checked_grant, client_secret, before_write_now)
        try:
            _write_bundle(backend, profile.credential_ref, bundle)
        except AdhelmError as error:
            if error.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED":
                # READY might have been retained while its immediate readback
                # failed. Try to leave a durable non-retry state without
                # masking the original local-state ambiguity.
                _best_effort_reconnect_required(
                    backend, profile.credential_ref, bundle, before_write_now
                )
            raise
        after_write_now = _checked_now()
        if after_write_now < before_write_now or after_write_now >= bundle.access_token_expires_at:
            _persist_reconnect_required(backend, profile.credential_ref, bundle, after_write_now)
            raise _grant_expired()

    return GoogleOAuthCredentialResult(
        provider=_google_provider(profile),
        operation="RECONNECT" if replace else "INSTALL",
    )


@dataclass(frozen=True, slots=True)
class GoogleOAuthLocalStatus:
    """Bounded nonsecret local credential state for one Google OAuth profile.

    Nothing here contacts Google.  No access token, refresh token, client
    secret, raw OIDC subject, or bundle encoding is read out: the identity is
    the already-pseudonymous `principal_ref` this build derives from the
    canonical issuer and subject.

    `identity_last_verified_at` is deliberately `None`.  The protected bundle
    schema retains no successful-verification instant, and inventing one would
    be a claim rather than a reading.  What *is* proven is the bound in
    `identity_last_verified_not_after`: the currently stored access token was
    issued by an authorization or refresh that re-verified this exact subject,
    and that token expires at the recorded instant, so the verification
    happened at some point before it.
    """

    profile_configuration_digest: str
    provider: Literal["google_ads", "ga4", "ga4_admin", "gtm"]
    purpose: Literal["READ", "EDIT"]
    local_state: Literal["READY", "REFRESH_STARTED", "RECONNECT_REQUIRED", "MISSING", "UNVERIFIED"]
    identity_pseudonym: str | None
    requested_scopes: tuple[str, ...]
    granted_scopes: tuple[str, ...]
    unrequested_granted_scopes: tuple[str, ...]
    access_token_expires_at: datetime | None
    access_token_expired: bool | None
    refresh_token_expires_at: datetime | None
    refresh_token_expired: bool | None
    identity_last_verified_at: None
    identity_last_verified_not_after: datetime | None
    primary_slot_present: bool


def google_oauth_local_status(
    configuration: Configuration, profile_id: str
) -> GoogleOAuthLocalStatus:
    """Read bounded local Google OAuth state; this makes no Google request.

    The protected slot is read under its own reference lock and released before
    this function returns.  No lock is held across any network call, because
    this path performs none: an expired access token is *reported* here and is
    refreshed only by a deliberate later read that needs one.
    """
    validated = _validated_configuration(configuration)
    _validate_configuration_references(validated)
    profile = _selected_google_oauth_profile(validated, profile_id)
    digest_value = digest(profile)
    with credential_mutation_lock(profile.credential_ref):
        raw = _read_slot(_credential_backend(), profile.credential_ref)
    if raw is None:
        return GoogleOAuthLocalStatus(
            profile_configuration_digest=digest_value,
            provider=_google_provider(profile),
            purpose=_checked_purpose(profile.purpose),
            local_state="MISSING",
            identity_pseudonym=None,
            requested_scopes=(),
            granted_scopes=(),
            unrequested_granted_scopes=(),
            access_token_expires_at=None,
            access_token_expired=None,
            refresh_token_expires_at=None,
            refresh_token_expired=None,
            identity_last_verified_at=None,
            identity_last_verified_not_after=None,
            primary_slot_present=False,
        )
    try:
        bundle = _decode_bundle(raw)
        _validate_bundle_binding(bundle, profile)
    except AdhelmError:
        # A slot that exists but cannot be decoded or bound to this exact
        # profile is reported as unverified.  It is never repaired, refreshed,
        # or removed by a status read, and no part of its content is disclosed.
        return GoogleOAuthLocalStatus(
            profile_configuration_digest=digest_value,
            provider=_google_provider(profile),
            purpose=_checked_purpose(profile.purpose),
            local_state="UNVERIFIED",
            identity_pseudonym=None,
            requested_scopes=(),
            granted_scopes=(),
            unrequested_granted_scopes=(),
            access_token_expires_at=None,
            access_token_expired=None,
            refresh_token_expires_at=None,
            refresh_token_expired=None,
            identity_last_verified_at=None,
            identity_last_verified_not_after=None,
            primary_slot_present=True,
        )
    now = _checked_now()
    requested = tuple(bundle.requested_scopes)
    granted = tuple(bundle.returned_scopes)
    return GoogleOAuthLocalStatus(
        profile_configuration_digest=digest_value,
        provider=_google_provider(profile),
        purpose=_checked_purpose(profile.purpose),
        local_state=bundle.state,
        identity_pseudonym=bundle.principal_ref,
        requested_scopes=requested,
        granted_scopes=granted,
        # Permissions Google echoed that this enrollment never requested. They
        # are reported, never recorded as local authority.
        unrequested_granted_scopes=tuple(scope for scope in granted if scope not in set(requested)),
        access_token_expires_at=bundle.access_token_expires_at,
        access_token_expired=bundle.access_token_expires_at <= now,
        refresh_token_expires_at=bundle.refresh_token_expires_at,
        refresh_token_expired=(
            None
            if bundle.refresh_token_expires_at is None
            else bundle.refresh_token_expires_at <= now
        ),
        identity_last_verified_at=None,
        identity_last_verified_not_after=bundle.access_token_expires_at,
        primary_slot_present=True,
    )


def remove_google_oauth_bundle(profile: Profile) -> None:
    """Delete one local protected bundle with readback; it never revokes Google."""
    checked = _validated_google_oauth_profile(profile)
    with credential_mutation_lock(checked.credential_ref):
        backend = _credential_backend()
        try:
            backend.delete_password(_SERVICE, checked.credential_ref)
        except Exception:
            pass
        _verify_removed(backend, checked.credential_ref)
    return None


def google_oauth_bundle_present(profile: Profile) -> bool:
    """Return bounded protected-bundle presence for source-aware status only.

    This deliberately does not decode a bundle or claim usable provider access.
    It avoids probing the ordinary OS-store service for OAuth primary state.
    """
    checked = _validated_google_oauth_profile(profile)
    with credential_mutation_lock(checked.credential_ref):
        return _read_slot(_credential_backend(), checked.credential_ref) is not None


def resolve_google_oauth_access_token(profile: Profile) -> str:
    """Return one currently usable access token or fail closed before resource egress.

    A cached token is used only outside its near-expiry window.  Refresh marks
    the bundle ``REFRESH_STARTED`` and proves that marker through readback
    before fixed OAuth token egress.  Any ambiguous or rejected outcome blocks
    automatic retry until a deliberate reconnect writes a new verified grant.
    """
    return _resolve_google_oauth_execution_snapshot(profile)._bearer_token


def _resolve_google_oauth_execution_snapshot(profile: Profile) -> _GoogleOAuthExecutionSnapshot:
    """Resolve one protected bearer and exact sealed association under one lock.

    The caller receives no raw OIDC subject, refresh token, client secret, or
    bundle encoding.  The existing public token resolver delegates here so an
    execution binding never re-reads or refreshes a second bundle.
    """

    checked = _validated_google_oauth_profile(profile)
    with credential_mutation_lock(checked.credential_ref):
        bundle, resolved_at = _resolve_google_oauth_bundle_locked(_credential_backend(), checked)
        return _GoogleOAuthExecutionSnapshot(
            _bearer_token=bundle.access_token,
            profile_digest=bundle.profile_digest,
            provider=_google_provider(checked),
            purpose=cast(Literal["READ", "EDIT"], checked.purpose),
            account_ref=bundle.account_ref,
            principal_ref=bundle.principal_ref,
            client_ref=bundle.client_ref,
            api_scope=bundle.api_scope,
            _access_token_expires_at=bundle.access_token_expires_at,
            _resolved_at=resolved_at,
            _issuer=_EXECUTION_SNAPSHOT_ISSUER,
        )


def _resolve_google_ads_edit_execution_snapshot(
    profile: Profile,
) -> _GoogleAdsEditExecutionSnapshot:
    """Resolve one sealed Google Ads EDIT OAuth association.

    This locks the one configured OAuth reference and calls the existing
    primary-bundle locked core.  No generic credential resolver is involved,
    and callers receive no serializable secret surface.
    """

    checked = _checked_google_ads_edit_profile(profile)
    with credential_mutation_lock(checked.credential_ref):
        bundle, resolved_at = _resolve_google_oauth_bundle_locked(_credential_backend(), checked)
        return _GoogleAdsEditExecutionSnapshot(
            _bearer_token=bundle.access_token,
            profile_digest=bundle.profile_digest,
            provider="google_ads",
            purpose="EDIT",
            account_ref=bundle.account_ref,
            principal_ref=bundle.principal_ref,
            client_ref=bundle.client_ref,
            api_scope="https://www.googleapis.com/auth/adwords",
            credential_ref=checked.credential_ref,
            login_customer_ref=checked.login_customer_ref,
            currency=cast(str, checked.currency),
            timezone=checked.timezone,
            _access_token_expires_at=bundle.access_token_expires_at,
            _resolved_at=resolved_at,
            _issuer=_EXECUTION_SNAPSHOT_ISSUER,
        )


def _checked_google_ads_edit_execution_snapshot(
    value: object, profile: Profile
) -> _GoogleAdsEditExecutionSnapshot:
    """Accept only the lifecycle-issued snapshot for this profile."""

    if type(value) is not _GoogleAdsEditExecutionSnapshot:
        raise _invalid_google_ads_edit_execution_snapshot()
    checked = _checked_google_ads_edit_profile(profile)
    if checked.currency is None:
        raise _invalid_google_ads_edit_execution_snapshot()
    try:
        issuer = value._issuer
        bearer_token = value._bearer_token
        profile_digest = value.profile_digest
        provider = value.provider
        purpose = value.purpose
        account_ref = value.account_ref
        principal_ref = value.principal_ref
        client_ref = value.client_ref
        api_scope = value.api_scope
        credential_ref = value.credential_ref
        login_customer_ref = value.login_customer_ref
        currency = value.currency
        timezone = value.timezone
        expires_at = value._access_token_expires_at
        resolved_at = value._resolved_at
        expected_values = (
            (profile_digest, digest(checked)),
            (provider, "google_ads"),
            (purpose, "EDIT"),
            (account_ref, checked.account_ref),
            (principal_ref, checked.principal_ref),
            (client_ref, checked.client_ref),
            (api_scope, "https://www.googleapis.com/auth/adwords"),
            (credential_ref, checked.credential_ref),
            (currency, checked.currency),
            (timezone, checked.timezone),
        )
        if issuer is not _EXECUTION_SNAPSHOT_ISSUER or not all(
            type(actual) is str and type(expected) is str and hmac.compare_digest(actual, expected)
            for actual, expected in expected_values
        ):
            raise ValueError("snapshot association")
        if login_customer_ref != checked.login_customer_ref:
            raise ValueError("login customer")
        _checked_secret_text(bearer_token, name="token", maximum=_MAX_SECRET_BYTES)
        if (
            type(expires_at) is not datetime
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or type(resolved_at) is not datetime
            or resolved_at.tzinfo is None
            or resolved_at.utcoffset() is None
            or resolved_at.astimezone(UTC) >= expires_at.astimezone(UTC)
        ):
            raise ValueError("snapshot expiry")
    except AdhelmError:
        raise _invalid_google_ads_edit_execution_snapshot() from None
    except Exception:
        raise _invalid_google_ads_edit_execution_snapshot() from None
    return value


def _prepare_google_ads_pause_protected_snapshot(
    profile: Profile, configuration_digest: str
) -> _GoogleAdsPauseProtectedSnapshot:
    """Issue one sealed two-slot Google Ads PAUSE planning snapshot.

    This is the only PAUSE helper permitted to refresh the OAuth bundle or
    create the dedicated local binding key.  A caller must already hold its
    configuration transaction; this helper then acquires the configured OAuth
    and fixed binding-key references in casefolded order.
    """

    checked = _checked_google_ads_edit_profile(profile)
    checked_configuration_digest = _checked_google_ads_pause_configuration_digest(
        configuration_digest
    )
    references = _google_ads_pause_lock_references(checked)
    with ExitStack() as stack:
        for reference in references:
            stack.enter_context(credential_mutation_lock(reference))
        bundle, resolved_at = _resolve_google_oauth_bundle_locked(_credential_backend(), checked)
        generic_backend, generic_service = _generic_credential_store()
        binding_key = _prepare_google_ads_pause_binding_key_locked(generic_backend, generic_service)
        return _new_google_ads_pause_protected_snapshot(
            profile=checked,
            configuration_digest=checked_configuration_digest,
            bundle=bundle,
            binding_key=binding_key,
            resolved_at=resolved_at,
        )


def _current_google_ads_pause_protected_snapshot(
    profile: Profile,
    configuration_digest: str,
    expected_binding: object,
) -> _GoogleAdsPauseProtectedSnapshot:
    """Re-read both PAUSE slots without refresh, write, or key creation.

    The expected snapshot is a private capability held by the fixed planning
    session.  It is structurally checked before any protected-store read, then
    compared to a fresh no-refresh snapshot under both ordered locks.
    """

    checked = _checked_google_ads_edit_profile(profile)
    checked_configuration_digest = _checked_google_ads_pause_configuration_digest(
        configuration_digest
    )
    expected = _checked_google_ads_pause_protected_snapshot(expected_binding)
    _assert_google_ads_pause_snapshot_context(expected, checked, checked_configuration_digest)
    references = _google_ads_pause_lock_references(checked)
    with ExitStack() as stack:
        for reference in references:
            stack.enter_context(credential_mutation_lock(reference))
        return _current_google_ads_pause_protected_snapshot_locked(
            checked,
            checked_configuration_digest,
            expected,
        )


@contextmanager
def _google_ads_pause_retention_recheck(
    profile: Profile,
    configuration_digest: str,
    expected_binding: object,
) -> Iterator[Callable[[], datetime]]:
    """Hold verified local PAUSE facts for one owner-only append callback.

    The caller supplies a profile selected while its configuration transaction
    remains open.  This helper then holds both protected-slot locks.  Its
    callback rereads the two slots through the already-held local handles
    and checks the trusted clock.  It does not acquire a new backend, reenter a
    lock, refresh, write, use a provider route, or touch the ledger.  At least
    one callback is required before the context closes.
    """

    checked = _checked_google_ads_edit_profile(profile)
    checked_configuration_digest = _checked_google_ads_pause_configuration_digest(
        configuration_digest
    )
    expected = _checked_google_ads_pause_protected_snapshot(expected_binding)
    _assert_google_ads_pause_snapshot_context(expected, checked, checked_configuration_digest)
    references = _google_ads_pause_lock_references(checked)
    with ExitStack() as stack:
        for reference in references:
            stack.enter_context(credential_mutation_lock(reference))
        oauth_backend = _credential_backend()
        generic_backend, generic_service = _generic_credential_store()
        expected = _current_google_ads_pause_protected_snapshot_from_handles_locked(
            checked,
            checked_configuration_digest,
            expected,
            oauth_backend=oauth_backend,
            generic_backend=generic_backend,
            generic_service=generic_service,
        )
        owner_thread = get_ident()
        active = True
        callback_count = 0

        def recheck() -> datetime:
            nonlocal callback_count, expected
            if not active or get_ident() != owner_thread:
                raise _google_ads_pause_retention_recheck_required()
            try:
                # This is intentionally a local reread through the exact
                # handles captured while both locks were acquired.  It
                # never obtains a backend, reacquires a lock, dispatches
                # refresh, writes a slot, opens a provider route, or touches
                # the ledger, but it detects manual protected-store drift that
                # happened after the retention context opened.
                _checked_google_ads_pause_protected_snapshot(expected)
                expected = _current_google_ads_pause_protected_snapshot_from_handles_locked(
                    checked,
                    checked_configuration_digest,
                    expected,
                    oauth_backend=oauth_backend,
                    generic_backend=generic_backend,
                    generic_service=generic_service,
                )
            except AdhelmError:
                raise
            except Exception:
                raise _google_ads_pause_credential_changed() from None
            callback_count += 1
            return expected._resolved_at

        try:
            yield recheck
            if callback_count == 0:
                raise _google_ads_pause_retention_recheck_required()
        finally:
            active = False


def _current_google_ads_pause_protected_snapshot_locked(
    profile: Profile,
    configuration_digest: str,
    expected: _GoogleAdsPauseProtectedSnapshot,
) -> _GoogleAdsPauseProtectedSnapshot:
    """Build and compare one no-refresh snapshot while both locks are held."""

    oauth_backend = _credential_backend()
    generic_backend, generic_service = _generic_credential_store()
    return _current_google_ads_pause_protected_snapshot_from_handles_locked(
        profile,
        configuration_digest,
        expected,
        oauth_backend=oauth_backend,
        generic_backend=generic_backend,
        generic_service=generic_service,
    )


def _current_google_ads_pause_protected_snapshot_from_handles_locked(
    profile: Profile,
    configuration_digest: str,
    expected: _GoogleAdsPauseProtectedSnapshot,
    *,
    oauth_backend: _CredentialStore,
    generic_backend: _CredentialStore,
    generic_service: str,
) -> _GoogleAdsPauseProtectedSnapshot:
    """Use captured locked handles to prove the current two-slot association.

    This narrow core is used by the retention callback.  It must stay free of
    backend getters, lock acquisition, refresh dispatch, and writes so the
    caller can safely invoke it while the ledger owns its stream lock.
    """

    try:
        bundle, resolved_at = _read_google_ads_pause_bundle_no_refresh_locked(
            oauth_backend, profile
        )
        binding_key = _read_google_ads_pause_binding_key_locked(generic_backend, generic_service)
        current = _new_google_ads_pause_protected_snapshot(
            profile=profile,
            configuration_digest=configuration_digest,
            bundle=bundle,
            binding_key=binding_key,
            resolved_at=resolved_at,
        )
        _assert_google_ads_pause_snapshot_matches(current, expected)
        return current
    except AdhelmError as error:
        if error.code in {
            "GOOGLE_ADS_PAUSE_BINDING_KEY_UNAVAILABLE",
            "CLOCK_INVALID",
            "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED",
        }:
            raise
        raise _google_ads_pause_credential_changed() from None
    except Exception:
        raise _google_ads_pause_credential_changed() from None


def _read_google_ads_pause_bundle_no_refresh_locked(
    backend: _CredentialStore, profile: Profile
) -> tuple[_GoogleOAuthCredentialBundle, datetime]:
    """Read a READY bundle without dispatching OAuth or persisting any state."""

    try:
        raw = _read_slot(backend, profile.credential_ref)
        if raw is None:
            raise ValueError("missing bundle")
        bundle = _decode_bundle(raw)
        _validate_bundle_binding(bundle, profile)
        observed_at = _checked_now()
        if (
            bundle.state != "READY"
            or (
                bundle.refresh_token_expires_at is not None
                and bundle.refresh_token_expires_at.astimezone(UTC) <= observed_at
            )
            or bundle.access_token_expires_at.astimezone(UTC) <= observed_at
        ):
            raise ValueError("bundle is not current")
        resolved_at = _checked_now()
        if resolved_at < observed_at:
            raise _google_ads_pause_clock_invalid()
        if resolved_at >= bundle.access_token_expires_at.astimezone(UTC):
            raise ValueError("bundle expired between trusted samples")
        return bundle, resolved_at
    except AdhelmError as error:
        if error.code == "GOOGLE_OAUTH_CLOCK_UNAVAILABLE":
            raise _google_ads_pause_clock_invalid() from None
        if error.code == "CLOCK_INVALID":
            raise
        raise _google_ads_pause_credential_changed() from None
    except Exception:
        raise _google_ads_pause_credential_changed() from None


def _new_google_ads_pause_protected_snapshot(
    *,
    profile: Profile,
    configuration_digest: str,
    bundle: _GoogleOAuthCredentialBundle,
    binding_key: bytes,
    resolved_at: datetime,
) -> _GoogleAdsPauseProtectedSnapshot:
    """Detach one private snapshot after all local protected facts are proved."""

    checked = _checked_google_ads_edit_profile(profile)
    checked_configuration_digest = _checked_google_ads_pause_configuration_digest(
        configuration_digest
    )
    try:
        _validate_bundle_binding(bundle, checked)
        _checked_google_ads_pause_binding_key_bytes(binding_key)
        expires_at = _pause_utc_time(bundle.access_token_expires_at)
        checked_resolved_at = _pause_utc_time(resolved_at)
        if checked_resolved_at >= expires_at:
            raise ValueError("expiry")
        currency = checked.currency
        if type(currency) is not str:
            raise ValueError("currency")
        return _GoogleAdsPauseProtectedSnapshot(
            _bearer_token=_checked_secret_text(
                bundle.access_token, name="token", maximum=_MAX_SECRET_BYTES
            ),
            profile_ref=checked.profile_id,
            profile_configuration_digest=digest(checked),
            account_client_ref=checked.account_ref,
            currency=currency,
            timezone=checked.timezone,
            client_application_ref=bundle.client_ref,
            principal_ref=bundle.principal_ref,
            required_scope="https://www.googleapis.com/auth/adwords",
            login_customer_client_ref=checked.login_customer_ref,
            credential_binding_ref=_google_ads_pause_credential_binding_ref(
                binding_key=binding_key,
                profile=checked,
                configuration_digest=checked_configuration_digest,
                bundle=bundle,
            ),
            configuration_digest=checked_configuration_digest,
            _resolved_at=checked_resolved_at,
            _expires_at=expires_at,
            _issuer=_GOOGLE_ADS_PAUSE_SNAPSHOT_ISSUER,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _google_ads_pause_protected_snapshot_invalid() from None


def _google_ads_pause_credential_binding_ref(
    *,
    binding_key: bytes,
    profile: Profile,
    configuration_digest: str,
    bundle: _GoogleOAuthCredentialBundle,
) -> str:
    """Return the stable nonsecret ref for complete canonical protected facts."""

    try:
        _checked_google_ads_pause_binding_key_bytes(binding_key)
        checked = _checked_google_ads_edit_profile(profile)
        payload: dict[str, object] = {
            "account_client_ref": checked.account_ref,
            "configuration_digest": _checked_google_ads_pause_configuration_digest(
                configuration_digest
            ),
            "currency": checked.currency,
            "login_customer_client_ref": checked.login_customer_ref,
            "oauth_credential_ref": checked.credential_ref,
            # This is the complete canonical protected bundle, including its
            # refresh/client secrets, state, and finite expiry provenance.  It
            # remains only HMAC input and is never returned or persisted here.
            "oauth_bundle": bundle._data(),
            "principal_ref": checked.principal_ref,
            "profile_configuration_digest": digest(checked),
            "profile_ref": checked.profile_id,
            "provider": "google_ads",
            "purpose": "EDIT",
            "required_scope": "https://www.googleapis.com/auth/adwords",
            "schema_version": 1,
            "timezone": checked.timezone,
            "client_application_ref": checked.client_ref,
        }
        encoded = canonical_bytes(payload)
        value = hmac.new(
            binding_key,
            _GOOGLE_ADS_PAUSE_BINDING_DOMAIN + encoded,
            "sha256",
        ).hexdigest()
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("binding digest")
        return f"credential-binding:sha256:{value}"
    except AdhelmError:
        raise
    except Exception:
        raise _google_ads_pause_protected_snapshot_invalid() from None


def _checked_google_ads_pause_protected_snapshot(
    value: object,
) -> _GoogleAdsPauseProtectedSnapshot:
    """Reject a constructed or malformed PAUSE snapshot before slot access."""

    if type(value) is not _GoogleAdsPauseProtectedSnapshot:
        raise _google_ads_pause_protected_snapshot_invalid()
    try:
        issuer = value._issuer
        bearer_token = value._bearer_token
        profile_ref = value.profile_ref
        profile_digest = value.profile_configuration_digest
        account_client_ref = value.account_client_ref
        currency = value.currency
        timezone = value.timezone
        client_application_ref = value.client_application_ref
        principal_ref = value.principal_ref
        required_scope = value.required_scope
        login_customer_client_ref = value.login_customer_client_ref
        credential_binding_ref = value.credential_binding_ref
        configuration_digest = value.configuration_digest
        resolved_at = value._resolved_at
        expires_at = value._expires_at
        if (
            issuer is not _GOOGLE_ADS_PAUSE_SNAPSHOT_ISSUER
            or type(profile_ref) is not str
            or _SAFE_REFERENCE.fullmatch(profile_ref) is None
            or type(profile_digest) is not str
            or _DIGEST.fullmatch(profile_digest) is None
            or type(account_client_ref) is not str
            or re.fullmatch(r"[0-9]{10}", account_client_ref) is None
            or type(currency) is not str
            or re.fullmatch(r"[A-Z]{3}", currency) is None
            or type(timezone) is not str
            or not timezone
            or not is_google_oauth_client_id(client_application_ref)
            or not is_google_oauth_operational_principal_ref(principal_ref)
            or required_scope != "https://www.googleapis.com/auth/adwords"
            or (
                login_customer_client_ref is not None
                and (
                    type(login_customer_client_ref) is not str
                    or re.fullmatch(r"[0-9]{10}", login_customer_client_ref) is None
                )
            )
            or type(credential_binding_ref) is not str
            or _GOOGLE_ADS_PAUSE_BINDING_REF.fullmatch(credential_binding_ref) is None
            or type(configuration_digest) is not str
            or _DIGEST.fullmatch(configuration_digest) is None
        ):
            raise ValueError("snapshot association")
        _checked_secret_text(bearer_token, name="token", maximum=_MAX_SECRET_BYTES)
        checked_resolved_at = _pause_utc_time(resolved_at)
        checked_expires_at = _pause_utc_time(expires_at)
        if checked_resolved_at >= checked_expires_at:
            raise ValueError("snapshot expiry")
    except AdhelmError:
        raise _google_ads_pause_protected_snapshot_invalid() from None
    except Exception:
        raise _google_ads_pause_protected_snapshot_invalid() from None
    return value


def _assert_google_ads_pause_snapshot_context(
    expected: _GoogleAdsPauseProtectedSnapshot,
    profile: Profile,
    configuration_digest: str,
) -> None:
    """Require an issued snapshot to belong to this exact local profile/context."""

    try:
        checked = _checked_google_ads_edit_profile(profile)
        expected_values = (
            (expected.profile_ref, checked.profile_id),
            (expected.profile_configuration_digest, digest(checked)),
            (expected.account_client_ref, checked.account_ref),
            (expected.currency, checked.currency),
            (expected.timezone, checked.timezone),
            (expected.client_application_ref, checked.client_ref),
            (expected.principal_ref, checked.principal_ref),
            (expected.required_scope, "https://www.googleapis.com/auth/adwords"),
            (expected.configuration_digest, configuration_digest),
        )
        if (
            not all(
                type(actual) is str and type(wanted) is str and hmac.compare_digest(actual, wanted)
                for actual, wanted in expected_values
            )
            or expected.login_customer_client_ref != checked.login_customer_ref
        ):
            raise ValueError("snapshot context")
    except AdhelmError:
        raise
    except Exception:
        raise _google_ads_pause_credential_changed() from None


def _assert_google_ads_pause_snapshot_matches(
    current: _GoogleAdsPauseProtectedSnapshot,
    expected: _GoogleAdsPauseProtectedSnapshot,
) -> None:
    """Reject changed protected data, expiry, or a clock moving before preparation."""

    try:
        expected_values = (
            (current.profile_ref, expected.profile_ref),
            (current.profile_configuration_digest, expected.profile_configuration_digest),
            (current.account_client_ref, expected.account_client_ref),
            (current.currency, expected.currency),
            (current.timezone, expected.timezone),
            (current.client_application_ref, expected.client_application_ref),
            (current.principal_ref, expected.principal_ref),
            (current.required_scope, expected.required_scope),
            (current.configuration_digest, expected.configuration_digest),
            (current.credential_binding_ref, expected.credential_binding_ref),
        )
        if (
            not all(
                type(actual) is str and type(wanted) is str and hmac.compare_digest(actual, wanted)
                for actual, wanted in expected_values
            )
            or current.login_customer_client_ref != expected.login_customer_client_ref
        ):
            raise ValueError("protected association")
        if current._expires_at.astimezone(UTC) != expected._expires_at.astimezone(
            UTC
        ) or current._resolved_at.astimezone(UTC) < expected._resolved_at.astimezone(UTC):
            raise _google_ads_pause_clock_invalid()
    except AdhelmError:
        raise
    except Exception:
        raise _google_ads_pause_credential_changed() from None


def _google_ads_pause_lock_references(profile: Profile) -> tuple[str, str]:
    """Return exactly the two distinct PAUSE slots in deterministic lock order."""

    references = tuple(
        sorted(
            (profile.credential_ref, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE),
            key=str.casefold,
        )
    )
    if len(references) != 2 or len({reference.casefold() for reference in references}) != 2:
        raise _google_ads_edit_profile_required()
    return references


def _prepare_google_ads_pause_binding_key_locked(backend: _CredentialStore, service: str) -> bytes:
    """Create/read the fixed PAUSE HMAC key with exact private readback proof."""

    try:
        value = backend.get_password(service, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)
        if value is None:
            generated = secrets.token_hex(32)
            _google_ads_pause_binding_key_from_stored_hex(generated)
            backend.set_password(service, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE, generated)
            observed = backend.get_password(service, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)
            if type(observed) is not str or not hmac.compare_digest(observed, generated):
                raise ValueError("binding key readback")
            value = observed
        return _google_ads_pause_binding_key_from_stored_hex(value)
    except AdhelmError:
        raise
    except Exception:
        raise _google_ads_pause_binding_key_unavailable() from None


def _read_google_ads_pause_binding_key_locked(backend: _CredentialStore, service: str) -> bytes:
    """Read the existing fixed PAUSE HMAC key; never create or repair it."""

    try:
        value = backend.get_password(service, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)
        return _google_ads_pause_binding_key_from_stored_hex(value)
    except AdhelmError:
        raise
    except Exception:
        raise _google_ads_pause_binding_key_unavailable() from None


def _google_ads_pause_binding_key_from_stored_hex(value: object) -> bytes:
    if type(value) is not str or _GOOGLE_ADS_PAUSE_BINDING_KEY_HEX.fullmatch(value) is None:
        raise ValueError("binding key")
    decoded = bytes.fromhex(value)
    return _checked_google_ads_pause_binding_key_bytes(decoded)


def _checked_google_ads_pause_binding_key_bytes(value: object) -> bytes:
    if type(value) is not bytes:
        raise ValueError("binding key")
    decoded = value
    if len(decoded) != 32:
        raise ValueError("binding key")
    return decoded


def _checked_google_ads_pause_configuration_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _google_ads_pause_protected_snapshot_invalid()
    return value


def _pause_utc_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time")
    return value.astimezone(UTC)


def _resolve_google_oauth_bundle_locked(
    backend: _CredentialStore, profile: Profile
) -> tuple[_GoogleOAuthCredentialBundle, datetime]:
    """Locked cache-or-refresh core shared by token and execution resolution."""

    raw = _read_slot(backend, profile.credential_ref)
    if raw is None:
        raise _missing_bundle()
    bundle = _decode_bundle(raw)
    _validate_bundle_binding(bundle, profile)
    now = _checked_now()
    if bundle.state != "READY":
        raise _reconnect_required()
    if bundle.refresh_token_expires_at is not None and bundle.refresh_token_expires_at <= now:
        _persist_reconnect_required(backend, profile.credential_ref, bundle, now)
        raise _reconnect_required()
    if bundle.access_token_expires_at > now + _CACHE_REFRESH_SKEW:
        resolved_at = _checked_now()
        if resolved_at < now:
            raise _reconnect_required()
        if resolved_at >= bundle.access_token_expires_at:
            _persist_reconnect_required(backend, profile.credential_ref, bundle, resolved_at)
            raise _reconnect_required()
        return bundle, resolved_at

    started = replace(bundle, state="REFRESH_STARTED", refresh_started_at=now)
    _write_bundle(backend, profile.credential_ref, started)
    try:
        refreshed = _dispatch_refresh(started)
    except Exception as exc:
        _handle_refresh_failure(
            backend,
            profile.credential_ref,
            original=bundle,
            started_at=now,
            error=exc,
        )
        raise AssertionError("refresh failure handler must raise") from None
    refreshed = revalidate_google_oauth_refresh_grant(refreshed)
    before_complete_write_now = _checked_now()
    completed = _bundle_from_refresh(started, refreshed, before_complete_write_now)
    try:
        _write_bundle(backend, profile.credential_ref, completed)
    except AdhelmError as error:
        if error.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED":
            # A completed token is never returned from this invocation until
            # its exact local write/readback succeeds. A best-effort durable
            # block keeps later automatic refresh from reusing an ambiguously
            # completed state when the backend permits it.
            _best_effort_reconnect_required(
                backend, profile.credential_ref, completed, before_complete_write_now
            )
        raise
    return_now = _checked_now()
    if return_now < before_complete_write_now or return_now >= completed.access_token_expires_at:
        _persist_reconnect_required(backend, profile.credential_ref, completed, return_now)
        raise _reconnect_required()
    return completed, return_now


def _checked_google_oauth_execution_snapshot(
    value: object, profile: Profile
) -> _GoogleOAuthExecutionSnapshot:
    """Accept only the just-issued sealed-bundle snapshot for this exact profile."""

    if type(value) is not _GoogleOAuthExecutionSnapshot:
        raise _invalid_execution_snapshot()
    checked_profile = _validated_google_oauth_profile(profile)
    try:
        issuer = value._issuer
        bearer_token = value._bearer_token
        profile_digest = value.profile_digest
        provider = value.provider
        purpose = value.purpose
        account_ref = value.account_ref
        principal_ref = value.principal_ref
        client_ref = value.client_ref
        api_scope = value.api_scope
        access_token_expires_at = value._access_token_expires_at
        resolved_at = value._resolved_at
    except Exception:
        raise _invalid_execution_snapshot() from None
    expected_scope = _profile_scope(checked_profile)
    expected_values = (
        (profile_digest, digest(checked_profile)),
        (provider, checked_profile.provider),
        (purpose, checked_profile.purpose),
        (account_ref, checked_profile.account_ref),
        (principal_ref, checked_profile.principal_ref),
        (client_ref, checked_profile.client_ref),
        (api_scope, expected_scope),
    )
    try:
        if issuer is not _EXECUTION_SNAPSHOT_ISSUER or not all(
            type(actual) is str and type(expected) is str and hmac.compare_digest(actual, expected)
            for actual, expected in expected_values
        ):
            raise ValueError("snapshot association")
        _checked_secret_text(bearer_token, name="token", maximum=_MAX_SECRET_BYTES)
        if (
            type(access_token_expires_at) is not datetime
            or access_token_expires_at.tzinfo is None
            or access_token_expires_at.utcoffset() is None
            or type(resolved_at) is not datetime
            or resolved_at.tzinfo is None
            or resolved_at.utcoffset() is None
            or resolved_at.astimezone(UTC) >= access_token_expires_at.astimezone(UTC)
        ):
            raise ValueError("snapshot expiry")
    except Exception:
        raise _invalid_execution_snapshot() from None
    return value


def _dispatch_refresh(bundle: _GoogleOAuthCredentialBundle) -> GoogleOAuthRefreshGrant:
    """Use the only production fixed-endpoint transport after durable STARTED."""
    from adhelm.google_oauth_protocol import refresh_google_oauth_grant
    from adhelm.google_oauth_transport import FixedGoogleOAuthTransport

    target = GoogleOAuthTarget(client_ref=bundle.client_ref, api_scope=bundle.api_scope)
    identity = VerifiedGoogleOidcIdentity(
        canonical_issuer=bundle.canonical_issuer,
        raw_subject=bundle.raw_subject,
        principal_ref=bundle.principal_ref,
        client_ref=bundle.client_ref,
    )
    with FixedGoogleOAuthTransport.open() as transport:
        return refresh_google_oauth_grant(
            target,
            identity,
            refresh_token=bundle.refresh_token,
            token_transport=transport,
            certificate_transport=transport,
            client_secret=bundle.client_secret,
        )


def _handle_refresh_failure(
    backend: _CredentialStore,
    reference: str,
    *,
    original: _GoogleOAuthCredentialBundle,
    started_at: datetime,
    error: Exception,
) -> None:
    """Restore only a proven pre-dispatch failure; every other outcome blocks."""
    if (
        isinstance(error, GoogleOAuthTransportError)
        and error.dispatch_state is GoogleOAuthTokenDispatchState.NOT_DISPATCHED
    ):
        _write_bundle(backend, reference, original)
        raise error
    _persist_reconnect_required(backend, reference, original, started_at)
    raise _reconnect_required()


def _persist_reconnect_required(
    backend: _CredentialStore,
    reference: str,
    bundle: _GoogleOAuthCredentialBundle,
    started_at: datetime,
) -> None:
    _write_bundle(
        backend,
        reference,
        replace(bundle, state="RECONNECT_REQUIRED", refresh_started_at=started_at),
    )


def _best_effort_reconnect_required(
    backend: _CredentialStore,
    reference: str,
    bundle: _GoogleOAuthCredentialBundle,
    started_at: datetime,
) -> None:
    """Try to persist a block without replacing the original ambiguity error.

    An OS credential store does not offer an atomic multi-write transaction.
    If this defensive write also cannot be read back, the current operation
    remains failed and a later fresh local read can only reconcile the exact
    protected state it then observes; no token is returned by this call.
    """
    try:
        _persist_reconnect_required(backend, reference, bundle, started_at)
    except AdhelmError:
        return


def _bundle_from_initial(
    profile: Profile,
    grant: GoogleOAuthInitialGrant,
    client_secret: str | None,
    now: datetime,
) -> _GoogleOAuthCredentialBundle:
    _validate_initial_grant_binding(profile, grant)
    if grant.expires_at <= now:
        raise _grant_expired()
    if grant.refresh_token_expires_at is not None and grant.refresh_token_expires_at <= now:
        raise _grant_expired()
    return _GoogleOAuthCredentialBundle(
        state="READY",
        profile_digest=digest(profile),
        provider=profile.provider,
        purpose=profile.purpose,
        account_ref=profile.account_ref,
        principal_ref=profile.principal_ref,
        client_ref=profile.client_ref,
        api_scope=profile.scopes[0],
        canonical_issuer=grant.identity.canonical_issuer,
        raw_subject=grant.identity.raw_subject,
        requested_scopes=grant.requested_scopes,
        returned_scopes=grant.returned_scopes,
        client_secret=client_secret,
        refresh_token=grant.refresh_token,
        refresh_token_expires_at=grant.refresh_token_expires_at,
        access_token=grant.access_token,
        access_token_expires_at=grant.expires_at,
        refresh_started_at=None,
    )


def _bundle_from_refresh(
    bundle: _GoogleOAuthCredentialBundle,
    grant: GoogleOAuthRefreshGrant,
    now: datetime,
) -> _GoogleOAuthCredentialBundle:
    target = GoogleOAuthTarget(client_ref=bundle.client_ref, api_scope=bundle.api_scope)
    if grant.target != target or grant.expires_at <= now:
        raise _refresh_result_invalid()
    refresh_token = grant.rotated_refresh_token or bundle.refresh_token
    refresh_expires_at = (
        grant.rotated_refresh_token_expires_at
        if grant.rotated_refresh_token is not None
        else bundle.refresh_token_expires_at
    )
    if refresh_expires_at is not None and refresh_expires_at <= now:
        raise _refresh_result_invalid()
    returned_scopes = (
        bundle.returned_scopes if grant.returned_scopes is None else grant.returned_scopes
    )
    _validate_scope_tuple(returned_scopes, bundle.api_scope, name="returned_scopes")
    return replace(
        bundle,
        state="READY",
        access_token=grant.access_token,
        access_token_expires_at=grant.expires_at,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires_at,
        returned_scopes=returned_scopes,
        refresh_started_at=None,
    )


def _validated_configuration(value: Configuration) -> Configuration:
    try:
        return validate_configuration(value)
    except Exception:
        raise AdhelmError(
            "PROFILE_CONFIGURATION_INVALID",
            "Google OAuth credential setup requires valid configured profiles.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _selected_google_oauth_profile(configuration: Configuration, profile_id: str) -> Profile:
    if type(profile_id) is not str:
        raise AdhelmError(
            "PROFILE_REQUIRED",
            "Google OAuth credential setup requires one configured profile.",
            exit_code=2,
        )
    matches = [profile for profile in configuration.profiles if profile.profile_id == profile_id]
    if len(matches) != 1:
        raise AdhelmError(
            "PROFILE_REQUIRED",
            "Google OAuth credential setup requires one configured profile.",
            exit_code=2,
        )
    if not isinstance(matches[0], Profile):
        raise AdhelmError(
            "GOOGLE_OAUTH_PROFILE_REQUIRED",
            "Google OAuth requires its configured scoped profile.",
            exit_code=2,
        )
    return _validated_google_oauth_profile(matches[0])


def _validated_google_oauth_profile(value: Profile) -> Profile:
    try:
        checked = validate_configured_profile(value)
    except Exception:
        raise AdhelmError(
            "PROFILE_INVALID", "The selected authentication profile failed validation.", exit_code=2
        ) from None
    if not isinstance(checked, Profile) or checked.credential_source != "OS_OAUTH_GOOGLE":
        raise AdhelmError(
            "GOOGLE_OAUTH_PROFILE_REQUIRED",
            "Google OAuth requires its configured scoped profile.",
            exit_code=2,
        )
    _profile_scope(checked)
    return checked


def _checked_google_ads_edit_profile(value: object) -> Profile:
    """Require the one locally declared Google Ads OAuth EDIT target."""

    if type(value) is not Profile:
        raise _google_ads_edit_profile_required()
    try:
        checked = _validated_google_oauth_profile(value)
    except AdhelmError:
        raise _google_ads_edit_profile_required() from None
    if (
        checked.provider != "google_ads"
        or checked.purpose != "EDIT"
        or checked.scopes != ["https://www.googleapis.com/auth/adwords"]
        or checked.currency is None
    ):
        raise _google_ads_edit_profile_required()
    try:
        if (
            type(checked.account_ref) is not str
            or re.fullmatch(r"[0-9]{10}", checked.account_ref) is None
            or (
                checked.login_customer_ref is not None
                and type(checked.login_customer_ref) is not str
            )
        ):
            raise ValueError("profile")
        if (
            checked.login_customer_ref is not None
            and re.fullmatch(r"[0-9]{10}", checked.login_customer_ref) is None
        ):
            raise ValueError("login")
    except Exception:
        raise _google_ads_edit_profile_required() from None
    return checked


def _google_provider(profile: Profile) -> Literal["google_ads", "ga4", "ga4_admin", "gtm"]:
    provider = profile.provider
    if provider not in {"google_ads", "ga4", "ga4_admin", "gtm"}:
        raise AdhelmError(
            "GOOGLE_OAUTH_PROFILE_INVALID",
            "Google OAuth profile has an unsupported local target.",
            exit_code=2,
        )
    return cast(Literal["google_ads", "ga4", "ga4_admin", "gtm"], provider)


def _profile_scope(profile: Profile) -> str:
    expected = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.get(
        (profile.provider, profile.purpose)
    )
    if expected is None or profile.scopes != [expected]:
        raise AdhelmError(
            "GOOGLE_OAUTH_PROFILE_INVALID",
            "Google OAuth profile has an unsupported local target.",
            exit_code=2,
        )
    return expected


def _validate_configuration_references(configuration: Configuration) -> None:
    # Keep profile/config validation and all credential-slot alias checks before
    # browser handoff or protected bundle operations.  This is a private helper
    # in the existing credential setup module to avoid a second namespace rule.
    from adhelm.credential_setup import _validate_all_references

    try:
        _validate_all_references(configuration.profiles)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "PROFILE_CONFIGURATION_INVALID",
            "Google OAuth credential setup requires valid configured profiles.",
            exit_code=2,
        ) from None


def _checked_install_slot(
    value: GoogleOAuthInstallSlot,
    profile: Profile,
    configuration: Configuration,
    *,
    replace: bool,
) -> GoogleOAuthInstallSlot:
    if type(value) is not GoogleOAuthInstallSlot:
        raise _invalid_install_snapshot()
    try:
        issuer = value._issuer
        reference_value = value._reference
        expected_hmac = value._expected_value_hmac
        snapshot_replace = value._replace
        configuration_digest = value._configuration_digest
        profile_id = value._profile_id
    except Exception:
        raise _invalid_install_snapshot() from None
    if issuer is not _SLOT_ISSUER:
        raise _invalid_install_snapshot()
    try:
        reference = _checked_reference(reference_value)
    except AdhelmError:
        raise _invalid_install_snapshot() from None
    if type(expected_hmac) is not str or _DIGEST.fullmatch(expected_hmac) is None:
        raise _invalid_install_snapshot()
    if type(snapshot_replace) is not bool or snapshot_replace is not replace:
        raise _invalid_install_snapshot()
    if (configuration_digest is None) != (profile_id is None):
        raise _invalid_install_snapshot()
    if replace and configuration_digest is None:
        # Reconnect may only use the configuration-bound preflight helper;
        # a raw-reference snapshot cannot authorize a profile replacement.
        raise _invalid_install_snapshot()
    if configuration_digest is not None:
        if (
            type(configuration_digest) is not str
            or _DIGEST.fullmatch(configuration_digest) is None
            or type(profile_id) is not str
            or _SAFE_REFERENCE.fullmatch(profile_id) is None
            or not hmac.compare_digest(configuration_digest, digest(configuration))
            or not hmac.compare_digest(profile_id, profile.profile_id)
        ):
            raise _invalid_install_snapshot()
    if not hmac.compare_digest(reference, profile.credential_ref):
        raise _invalid_install_snapshot()
    return value


def _slot_value_hmac(reference: str, value: str | None) -> str:
    """Bind an opaque snapshot to one exact local slot without retaining raw bytes."""
    if value is not None and (
        type(value) is not str or not 1 <= len(value.encode("utf-8")) <= _MAX_BUNDLE_BYTES
    ):
        raise _invalid_bundle()
    state = b"M" if value is None else b"V" + value.encode("utf-8")
    material = b"adhelm-google-oauth-install-slot-v1\x00" + reference.casefold().encode("ascii")
    return hmac.new(_slot_hmac_key(), material + b"\x00" + state, "sha256").hexdigest()


def _slot_hmac_key() -> bytes:
    global _SLOT_HMAC_KEY
    with _SLOT_HMAC_KEY_LOCK:
        if _SLOT_HMAC_KEY is None:
            try:
                candidate = secrets.token_bytes(32)
            except Exception:
                raise AdhelmError(
                    "GOOGLE_OAUTH_RANDOMNESS_UNAVAILABLE",
                    "Secure local Google OAuth snapshot protection is unavailable.",
                ) from None
            if type(candidate) is not bytes or len(candidate) != 32:
                raise AdhelmError(
                    "GOOGLE_OAUTH_RANDOMNESS_UNAVAILABLE",
                    "Secure local Google OAuth snapshot protection is unavailable.",
                )
            _SLOT_HMAC_KEY = candidate
        return _SLOT_HMAC_KEY


def _validate_initial_grant_binding(profile: Profile, grant: GoogleOAuthInitialGrant) -> None:
    scope = _profile_scope(profile)
    expected_target = GoogleOAuthTarget(client_ref=profile.client_ref, api_scope=scope)
    if grant.target != expected_target or not (
        hmac.compare_digest(grant.identity.client_ref, profile.client_ref)
        and hmac.compare_digest(grant.identity.principal_ref, profile.principal_ref)
        and hmac.compare_digest(grant.identity.canonical_issuer, GOOGLE_OIDC_CANONICAL_ISSUER)
    ):
        raise AdhelmError(
            "GOOGLE_OAUTH_GRANT_BINDING_INVALID",
            "The verified Google grant does not match the configured profile.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Restart local Google enrollment with the selected profile.",
        )


def _validate_bundle_binding(bundle: _GoogleOAuthCredentialBundle, profile: Profile) -> None:
    scope = _profile_scope(profile)
    expected_values = (
        (bundle.profile_digest, digest(profile)),
        (bundle.provider, profile.provider),
        (bundle.purpose, profile.purpose),
        (bundle.account_ref, profile.account_ref),
        (bundle.principal_ref, profile.principal_ref),
        (bundle.client_ref, profile.client_ref),
        (bundle.api_scope, scope),
    )
    if not all(hmac.compare_digest(actual, expected) for actual, expected in expected_values):
        # Only the configuration digest differs: the grant is for this very account,
        # principal, client and scope, but was sealed against an older shape of the profile
        # (for example by AdHelm 0.1.0, before the developer-token field was removed).
        if all(hmac.compare_digest(actual, expected) for actual, expected in expected_values[1:]):
            raise _stale_bundle(profile)
        raise _invalid_bundle()


def _write_bundle(
    backend: _CredentialStore, reference: str, bundle: _GoogleOAuthCredentialBundle
) -> None:
    encoded = bundle._encoded()
    try:
        backend.set_password(_SERVICE, reference, encoded)
    except Exception:
        pass
    observed = _read_slot(backend, reference, unverified_change=True)
    if not _same_slot_value(observed, encoded):
        raise _unverified_local_change()


def _read_slot(
    backend: _CredentialStore, reference: str, *, unverified_change: bool = False
) -> str | None:
    try:
        value = backend.get_password(_SERVICE, reference)
    except Exception:
        if unverified_change:
            raise _unverified_local_change() from None
        raise _credential_store_unavailable() from None
    if value is None:
        return None
    if type(value) is not str or not value:
        if unverified_change:
            raise _unverified_local_change()
        raise _invalid_bundle()
    return value


def _verify_removed(backend: _CredentialStore, reference: str) -> None:
    try:
        observed = backend.get_password(_SERVICE, reference)
    except Exception:
        raise _unverified_local_change() from None
    if observed is not None:
        raise _unverified_local_change()


def _same_slot_value(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return hmac.compare_digest(left, right)


def _credential_backend() -> _CredentialStore:
    # Imported only at an actual protected-store boundary.  This avoids a
    # credentials -> lifecycle import cycle while retaining the existing backend
    # approval policy and its fake-test seam.
    from adhelm.credentials import _backend

    return _backend()


def _generic_credential_store() -> tuple[_CredentialStore, str]:
    """Return the separate ordinary-store backend/service for the PAUSE binding key.

    Importing this only at the protected boundary preserves the existing
    credentials-to-lifecycle import direction.  The OAuth bundle service and
    ordinary-store service intentionally remain distinct.
    """

    from adhelm.credentials import _SERVICE as generic_service
    from adhelm.credentials import _backend

    try:
        backend = _backend()
        if type(generic_service) is not str or not generic_service:
            raise ValueError("service")
        return backend, generic_service
    except AdhelmError:
        raise
    except Exception:
        raise _generic_store_unavailable() from None


def _decode_bundle(raw: str) -> _GoogleOAuthCredentialBundle:
    if type(raw) is not str or not 1 <= len(raw.encode("utf-8")) <= _MAX_BUNDLE_BYTES:
        raise _invalid_bundle()
    try:
        parsed = parse_json(raw, maximum_bytes=_MAX_BUNDLE_BYTES, decimal_numbers=True)
        if type(parsed) is not dict or canonical_bytes(parsed) != raw.encode("utf-8"):
            raise ValueError("noncanonical")
        return _bundle_from_data(parsed)
    except AdhelmError:
        raise _invalid_bundle() from None
    except Exception:
        raise _invalid_bundle() from None


def _bundle_from_data(data: Mapping[str, object]) -> _GoogleOAuthCredentialBundle:
    expected_fields = {
        "access_token",
        "access_token_expires_at",
        "account_ref",
        "api_scope",
        "canonical_issuer",
        "client_ref",
        "client_secret",
        "principal_ref",
        "profile_digest",
        "provider",
        "purpose",
        "raw_subject",
        "refresh_started_at",
        "refresh_token",
        "refresh_token_expires_at",
        "requested_scopes",
        "returned_scopes",
        "schema_version",
        "state",
    }
    if (
        set(data) != expected_fields
        or type(data.get("schema_version")) is not int
        or data.get("schema_version") != 1
    ):
        raise ValueError("bundle shape")
    state = data.get("state")
    if state not in {"READY", "REFRESH_STARTED", "RECONNECT_REQUIRED"}:
        raise ValueError("state")
    state = cast(_BundleState, state)
    profile_digest = _checked_digest(data.get("profile_digest"))
    provider = _checked_provider(data.get("provider"))
    purpose = _checked_purpose(data.get("purpose"))
    account_ref = _checked_account_ref(data.get("account_ref"), provider)
    client_ref = _checked_client_ref(data.get("client_ref"))
    api_scope = _checked_api_scope(provider, purpose, data.get("api_scope"))
    principal_ref = _checked_principal_ref(data.get("principal_ref"))
    canonical_issuer = data.get("canonical_issuer")
    raw_subject = _checked_secret_text(data.get("raw_subject"), name="subject", maximum=255)
    if canonical_issuer != GOOGLE_OIDC_CANONICAL_ISSUER or not hmac.compare_digest(
        principal_ref, derive_google_oidc_principal_ref(canonical_issuer, raw_subject)
    ):
        raise ValueError("identity")
    requested_scopes = _checked_requested_scopes(data.get("requested_scopes"), api_scope)
    returned_scopes = _checked_returned_scopes(data.get("returned_scopes"), api_scope)
    client_secret_value = data.get("client_secret")
    client_secret = (
        None
        if client_secret_value is None
        else _checked_secret_text(client_secret_value, name="secret", maximum=_MAX_SECRET_BYTES)
    )
    refresh_token = _checked_secret_text(
        data.get("refresh_token"), name="token", maximum=_MAX_SECRET_BYTES
    )
    refresh_token_expires_at = _checked_optional_time(data.get("refresh_token_expires_at"))
    access_token = _checked_secret_text(
        data.get("access_token"), name="token", maximum=_MAX_SECRET_BYTES
    )
    access_token_expires_at = _checked_time(data.get("access_token_expires_at"))
    refresh_started_at = _checked_optional_time(data.get("refresh_started_at"))
    if (state == "READY") != (refresh_started_at is None):
        raise ValueError("state transition")
    return _GoogleOAuthCredentialBundle(
        state=state,
        profile_digest=profile_digest,
        provider=provider,
        purpose=purpose,
        account_ref=account_ref,
        principal_ref=principal_ref,
        client_ref=client_ref,
        api_scope=api_scope,
        canonical_issuer=canonical_issuer,
        raw_subject=raw_subject,
        requested_scopes=requested_scopes,
        returned_scopes=returned_scopes,
        client_secret=client_secret,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_token_expires_at,
        access_token=access_token,
        access_token_expires_at=access_token_expires_at,
        refresh_started_at=refresh_started_at,
    )


def _checked_reference(value: object) -> str:
    if (
        type(value) is not str
        or _SAFE_REFERENCE.fullmatch(value) is None
        or is_reserved_credential_reference(value)
    ):
        raise AdhelmError(
            "INVALID_CREDENTIAL_REFERENCE",
            "Google OAuth requires a supported local credential reference.",
            exit_code=2,
        )
    return value


def _checked_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest")
    return value


def _checked_provider(value: object) -> Literal["google_ads", "ga4", "ga4_admin", "gtm"]:
    if value not in {"google_ads", "ga4", "ga4_admin", "gtm"}:
        raise ValueError("provider")
    return cast(Literal["google_ads", "ga4", "ga4_admin", "gtm"], value)


def _checked_purpose(value: object) -> Literal["READ", "EDIT"]:
    if type(value) is not str or value not in {"READ", "EDIT"}:
        raise ValueError("purpose")
    return cast(Literal["READ", "EDIT"], value)


def _checked_account_ref(value: object, provider: str) -> str:
    if type(value) is not str:
        raise ValueError("account")
    pattern = r"[0-9]{10}" if provider == "google_ads" else r"[0-9]{1,20}"
    if re.fullmatch(pattern, value) is None:
        raise ValueError("account")
    return value


def _checked_client_ref(value: object) -> str:
    if not is_google_oauth_client_id(value):
        raise ValueError("client")
    return cast(str, value)


def _checked_principal_ref(value: object) -> str:
    if not is_google_oauth_operational_principal_ref(value):
        raise ValueError("principal")
    return cast(str, value)


def _checked_api_scope(provider: str, purpose: str, value: object) -> str:
    expected = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.get((provider, purpose))
    if type(value) is not str or expected is None or not hmac.compare_digest(value, expected):
        raise ValueError("scope")
    return expected


def _checked_requested_scopes(value: object, api_scope: str) -> tuple[str, str]:
    if type(value) is not list or value != ["openid", api_scope]:
        raise ValueError("requested scopes")
    return ("openid", api_scope)


def _checked_returned_scopes(value: object, api_scope: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError("returned scopes")
    scopes = tuple(value)
    _validate_scope_tuple(scopes, api_scope, name="returned_scopes")
    return scopes


def _validate_scope_tuple(value: object, api_scope: str, *, name: str) -> None:
    if value not in {(api_scope,), ("openid", api_scope)}:
        raise ValueError(name)


def _checked_secret_text(value: object, *, name: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= maximum
        or not value.isascii()
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError(name)
    return value


def _validate_client_secret(value: str | None) -> None:
    if value is not None:
        try:
            _checked_secret_text(value, name="secret", maximum=_MAX_SECRET_BYTES)
        except ValueError:
            raise AdhelmError(
                "GOOGLE_OAUTH_CLIENT_SECRET_INVALID",
                "Google OAuth client material could not be used.",
                exit_code=2,
            ) from None


def _checked_optional_time(value: object) -> datetime | None:
    return None if value is None else _checked_time(value)


def _checked_time(value: object) -> datetime:
    if type(value) is not str or _UTC_TIME.fullmatch(value) is None:
        raise ValueError("time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("time") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise ValueError("time")
    return parsed.astimezone(UTC)


def _time_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time")
    normalized = value.astimezone(UTC)
    text = normalized.isoformat()
    if _UTC_TIME.fullmatch(text) is None:
        raise ValueError("time")
    return text


def _utc_now() -> datetime:
    """Private trusted-time seam for deterministic local-only tests."""
    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        value = _utc_now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_CLOCK_UNAVAILABLE", "A trusted local clock is unavailable."
        ) from None


def _credential_already_exists() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_ALREADY_EXISTS",
        "A local credential already exists for this exact profile reference.",
        next_action="Use deliberate Google reconnect after reviewing the local credential.",
    )


def _missing_bundle() -> AdhelmError:
    return AdhelmError(
        "AUTHENTICATION_REQUIRED",
        "Profile has no usable locally stored Google OAuth credential.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Complete the supported Google OAuth flow locally.",
    )


def _credential_store_unavailable() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_STORE_UNAVAILABLE",
        "The OS credential store is unavailable.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Check the local OS credential store and retry deliberately.",
    )


def _invalid_bundle() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_CREDENTIAL_INVALID",
        "The protected Google OAuth credential cannot be used.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reconnect the configured Google profile locally.",
    )


def _stale_bundle(profile: Profile) -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_PROFILE_BINDING_STALE",
        "The stored Google OAuth grant was sealed against an older form of this profile "
        "(for example by AdHelm 0.1.0) and no longer matches it.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action=(
            "Reconnect this profile with `adhelm auth google-oauth reconnect --profile "
            f"{profile.profile_id} --client-config <client_secret.json> --interactive`, signing "
            "in as the same Google user. The profile and your history are kept."
        ),
    )


def _invalid_execution_snapshot() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_EXECUTION_SNAPSHOT_INVALID",
        "The protected Google OAuth execution binding cannot be used.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reconnect the configured Google profile locally before another read.",
    )


def _google_ads_edit_profile_required() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
        "A configured Google Ads OAuth EDIT profile is required.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Configure and authorize the selected Google Ads EDIT profile locally.",
    )


def _generic_store_unavailable() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_STORE_UNAVAILABLE",
        "The OS credential store is unavailable for the Google Ads PAUSE binding key.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Check the local OS credential store and retry deliberately.",
    )


def _invalid_google_ads_edit_execution_snapshot() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_EXECUTION_SNAPSHOT_INVALID",
        "The protected Google Ads EDIT execution binding cannot be used.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
    )


def _google_ads_pause_protected_snapshot_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_PROTECTED_SNAPSHOT_INVALID",
        "The protected Google Ads PAUSE planning association cannot be used.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Prepare a fresh configured Google Ads PAUSE observation locally.",
    )


def _google_ads_pause_credential_changed() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED",
        "The protected Google Ads PAUSE credential association changed or is no longer current.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Prepare a new Google Ads PAUSE plan from fresh local observations.",
    )


def _google_ads_pause_binding_key_unavailable() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_BINDING_KEY_UNAVAILABLE",
        "The private Google Ads PAUSE credential-binding key is unavailable in the OS store.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Check the local OS credential store before preparing a Google Ads PAUSE plan.",
    )


def _google_ads_pause_retention_recheck_required() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED",
        "Google Ads PAUSE plan retention requires its immediate owner-thread local recheck.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Prepare a fresh Google Ads PAUSE plan before retaining it.",
    )


def _google_ads_pause_clock_invalid() -> AdhelmError:
    return AdhelmError(
        "CLOCK_INVALID",
        "A trusted local clock is not continuous for the Google Ads PAUSE planning association.",
        exit_code=ExitCode.AUTHORIZATION,
    )


def _invalid_install_snapshot() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_INSTALL_SNAPSHOT_INVALID",
        "Google OAuth enrollment must restart from a fresh local preflight.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a deliberate new local Google OAuth enrollment.",
    )


def _grant_expired() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_GRANT_EXPIRED",
        "The verified Google OAuth grant expired before protected storage.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Start a deliberate new local Google OAuth enrollment.",
    )


def _refresh_result_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_REFRESH_RESULT_INVALID",
        "Google OAuth refresh could not produce a usable protected credential.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Reconnect the configured Google profile locally.",
    )


def _reconnect_required() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_RECONNECT_REQUIRED",
        "Google OAuth refresh requires a deliberate local reconnect.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reconnect the configured Google profile locally before another read.",
    )


def _concurrent_change() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_CONCURRENT_CHANGE",
        "The local Google credential changed during enrollment.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Review the local credential and start a deliberate new enrollment.",
    )


def _unverified_local_change() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED",
        "The requested local credential change could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect the local credential store before any deliberate follow-up.",
    )

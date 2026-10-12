"""Private shared continuity guard for the two fixed Meta Ads token sessions.

This module deliberately has no public factory, generic credential resolver,
provider URL, or mutation surface.  The READ and EDIT facades each select a
fixed policy below, then use the same local configuration, protected-bundle,
finite-expiry, account-identity, and retention checks.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock, get_ident
from typing import Literal, Never

import httpx

from adhelm import meta_token_credentials as token_credentials
from adhelm.canonical import digest
from adhelm.config import ConfigurationTransaction, configuration_transaction
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError, ExitCode
from adhelm.meta_token_inspection import (
    MetaTokenInspectionCredentials,
    MetaTokenObservation,
    _credential_digest,
    inspect_meta_token,
)
from adhelm.models import MetaAccountTokenProfile
from adhelm.providers.meta_ads import MetaAdsClient, MetaAdsIdentity
from adhelm.providers.meta_campaign_state import _validate_transport as _validate_campaign_transport
from adhelm.providers.meta_create_contract import form_value_expressible
from adhelm.transport import SafeHttpClient

_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_NATIVE_ID = re.compile(r"^[1-9][0-9]{0,39}$", re.ASCII)
_SCOPE = re.compile(r"^[a-z][a-z0-9_]{0,99}$", re.ASCII)
_PRINCIPAL_REF = re.compile(r"^meta_token_principal_v1\.[a-f0-9]{64}$", re.ASCII)
_CREDENTIAL_BINDING_REF = re.compile(r"^credential-binding:sha256:[a-f0-9]{64}$", re.ASCII)
_READ_CONTINUITY_DOMAIN = b"adhelm/meta/ads/read-session/continuity/v1\x00"
_EDIT_CONTINUITY_DOMAIN = b"adhelm/meta/ads/edit-session/continuity/v1\x00"
_SESSION_ISSUER = object()
#: The five fixed account-scoped create edges.  No other path may reach the POST
#: routes below.
_META_CREATE_WRITE_PATH = re.compile(
    r"^/v26\.0/act_[1-9][0-9]{0,39}/(?:adimages|campaigns|adsets|adcreatives|ads)$", re.ASCII
)


@dataclass(frozen=True, slots=True, repr=False)
class _SessionPolicy:
    """Closed policy chosen only by a fixed facade in this module."""

    _purpose: Literal["READ", "EDIT"]
    _scope: Literal["ads_read", "ads_management"]
    _kind: Literal["READ", "EDIT"]
    _continuity_domain: bytes
    _retention_code: str

    def __repr__(self) -> str:
        return "<_SessionPolicy>"


_READ_POLICY = _SessionPolicy(
    "READ",
    "ads_read",
    "READ",
    _READ_CONTINUITY_DOMAIN,
    "META_TOKEN_RETENTION_RECHECK_REQUIRED",
)
_EDIT_POLICY = _SessionPolicy(
    "EDIT",
    "ads_management",
    "EDIT",
    _EDIT_CONTINUITY_DOMAIN,
    "META_TOKEN_EDIT_RETENTION_RECHECK_REQUIRED",
)


def _now() -> datetime:
    """Private clock seam used only by synthetic tests."""
    return datetime.now(UTC)


def _meta_http_client() -> httpx.Client:
    """Create the sole fixed-host client used by a configured token session."""
    return httpx.Client(trust_env=False)


@dataclass(frozen=True, slots=True, repr=False)
class _TokenSessionContext:
    """Safe facts whose continuity must remain exact for one local session."""

    _directory: Path
    _inspection_context: token_credentials._MetaTokenInspectionContext
    _profile: MetaAccountTokenProfile
    _expected_configuration_digest: str
    _continuity_marker: bytes
    _principal_ref: str
    _credential_binding_ref: str
    _observed_at: datetime
    _expires_at: datetime
    _data_access_expires_at: datetime
    _policy: _SessionPolicy

    def __repr__(self) -> str:
        return "<_TokenSessionContext redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads token session contexts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads token session contexts cannot be serialized")


class _ConfiguredMetaAdsTokenSession:
    """Private nonsecret state shared by the fixed READ and EDIT facades."""

    __slots__ = (
        "_client",
        "_context",
        "_identity",
        "_issuer",
        "_lock",
        "_closed",
        "_retention_generation",
        "_retention_owner",
        "_retention_token",
        "_transport",
    )

    def __init__(
        self,
        *,
        context: _TokenSessionContext,
        client: httpx.Client,
        issuer: object,
    ) -> None:
        self._context = context
        self._client = client
        self._issuer = issuer
        self._identity: MetaAdsIdentity | None = None
        self._lock = RLock()
        self._closed = False
        self._retention_generation = 0
        self._retention_owner: int | None = None
        self._retention_token: object | None = None
        self._transport = SafeHttpClient(client, {"graph.facebook.com"}, read_attempts=1)

    def __repr__(self) -> str:
        return "<_ConfiguredMetaAdsTokenSession redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads token sessions cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads token sessions cannot be serialized")

    def _prove_identity(self) -> MetaAdsIdentity:
        """Perform the one fixed account identity GET before a facade is yielded."""
        return self._refresh_identity()

    def _refresh_identity(self) -> MetaAdsIdentity:
        """Re-read the one fixed configured account before a guarded write.

        This remains a closed account-node GET.  It returns no credential
        material and is reachable only through the EDIT facade's private
        pause capability.
        """
        with self._network_operation():
            profile = self._context._profile
            adapter = MetaAdsClient(
                self._transport,
                self._subject_token_for_provider_read,
                now=_now,
            )
            identity = adapter.discover_identity(
                profile.account_ref,
                expected_account_client_ref=profile.account_ref,
            )
            checked = _checked_identity(identity, profile)
            self._identity = checked
            return checked

    def _checked_identity(self) -> MetaAdsIdentity:
        with self._lock:
            self._require_live()
            if self._identity is None:
                raise _session_invalid(self._context._policy)
            return _checked_identity(self._identity, self._context._profile)

    @contextmanager
    def _network_operation(self) -> Iterator[None]:
        """Serialize one fixed GET and reject it during retention rechecks."""
        observed_generation = self._retention_generation
        if self._retention_token is not None:
            raise _session_unavailable(self._context._policy)
        with self._lock:
            if observed_generation != self._retention_generation:
                raise _session_unavailable(self._context._policy)
            self._require_live()
            if self._retention_token is not None:
                raise _session_unavailable(self._context._policy)
            yield

    def _subject_token_for_provider_read(self) -> str:
        """Return the current subject token only immediately before a fixed GET."""
        observed_generation = self._retention_generation
        if self._retention_token is not None:
            raise _session_unavailable(self._context._policy)
        with self._lock:
            if observed_generation != self._retention_generation:
                raise _session_unavailable(self._context._policy)
            self._require_live()
            if self._retention_token is not None:
                raise _session_unavailable(self._context._policy)
            return _current_subject_token(self._context)

    def _new_campaign_state_transport(self) -> SafeHttpClient:
        """Create the one bounded transport shape accepted by campaign-state reads."""
        with self._lock:
            self._require_live()
            if self._retention_token is not None:
                raise _session_unavailable(self._context._policy)
            return SafeHttpClient(
                self._client,
                {"graph.facebook.com"},
                maximum_response_bytes=65_536,
                read_attempts=1,
            )

    def _preflight_fixed_meta_campaign_pause(self, campaign_id: object) -> None:
        """Check current EDIT continuity without sending a provider request.

        The fixed adapter invokes this immediately before its kernel permit is
        consumed.  It deliberately discards the transient subject token; the
        POST below obtains and rechecks it again immediately before dispatch.
        """

        self._require_edit_write_policy()
        _fixed_campaign_id(campaign_id, self._context._policy)
        with self._network_operation():
            token = self._subject_token_for_provider_read()
            del token

    def _post_fixed_meta_campaign_pause(self, campaign_id: object) -> httpx.Response:
        """Send only the fixed v26 ``status=PAUSED`` campaign form request.

        This private method has no URL, token, method, header, or body
        parameter.  It rechecks the current protected bundle and finite token
        window immediately before the sole mutation attempt.
        """

        self._require_edit_write_policy()
        campaign = _fixed_campaign_id(campaign_id, self._context._policy)
        with self._network_operation():
            token = self._subject_token_for_provider_read()
            transport = self._new_campaign_state_transport()
            # The last fixed campaign GET can mutate the shared httpx client
            # (for example through Set-Cookie). Revalidate the complete
            # one-attempt/host/ambient-state contract after the current token
            # snapshot and immediately before the sole POST.
            _validate_campaign_transport(transport)
            return transport.request(
                "POST",
                f"https://graph.facebook.com/v26.0/{campaign}/",
                headers={"authorization": f"Bearer {token}"},
                form={"status": "PAUSED"},
                mutation=True,
            )

    def _require_edit_write_policy(self) -> None:
        """Reject a READ core before it can inspect credentials or route a POST."""

        with self._lock:
            self._require_live()
            if self._context._policy is not _EDIT_POLICY:
                raise _profile_required(_EDIT_POLICY)

    def _preflight_fixed_meta_create(self) -> None:
        """Check current EDIT continuity without sending a provider request.

        The fixed child-egress adapter invokes this immediately before each
        kernel child permit is consumed.  It deliberately discards the transient
        subject token; every route below obtains and rechecks it again
        immediately before its own dispatch.
        """

        self._require_edit_write_policy()
        with self._network_operation():
            token = self._subject_token_for_provider_read()
            del token

    def _post_fixed_meta_create(self, path: object, form: object) -> httpx.Response:
        """Send one already-validated fixed Graph create form request.

        The route grammar lives in the EDIT facade that issues the capability;
        this private method re-checks it rather than trusting it, has no token,
        method, header, or client parameter, and rechecks the current protected
        bundle and finite token window immediately before the sole attempt.
        """

        self._require_edit_write_policy()
        checked = _fixed_meta_create_path(path, self._context._policy)
        checked_form = _fixed_meta_create_form(form, self._context._policy)
        with self._network_operation():
            token = self._subject_token_for_provider_read()
            transport = self._new_campaign_state_transport()
            # A previous fixed GET can mutate the shared httpx client (for
            # example through Set-Cookie).  Revalidate the complete
            # one-attempt/host/ambient-state contract after the current token
            # snapshot and immediately before this sole POST.
            _validate_campaign_transport(transport)
            return transport.request(
                "POST",
                f"https://graph.facebook.com{checked}",
                headers={"authorization": f"Bearer {token}"},
                form=checked_form,
                mutation=True,
            )

    def _upload_fixed_meta_create_image(
        self, path: object, filename: object, media_type: object, content: object
    ) -> httpx.Response:
        """Send the one bounded multipart ad-image upload this route performs."""

        self._require_edit_write_policy()
        checked = _fixed_meta_create_path(path, self._context._policy)
        if not checked.endswith("/adimages"):
            raise _session_invalid(self._context._policy)
        if (
            type(filename) is not str
            or filename not in {"creative.png", "creative.jpg"}
            or type(media_type) is not str
            or media_type not in {"image/png", "image/jpeg"}
            or type(content) is not bytes
            or not 1 <= len(content) <= SafeHttpClient.MAX_UPLOAD_BYTES
        ):
            raise _session_invalid(self._context._policy)
        with self._network_operation():
            token = self._subject_token_for_provider_read()
            transport = self._new_campaign_state_transport()
            _validate_campaign_transport(transport)
            return transport.upload_image(
                f"https://graph.facebook.com{checked}",
                headers={"authorization": f"Bearer {token}"},
                field_name="filename",
                filename=filename,
                media_type=media_type,
                content=content,
            )

    @contextmanager
    def _retention_recheck(self) -> Iterator[Callable[[], None]]:
        """Hold local facts for one owner-thread-only pre-append recheck."""
        with self._lock:
            self._require_live()
            if self._retention_token is not None:
                raise _session_unavailable(self._context._policy)
            token = object()
            self._retention_token = token
            owner_thread = get_ident()
            self._retention_owner = owner_thread
            self._retention_generation += 1
            callback_count = 0
            try:
                with configuration_transaction(self._context._directory) as transaction:
                    profile = _current_profile(transaction, self._context)
                    with credential_mutation_lock(profile.credential_ref):
                        backend = token_credentials._credential_backend()
                        _verify_current_state_locked(transaction, backend, self._context)

                        def recheck() -> None:
                            nonlocal callback_count
                            if (
                                get_ident() != owner_thread
                                or self._retention_owner != owner_thread
                                or self._retention_token is not token
                            ):
                                raise _retention_recheck_required(self._context._policy)
                            _verify_current_state_locked(transaction, backend, self._context)
                            callback_count += 1

                        yield recheck
                        if callback_count == 0:
                            raise _retention_recheck_required(self._context._policy)
            finally:
                self._retention_token = None
                self._retention_owner = None

    def _binding_fields(
        self,
    ) -> tuple[str, str, str, str, str, str, str, datetime, datetime, datetime]:
        """Return only the closed nonsecret facts required by the EDIT facade."""
        with self._lock:
            self._require_live()
            context = self._context
            return (
                context._profile.profile_id,
                context._profile.client_application_ref,
                context._principal_ref,
                context._policy._scope,
                context._credential_binding_ref,
                context._expected_configuration_digest,
                digest(context._profile),
                context._observed_at,
                context._expires_at,
                context._data_access_expires_at,
            )

    def _require_live(self) -> None:
        if self._issuer is not _SESSION_ISSUER or self._closed:
            raise _session_closed(self._context._policy)

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._retention_token = None
            self._retention_owner = None
            try:
                self._client.close()
            except Exception:
                # Closing local read transport cannot alter remote/provider state.
                pass


@contextmanager
def _configured_meta_ads_token_read_state(
    directory: Path,
    profile_id: str,
    *,
    expected_configuration_digest: str,
) -> Iterator[_ConfiguredMetaAdsTokenSession]:
    """Internal READ-only state factory; purpose is fixed here, not caller-selected."""
    with _configured_meta_ads_token_state(
        directory,
        profile_id,
        expected_configuration_digest=expected_configuration_digest,
        policy=_READ_POLICY,
    ) as session:
        yield session


@contextmanager
def _configured_meta_ads_token_edit_state(
    directory: Path,
    profile_id: str,
    *,
    expected_configuration_digest: str,
) -> Iterator[_ConfiguredMetaAdsTokenSession]:
    """Internal EDIT-only state factory; purpose is fixed here, not caller-selected."""
    with _configured_meta_ads_token_state(
        directory,
        profile_id,
        expected_configuration_digest=expected_configuration_digest,
        policy=_EDIT_POLICY,
    ) as session:
        yield session


@contextmanager
def _configured_meta_ads_token_state(
    directory: Path,
    profile_id: str,
    *,
    expected_configuration_digest: str,
    policy: _SessionPolicy,
) -> Iterator[_ConfiguredMetaAdsTokenSession]:
    """Build nonsecret state after a single fixed inspection and identity read."""
    expected = _checked_expected_configuration_digest(expected_configuration_digest, policy)
    inspection_context = token_credentials._prepare_meta_token_inspection(directory, profile_id)
    profile = _profile_for_context(directory, inspection_context, expected, policy)

    captured_marker: bytes | None = None
    captured_binding_ref: str | None = None

    def inspection_credentials() -> MetaTokenInspectionCredentials:
        nonlocal captured_marker, captured_binding_ref
        credentials = token_credentials._resolve_meta_token_inspection_credentials(
            directory, inspection_context
        )
        marker = _continuity_marker(credentials, inspection_context, policy)
        binding_ref = _credential_digest(credentials)
        if captured_marker is None:
            captured_marker = marker
            captured_binding_ref = binding_ref
        elif not (
            hmac.compare_digest(captured_marker, marker)
            and captured_binding_ref is not None
            and hmac.compare_digest(captured_binding_ref, binding_ref)
        ):
            raise token_credentials._credential_changed()
        return credentials

    observation = inspect_meta_token(
        credential_supplier=inspection_credentials,
        expected_client_application_ref=profile.client_application_ref,
        required_scope=policy._scope,
    )
    del inspection_credentials
    if captured_marker is None or captured_binding_ref is None:
        raise _session_invalid(policy)
    if not _post_inspection_bundle_matches(
        directory,
        inspection_context,
        captured_marker,
        captured_binding_ref,
        policy,
    ):
        raise token_credentials._credential_changed()
    observed_at, expires_at, data_access_expires_at, principal_ref = _checked_observation(
        observation,
        profile,
        expected_binding_ref=captured_binding_ref,
        policy=policy,
    )
    # The retained context carries only the verified nonsecret fields below;
    # do not leave the inspector's complete scope tuple in this generator at
    # the point where the facade is yielded.
    del observation
    context = _TokenSessionContext(
        _directory=Path(directory),
        _inspection_context=inspection_context,
        _profile=profile,
        _expected_configuration_digest=expected,
        _continuity_marker=captured_marker,
        _principal_ref=principal_ref,
        _credential_binding_ref=captured_binding_ref,
        _observed_at=observed_at,
        _expires_at=expires_at,
        _data_access_expires_at=data_access_expires_at,
        _policy=policy,
    )
    _check_not_expired(context)
    with _meta_http_client() as client:
        session = _ConfiguredMetaAdsTokenSession(
            context=context,
            client=client,
            issuer=_SESSION_ISSUER,
        )
        try:
            session._prove_identity()
            yield session
        finally:
            session._close()


def _checked_expected_configuration_digest(value: object, policy: _SessionPolicy) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _session_invalid(policy)
    return value


def _fixed_campaign_id(value: object, policy: _SessionPolicy) -> str:
    """Validate the sole native path component admitted by the EDIT POST."""

    if type(value) is not str or _NATIVE_ID.fullmatch(value) is None:
        raise _session_invalid(policy)
    return value


def _fixed_meta_create_path(value: object, policy: _SessionPolicy) -> str:
    """Accept only this build's fixed Meta CREATE route shapes."""

    if type(value) is not str or _META_CREATE_WRITE_PATH.fullmatch(value) is None:
        raise _session_invalid(policy)
    return value


def _fixed_meta_create_form(value: object, policy: _SessionPolicy) -> dict[str, str]:
    """Bound a fixed create form body before the shared transport ever sees it."""

    if type(value) is not dict or not 1 <= len(value) <= 16:
        raise _session_invalid(policy)
    for name, item in value.items():
        if (
            type(name) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
            or not form_value_expressible(item)
        ):
            raise _session_invalid(policy)
    return dict(value)


def _profile_for_context(
    directory: Path,
    context: token_credentials._MetaTokenInspectionContext,
    expected_configuration_digest: str,
    policy: _SessionPolicy,
) -> MetaAccountTokenProfile:
    _checked_inspection_context(context, expected_configuration_digest, policy)
    with configuration_transaction(directory) as transaction:
        return _current_profile(
            transaction,
            _ContextView(context, expected_configuration_digest, policy),
        )


@dataclass(frozen=True, slots=True, repr=False)
class _ContextView:
    _inspection_context: token_credentials._MetaTokenInspectionContext
    _expected_configuration_digest: str
    _policy: _SessionPolicy

    def __repr__(self) -> str:
        return "<_ContextView redacted>"


def _checked_inspection_context(
    value: object,
    expected_configuration_digest: str | None = None,
    policy: _SessionPolicy | None = None,
) -> token_credentials._MetaTokenInspectionContext:
    if type(value) is not token_credentials._MetaTokenInspectionContext:
        raise _session_invalid(policy or _READ_POLICY)
    try:
        configuration_digest = value._configuration_digest
        profile_digest = value._profile_digest
        profile_id = value._profile_id
        application = value._client_application_ref
        scope = value._required_scope
    except Exception:
        raise _session_invalid(policy or _READ_POLICY) from None
    if (
        type(configuration_digest) is not str
        or _DIGEST.fullmatch(configuration_digest) is None
        or type(profile_digest) is not str
        or _DIGEST.fullmatch(profile_digest) is None
        or type(profile_id) is not str
        or not 1 <= len(profile_id) <= 160
        or type(application) is not str
        or not application.isdecimal()
        or type(scope) is not str
        or scope not in {"ads_read", "ads_management"}
    ):
        raise _session_invalid(policy or _READ_POLICY)
    if expected_configuration_digest is not None and not hmac.compare_digest(
        configuration_digest, expected_configuration_digest
    ):
        raise _configuration_changed()
    return value


def _current_profile(
    transaction: ConfigurationTransaction,
    context: _TokenSessionContext | _ContextView,
) -> MetaAccountTokenProfile:
    inspection_context = context._inspection_context
    policy = context._policy
    _checked_inspection_context(inspection_context, context._expected_configuration_digest, policy)
    configuration = token_credentials._configuration(
        transaction.require_unchanged(context._expected_configuration_digest)
    )
    profile = token_credentials._selected_profile(configuration, inspection_context._profile_id)
    if not hmac.compare_digest(digest(profile), inspection_context._profile_digest):
        raise token_credentials._credential_changed()
    if profile.purpose != policy._purpose or profile.required_scope != policy._scope:
        raise _profile_required(policy)
    return profile


def _continuity_marker(
    credentials: MetaTokenInspectionCredentials,
    context: token_credentials._MetaTokenInspectionContext,
    policy: _SessionPolicy,
) -> bytes:
    if type(credentials) is not MetaTokenInspectionCredentials:
        raise _session_invalid(policy)
    _checked_inspection_context(context, policy=policy)
    try:
        if (
            credentials.profile_ref != context._profile_id
            or type(credentials.binding_key) is not bytes
            or len(credentials.binding_key) != 32
            or any(
                type(token) is not str
                or not 1 <= len(token) <= 16_384
                or any(not 33 <= ord(character) <= 126 for character in token)
                for token in (credentials.subject_token, credentials.inspector_token)
            )
        ):
            raise ValueError("credentials")
        material = b"".join(
            (
                policy._continuity_domain,
                context._configuration_digest.encode("ascii"),
                b"\x00",
                context._profile_digest.encode("ascii"),
                b"\x00",
                context._profile_id.encode("ascii"),
                b"\x00",
                credentials.subject_token.encode("ascii"),
                b"\x00",
                credentials.inspector_token.encode("ascii"),
            )
        )
        return hmac.new(credentials.binding_key, material, hashlib.sha256).digest()
    except AdhelmError:
        raise
    except Exception:
        raise _session_invalid(policy) from None


def _post_inspection_bundle_matches(
    directory: Path,
    context: token_credentials._MetaTokenInspectionContext,
    expected_marker: bytes,
    expected_binding_ref: str,
    policy: _SessionPolicy,
) -> bool:
    """Compare one transient reread without retaining either protected token."""
    credentials = token_credentials._resolve_meta_token_inspection_credentials(directory, context)
    try:
        marker = _continuity_marker(credentials, context, policy)
        binding_ref = _credential_digest(credentials)
    finally:
        del credentials
    return hmac.compare_digest(expected_marker, marker) and hmac.compare_digest(
        expected_binding_ref, binding_ref
    )


def _checked_observation(
    value: object,
    profile: MetaAccountTokenProfile,
    *,
    expected_binding_ref: str,
    policy: _SessionPolicy,
) -> tuple[datetime, datetime, datetime, str]:
    if type(value) is not MetaTokenObservation:
        raise _session_invalid(policy)
    try:
        profile_ref = value.profile_ref
        application = value.client_application_ref
        principal_ref = value.principal_ref
        binding_ref = value.credential_binding_ref
        scopes = value.scopes
        observed_at = _utc_time(value.observed_at)
        expires_at = _utc_time(value.expires_at)
        data_access_expires_at = _utc_time(value.data_access_expires_at)
    except AdhelmError:
        raise
    except Exception:
        raise _session_invalid(policy) from None
    if (
        profile_ref != profile.profile_id
        or application != profile.client_application_ref
        or type(principal_ref) is not str
        or _PRINCIPAL_REF.fullmatch(principal_ref) is None
        or type(binding_ref) is not str
        or _CREDENTIAL_BINDING_REF.fullmatch(binding_ref) is None
        or not hmac.compare_digest(binding_ref, expected_binding_ref)
        or type(scopes) is not tuple
        or not 1 <= len(scopes) <= 100
        or tuple(sorted(scopes)) != scopes
        or len(set(scopes)) != len(scopes)
        or any(type(scope) is not str or _SCOPE.fullmatch(scope) is None for scope in scopes)
        or policy._scope not in scopes
        or observed_at >= expires_at
        or observed_at >= data_access_expires_at
    ):
        raise _session_invalid(policy)
    return observed_at, expires_at, data_access_expires_at, principal_ref


def _checked_identity(value: object, profile: MetaAccountTokenProfile) -> MetaAdsIdentity:
    if type(value) is not MetaAdsIdentity:
        raise _identity_mismatch()
    try:
        identity = MetaAdsIdentity.model_validate(value.model_dump(mode="python", warnings="error"))
    except Exception:
        raise _identity_mismatch() from None
    expected_client_reference = f"act_{profile.account_ref}"
    expected_reference = f"meta_ads.account.{expected_client_reference}"
    if (
        identity.account_client_ref != expected_client_reference
        or identity.account_ref != expected_reference
        or identity.currency != profile.currency
        or identity.timezone != profile.timezone
    ):
        raise _identity_mismatch()
    return identity


def _current_subject_token(context: _TokenSessionContext) -> str:
    with configuration_transaction(context._directory) as transaction:
        profile = _current_profile(transaction, context)
        with credential_mutation_lock(profile.credential_ref):
            backend = token_credentials._credential_backend()
            bundle = _verify_current_state_locked(transaction, backend, context)
            subject_token = bundle.subject_token
    return subject_token


def _verify_current_state_locked(
    transaction: ConfigurationTransaction,
    backend: token_credentials._CredentialStore,
    context: _TokenSessionContext,
) -> token_credentials._MetaTokenCredentialBundle:
    """Verify state with caller-held config then credential locks only."""
    profile = _current_profile(transaction, context)
    raw = token_credentials._read_raw_slot(backend, profile.credential_ref)
    if raw is None:
        raise token_credentials._credential_missing()
    bundle = token_credentials._bind_bundle(token_credentials._decode_bundle(raw), profile)
    marker = _continuity_marker(
        MetaTokenInspectionCredentials(
            profile_ref=bundle.profile_id,
            subject_token=bundle.subject_token,
            inspector_token=bundle.inspector_token,
            binding_key=bundle.binding_key,
        ),
        context._inspection_context,
        context._policy,
    )
    if not hmac.compare_digest(marker, context._continuity_marker):
        raise token_credentials._credential_changed()
    _check_not_expired(context)
    transaction.require_unchanged(context._expected_configuration_digest)
    return bundle


def _check_not_expired(context: _TokenSessionContext) -> None:
    current = _utc_time(_now())
    if current < context._observed_at:
        raise AdhelmError(
            "CLOCK_INVALID",
            "Meta token session clock moved before its inspection observation.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    if current >= context._expires_at or current >= context._data_access_expires_at:
        raise AdhelmError(
            "META_TOKEN_EXPIRED",
            "The inspected Meta token is no longer usable for this fixed session.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Inspect the configured Meta token again before a new fixed operation.",
        )


def _utc_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AdhelmError(
            "CLOCK_INVALID",
            "Meta token session requires an aware UTC clock.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    return value.astimezone(UTC)


def _profile_required(policy: _SessionPolicy) -> AdhelmError:
    return AdhelmError(
        f"META_TOKEN_{policy._kind}_PROFILE_REQUIRED",
        (
            f"Meta Ads token {policy._kind.lower()} session requires a configured Meta "
            f"{policy._kind} profile."
        ),
        exit_code=ExitCode.AUTHORIZATION,
        next_action=(
            f"Configure a dedicated Meta {policy._kind} profile before a fixed account operation."
        ),
    )


def _identity_mismatch() -> AdhelmError:
    return AdhelmError(
        "IDENTITY_MISMATCH",
        "Meta Ads account identity differs from the configured declaration.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Review the configured Meta account declarations before a new fixed operation.",
    )


def _session_invalid(policy: _SessionPolicy) -> AdhelmError:
    return AdhelmError(
        f"META_TOKEN_{policy._kind}_SESSION_INVALID",
        f"Meta Ads token {policy._kind.lower()} session state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            f"Start a new fixed Meta {policy._kind.lower()} session from current "
            "local configuration."
        ),
    )


def _session_closed(policy: _SessionPolicy) -> AdhelmError:
    return AdhelmError(
        f"META_TOKEN_{policy._kind}_SESSION_CLOSED",
        f"Meta Ads token {policy._kind.lower()} session is no longer active.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            f"Start a new fixed Meta {policy._kind.lower()} session from current "
            "local configuration."
        ),
    )


def _session_unavailable(policy: _SessionPolicy) -> AdhelmError:
    return AdhelmError(
        f"META_TOKEN_{policy._kind}_SESSION_BUSY",
        (
            f"Meta Ads token {policy._kind.lower()} fixed reads are unavailable during "
            "retention recheck."
        ),
        exit_code=ExitCode.PRECONDITION,
        next_action="Finish the current local retention step before another fixed provider read.",
    )


def _retention_recheck_required(policy: _SessionPolicy) -> AdhelmError:
    return AdhelmError(
        policy._retention_code,
        f"Meta Ads {policy._kind.lower()} results require one immediate local retention recheck.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Restart the fixed Meta operation before a durable history append.",
    )


def _configuration_changed() -> AdhelmError:
    return AdhelmError(
        "CONFIGURATION_CHANGED",
        "Local configuration changed during the requested operation.",
        next_action="Review the current configuration before a deliberate new action.",
    )

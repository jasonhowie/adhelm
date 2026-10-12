"""Closed Google desktop OAuth protocol primitives.

The module owns the narrowly supported authorization-code/PKCE wire contract.
It deliberately does not read configuration, launch a browser, bind a
loopback listener, access the OS credential store, or call a provider resource
API.  Secret-bearing values are private frozen dataclasses with no public
serialization method; callers may use them only to complete the immediate
local onboarding or protected refresh lifecycle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from adhelm.errors import AdhelmError, ExitCode

GOOGLE_OAUTH_AUTHORIZATION_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_CERTS_ENDPOINT: Final = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_OIDC_CANONICAL_ISSUER: Final = "https://accounts.google.com"
GOOGLE_OIDC_PRINCIPAL_REF_PREFIX: Final = "google_oidc_principal_v1"

# This is imported by the Profile validator, so it must remain independent of
# models, credentials, keyring, CLI registration, and provider adapters.
GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE: Final[Mapping[tuple[str, str], str]] = (
    MappingProxyType(
        {
            ("google_ads", "READ"): "https://www.googleapis.com/auth/adwords",
            ("google_ads", "EDIT"): "https://www.googleapis.com/auth/adwords",
            ("ga4", "READ"): "https://www.googleapis.com/auth/analytics.readonly",
            ("ga4_admin", "READ"): "https://www.googleapis.com/auth/analytics.readonly",
            ("ga4_admin", "EDIT"): "https://www.googleapis.com/auth/analytics.edit",
            ("gtm", "READ"): "https://www.googleapis.com/auth/tagmanager.readonly",
        }
    )
)

_SUPPORTED_API_SCOPES: Final = frozenset(GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.values())
_GOOGLE_ISSUERS: Final = frozenset({GOOGLE_OIDC_CANONICAL_ISSUER, "accounts.google.com"})
_CLIENT_REF = re.compile(r"^[A-Za-z0-9_-]{1,120}\.apps\.googleusercontent\.com$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_PRINCIPAL_REF = re.compile(r"^google_oidc_principal_v1\.[0-9a-f]{64}$")
_LOOPBACK_PATH = re.compile(r"^/oauth/callback/[A-Za-z0-9_-]{32}$")
_AUTH_CODE = re.compile(r"^[\x21-\x7e]{1,4096}$")
_SECRET_TEXT = re.compile(r"^[\x21-\x7e]{1,16384}$")
_MAX_CALLBACK_QUERY_BYTES: Final = 8_192
_STATE_BYTES: Final = 32
_NONCE_BYTES: Final = 32
_PKCE_RANDOM_BYTES: Final = 64
_FLOW_LIFETIME: Final = timedelta(minutes=10)
_MAX_TOKEN_RESPONSE_BYTES: Final = 65_536
_MAX_UNIX_SECONDS: Final = 4_102_444_800  # 2100-01-01T00:00:00Z
_MAX_REFRESH_TOKEN_LIFETIME_SECONDS: Final = 315_576_000  # ten Gregorian years
_MAX_FUTURE_ISSUED_AT_SKEW_SECONDS: Final = 60
_PRINCIPAL_DOMAIN: Final = b"adhelm-google-oidc-principal-ref-v1\x00"


class GoogleOAuthTokenDispatchState(StrEnum):
    """Whether a token request may have reached Google."""

    NOT_DISPATCHED = "NOT_DISPATCHED"
    DEFINITE_RESPONSE = "DEFINITE_RESPONSE"
    UNCERTAIN = "UNCERTAIN"


class GoogleOAuthTransportError(AdhelmError):
    """Sanitized fixed-token error carrying only the dispatch classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        dispatch_state: GoogleOAuthTokenDispatchState,
        exit_code: int = ExitCode.AUTHORIZATION,
        next_action: str = "Reconnect the configured Google profile locally.",
    ) -> None:
        super().__init__(code, message, exit_code=exit_code, next_action=next_action)
        self.dispatch_state = dispatch_state


class GoogleOAuthTokenTransport(Protocol):
    """Opaque token-form seam used by protocol and fake-only tests."""

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes: ...


class GoogleOAuthCertificateTransport(Protocol):
    """Opaque certificate-read seam used only by the maintained verifier."""

    def get_google_oauth_certificates(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class GoogleOAuthTarget:
    """The one public, non-secret target selected from a valid OAuth profile."""

    client_ref: str
    api_scope: str

    def __post_init__(self) -> None:
        _validate_target_fields(self.client_ref, self.api_scope)


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthAuthorizationAttempt:
    """One in-memory PKCE/state/nonce-bound authorization attempt."""

    target: GoogleOAuthTarget
    redirect_uri: str
    state: str
    nonce: str
    code_verifier: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        target = _checked_target(self.target)
        _validate_loopback_redirect_uri(self.redirect_uri)
        _decode_exact_base64url(self.state, expected_bytes=_STATE_BYTES)
        _decode_exact_base64url(self.nonce, expected_bytes=_NONCE_BYTES)
        _validate_pkce_verifier(self.code_verifier)
        issued_at = _checked_time(self.issued_at)
        expires_at = _checked_time(self.expires_at)
        if expires_at != issued_at + _FLOW_LIFETIME:
            raise ValueError("OAuth authorization attempt lifetime is invalid")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True, slots=True, repr=False)
class GoogleAuthorizationCode:
    """A callback code bound to one in-memory attempt state."""

    code: str
    state: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _AUTH_CODE.fullmatch(self.code) is None:
            raise ValueError("OAuth authorization code is invalid")
        _decode_exact_base64url(self.state, expected_bytes=_STATE_BYTES)


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedGoogleOidcIdentity:
    """Initial signed OIDC identity, for transient/protected-bundle use only."""

    canonical_issuer: str
    raw_subject: str
    principal_ref: str
    client_ref: str

    def __post_init__(self) -> None:
        canonical_issuer = _canonical_google_issuer(self.canonical_issuer)
        _validate_google_subject(self.raw_subject)
        _validate_client_ref(self.client_ref)
        if (
            type(self.principal_ref) is not str
            or _PRINCIPAL_REF.fullmatch(self.principal_ref) is None
            or not hmac.compare_digest(
                self.principal_ref,
                derive_google_oidc_principal_ref(canonical_issuer, self.raw_subject),
            )
        ):
            raise ValueError("Google OIDC principal reference is invalid")
        object.__setattr__(self, "canonical_issuer", canonical_issuer)


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthInitialGrant:
    """Verified initial token exchange; never serialize this object publicly."""

    target: GoogleOAuthTarget
    access_token: str
    expires_at: datetime
    refresh_token: str
    refresh_token_expires_at: datetime | None
    requested_scopes: tuple[str, str]
    returned_scopes: tuple[str, ...]
    identity: VerifiedGoogleOidcIdentity

    def __post_init__(self) -> None:
        target = _checked_target(self.target)
        _validate_secret_text(self.access_token)
        _validate_secret_text(self.refresh_token)
        expires_at = _checked_time(self.expires_at)
        refresh_token_expires_at = (
            None
            if self.refresh_token_expires_at is None
            else _checked_time(self.refresh_token_expires_at)
        )
        if type(self.requested_scopes) is not tuple or self.requested_scopes != (
            "openid",
            target.api_scope,
        ):
            raise ValueError("OAuth requested scopes are invalid")
        _validate_granted_scopes(self.returned_scopes, target.api_scope, required=True)
        identity = _checked_identity(self.identity)
        if identity.client_ref != target.client_ref:
            raise ValueError("Google OIDC client binding is invalid")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "refresh_token_expires_at", refresh_token_expires_at)
        object.__setattr__(self, "identity", identity)


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthRefreshGrant:
    """One normalized refresh response; it has no identity-replacement capability."""

    target: GoogleOAuthTarget
    access_token: str
    expires_at: datetime
    returned_scopes: tuple[str, ...] | None
    identity_reverified: bool
    rotated_refresh_token: str | None
    rotated_refresh_token_expires_at: datetime | None

    def __post_init__(self) -> None:
        target = _checked_target(self.target)
        _validate_secret_text(self.access_token)
        expires_at = _checked_time(self.expires_at)
        if self.returned_scopes is not None:
            _validate_granted_scopes(self.returned_scopes, target.api_scope, required=True)
        if type(self.identity_reverified) is not bool:
            raise ValueError("Google refresh identity status is invalid")
        if self.rotated_refresh_token is not None:
            _validate_secret_text(self.rotated_refresh_token)
        if self.rotated_refresh_token is None and self.rotated_refresh_token_expires_at is not None:
            raise ValueError("Google OAuth refresh-token expiry is invalid")
        rotated_refresh_token_expires_at = (
            None
            if self.rotated_refresh_token_expires_at is None
            else _checked_time(self.rotated_refresh_token_expires_at)
        )
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self, "rotated_refresh_token_expires_at", rotated_refresh_token_expires_at
        )


def start_google_oauth_authorization(
    target: GoogleOAuthTarget,
    redirect_uri: str,
) -> GoogleOAuthAuthorizationAttempt:
    """Create one secure, fixed authorization request after loopback binding.

    The caller must bind its IPv4 loopback listener first and pass the actual
    callback URL.  This helper never launches the browser or emits the URL.
    """
    try:
        checked_target = _checked_target(target)
        _validate_loopback_redirect_uri(redirect_uri)
        issued_at = _current_time()
        return GoogleOAuthAuthorizationAttempt(
            target=checked_target,
            redirect_uri=redirect_uri,
            state=_random_base64url(_STATE_BYTES),
            nonce=_random_base64url(_NONCE_BYTES),
            code_verifier=_random_base64url(_PKCE_RANDOM_BYTES),
            issued_at=issued_at,
            expires_at=issued_at + _FLOW_LIFETIME,
        )
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_INVALID",
            "Google OAuth authorization cannot be prepared.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def google_oauth_authorization_url(attempt: GoogleOAuthAuthorizationAttempt) -> str:
    """Revalidate one current attempt before handing its exact URL to the OS browser.

    This is the sole browser-facing URL constructor.  Browser code should
    accept the typed attempt, never an arbitrary URL string.
    """
    checked = _checked_attempt(attempt)
    if _current_time() >= checked.expires_at:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_EXPIRED",
            "Google OAuth authorization expired before browser handoff.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    return _authorization_url(checked)


def _authorization_url(attempt: GoogleOAuthAuthorizationAttempt) -> str:
    query = urlencode(
        (
            ("response_type", "code"),
            ("client_id", attempt.target.client_ref),
            ("redirect_uri", attempt.redirect_uri),
            ("scope", f"openid {attempt.target.api_scope}"),
            ("state", attempt.state),
            ("nonce", attempt.nonce),
            ("code_challenge", pkce_s256_challenge(attempt.code_verifier)),
            ("code_challenge_method", "S256"),
            ("access_type", "offline"),
            # Enrollment/reconnect needs a newly returned protected refresh
            # token.  Do not use unsupported incremental scopes.
            ("prompt", "consent"),
        )
    )
    return f"{GOOGLE_OAUTH_AUTHORIZATION_ENDPOINT}?{query}"


def consume_google_oauth_callback(
    attempt: GoogleOAuthAuthorizationAttempt,
    raw_query: str,
) -> GoogleAuthorizationCode:
    """Validate one raw loopback query without exposing provider callback data."""
    checked = _checked_attempt(attempt)
    current = _current_time()
    if current >= checked.expires_at:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_EXPIRED",
            "Google OAuth authorization expired before the local callback completed.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    fields = _parse_callback_query(raw_query)
    # RFC 9207 requires exact issuer comparison when the response supplies it.
    # Do not apply the legacy ID-token issuer normalization to this field.
    if "iss" in fields and fields["iss"] != GOOGLE_OIDC_CANONICAL_ISSUER:
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback issuer did not match the expected provider.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    state = fields.get("state")
    if type(state) is not str or not hmac.compare_digest(state, checked.state):
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback did not match the local authorization attempt.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    if "error" in fields:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_DENIED",
            "Google OAuth authorization did not return a usable local grant.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Review the requested local Google authorization and reconnect deliberately."
            ),
        )
    code = fields.get("code")
    if type(code) is not str or _AUTH_CODE.fullmatch(code) is None:
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback did not contain a usable authorization code.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    return GoogleAuthorizationCode(code=code, state=checked.state)


def complete_google_oauth_authorization(
    attempt: GoogleOAuthAuthorizationAttempt,
    callback: GoogleAuthorizationCode,
    *,
    token_transport: GoogleOAuthTokenTransport,
    certificate_transport: GoogleOAuthCertificateTransport,
    client_secret: str | None = None,
) -> GoogleOAuthInitialGrant:
    """Exchange one callback code and verify its nonce-bound OIDC identity."""
    checked_attempt = _checked_attempt(attempt)
    checked_callback = _checked_callback(callback, checked_attempt)
    current = _current_time()
    if current >= checked_attempt.expires_at:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_EXPIRED",
            "Google OAuth authorization expired before token exchange.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    form: list[tuple[str, str]] = [
        ("client_id", checked_attempt.target.client_ref),
        ("code", checked_callback.code),
        ("code_verifier", checked_attempt.code_verifier),
        ("grant_type", "authorization_code"),
        ("redirect_uri", checked_attempt.redirect_uri),
    ]
    if client_secret is not None:
        _validate_secret_text(client_secret)
        form.insert(1, ("client_secret", client_secret))
    raw_response = _post_token_form(token_transport, tuple(form))
    parsed = _parse_initial_token_response(raw_response, checked_attempt.target)
    identity = verify_google_oidc_identity(
        parsed.id_token,
        target=checked_attempt.target,
        expected_nonce=checked_attempt.nonce,
        certificate_transport=certificate_transport,
    )
    return GoogleOAuthInitialGrant(
        target=checked_attempt.target,
        access_token=parsed.access_token,
        expires_at=current + timedelta(seconds=parsed.expires_in),
        refresh_token=parsed.refresh_token,
        refresh_token_expires_at=(
            None
            if parsed.refresh_token_expires_in is None
            else current + timedelta(seconds=parsed.refresh_token_expires_in)
        ),
        requested_scopes=("openid", checked_attempt.target.api_scope),
        returned_scopes=parsed.returned_scopes,
        identity=identity,
    )


def refresh_google_oauth_grant(
    target: GoogleOAuthTarget,
    expected_identity: VerifiedGoogleOidcIdentity,
    *,
    refresh_token: str,
    token_transport: GoogleOAuthTokenTransport,
    certificate_transport: GoogleOAuthCertificateTransport,
    client_secret: str | None = None,
) -> GoogleOAuthRefreshGrant:
    """Refresh an already sealed grant without establishing a new identity."""
    checked_target = _checked_target(target)
    checked_identity = _checked_identity(expected_identity)
    if checked_identity.client_ref != checked_target.client_ref:
        raise AdhelmError(
            "GOOGLE_OAUTH_IDENTITY_BINDING_INVALID",
            "Google OAuth protected identity does not match the selected client.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        )
    _validate_secret_text(refresh_token)
    current = _current_time()
    form: list[tuple[str, str]] = [
        ("client_id", checked_target.client_ref),
        ("grant_type", "refresh_token"),
        ("refresh_token", refresh_token),
    ]
    if client_secret is not None:
        _validate_secret_text(client_secret)
        form.insert(1, ("client_secret", client_secret))
    raw_response = _post_token_form(token_transport, tuple(form))
    parsed = _parse_refresh_token_response(raw_response, checked_target)
    identity_reverified = verify_google_refresh_identity_token(
        parsed.id_token,
        target=checked_target,
        expected_identity=checked_identity,
        certificate_transport=certificate_transport,
    )
    return GoogleOAuthRefreshGrant(
        target=checked_target,
        access_token=parsed.access_token,
        expires_at=current + timedelta(seconds=parsed.expires_in),
        returned_scopes=parsed.returned_scopes,
        identity_reverified=identity_reverified,
        rotated_refresh_token=parsed.rotated_refresh_token,
        rotated_refresh_token_expires_at=(
            None
            if parsed.rotated_refresh_token_expires_in is None
            else current + timedelta(seconds=parsed.rotated_refresh_token_expires_in)
        ),
    )


def verify_google_oidc_identity(
    id_token: str,
    *,
    target: GoogleOAuthTarget,
    expected_nonce: str,
    certificate_transport: GoogleOAuthCertificateTransport,
) -> VerifiedGoogleOidcIdentity:
    """Verify an initial OIDC token and require its exact one-use nonce."""
    checked_target = _checked_target(target)
    _decode_exact_base64url(expected_nonce, expected_bytes=_NONCE_BYTES)
    claims = _verified_google_claims(
        id_token,
        target=checked_target,
        certificate_transport=certificate_transport,
        now=_current_time(),
    )
    nonce = claims.get("nonce")
    if type(nonce) is not str or not hmac.compare_digest(nonce, expected_nonce):
        raise AdhelmError(
            "GOOGLE_OIDC_IDENTITY_INVALID",
            "Google identity token did not match the local authorization attempt.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    issuer = _canonical_google_issuer(claims["iss"])
    subject = claims["sub"]
    assert type(subject) is str  # checked by _verified_google_claims
    return VerifiedGoogleOidcIdentity(
        canonical_issuer=issuer,
        raw_subject=subject,
        principal_ref=derive_google_oidc_principal_ref(issuer, subject),
        client_ref=checked_target.client_ref,
    )


def verify_google_refresh_identity_token(
    id_token: str | None,
    *,
    target: GoogleOAuthTarget,
    expected_identity: VerifiedGoogleOidcIdentity,
    certificate_transport: GoogleOAuthCertificateTransport,
) -> bool:
    """Optionally reverify a refresh ID token without nonce or identity replacement.

    A missing refresh ID token returns ``False`` and makes no fresh identity
    claim.  A present token must match the sealed initial identity exactly.
    """
    if id_token is None:
        return False
    checked_target = _checked_target(target)
    checked_identity = _checked_identity(expected_identity)
    if checked_identity.client_ref != checked_target.client_ref:
        raise AdhelmError(
            "GOOGLE_OAUTH_IDENTITY_BINDING_INVALID",
            "Google OAuth protected identity does not match the selected client.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        )
    claims = _verified_google_claims(
        id_token,
        target=checked_target,
        certificate_transport=certificate_transport,
        now=_current_time(),
    )
    issuer = _canonical_google_issuer(claims["iss"])
    subject = claims["sub"]
    assert type(subject) is str  # checked by _verified_google_claims
    actual_ref = derive_google_oidc_principal_ref(issuer, subject)
    if not (
        hmac.compare_digest(issuer, checked_identity.canonical_issuer)
        and hmac.compare_digest(subject, checked_identity.raw_subject)
        and hmac.compare_digest(actual_ref, checked_identity.principal_ref)
    ):
        raise AdhelmError(
            "GOOGLE_OAUTH_IDENTITY_CHANGED",
            "Google OAuth refresh identity differs from the protected initial identity.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        )
    return True


def derive_google_oidc_principal_ref(issuer: str, subject: str) -> str:
    """Derive the fixed-size operational reference from verified issuer and ``sub``."""
    canonical_issuer = _canonical_google_issuer(issuer)
    _validate_google_subject(subject)
    digest = hashlib.sha256(
        _PRINCIPAL_DOMAIN + canonical_issuer.encode("ascii") + b"\x00" + subject.encode("ascii")
    ).hexdigest()
    return f"{GOOGLE_OIDC_PRINCIPAL_REF_PREFIX}.{digest}"


def is_google_oauth_client_id(value: object) -> bool:
    """Return whether one public profile value is a supported installed client ID."""
    return type(value) is str and _CLIENT_REF.fullmatch(value) is not None


def is_google_oauth_operational_principal_ref(value: object) -> bool:
    """Return whether one profile ref has the closed derived OIDC grammar."""
    return type(value) is str and _PRINCIPAL_REF.fullmatch(value) is not None


def pkce_s256_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for one validated verifier."""
    _validate_pkce_verifier(code_verifier)
    return (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def revalidate_google_oauth_initial_grant(value: object) -> GoogleOAuthInitialGrant:
    """Detach a potentially constructed initial grant before protected storage."""
    if type(value) is not GoogleOAuthInitialGrant:
        raise AdhelmError(
            "GOOGLE_OAUTH_GRANT_INVALID",
            "Google OAuth initial grant is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    try:
        return GoogleOAuthInitialGrant(
            target=_checked_target(value.target),
            access_token=value.access_token,
            expires_at=value.expires_at,
            refresh_token=value.refresh_token,
            refresh_token_expires_at=value.refresh_token_expires_at,
            requested_scopes=value.requested_scopes,
            returned_scopes=value.returned_scopes,
            identity=_checked_identity(value.identity),
        )
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_GRANT_INVALID",
            "Google OAuth initial grant is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def revalidate_google_oauth_refresh_grant(value: object) -> GoogleOAuthRefreshGrant:
    """Detach a potentially constructed refresh grant before protected storage."""
    if type(value) is not GoogleOAuthRefreshGrant:
        raise AdhelmError(
            "GOOGLE_OAUTH_REFRESH_GRANT_INVALID",
            "Google OAuth refresh result is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    try:
        return GoogleOAuthRefreshGrant(
            target=_checked_target(value.target),
            access_token=value.access_token,
            expires_at=value.expires_at,
            returned_scopes=value.returned_scopes,
            identity_reverified=value.identity_reverified,
            rotated_refresh_token=value.rotated_refresh_token,
            rotated_refresh_token_expires_at=value.rotated_refresh_token_expires_at,
        )
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_REFRESH_GRANT_INVALID",
            "Google OAuth refresh result is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


@dataclass(frozen=True, slots=True, repr=False)
class _InitialTokenResponse:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_token_expires_in: int | None
    returned_scopes: tuple[str, ...]
    id_token: str


@dataclass(frozen=True, slots=True, repr=False)
class _RefreshTokenResponse:
    access_token: str
    expires_in: int
    returned_scopes: tuple[str, ...] | None
    id_token: str | None
    rotated_refresh_token: str | None
    rotated_refresh_token_expires_in: int | None


def _post_token_form(
    transport: GoogleOAuthTokenTransport, fields: tuple[tuple[str, str], ...]
) -> bytes:
    try:
        response = transport.post_token_form(fields)
    except GoogleOAuthTransportError:
        raise
    except Exception:
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_TOKEN_OUTCOME_UNCERTAIN",
            "Google OAuth token exchange outcome is uncertain; reconnect before another attempt.",
            dispatch_state=GoogleOAuthTokenDispatchState.UNCERTAIN,
            exit_code=ExitCode.AMBIGUOUS,
        ) from None
    if type(response) is not bytes or len(response) > _MAX_TOKEN_RESPONSE_BYTES:
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_TOKEN_OUTCOME_UNCERTAIN",
            "Google OAuth token exchange outcome is uncertain; reconnect before another attempt.",
            dispatch_state=GoogleOAuthTokenDispatchState.UNCERTAIN,
            exit_code=ExitCode.AMBIGUOUS,
        )
    return response


def _parse_initial_token_response(raw: bytes, target: GoogleOAuthTarget) -> _InitialTokenResponse:
    data = _parse_token_response(raw)
    required = {"access_token", "expires_in", "token_type", "scope", "refresh_token", "id_token"}
    allowed = required | {"refresh_token_expires_in"}
    if not required.issubset(data) or not set(data) <= allowed:
        raise _unsupported_token_response()
    access_token = _token_string(data, "access_token")
    expires_in = _expires_in(data)
    if data.get("token_type") != "Bearer":
        raise _unsupported_token_response()
    scope = _normalized_scope_text(data.get("scope"), target.api_scope, required=True)
    refresh_token = _token_string(data, "refresh_token")
    id_token = _token_string(data, "id_token")
    return _InitialTokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=refresh_token,
        refresh_token_expires_in=_refresh_token_expires_in(data),
        returned_scopes=scope,
        id_token=id_token,
    )


def _parse_refresh_token_response(raw: bytes, target: GoogleOAuthTarget) -> _RefreshTokenResponse:
    data = _parse_token_response(raw)
    allowed = {
        "access_token",
        "expires_in",
        "token_type",
        "scope",
        "id_token",
        "refresh_token",
        "refresh_token_expires_in",
    }
    if not {"access_token", "expires_in", "token_type"}.issubset(data) or not set(data) <= allowed:
        raise _unsupported_token_response()
    access_token = _token_string(data, "access_token")
    expires_in = _expires_in(data)
    if data.get("token_type") != "Bearer":
        raise _unsupported_token_response()
    returned_scopes = (
        None
        if "scope" not in data
        else _normalized_scope_text(data["scope"], target.api_scope, required=True)
    )
    id_token = None if "id_token" not in data else _token_string(data, "id_token")
    rotated = None if "refresh_token" not in data else _token_string(data, "refresh_token")
    rotated_expires_in = _refresh_token_expires_in(data)
    if rotated is None and rotated_expires_in is not None:
        raise _unsupported_token_response()
    return _RefreshTokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        returned_scopes=returned_scopes,
        id_token=id_token,
        rotated_refresh_token=rotated,
        rotated_refresh_token_expires_in=rotated_expires_in,
    )


def _parse_token_response(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_TOKEN_RESPONSE_BYTES:
        raise _unsupported_token_response()
    try:
        from adhelm.canonical import parse_json

        data = parse_json(raw, maximum_bytes=_MAX_TOKEN_RESPONSE_BYTES, decimal_numbers=True)
    except AdhelmError:
        raise _unsupported_token_response() from None
    if type(data) is not dict or not all(type(key) is str for key in data):
        raise _unsupported_token_response()
    return data


def _token_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if type(value) is not str:
        raise _unsupported_token_response()
    try:
        _validate_secret_text(value)
    except ValueError:
        raise _unsupported_token_response() from None
    return value


def _expires_in(data: Mapping[str, object]) -> int:
    value = data.get("expires_in")
    if type(value) is not int or not 1 <= value <= 86_400:
        raise _unsupported_token_response()
    return value


def _refresh_token_expires_in(data: Mapping[str, object]) -> int | None:
    if "refresh_token_expires_in" not in data:
        return None
    value = data["refresh_token_expires_in"]
    if type(value) is not int or not 1 <= value <= _MAX_REFRESH_TOKEN_LIFETIME_SECONDS:
        raise _unsupported_token_response()
    return value


def _normalized_scope_text(value: object, api_scope: str, *, required: bool) -> tuple[str, ...]:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 512:
        raise _unsupported_token_response()
    tokens = value.split(" ")
    if any(not item for item in tokens) or len(tokens) != len(set(tokens)):
        raise _unsupported_token_response()
    token_set = frozenset(tokens)
    if api_scope not in token_set or not token_set <= {"openid", api_scope}:
        raise _unsupported_token_response()
    normalized = tuple(item for item in ("openid", api_scope) if item in token_set)
    _validate_granted_scopes(normalized, api_scope, required=required)
    return normalized


def _validate_granted_scopes(value: object, api_scope: str, *, required: bool) -> None:
    if type(value) is not tuple or not value:
        raise ValueError("OAuth granted scopes are invalid")
    if not all(type(scope) is str for scope in value):
        raise ValueError("OAuth granted scopes are invalid")
    permitted = (api_scope,) if value == (api_scope,) else ("openid", api_scope)
    if value != permitted or (required and api_scope not in value):
        raise ValueError("OAuth granted scopes are invalid")


def _verified_google_claims(
    id_token: str,
    *,
    target: GoogleOAuthTarget,
    certificate_transport: GoogleOAuthCertificateTransport,
    now: datetime,
) -> Mapping[str, object]:
    try:
        _validate_secret_text(id_token)
    except ValueError:
        raise AdhelmError(
            "GOOGLE_OIDC_IDENTITY_INVALID",
            "Google identity token is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        ) from None
    try:
        # Initial acquisition is intentionally outside google-auth.  The
        # maintained verifier owns issuer/audience/signature/expiry checking
        # and receives only our fixed certificate GET adapter.
        from google.auth import exceptions as google_auth_exceptions
        from google.oauth2 import id_token as google_id_token

        from adhelm.google_oauth_transport import GoogleOAuthCertificateRequest

        claims = google_id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            id_token,
            GoogleOAuthCertificateRequest(certificate_transport),
            audience=target.client_ref,
            clock_skew_in_seconds=0,
        )
    except GoogleOAuthTransportError:
        raise AdhelmError(
            "GOOGLE_OIDC_CERTIFICATES_UNAVAILABLE",
            "Google identity verification certificates are unavailable.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Retry local identity verification after the fixed certificate "
                "service is available."
            ),
        ) from None
    except Exception as exc:
        # Avoid importing exception classes until the optional verification
        # path runs, and do not surface verifier/provider text.
        if "google_auth_exceptions" in locals() and isinstance(
            exc, google_auth_exceptions.TransportError
        ):
            raise AdhelmError(
                "GOOGLE_OIDC_CERTIFICATES_UNAVAILABLE",
                "Google identity verification certificates are unavailable.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Retry local identity verification after the fixed certificate "
                    "service is available."
                ),
            ) from None
        raise AdhelmError(
            "GOOGLE_OIDC_IDENTITY_INVALID",
            "Google identity token is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        ) from None
    if type(claims) is not dict:
        raise AdhelmError(
            "GOOGLE_OIDC_IDENTITY_INVALID",
            "Google identity token is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        )
    _validate_verified_claims(claims, target=target, now=now)
    return claims


def _validate_verified_claims(
    claims: Mapping[str, object], *, target: GoogleOAuthTarget, now: datetime
) -> None:
    issuer = claims.get("iss")
    audience = claims.get("aud")
    subject = claims.get("sub")
    expiry = claims.get("exp")
    issued_at = claims.get("iat")
    authorized_party = claims.get("azp")
    try:
        _canonical_google_issuer(issuer)
        _validate_google_subject(subject)
        if type(audience) is not str or not hmac.compare_digest(audience, target.client_ref):
            raise ValueError("OIDC audience is invalid")
        if authorized_party is not None and (
            type(authorized_party) is not str
            or not hmac.compare_digest(authorized_party, target.client_ref)
        ):
            raise ValueError("OIDC authorized party is invalid")
        _validate_jwt_timestamp(expiry)
        _validate_jwt_timestamp(issued_at)
        assert type(expiry) is int
        assert type(issued_at) is int
        now_seconds = int(now.timestamp())
        if (
            expiry <= now_seconds
            or issued_at >= expiry
            or issued_at > now_seconds + _MAX_FUTURE_ISSUED_AT_SKEW_SECONDS
        ):
            raise ValueError("OIDC timestamp is invalid")
    except ValueError:
        raise AdhelmError(
            "GOOGLE_OIDC_IDENTITY_INVALID",
            "Google identity token is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reconnect the configured Google profile locally.",
        ) from None


def _validate_jwt_timestamp(value: object) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_UNIX_SECONDS:
        raise ValueError("OIDC timestamp is invalid")


def _parse_callback_query(raw_query: str) -> dict[str, str]:
    if (
        type(raw_query) is not str
        or not raw_query
        or len(raw_query.encode("utf-8")) > _MAX_CALLBACK_QUERY_BYTES
        or any(ord(character) < 32 or ord(character) > 126 for character in raw_query)
    ):
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=9)
    except ValueError:
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        ) from None
    # Google also returns UI/account-selection metadata. It is bounded here,
    # never used to establish identity, and discarded by the callback consumer.
    allowed = {
        "code",
        "state",
        "error",
        "error_description",
        "error_uri",
        "scope",
        "iss",
        "authuser",
        "hd",
        "prompt",
    }
    result: dict[str, str] = {}
    for key, value in pairs:
        if (
            key not in allowed
            or key in result
            or type(value) is not str
            or len(value.encode("utf-8")) > 4_096
            or any(ord(character) < 32 for character in value)
        ):
            raise AdhelmError(
                "GOOGLE_OAUTH_CALLBACK_INVALID",
                "Google OAuth callback is invalid.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Start a new local Google OAuth authorization.",
            )
        result[key] = value
    if "state" not in result or ("code" in result) == ("error" in result):
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Start a new local Google OAuth authorization.",
        )
    return result


def _checked_target(value: object) -> GoogleOAuthTarget:
    if type(value) is not GoogleOAuthTarget:
        raise ValueError("Google OAuth target is invalid")
    return GoogleOAuthTarget(client_ref=value.client_ref, api_scope=value.api_scope)


def _checked_attempt(value: object) -> GoogleOAuthAuthorizationAttempt:
    if type(value) is not GoogleOAuthAuthorizationAttempt:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_INVALID",
            "Google OAuth authorization is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    try:
        return GoogleOAuthAuthorizationAttempt(
            target=_checked_target(value.target),
            redirect_uri=value.redirect_uri,
            state=value.state,
            nonce=value.nonce,
            code_verifier=value.code_verifier,
            issued_at=value.issued_at,
            expires_at=value.expires_at,
        )
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_AUTHORIZATION_INVALID",
            "Google OAuth authorization is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _checked_callback(
    value: object, attempt: GoogleOAuthAuthorizationAttempt
) -> GoogleAuthorizationCode:
    if type(value) is not GoogleAuthorizationCode:
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    try:
        checked = GoogleAuthorizationCode(code=value.code, state=value.state)
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback is invalid.",
            exit_code=ExitCode.AUTHORIZATION,
        ) from None
    if not hmac.compare_digest(checked.state, attempt.state):
        raise AdhelmError(
            "GOOGLE_OAUTH_CALLBACK_INVALID",
            "Google OAuth callback did not match the local authorization attempt.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    return checked


def _checked_identity(value: object) -> VerifiedGoogleOidcIdentity:
    if type(value) is not VerifiedGoogleOidcIdentity:
        raise ValueError("Google OIDC identity is invalid")
    return VerifiedGoogleOidcIdentity(
        canonical_issuer=value.canonical_issuer,
        raw_subject=value.raw_subject,
        principal_ref=value.principal_ref,
        client_ref=value.client_ref,
    )


def _validate_target_fields(client_ref: object, api_scope: object) -> None:
    _validate_client_ref(client_ref)
    if type(api_scope) is not str or api_scope not in _SUPPORTED_API_SCOPES:
        raise ValueError("Google OAuth API scope is unsupported")


def _validate_client_ref(value: object) -> None:
    if not is_google_oauth_client_id(value):
        raise ValueError("Google OAuth client reference is unsupported")


def _validate_loopback_redirect_uri(value: object) -> None:
    if type(value) is not str or len(value) > 256:
        raise ValueError("Google OAuth redirect URI is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Google OAuth redirect URI is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or _LOOPBACK_PATH.fullmatch(parsed.path) is None
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or value != f"http://127.0.0.1:{port}{parsed.path}"
    ):
        raise ValueError("Google OAuth redirect URI is invalid")


def _validate_pkce_verifier(value: object) -> None:
    if type(value) is not str or _PKCE_VERIFIER.fullmatch(value) is None:
        raise ValueError("PKCE verifier is invalid")


def _validate_google_subject(value: object) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or not value.isascii()
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise ValueError("Google OIDC subject is invalid")


def _canonical_google_issuer(value: object) -> str:
    if type(value) is not str or value not in _GOOGLE_ISSUERS:
        raise ValueError("Google OIDC issuer is invalid")
    return GOOGLE_OIDC_CANONICAL_ISSUER


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
            "GOOGLE_OAUTH_RANDOMNESS_UNAVAILABLE",
            "Secure local OAuth randomness is unavailable.",
            exit_code=ExitCode.PRECONDITION,
        ) from None
    if type(value) is not bytes or len(value) != size:
        raise AdhelmError(
            "GOOGLE_OAUTH_RANDOMNESS_UNAVAILABLE",
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
            "GOOGLE_OAUTH_CLOCK_INVALID",
            "Google OAuth requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _unsupported_token_response() -> GoogleOAuthTransportError:
    return GoogleOAuthTransportError(
        "GOOGLE_OAUTH_TOKEN_RESPONSE_UNSUPPORTED",
        "Google OAuth token response cannot be safely accepted; reconnect before another attempt.",
        dispatch_state=GoogleOAuthTokenDispatchState.UNCERTAIN,
        exit_code=ExitCode.AMBIGUOUS,
    )

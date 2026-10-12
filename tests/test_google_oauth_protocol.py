from __future__ import annotations

import inspect
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

import adhelm.google_oauth_protocol as protocol
import adhelm.google_oauth_transport as oauth_transport
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_CERTS_ENDPOINT,
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthTarget,
    GoogleOAuthTransportError,
    consume_google_oauth_callback,
    derive_google_oidc_principal_ref,
    google_oauth_authorization_url,
    is_google_oauth_client_id,
    is_google_oauth_operational_principal_ref,
    pkce_s256_challenge,
    refresh_google_oauth_grant,
    revalidate_google_oauth_initial_grant,
    start_google_oauth_authorization,
)
from adhelm.google_oauth_transport import (
    GoogleOAuthCertificateRequest,
    GoogleOAuthTokenDispatchState,
)

_CLIENT_REF = "client_123.apps.googleusercontent.com"
_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
_REDIRECT_URI = "http://127.0.0.1:43123/oauth/callback/" + "A" * 32


class _FakeTransport:
    def __init__(self, token_response: bytes, certificates: bytes) -> None:
        self.token_response = token_response
        self.certificates = certificates
        self.forms: list[tuple[tuple[str, str], ...]] = []
        self.certificate_reads = 0

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        self.forms.append(fields)
        return self.token_response

    def get_google_oauth_certificates(self) -> bytes:
        self.certificate_reads += 1
        return self.certificates


def _target() -> GoogleOAuthTarget:
    return GoogleOAuthTarget(client_ref=_CLIENT_REF, api_scope=_SCOPE)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _attempt(
    monkeypatch: pytest.MonkeyPatch, now: datetime | None = None
) -> protocol.GoogleOAuthAuthorizationAttempt:
    current = now or _now()
    monkeypatch.setattr(protocol, "_utc_now", lambda: current)
    monkeypatch.setattr(protocol, "_secure_random_bytes", lambda size: bytes(range(size)))
    return start_google_oauth_authorization(_target(), _REDIRECT_URI)


def _signed_token(*, nonce: str | None, subject: str = "google-subject-123") -> tuple[str, bytes]:
    now = _now()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-google")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    payload: dict[str, Any] = {
        "iss": GOOGLE_OIDC_CANONICAL_ISSUER,
        "sub": subject,
        "aud": _CLIENT_REF,
        "azp": _CLIENT_REF,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    if nonce is not None:
        payload["nonce"] = nonce
    token = jwt.encode(crypt.RSASigner.from_string(private_pem), payload, key_id="test-key")
    certs = json.dumps(
        {
            "test-key": certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        }
    ).encode("utf-8")
    return token.decode("ascii"), certs


def _initial_response(id_token: str, **overrides: object) -> bytes:
    response: dict[str, object] = {
        "access_token": "access-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": f"openid {_SCOPE}",
        "refresh_token": "refresh-token",
        "id_token": id_token,
    }
    response.update(overrides)
    return json.dumps(response).encode("utf-8")


def test_profile_scope_map_and_operational_grammars_are_closed() -> None:
    assert GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE == {
        ("google_ads", "READ"): "https://www.googleapis.com/auth/adwords",
        ("google_ads", "EDIT"): "https://www.googleapis.com/auth/adwords",
        ("ga4", "READ"): _SCOPE,
        ("ga4_admin", "READ"): _SCOPE,
        ("ga4_admin", "EDIT"): "https://www.googleapis.com/auth/analytics.edit",
        ("gtm", "READ"): "https://www.googleapis.com/auth/tagmanager.readonly",
    }
    assert is_google_oauth_client_id(_CLIENT_REF)
    assert not is_google_oauth_client_id("client.apps.googleusercontent.com.evil")
    principal = derive_google_oidc_principal_ref("accounts.google.com", "subject-1")
    assert is_google_oauth_operational_principal_ref(principal)
    assert not is_google_oauth_operational_principal_ref(principal.upper())
    assert derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, "subject-1") == principal


def test_pkce_s256_uses_the_rfc7636_vector() -> None:
    assert (
        pkce_s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_authorization_handoff_is_exact_typed_and_current(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _now()
    attempt = _attempt(monkeypatch, now)
    query = parse_qs(urlsplit(google_oauth_authorization_url(attempt)).query, strict_parsing=True)
    assert query == {
        "response_type": ["code"],
        "client_id": [_CLIENT_REF],
        "redirect_uri": [_REDIRECT_URI],
        "scope": [f"openid {_SCOPE}"],
        "state": [attempt.state],
        "nonce": [attempt.nonce],
        "code_challenge": [pkce_s256_challenge(attempt.code_verifier)],
        "code_challenge_method": ["S256"],
        "access_type": ["offline"],
        "prompt": ["consent"],
    }
    monkeypatch.setattr(protocol, "_utc_now", lambda: attempt.expires_at)
    with pytest.raises(AdhelmError, match="expired") as raised:
        google_oauth_authorization_url(attempt)
    assert raised.value.code == "GOOGLE_OAUTH_AUTHORIZATION_EXPIRED"
    assert "now" not in inspect.signature(google_oauth_authorization_url).parameters
    assert "now" not in inspect.signature(consume_google_oauth_callback).parameters
    assert "random_bytes" not in inspect.signature(start_google_oauth_authorization).parameters


def test_callback_rejects_duplicate_state_and_never_exposes_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        consume_google_oauth_callback(
            attempt, f"code=private-code&state={attempt.state}&state={attempt.state}"
        )
    assert raised.value.code == "GOOGLE_OAUTH_CALLBACK_INVALID"
    assert "private-code" not in str(raised.value)


def test_callback_preserves_denial_with_exact_google_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        consume_google_oauth_callback(
            attempt,
            f"error=access_denied&state={attempt.state}&iss=https%3A%2F%2Faccounts.google.com",
        )
    assert raised.value.code == "GOOGLE_OAUTH_AUTHORIZATION_DENIED"


@pytest.mark.parametrize(
    "metadata", ["&authuser=0&prompt=consent", "&authuser=1&hd=example.com&prompt=consent"]
)
def test_callback_accepts_google_metadata_without_using_it_for_identity(
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(
        attempt,
        f"code=private-code&state={attempt.state}&iss=https%3A%2F%2Faccounts.google.com"
        f"&scope=openid{metadata}",
    )
    assert callback.code == "private-code"
    assert {field.name for field in fields(callback)} == {"code", "state"}


@pytest.mark.parametrize(
    "metadata",
    [
        "authuser=0&authuser=1",
        "prompt=consent&prompt=none",
        "hd=example.com&hd=other.example",
        "prompt=%0A",
        "unknown=private-value",
        "prompt=" + "x" * 4097,
    ],
)
def test_callback_rejects_duplicate_or_malformed_google_metadata(
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    attempt = _attempt(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        consume_google_oauth_callback(
            attempt, f"code=private-code&state={attempt.state}&{metadata}"
        )
    assert raised.value.code == "GOOGLE_OAUTH_CALLBACK_INVALID"
    assert "private-code" not in str(raised.value)


def test_callback_accepts_exact_google_response_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(
        attempt,
        f"code=private-code&state={attempt.state}&iss=https%3A%2F%2Faccounts.google.com",
    )
    assert callback.code == "private-code"


@pytest.mark.parametrize(
    "issuer",
    [
        "",
        "accounts.google.com",
        "http://accounts.google.com",
        "https://accounts.google.com/",
        "https://other.example",
        "https://accounts.google.com&iss=https://accounts.google.com",
    ],
)
@pytest.mark.parametrize("result", ["code=private-code", "error=access_denied"])
def test_callback_rejects_invalid_response_issuer(
    monkeypatch: pytest.MonkeyPatch,
    issuer: str,
    result: str,
) -> None:
    attempt = _attempt(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        consume_google_oauth_callback(attempt, f"{result}&state={attempt.state}&iss={issuer}")
    assert raised.value.code == "GOOGLE_OAUTH_CALLBACK_INVALID"
    assert "private-code" not in str(raised.value)
    assert "other.example" not in str(raised.value)


def test_initial_exchange_uses_actual_google_auth_verifier_and_exact_certificate_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    id_token, certs = _signed_token(nonce=attempt.nonce)
    transport = _FakeTransport(_initial_response(id_token), certs)
    seen: list[tuple[str, str, object, object]] = []
    base = GoogleOAuthCertificateRequest

    class RecordingRequest(base):
        def __call__(
            self,
            url: str,
            method: str = "GET",
            body: bytes | None = None,
            headers: object = None,
            timeout: float | None = None,
            **kwargs: object,
        ) -> object:
            seen.append((url, method, body, headers))
            return super().__call__(url, method, body, headers, timeout, **kwargs)

    monkeypatch.setattr(oauth_transport, "GoogleOAuthCertificateRequest", RecordingRequest)
    grant = protocol.complete_google_oauth_authorization(
        attempt,
        callback,
        token_transport=transport,
        certificate_transport=transport,
    )
    assert grant.identity.principal_ref == derive_google_oidc_principal_ref(
        GOOGLE_OIDC_CANONICAL_ISSUER, "google-subject-123"
    )
    assert grant.requested_scopes == ("openid", _SCOPE)
    assert grant.returned_scopes == ("openid", _SCOPE)
    assert transport.forms == [
        (
            ("client_id", _CLIENT_REF),
            ("code", "private-code"),
            ("code_verifier", attempt.code_verifier),
            ("grant_type", "authorization_code"),
            ("redirect_uri", _REDIRECT_URI),
        )
    ]
    assert seen == [(GOOGLE_OAUTH_CERTS_ENDPOINT, "GET", None, None)]
    assert transport.certificate_reads == 1
    assert "access-token" not in repr(grant)
    assert "refresh-token" not in repr(grant)


def test_initial_response_scope_omission_of_openid_is_recorded_without_inventing_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    id_token, certs = _signed_token(nonce=attempt.nonce)
    transport = _FakeTransport(_initial_response(id_token, scope=_SCOPE), certs)
    grant = protocol.complete_google_oauth_authorization(
        attempt, callback, token_transport=transport, certificate_transport=transport
    )
    assert grant.requested_scopes == ("openid", _SCOPE)
    assert grant.returned_scopes == (_SCOPE,)


@pytest.mark.parametrize("value", [True, 3600.0, "3600"])
def test_initial_response_rejects_noninteger_expiry_without_laundering(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    id_token, certs = _signed_token(nonce=attempt.nonce)
    transport = _FakeTransport(_initial_response(id_token, expires_in=value), certs)
    with pytest.raises(GoogleOAuthTransportError) as raised:
        protocol.complete_google_oauth_authorization(
            attempt, callback, token_transport=transport, certificate_transport=transport
        )
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.UNCERTAIN
    assert raised.value.code == "GOOGLE_OAUTH_TOKEN_RESPONSE_UNSUPPORTED"


def test_html_or_nonjson_token_body_cannot_become_a_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    _, certs = _signed_token(nonce=attempt.nonce)
    transport = _FakeTransport(b"<html>private provider response</html>", certs)
    with pytest.raises(GoogleOAuthTransportError) as raised:
        protocol.complete_google_oauth_authorization(
            attempt, callback, token_transport=transport, certificate_transport=transport
        )
    assert raised.value.code == "GOOGLE_OAUTH_TOKEN_RESPONSE_UNSUPPORTED"
    assert "private provider response" not in str(raised.value)


@pytest.mark.parametrize(
    "optional_field",
    ["scope", "id_token", "refresh_token", "refresh_token_expires_in"],
)
def test_refresh_rejects_explicit_null_optional_fields(
    monkeypatch: pytest.MonkeyPatch, optional_field: str
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    initial_token, certs = _signed_token(nonce=attempt.nonce)
    initial_transport = _FakeTransport(_initial_response(initial_token), certs)
    initial = protocol.complete_google_oauth_authorization(
        attempt,
        callback,
        token_transport=initial_transport,
        certificate_transport=initial_transport,
    )
    body: dict[str, object] = {
        "access_token": "new-access-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        optional_field: None,
    }
    refreshed = _FakeTransport(json.dumps(body).encode("utf-8"), certs)
    with pytest.raises(GoogleOAuthTransportError) as raised:
        refresh_google_oauth_grant(
            _target(),
            initial.identity,
            refresh_token=initial.refresh_token,
            token_transport=refreshed,
            certificate_transport=refreshed,
        )
    assert raised.value.code == "GOOGLE_OAUTH_TOKEN_RESPONSE_UNSUPPORTED"


def test_refresh_id_token_can_only_reverify_the_sealed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    initial_token, certs = _signed_token(nonce=attempt.nonce)
    initial_transport = _FakeTransport(_initial_response(initial_token), certs)
    initial = protocol.complete_google_oauth_authorization(
        attempt,
        callback,
        token_transport=initial_transport,
        certificate_transport=initial_transport,
    )
    refresh_token, refresh_certs = _signed_token(nonce=None)
    refreshed = _FakeTransport(
        json.dumps(
            {
                "access_token": "new-access-token",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": _SCOPE,
                "id_token": refresh_token,
            }
        ).encode("utf-8"),
        refresh_certs,
    )
    result = refresh_google_oauth_grant(
        _target(),
        initial.identity,
        refresh_token=initial.refresh_token,
        token_transport=refreshed,
        certificate_transport=refreshed,
    )
    assert result.identity_reverified is True
    assert result.returned_scopes == (_SCOPE,)
    assert not hasattr(result, "identity")


def test_refresh_without_id_token_makes_no_new_identity_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    initial_token, certs = _signed_token(nonce=attempt.nonce)
    initial_transport = _FakeTransport(_initial_response(initial_token), certs)
    initial = protocol.complete_google_oauth_authorization(
        attempt,
        callback,
        token_transport=initial_transport,
        certificate_transport=initial_transport,
    )
    refreshed = _FakeTransport(
        json.dumps(
            {"access_token": "new-access-token", "expires_in": 3600, "token_type": "Bearer"}
        ).encode("utf-8"),
        certs,
    )
    result = refresh_google_oauth_grant(
        _target(),
        initial.identity,
        refresh_token=initial.refresh_token,
        token_transport=refreshed,
        certificate_transport=refreshed,
    )
    assert result.identity_reverified is False
    assert result.returned_scopes is None
    assert refreshed.certificate_reads == 0


def test_revalidation_rejects_constructed_private_grant_type_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt(monkeypatch)
    callback = consume_google_oauth_callback(attempt, f"code=private-code&state={attempt.state}")
    id_token, certs = _signed_token(nonce=attempt.nonce)
    transport = _FakeTransport(_initial_response(id_token), certs)
    grant = protocol.complete_google_oauth_authorization(
        attempt, callback, token_transport=transport, certificate_transport=transport
    )
    forged = object.__new__(type(grant))
    for field in fields(grant):
        object.__setattr__(
            forged,
            field.name,
            True if field.name == "refresh_token_expires_at" else getattr(grant, field.name),
        )
    with pytest.raises(AdhelmError) as raised:
        revalidate_google_oauth_initial_grant(forged)
    assert raised.value.code == "GOOGLE_OAUTH_GRANT_INVALID"

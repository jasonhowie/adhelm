"""Independent adversarial review of the protected Google OAuth lifecycle.

These fake-store tests never call a browser, keychain, Google endpoint, or
certificate service.  They exercise the public lifecycle through its bounded
local test seams.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from adhelm import credential_setup, credentials
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
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
)
from adhelm.models import Configuration, Profile

_NOW = datetime(2029, 1, 1, tzinfo=UTC)
_SCOPE = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4", "READ")]


@dataclass
class _MemoryStore:
    values: dict[str, str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        del service_name
        return self.values.get(username)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        del service_name
        self.values[username] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del service_name
        self.values.pop(username, None)


@dataclass
class _ServiceMemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile() -> Profile:
    subject = "independent-review-subject"
    return Profile(
        profile_id="independent-ga4-read",
        provider="ga4",
        account_ref="12345",
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, subject),
        client_ref="independent_client.apps.googleusercontent.com",
        scopes=[_SCOPE],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_source="OS_OAUTH_GOOGLE",
        credential_ref="independent-ga4-primary",
    )


def _configuration(profile: Profile) -> Configuration:
    return Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})


def _grant(
    profile: Profile,
    *,
    expires_at: datetime = _NOW + timedelta(seconds=30),
    refresh_expires_at: datetime | None = None,
) -> GoogleOAuthInitialGrant:
    identity = VerifiedGoogleOidcIdentity(
        canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
        raw_subject="independent-review-subject",
        principal_ref=profile.principal_ref,
        client_ref=profile.client_ref,
    )
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, _SCOPE),
        access_token="independent-access-token",
        expires_at=expires_at,
        refresh_token="independent-refresh-token",
        refresh_token_expires_at=refresh_expires_at,
        requested_scopes=("openid", _SCOPE),
        returned_scopes=(_SCOPE,),
        identity=identity,
    )


def _wire_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    store: _MemoryStore,
    *,
    grant: GoogleOAuthInitialGrant | None = None,
) -> tuple[Profile, Configuration]:
    _wire_store(monkeypatch, store)
    profile = _profile()
    configuration = _configuration(profile)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile) if grant is None else grant,
        client_secret=None,
        expected_slot=slot,
    )
    return profile, configuration


def _state(store: _MemoryStore, profile: Profile) -> str:
    return str(json.loads(store.values[profile.credential_ref])["state"])


def test_opaque_install_snapshot_is_hmac_bound_and_cannot_overwrite_changed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore(values={"independent-ga4-primary": "private-old-slot"})
    _wire_store(monkeypatch, store)
    profile = _profile()
    configuration = _configuration(profile)
    snapshot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=True
    )
    assert "private-old-slot" not in repr(snapshot)
    assert "private-old-slot" not in snapshot._expected_value_hmac

    store.values[profile.credential_ref] = "private-replaced-slot"
    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            _grant(profile),
            client_secret=None,
            expected_slot=snapshot,
            replace=True,
        )
    assert raised.value.code == "CREDENTIAL_CONCURRENT_CHANGE"
    assert store.values[profile.credential_ref] == "private-replaced-slot"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("state", True),
        ("refresh_started_at", True),
        ("returned_scopes", [True]),
    ],
)
def test_private_bundle_codec_rejects_original_boolean_types_before_refresh(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    store = _MemoryStore()
    profile, _ = _install(monkeypatch, store)
    raw = json.loads(store.values[profile.credential_ref])
    raw[field] = value
    store.values[profile.credential_ref] = canonical_bytes(raw).decode("utf-8")
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("invalid local bundle reached token dispatch"),
    )

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert raised.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"


def test_post_dispatch_identity_verification_failure_persists_reconnect_and_blocks_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    profile, _ = _install(monkeypatch, store)
    calls = 0

    def certificate_style_failure(_: object) -> object:
        nonlocal calls
        calls += 1
        raise AdhelmError(
            "GOOGLE_OIDC_CERTIFICATES_UNAVAILABLE",
            "Google identity verification certificates are unavailable.",
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", certificate_style_failure)
    with pytest.raises(AdhelmError) as first:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert first.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert _state(store, profile) == "RECONNECT_REQUIRED"

    with pytest.raises(AdhelmError) as second:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert second.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert calls == 1


def test_only_proven_pre_dispatch_failure_restores_ready_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    profile, _ = _install(monkeypatch, store)

    def pre_dispatch_failure(_: object) -> object:
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_TRANSPORT_CLOCK_UNAVAILABLE",
            "Google OAuth token exchange cannot start with the local transport clock unavailable.",
            dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", pre_dispatch_failure)
    with pytest.raises(GoogleOAuthTransportError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert raised.value.dispatch_state is GoogleOAuthTokenDispatchState.NOT_DISPATCHED
    assert _state(store, profile) == "READY"


def test_initial_grant_with_expired_time_bound_refresh_token_never_reaches_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    profile = _profile()
    configuration = _configuration(profile)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            _grant(profile, expires_at=_NOW + timedelta(hours=1), refresh_expires_at=_NOW),
            client_secret=None,
            expected_slot=slot,
        )
    assert raised.value.code == "GOOGLE_OAUTH_GRANT_EXPIRED"
    assert store.values == {}


def test_generic_os_store_profile_cannot_read_oauth_bundle_from_same_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different workspace/source must never receive a sealed bundle as a bearer."""
    store = _ServiceMemoryStore()
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)
    oauth_profile = _profile()
    configuration = _configuration(oauth_profile)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, oauth_profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        oauth_profile.profile_id,
        _grant(oauth_profile),
        client_secret=None,
        expected_slot=slot,
    )
    generic_profile = Profile(
        profile_id="other-workspace-ga4-read",
        provider="ga4",
        account_ref="12345",
        principal_ref="other-workspace-principal",
        client_ref="other-workspace-client",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_source="OS_STORE",
        credential_ref=oauth_profile.credential_ref,
    )

    with pytest.raises(AdhelmError) as raised:
        credentials.credential(generic_profile)
    assert raised.value.code == "AUTHENTICATION_REQUIRED"


def test_oauth_status_and_source_aware_local_remove_use_the_sealed_bundle_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ServiceMemoryStore()
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)
    profile = _profile()
    configuration = _configuration(profile)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile),
        client_secret=None,
        expected_slot=slot,
    )

    status = credential_setup.credential_status(configuration, profile.profile_id)
    assert len(status) == 1
    assert status[0].state == "PRESENT"
    removed = credential_setup.local_remove(configuration, profile.profile_id, "PRIMARY")
    assert removed.operation == "LOCAL_REMOVE_ONLY"
    assert removed.state == "LOCAL_REMOVED"
    status_after_remove = credential_setup.credential_status(configuration, profile.profile_id)
    assert status_after_remove[0].state == "MISSING"


def test_generic_local_remove_cannot_delete_oauth_bundle_from_same_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ServiceMemoryStore()
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)
    oauth_profile = _profile()
    oauth_configuration = _configuration(oauth_profile)
    slot = oauth_credentials.prepare_google_oauth_install(
        oauth_configuration, oauth_profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        oauth_configuration,
        oauth_profile.profile_id,
        _grant(oauth_profile, expires_at=_NOW + timedelta(hours=1)),
        client_secret=None,
        expected_slot=slot,
    )
    generic_profile = Profile(
        profile_id="other-workspace-ga4-read",
        provider="ga4",
        account_ref="12345",
        principal_ref="other-workspace-principal",
        client_ref="other-workspace-client",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_source="OS_STORE",
        credential_ref=oauth_profile.credential_ref,
    )
    generic_configuration = _configuration(generic_profile)

    removed = credential_setup.local_remove(
        generic_configuration, generic_profile.profile_id, "PRIMARY"
    )
    assert removed.state == "LOCAL_REMOVED"
    assert (
        oauth_credentials.resolve_google_oauth_access_token(oauth_profile)
        == "independent-access-token"
    )


def test_generic_same_reference_secret_is_not_adopted_as_an_oauth_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ServiceMemoryStore(values={("adhelm", "independent-ga4-primary"): "generic-secret"})
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    profile = _profile()
    configuration = _configuration(profile)

    status = credential_setup.credential_status(configuration, profile.profile_id)
    assert status[0].state == "MISSING"
    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert raised.value.code == "AUTHENTICATION_REQUIRED"


def test_expired_time_bound_refresh_token_blocks_before_any_refresh_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])
    profile = _profile()
    configuration = _configuration(profile)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(
            profile,
            expires_at=_NOW + timedelta(seconds=30),
            refresh_expires_at=_NOW + timedelta(seconds=1),
        ),
        client_secret=None,
        expected_slot=slot,
    )
    current[0] = _NOW + timedelta(seconds=1)
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("expired refresh token reached dispatch"),
    )

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert raised.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert _state(store, profile) == "RECONNECT_REQUIRED"


def test_expired_rotated_refresh_token_after_dispatch_never_returns_or_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    profile, _ = _install(monkeypatch, store)
    calls = 0

    def expired_rotation(_: object) -> GoogleOAuthRefreshGrant:
        nonlocal calls
        calls += 1
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(profile.client_ref, _SCOPE),
            access_token="independent-rotated-access-token",
            expires_at=_NOW + timedelta(hours=1),
            returned_scopes=None,
            identity_reverified=False,
            rotated_refresh_token="independent-rotated-refresh-token",
            rotated_refresh_token_expires_at=_NOW,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", expired_rotation)
    with pytest.raises(AdhelmError) as first:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert first.value.code == "GOOGLE_OAUTH_REFRESH_RESULT_INVALID"
    assert _state(store, profile) != "READY"

    with pytest.raises(AdhelmError) as second:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert second.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert calls == 1

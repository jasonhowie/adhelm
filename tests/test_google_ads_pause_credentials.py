"""Fake-only tests for Google Ads PAUSE three-slot planning continuity."""

from __future__ import annotations

import json
import pickle
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.models import (
    GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
    Configuration,
    Profile,
)

_NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
_CUSTOMER = "1234567890"
_LOGIN = "0987654321"
_CLIENT = "m56-google-ads.apps.googleusercontent.com"
_SUBJECT = "m56-google-ads-subject"
_ACCESS = "m56-google-ads-access-token"
_DEVELOPER = "m56-google-ads-developer-token"
_CONFIGURATION_DIGEST = "a" * 64
_GENERIC_SERVICE = "fake-generic-service"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads.append((service_name, username))
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes.append((service_name, username, password))
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile(**updates: object) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "EDIT")]
    values: dict[str, object] = {
        "profile_id": "google-ads-pause-edit",
        "provider": "google_ads",
        "account_ref": _CUSTOMER,
        "principal_ref": derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        "client_ref": _CLIENT,
        "scopes": [scope],
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_OAUTH_GOOGLE",
        "credential_ref": "google-ads-pause-oauth",
        "secondary_credential_ref": "google-ads-pause-developer",
        "login_customer_ref": _LOGIN,
    }
    values.update(updates)
    return Profile.model_validate(values)


def _grant(profile: Profile, *, expires_at: datetime) -> GoogleOAuthInitialGrant:
    scope = profile.scopes[0]
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, scope),
        access_token=_ACCESS,
        expires_at=expires_at,
        refresh_token="m56-google-ads-refresh-token",
        refresh_token_expires_at=_NOW + timedelta(days=1),
        requested_scopes=("openid", scope),
        returned_scopes=(scope,),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=_SUBJECT,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _wire(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore, current: list[datetime]) -> None:
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials,
        "_generic_credential_store",
        lambda: (store, _GENERIC_SERVICE),
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])


def _installed(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime | None = None,
) -> tuple[_MemoryStore, Profile, list[datetime]]:
    current = [_NOW]
    store = _MemoryStore()
    _wire(monkeypatch, store, current)
    profile = _profile()
    configuration = Configuration(profiles=[profile])
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile, expires_at=expires_at or _NOW + timedelta(minutes=10)),
        client_secret="m56-google-client-secret",
        expected_slot=slot,
    )
    assert profile.secondary_credential_ref is not None
    store.values[(_GENERIC_SERVICE, profile.secondary_credential_ref)] = _DEVELOPER
    store.reads.clear()
    store.writes.clear()
    return store, profile, current


def _prepare(profile: Profile) -> oauth_credentials._GoogleAdsPauseProtectedSnapshot:
    return oauth_credentials._prepare_google_ads_pause_protected_snapshot(
        profile, _CONFIGURATION_DIGEST
    )


def test_prepare_uses_dedicated_key_and_exposes_only_detached_nonsecret_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    monkeypatch.setattr(secrets, "token_hex", lambda count: "b" * (count * 2))

    snapshot = _prepare(profile)

    assert snapshot.profile_ref == profile.profile_id
    assert snapshot.profile_configuration_digest == digest(profile)
    assert snapshot.account_client_ref == _CUSTOMER
    assert snapshot.login_customer_client_ref == _LOGIN
    assert snapshot.required_scope == "https://www.googleapis.com/auth/adwords"
    assert snapshot.credential_binding_ref.startswith("credential-binding:sha256:")
    assert store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] == "b" * 64
    rendered = repr(snapshot)
    for secret in (_ACCESS, _DEVELOPER, "m56-google-ads-refresh-token", "m56-google-client-secret"):
        assert secret not in rendered
    with pytest.raises(TypeError):
        pickle.dumps(snapshot)
    with pytest.raises(TypeError):
        vars(snapshot)
    with pytest.raises(TypeError):
        json.dumps(snapshot)


def test_current_is_no_refresh_no_write_and_keeps_original_binding_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, current = _installed(monkeypatch)
    expected = _prepare(profile)
    writes_before = len(store.writes)
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("current PAUSE continuity must never refresh OAuth"),
    )

    current[0] += timedelta(seconds=1)
    observed = oauth_credentials._current_google_ads_pause_protected_snapshot(
        profile, _CONFIGURATION_DIGEST, expected
    )

    assert observed.credential_binding_ref == expected.credential_binding_ref
    assert observed._resolved_at >= expected._resolved_at
    assert len(store.writes) == writes_before


@pytest.mark.parametrize("field", ("refresh_token", "client_secret"))
def test_complete_canonical_oauth_bundle_drift_changes_the_pause_binding(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    store, profile, _current = _installed(monkeypatch)
    expected = _prepare(profile)
    bundle = oauth_credentials._decode_bundle(
        store.values[(oauth_credentials._SERVICE, profile.credential_ref)]
    )
    replacement = "m56-rotated-refresh-token" if field == "refresh_token" else "m56-rotated-client"
    changed_bundle = (
        replace(bundle, refresh_token=replacement)
        if field == "refresh_token"
        else replace(bundle, client_secret=replacement)
    )
    oauth_credentials._write_bundle(
        store,
        profile.credential_ref,
        changed_bundle,
    )
    writes_before = len(store.writes)

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials._current_google_ads_pause_protected_snapshot(
            profile, _CONFIGURATION_DIGEST, expected
        )

    assert raised.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    assert len(store.writes) == writes_before
    assert replacement not in str(raised.value)


def test_developer_or_key_rotation_is_detected_without_repair_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    expected = _prepare(profile)
    assert profile.secondary_credential_ref is not None

    for reference, replacement in (
        (profile.secondary_credential_ref, "m56-rotated-developer"),
        (GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE, "c" * 64),
    ):
        store.values[(_GENERIC_SERVICE, reference)] = replacement
        writes_before = len(store.writes)
        with pytest.raises(AdhelmError) as raised:
            oauth_credentials._current_google_ads_pause_protected_snapshot(
                profile, _CONFIGURATION_DIGEST, expected
            )
        assert raised.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
        assert len(store.writes) == writes_before
        store.values[(_GENERIC_SERVICE, reference)] = (
            _DEVELOPER if reference == profile.secondary_credential_ref else "b" * 64
        )


def test_current_refuses_expired_bundle_without_refresh_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, current = _installed(monkeypatch)
    expected = _prepare(profile)
    bundle = oauth_credentials._decode_bundle(
        store.values[(oauth_credentials._SERVICE, profile.credential_ref)]
    )
    oauth_credentials._write_bundle(
        store,
        profile.credential_ref,
        replace(bundle, access_token_expires_at=current[0]),
    )
    writes_before = len(store.writes)
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("expired retention check must not refresh OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials._current_google_ads_pause_protected_snapshot(
            profile, _CONFIGURATION_DIGEST, expected
        )

    assert raised.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    assert len(store.writes) == writes_before


def test_current_treats_expiry_between_trusted_samples_as_credential_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    expected = _prepare(profile)
    bundle = oauth_credentials._decode_bundle(
        store.values[(oauth_credentials._SERVICE, profile.credential_ref)]
    )
    writes_before = len(store.writes)
    samples = iter(
        (
            bundle.access_token_expires_at - timedelta(seconds=1),
            bundle.access_token_expires_at,
        )
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(samples))
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("no-refresh PAUSE check must not refresh OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials._current_google_ads_pause_protected_snapshot(
            profile, _CONFIGURATION_DIGEST, expected
        )

    assert raised.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    assert len(store.writes) == writes_before


def test_invalid_or_mismatched_expected_binding_stops_before_slot_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    expected = _prepare(profile)
    reads_before = len(store.reads)

    forged = object.__new__(oauth_credentials._GoogleAdsPauseProtectedSnapshot)
    with pytest.raises(AdhelmError) as malformed:
        oauth_credentials._current_google_ads_pause_protected_snapshot(
            profile, _CONFIGURATION_DIGEST, forged
        )
    assert malformed.value.code == "GOOGLE_ADS_PAUSE_PROTECTED_SNAPSHOT_INVALID"
    assert len(store.reads) == reads_before

    with pytest.raises(AdhelmError) as stale_configuration:
        oauth_credentials._current_google_ads_pause_protected_snapshot(profile, "d" * 64, expected)
    assert stale_configuration.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    assert len(store.reads) == reads_before


def test_retention_holds_sorted_three_slots_and_callback_rereads_each_held_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    expected = _prepare(profile)
    acquired: list[str] = []

    @contextmanager
    def tracked_lock(reference: str) -> Iterator[None]:
        acquired.append(reference)
        yield

    monkeypatch.setattr(oauth_credentials, "credential_mutation_lock", tracked_lock)
    escaped: list[object] = []
    with oauth_credentials._google_ads_pause_retention_recheck(
        profile, _CONFIGURATION_DIGEST, expected
    ) as recheck:
        escaped.append(recheck)
        reads_before = len(store.reads)
        recheck()
        recheck()
        assert len(store.reads) == reads_before + 6

    assert acquired == sorted(acquired, key=str.casefold)
    assert acquired == [
        GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
        profile.secondary_credential_ref,
        profile.credential_ref,
    ]
    assert callable(escaped[0])
    with pytest.raises(AdhelmError) as expired_callback:
        escaped[0]()
    assert expired_callback.value.code == "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED"


def test_retention_callback_rereads_captured_handles_and_rejects_late_slot_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    expected = _prepare(profile)
    assert profile.secondary_credential_ref is not None
    writes_before = len(store.writes)

    with oauth_credentials._google_ads_pause_retention_recheck(
        profile, _CONFIGURATION_DIGEST, expected
    ) as recheck:
        # The context has captured both backend handles.  A callback must not
        # acquire them again while the caller may be inside ledger append.
        monkeypatch.setattr(
            oauth_credentials,
            "_credential_backend",
            lambda: pytest.fail("retention callback acquired an OAuth backend"),
        )
        monkeypatch.setattr(
            oauth_credentials,
            "_generic_credential_store",
            lambda: pytest.fail("retention callback acquired a generic backend"),
        )
        recheck()
        store.values[(_GENERIC_SERVICE, profile.secondary_credential_ref)] = (
            "m56-late-rotated-developer"
        )
        with pytest.raises(AdhelmError) as drift:
            recheck()

    assert drift.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    assert len(store.writes) == writes_before


def test_retention_requires_callback_and_rejects_clock_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, current = _installed(monkeypatch)
    expected = _prepare(profile)

    with pytest.raises(AdhelmError) as missing_callback:
        with oauth_credentials._google_ads_pause_retention_recheck(
            profile, _CONFIGURATION_DIGEST, expected
        ):
            pass
    assert missing_callback.value.code == "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED"

    with oauth_credentials._google_ads_pause_retention_recheck(
        profile, _CONFIGURATION_DIGEST, expected
    ) as recheck:
        recheck()
        current[0] -= timedelta(seconds=1)
        with pytest.raises(AdhelmError) as rollback:
            recheck()
    assert rollback.value.code == "CLOCK_INVALID"


def test_retention_clock_floor_advances_after_each_held_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, current = _installed(monkeypatch)
    expected = _prepare(profile)
    writes_before = len(store.writes)
    with oauth_credentials._google_ads_pause_retention_recheck(
        profile, _CONFIGURATION_DIGEST, expected
    ) as recheck:
        current[0] = _NOW + timedelta(seconds=120)
        recheck()
        current[0] = _NOW + timedelta(seconds=60)
        with pytest.raises(AdhelmError) as raised:
            recheck()
    assert raised.value.code == "CLOCK_INVALID"
    assert len(store.writes) == writes_before

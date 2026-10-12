"""Local-only Google OAuth profile and bundle lifecycle regressions."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm import credential_setup, credentials
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.config import load_configuration, save_configuration
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
)
from adhelm.models import (
    Configuration,
    Profile,
    validate_configuration,
    validate_configured_profile,
)


def _legacy_profile() -> Profile:
    return Profile(
        profile_id="legacy-google-read",
        provider="google_ads",
        account_ref="1234567890",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="legacy-google-token",
    )


@pytest.mark.parametrize("invalid", [True, 1.0, "1"])
def test_profile_and_configuration_schema_versions_require_exact_integer_one(
    invalid: object,
) -> None:
    profile = _legacy_profile()
    raw_profile = profile.model_dump(mode="python")
    raw_profile["schema_version"] = invalid
    with pytest.raises(ValidationError):
        Profile.model_validate(raw_profile)

    forged_profile = profile.model_copy(update={"schema_version": invalid})
    with pytest.raises(ValidationError):
        validate_configured_profile(forged_profile)

    raw_configuration = {"schema_version": invalid, "profiles": [profile.model_dump()]}
    with pytest.raises(ValidationError):
        Configuration.model_validate(raw_configuration)

    forged_configuration = Configuration.model_construct(schema_version=invalid, profiles=[profile])
    with pytest.raises(ValidationError):
        validate_configuration(forged_configuration)


def _oauth_profile(provider: str = "google_ads", *, account_ref: str | None = None) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[(provider, "READ")]
    return Profile(
        profile_id=f"oauth-{provider}",
        provider=provider,
        account_ref=account_ref or ("1234567890" if provider == "google_ads" else "12345"),
        principal_ref=derive_google_oidc_principal_ref(
            "https://accounts.google.com", "fixture-subject"
        ),
        client_ref="fixture_client.apps.googleusercontent.com",
        scopes=[scope],
        purpose="READ",
        currency="USD" if provider in {"google_ads", "ga4", "ga4_admin"} else None,
        timezone="UTC",
        credential_source="OS_OAUTH_GOOGLE",
        credential_ref=f"oauth-{provider}-primary",
    )


@pytest.mark.parametrize(
    ("provider", "account_ref"),
    [
        ("google_ads", "1234567890"),
        ("ga4", "12345"),
        ("ga4_admin", "12345"),
        ("gtm", "12345"),
    ],
)
def test_google_oauth_profile_requires_one_exact_supported_read_target(
    provider: str, account_ref: str
) -> None:
    profile = _oauth_profile(provider, account_ref=account_ref)
    assert profile.scopes == [GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[(provider, "READ")]]
    assert profile.credential_source == "OS_OAUTH_GOOGLE"


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "ACTIVATE"},
        {"scopes": ["openid"]},
        {"scopes": ["email"]},
        {"scopes": ["profile"]},
        {"scopes": ["https://www.googleapis.com/auth/adwords", "openid"]},
        {"scopes": ["openid", "https://www.googleapis.com/auth/adwords"]},
        {"scopes": ["https://www.googleapis.com/auth/adwords"] * 2},
        {"client_ref": "client.fixture"},
        {"principal_ref": "principal.fixture"},
        {"account_ref": "12345"},
        {"timezone": "not/an-iana-zone"},
    ],
)
def test_google_oauth_profile_rejects_unapproved_scope_or_identity_shapes(
    changes: dict[str, object],
) -> None:
    raw = _oauth_profile().model_dump(mode="python")
    raw.update(changes)
    with pytest.raises(ValidationError):
        Profile.model_validate(raw)


def test_google_oauth_profile_requires_ga4_currency_declaration() -> None:
    raw = _oauth_profile("ga4", account_ref="12345").model_dump(mode="python")
    raw["currency"] = None
    with pytest.raises(ValidationError):
        Profile.model_validate(raw)


def test_google_oauth_profile_revalidation_rejects_forged_model_copy() -> None:
    profile = _oauth_profile()
    forged = profile.model_copy(update={"scopes": ["openid"]})
    with pytest.raises(ValidationError):
        validate_configured_profile(forged)


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    drop_writes: bool = False
    get_calls: list[tuple[str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.get_calls.append((service_name, username))
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if not self.drop_writes:
            self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)

    def value(self, service_name: str, username: str) -> str:
        return self.values[(service_name, username)]


@dataclass
class _CompletedReadbackFailureStore(_MemoryStore):
    _fail_completed_readback: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        if self._fail_completed_readback:
            self._fail_completed_readback = False
            raise RuntimeError("private backend read failure")
        return super().get_password(service_name, username)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        super().set_password(service_name, username, password)
        if (
            service_name == oauth_credentials._SERVICE
            and '"access_token":"completed-readback-failure-token"' in password
            and '"state":"READY"' in password
        ):
            self._fail_completed_readback = True


def _oauth_value(store: _MemoryStore, profile: Profile) -> str:
    return store.value(oauth_credentials._SERVICE, profile.credential_ref)


def _oauth_grant(
    profile: Profile,
    *,
    access_token: str = "fixture-access-token",
    expires_at: datetime | None = None,
) -> GoogleOAuthInitialGrant:
    target = GoogleOAuthTarget(profile.client_ref, profile.scopes[0])
    identity = VerifiedGoogleOidcIdentity(
        canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
        raw_subject="fixture-subject",
        principal_ref=profile.principal_ref,
        client_ref=profile.client_ref,
    )
    return GoogleOAuthInitialGrant(
        target=target,
        access_token=access_token,
        expires_at=expires_at or datetime(2030, 1, 1, tzinfo=UTC),
        refresh_token="fixture-refresh-token",
        refresh_token_expires_at=None,
        requested_scopes=("openid", profile.scopes[0]),
        returned_scopes=(profile.scopes[0],),
        identity=identity,
    )


def _oauth_configuration(profile: Profile) -> Configuration:
    return Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})


def _fake_local_bundle_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )


def test_reconnect_allows_an_expected_missing_slot_but_never_resurrects_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    grant = _oauth_grant(profile)
    monkeypatch.setattr(
        oauth_credentials,
        "_utc_now",
        lambda: datetime(2029, 1, 1, tzinfo=UTC),
    )

    missing = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=True
    )
    result = oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        grant,
        client_secret=None,
        expected_slot=missing,
        replace=True,
    )
    assert result.operation == "RECONNECT"
    assert store.values

    generic = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=True)
    with pytest.raises(AdhelmError) as bypass:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            grant,
            client_secret=None,
            expected_slot=generic,
            replace=True,
        )
    assert bypass.value.code == "GOOGLE_OAUTH_INSTALL_SNAPSHOT_INVALID"

    expected_present = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=True
    )
    store.values.clear()
    with pytest.raises(AdhelmError) as failure:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            grant,
            client_secret=None,
            expected_slot=expected_present,
            replace=True,
        )
    assert failure.value.code == "CREDENTIAL_CONCURRENT_CHANGE"
    assert store.values == {}


def test_oauth_primary_routes_to_sealed_bundle_and_source_aware_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    now = datetime(2029, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now)
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _oauth_grant(profile),
        client_secret=None,
        expected_slot=slot,
    )

    assert credentials.credential(profile) == "fixture-access-token"
    assert next(iter(store.values.values())) != "fixture-access-token"
    removed = credential_setup.local_remove(configuration, profile.profile_id)
    assert removed.operation == "LOCAL_REMOVE_ONLY"
    assert store.values == {}


def test_generic_os_store_credential_cannot_read_a_sealed_oauth_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=UTC))
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _oauth_grant(profile),
        client_secret=None,
        expected_slot=slot,
    )
    sealed = _oauth_value(store, profile)
    forged_os_store = _legacy_profile().model_copy(
        update={"profile_id": "generic-same-ref", "credential_ref": profile.credential_ref}
    )

    store.get_calls.clear()
    with pytest.raises(AdhelmError) as failure:
        credentials.credential(forged_os_store)

    assert failure.value.code == "AUTHENTICATION_REQUIRED"
    assert sealed not in str(failure.value)
    assert store.get_calls == [("adhelm", profile.credential_ref)]


def test_oauth_status_and_remove_use_only_the_dedicated_bundle_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=UTC))
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _oauth_grant(profile),
        client_secret=None,
        expected_slot=slot,
    )

    store.get_calls.clear()
    assert (
        credential_setup.credential_status(configuration, profile.profile_id)[0].state == "PRESENT"
    )
    assert store.get_calls == [(oauth_credentials._SERVICE, profile.credential_ref)]
    removed = credential_setup.local_remove(configuration, profile.profile_id)
    assert removed.state == "LOCAL_REMOVED"
    assert store.values == {}
    assert (
        credential_setup.credential_status(configuration, profile.profile_id)[0].state == "MISSING"
    )


def test_install_snapshot_never_exposes_the_raw_protected_slot_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore(
        values={(oauth_credentials._SERVICE, "oauth-ga4-primary"): "private-sealed-bundle-sentinel"}
    )
    _fake_local_bundle_store(monkeypatch, store)
    slot = oauth_credentials.prepare_google_oauth_slot("oauth-ga4-primary", replace=True)
    assert not hasattr(slot, "_expected_value")
    assert "private-sealed-bundle-sentinel" not in repr(slot)
    assert "private-sealed-bundle-sentinel" not in str(slot._expected_value_hmac)
    with pytest.raises(TypeError):
        vars(slot)


def test_constructed_install_snapshot_fails_before_bundle_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    empty = object.__new__(oauth_credentials.GoogleOAuthInstallSlot)
    with pytest.raises(AdhelmError) as empty_failure:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            _oauth_grant(profile),
            client_secret=None,
            expected_slot=empty,
        )
    assert empty_failure.value.code == "GOOGLE_OAUTH_INSTALL_SNAPSHOT_INVALID"

    malformed = object.__new__(oauth_credentials.GoogleOAuthInstallSlot)
    object.__setattr__(malformed, "_reference", profile.credential_ref)
    object.__setattr__(malformed, "_expected_value_hmac", "a" * 64)
    object.__setattr__(malformed, "_replace", False)
    object.__setattr__(malformed, "_configuration_digest", None)
    object.__setattr__(malformed, "_profile_id", None)
    object.__setattr__(malformed, "_issuer", object())
    with pytest.raises(AdhelmError) as failure:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            _oauth_grant(profile),
            client_secret=None,
            expected_slot=malformed,
        )
    assert failure.value.code == "GOOGLE_OAUTH_INSTALL_SNAPSHOT_INVALID"
    assert store.values == {}


def test_final_install_clock_check_blocks_expired_result_after_verified_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    issued = datetime(2029, 1, 1, tzinfo=UTC)
    expires = issued + timedelta(seconds=5)
    samples = iter((issued, expires))
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(samples))
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    with pytest.raises(AdhelmError) as failure:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            profile.profile_id,
            _oauth_grant(profile, expires_at=expires),
            client_secret=None,
            expected_slot=slot,
        )
    assert failure.value.code == "GOOGLE_OAUTH_GRANT_EXPIRED"
    assert '"state":"RECONNECT_REQUIRED"' in next(iter(store.values.values()))


def test_refresh_return_clock_check_blocks_token_after_durable_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    issued = datetime(2029, 1, 1, tzinfo=UTC)
    initial_expiry = issued + timedelta(seconds=30)
    refreshed_expiry = issued + timedelta(seconds=120)
    # install-before, install-after, cached/refresh decision, pre-ready-write,
    # final return check.  The final sample has crossed the fresh token expiry.
    samples = iter((issued, issued, issued, issued + timedelta(seconds=1), refreshed_expiry))
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(samples))
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _oauth_grant(profile, expires_at=initial_expiry),
        client_secret=None,
        expected_slot=slot,
    )

    def refreshed(_bundle: object) -> GoogleOAuthRefreshGrant:
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(profile.client_ref, profile.scopes[0]),
            access_token="fresh-access-token",
            expires_at=refreshed_expiry,
            returned_scopes=None,
            identity_reverified=False,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", refreshed)
    with pytest.raises(AdhelmError) as failure:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert failure.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert '"state":"RECONNECT_REQUIRED"' in next(iter(store.values.values()))


def test_unverified_completed_ready_write_is_blocked_before_later_automatic_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2029, 1, 1, tzinfo=UTC)
    store = _CompletedReadbackFailureStore()
    profile = _oauth_profile("ga4", account_ref="12345")
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now)
    _install_ready_bundle(monkeypatch, store, profile, expires_at=now + timedelta(seconds=30))
    attempts: list[object] = []

    def refreshed(_bundle: object) -> GoogleOAuthRefreshGrant:
        attempts.append(_bundle)
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(profile.client_ref, profile.scopes[0]),
            access_token="completed-readback-failure-token",
            expires_at=now + timedelta(hours=1),
            returned_scopes=None,
            identity_reverified=False,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", refreshed)
    with pytest.raises(AdhelmError) as first:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert first.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert '"state":"RECONNECT_REQUIRED"' in _oauth_value(store, profile)
    with pytest.raises(AdhelmError) as second:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert second.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert len(attempts) == 1


def _install_ready_bundle(
    monkeypatch: pytest.MonkeyPatch,
    store: _MemoryStore,
    profile: Profile,
    *,
    expires_at: datetime,
) -> Configuration:
    _fake_local_bundle_store(monkeypatch, store)
    configuration = _oauth_configuration(profile)
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _oauth_grant(profile, expires_at=expires_at),
        client_secret=None,
        expected_slot=slot,
    )
    return configuration


def test_refresh_not_dispatched_restores_ready_without_returning_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2029, 1, 1, tzinfo=UTC)
    store = _MemoryStore()
    profile = _oauth_profile("ga4", account_ref="12345")
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now)
    _install_ready_bundle(monkeypatch, store, profile, expires_at=now + timedelta(seconds=30))
    attempts: list[object] = []

    def not_dispatched(bundle: object) -> GoogleOAuthRefreshGrant:
        attempts.append(bundle)
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_REQUEST_UNAVAILABLE",
            "sanitized",
            dispatch_state=GoogleOAuthTokenDispatchState.NOT_DISPATCHED,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", not_dispatched)
    with pytest.raises(GoogleOAuthTransportError) as failure:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert failure.value.dispatch_state is GoogleOAuthTokenDispatchState.NOT_DISPATCHED
    assert len(attempts) == 1
    assert '"state":"READY"' in next(iter(store.values.values()))


def test_uncertain_refresh_blocks_replay_and_retains_reconnect_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2029, 1, 1, tzinfo=UTC)
    store = _MemoryStore()
    profile = _oauth_profile("ga4", account_ref="12345")
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now)
    _install_ready_bundle(monkeypatch, store, profile, expires_at=now + timedelta(seconds=30))
    attempts: list[object] = []

    def uncertain(bundle: object) -> GoogleOAuthRefreshGrant:
        attempts.append(bundle)
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_TOKEN_OUTCOME_UNCERTAIN",
            "sanitized",
            dispatch_state=GoogleOAuthTokenDispatchState.UNCERTAIN,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", uncertain)
    with pytest.raises(AdhelmError) as first:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert first.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert '"state":"RECONNECT_REQUIRED"' in next(iter(store.values.values()))
    with pytest.raises(AdhelmError) as second:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert second.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert len(attempts) == 1


def test_malformed_or_forged_profile_blocks_before_refresh_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2029, 1, 1, tzinfo=UTC)
    store = _MemoryStore()
    profile = _oauth_profile("ga4", account_ref="12345")
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now)
    _install_ready_bundle(monkeypatch, store, profile, expires_at=now + timedelta(seconds=30))
    original_bundle = _oauth_value(store, profile)
    store.values[(oauth_credentials._SERVICE, profile.credential_ref)] = original_bundle.replace(
        '"schema_version":1', '"schema_version":1.0'
    )
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("Malformed bundle must fail before token transport"),
    )
    with pytest.raises(AdhelmError) as malformed:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert malformed.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"

    store.values[(oauth_credentials._SERVICE, profile.credential_ref)] = original_bundle
    forged = profile.model_copy(update={"principal_ref": "google_oidc_principal_v1." + "0" * 64})
    with pytest.raises(AdhelmError) as invalid_profile:
        oauth_credentials.resolve_google_oauth_access_token(forged)
    assert invalid_profile.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"


def test_google_ads_oauth_primary_setup_is_rejected_and_there_is_no_secondary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile()
    configuration = _oauth_configuration(profile)
    with pytest.raises(TypeError):
        credentials.credential(profile, secondary=True)  # type: ignore[call-arg]
    monkeypatch.setattr(
        credential_setup,
        "prompt_secret_from_terminal",
        lambda: pytest.fail("OAuth primary must not open the generic secret prompt"),
    )
    with pytest.raises(AdhelmError) as failure:
        credential_setup.store_interactive(configuration, profile.profile_id)
    assert failure.value.code == "GOOGLE_OAUTH_INTERACTIVE_SETUP_UNSUPPORTED"


def test_oauth_slot_aliases_static_os_store_refs_before_any_store_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_profile = _oauth_profile("ga4", account_ref="12345")
    legacy = _legacy_profile().model_copy(
        update={"profile_id": "legacy-other", "credential_ref": oauth_profile.credential_ref}
    )
    configuration = Configuration.model_validate(
        {"profiles": [oauth_profile.model_dump(mode="json"), legacy.model_dump(mode="json")]}
    )
    monkeypatch.setattr(
        credential_setup,
        "os_store_backend",
        lambda: pytest.fail("aliases must fail before OS-store access"),
    )
    with pytest.raises(AdhelmError) as failure:
        credential_setup.credential_status(configuration, oauth_profile.profile_id)
    assert failure.value.code == "CREDENTIAL_REFERENCE_ALIAS"


def test_a_config_written_by_0_1_0_loads_and_the_retired_field_is_not_written_back(
    tmp_path: Path,
) -> None:
    """0.1.0 wrote `secondary_credential_ref` (null, or a token entry name) on every profile."""
    for retired in (None, "google-ads-read-developer-token"):
        profile = _oauth_profile().model_dump(mode="json")
        profile["secondary_credential_ref"] = retired
        directory = tmp_path / f"retired-{retired}"
        directory.mkdir()
        (directory / "config.json").write_text(
            json.dumps({"profiles": [profile]}), encoding="utf-8"
        )
        loaded = load_configuration(directory)
        assert [p.profile_id for p in loaded.profiles] == [profile["profile_id"]]
        assert "secondary_credential_ref" not in loaded.profiles[0].model_dump()
        save_configuration(directory, loaded, replace=True)
        assert "secondary_credential_ref" not in (directory / "config.json").read_text()


def test_a_grant_sealed_against_an_older_profile_form_asks_for_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 0.1.0 bundle is bound to a profile digest that no longer exists: say so, specifically."""
    store = _MemoryStore()
    _fake_local_bundle_store(monkeypatch, store)
    profile = _oauth_profile("ga4", account_ref="12345")
    configuration = _oauth_configuration(profile)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=UTC))
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _oauth_grant(profile),
        client_secret=None,
        expected_slot=slot,
    )
    key = (oauth_credentials._SERVICE, profile.credential_ref)
    bundle = oauth_credentials._decode_bundle(store.values[key])
    store.values[key] = replace(bundle, profile_digest="0" * 64)._encoded()

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert raised.value.code == "GOOGLE_OAUTH_PROFILE_BINDING_STALE"
    assert raised.value.exit_code == int(ExitCode.AUTHORIZATION)
    assert "auth google-oauth reconnect" in raised.value.next_action
    assert profile.profile_id in raised.value.next_action

    # A genuinely different account still gets the generic refusal, not the reconnect hint.
    store.values[key] = replace(bundle, account_ref="99999")._encoded()
    with pytest.raises(AdhelmError) as other:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert other.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"

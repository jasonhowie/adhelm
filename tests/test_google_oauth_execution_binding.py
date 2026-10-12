"""Local-only regressions for the GA4 Admin EDIT OAuth execution handoff."""

from __future__ import annotations

import json
import pickle
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from adhelm import google_oauth_credentials as oauth_credentials
from adhelm import google_oauth_execution_binding as execution_binding
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthRefreshGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.models import Configuration, Profile

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_CLIENT = "m23_ga4_admin_edit.apps.googleusercontent.com"
_SUBJECT = "m23-private-subject"
_ACCESS_TOKEN = "m23-private-access-token"
_REFRESH_TOKEN = "m23-private-refresh-token"
_SCOPE = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", "EDIT")]


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: int = 0
    writes: int = 0

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads += 1
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes += 1
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile(
    *,
    purpose: str = "EDIT",
    profile_id: str = "m23-ga4-admin-edit",
    credential_source: str = "OS_OAUTH_GOOGLE",
) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", purpose)]
    return Profile(
        profile_id=profile_id,
        provider="ga4_admin",
        account_ref="12345",
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        client_ref=_CLIENT,
        scopes=[scope],
        purpose=purpose,
        currency="USD",
        timezone="UTC",
        credential_source=credential_source,
        credential_ref="m23-ga4-admin-primary",
    )


def _grant(profile: Profile, *, expires_at: datetime) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, _SCOPE),
        access_token=_ACCESS_TOKEN,
        expires_at=expires_at,
        refresh_token=_REFRESH_TOKEN,
        refresh_token_expires_at=None,
        requested_scopes=("openid", _SCOPE),
        returned_scopes=(_SCOPE,),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=_SUBJECT,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _wire(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore, current: list[datetime]) -> None:
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])


def _installed(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime = _NOW + timedelta(hours=1),
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
        _grant(profile, expires_at=expires_at),
        client_secret=None,
        expected_slot=slot,
    )
    store.reads = 0
    store.writes = 0
    return store, profile, current


def test_pairs_one_protected_snapshot_and_consumes_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)

    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)

    assert binding.profile_ref == profile.profile_id
    assert binding.provider == "ga4_admin"
    assert binding.purpose == "EDIT"
    assert binding.credential_source == "OS_OAUTH_GOOGLE"
    assert binding.account_ref == profile.account_ref
    assert binding.principal_ref == profile.principal_ref
    assert binding.client_ref == profile.client_ref
    assert binding.api_scope == _SCOPE
    assert store.reads == 1
    assert execution_binding.bearer_for_ga4_admin_edit(binding, profile) == _ACCESS_TOKEN

    with pytest.raises(AdhelmError) as repeated:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert repeated.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"


def test_binding_never_exposes_bearer_through_common_serializers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, _current = _installed(monkeypatch)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    snapshot = oauth_credentials._resolve_google_oauth_execution_snapshot(profile)
    with execution_binding._BINDINGS_LOCK:
        record = execution_binding._BINDINGS[binding]

    for value in (binding, snapshot, record):
        assert _ACCESS_TOKEN not in repr(value)
        with pytest.raises(TypeError):
            asdict(value)
        with pytest.raises(TypeError):
            vars(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            json.dumps(value)


def test_rejects_static_read_and_constructed_profiles_before_private_bundle_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    raw = profile.model_dump(mode="python")
    raw["credential_source"] = "OS_STORE"
    static_profile = Profile.model_validate(raw)
    read_profile = _profile(purpose="READ")
    forged_edit = profile.model_copy(update={"scopes": [_SCOPE + ".widened"]})

    for value in (static_profile, read_profile, forged_edit):
        with pytest.raises(AdhelmError) as rejected:
            execution_binding.resolve_ga4_admin_edit_execution_binding(value)
        assert rejected.value.code == "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED"
    assert store.reads == 0


def test_forged_or_different_profile_binding_never_releases_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, _current = _installed(monkeypatch)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    other = profile.model_copy(update={"profile_id": "m23-different-profile"})

    with pytest.raises(AdhelmError) as different:
        execution_binding.bearer_for_ga4_admin_edit(binding, other)
    assert different.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"
    assert execution_binding.bearer_for_ga4_admin_edit(binding, profile) == _ACCESS_TOKEN

    incomplete = object.__new__(execution_binding.Ga4AdminEditOAuthExecutionBinding)
    with pytest.raises(AdhelmError) as forged:
        execution_binding.bearer_for_ga4_admin_edit(incomplete, profile)
    assert forged.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"


def test_consume_rechecks_current_access_token_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires_at = _NOW + timedelta(seconds=90)
    _store, profile, current = _installed(monkeypatch, expires_at=expires_at)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    current[0] = expires_at

    with pytest.raises(AdhelmError) as expired:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert expired.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert _ACCESS_TOKEN not in str(expired.value)

    with pytest.raises(AdhelmError) as consumed:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert consumed.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"


def test_consume_rejects_a_trusted_clock_rollback_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, current = _installed(monkeypatch)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    current[0] = _NOW - timedelta(microseconds=1)

    with pytest.raises(AdhelmError) as rollback:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert rollback.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert _ACCESS_TOKEN not in str(rollback.value)


def test_binding_creation_rejects_clock_rollback_after_locked_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    samples = iter((_NOW, _NOW + timedelta(microseconds=1), _NOW))
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(samples))

    with pytest.raises(AdhelmError) as rollback:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert rollback.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert store.reads == 1


def test_consume_rejects_clock_rollback_after_binding_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, _current = _installed(monkeypatch)
    samples = iter(
        (
            _NOW,
            _NOW,
            _NOW + timedelta(seconds=2),
            _NOW + timedelta(seconds=1),
        )
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(samples))

    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    with pytest.raises(AdhelmError) as rollback:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert rollback.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert _ACCESS_TOKEN not in str(rollback.value)


def test_consume_clock_failure_invalidates_the_private_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, current = _installed(monkeypatch)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: None)

    with pytest.raises(AdhelmError) as unavailable:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert unavailable.value.code == "GOOGLE_OAUTH_CLOCK_UNAVAILABLE"

    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])
    with pytest.raises(AdhelmError) as consumed:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert consumed.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"


def test_refreshes_once_then_pairs_the_refreshed_bearer_without_a_second_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch, expires_at=_NOW + timedelta(seconds=30))
    calls: list[str] = []

    def refreshed(_bundle: object) -> GoogleOAuthRefreshGrant:
        calls.append("refresh")
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(profile.client_ref, _SCOPE),
            access_token="m23-refreshed-access-token",
            expires_at=_NOW + timedelta(hours=1),
            returned_scopes=(_SCOPE,),
            identity_reverified=True,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", refreshed)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)

    assert calls == ["refresh"]
    # One initial slot read plus one proven readback for each durable refresh state.
    assert store.reads == 3
    assert (
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
        == "m23-refreshed-access-token"
    )


def test_public_token_resolver_keeps_existing_ready_bundle_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)

    assert oauth_credentials.resolve_google_oauth_access_token(profile) == _ACCESS_TOKEN
    assert store.reads == 1


def test_malformed_bundle_error_is_generic_and_does_not_expose_protected_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, _current = _installed(monkeypatch)
    sentinel = "m23-private-malformed-bundle-sentinel"
    store.values[(oauth_credentials._SERVICE, profile.credential_ref)] = sentinel

    with pytest.raises(AdhelmError) as invalid:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert invalid.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"
    assert sentinel not in str(invalid.value)

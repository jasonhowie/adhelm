"""Independent adversarial review of the Google OAuth EDIT execution binding.

Every test uses only an in-memory protected-store fake and a patched local
clock.  It never opens a browser, calls a provider, reads an OS credential
store, or dispatches a real refresh request.
"""

from __future__ import annotations

import json
import pickle
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta

import pytest

from adhelm import credentials
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm import google_oauth_execution_binding as execution_binding
from adhelm.canonical import canonical_bytes, digest
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
_EDIT_SCOPE = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", "EDIT")]
_READ_SCOPE = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", "READ")]
_CLIENT = "independent-edit.apps.googleusercontent.com"
_SUBJECT = "independent-execution-subject"
_ACCESS = "independent-execution-access-token"
_REFRESH = "independent-execution-refresh-token"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    get_calls: list[tuple[str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.get_calls.append((service_name, username))
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


class _RaisingReadStore(_MemoryStore):
    def get_password(self, service_name: str, username: str) -> str | None:
        del service_name, username
        raise RuntimeError("private-protected-bundle-backend-sentinel")


@dataclass
class _NoNestedLock:
    active: bool = False
    entries: int = 0

    @contextmanager
    def __call__(self, _reference: str) -> Iterator[None]:
        if self.active:
            raise AssertionError("OAuth execution resolution acquired its credential lock twice")
        self.active = True
        self.entries += 1
        try:
            yield
        finally:
            self.active = False


def _profile(
    *,
    credential_source: str = "OS_OAUTH_GOOGLE",
    purpose: str = "EDIT",
    credential_ref: str = "independent-edit-primary",
) -> Profile:
    scope = _EDIT_SCOPE if purpose == "EDIT" else _READ_SCOPE
    return Profile(
        profile_id=f"independent-ga4-admin-{purpose.lower()}",
        provider="ga4_admin",
        account_ref="12345",
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        client_ref=_CLIENT,
        scopes=[scope],
        purpose=purpose,
        currency="USD",
        timezone="UTC",
        credential_source=credential_source,
        credential_ref=credential_ref,
    )


def _grant(
    profile: Profile, *, expires_at: datetime, access_token: str = _ACCESS
) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, profile.scopes[0]),
        access_token=access_token,
        expires_at=expires_at,
        refresh_token=_REFRESH,
        refresh_token_expires_at=None,
        requested_scopes=("openid", profile.scopes[0]),
        returned_scopes=(profile.scopes[0],),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=_SUBJECT,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _wire_store(
    monkeypatch: pytest.MonkeyPatch,
    store: _MemoryStore,
    current: list[datetime],
) -> None:
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])


def _install(
    monkeypatch: pytest.MonkeyPatch,
    store: _MemoryStore,
    current: list[datetime],
    *,
    expires_at: datetime,
) -> Profile:
    _wire_store(monkeypatch, store, current)
    profile = _profile()
    configuration = Configuration(profiles=[profile])
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile, expires_at=expires_at),
        client_secret=None,
        expected_slot=slot,
    )
    return profile


def _stored_data(store: _MemoryStore, profile: Profile) -> dict[str, object]:
    return json.loads(store.values[(oauth_credentials._SERVICE, profile.credential_ref)])


def _assert_redacted(value: object) -> None:
    rendered = str(value)
    for secret in (_ACCESS, _REFRESH, _SUBJECT, "private-raw-subject-sentinel"):
        assert secret not in rendered


def test_matching_edit_profile_issues_one_snapshot_and_consumes_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    store.get_calls.clear()

    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)

    assert binding.profile_ref == profile.profile_id
    assert binding.profile_digest == digest(profile)
    assert binding.provider == "ga4_admin"
    assert binding.purpose == "EDIT"
    assert binding.credential_source == "OS_OAUTH_GOOGLE"
    assert binding.account_ref == profile.account_ref
    assert binding.principal_ref == profile.principal_ref
    assert binding.client_ref == profile.client_ref
    assert binding.api_scope == _EDIT_SCOPE
    assert not hasattr(binding, "execution_authority")
    assert all(
        not hasattr(binding, name)
        for name in ("_bearer_token", "access_token", "refresh_token", "raw_subject")
    )
    assert store.get_calls == [(oauth_credentials._SERVICE, profile.credential_ref)]

    assert execution_binding.bearer_for_ga4_admin_edit(binding, profile) == _ACCESS
    assert store.get_calls == [(oauth_credentials._SERVICE, profile.credential_ref)]
    with pytest.raises(AdhelmError) as reused:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert reused.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"


def test_non_oauth_ci_static_and_read_profiles_stop_before_snapshot_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected_snapshot(_: Profile) -> object:
        nonlocal calls
        calls += 1
        pytest.fail("an invalid source/purpose reached protected snapshot resolution")

    monkeypatch.setattr(
        execution_binding, "_resolve_google_oauth_execution_snapshot", unexpected_snapshot
    )
    forged_read = _profile(purpose="EDIT").model_copy(
        update={"purpose": "READ", "scopes": [_READ_SCOPE]}
    )
    for profile in (
        _profile(credential_source="OS_STORE"),
        _profile(credential_source="EXPLICIT_CI"),
        _profile(purpose="READ"),
        forged_read,
    ):
        with pytest.raises(AdhelmError) as raised:
            execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
        assert raised.value.code == "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED"
    assert calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_digest", "0" * 64),
        ("account_ref", "99999"),
        ("raw_subject", "private-raw-subject-sentinel"),
        ("client_ref", "other-edit.apps.googleusercontent.com"),
        ("api_scope", _READ_SCOPE),
    ],
)
def test_tampered_sealed_bundle_never_issues_an_execution_binding(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    raw = _stored_data(store, profile)
    raw[field] = value
    slot = (oauth_credentials._SERVICE, profile.credential_ref)
    store.values[slot] = canonical_bytes(raw).decode("utf-8")

    with pytest.raises(AdhelmError) as raised:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)

    # A bundle that differs only in its profile digest is the specific, actionable
    # "reconnect" refusal; any other tampering stays the generic refusal.
    assert raised.value.code == (
        "GOOGLE_OAUTH_PROFILE_BINDING_STALE"
        if field == "profile_digest"
        else "GOOGLE_OAUTH_CREDENTIAL_INVALID"
    )
    _assert_redacted(raised.value)


def test_refresh_uses_one_reference_lock_and_one_returned_token_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(seconds=30))
    lock = _NoNestedLock()
    monkeypatch.setattr(oauth_credentials, "credential_mutation_lock", lock)
    dispatches: list[object] = []

    def refreshed(bundle: object) -> GoogleOAuthRefreshGrant:
        dispatches.append(bundle)
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(profile.client_ref, _EDIT_SCOPE),
            access_token="independent-refreshed-access-token",
            expires_at=_NOW + timedelta(minutes=10),
            returned_scopes=None,
            identity_reverified=False,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", refreshed)
    store.get_calls.clear()
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)

    assert lock.entries == 1
    assert len(dispatches) == 1
    assert execution_binding.bearer_for_ga4_admin_edit(binding, profile) == (
        "independent-refreshed-access-token"
    )
    assert all(service == oauth_credentials._SERVICE for service, _ in store.get_calls)


def test_missing_or_nonready_bundle_fails_before_a_binding_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    _wire_store(monkeypatch, store, current)
    profile = _profile()

    with pytest.raises(AdhelmError) as missing:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert missing.value.code == "AUTHENTICATION_REQUIRED"

    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    raw = _stored_data(store, profile)
    raw["state"] = "REFRESH_STARTED"
    raw["refresh_started_at"] = _NOW.isoformat()
    slot = (oauth_credentials._SERVICE, profile.credential_ref)
    store.values[slot] = canonical_bytes(raw).decode("utf-8")
    before = len(execution_binding._BINDINGS)

    with pytest.raises(AdhelmError) as nonready:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert nonready.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert len(execution_binding._BINDINGS) == before


def test_protected_store_exception_is_sanitized_before_a_binding_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _RaisingReadStore()
    _wire_store(monkeypatch, store, current)
    before = len(execution_binding._BINDINGS)

    with pytest.raises(AdhelmError) as raised:
        execution_binding.resolve_ga4_admin_edit_execution_binding(_profile())
    assert raised.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "private-protected-bundle-backend-sentinel" not in str(raised.value)
    assert len(execution_binding._BINDINGS) == before


def test_uncertain_refresh_blocks_execution_binding_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(seconds=30))
    attempts = 0

    def uncertain_refresh(_: object) -> object:
        nonlocal attempts
        attempts += 1
        raise GoogleOAuthTransportError(
            "GOOGLE_OAUTH_TOKEN_OUTCOME_UNCERTAIN",
            "private transport detail must not escape",
            dispatch_state=GoogleOAuthTokenDispatchState.UNCERTAIN,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", uncertain_refresh)
    with pytest.raises(AdhelmError) as first:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert first.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    _assert_redacted(first.value)
    with pytest.raises(AdhelmError) as second:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert second.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert attempts == 1


def test_expired_binding_cannot_release_a_cached_bearer_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    expires_at = _NOW + timedelta(seconds=61)
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=expires_at)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    current[0] = expires_at

    with pytest.raises(AdhelmError) as expired:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert expired.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    _assert_redacted(expired.value)
    assert binding not in execution_binding._BINDINGS
    with pytest.raises(AdhelmError):
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)


def test_clock_rollback_after_resolution_cannot_release_a_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    current[0] = _NOW - timedelta(seconds=1)

    with pytest.raises(AdhelmError) as rollback:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert rollback.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    _assert_redacted(rollback.value)
    assert binding not in execution_binding._BINDINGS


def test_clock_rollback_between_lifecycle_snapshot_and_binding_registration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    samples = iter((_NOW, _NOW - timedelta(seconds=1)))
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(samples))
    before = len(execution_binding._BINDINGS)

    with pytest.raises(AdhelmError) as rollback:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert rollback.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    _assert_redacted(rollback.value)
    assert len(execution_binding._BINDINGS) == before


def test_consume_clock_failure_destroys_the_matching_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    monkeypatch.setattr(
        oauth_credentials,
        "_utc_now",
        lambda: (_ for _ in ()).throw(RuntimeError("private-clock-sentinel")),
    )

    with pytest.raises(AdhelmError) as failure:
        execution_binding.bearer_for_ga4_admin_edit(binding, profile)
    assert failure.value.code == "GOOGLE_OAUTH_CLOCK_UNAVAILABLE"
    assert "private-clock-sentinel" not in str(failure.value)
    assert binding not in execution_binding._BINDINGS


def test_snapshot_binding_and_registry_do_not_offer_generic_secret_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    snapshot = oauth_credentials._resolve_google_oauth_execution_snapshot(profile)
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    private_values = (snapshot, binding, execution_binding._BINDINGS[binding])

    for value in private_values:
        assert not is_dataclass(value)
        _assert_redacted(repr(value))
        with pytest.raises(TypeError):
            vars(value)
        with pytest.raises(TypeError):
            asdict(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)


def test_constructed_snapshot_or_binding_cannot_be_promoted_to_bearer_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    forged_snapshot = object.__new__(oauth_credentials._GoogleOAuthExecutionSnapshot)
    for name, value in {
        "_bearer_token": "forged-private-bearer",
        "profile_digest": digest(profile),
        "provider": "ga4_admin",
        "purpose": "EDIT",
        "account_ref": profile.account_ref,
        "principal_ref": profile.principal_ref,
        "client_ref": profile.client_ref,
        "api_scope": _EDIT_SCOPE,
        "_issuer": object(),
    }.items():
        object.__setattr__(forged_snapshot, name, value)
    monkeypatch.setattr(
        execution_binding,
        "_resolve_google_oauth_execution_snapshot",
        lambda _profile: forged_snapshot,
    )

    with pytest.raises(AdhelmError) as forged_snapshot_failure:
        execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    assert forged_snapshot_failure.value.code == "GOOGLE_OAUTH_EXECUTION_SNAPSHOT_INVALID"
    assert "forged-private-bearer" not in str(forged_snapshot_failure.value)

    forged_binding = object.__new__(execution_binding.Ga4AdminEditOAuthExecutionBinding)
    with pytest.raises(AdhelmError) as forged_binding_failure:
        execution_binding.bearer_for_ga4_admin_edit(forged_binding, profile)
    assert forged_binding_failure.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"


@pytest.mark.parametrize(
    "changes",
    [
        {"profile_id": "independent-other-profile"},
        {"account_ref": "54321"},
        {
            "principal_ref": derive_google_oidc_principal_ref(
                GOOGLE_OIDC_CANONICAL_ISSUER, "independent-other-subject"
            )
        },
        {"client_ref": "independent-other.apps.googleusercontent.com"},
        {"timezone": "America/Chicago"},
    ],
)
def test_profile_drift_after_resolution_cannot_consume_its_binding(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str]
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))
    binding = execution_binding.resolve_ga4_admin_edit_execution_binding(profile)
    drifted = profile.model_copy(update=changes)

    with pytest.raises(AdhelmError) as raised:
        execution_binding.bearer_for_ga4_admin_edit(binding, drifted)
    assert raised.value.code == "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID"
    _assert_redacted(raised.value)


def test_existing_primary_oauth_token_resolver_remains_a_string_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_NOW]
    store = _MemoryStore()
    profile = _install(monkeypatch, store, current, expires_at=_NOW + timedelta(minutes=10))

    assert oauth_credentials.resolve_google_oauth_access_token(profile) == _ACCESS
    assert credentials.credential(profile) == _ACCESS

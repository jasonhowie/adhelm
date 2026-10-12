"""Fake-store coverage for the protected LinkedIn Ads OAuth credential lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adhelm import config as config_module
from adhelm import linkedin_token_credentials as lifecycle
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.linkedin_token_profiles import (
    LINKEDIN_OAUTH_CONTRACT,
    LINKEDIN_TOKEN_SERVICE,
    linkedin_token_reference,
)
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.oauth_enrollment import (
    OAuthAuthorizationTarget,
    OAuthInitialGrant,
    OAuthRefreshGrant,
)

ACCESS = "linkedin-fixture-access-value"
REFRESH = "linkedin-fixture-refresh-value"
ROTATED_REFRESH = "linkedin-fixture-rotated-refresh-value"
CLIENT_SECRET = "linkedin-fixture-client-secret"
CLIENT_ID = "linkedin-fixture-client-id"
ACCOUNT = "503012345"
BEARER_REF = "linkedin-read-bearer"
PROFILE_ID = "linkedin-read"
GENERIC_SERVICE = "adhelm"


@dataclass
class MemoryStore:
    """A fake OS credential store; it records every write in order."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)
    set_calls: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)
    drop_writes: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.set_calls.append((service_name, username))
        if not self.drop_writes:
            self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.deleted.append((service_name, username))
        self.values.pop((service_name, username), None)


def linkedin_profile(**updates: object) -> Profile:
    value: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "provider": "linkedin_ads",
        "account_ref": ACCOUNT,
        "principal_ref": "linkedin-member-local",
        "client_ref": "linkedin-app-local",
        "scopes": ["r_ads", "r_ads_reporting"],
        "purpose": "READ",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_STORE",
        "credential_ref": BEARER_REF,
    }
    value.update(updates)
    return Profile.model_validate(value)


def configured_directory(tmp_path: Path, profile: Profile | None = None) -> Path:
    directory = tmp_path / "data"
    save_configuration(
        directory,
        Configuration(
            profiles=[profile or linkedin_profile()],
            output=OutputPreferences(max_output_bytes=8192),
        ),
    )
    return directory


def install_fake_store(
    monkeypatch: pytest.MonkeyPatch, store: MemoryStore, events: list[str] | None = None
) -> None:
    """Replace the credential backend and lock with in-memory fakes."""

    @contextmanager
    def recording_lock(reference: str) -> Iterator[None]:
        if events is not None:
            events.append(f"lock:{reference}")
        try:
            yield
        finally:
            if events is not None:
                events.append(f"unlock:{reference}")

    monkeypatch.setattr(lifecycle, "_credential_backend", lambda: store)
    monkeypatch.setattr(lifecycle, "credential_mutation_lock", recording_lock)
    monkeypatch.setattr(
        config_module,
        "credential_mutation_lock",
        recording_lock if events is not None else (lambda _r: nullcontext()),
    )


def target(scopes: tuple[str, ...] = ("r_ads", "r_ads_reporting")) -> OAuthAuthorizationTarget:
    return OAuthAuthorizationTarget(
        contract=LINKEDIN_OAUTH_CONTRACT, client_ref=CLIENT_ID, scopes=scopes
    )


def initial_grant(
    *,
    refresh_token: str | None = REFRESH,
    scopes: tuple[str, ...] = ("r_ads", "r_ads_reporting"),
    access_lifetime: timedelta = timedelta(days=60),
) -> OAuthInitialGrant:
    now = datetime.now(UTC)
    return OAuthInitialGrant(
        target=target(scopes),
        access_token=ACCESS,
        access_token_expires_at=now + access_lifetime,
        refresh_token=refresh_token,
        refresh_token_expires_at=None if refresh_token is None else now + timedelta(days=365),
        refresh_status="GRANTED" if refresh_token else "REFRESH_NOT_GRANTED",
        granted_scopes=scopes,
    )


def refresh_grant(
    *, rotated: str | None = None, scopes: tuple[str, ...] | None = None
) -> OAuthRefreshGrant:
    now = datetime.now(UTC)
    return OAuthRefreshGrant(
        target=target(),
        access_token="linkedin-fixture-refreshed-access",
        access_token_expires_at=now + timedelta(days=60),
        granted_scopes=scopes,
        rotated_refresh_token=rotated,
        rotated_refresh_token_expires_at=None if rotated is None else now + timedelta(days=365),
    )


def pseudonym(salt: bytes) -> str:
    return lifecycle.linkedin_account_pseudonym(salt, ACCOUNT)


def enroll(directory: Path, grant: OAuthInitialGrant | None = None) -> bytes:
    """Complete one enrollment and return the salt used for the pseudonym."""
    slot = lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID)
    salt = lifecycle.new_identity_salt()
    lifecycle.install_linkedin_token_grant(
        directory,
        slot,
        grant or initial_grant(),
        client_secret=CLIENT_SECRET,
        identity_salt=salt,
        identity_pseudonym=pseudonym(salt),
    )
    return salt


def test_enrollment_writes_distinct_slots_and_keeps_the_bearer_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    enroll(directory)

    state_ref = linkedin_token_reference(PROFILE_ID, "STATE")
    refresh_ref = linkedin_token_reference(PROFILE_ID, "REFRESH_TOKEN")
    secret_ref = linkedin_token_reference(PROFILE_ID, "CLIENT_SECRET")
    assert store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS
    assert store.values[(LINKEDIN_TOKEN_SERVICE, refresh_ref)] == REFRESH
    assert store.values[(LINKEDIN_TOKEN_SERVICE, secret_ref)] == CLIENT_SECRET
    # The bearer and the refresh token can never share one slot.
    assert refresh_ref != BEARER_REF and secret_ref != BEARER_REF
    assert (LINKEDIN_TOKEN_SERVICE, BEARER_REF) not in store.values
    # The durable start is written before any secret and resolved afterwards.
    assert store.set_calls[0] == (LINKEDIN_TOKEN_SERVICE, state_ref)
    assert store.set_calls[-1] == (LINKEDIN_TOKEN_SERVICE, state_ref)
    assert store.set_calls.count((LINKEDIN_TOKEN_SERVICE, state_ref)) == 2
    assert '"local_state":"READY"' in store.values[(LINKEDIN_TOKEN_SERVICE, state_ref)]


def test_enrollment_state_contains_no_secret_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    enroll(directory)

    state = store.values[(LINKEDIN_TOKEN_SERVICE, linkedin_token_reference(PROFILE_ID, "STATE"))]
    for secret in (ACCESS, REFRESH, CLIENT_SECRET):
        assert secret not in state
    assert ACCOUNT not in state


def test_second_enrollment_refuses_to_overwrite_existing_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)

    with pytest.raises(AdhelmError) as error:
        lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID)

    assert error.value.code == "LINKEDIN_TOKEN_CREDENTIAL_EXISTS"


def test_status_reports_expiry_scopes_pseudonym_and_slot_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    salt = enroll(directory)

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)

    assert status.local_state == "READY"
    assert status.granted_scopes == ("r_ads", "r_ads_reporting")
    assert status.refresh_status == "GRANTED"
    assert status.identity_pseudonym == pseudonym(salt)
    assert status.access_token_expired is False
    assert status.bearer_slot_present is True
    assert status.refresh_slot_present is True
    assert status.app_review_verified is False
    assert status.execution_authority is False


def test_status_reports_missing_state_without_a_credential_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)

    assert status.local_state == "MISSING"
    assert status.identity_pseudonym is None
    assert status.refresh_status is None
    assert BEARER_REF not in repr(status)


def test_status_reports_expired_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory, initial_grant(access_lifetime=timedelta(seconds=1)))
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: datetime.now(UTC) + timedelta(days=1))

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)

    assert status.access_token_expired is True
    assert status.local_state == "READY"


def test_status_marks_corrupt_state_unverified_rather_than_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    store.values[(LINKEDIN_TOKEN_SERVICE, linkedin_token_reference(PROFILE_ID, "STATE"))] = "{}"

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)

    assert status.local_state == "UNVERIFIED"
    assert status.granted_scopes == ()


def test_refresh_without_a_granted_refresh_token_fails_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory, initial_grant(refresh_token=None))

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)
    assert status.refresh_status == "REFRESH_NOT_GRANTED"
    assert status.refresh_slot_present is False

    with pytest.raises(AdhelmError) as error:
        lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    assert error.value.code == "REFRESH_NOT_GRANTED"
    assert error.value.exit_code == 4


def test_begin_refresh_records_a_durable_start_and_releases_every_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    events: list[str] = []
    install_fake_store(monkeypatch, store, events)
    directory = configured_directory(tmp_path)
    enroll(directory)
    events.clear()

    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    state_ref = linkedin_token_reference(PROFILE_ID, "STATE")
    assert '"local_state":"REFRESH_STARTED"' in store.values[(LINKEDIN_TOKEN_SERVICE, state_ref)]
    # One configuration lock plus the four exact credential slot locks, all released.
    assert len([event for event in events if event.startswith("lock:")]) == 5
    assert len([event for event in events if event.startswith("unlock:")]) == 5
    assert repr(context) == "<LinkedInTokenRefreshContext redacted>"
    for secret in (REFRESH, CLIENT_SECRET):
        assert secret not in repr(context)


def test_complete_refresh_stores_a_new_bearer_and_rotated_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    result = lifecycle.complete_linkedin_token_refresh(
        directory, context, refresh_grant(rotated=ROTATED_REFRESH)
    )

    refresh_ref = linkedin_token_reference(PROFILE_ID, "REFRESH_TOKEN")
    assert result.operation == "REFRESH"
    assert result.refresh_status == "GRANTED"
    assert store.values[(GENERIC_SERVICE, BEARER_REF)] == "linkedin-fixture-refreshed-access"
    assert store.values[(LINKEDIN_TOKEN_SERVICE, refresh_ref)] == ROTATED_REFRESH
    assert lifecycle.linkedin_token_status(directory, PROFILE_ID).local_state == "READY"


def test_complete_refresh_keeps_the_existing_refresh_token_when_none_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    lifecycle.complete_linkedin_token_refresh(directory, context, refresh_grant())

    refresh_ref = linkedin_token_reference(PROFILE_ID, "REFRESH_TOKEN")
    assert store.values[(LINKEDIN_TOKEN_SERVICE, refresh_ref)] == REFRESH


def test_complete_refresh_rejects_a_reduced_scope_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    with pytest.raises(AdhelmError) as error:
        lifecycle.complete_linkedin_token_refresh(
            directory, context, refresh_grant(scopes=("r_ads",))
        )

    assert error.value.code == "LINKEDIN_TOKEN_SCOPE_REDUCED"
    assert store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS


def test_blocking_a_started_refresh_requires_reauthorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    lifecycle.block_linkedin_token_refresh(directory, context)

    assert (
        lifecycle.linkedin_token_status(directory, PROFILE_ID).local_state
        == "REAUTHORIZATION_REQUIRED"
    )
    with pytest.raises(AdhelmError) as error:
        lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)
    assert error.value.code == "LINKEDIN_TOKEN_REAUTHORIZATION_REQUIRED"
    assert error.value.exit_code == 4


def test_expired_refresh_token_fails_before_any_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: datetime.now(UTC) + timedelta(days=400))

    with pytest.raises(AdhelmError) as error:
        lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    assert error.value.code == "LINKEDIN_TOKEN_REFRESH_EXPIRED"
    assert error.value.exit_code == 4


def test_state_is_bound_to_the_exact_profile_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    save_configuration(
        directory,
        Configuration(
            profiles=[linkedin_profile(account_ref="503099999")],
            output=OutputPreferences(max_output_bytes=8192),
        ),
        replace=True,
    )

    with pytest.raises(AdhelmError) as error:
        lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    assert error.value.code == "LINKEDIN_TOKEN_PROFILE_CHANGED"


def test_local_removal_deletes_only_the_protected_linkedin_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)

    profile_digest, removed = lifecycle.remove_local_linkedin_token_slots(directory, PROFILE_ID)

    assert profile_digest == digest(linkedin_profile())
    assert removed == 3
    assert store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS
    assert all(service == LINKEDIN_TOKEN_SERVICE for service, _ in store.deleted)
    assert lifecycle.linkedin_token_status(directory, PROFILE_ID).local_state == "MISSING"


def test_a_dropped_write_reports_unverified_local_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore(drop_writes=True)
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    with pytest.raises(AdhelmError) as error:
        enroll(directory)

    assert error.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert error.value.exit_code == 6


def test_account_pseudonym_is_salted_and_reveals_no_account_id() -> None:
    first = lifecycle.new_identity_salt()
    second = lifecycle.new_identity_salt()

    assert lifecycle.linkedin_account_pseudonym(first, ACCOUNT) != (
        lifecycle.linkedin_account_pseudonym(second, ACCOUNT)
    )
    assert lifecycle.linkedin_account_pseudonym(first, ACCOUNT) == (
        lifecycle.linkedin_account_pseudonym(first, ACCOUNT)
    )
    assert ACCOUNT not in lifecycle.linkedin_account_pseudonym(first, ACCOUNT)


def test_lifecycle_contexts_cannot_be_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pickle

    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    slot = lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID)

    with pytest.raises(TypeError):
        pickle.dumps(slot)
    assert repr(slot) == "<LinkedInTokenEnrollmentSlot redacted>"


def test_a_read_profile_refuses_to_record_the_write_scope_even_from_a_direct_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    slot = lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID)
    salt = lifecycle.new_identity_salt()
    # The generic flow already clamps a wider echoed grant; this is the second,
    # provider-specific refusal, reached only by a direct constructor call.
    widened = OAuthInitialGrant(
        target=target(),
        access_token=ACCESS,
        access_token_expires_at=datetime.now(UTC) + timedelta(days=60),
        refresh_token=REFRESH,
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=365),
        refresh_status="GRANTED",
        granted_scopes=("r_ads", "r_ads_reporting", "rw_ads"),
    )

    with pytest.raises(AdhelmError) as error:
        lifecycle.install_linkedin_token_grant(
            directory,
            slot,
            widened,
            client_secret=CLIENT_SECRET,
            identity_salt=salt,
            identity_pseudonym=pseudonym(salt),
        )

    assert error.value.code == "LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE"
    assert error.value.exit_code == 4
    assert store.set_calls == []


def test_a_rotated_refresh_token_without_an_expiry_keeps_the_recorded_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    enrolled = lifecycle.linkedin_token_status(directory, PROFILE_ID).refresh_token_expires_at
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)
    grant = OAuthRefreshGrant(
        target=target(),
        access_token="linkedin-fixture-refreshed-access",
        access_token_expires_at=datetime.now(UTC) + timedelta(days=60),
        granted_scopes=None,
        rotated_refresh_token=ROTATED_REFRESH,
        rotated_refresh_token_expires_at=None,
    )

    result = lifecycle.complete_linkedin_token_refresh(directory, context, grant)

    assert result.refresh_token_expires_at == enrolled
    assert (
        lifecycle.linkedin_token_status(directory, PROFILE_ID).refresh_token_expires_at == enrolled
    )


def test_restoring_a_never_dispatched_refresh_leaves_the_profile_refreshable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)

    lifecycle.restore_linkedin_token_refresh(directory, context)

    assert lifecycle.linkedin_token_status(directory, PROFILE_ID).local_state == "READY"
    # The restored profile can start a fresh refresh; the grant was not consumed.
    assert lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID) is not None


def test_a_restore_never_overwrites_state_written_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    context = lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)
    lifecycle.block_linkedin_token_refresh(directory, context)

    lifecycle.restore_linkedin_token_refresh(directory, context)

    assert (
        lifecycle.linkedin_token_status(directory, PROFILE_ID).local_state
        == "REAUTHORIZATION_REQUIRED"
    )


def test_the_derived_linkedin_slots_are_reserved_credential_references() -> None:
    from pydantic import ValidationError

    from adhelm.models import is_reserved_credential_reference

    for slot in ("CLIENT_SECRET", "REFRESH_TOKEN", "STATE"):
        reference = linkedin_token_reference(PROFILE_ID, slot)
        assert is_reserved_credential_reference(reference)
        with pytest.raises(ValidationError):
            linkedin_profile(credential_ref=reference)
    assert not is_reserved_credential_reference(BEARER_REF)


def _tamper_recorded_scopes(store: MemoryStore, replacement: str) -> None:
    reference = linkedin_token_reference(PROFILE_ID, "STATE")
    stored = store.values[(LINKEDIN_TOKEN_SERVICE, reference)]
    tampered = stored.replace('["r_ads","r_ads_reporting"]', replacement)
    assert tampered != stored
    store.values[(LINKEDIN_TOKEN_SERVICE, reference)] = tampered


def test_a_recorded_scope_outside_the_purpose_allowlist_blocks_every_read_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    enroll(directory)
    _tamper_recorded_scopes(store, '["r_ads","r_ads_reporting","rw_ads"]')

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)
    assert status.local_state == "UNVERIFIED"
    assert status.granted_scopes == ()
    assert status.refresh_status is None

    # The same guard closes the refresh path, not just the reported status.
    with pytest.raises(AdhelmError) as error:
        lifecycle.begin_linkedin_token_refresh(directory, PROFILE_ID)
    assert error.value.code == "LINKEDIN_TOKEN_STATE_INVALID"
    assert error.value.exit_code == 4


def test_an_edit_profile_still_records_and_reads_back_the_write_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    profile = linkedin_profile(purpose="EDIT", scopes=["r_ads", "r_ads_reporting", "rw_ads"])
    directory = configured_directory(tmp_path, profile)
    edit_scopes = ("r_ads", "r_ads_reporting", "rw_ads")
    slot = lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID, edit_scopes)
    salt = lifecycle.new_identity_salt()
    lifecycle.install_linkedin_token_grant(
        directory,
        slot,
        initial_grant(scopes=edit_scopes),
        client_secret=CLIENT_SECRET,
        identity_salt=salt,
        identity_pseudonym=pseudonym(salt),
    )

    status = lifecycle.linkedin_token_status(directory, PROFILE_ID)

    assert status.purpose == "EDIT"
    assert status.local_state == "READY"
    assert status.granted_scopes == edit_scopes

"""Fake-store coverage for the protected TikTok Ads app auth-code lifecycle.

No test here opens a socket or an OS credential store.  The credential backend
and the mutation lock are in-memory fakes and every assertion is local.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adhelm import config as config_module
from adhelm import tiktok_token_credentials as lifecycle
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.tiktok_token_profiles import (
    TIKTOK_TOKEN_SERVICE,
    is_tiktok_token_reference,
    tiktok_environment_host,
    tiktok_token_reference,
)

ACCESS = "tiktok-fixture-access-value"
APP_SECRET = "tiktok-fixture-app-secret"
APP_ID = "tiktok-fixture-app-id"
AUTH_CODE = "tiktok-fixture-auth-code"
ADVERTISER = "123"
OTHER_ADVERTISER = "456"
ACCESS_REF = "tiktok-read-token"
PROFILE_ID = "tiktok-read"
GENERIC_SERVICE = "adhelm"
SECRETS = (ACCESS, APP_SECRET, APP_ID, AUTH_CODE)


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


def tiktok_profile(**updates: object) -> Profile:
    value: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "provider": "tiktok_ads",
        "account_ref": ADVERTISER,
        "principal_ref": "tiktok-advertiser-local",
        "client_ref": "tiktok-app-local",
        # TikTok publishes no permission vocabulary this build could fetch, so a
        # declared scope here is a local label and never a provider permission.
        "scopes": ["tiktok-local-read"],
        "purpose": "READ",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_STORE",
        "credential_ref": ACCESS_REF,
    }
    value.update(updates)
    return Profile.model_validate(value)


def configured_directory(tmp_path: Path, profile: Profile | None = None) -> Path:
    directory = tmp_path / "data"
    save_configuration(
        directory,
        Configuration(
            profiles=[profile or tiktok_profile()],
            output=OutputPreferences(max_output_bytes=65_536),
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


def grant(**updates: object) -> lifecycle.TikTokTokenGrant:
    value: dict[str, object] = {
        "app_ref": APP_ID,
        "environment": "PRODUCTION",
        "access_token": ACCESS,
        "access_token_expires_at": None,
        "expiry_source": "NOT_DOCUMENTED_LOCALLY",
        "advertiser_ids": (ADVERTISER, OTHER_ADVERTISER),
        "scope_field_returned": True,
    }
    value.update(updates)
    return lifecycle.TikTokTokenGrant(**value)  # type: ignore[arg-type]


def enrolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **grant_updates: object
) -> tuple[Path, MemoryStore, lifecycle.TikTokTokenLifecycleResult]:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    slot = lifecycle.prepare_tiktok_token_enrollment(directory, PROFILE_ID, "PRODUCTION")
    result = lifecycle.install_tiktok_token_grant(
        directory,
        slot,
        grant(**grant_updates),
        app_secret=APP_SECRET,
        identity_salt=lifecycle.new_identity_salt(),
    )
    return directory, store, result


# ------------------------------------------------------------ slot selectors


def test_each_protected_slot_has_its_own_derived_reference() -> None:
    references = {
        slot: tiktok_token_reference(PROFILE_ID, slot)
        for slot in ("APP_SECRET", "ADVERTISER_IDS", "STATE")
    }
    assert len(set(references.values())) == 3
    for reference in references.values():
        assert is_tiktok_token_reference(reference)
        # The "#" separator cannot occur in a configured SafeRef, so a derived
        # selector can never collide with a customer-chosen reference.
        assert "#" in reference
    assert not is_tiktok_token_reference(ACCESS_REF)


def test_a_derived_slot_cannot_even_be_expressed_as_a_configured_reference() -> None:
    # A configured `SafeRef` may not contain "#", and every derived TikTok
    # selector does, so a customer-chosen credential reference can never collide
    # with one in either the credential-store or the local lock namespace.  The
    # profile validator refuses it before the model is even reachable.
    from pydantic import ValidationError

    from adhelm.models import is_reserved_credential_reference

    reference = tiktok_token_reference(PROFILE_ID, "STATE")
    assert "#" in reference
    assert is_reserved_credential_reference(reference)
    assert not is_reserved_credential_reference(ACCESS_REF)
    with pytest.raises(ValidationError):
        tiktok_profile(credential_ref=reference)
    # And the lifecycle refuses any profile that somehow presented one.
    assert is_tiktok_token_reference(reference)


def test_the_sandbox_environment_is_refused_rather_than_pointed_at_a_guessed_host() -> None:
    assert tiktok_environment_host("PRODUCTION") == "business-api.tiktok.com"
    with pytest.raises(AdhelmError) as error:
        tiktok_environment_host("SANDBOX")
    assert error.value.code == "TIKTOK_TOKEN_ENVIRONMENT_NOT_DOCUMENTED"
    with pytest.raises(AdhelmError) as invalid:
        tiktok_environment_host("PROD")
    assert invalid.value.code == "TIKTOK_TOKEN_ENVIRONMENT_INVALID"
    assert invalid.value.exit_code == 2


# ------------------------------------------------------------------- install


def test_installing_a_grant_writes_four_distinct_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store, result = enrolled(tmp_path, monkeypatch)

    assert store.values[(GENERIC_SERVICE, ACCESS_REF)] == ACCESS
    secret_ref = tiktok_token_reference(PROFILE_ID, "APP_SECRET")
    advertisers_ref = tiktok_token_reference(PROFILE_ID, "ADVERTISER_IDS")
    state_ref = tiktok_token_reference(PROFILE_ID, "STATE")
    assert store.values[(TIKTOK_TOKEN_SERVICE, secret_ref)] == APP_SECRET
    assert ADVERTISER in store.values[(TIKTOK_TOKEN_SERVICE, advertisers_ref)]
    # The state slot is non-secret: no token, secret, or auth code is in it.
    state = store.values[(TIKTOK_TOKEN_SERVICE, state_ref)]
    for secret in (ACCESS, APP_SECRET, AUTH_CODE):
        assert secret not in state
    assert result.advertiser_binding == "PROVEN_IN_EXCHANGE_RESPONSE"
    assert result.granted_permissions == "NOT_DOCUMENTED_LOCALLY"
    assert result.refresh_route == "NOT_DOCUMENTED"
    assert lifecycle.tiktok_token_status(directory, PROFILE_ID).local_state == "READY"


def test_a_grant_that_does_not_name_the_configured_advertiser_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    slot = lifecycle.prepare_tiktok_token_enrollment(directory, PROFILE_ID, "PRODUCTION")

    with pytest.raises(AdhelmError) as error:
        lifecycle.install_tiktok_token_grant(
            directory,
            slot,
            grant(advertiser_ids=(OTHER_ADVERTISER,)),
            app_secret=APP_SECRET,
            identity_salt=lifecycle.new_identity_salt(),
        )

    assert error.value.code == "TIKTOK_TOKEN_ADVERTISER_NOT_GRANTED"
    assert error.value.exit_code == 4
    assert store.set_calls == []


def test_an_absent_advertiser_list_is_recorded_as_unproven_not_as_an_empty_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store, result = enrolled(tmp_path, monkeypatch, advertiser_ids=None)

    assert result.advertiser_list_status == "NOT_RETURNED"
    assert result.advertiser_binding == "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"
    assert result.advertiser_pseudonyms == ()
    # The configured advertiser still has a pseudonym; only the binding is
    # unproven.  The advertiser slot is not written at all.
    assert result.bound_advertiser_pseudonym.startswith("tiktok_ads_advertiser_v1.")
    advertisers_ref = tiktok_token_reference(PROFILE_ID, "ADVERTISER_IDS")
    assert (TIKTOK_TOKEN_SERVICE, advertisers_ref) not in store.values
    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)
    assert status.advertiser_slot_present is False
    assert status.advertiser_binding == "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"


def test_a_declared_lifetime_is_tracked_and_an_absent_one_is_never_read_as_no_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expires = datetime.now(UTC) + timedelta(hours=1)
    directory, _store, result = enrolled(
        tmp_path,
        monkeypatch,
        access_token_expires_at=expires,
        expiry_source="PROVIDER_DECLARED",
    )

    assert result.expiry_source == "PROVIDER_DECLARED"
    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)
    assert status.access_token_expired is False
    assert status.access_token_expires_at is not None


def test_an_undocumented_expiry_reports_unknown_rather_than_not_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _store, _result = enrolled(tmp_path, monkeypatch)

    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)

    # `None` is the whole point: a boolean would report an undocumented lifetime
    # as healthy.
    assert status.access_token_expired is None
    assert status.expiry_source == "NOT_DOCUMENTED_LOCALLY"


def test_an_expired_declared_lifetime_is_reported_as_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expires = datetime.now(UTC) - timedelta(seconds=1)
    directory, _store, _result = enrolled(
        tmp_path,
        monkeypatch,
        access_token_expires_at=expires,
        expiry_source="PROVIDER_DECLARED",
    )

    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)

    assert status.access_token_expired is True
    # `refresh` is the operation that needs a usable credential, and it refuses
    # with the authorization exit code whatever the expiry says.
    with pytest.raises(AdhelmError) as error:
        lifecycle.require_tiktok_token_refresh_route(directory, PROFILE_ID)
    assert error.value.exit_code == 4


def test_a_grant_declaring_an_expiry_without_a_time_is_rejected() -> None:
    with pytest.raises(AdhelmError):
        grant(expiry_source="PROVIDER_DECLARED")
    with pytest.raises(AdhelmError):
        grant(access_token_expires_at=datetime.now(UTC))


def test_a_grant_never_serializes_or_reprs_its_secret() -> None:
    value = grant()
    assert repr(value) == "<TikTokTokenGrant redacted>"
    for secret in SECRETS:
        assert secret not in repr(value)
    with pytest.raises(TypeError):
        value.__reduce__()
    with pytest.raises(TypeError):
        value.__getstate__()


def test_a_second_enrollment_refuses_while_any_slot_is_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _store, _result = enrolled(tmp_path, monkeypatch)

    with pytest.raises(AdhelmError) as error:
        lifecycle.prepare_tiktok_token_enrollment(directory, PROFILE_ID, "PRODUCTION")

    assert error.value.code == "TIKTOK_TOKEN_CREDENTIAL_EXISTS"
    assert error.value.exit_code == 3


def test_an_unverifiable_write_is_reported_as_ambiguous_not_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)
    slot = lifecycle.prepare_tiktok_token_enrollment(directory, PROFILE_ID, "PRODUCTION")
    store.drop_writes = True

    with pytest.raises(AdhelmError) as error:
        lifecycle.install_tiktok_token_grant(
            directory,
            slot,
            grant(),
            app_secret=APP_SECRET,
            identity_salt=lifecycle.new_identity_salt(),
        )

    assert error.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert error.value.exit_code == 6


# -------------------------------------------------------------------- status


def test_a_missing_enrollment_is_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)

    assert status.local_state == "MISSING"
    assert status.environment is None
    assert status.advertiser_pseudonyms == ()


def test_a_store_failure_is_raised_and_never_reported_as_an_absent_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStore(MemoryStore):
        def get_password(self, service_name: str, username: str) -> str | None:
            raise OSError("store unavailable")

    store = BrokenStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    with pytest.raises(AdhelmError) as error:
        lifecycle.tiktok_token_status(directory, PROFILE_ID)

    assert error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"


def test_an_interrupted_enrollment_is_never_reported_as_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store, _result = enrolled(tmp_path, monkeypatch)
    state_ref = tiktok_token_reference(PROFILE_ID, "STATE")
    raw = store.values[(TIKTOK_TOKEN_SERVICE, state_ref)]
    store.values[(TIKTOK_TOKEN_SERVICE, state_ref)] = raw.replace(
        '"local_state":"READY"', '"local_state":"ENROLL_STARTED"'
    )

    assert lifecycle.tiktok_token_status(directory, PROFILE_ID).local_state == "UNVERIFIED"


@pytest.mark.parametrize("unwritten_state", ["REAUTHORIZATION_REQUIRED", "INVENTED"])
def test_a_local_state_this_product_never_writes_is_reported_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwritten_state: str
) -> None:
    """TikTok has no refresh path, so only a foreign or tampered record holds one."""
    directory, store, _result = enrolled(tmp_path, monkeypatch)
    state_ref = tiktok_token_reference(PROFILE_ID, "STATE")
    raw = store.values[(TIKTOK_TOKEN_SERVICE, state_ref)]
    tampered = raw.replace('"local_state":"READY"', f'"local_state":"{unwritten_state}"')
    assert tampered != raw
    store.values[(TIKTOK_TOKEN_SERVICE, state_ref)] = tampered

    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)

    assert status.local_state == "UNVERIFIED"
    # The decoder must reject the record outright. A record that decoded and was merely
    # reported UNVERIFIED would still carry its enrolled environment, expiry and binding.
    assert status.environment is None
    assert status.api_version is None
    assert status.access_token_expires_at is None
    assert status.advertiser_list_status is None
    assert status.advertiser_binding is None
    assert status.bound_advertiser_pseudonym is None
    assert status.advertiser_pseudonyms == ()


def test_a_ready_state_without_its_access_token_is_reported_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store, _result = enrolled(tmp_path, monkeypatch)
    store.values.pop((GENERIC_SERVICE, ACCESS_REF))

    status = lifecycle.tiktok_token_status(directory, PROFILE_ID)

    assert status.local_state == "UNVERIFIED"
    assert status.access_token_slot_present is False


def test_an_unreadable_state_slot_is_reported_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store, _result = enrolled(tmp_path, monkeypatch)
    state_ref = tiktok_token_reference(PROFILE_ID, "STATE")
    store.values[(TIKTOK_TOKEN_SERVICE, state_ref)] = "{}"

    assert lifecycle.tiktok_token_status(directory, PROFILE_ID).local_state == "UNVERIFIED"


def test_a_changed_profile_invalidates_the_stored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _store, _result = enrolled(tmp_path, monkeypatch)
    save_configuration(
        directory,
        Configuration(
            profiles=[tiktok_profile(purpose="EDIT")],
            output=OutputPreferences(max_output_bytes=65_536),
        ),
        replace=True,
    )

    with pytest.raises(AdhelmError) as error:
        lifecycle.tiktok_token_status(directory, PROFILE_ID)

    assert error.value.code == "TIKTOK_TOKEN_PROFILE_CHANGED"
    assert error.value.exit_code == 4


def test_state_recording_a_scope_field_never_records_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _directory, store, _result = enrolled(tmp_path, monkeypatch)
    state = store.values[(TIKTOK_TOKEN_SERVICE, tiktok_token_reference(PROFILE_ID, "STATE"))]

    assert '"scope_field_returned":true' in state
    assert '"scope"' not in state


# ------------------------------------------------------------------- refresh


def test_refresh_refuses_with_a_typed_diagnostic_pointing_at_re_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _store, _result = enrolled(tmp_path, monkeypatch)

    with pytest.raises(AdhelmError) as error:
        lifecycle.require_tiktok_token_refresh_route(directory, PROFILE_ID)

    assert error.value.code == "TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED"
    assert error.value.exit_code == 4
    assert "enroll" in error.value.next_action


def test_refresh_on_an_unenrolled_profile_reports_the_missing_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    with pytest.raises(AdhelmError) as error:
        lifecycle.require_tiktok_token_refresh_route(directory, PROFILE_ID)

    assert error.value.code == "TIKTOK_TOKEN_ENROLLMENT_REQUIRED"
    assert error.value.exit_code == 4


# -------------------------------------------------------------- remove-local


def test_remove_local_deletes_only_the_protected_tiktok_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, store, _result = enrolled(tmp_path, monkeypatch)

    profile_digest, removed = lifecycle.remove_local_tiktok_token_slots(directory, PROFILE_ID)

    assert removed == 3
    assert profile_digest == digest(tiktok_profile())
    # The configured access-token slot belongs to the shared `auth remove-local`
    # contract and is deliberately untouched.
    assert store.values[(GENERIC_SERVICE, ACCESS_REF)] == ACCESS
    assert all(service == TIKTOK_TOKEN_SERVICE for service, _ in store.deleted)
    assert lifecycle.tiktok_token_status(directory, PROFILE_ID).local_state == "MISSING"


def test_remove_local_on_an_unenrolled_profile_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path)

    _digest, removed = lifecycle.remove_local_tiktok_token_slots(directory, PROFILE_ID)

    assert removed == 0
    assert store.deleted == []


# ------------------------------------------------------------------ profiles


@pytest.mark.parametrize(
    "updates",
    [
        {"provider": "meta_ads"},
        {"credential_source": "EXPLICIT_CI"},
        {"purpose": "ACTIVATE"},
        {"account_ref": "not-a-number"},
    ],
)
def test_only_a_configured_tiktok_os_store_read_or_edit_profile_is_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, updates: dict[str, object]
) -> None:
    from adhelm.tiktok_token_profiles import select_tiktok_ads_profile

    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path, tiktok_profile(**updates))
    from adhelm.config import load_configuration

    with pytest.raises(AdhelmError):
        select_tiktok_ads_profile(load_configuration(directory), PROFILE_ID)

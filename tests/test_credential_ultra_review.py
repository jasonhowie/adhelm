"""Adversarial local-only tests for credential slot separation and mutation safety."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_credential_setup import FakeStore, _configuration, _profile

from adhelm import credential_setup, credentials
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError
from adhelm.models import Profile


@pytest.mark.parametrize("reference", ["EXPORT-PSEUDONYMIZATION-V1", "Export-Pseudonymization-V1"])
def test_casefolded_export_key_reference_is_rejected_before_store_access(
    reference: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _profile().model_dump()
    data["credential_ref"] = reference
    with pytest.raises(ValidationError):
        Profile.model_validate(data)

    forged = _profile().model_copy(update={"credential_ref": reference})
    monkeypatch.setattr(
        credentials,
        "_backend",
        lambda: pytest.fail("forged export-key alias must fail before keychain access"),
    )
    with pytest.raises(AdhelmError) as caught:
        credentials.credential(forged)
    assert caught.value.code == "PROFILE_INVALID"


def test_casefolded_primary_and_secondary_aliases_are_rejected() -> None:
    data = _profile().model_dump()
    data["secondary_credential_ref"] = data["credential_ref"].upper()
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_casefolded_cross_profile_os_store_alias_fails_before_backend_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _profile()
    second = _profile(provider="meta_ads", primary=first.credential_ref.upper()).model_copy(
        update={"profile_id": "second-profile"}
    )
    configuration = _configuration(first, second)
    monkeypatch.setattr(
        credential_setup,
        "os_store_backend",
        lambda: pytest.fail("aliased configuration must fail before opening the OS store"),
    )
    with pytest.raises(AdhelmError) as caught:
        credential_setup.credential_status(configuration, first.profile_id)
    assert caught.value.code == "CREDENTIAL_REFERENCE_ALIAS"


def test_store_refuses_an_existing_empty_slot_without_prompt_or_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = FakeStore(values={profile.credential_ref: ""})
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(
        credential_setup,
        "prompt_secret_from_terminal",
        lambda: pytest.fail("store must require explicit rotate for an existing slot"),
    )
    with pytest.raises(AdhelmError) as caught:
        credential_setup.store_interactive(_configuration(profile), profile.profile_id, "PRIMARY")
    assert caught.value.code == "CREDENTIAL_ALREADY_EXISTS"
    assert store.values == {profile.credential_ref: ""}


def test_export_key_requires_durable_exact_readback_after_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore(drop_writes=True)
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(credentials.secrets, "token_hex", lambda _: "a" * 64)

    with pytest.raises(AdhelmError) as dropped:
        credentials.export_key()
    assert dropped.value.code == "EXPORT_KEY_UNAVAILABLE"
    assert store.values == {}

    class ReplacingStore(FakeStore):
        def set_password(self, service_name: str, username: str, password: str) -> None:
            super().set_password(service_name, username, "b" * 64)

    replacing = ReplacingStore()
    monkeypatch.setattr(credentials, "_backend", lambda: replacing)
    with pytest.raises(AdhelmError) as replaced:
        credentials.export_key()
    assert replaced.value.code == "EXPORT_KEY_UNAVAILABLE"
    assert replacing.values["export-pseudonymization-v1"] == "b" * 64


def test_export_key_rejects_malformed_store_values_without_leaking_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore(values={"export-pseudonymization-v1": "not-a-private-key"})
    monkeypatch.setattr(credentials, "_backend", lambda: store)

    with pytest.raises(AdhelmError) as malformed:
        credentials.export_key()
    assert malformed.value.code == "EXPORT_KEY_UNAVAILABLE"
    assert "not-a-private-key" not in malformed.value.message


def test_export_key_busy_lock_stops_before_opening_the_credential_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credentials,
        "_backend",
        lambda: pytest.fail("an occupied export-key lock must stop before backend access"),
    )
    with credential_mutation_lock("EXPORT-PSEUDONYMIZATION-V1"):
        with pytest.raises(AdhelmError) as busy:
            credentials.export_key()
    assert busy.value.code == "CREDENTIAL_SETUP_BUSY"


def test_unexpected_os_store_exceptions_are_sanitized_at_every_credential_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedFailureStore(FakeStore):
        def get_password(self, service_name: str, username: str) -> str | None:
            raise RuntimeError("private keychain backend detail")

    monkeypatch.setattr(credentials, "_backend", lambda: UnexpectedFailureStore())
    with pytest.raises(AdhelmError) as credential_failure:
        credentials.credential(_profile())
    assert credential_failure.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "private keychain backend detail" not in credential_failure.value.message

    with pytest.raises(AdhelmError) as export_failure:
        credentials.export_key()
    assert export_failure.value.code == "EXPORT_KEY_UNAVAILABLE"
    assert "private keychain backend detail" not in export_failure.value.message

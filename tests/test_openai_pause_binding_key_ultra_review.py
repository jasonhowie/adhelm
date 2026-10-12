"""Adversarial local-only checks for the OpenAI pause binding-key lifecycle."""

from __future__ import annotations

import secrets
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from pydantic import ValidationError

import adhelm.credential_lock as credential_lock
from adhelm import credential_setup, credentials
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError
from adhelm.models import (
    OPENAI_PAUSE_BINDING_KEY_REFERENCE,
    Configuration,
    OpenAIAccountKeyProfile,
    Profile,
)

_SERVICE = "adhelm"


@dataclass
class _FakeStore:
    values: dict[str, object] = field(default_factory=dict)
    drop_writes: bool = False
    replacement: object | None = None
    read_error: bool = False
    write_error: bool = False
    set_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        assert service_name == _SERVICE
        if self.read_error:
            raise RuntimeError("private binding-store sentinel")
        return cast(str | None, self.values.get(username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        assert service_name == _SERVICE
        if self.write_error:
            raise RuntimeError("private binding-store sentinel")
        self.set_calls.append((service_name, username, password))
        if self.drop_writes:
            return
        self.values[username] = password if self.replacement is None else self.replacement

    def delete_password(self, service_name: str, username: str) -> None:
        assert service_name == _SERVICE
        self.values.pop(username, None)


def _profile(**changes: object) -> OpenAIAccountKeyProfile:
    values: dict[str, object] = {
        "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
        "profile_id": "openai-pause-edit",
        "account_ref": "account_fixture",
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_STORE",
        "credential_ref": "openai-provider-secret-slot",
    }
    values.update(changes)
    return OpenAIAccountKeyProfile.model_validate(values)


def _legacy_profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "legacy-provider-profile",
        "provider": "google_ads",
        "account_ref": "account",
        "principal_ref": "principal",
        "client_ref": "client",
        "scopes": ["read"],
        "credential_source": "OS_STORE",
        "credential_ref": "legacy-primary-slot",
        "secondary_credential_ref": "legacy-secondary-slot",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _redirect_lock_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(credential_lock, "_default_lock_root", lambda: root / "locks")


def test_binding_key_is_separate_reused_and_exactly_32_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_lock_root(monkeypatch, tmp_path)
    store = _FakeStore()
    generated = iter(("a" * 64, "b" * 64))

    def token_hex(bytes_count: int) -> str:
        assert bytes_count == 32
        return next(generated)

    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(secrets, "token_hex", token_hex)

    first = credentials.openai_pause_binding_key(_profile())
    second = credentials.openai_pause_binding_key(_profile())
    projection = credentials.export_key()

    assert first == second == bytes.fromhex("a" * 64)
    assert len(first) == 32
    assert projection == bytes.fromhex("b" * 64)
    assert store.values == {
        OPENAI_PAUSE_BINDING_KEY_REFERENCE: "a" * 64,
        "export-pseudonymization-v1": "b" * 64,
    }
    assert [call[1] for call in store.set_calls] == [
        OPENAI_PAUSE_BINDING_KEY_REFERENCE,
        "export-pseudonymization-v1",
    ]


@pytest.mark.parametrize(
    "stored",
    (
        "private-binding-key-sentinel",
        "g" * 64,
        b"private-binding-key-sentinel",
    ),
)
def test_binding_key_rejects_corrupt_store_values_without_overwrite_or_leakage(
    stored: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_lock_root(monkeypatch, tmp_path)
    store = _FakeStore(values={OPENAI_PAUSE_BINDING_KEY_REFERENCE: stored})
    monkeypatch.setattr(credentials, "_backend", lambda: store)

    with pytest.raises(AdhelmError) as failure:
        credentials.openai_pause_binding_key(_profile())

    assert failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"
    assert store.set_calls == []
    for private_value in ("private-binding-key-sentinel", "g" * 64):
        assert private_value not in str(failure.value)
        assert private_value not in repr(failure.value)


def test_malformed_generated_binding_key_fails_before_store_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_lock_root(monkeypatch, tmp_path)
    generated = "private-generated-binding-key-sentinel"
    store = _FakeStore()
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(secrets, "token_hex", lambda _: generated)

    with pytest.raises(AdhelmError) as failure:
        credentials.openai_pause_binding_key(_profile())

    assert failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"
    assert store.set_calls == [] and store.values == {}
    assert generated not in "".join(traceback.format_exception(failure.value))


def test_binding_key_write_and_readback_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_lock_root(monkeypatch, tmp_path)
    generated = "c" * 64
    monkeypatch.setattr(secrets, "token_hex", lambda _: generated)

    dropped = _FakeStore(drop_writes=True)
    monkeypatch.setattr(credentials, "_backend", lambda: dropped)
    with pytest.raises(AdhelmError) as dropped_failure:
        credentials.openai_pause_binding_key(_profile())
    assert dropped_failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"
    assert dropped.values == {}

    replaced = _FakeStore(replacement="d" * 64)
    monkeypatch.setattr(credentials, "_backend", lambda: replaced)
    with pytest.raises(AdhelmError) as replaced_failure:
        credentials.openai_pause_binding_key(_profile())
    assert replaced_failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"
    assert replaced.values[OPENAI_PAUSE_BINDING_KEY_REFERENCE] == "d" * 64

    failed_write = _FakeStore(write_error=True)
    monkeypatch.setattr(credentials, "_backend", lambda: failed_write)
    with pytest.raises(AdhelmError) as write_failure:
        credentials.openai_pause_binding_key(_profile())
    assert write_failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"

    failed_read = _FakeStore(read_error=True)
    monkeypatch.setattr(credentials, "_backend", lambda: failed_read)
    with pytest.raises(AdhelmError) as read_failure:
        credentials.openai_pause_binding_key(_profile())
    assert read_failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"

    for failure in (
        dropped_failure.value,
        replaced_failure.value,
        write_failure.value,
        read_failure.value,
    ):
        rendered = "".join(traceback.format_exception(failure))
        for private_value in (generated, "d" * 64, "private binding-store sentinel"):
            assert private_value not in rendered


def test_binding_key_lock_is_casefolded_and_stops_before_backend_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_lock_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        credentials,
        "_backend",
        lambda: pytest.fail("occupied binding-key lock must stop before backend access"),
    )

    with credential_mutation_lock(OPENAI_PAUSE_BINDING_KEY_REFERENCE.upper()):
        with pytest.raises(AdhelmError) as busy:
            credentials.openai_pause_binding_key(_profile())

    assert busy.value.code == "CREDENTIAL_SETUP_BUSY"


def test_binding_key_ci_path_never_reads_an_ambient_environment_or_os_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoAmbientOs:
        @property
        def environ(self) -> NoReturn:
            pytest.fail("CI binding-key path must not inspect an ambient environment")

    profile = _profile(credential_source="EXPLICIT_CI", credential_ref="OPENAI_PROVIDER_CREDENTIAL")
    monkeypatch.setattr(credentials, "_backend", lambda: pytest.fail("CI path opened keychain"))
    monkeypatch.setattr(cast(Any, credentials), "os", _NoAmbientOs())

    with pytest.raises(AdhelmError) as failure:
        credentials.openai_pause_binding_key(profile, ci_enabled=True)

    assert failure.value.code == "OPENAI_PAUSE_BINDING_KEY_CI_UNSUPPORTED"


@pytest.mark.parametrize("reference", (OPENAI_PAUSE_BINDING_KEY_REFERENCE,))
@pytest.mark.parametrize("source", ("OS_STORE", "EXPLICIT_CI"))
@pytest.mark.parametrize("slot", ("credential_ref", "secondary_credential_ref"))
def test_binding_reference_is_reserved_in_primary_secondary_and_ci_profile_slots(
    reference: str, source: str, slot: str
) -> None:
    values = _legacy_profile(credential_source=source).model_dump()
    values[slot] = reference.upper()
    with pytest.raises(ValidationError):
        Profile.model_validate(values)

    key_values = _profile(credential_source=source).model_dump()
    key_values["credential_ref"] = reference.upper()
    with pytest.raises(ValidationError):
        OpenAIAccountKeyProfile.model_validate(key_values)


def test_constructed_binding_reference_cannot_reach_provider_or_internal_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_key_profile = _profile().model_copy(
        update={"credential_ref": OPENAI_PAUSE_BINDING_KEY_REFERENCE.upper()}
    )
    forged_provider_profile = _legacy_profile().model_copy(
        update={"credential_ref": OPENAI_PAUSE_BINDING_KEY_REFERENCE.upper()}
    )
    monkeypatch.setattr(
        credentials,
        "_backend",
        lambda: pytest.fail("constructed reserved reference reached the keychain"),
    )

    with pytest.raises(AdhelmError) as binding_failure:
        credentials.openai_pause_binding_key(forged_key_profile)
    with pytest.raises(AdhelmError) as provider_failure:
        credentials.credential(forged_provider_profile)
    assert binding_failure.value.code == provider_failure.value.code == "PROFILE_INVALID"

    configuration = Configuration.model_construct(profiles=[forged_key_profile])
    monkeypatch.setattr(
        credential_setup,
        "os_store_backend",
        lambda: pytest.fail("constructed reserved configuration reached the keychain"),
    )
    with pytest.raises(AdhelmError) as configuration_failure:
        credential_setup.credential_status(configuration, forged_key_profile.profile_id)
    assert configuration_failure.value.code == "PROFILE_CONFIGURATION_INVALID"

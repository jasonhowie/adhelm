from __future__ import annotations

import getpass
import warnings
from dataclasses import dataclass, field
from io import StringIO
from typing import cast

import pytest

import adhelm.credential_setup as setup
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile


@dataclass
class FakeStore:
    values: dict[str, str] = field(default_factory=dict)
    fail: bool = False
    drop_writes: bool = False
    drop_deletes: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        assert service_name == "adhelm"
        if self.fail:
            raise RuntimeError("private backend detail")
        return self.values.get(username)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        assert service_name == "adhelm"
        if self.fail:
            raise RuntimeError("private backend detail")
        if not self.drop_writes:
            self.values[username] = password

    def delete_password(self, service_name: str, username: str) -> None:
        assert service_name == "adhelm"
        if self.fail:
            raise RuntimeError("private backend detail")
        if not self.drop_deletes:
            self.values.pop(username, None)


def _profile(
    *, provider: str = "google_ads", source: str = "OS_STORE", primary: str = "primary-reference"
) -> Profile:
    values = {
        "profile_id": "configured-profile",
        "provider": provider,
        "account_ref": "account",
        "principal_ref": "principal",
        "client_ref": "client",
        "scopes": ["read"],
        "credential_source": source,
        "credential_ref": primary,
    }
    if provider == "google_ads":
        values["secondary_credential_ref"] = "secondary-reference"
    return Profile.model_validate(values)


def _configuration(*profiles: Profile) -> Configuration:
    return Configuration.model_validate(
        {"profiles": [profile.model_dump() for profile in profiles]}
    )


def test_status_store_rotate_and_local_remove_bind_exact_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)
    profile = _profile()
    configuration = _configuration(profile)
    monkeypatch.setattr(setup, "prompt_secret_from_terminal", lambda: "secret-value")
    assert [
        item.state for item in setup.credential_status(configuration, "configured-profile")
    ] == [
        "MISSING",
        "MISSING",
    ]
    stored = setup.store_interactive(configuration, "configured-profile", "PRIMARY")
    assert stored.operation == "STORE"
    assert store.values == {"primary-reference": "secret-value"}
    with pytest.raises(AdhelmError, match="already exists"):
        setup.store_interactive(configuration, "configured-profile", "PRIMARY")
    monkeypatch.setattr(setup, "prompt_secret_from_terminal", lambda: "different-secret")
    rotated = setup.rotate_interactive(configuration, "configured-profile", "PRIMARY")
    assert rotated.operation == "ROTATE"
    assert store.values["primary-reference"] == "different-secret"
    removed = setup.local_remove(configuration, "configured-profile", "PRIMARY")
    assert removed.operation == "LOCAL_REMOVE_ONLY"
    assert removed.remote_revocation_verified is False
    assert "primary-reference" not in store.values


def test_google_secondary_and_ci_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)
    google = _profile()
    monkeypatch.setattr(setup, "prompt_secret_from_terminal", lambda: "developer-token")
    result = setup.store_interactive(_configuration(google), "configured-profile", "SECONDARY")
    assert result.credential_ref == "secondary-reference"
    meta = _profile(provider="meta_ads")
    with pytest.raises(AdhelmError, match="only for Google"):
        setup.store_interactive(_configuration(meta), "configured-profile", "SECONDARY")
    ci = _profile(source="EXPLICIT_CI")
    assert (
        setup.credential_status(_configuration(ci), "configured-profile")[0].state == "CI_MANAGED"
    )
    with pytest.raises(AdhelmError, match="explicit-CI"):
        setup.store_interactive(_configuration(ci), "configured-profile", "PRIMARY")


def test_reserved_or_aliased_references_fail_before_store_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)
    reserved = _profile().model_copy(update={"credential_ref": "export-pseudonymization-v1"})
    invalid_configuration = _configuration(_profile()).model_copy(update={"profiles": [reserved]})
    with pytest.raises(AdhelmError, match="valid configured profiles"):
        setup.credential_status(invalid_configuration, "configured-profile")
    first = _profile()
    second = _profile(provider="meta_ads", primary="primary-reference").model_copy(
        update={"profile_id": "other-profile"}
    )
    with pytest.raises(AdhelmError, match="unique"):
        setup.store_interactive(_configuration(first, second), "configured-profile", "PRIMARY")
    assert store.values == {}


def test_ci_references_have_a_separate_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _profile(source="EXPLICIT_CI")
    second = _profile(source="EXPLICIT_CI").model_copy(update={"profile_id": "other-profile"})
    monkeypatch.setattr(
        setup,
        "os_store_backend",
        lambda: pytest.fail("CI status must not open the OS credential store"),
    )
    result = setup.credential_status(_configuration(first, second), "configured-profile")
    assert [item.state for item in result] == ["CI_MANAGED", "CI_MANAGED"]


def test_setup_detects_concurrent_change_and_unverified_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    configuration = _configuration(profile)
    store = FakeStore()
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)

    def conflicting_prompt() -> str:
        store.values["primary-reference"] = "other-local-value"
        return "new-secret"

    monkeypatch.setattr(setup, "prompt_secret_from_terminal", conflicting_prompt)
    with pytest.raises(AdhelmError, match="changed while waiting"):
        setup.store_interactive(configuration, "configured-profile", "PRIMARY")
    assert store.values["primary-reference"] == "other-local-value"

    store = FakeStore(drop_writes=True)
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(setup, "prompt_secret_from_terminal", lambda: "new-secret")
    with pytest.raises(AdhelmError) as failure:
        setup.store_interactive(configuration, "configured-profile", "PRIMARY")
    assert failure.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert failure.value.exit_code == 6
    assert "new-secret" not in failure.value.message

    store = FakeStore(values={"primary-reference": "old-secret"}, drop_deletes=True)
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)
    with pytest.raises(AdhelmError) as removal:
        setup.local_remove(configuration, "configured-profile", "PRIMARY")
    assert removal.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert removal.value.exit_code == 6


def test_hidden_prompt_and_backend_errors_never_expose_secret_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonTty(StringIO):
        def isatty(self) -> bool:
            return False

    class Tty(StringIO):
        def isatty(self) -> bool:
            return True

    with pytest.raises(AdhelmError, match="interactive terminal"):
        setup.prompt_secret_from_terminal(stdin=NonTty(), stderr=NonTty())
    prompts: list[tuple[str, StringIO]] = []

    def hidden_prompt(prompt: str, stream: StringIO) -> str:
        prompts.append((prompt, stream))
        return "secret-value"

    terminal = Tty()
    assert (
        setup.prompt_secret_from_terminal(
            stdin=terminal, stderr=terminal, getpass_function=hidden_prompt
        )
        == "secret-value"
    )
    assert prompts == [("Credential secret: ", terminal)]

    def warning_prompt(prompt: str, stream: StringIO) -> str:
        warnings.warn("echo fallback", getpass.GetPassWarning, stacklevel=2)
        return "secret-value"

    with pytest.raises(AdhelmError, match="hidden credential prompt"):
        setup.prompt_secret_from_terminal(
            stdin=terminal, stderr=terminal, getpass_function=warning_prompt
        )
    store = FakeStore(fail=True)
    monkeypatch.setattr(setup, "os_store_backend", lambda: store)
    with pytest.raises(AdhelmError) as failure:
        setup.credential_status(_configuration(_profile()), "configured-profile")
    assert "private backend detail" not in failure.value.message
    with pytest.raises(AdhelmError) as invalid:
        monkeypatch.setattr(setup, "prompt_secret_from_terminal", lambda: "bad\nsecret")
        setup.store_interactive(_configuration(_profile()), "configured-profile", "PRIMARY")
    assert "bad" not in invalid.value.message
    invalid_runtime_slot = cast(setup.CredentialSlot, "UNEXPECTED")
    with pytest.raises(AdhelmError) as invalid_slot:
        setup.local_remove(_configuration(_profile()), "configured-profile", invalid_runtime_slot)
    assert invalid_slot.value.code == "INVALID_CREDENTIAL_SLOT"

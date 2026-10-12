"""Fake-store coverage for the closed Meta token profile/bundle lifecycle."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from adhelm import config as config_module
from adhelm import credential_setup, credentials
from adhelm import meta_token_credentials as token_credentials
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import (
    MetaTokenCredentialStatus,
    enroll_meta_token_profile,
    install_meta_token_credentials,
    meta_token_credential_status,
    prepare_meta_token_enrollment,
    prepare_meta_token_rotation,
    rotate_meta_token_credentials,
)
from adhelm.meta_token_profiles import (
    MetaAccountTokenProfileDraft,
    finalize_meta_account_token_profile,
)
from adhelm.models import Configuration, Profile


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    set_calls: list[tuple[str, str, str]] = field(default_factory=list)
    drop_writes: bool = False
    replace_readback: str | None = None

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.replace_readback is not None:
            value = self.replace_readback
            self.replace_readback = None
            return value
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.set_calls.append((service_name, username, password))
        if not self.drop_writes:
            self.values[(service_name, username)] = password


def _draft(**updates: object) -> MetaAccountTokenProfileDraft:
    value: dict[str, object] = {
        "profile_id": "meta-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    value.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(value)


def _install_fake_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())


def _bundle_value(store: _MemoryStore, profile_id: str = "meta-read") -> str:
    profile = finalize_meta_account_token_profile(_draft(profile_id=profile_id))
    return store.values[(token_credentials._SERVICE, profile.credential_ref)]


def test_enrollment_writes_one_bound_private_bundle_with_exact_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)

    result = enroll_meta_token_profile(
        tmp_path,
        _draft(),
        subject_token_supplier=lambda: "subject-fixture-token",
        inspector_token_supplier=lambda: "inspector-fixture-token",
    )

    profile = finalize_meta_account_token_profile(_draft())
    assert result.operation == "ENROLL"
    assert result.provider_authentication_verified is False
    assert result.account_access_verified is False
    assert result.execution_authority is False
    assert result.profile_configuration_digest == token_credentials.digest(profile)
    assert load_configuration(tmp_path).profiles == [profile]
    assert [(service, reference) for service, reference, _ in store.set_calls] == [
        (token_credentials._SERVICE, profile.credential_ref)
    ]
    bundle = token_credentials._decode_bundle(_bundle_value(store))
    assert bundle.profile_id == profile.profile_id
    assert bundle.profile_digest == token_credentials.digest(profile)
    assert bundle.subject_token == "subject-fixture-token"
    assert bundle.inspector_token == "inspector-fixture-token"
    assert len(bundle.binding_key) == 32
    assert meta_token_credential_status(tmp_path, profile.profile_id) == MetaTokenCredentialStatus(
        profile_configuration_digest=token_credentials.digest(profile),
        purpose="READ",
        local_state="READY",
        # Derived from the installed bundle's own private key and subject
        # token; it names no token and is never emitted in a status result.
        credential_binding_ref=token_credentials._credential_binding_ref(bundle),
    )
    assert "fixture-token" not in repr(result)


def test_rotation_replaces_the_complete_bundle_without_rewriting_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    enroll_meta_token_profile(
        tmp_path,
        _draft(),
        subject_token_supplier=lambda: "subject-before",
        inspector_token_supplier=lambda: "inspector-before",
    )
    profile = finalize_meta_account_token_profile(_draft())
    before = token_credentials._decode_bundle(_bundle_value(store))
    profile_digest = token_credentials.digest(profile)

    result = rotate_meta_token_credentials(
        tmp_path,
        profile.profile_id,
        subject_token_supplier=lambda: "subject-after",
        inspector_token_supplier=lambda: "inspector-after",
    )

    after = token_credentials._decode_bundle(_bundle_value(store))
    assert result.operation == "ROTATE"
    assert load_configuration(tmp_path).profiles == [profile]
    assert result.profile_configuration_digest == profile_digest
    assert after.profile_digest == before.profile_digest == profile_digest
    assert (after.subject_token, after.inspector_token) == ("subject-after", "inspector-after")
    assert after.binding_key != before.binding_key
    assert len(store.set_calls) == 2


@pytest.mark.parametrize("field", ["currency", "timezone"])
def test_bundle_binding_rejects_changed_customer_declared_account_expectations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    enroll_meta_token_profile(
        tmp_path,
        _draft(),
        subject_token_supplier=lambda: "subject-token",
        inspector_token_supplier=lambda: "inspector-token",
    )
    profile = finalize_meta_account_token_profile(_draft())
    bundle = token_credentials._decode_bundle(_bundle_value(store))
    replacement = "EUR" if field == "currency" else "America/Chicago"
    changed = profile.model_copy(update={field: replacement})

    with pytest.raises(AdhelmError) as failure:
        token_credentials._bind_bundle(bundle, changed)

    assert failure.value.code == "META_TOKEN_CREDENTIAL_CHANGED"


@pytest.mark.parametrize("stored", [None, "not-a-canonical-private-bundle"])
def test_rotation_does_not_overwrite_missing_or_unverified_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str | None
) -> None:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    profile = finalize_meta_account_token_profile(_draft())
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    if stored is not None:
        store.values[(token_credentials._SERVICE, profile.credential_ref)] = stored

    with pytest.raises(AdhelmError) as failure:
        prepare_meta_token_rotation(tmp_path, profile.profile_id)

    assert failure.value.code in {
        "META_TOKEN_CREDENTIAL_MISSING",
        "META_TOKEN_CREDENTIAL_UNVERIFIED",
    }
    assert store.set_calls == []


def test_stale_configuration_or_bundle_blocks_before_a_new_bundle_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    slot = prepare_meta_token_enrollment(tmp_path, _draft())
    existing = Profile(
        profile_id="other-read",
        provider="google_ads",
        account_ref="1234567890",
        principal_ref="principal",
        client_ref="client",
        scopes=["read"],
        credential_ref="other-primary",
        secondary_credential_ref="other-secondary",
    )
    save_configuration(tmp_path, Configuration(profiles=[existing]), replace=True)
    with pytest.raises(AdhelmError) as changed:
        install_meta_token_credentials(
            tmp_path,
            slot,
            subject_token_supplier=lambda: "subject-token",
            inspector_token_supplier=lambda: "inspector-token",
        )
    assert changed.value.code == "CONFIGURATION_CHANGED"
    assert store.set_calls == []

    other_root = tmp_path / "bundle-change"
    next_slot = prepare_meta_token_enrollment(other_root, _draft())
    profile = finalize_meta_account_token_profile(_draft())
    store.values[(token_credentials._SERVICE, profile.credential_ref)] = "external-value"
    with pytest.raises(AdhelmError) as bundle_changed:
        install_meta_token_credentials(
            other_root,
            next_slot,
            subject_token_supplier=lambda: "subject-token",
            inspector_token_supplier=lambda: "inspector-token",
        )
    assert bundle_changed.value.code == "META_TOKEN_CREDENTIAL_CHANGED"
    assert store.set_calls == []


def test_post_write_readback_failure_is_ambiguous_and_does_not_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore(drop_writes=True)
    _install_fake_store(monkeypatch, store)

    with pytest.raises(AdhelmError) as failure:
        enroll_meta_token_profile(
            tmp_path,
            _draft(),
            subject_token_supplier=lambda: "subject-token",
            inspector_token_supplier=lambda: "inspector-token",
        )

    assert failure.value.code == "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED"
    assert failure.value.exit_code == 6
    profile = finalize_meta_account_token_profile(_draft())
    assert load_configuration(tmp_path).profiles == [profile]
    assert meta_token_credential_status(tmp_path, profile.profile_id).local_state == "MISSING"
    assert "subject-token" not in failure.value.message
    assert "inspector-token" not in failure.value.next_action


def test_untrusted_supplier_and_store_errors_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_backend_resolver = token_credentials._credential_backend
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)

    def secret_supplier_error() -> str:
        raise AdhelmError(
            "subject-token-secret-code",
            "subject-token-secret-message",
            next_action="subject-token-secret-next-action",
        )

    with pytest.raises(AdhelmError) as prompt_error:
        enroll_meta_token_profile(
            tmp_path,
            _draft(),
            subject_token_supplier=secret_supplier_error,
            inspector_token_supplier=lambda: "inspector-token",
        )
    assert prompt_error.value.code == "META_TOKEN_CREDENTIAL_INPUT_INVALID"
    assert "subject-token-secret" not in str(prompt_error.value)
    assert "subject-token-secret" not in prompt_error.value.next_action
    assert not (tmp_path / "config.json").exists()

    class _SecretBackendErrorStore(_MemoryStore):
        def get_password(self, service_name: str, username: str) -> str | None:
            raise AdhelmError(
                "inspector-token-secret-code",
                "inspector-token-secret-message",
                next_action="inspector-token-secret-next-action",
            )

    _install_fake_store(monkeypatch, _SecretBackendErrorStore())
    with pytest.raises(AdhelmError) as backend_error:
        prepare_meta_token_enrollment(tmp_path / "store-error", _draft())
    assert backend_error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "inspector-token-secret" not in str(backend_error.value)
    assert "inspector-token-secret" not in backend_error.value.next_action

    def secret_backend_factory() -> _MemoryStore:
        raise AdhelmError(
            "backend-secret-code",
            "backend-secret-message",
            next_action="backend-secret-next-action",
        )

    monkeypatch.setattr(token_credentials, "_credential_backend", credential_backend_resolver)
    monkeypatch.setattr(credentials, "_backend", secret_backend_factory)
    with pytest.raises(AdhelmError) as factory_error:
        prepare_meta_token_enrollment(tmp_path / "factory-error", _draft())
    assert factory_error.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "backend-secret" not in str(factory_error.value)
    assert "backend-secret" not in factory_error.value.next_action


def test_lone_surrogate_store_value_is_a_fixed_unverified_bundle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    profile = finalize_meta_account_token_profile(_draft())
    store.values[(token_credentials._SERVICE, profile.credential_ref)] = "\ud800"

    with pytest.raises(AdhelmError) as failure:
        prepare_meta_token_enrollment(tmp_path, _draft())

    assert failure.value.code == "META_TOKEN_CREDENTIAL_UNVERIFIED"
    assert "surrogate" not in failure.value.message.casefold()

    # Status is deliberately diagnostic-only: malformed local bytes are a
    # bounded state, while a backend outage remains an operational error.
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    status = meta_token_credential_status(tmp_path, profile.profile_id)
    assert status.local_state == "UNVERIFIED"


def test_generic_credential_paths_refuse_meta_bundle_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = finalize_meta_account_token_profile(_draft())
    configuration = Configuration(profiles=[profile])

    def forbidden() -> None:
        pytest.fail("generic credential paths must not probe the Meta private bundle")

    monkeypatch.setattr(credentials, "_backend", forbidden)
    monkeypatch.setattr(credential_setup, "os_store_backend", forbidden)
    with pytest.raises(AdhelmError) as generic:
        credentials.credential(profile)
    assert generic.value.code == "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED"
    with pytest.raises(AdhelmError) as setup:
        credential_setup.credential_status(configuration, profile.profile_id)
    assert setup.value.code == "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED"

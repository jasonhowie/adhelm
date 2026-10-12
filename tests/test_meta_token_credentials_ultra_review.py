"""Independent fake-only review of the Meta token credential lifecycle.

These tests use synthetic credentials, an in-memory protected-store seam, and
an in-memory HTTP transport.  They never prompt a terminal, contact Meta, or
open an OS credential store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from adhelm import config as config_module
from adhelm import credential_setup, credentials
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm import meta_token_workflow as workflow
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.meta_token_credentials import (
    MetaTokenCredentialInstallSlot,
    enroll_meta_token_profile,
    install_meta_token_credentials,
    prepare_meta_token_rotation,
    rotate_meta_token_credentials,
)
from adhelm.meta_token_profiles import (
    MetaAccountTokenProfileDraft,
    finalize_meta_account_token_profile,
)
from adhelm.models import Configuration, MetaAccountTokenProfile

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_SUBJECT = "independent-subject-token"
_INSPECTOR = "independent-inspector-token"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    drop_next_write: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes.append((service_name, username, password))
        if self.drop_next_write:
            self.drop_next_write = False
            return
        self.values[(service_name, username)] = password


def _draft(**updates: object) -> MetaAccountTokenProfileDraft:
    value: dict[str, object] = {
        "profile_id": "independent-meta-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
        "currency": "USD",
        "timezone": "UTC",
    }
    value.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(value)


def _profile(**updates: object) -> MetaAccountTokenProfile:
    return finalize_meta_account_token_profile(_draft(**updates))


def _wire_store(
    monkeypatch: pytest.MonkeyPatch,
    store: _MemoryStore,
    *,
    active_locks: list[str] | None = None,
) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    if active_locks is None:
        monkeypatch.setattr(
            token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
        )
        monkeypatch.setattr(
            config_module, "credential_mutation_lock", lambda _reference: nullcontext()
        )
        return

    @contextmanager
    def lock(reference: str) -> Iterator[None]:
        active_locks.append(reference)
        try:
            yield
        finally:
            assert active_locks[-1] == reference
            active_locks.pop()

    monkeypatch.setattr(token_credentials, "credential_mutation_lock", lock)
    monkeypatch.setattr(config_module, "credential_mutation_lock", lock)


def _enroll(root: Path, **draft_updates: object):
    return enroll_meta_token_profile(
        root,
        _draft(**draft_updates),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )


def _inspection_response() -> dict[str, object]:
    return {
        "data": {
            "app_id": "67890",
            "user_id": "54321",
            "is_valid": True,
            "type": "USER",
            "scopes": ["ads_read", "public_profile"],
            "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
            "expires_at": int((_NOW + timedelta(minutes=5)).timestamp()),
            "data_access_expires_at": int((_NOW + timedelta(days=1)).timestamp()),
        }
    }


def _install_inspection_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.Client

    def factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(inspection.httpx, "Client", factory)
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)


def test_hidden_token_suppliers_run_after_configuration_and_bundle_locks_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    active_locks: list[str] = []
    _wire_store(monkeypatch, store, active_locks=active_locks)
    supplied: list[str] = []

    def subject() -> str:
        assert active_locks == []
        supplied.append("subject")
        return _SUBJECT

    def inspector() -> str:
        assert active_locks == []
        supplied.append("inspector")
        return _INSPECTOR

    result = enroll_meta_token_profile(
        tmp_path,
        _draft(),
        subject_token_supplier=subject,
        inspector_token_supplier=inspector,
    )

    assert supplied == ["subject", "inspector"]
    assert active_locks == []
    assert result.local_state == "READY"
    assert result.provider_authentication_verified is False
    assert result.account_access_verified is False
    assert result.execution_authority is False


def test_changed_rotation_slot_cannot_be_overwritten_after_hidden_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    _enroll(tmp_path)
    profile = _profile()
    slot = prepare_meta_token_rotation(tmp_path, profile.profile_id)
    replacement = token_credentials._new_bundle(
        profile, "external-subject-token", "external-inspector-token"
    )._encoded()
    store.values[(token_credentials._SERVICE, profile.credential_ref)] = replacement
    supplied: list[str] = []

    with pytest.raises(AdhelmError) as raised:
        install_meta_token_credentials(
            tmp_path,
            slot,
            subject_token_supplier=lambda: supplied.append("subject") or _SUBJECT,
            inspector_token_supplier=lambda: supplied.append("inspector") or _INSPECTOR,
        )

    assert raised.value.code == "META_TOKEN_CREDENTIAL_CHANGED"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
    assert supplied == ["subject", "inspector"]
    assert len(store.writes) == 1
    assert store.values[(token_credentials._SERVICE, profile.credential_ref)] == replacement


def test_declared_profile_metadata_drift_blocks_rotation_before_bundle_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    _enroll(tmp_path)
    profile = _profile()
    slot = prepare_meta_token_rotation(tmp_path, profile.profile_id)
    current = load_configuration(tmp_path)
    changed_profile = profile.model_copy(update={"currency": "EUR"})
    save_configuration(
        tmp_path, current.model_copy(update={"profiles": [changed_profile]}), replace=True
    )

    with pytest.raises(AdhelmError) as raised:
        install_meta_token_credentials(
            tmp_path,
            slot,
            subject_token_supplier=lambda: _SUBJECT,
            inspector_token_supplier=lambda: _INSPECTOR,
        )

    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert len(store.writes) == 1
    assert load_configuration(tmp_path).profiles == [changed_profile]


def test_rotation_readback_ambiguity_does_not_retry_or_erase_retained_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    _enroll(tmp_path)
    profile = _profile()
    retained = store.values[(token_credentials._SERVICE, profile.credential_ref)]
    store.drop_next_write = True

    with pytest.raises(AdhelmError) as raised:
        rotate_meta_token_credentials(
            tmp_path,
            profile.profile_id,
            subject_token_supplier=lambda: "replacement-subject-token",
            inspector_token_supplier=lambda: "replacement-inspector-token",
        )

    assert raised.value.code == "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
    assert len(store.writes) == 2
    assert store.values[(token_credentials._SERVICE, profile.credential_ref)] == retained
    public = str(raised.value)
    assert "replacement-subject-token" not in public
    assert "replacement-inspector-token" not in public
    assert load_configuration(tmp_path).profiles == [profile]


def test_generic_credential_operations_refuse_the_dedicated_bundle_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    configuration = Configuration(profiles=[profile])
    backend_calls = 0

    def forbidden_backend() -> object:
        nonlocal backend_calls
        backend_calls += 1
        pytest.fail("generic routes must not open a dedicated Meta bundle")

    monkeypatch.setattr(credentials, "_backend", forbidden_backend)
    monkeypatch.setattr(credential_setup, "os_store_backend", forbidden_backend)
    monkeypatch.setattr(
        credential_setup,
        "credential_mutation_lock",
        lambda _reference: pytest.fail("generic routes must not lock a dedicated Meta bundle"),
    )

    operations = (
        lambda: credentials.credential(profile),
        lambda: credential_setup.credential_status(configuration, profile.profile_id),
        lambda: credential_setup.store_interactive(configuration, profile.profile_id),
        lambda: credential_setup.rotate_interactive(configuration, profile.profile_id),
        lambda: credential_setup.local_remove(configuration, profile.profile_id),
    )
    for operation in operations:
        with pytest.raises(AdhelmError) as raised:
            operation()
        assert raised.value.code == "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED"
        assert raised.value.exit_code == ExitCode.AUTHORIZATION

    forged = profile.model_copy(update={"timezone": "not-an-iana-zone"})
    with pytest.raises(AdhelmError) as invalid:
        credentials.credential(forged)
    assert invalid.value.code == "PROFILE_INVALID"
    assert backend_calls == 0


def test_constructed_install_slot_fails_before_any_hidden_input_or_store_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = object.__new__(MetaTokenCredentialInstallSlot)
    supplier_calls = 0
    monkeypatch.setattr(
        token_credentials,
        "_credential_backend",
        lambda: pytest.fail("constructed slot must not reach the protected store"),
    )

    def supplier() -> str:
        nonlocal supplier_calls
        supplier_calls += 1
        pytest.fail("constructed slot must not prompt for a token")

    with pytest.raises(AdhelmError) as raised:
        install_meta_token_credentials(
            tmp_path,
            forged,
            subject_token_supplier=supplier,
            inspector_token_supplier=supplier,
        )

    assert raised.value.code == "META_TOKEN_CREDENTIAL_SNAPSHOT_INVALID"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
    assert supplier_calls == 0


def test_inspection_http_runs_outside_local_locks_and_never_claims_account_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    active_locks: list[str] = []
    _wire_store(monkeypatch, store, active_locks=active_locks)
    _enroll(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert active_locks == []
        calls.append(request)
        return httpx.Response(200, json=_inspection_response())

    _install_inspection_transport(monkeypatch, handler)
    observation = workflow.inspect_configured_meta_token(tmp_path, "independent-meta-read")

    assert len(calls) == 1
    assert active_locks == []
    assert observation.client_application_ref == "67890"
    assert observation.scopes == ("ads_read", "public_profile")
    assert not hasattr(observation, "account_access_verified")
    assert not hasattr(observation, "execution_authority")
    assert not hasattr(observation, "currency")
    assert not hasattr(observation, "timezone")
    public = repr(observation)
    assert "12345" not in public
    assert _SUBJECT not in public
    assert _INSPECTOR not in public


def test_valid_slot_replacement_during_http_blocks_a_redacted_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    _enroll(tmp_path)
    profile = _profile()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        store.values[(token_credentials._SERVICE, profile.credential_ref)] = (
            token_credentials._new_bundle(
                profile, "changed-subject-token", "changed-inspector-token"
            )._encoded()
        )
        return httpx.Response(200, json=_inspection_response())

    _install_inspection_transport(monkeypatch, handler)
    with pytest.raises(AdhelmError) as raised:
        workflow.inspect_configured_meta_token(tmp_path, profile.profile_id)

    assert calls == 1
    assert raised.value.code == "META_TOKEN_CREDENTIAL_CHANGED"
    assert "changed-subject-token" not in str(raised.value)
    assert "changed-inspector-token" not in str(raised.value)

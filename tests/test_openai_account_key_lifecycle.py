# ruff: noqa: E501
"""Fake-store coverage for the OpenAI Ads account-key status and rotation lifecycle.

Every case is offline: the credential store is an in-memory fake and the only
provider call is a `httpx.MockTransport` identity read.  No test contacts
OpenAI, and no case reconciles against OpenAI Ads Manager.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from adhelm import openai_account_key_lifecycle as lifecycle

# `adhelm.cli` already registers the `auth openai-key` commands on `auth_app` at
# import time; registering them a second time here would install a duplicate
# command for every name.
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, OpenAIAccountKeyProfile, OutputPreferences

runner = CliRunner()

PROFILE_ID = "openai-read"
KEY_REF = "openai-read-key"
ACCOUNT = "account_fixture"
OLD_KEY = "openai-fixture-old-key"
NEW_KEY = "openai-fixture-new-key"
GENERIC_SERVICE = "adhelm"


@dataclass
class MemoryStore:
    """A fake OS credential store; it records every read and write in order."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)
    set_calls: list[tuple[str, str]] = field(default_factory=list)
    drop_writes: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.set_calls.append((service_name, username))
        if not self.drop_writes:
            self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile(**updates: object) -> OpenAIAccountKeyProfile:
    value: dict[str, object] = {
        "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
        "profile_id": PROFILE_ID,
        "account_ref": ACCOUNT,
        "purpose": "READ",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_STORE",
        "credential_ref": KEY_REF,
    }
    value.update(updates)
    return OpenAIAccountKeyProfile.model_validate(value)


def _directory(tmp_path: Path, profile: OpenAIAccountKeyProfile | None = None) -> Path:
    directory = tmp_path / "data"
    save_configuration(
        directory,
        Configuration(
            profiles=[profile or _profile()],
            output=OutputPreferences(max_output_bytes=16_384),
        ),
    )
    return directory


def _account(account_id: str = ACCOUNT) -> dict[str, Any]:
    return {
        "id": account_id,
        "name": "Fixture account",
        "url": "https://example.invalid",
        "preview_url": None,
        "status": "active",
        "timezone": "UTC",
        "currency_code": "USD",
        "review": {"status": "approved"},
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    store: MemoryStore,
    *,
    events: list[str] | None = None,
    responses: Any = None,
    key: str = NEW_KEY,
) -> list[str]:
    """Replace the store, the lock, the hidden prompt, and the HTTP client."""
    ordering = events if events is not None else []

    @contextmanager
    def recording_lock(reference: str) -> Iterator[None]:
        ordering.append(f"lock:{reference}")
        try:
            yield
        finally:
            ordering.append(f"unlock:{reference}")

    def handler(request: httpx.Request) -> httpx.Response:
        ordering.append("http:ad_account")
        if responses is not None:
            return responses(request)
        return httpx.Response(200, json=_account())

    monkeypatch.setattr(lifecycle, "_backend", lambda: store)
    monkeypatch.setattr(lifecycle, "credential_mutation_lock", recording_lock)
    monkeypatch.setattr(lifecycle, "_prompt_key", lambda: key)
    monkeypatch.setattr(lifecycle, "_require_interactive", lambda _value: None)
    monkeypatch.setattr(
        lifecycle, "_open_read_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return ordering


@contextmanager
def _no_lock(_reference: str) -> Iterator[None]:
    yield


def _stored(store: MemoryStore) -> MemoryStore:
    store.values[(GENERIC_SERVICE, KEY_REF)] = OLD_KEY
    return store


# --- status -------------------------------------------------------------------


def test_status_reports_a_missing_key_without_contacting_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    _install(monkeypatch, store)
    monkeypatch.setattr(
        lifecycle, "_open_read_client", lambda: pytest.fail("status must make no request")
    )
    status = lifecycle.openai_account_key_status(_directory(tmp_path), PROFILE_ID)
    assert status.local_state == "MISSING"
    assert status.key_slot_present is False
    assert status.identity_pseudonym is None
    assert status.last_verified_at is None
    assert status.account_ref == f"openai_ads.account.{ACCOUNT}"
    assert status.provider_revocation_verified is False


def test_status_reports_a_stored_but_never_verified_key_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    status = lifecycle.openai_account_key_status(_directory(tmp_path), PROFILE_ID)
    assert status.local_state == "UNVERIFIED"
    assert status.key_slot_present is True
    assert status.identity_pseudonym is None


def test_status_never_reveals_the_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    status = lifecycle.openai_account_key_status(_directory(tmp_path), PROFILE_ID)
    assert OLD_KEY not in json.dumps(status, default=str)


def test_status_describes_provider_revocation_and_never_calls_removal_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    status = lifecycle.openai_account_key_status(_directory(tmp_path), PROFILE_ID)
    guidance = status.revocation_guidance
    assert "Settings tab of OpenAI Ads Manager" in guidance
    assert "Removing the local copy" in guidance
    assert "cannot verify a provider-side revocation" in guidance
    # Local deletion is never presented as revocation.
    assert "revoke the key locally" not in guidance.lower()
    assert status.provider_revocation_verified is False


def test_status_reports_a_ci_managed_value_without_a_local_presence_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(credential_source="EXPLICIT_CI", credential_ref="OPENAI_ADS_KEY")
    store = MemoryStore()
    _install(monkeypatch, store)
    status = lifecycle.openai_account_key_status(_directory(tmp_path, profile), PROFILE_ID)
    assert status.local_state == "CI_MANAGED"
    assert status.key_slot_present is False


def test_status_requires_an_openai_account_key_profile(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as error:
        lifecycle.openai_account_key_status(_directory(tmp_path), "not-configured")
    assert error.value.code == "OPENAI_ACCOUNT_KEY_PROFILE_REQUIRED"


# --- rotation ordering --------------------------------------------------------


def test_rotation_verifies_the_new_key_before_it_replaces_the_old_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    events = _install(monkeypatch, store)
    result = lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    assert result.operation == "ROTATE"
    assert result.identity_reverified_before_replacement is True
    assert store.values[(GENERIC_SERVICE, KEY_REF)] == NEW_KEY
    # The identity read happens after the first locked snapshot and before the
    # write, and no lock is open while it runs.
    http = events.index("http:ad_account")
    opened = [index for index, item in enumerate(events) if item.startswith("lock:")]
    closed = [index for index, item in enumerate(events) if item.startswith("unlock:")]
    assert any(index < http for index in closed)
    assert any(index > http for index in opened)
    for open_index, close_index in zip(opened, closed, strict=True):
        assert not open_index < http < close_index


def test_a_failed_verification_leaves_the_previous_key_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(
        monkeypatch,
        store,
        responses=lambda _request: httpx.Response(401, json={"error": "expired"}),
    )
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    assert error.value.code == "OPENAI_ADS_KEY_INVALID_OR_EXPIRED"
    assert error.value.exit_code == int(ExitCode.AUTHORIZATION)
    assert store.values[(GENERIC_SERVICE, KEY_REF)] == OLD_KEY
    assert (GENERIC_SERVICE, KEY_REF) not in store.set_calls


def test_a_replacement_for_a_different_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    # A second rotation whose key resolves to a different ad account is refused
    # by the adapter's own expected-identity check before anything is stored.
    _install(
        monkeypatch,
        store,
        responses=lambda _request: httpx.Response(200, json=_account("account_other")),
        key="openai-fixture-third-key",
    )
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    assert error.value.code == "IDENTITY_MISMATCH"
    assert store.values[(GENERIC_SERVICE, KEY_REF)] == NEW_KEY


def test_a_pseudonym_mismatch_refuses_and_preserves_the_previous_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    # Corrupt only the retained pseudonym: the provider answers for the
    # configured account, but the retained verification record disagrees.
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    key = (lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)
    state = json.loads(store.values[key])
    state["identity_pseudonym"] = "openai_ads_account_v1." + "b" * 64
    store.values[key] = json.dumps(state, separators=(",", ":"), sort_keys=True)
    _install(monkeypatch, store, key="openai-fixture-third-key")
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    assert error.value.code == "OPENAI_ACCOUNT_KEY_IDENTITY_MISMATCH"
    assert error.value.exit_code == int(ExitCode.AUTHORIZATION)
    assert "previous key was left in place" in error.value.next_action
    assert store.values[(GENERIC_SERVICE, KEY_REF)] == NEW_KEY


def test_a_concurrent_local_change_refuses_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())

    def swap(_request: httpx.Request) -> httpx.Response:
        # Another local process replaced the slot while this rotation verified.
        store.values[(GENERIC_SERVICE, KEY_REF)] = "openai-fixture-someone-elses-key"
        return httpx.Response(200, json=_account())

    _install(monkeypatch, store, responses=swap)
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    assert error.value.code == "CREDENTIAL_CONCURRENT_CHANGE"
    assert store.values[(GENERIC_SERVICE, KEY_REF)] == "openai-fixture-someone-elses-key"


def test_rotation_requires_an_existing_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    _install(monkeypatch, store)
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    assert error.value.code == "OPENAI_ACCOUNT_KEY_MISSING"


def test_rotating_to_the_same_value_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    events = _install(monkeypatch, store, key=OLD_KEY)
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    assert error.value.code == "OPENAI_ACCOUNT_KEY_UNCHANGED"
    assert "http:ad_account" not in events


def test_rotation_requires_an_explicit_local_interactive_handoff(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID)
    assert error.value.code == "EXPLICIT_LOCAL_ROTATION_REQUIRED"
    assert error.value.exit_code == int(ExitCode.AUTHORIZATION)


def test_a_ci_managed_profile_has_no_local_rotation_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(credential_source="EXPLICIT_CI", credential_ref="OPENAI_ADS_KEY")
    _install(monkeypatch, MemoryStore())
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(
            _directory(tmp_path, profile), PROFILE_ID, interactive=True
        )
    assert error.value.code == "OPENAI_ACCOUNT_KEY_CI_ROTATION_UNSUPPORTED"


def test_an_unproven_store_write_is_reported_rather_than_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    store.drop_writes = True
    _install(monkeypatch, store)
    with pytest.raises(AdhelmError) as error:
        lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    assert error.value.code == "CREDENTIAL_CHANGE_UNVERIFIED"


def test_no_rotation_diagnostic_or_result_contains_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    result = lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    text = json.dumps(result, default=str)
    assert OLD_KEY not in text and NEW_KEY not in text
    # The retained verification record holds no key material either.
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    retained = store.values[(lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)]
    assert OLD_KEY not in retained and NEW_KEY not in retained

    for failure in (401, 403, 429, 500):
        _install(
            monkeypatch,
            store,
            responses=lambda _request, status=failure: httpx.Response(
                status, json={"error": NEW_KEY}
            ),
            key="openai-fixture-fourth-key",
        )
        with pytest.raises(AdhelmError) as error:
            lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
        combined = f"{error.value.code}{error.value.message}{error.value.next_action}"
        assert NEW_KEY not in combined
        assert "openai-fixture-fourth-key" not in combined


def test_a_successful_rotation_records_a_stable_masked_identity_and_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    first = lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    status = lifecycle.openai_account_key_status(directory, PROFILE_ID)
    assert status.local_state == "READY"
    assert status.identity_pseudonym == first.identity_pseudonym
    assert status.last_verified_at == first.verified_at
    assert status.last_verified_api_version == "v1"
    assert first.identity_pseudonym.startswith("openai_ads_account_v1.")
    assert ACCOUNT not in first.identity_pseudonym

    _install(monkeypatch, store, key="openai-fixture-third-key")
    second = lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    # The salt is retained, so the same ad account keeps the same label.
    assert second.identity_pseudonym == first.identity_pseudonym


def test_the_state_slot_is_derived_and_never_echoes_the_profile_identifier() -> None:
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    assert PROFILE_ID not in reference
    assert reference.startswith("openai-account-key-state.v1.")
    assert reference != lifecycle.openai_account_key_state_reference("other-profile")


def test_a_corrupted_verification_record_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    store.values[(lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)] = "{not json"
    with pytest.raises(AdhelmError) as error:
        lifecycle.openai_account_key_status(_directory(tmp_path), PROFILE_ID)
    assert error.value.code == "OPENAI_ACCOUNT_KEY_STATE_INVALID"


# --- CLI ----------------------------------------------------------------------


def test_the_status_command_emits_the_bounded_local_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    result = runner.invoke(
        app,
        ["--data-dir", str(directory), "auth", "openai-key", "status", "--profile", PROFILE_ID],
    )
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "auth openai-key status"
    data = envelope["data"]
    assert data["local_state"] == "READY"
    assert data["provider_revocation_verified"] is False
    assert data["execution_authority"] is False
    assert data["identity_pseudonym"].startswith("openai_ads_account_v1.")
    assert OLD_KEY not in result.stdout and NEW_KEY not in result.stdout


def test_the_rotate_command_emits_the_verified_rotation_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "openai-key",
            "rotate",
            "--profile",
            PROFILE_ID,
            "--interactive",
        ],
    )
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "auth openai-key rotate"
    data = envelope["data"]
    assert data["operation"] == "ROTATE"
    assert data["identity_reverified_before_replacement"] is True
    assert data["previous_key_revoked_at_provider"] is False
    assert "Settings tab of OpenAI Ads Manager" in data["revocation_guidance"]
    assert NEW_KEY not in result.stdout


def test_the_rotate_command_reports_an_expired_key_with_exit_code_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(
        monkeypatch,
        store,
        responses=lambda _request: httpx.Response(401, json={"error": "expired"}),
    )
    directory = _directory(tmp_path)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "openai-key",
            "rotate",
            "--profile",
            PROFILE_ID,
            "--interactive",
        ],
    )
    assert result.exit_code == int(ExitCode.AUTHORIZATION)
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "OPENAI_ADS_KEY_INVALID_OR_EXPIRED"
    assert NEW_KEY not in result.stdout and OLD_KEY not in result.stdout


# --- the verification record is bound to the key it verified ------------------


def test_the_retained_record_binds_to_the_key_and_holds_no_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    lifecycle.rotate_openai_account_key(_directory(tmp_path), PROFILE_ID, interactive=True)
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    state = json.loads(store.values[(lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)])
    assert state["schema_version"] == 2
    # A keyed digest under the record's own local salt, never the key and never
    # an unkeyed hash of it.
    assert state["key_binding"] != NEW_KEY
    assert NEW_KEY not in json.dumps(state)
    import hashlib

    assert state["key_binding"] != hashlib.sha256(NEW_KEY.encode()).hexdigest()


def test_a_record_written_under_an_older_schema_is_not_read_as_a_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbound record cannot say which key it verified, so it fails closed."""
    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    key = (lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)
    state = json.loads(store.values[key])
    del state["key_binding"]
    state["schema_version"] = 1
    store.values[key] = json.dumps(state, separators=(",", ":"), sort_keys=True)
    with pytest.raises(AdhelmError) as error:
        lifecycle.openai_account_key_status(directory, PROFILE_ID)
    assert error.value.code == "OPENAI_ACCOUNT_KEY_STATE_INVALID"


def test_local_removal_clears_the_derived_verification_record_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auth remove-local` on this profile clears both slots, or fails.

    Leaving the record behind would leave a masked identity and a verification
    instant for a key this machine no longer holds, and the next stored key
    would inherit them.
    """
    from adhelm import credential_setup

    store = _stored(MemoryStore())
    _install(monkeypatch, store)
    directory = _directory(tmp_path)
    lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)
    reference = lifecycle.openai_account_key_state_reference(PROFILE_ID)
    assert (lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference) in store.values

    monkeypatch.setattr(credential_setup, "_backend_or_error", lambda: store)
    monkeypatch.setattr(credential_setup, "credential_mutation_lock", _no_lock)
    result = credential_setup.local_remove(
        Configuration(profiles=[_profile()], output=OutputPreferences(max_output_bytes=16_384)),
        PROFILE_ID,
        "PRIMARY",
    )
    assert result.state == "LOCAL_REMOVED"
    assert result.remote_revocation_verified is False
    assert (GENERIC_SERVICE, KEY_REF) not in store.values
    assert (lifecycle.OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference) not in store.values
    status = lifecycle.openai_account_key_status(directory, PROFILE_ID)
    assert status.local_state == "MISSING"
    assert status.identity_pseudonym is None

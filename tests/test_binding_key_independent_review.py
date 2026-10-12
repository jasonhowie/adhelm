"""Independent fake-store review of the OpenAI pause binding-key lifecycle."""

from __future__ import annotations

import multiprocessing
import traceback
from pathlib import Path

import pytest

import adhelm.credential_lock as credential_lock
from adhelm import credentials
from adhelm.errors import AdhelmError
from adhelm.models import (
    OPENAI_PAUSE_BINDING_KEY_REFERENCE,
    OpenAIAccountKeyProfile,
)

_SERVICE = "adhelm"
_PROCESS_TIMEOUT_SECONDS = 10


def _profile() -> OpenAIAccountKeyProfile:
    return OpenAIAccountKeyProfile(
        profile_kind="OPENAI_ADS_ACCOUNT_KEY",
        profile_id="binding-key-review",
        account_ref="account_fixture",
        purpose="EDIT",
        currency="USD",
        timezone="UTC",
        credential_source="OS_STORE",
        credential_ref="openai-provider-secret-slot",
    )


def _binding_key_worker(
    role: str,
    lock_root: str,
    state_path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    backend_probe: str,
    wait_for_release: bool,
    results: multiprocessing.queues.Queue[str],
) -> None:
    """Call the real binding-key helper with a shared fake OS-store item."""
    state = Path(state_path)
    probe = Path(backend_probe)

    class FileStore:
        def get_password(self, service_name: str, username: str) -> str | None:
            assert service_name == _SERVICE
            assert username == OPENAI_PAUSE_BINDING_KEY_REFERENCE
            if not state.exists():
                if wait_for_release:
                    entered.set()
                    if not release.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                        raise RuntimeError("review fixture release timed out")
                else:
                    probe.write_text("backend accessed", encoding="utf-8")
                return None
            return state.read_text(encoding="utf-8")

        def set_password(self, service_name: str, username: str, password: str) -> None:
            assert service_name == _SERVICE
            assert username == OPENAI_PAUSE_BINDING_KEY_REFERENCE
            state.write_text(password, encoding="utf-8")

    credential_lock._default_lock_root = lambda: Path(lock_root)
    credentials._backend = lambda: FileStore()
    credentials.secrets.token_hex = lambda count: "a" * (count * 2)
    try:
        key = credentials.openai_pause_binding_key(_profile())
        results.put(f"{role}:OK:{len(key)}")
    except AdhelmError as error:
        results.put(f"{role}:{error.code}")


def test_binding_key_initialization_is_serialized_across_processes_with_fake_store(
    tmp_path: Path,
) -> None:
    """A competing process must not reach the shared fake backend while held."""
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    results = context.Queue()
    lock_root = tmp_path / "locks"
    state = tmp_path / "binding-key-store"
    backend_probe = tmp_path / "second-backend-probe"
    first = context.Process(
        target=_binding_key_worker,
        args=(
            "first",
            str(lock_root),
            str(state),
            entered,
            release,
            str(backend_probe),
            True,
            results,
        ),
    )
    second = context.Process(
        target=_binding_key_worker,
        args=(
            "second",
            str(lock_root),
            str(state),
            entered,
            release,
            str(backend_probe),
            False,
            results,
        ),
    )
    first.start()
    try:
        assert entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
        second.start()
        assert results.get(timeout=_PROCESS_TIMEOUT_SECONDS) == "second:CREDENTIAL_SETUP_BUSY"
        assert not backend_probe.exists()
        release.set()
        assert results.get(timeout=_PROCESS_TIMEOUT_SECONDS) == "first:OK:32"
    finally:
        release.set()
        for worker in (first, second):
            worker.join(timeout=_PROCESS_TIMEOUT_SECONDS)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=_PROCESS_TIMEOUT_SECONDS)
    assert first.exitcode == second.exitcode == 0
    assert len(state.read_text(encoding="utf-8")) == 64


def test_binding_key_backend_failure_suppresses_private_error_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_detail = "PRIVATE-BINDING-BACKEND-DETAIL"
    monkeypatch.setattr(credential_lock, "_default_lock_root", lambda: tmp_path / "locks")

    def failing_backend() -> object:
        raise RuntimeError(private_detail)

    monkeypatch.setattr(credentials, "_backend", failing_backend)
    with pytest.raises(AdhelmError) as failure:
        credentials.openai_pause_binding_key(_profile())

    rendered = "".join(traceback.format_exception(failure.value))
    assert failure.value.code == "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE"
    assert private_detail not in str(failure.value)
    assert private_detail not in rendered


def test_constructed_profile_with_private_object_fails_before_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_detail = "PRIVATE-CONSTRUCTED-PROFILE-DETAIL"

    class UntrustedReference:
        def __repr__(self) -> str:
            return private_detail

    forged = _profile().model_copy(update={"credential_ref": UntrustedReference()})
    monkeypatch.setattr(
        credentials,
        "_backend",
        lambda: pytest.fail("malformed profile reached the credential backend"),
    )
    with pytest.raises(AdhelmError) as failure:
        credentials.openai_pause_binding_key(forged)

    rendered = "".join(traceback.format_exception(failure.value))
    assert failure.value.code == "PROFILE_INVALID"
    assert private_detail not in str(failure.value)
    assert private_detail not in rendered

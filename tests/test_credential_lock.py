from __future__ import annotations

import hashlib
import multiprocessing
import os
import stat
import sys
from pathlib import Path

import pytest

import adhelm.credential_lock as credential_lock
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError

_REQUIRES_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: these cases drive POSIX mode bits, FIFOs and chmod. The Windows counterparts "
        "(DACL, owner, junction and hard-link rejection) are in "
        "tests/test_windows_filesystem.py."
    ),
)


def _filename(reference: str) -> str:
    namespace = f"adhelm\x00{reference.casefold()}".encode()
    return f"{hashlib.sha256(namespace).hexdigest()}.lock"


def _simulated_empty_store_worker(
    root: str,
    reference: str,
    start: multiprocessing.synchronize.Event,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue[str],
    store_path: str,
) -> None:
    start.wait(timeout=5)
    try:
        with credential_mutation_lock(reference, root_override=Path(root)):
            path = Path(store_path)
            if path.exists():
                result.put("existing")
            else:
                path.write_text("written", encoding="utf-8")
                result.put("wrote")
            acquired.set()
            release.wait(timeout=5)
    except AdhelmError as exc:
        result.put("busy" if exc.code == "CREDENTIAL_SETUP_BUSY" else exc.code)


def test_separate_processes_serialize_an_empty_simulated_os_store(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    acquired = context.Event()
    release = context.Event()
    result = context.Queue()
    root = tmp_path / "locks"
    simulated_store = tmp_path / "empty-store-item"
    workers = [
        context.Process(
            target=_simulated_empty_store_worker,
            args=(
                str(root),
                "Primary-Reference",
                start,
                acquired,
                release,
                result,
                str(simulated_store),
            ),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    assert acquired.wait(timeout=10)
    outcomes = sorted(result.get(timeout=10) for _ in workers)
    release.set()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert outcomes == ["busy", "wrote"]
    assert simulated_store.read_text(encoding="utf-8") == "written"


def test_busy_lock_returns_before_a_second_mutation_can_start(tmp_path: Path) -> None:
    with credential_mutation_lock("Primary-Reference", root_override=tmp_path / "locks"):
        with pytest.raises(AdhelmError) as failure:
            with credential_mutation_lock("primary-reference", root_override=tmp_path / "locks"):
                pytest.fail("the nonblocking lock must not be acquired")
    assert failure.value.code == "CREDENTIAL_SETUP_BUSY"


def test_lock_is_private_stable_and_does_not_expose_reference(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    reference = "credential-reference"
    with credential_mutation_lock(reference, root_override=root):
        pass
    path = root / _filename(reference)
    inode = path.stat().st_ino
    assert path.is_file()
    assert path.read_bytes() == b""
    assert reference not in path.name
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    with credential_mutation_lock(reference, root_override=root):
        pass
    assert path.stat().st_ino == inode


def test_default_root_can_be_hermetically_redirected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "redirected-default"
    monkeypatch.setattr(credential_lock, "_default_lock_root", lambda: root)
    with credential_mutation_lock("reference"):
        pass
    assert (root / _filename("reference")).is_file()


@_REQUIRES_POSIX_MODES
def test_symlink_fifo_and_permissive_paths_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(AdhelmError) as symlink_failure:
        with credential_mutation_lock("reference", root_override=linked_root):
            pass
    assert symlink_failure.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    root = tmp_path / "locks"
    root.mkdir(mode=0o700)
    fifo = root / _filename("reference")
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(AdhelmError) as fifo_failure:
        with credential_mutation_lock("reference", root_override=root):
            pass
    assert fifo_failure.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    file_root = tmp_path / "file-permissions"
    file_root.mkdir(mode=0o700)
    lockfile = file_root / _filename("reference")
    lockfile.write_bytes(b"")
    os.chmod(lockfile, 0o644)
    with pytest.raises(AdhelmError) as file_permissions_failure:
        with credential_mutation_lock("reference", root_override=file_root):
            pass
    assert file_permissions_failure.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    hardlink_root = tmp_path / "hardlink"
    hardlink_root.mkdir(mode=0o700)
    linked_lock = hardlink_root / _filename("reference")
    linked_lock.write_bytes(b"")
    os.chmod(linked_lock, 0o600)
    os.link(linked_lock, hardlink_root / "second-name")
    with pytest.raises(AdhelmError) as hardlink_failure:
        with credential_mutation_lock("reference", root_override=hardlink_root):
            pass
    assert hardlink_failure.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    permissive_root = tmp_path / "permissive"
    permissive_root.mkdir(mode=0o755)
    os.chmod(permissive_root, 0o755)
    with pytest.raises(AdhelmError) as permissions_failure:
        with credential_mutation_lock("reference", root_override=permissive_root):
            pass
    assert permissions_failure.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"


@_REQUIRES_POSIX_MODES
def test_nonsticky_writable_ancestor_cannot_host_a_lock_root(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    os.chmod(unsafe_parent, 0o777)
    with pytest.raises(AdhelmError) as unsafe_failure:
        with credential_mutation_lock("reference", root_override=unsafe_parent / "locks"):
            pass
    assert unsafe_failure.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"
    assert not (unsafe_parent / "locks").exists()

    sticky_parent = tmp_path / "sticky-parent"
    sticky_parent.mkdir(mode=0o700)
    os.chmod(sticky_parent, 0o1777)
    with credential_mutation_lock("reference", root_override=sticky_parent / "locks"):
        pass
    assert (sticky_parent / "locks" / _filename("reference")).is_file()


def test_unsupported_platform_fails_before_lock_path_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "plan9")
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(AdhelmError) as failure:
        with credential_mutation_lock("reference", root_override=tmp_path / "locks"):
            pass
    assert failure.value.code == "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED"
    assert not (tmp_path / "locks").exists()


def test_cleanup_failure_is_sanitized_and_does_not_mask_active_operation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failed_unlock(*args: object, **kwargs: object) -> None:
        raise OSError("private lock backend detail")

    monkeypatch.setattr(credential_lock.portalocker, "unlock", failed_unlock)
    with pytest.raises(AdhelmError) as cleanup:
        with credential_mutation_lock("reference", root_override=tmp_path / "locks"):
            pass
    assert cleanup.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert cleanup.value.exit_code == 6
    assert "private lock backend detail" not in cleanup.value.message

    with pytest.raises(RuntimeError, match="operation failure"):
        with credential_mutation_lock("different-reference", root_override=tmp_path / "locks"):
            raise RuntimeError("operation failure")

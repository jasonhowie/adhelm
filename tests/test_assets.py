from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from adhelm import assets
from adhelm.errors import AdhelmError

DATA = b"local immutable image bytes; the caller owns decoding"
SHA = hashlib.sha256(DATA).hexdigest()

_REQUIRES_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: these cases drive POSIX mode bits, FIFOs, chmod, directory fsync or renaming a "
        "held directory. The Windows counterparts (DACL privacy, pinned directories, reparse "
        "points) are in tests/test_windows_filesystem.py."
    ),
)

_REQUIRES_POSIX_PRIMITIVES = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: the missing-POSIX-primitive refusal (O_NOFOLLOW and friends) has no Windows "
        "counterpart: the POSIX primitive gate does not exist there, because the vault uses "
        "windows_filesystem pinned handles instead (tests/test_windows_filesystem.py)."
    ),
)


def stored(root: Path) -> Path:
    return assets.store_image(root, DATA, SHA)


def test_private_atomic_roundtrip_and_idempotent_reuse(tmp_path: Path) -> None:
    root = tmp_path / "new" / "private"
    target = stored(root)
    before = target.stat()
    assert assets.read_image(root, SHA) == DATA
    assert stored(root) == target
    assert target.stat().st_ino == before.st_ino
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700
    assert target.stat().st_nlink == 1
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("digest", ["../escape", "A" * 64, "0" * 63, "0" * 65, True, None])
def test_bad_digest_fails_before_filesystem(tmp_path: Path, digest: object) -> None:
    root = tmp_path / "not-created"
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_DIGEST_MISMATCH"):
        assets.store_image(root, DATA, digest)  # type: ignore[arg-type]
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_DIGEST_MISMATCH"):
        assets.read_image(root, digest)  # type: ignore[arg-type]
    assert not root.exists()


def test_missing_read_does_not_create_storage(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_NOT_AVAILABLE"):
        assets.read_image(root, SHA)
    assert not root.exists()


def test_corruption_is_preserved_without_replacement(tmp_path: Path) -> None:
    target = stored(tmp_path)
    target.write_bytes(b"tampered bytes")
    for operation in (lambda: stored(tmp_path), lambda: assets.read_image(tmp_path, SHA)):
        with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_CORRUPT"):
            operation()
        assert target.read_bytes() == b"tampered bytes"
    assert len(list(target.parent.iterdir())) == 1


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink", "public"])
@_REQUIRES_POSIX_MODES
def test_nonprivate_or_nonregular_object_rejected_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    target = stored(tmp_path)
    if kind == "hardlink":
        os.link(target, tmp_path / "extra-link")
    elif kind == "public":
        target.chmod(0o644)
    else:
        target.unlink()
        if kind == "symlink":
            target.symlink_to(tmp_path / "outside")
        elif kind == "fifo":
            os.mkfifo(target, 0o600)
        else:
            target.mkdir()
    real_open = os.open

    def guarded_open(path: object, *args: object, **kwargs: object) -> int:
        assert path != SHA, "Nonregular or exposed object was opened"
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(assets.os, "open", guarded_open)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "UNSAFE_ASSET_PATH"):
        assets.read_image(tmp_path, SHA)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "UNSAFE_ASSET_PATH"):
        stored(tmp_path)


@_REQUIRES_POSIX_MODES
def test_raced_pipe_open_is_nonblocking_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = stored(tmp_path)
    real_open = os.open
    opened: list[int] = []

    def swap_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == SHA:
            target.unlink()
            os.mkfifo(target, 0o600)
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(assets.os, "open", swap_open)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "UNSAFE_ASSET_PATH"):
        assets.read_image(tmp_path, SHA)
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            "replace",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason=(
                    "O3.1: Windows refuses to replace a file that is open for reading, so the swap "
                    "cannot happen; see tests/test_windows_filesystem.py::"
                    "test_replacing_a_file_that_is_being_read_is_refused_by_the_system"
                ),
            ),
        ),
        "delete",
        "rewrite",
        "grow",
    ],
)
def test_observed_read_changes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    target = stored(tmp_path)
    real_read = os.read
    changed = False

    def changing_read(descriptor: int, maximum: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, maximum)
        if not changed:
            changed = True
            if change == "delete":
                target.unlink()
            elif change == "replace":
                replacement = target.parent / "replacement"
                replacement.write_bytes(DATA)
                replacement.chmod(0o600)
                replacement.replace(target)
            elif change == "rewrite":
                target.write_bytes(b"x" * len(DATA))
            else:
                with target.open("ab") as handle:
                    handle.write(b"growth")
        return chunk

    monkeypatch.setattr(assets.os, "read", changing_read)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_CHANGED"):
        assets.read_image(tmp_path, SHA)


@_REQUIRES_POSIX_MODES
def test_directory_replacement_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = stored(tmp_path)
    real_read = os.read
    changed = False

    def replace_parent(descriptor: int, maximum: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, maximum)
        if not changed:
            changed = True
            target.parent.rename(target.parent.with_name("moved"))
            target.parent.mkdir(mode=0o700)
        return chunk

    monkeypatch.setattr(assets.os, "read", replace_parent)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_CHANGED"):
        assets.read_image(tmp_path, SHA)
    assert not target.exists()


@_REQUIRES_POSIX_MODES
def test_parent_symlink_and_public_vault_rejected(tmp_path: Path) -> None:
    other = tmp_path / "outside"
    other.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(other, target_is_directory=True)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "UNSAFE_ASSET_PATH"):
        stored(link)
    assert list(other.iterdir()) == []
    target = stored(tmp_path / "private")
    target.parent.chmod(0o755)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "UNSAFE_ASSET_PATH"):
        assets.read_image(tmp_path / "private", SHA)


def test_file_fsync_failure_cleans_only_temporary_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def fail_file(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("PRIVATE-SENTINEL-DO-NOT-ECHO")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(assets.os, "fsync", fail_file)
        with pytest.raises(
            AdhelmError, check=lambda error: error.code == "ASSET_STORAGE_FAILED"
        ) as failure:
            stored(tmp_path)
        assert "PRIVATE-SENTINEL" not in str(failure.value)
    assert list((tmp_path / "assets" / "sha256").iterdir()) == []
    assert stored(tmp_path).read_bytes() == DATA


@_REQUIRES_POSIX_MODES
def test_directory_fsync_failure_retains_published_bytes_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    target = tmp_path / "assets" / "sha256" / SHA

    def fail_directory(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and target.exists():
            raise OSError("synthetic directory fsync error")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(assets.os, "fsync", fail_directory)
        with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_STORAGE_FAILED"):
            stored(tmp_path)
    assert target.read_bytes() == DATA
    assert list(target.parent.iterdir()) == [target]
    assert stored(tmp_path) == target


@_REQUIRES_POSIX_PRIMITIVES
def test_missing_platform_primitive_fails_before_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "new"
    monkeypatch.setattr(assets.os, "O_NOFOLLOW", 0, raising=False)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_STORAGE_UNSUPPORTED"):
        stored(root)
    assert not root.exists()


def test_oversized_existing_blob_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = stored(tmp_path)
    with target.open("r+b") as handle:
        handle.truncate(assets.MAX_IMAGE_BYTES + 1)

    def no_read(*args: object) -> bytes:
        raise AssertionError("oversized asset was read")

    monkeypatch.setattr(assets.os, "read", no_read)
    with pytest.raises(AdhelmError, check=lambda error: error.code == "ASSET_CORRUPT"):
        assets.read_image(tmp_path, SHA)

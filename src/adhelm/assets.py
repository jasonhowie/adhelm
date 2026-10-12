"""Private, bounded image storage with digest-verified, no-follow access.

Reads prove the selected local bytes at that observation. Historical asset
records do not promise continued availability. Corrupt objects are not replaced.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from adhelm.creative import MAX_IMAGE_BYTES
from adhelm.directory_access import (
    Directory,
    PinnedDirectory,
    create_private_child,
    fstat,
    link_child,
    open_child_for_read,
    stat_child,
    unlink_child,
)
from adhelm.errors import AdhelmError

if sys.platform == "win32":
    from adhelm import windows_filesystem

_DIR_FD_OPERATIONS = (os.open, os.stat, os.mkdir, os.unlink, os.link)
_NOFOLLOW_OPERATIONS = (os.stat, os.link)


def _failure(code: str) -> AdhelmError:
    messages = {
        "ASSET_DIGEST_MISMATCH": "Asset bytes do not match their declared digest.",
        "UNSAFE_ASSET_PATH": "Asset storage requires private regular files and no symbolic links.",
        "ASSET_CORRUPT": "Stored asset bytes failed bounded digest verification.",
        "ASSET_CHANGED": "The selected asset storage changed during access.",
        "ASSET_NOT_AVAILABLE": "The selected image is missing from the local asset vault.",
        "ASSET_STORAGE_UNSUPPORTED": "This platform lacks required safe asset storage primitives.",
        "ASSET_STORAGE_FAILED": "Local asset storage could not complete the operation.",
    }
    return AdhelmError(code, messages[code], exit_code=2)


def _validate_digest(value: str) -> None:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise _failure("ASSET_DIGEST_MISMATCH")


def _require_platform() -> None:
    if sys.platform == "win32":
        return
    if (
        os.name != "posix"
        or not all(getattr(os, name, 0) for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY"))
        or not all(operation in os.supports_dir_fd for operation in _DIR_FD_OPERATIONS)
        or not all(operation in os.supports_follow_symlinks for operation in _NOFOLLOW_OPERATIONS)
    ):
        raise _failure("ASSET_STORAGE_UNSUPPORTED")


def _required_os_flag(name: str) -> int:
    value = getattr(os, name, None)
    if type(value) is not int or not value:
        raise _failure("ASSET_STORAGE_UNSUPPORTED")
    return value


def _optional_os_flag(name: str) -> int:
    value = getattr(os, name, 0)
    return value if type(value) is int else 0


def _current_uid() -> int:
    if sys.platform == "win32":
        # Windows ownership is checked together with the DACL behind the privacy
        # bits that ``windows_filesystem`` reports; ``st_uid`` is then always 0.
        return 0
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise _failure("ASSET_STORAGE_UNSUPPORTED")
    value = getuid()
    if type(value) is not int:
        raise _failure("ASSET_STORAGE_UNSUPPORTED")
    return value


def _set_private_mode(descriptor: int, mode: int) -> None:
    if sys.platform == "win32":
        # The temporary is created with a protected private DACL.
        return
    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        raise _failure("ASSET_STORAGE_UNSUPPORTED")
    fchmod(descriptor, mode)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_inode(left, right) and (
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
        left.st_mode,
        left.st_uid,
        left.st_nlink,
    ) == (
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
        right.st_mode,
        right.st_uid,
        right.st_nlink,
    )


def _check_tree(nodes: list[tuple[int, str, int]]) -> None:
    for index, (parent, name, child) in enumerate(nodes):
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
        except OSError:
            raise _failure("ASSET_CHANGED") from None
        if not stat.S_ISDIR(current.st_mode) or not _same_inode(current, opened):
            raise _failure("ASSET_CHANGED")
        if index >= len(nodes) - 2 and (
            current.st_uid != _current_uid() or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise _failure("ASSET_CHANGED")


@contextmanager
def _vault(root: Path, *, create: bool) -> Iterator[tuple[Directory, Path]]:
    _require_platform()
    # Do not normalize a traversal across a potentially replaced directory.
    if ".." in root.parts:
        raise _failure("UNSAFE_ASSET_PATH")
    absolute = root.absolute()
    directory = absolute / "assets" / "sha256"
    if sys.platform == "win32":
        with _vault_windows(directory, create=create) as pinned:
            yield pinned, directory
        return
    descriptors: list[int] = []
    nodes: list[tuple[int, str, int]] = []
    flags = (
        os.O_RDONLY
        | _required_os_flag("O_DIRECTORY")
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_NONBLOCK")
    )
    flags |= _optional_os_flag("O_CLOEXEC")
    try:
        parent = os.open(absolute.anchor, flags)
        descriptors.append(parent)
        names = [*absolute.parts[1:], "assets", "sha256"]
        for index, name in enumerate(names):
            try:
                selected = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise _failure("ASSET_NOT_AVAILABLE") from None
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
                selected = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(selected.st_mode):
                raise _failure("UNSAFE_ASSET_PATH")
            child = os.open(name, flags, dir_fd=parent)
            descriptors.append(child)
            opened = os.fstat(child)
            if not _same_inode(selected, opened):
                raise _failure("ASSET_CHANGED")
            if index >= len(names) - 2 and (
                opened.st_uid != _current_uid() or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise _failure("UNSAFE_ASSET_PATH")
            nodes.append((parent, name, child))
            parent = child
        _check_tree(nodes)
        yield parent, directory
        _check_tree(nodes)
    except AdhelmError:
        raise
    except (OSError, ValueError):
        raise _failure("ASSET_STORAGE_FAILED") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


if sys.platform == "win32":

    @contextmanager
    def _vault_windows(
        directory: Path, *, create: bool
    ) -> Iterator[windows_filesystem.PinnedDirectory]:
        """Pin the vault chain; ``assets`` and ``sha256`` must be private to this user."""
        try:
            pinned = windows_filesystem.open_directory(directory, create=create, private_tail=2)
        except FileNotFoundError:
            raise _failure("ASSET_NOT_AVAILABLE") from None
        except windows_filesystem.UnsafeObjectError:
            raise _failure("UNSAFE_ASSET_PATH") from None
        except (OSError, ValueError):
            raise _failure("ASSET_STORAGE_FAILED") from None
        try:
            with pinned:
                yield pinned
        except AdhelmError:
            raise
        except (OSError, ValueError):
            raise _failure("ASSET_STORAGE_FAILED") from None


def _validate_regular(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _current_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise _failure("UNSAFE_ASSET_PATH")
    if not 1 <= metadata.st_size <= MAX_IMAGE_BYTES:
        raise _failure("ASSET_CORRUPT")


def _read_blob(directory: Directory, name: str) -> bytes:
    try:
        selected = stat_child(directory, name)
    except FileNotFoundError:
        raise _failure("ASSET_NOT_AVAILABLE") from None
    _validate_regular(selected)
    try:
        descriptor = open_child_for_read(directory, name)
    except FileNotFoundError:
        raise _failure("ASSET_CHANGED") from None
    try:
        opened = fstat(descriptor)
        _validate_regular(opened)
        if not _same_file(selected, opened):
            raise _failure("ASSET_CHANGED")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_IMAGE_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_IMAGE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        try:
            final = fstat(descriptor)
            named = stat_child(directory, name)
        except OSError:
            raise _failure("ASSET_CHANGED") from None
        if not _same_file(opened, final) or not _same_file(opened, named) or size != opened.st_size:
            raise _failure("ASSET_CHANGED")
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != name:
            raise _failure("ASSET_CORRUPT")
        return data
    finally:
        os.close(descriptor)


def read_image(root: Path, expected_digest: str) -> bytes:
    """Read one currently available private image object and verify its bytes."""
    _validate_digest(expected_digest)
    with _vault(root, create=False) as (directory, _):
        return _read_blob(directory, expected_digest)


def _remove_owned_temporary(directory: Directory, name: str, opened: os.stat_result) -> None:
    try:
        named = stat_child(directory, name)
    except FileNotFoundError:
        return
    if not _same_inode(named, opened):
        raise _failure("ASSET_CHANGED")
    unlink_child(directory, name)


def _sync_directory(directory: Directory) -> None:
    """Make a published name durable. Windows has no directory fsync: the file is
    flushed before it is linked and NTFS journals the directory entry."""
    if isinstance(directory, int):
        os.fsync(directory)


def _publish_posix(directory: int, data: bytes, expected_digest: str) -> None:
    temporary = f".asset-{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_os_flag("O_NOFOLLOW")
        | _required_os_flag("O_NONBLOCK")
    )
    flags |= _optional_os_flag("O_CLOEXEC")
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
    owned: os.stat_result | None = None
    try:
        owned = os.fstat(descriptor)
        _set_private_mode(descriptor, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        written_stat = os.fstat(descriptor)
        named = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
        if not _same_file(written_stat, named):
            raise _failure("ASSET_CHANGED")
        try:
            os.link(
                temporary,
                expected_digest,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError:
            pass
        else:
            published = os.stat(expected_digest, dir_fd=directory, follow_symlinks=False)
            if not _same_inode(written_stat, published):
                raise _failure("ASSET_CHANGED")
    finally:
        try:
            if owned is not None:
                _remove_owned_temporary(directory, temporary, owned)
        finally:
            os.close(descriptor)


def _publish_pinned(directory: PinnedDirectory, data: bytes, expected_digest: str) -> None:
    """Windows publication: the temporary is closed before it is linked and removed.

    An open handle would block removing the temporary name, so the file is closed
    first and the temporary is then verified by file ID before it is unlinked.
    """
    temporary = f".asset-{uuid.uuid4().hex}.tmp"
    descriptor = create_private_child(directory, temporary)
    owned: os.stat_result | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            owned = fstat(handle.fileno())
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            written_stat = fstat(handle.fileno())
        named = stat_child(directory, temporary)
        # Timestamps are not compared: Windows may stamp the file when the writer
        # closes it.
        if not _same_inode(written_stat, named) or named.st_size != len(data):
            raise _failure("ASSET_CHANGED")
        try:
            link_child(directory, temporary, expected_digest)
        except FileExistsError:
            pass
        else:
            published = stat_child(directory, expected_digest)
            if not _same_inode(written_stat, published):
                raise _failure("ASSET_CHANGED")
    finally:
        if owned is not None:
            _remove_owned_temporary(directory, temporary, owned)


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise _failure("ASSET_STORAGE_FAILED")
        remaining = remaining[written:]


def store_image(root: Path, data: bytes, expected_digest: str) -> Path:
    """Fsync and publish an immutable object without replacing existing data."""
    _validate_digest(expected_digest)
    if (
        type(data) is not bytes
        or not 1 <= len(data) <= MAX_IMAGE_BYTES
        or hashlib.sha256(data).hexdigest() != expected_digest
    ):
        raise _failure("ASSET_DIGEST_MISMATCH")
    with _vault(root, create=True) as (directory, path):
        try:
            _read_blob(directory, expected_digest)
        except AdhelmError as error:
            if error.code != "ASSET_NOT_AVAILABLE":
                raise
        else:
            _sync_directory(directory)
            return path / expected_digest
        if isinstance(directory, int):
            _publish_posix(directory, data, expected_digest)
        else:
            _publish_pinned(directory, data, expected_digest)
        # A competing publisher is accepted only after the same full validation.
        _read_blob(directory, expected_digest)
        _sync_directory(directory)
        return path / expected_digest

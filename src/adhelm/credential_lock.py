"""Private cross-process coordination for one operating-system credential item."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol

import portalocker

from adhelm.errors import AdhelmError

if sys.platform == "win32":
    from adhelm import windows_filesystem

_SERVICE = "adhelm"
_LOCK_DIRECTORY_NAME = "credential-locks"


class _LockDirectory(Protocol):
    """A checked private lock directory that stays valid until it is closed."""

    def open_lock(self, filename: str) -> BinaryIO: ...

    def close(self) -> None: ...


class _PosixLockDirectory:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def open_lock(self, filename: str) -> BinaryIO:
        return _open_private_lock(self._descriptor, filename)

    def close(self) -> None:
        os.close(self._descriptor)


if sys.platform == "win32":

    class _WindowsLockDirectory:
        """Lock directory pinned by Windows handles, with an explicit private DACL."""

        def __init__(self, pinned: windows_filesystem.PinnedDirectory) -> None:
            self._pinned = pinned

        def open_lock(self, filename: str) -> BinaryIO:
            try:
                descriptor = self._pinned.open_lock_file(filename)
            except OSError:
                raise _unsafe_lock_path() from None
            try:
                return os.fdopen(descriptor, "r+b", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise

        def close(self) -> None:
            self._pinned.close()


def _open_lock_directory(path: Path) -> _LockDirectory:
    if sys.platform == "win32":
        if not path.is_absolute() or ".." in path.parts or len(path.parts) <= 1:
            raise _unsafe_lock_path()
        try:
            return _WindowsLockDirectory(
                windows_filesystem.open_directory(
                    path, create=True, private_tail=1, trusted_ancestors=True
                )
            )
        except OSError:
            raise _unsafe_lock_path() from None
    return _PosixLockDirectory(_open_private_directory(path))


@contextmanager
def credential_mutation_lock(
    reference: str, *, root_override: Path | None = None
) -> Iterator[None]:
    """Hold a nonblocking local lock for one case-insensitive OS-store reference.

    ``root_override`` exists only for library and test isolation. It is not a
    configuration or command-line destination. The default is per OS user,
    independent of the application's data directory.
    """
    _require_secure_platform()
    lock_root = _lock_root(root_override)
    directory = _open_lock_directory(lock_root)
    stream: BinaryIO | None = None
    acquired = False
    active_exception = False
    try:
        stream = directory.open_lock(_lock_filename(reference))
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException:
            raise AdhelmError(
                "CREDENTIAL_SETUP_BUSY",
                "Another local credential setup is already changing this credential.",
                next_action="Wait for the local credential setup to finish before trying again.",
            ) from None
        except Exception:
            raise AdhelmError(
                "CREDENTIAL_LOCK_UNAVAILABLE",
                "Secure local credential coordination is unavailable.",
            ) from None
        acquired = True
        try:
            yield
        except BaseException:
            # A cleanup failure must never mask the operation result that led
            # the caller here. That result can carry the only useful recovery
            # information about a secret mutation.
            active_exception = True
            raise
    except BaseException:
        active_exception = True
        raise
    finally:
        cleanup_failed = False
        if stream is not None:
            if acquired:
                try:
                    portalocker.unlock(stream)
                except Exception:
                    cleanup_failed = True
            try:
                stream.close()
            except Exception:
                cleanup_failed = True
        try:
            directory.close()
        except OSError:
            cleanup_failed = True
        if cleanup_failed and not active_exception:
            raise _unverified_local_change()


def _lock_filename(reference: str) -> str:
    if not isinstance(reference, str) or not reference:
        raise AdhelmError(
            "INVALID_CREDENTIAL_REFERENCE", "Credential reference is required for setup."
        )
    # The reference is intentionally never emitted in the lock pathname or bytes.
    namespace = f"{_SERVICE}\x00{reference.casefold()}".encode()
    return f"{hashlib.sha256(namespace).hexdigest()}.lock"


def _require_secure_platform() -> None:
    if sys.platform == "win32":
        return
    if (
        os.name != "posix"
        or sys.platform not in {"darwin", "linux"}
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
    ):
        raise AdhelmError(
            "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED",
            "Secure cross-process credential setup locking is unavailable on this platform.",
        )


def _required_os_flag(name: str) -> int:
    value = getattr(os, name, None)
    if type(value) is not int or not value:
        raise AdhelmError(
            "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED",
            "Secure cross-process credential setup locking is unavailable on this platform.",
        )
    return value


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise AdhelmError(
            "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED",
            "Secure cross-process credential setup locking is unavailable on this platform.",
        )
    value = getuid()
    if type(value) is not int:
        raise AdhelmError(
            "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED",
            "Secure cross-process credential setup locking is unavailable on this platform.",
        )
    return value


def _lock_root(root_override: Path | None) -> Path:
    if root_override is not None:
        root = Path(root_override)
        if not root.is_absolute() or ".." in root.parts:
            raise AdhelmError(
                "UNSAFE_CREDENTIAL_LOCK_PATH",
                "Credential coordination requires an absolute local lock directory.",
            )
        return root
    return _default_lock_root()


def _default_lock_root() -> Path:
    """Return the per-OS-user coordination root; tests may monkeypatch this helper."""
    if sys.platform == "win32":
        try:
            return windows_filesystem.local_app_data() / _SERVICE / _LOCK_DIRECTORY_NAME
        except OSError:
            raise AdhelmError(
                "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED",
                "The operating-system user directory is unavailable for credential coordination.",
            ) from None
    try:
        import pwd

        getpwuid = getattr(pwd, "getpwuid", None)
        if not callable(getpwuid):
            raise OSError("Operating-system user lookup is unavailable")
        entry = getpwuid(_current_uid())
        home_directory = getattr(entry, "pw_dir", None)
        if type(home_directory) is not str:
            raise OSError("Operating-system user directory is unavailable")
        home = Path(home_directory)
    except (KeyError, OSError):
        raise AdhelmError(
            "CREDENTIAL_LOCK_PLATFORM_UNSUPPORTED",
            "The operating-system user directory is unavailable for credential coordination.",
        ) from None
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / _SERVICE / _LOCK_DIRECTORY_NAME
    return home / ".local" / "state" / _SERVICE / _LOCK_DIRECTORY_NAME


def _open_private_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise AdhelmError(
            "UNSAFE_CREDENTIAL_LOCK_PATH",
            "Credential coordination requires an absolute local lock directory.",
        )
    if len(path.parts) <= 1:
        raise _unsafe_lock_path()
    flags = os.O_RDONLY | _required_os_flag("O_DIRECTORY") | _required_os_flag("O_NOFOLLOW")
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError:
        raise _unsafe_lock_path() from None
    try:
        _validate_lock_ancestor(descriptor)
        parts = path.parts[1:]
        for index, component in enumerate(parts):
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                except OSError:
                    raise _unsafe_lock_path() from None
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except OSError:
                    raise _unsafe_lock_path() from None
            except OSError:
                raise _unsafe_lock_path() from None
            try:
                os.close(descriptor)
            except OSError:
                try:
                    os.close(next_descriptor)
                except OSError:
                    pass
                raise _unsafe_lock_path() from None
            descriptor = next_descriptor
            if index == len(parts) - 1:
                _validate_private_directory(descriptor)
            else:
                _validate_lock_ancestor(descriptor)
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _open_private_lock(directory_descriptor: int, filename: str) -> BinaryIO:
    flags = os.O_RDWR | _required_os_flag("O_NOFOLLOW") | _required_os_flag("O_NONBLOCK")
    created = False
    try:
        try:
            descriptor = os.open(
                filename,
                flags | os.O_CREAT | os.O_EXCL,
                mode=0o600,
                dir_fd=directory_descriptor,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except OSError:
        raise _unsafe_lock_path() from None
    try:
        _validate_private_lockfile(descriptor)
        if created:
            os.fsync(descriptor)
            os.fsync(directory_descriptor)
        return os.fdopen(descriptor, "a+b", buffering=0)
    except AdhelmError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise _unsafe_lock_path() from None


def _validate_private_directory(descriptor: int) -> None:
    try:
        status = os.fstat(descriptor)
    except OSError:
        raise _unsafe_lock_path() from None
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != _current_uid()
        or status.st_mode & 0o077
    ):
        raise _unsafe_lock_path()


def _validate_lock_ancestor(descriptor: int) -> None:
    """Require a trusted parent before descending into the lock-root path.

    A non-sticky group- or world-writable ancestor permits another account to
    rename a child after it has been checked. Root-owned system ancestors and
    sticky shared ancestors such as ``/tmp`` are safe for a current-user-owned
    child; an arbitrary owner's directory is not.
    """
    try:
        status = os.fstat(descriptor)
    except OSError:
        raise _unsafe_lock_path() from None
    mode = stat.S_IMODE(status.st_mode)
    trusted_owner = status.st_uid in {_current_uid(), 0}
    shared_without_sticky_protection = mode & 0o022 and not status.st_mode & stat.S_ISVTX
    if not stat.S_ISDIR(status.st_mode) or not trusted_owner or shared_without_sticky_protection:
        raise _unsafe_lock_path()


def _validate_private_lockfile(descriptor: int) -> None:
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != _current_uid()
        or status.st_mode & 0o077
        or status.st_nlink != 1
    ):
        raise _unsafe_lock_path()


def _unsafe_lock_path() -> AdhelmError:
    return AdhelmError(
        "UNSAFE_CREDENTIAL_LOCK_PATH",
        "Credential coordination requires a private local regular lock path.",
    )


def _unverified_local_change() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED",
        "The requested local credential change could not be verified.",
        exit_code=6,
        next_action="Inspect local credential status before any deliberate follow-up.",
    )

"""Native Windows filesystem boundary for selected input, credential locks and artifacts.

This is the Windows counterpart of the descriptor-relative POSIX checks in
``input_files``, ``credential_lock``, ``assets``, ``artifacts`` and
``storage_usage``. It does not emulate POSIX mode bits. It enforces the
equivalent properties with Win32 primitives:

* Every reparse point (symbolic link, junction, mount point, cloud placeholder)
  is rejected rather than followed. Objects are opened with
  ``FILE_FLAG_OPEN_REPARSE_POINT`` and inspected through their own handle.
* A checked directory chain is pinned. Each component stays open without
  ``FILE_SHARE_DELETE`` while the caller works, so none of them can be renamed,
  deleted or replaced underneath it: the counterpart of holding ``dir_fd``
  descriptors. Objects inside a pinned directory are then addressed by name, which
  is only sound because every ancestor stays pinned: a directory opened with
  ``open_child_directory`` keeps its parent's chain open until it is closed
  itself, even if the parent is closed first (the chain is reference counted).
  Nothing relies on an implicit NTFS rule about directories with open children.
* Private objects carry an explicit protected DACL naming only the current user,
  ``SYSTEM`` and ``Administrators``. An existing object is accepted as private
  only when its owner and every effective allow entry are within that set, plus
  the ``OWNER RIGHTS`` and ``CREATOR OWNER`` placeholders that mean the owner
  (see ``windows_acl``).
* Stable identity is the volume serial plus the 128-bit file ID. It is trusted
  only on a volume that advertises ``FILE_SUPPORTS_OPEN_BY_FILE_ID`` (NTFS, ReFS and
  servers that keep IDs stable) and only when neither value is zero. Any other
  object, for example one on FAT or exFAT, which synthesize a positional ID or
  none, fails with ``UnsafeObjectError`` wherever its state is read (see
  ``windows_acl.has_stable_identity``). Nothing is approximated. The refusal
  lives in the one state reader, so it covers every disk-object read, including
  the two readers that never compare identity: the Google OAuth client file
  (``GOOGLE_OAUTH_CLIENT_FILE_INVALID``) and the installed guidance bundle
  (``GUIDANCE_INVALID``). A client file on a FAT or exFAT USB stick, or a Python
  installation on an exFAT volume, is refused for that reason.
* Every ``open_directory`` caller is local-volume only: credential locks, the
  asset vault, artifact destinations and the cache, storage observation and the
  installed guidance bundle. Network drives, UNC paths and ``\\\\?\\`` paths are
  refused because cross-process locking and replacement are not reliable there.
  A Python installation on a network location therefore cannot serve guidance
  (``GUIDANCE_INVALID``). Selected input files do not use ``open_directory`` and
  may sit on a share whose server reports stable file IDs.
* Closing a pinned directory handle is checked. ``PinnedDirectory.close`` closes
  every handle it owns and then raises ``OSError`` if any ``CloseHandle`` failed,
  so the callers' fail-closed reporting of a cleanup failure behaves as the POSIX
  ``os.close`` path does. On an open error path a failed close is reported in
  place of the original refusal, with the refusal chained as ``__cause__``; the
  context manager instead keeps the error that ended its block. Transient handles
  that pin nothing (the attribute-only handle of ``path_state`` and ``lstat``, the
  process token read by ``principals``, and a file handle that
  ``msvcrt.open_osfhandle`` could not adopt, closed while that error propagates)
  are closed on a best-effort basis.
* A closed ``PinnedDirectory`` refuses every operation with ``UnsafeObjectError``:
  once the last reference is gone nothing pins the chain, so a name would be
  resolved by path alone.

Windows has no directory ``fsync``. A new name is made durable by flushing the
file (``FlushFileBuffers``) before publication and by ``MOVEFILE_WRITE_THROUGH``
for replacement; NTFS journals the directory entry itself.

The whole module body is Windows-only; callers import it behind a
``sys.platform == "win32"`` check.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    import functools
    import msvcrt
    import os
    import stat
    from collections.abc import Iterator
    from contextlib import contextmanager
    from ctypes import wintypes
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any

    from adhelm.windows_acl import (
        Principals,
        SecurityState,
        has_stable_identity,
        parse_acl,
        parse_sid,
    )

    class UnsafeObjectError(OSError):
        """A path component violates the reparse-point, identity or ACL boundary."""

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_LIST_DIRECTORY = 0x1
    _FILE_READ_ATTRIBUTES = 0x80
    _READ_CONTROL = 0x20000
    _SYNCHRONIZE = 0x100000

    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    # Pinning a directory means refusing other handles the right to delete or rename it.
    _SHARE_PINNED = _FILE_SHARE_READ | _FILE_SHARE_WRITE

    _CREATE_NEW = 1
    _OPEN_EXISTING = 3

    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    _FILE_TYPE_DISK = 1
    _FILE_TYPE_PIPE = 3
    _DRIVE_UNKNOWN = 0
    _DRIVE_NO_ROOT_DIR = 1
    _DRIVE_REMOTE = 4

    _FILE_BASIC_INFO = 0
    _FILE_STANDARD_INFO = 1
    _FILE_ID_INFO = 18

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x1
    _DACL_SECURITY_INFORMATION = 0x4
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _TOKEN_OWNER = 4
    _SDDL_REVISION_1 = 1

    _MOVEFILE_REPLACE_EXISTING = 0x1
    _MOVEFILE_WRITE_THROUGH = 0x8

    _FILE_FULL_ACCESS = "FA"
    _UNIX_EPOCH_AS_FILETIME = 116_444_736_000_000_000
    _FORBIDDEN_NAME_CHARACTERS = frozenset('\\/:*?"<>|')
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _BasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", wintypes.DWORD),
        ]

    class _StandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_int64),
            ("EndOfFile", ctypes.c_int64),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        ]

    class _IdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_uint64),
            ("FileId", ctypes.c_ubyte * 16),
        ]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    def _declare(library: Any, name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    _CreateFileW = _declare(
        _kernel32,
        "CreateFileW",
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    _CloseHandle = _declare(_kernel32, "CloseHandle", wintypes.BOOL, ctypes.c_void_p)
    _GetFileType = _declare(_kernel32, "GetFileType", wintypes.DWORD, ctypes.c_void_p)
    _GetFileInformationByHandleEx = _declare(
        _kernel32,
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _GetVolumeInformationByHandleW = _declare(
        _kernel32,
        "GetVolumeInformationByHandleW",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _GetDriveTypeW = _declare(_kernel32, "GetDriveTypeW", wintypes.UINT, wintypes.LPCWSTR)
    _CreateDirectoryW = _declare(
        _kernel32, "CreateDirectoryW", wintypes.BOOL, wintypes.LPCWSTR, ctypes.c_void_p
    )
    _MoveFileExW = _declare(
        _kernel32, "MoveFileExW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD
    )
    _GetCurrentProcess = _declare(_kernel32, "GetCurrentProcess", ctypes.c_void_p)
    _LocalFree = _declare(_kernel32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
    _OpenProcessToken = _declare(
        _advapi32,
        "OpenProcessToken",
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )
    _GetTokenInformation = _declare(
        _advapi32,
        "GetTokenInformation",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _GetLengthSid = _declare(_advapi32, "GetLengthSid", wintypes.DWORD, ctypes.c_void_p)
    _GetSecurityInfo = _declare(
        _advapi32,
        "GetSecurityInfo",
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    _ConvertStringSecurityDescriptor = _declare(
        _advapi32,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    )
    _SHGetKnownFolderPath = _declare(
        _shell32,
        "SHGetKnownFolderPath",
        ctypes.c_int,
        ctypes.POINTER(_Guid),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    _CoTaskMemFree = _declare(_ole32, "CoTaskMemFree", None, ctypes.c_void_p)

    def _unsafe() -> UnsafeObjectError:
        return UnsafeObjectError("Filesystem object failed the safety boundary")

    def _last_error() -> OSError:
        return ctypes.WinError(ctypes.get_last_error())

    def _close_handle(handle: int) -> bool:
        """Close a handle and report whether Windows accepted the close.

        Pinned directory handles are always checked (``_close_all``). The callers that
        ignore the result close a transient handle that pins nothing: the
        attribute-only handle of ``path_state`` and ``lstat``, the process token, and
        the file handle that ``_to_fd`` closes when ``msvcrt.open_osfhandle`` could not
        adopt it (the adoption error propagates and no directory is pinned).
        """
        return bool(_CloseHandle(handle))

    def _close_failed() -> OSError:
        return OSError("A filesystem handle could not be closed")

    def _close_all(handles: list[int]) -> bool:
        """Close every handle, last first, even after a failure. True if any close failed."""
        failed = False
        for handle in reversed(handles):
            failed = not _close_handle(handle) or failed
        return failed

    def _close_after_error(handles: list[int], active: BaseException) -> None:
        """Close handles on an error path, reporting a failed close.

        A failed close is raised, chained to the original error, when that was an
        ordinary failure, so the caller sees the cleanup problem in place of the
        original refusal (which stays reachable as ``__cause__``). An interrupt or
        exit keeps priority. ``PinnedDirectory.__exit__`` makes the opposite choice:
        the error that ended its block wins over a failed close.
        """
        if _close_all(handles) and isinstance(active, Exception):
            raise _close_failed() from active

    @dataclass(frozen=True)
    class FileState:
        """Handle-derived metadata for one object, never following a reparse point."""

        file_type: int
        attributes: int = 0
        volume_serial: int = 0
        file_id: bytes = b""
        size: int = 0
        links: int = 0
        write_time: int = 0
        change_time: int = 0

        @property
        def is_directory(self) -> bool:
            return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

        @property
        def is_reparse_point(self) -> bool:
            return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)

        @property
        def is_regular(self) -> bool:
            """A disk file that is neither a directory, a reparse point, pipe nor device."""
            return (
                self.file_type == _FILE_TYPE_DISK
                and not self.is_directory
                and not self.is_reparse_point
            )

        @property
        def is_plain_directory(self) -> bool:
            return (
                self.file_type == _FILE_TYPE_DISK
                and self.is_directory
                and not self.is_reparse_point
            )

        @property
        def object_id(self) -> tuple[int, bytes]:
            return self.volume_serial, self.file_id

        @property
        def fingerprint(self) -> tuple[object, ...]:
            """Identity plus content and metadata stamps; access time is excluded."""
            return (
                self.volume_serial,
                self.file_id,
                self.attributes,
                self.size,
                self.links,
                self.write_time,
                self.change_time,
            )

    def _unix_ns(filetime: int) -> int:
        return (filetime - _UNIX_EPOCH_AS_FILETIME) * 100

    def _stat_result(state: FileState, *, private: bool) -> os.stat_result:
        """Present handle metadata as the ``os.stat_result`` the POSIX-shaped callers read.

        Windows has no mode bits. The permission bits here are synthesized from the
        DACL check: ``0o600`` for a file and ``0o700`` for a directory exactly when
        the owner and every effective allow entry are private to the current user,
        otherwise permissive bits that make the callers' existing privacy checks
        fail. ``st_uid`` is 0 because ownership is part of that same check. The
        inode is the 128-bit file ID and the device is the volume serial number.
        """
        if state.is_reparse_point:
            kind, permissions = stat.S_IFLNK, 0o777
        elif state.is_directory:
            kind, permissions = stat.S_IFDIR, 0o700 if private else 0o777
        elif state.file_type == _FILE_TYPE_DISK:
            kind, permissions = stat.S_IFREG, 0o600 if private else 0o666
        else:
            kind, permissions = (
                (stat.S_IFIFO if state.file_type == _FILE_TYPE_PIPE else stat.S_IFCHR),
                0o666,
            )
        modified = _unix_ns(state.write_time)
        changed = _unix_ns(state.change_time)
        return os.stat_result(
            (
                kind | permissions,
                int.from_bytes(state.file_id, "little"),
                state.volume_serial,
                state.links,
                0,
                0,
                state.size,
                modified // 1_000_000_000,
                modified // 1_000_000_000,
                changed // 1_000_000_000,
                modified / 1e9,
                modified / 1e9,
                changed / 1e9,
                modified,
                modified,
                changed,
            )
        )

    def _query(handle: int, information_class: int, structure: ctypes.Structure) -> None:
        if not _GetFileInformationByHandleEx(
            handle, information_class, ctypes.byref(structure), ctypes.sizeof(structure)
        ):
            raise _last_error()

    def _volume_flags(handle: int) -> int:
        """File system flags of the volume holding the object behind ``handle``."""
        flags = wintypes.DWORD()
        if not _GetVolumeInformationByHandleW(
            handle, None, 0, None, None, ctypes.byref(flags), None, 0
        ):
            raise _last_error()
        return int(flags.value)

    def _state(handle: int) -> FileState:
        file_type = int(_GetFileType(handle))
        if file_type != _FILE_TYPE_DISK:
            # Pipes and character devices carry no file metadata to consult.
            return FileState(file_type=file_type)
        basic = _BasicInfo()
        standard = _StandardInfo()
        identity = _IdInfo()
        _query(handle, _FILE_BASIC_INFO, basic)
        _query(handle, _FILE_STANDARD_INFO, standard)
        _query(handle, _FILE_ID_INFO, identity)
        file_id = bytes(identity.FileId)
        if not has_stable_identity(
            int(identity.VolumeSerialNumber), file_id, _volume_flags(handle)
        ):
            # FAT-style volumes, or a zero ID: identity comparison could accept a swap.
            raise _unsafe()
        return FileState(
            file_type=file_type,
            attributes=int(basic.FileAttributes),
            volume_serial=int(identity.VolumeSerialNumber),
            file_id=file_id,
            size=int(standard.EndOfFile),
            links=int(standard.NumberOfLinks),
            write_time=int(basic.LastWriteTime),
            change_time=int(basic.ChangeTime),
        )

    def _security_state(handle: int) -> SecurityState:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = _GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise OSError(None, "Security descriptor is unavailable", None, status)
        try:
            if not owner.value:
                raise _unsafe()
            owner_sid = parse_sid(ctypes.string_at(owner.value, _GetLengthSid(owner.value)))
            if not dacl.value:
                return SecurityState(owner_sid, None)
            acl_size = int.from_bytes(ctypes.string_at(dacl.value + 2, 2), "little")
            return SecurityState(owner_sid, parse_acl(ctypes.string_at(dacl.value, acl_size)))
        except ValueError:
            raise _unsafe() from None
        finally:
            _LocalFree(descriptor.value)

    def _token_sid(information_class: int) -> str:
        token = ctypes.c_void_p()
        if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise _last_error()
        try:
            size = wintypes.DWORD()
            _GetTokenInformation(token, information_class, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not _GetTokenInformation(token, information_class, buffer, size, ctypes.byref(size)):
                raise _last_error()
            # TOKEN_USER and TOKEN_OWNER both begin with a pointer to the SID.
            sid = ctypes.c_void_p.from_buffer(buffer).value
            if not sid:
                raise _unsafe()
            return parse_sid(ctypes.string_at(sid, _GetLengthSid(sid)))
        finally:
            _close_handle(token.value or 0)

    @functools.cache
    def principals() -> Principals:
        """Identities of the running process: its user and its default object owner."""
        return Principals(_token_sid(_TOKEN_USER), _token_sid(_TOKEN_OWNER))

    @contextmanager
    def _private_attributes(*, directory: bool) -> Iterator[_SecurityAttributes]:
        """Yield ``SECURITY_ATTRIBUTES`` with a protected DACL for the private principals."""
        inherit = "OICI" if directory else ""
        entries = "".join(
            f"(A;{inherit};{_FILE_FULL_ACCESS};;;{sid})" for sid in (principals().user, "SY", "BA")
        )
        descriptor = ctypes.c_void_p()
        if not _ConvertStringSecurityDescriptor(
            f"D:P{entries}", _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise _last_error()
        try:
            yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False)
        finally:
            _LocalFree(descriptor.value)

    def _create_file(
        path: str,
        access: int,
        share: int,
        disposition: int,
        flags: int,
        attributes: _SecurityAttributes | None = None,
    ) -> int:
        handle = _CreateFileW(
            path,
            access,
            share,
            None if attributes is None else ctypes.byref(attributes),
            disposition,
            flags,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise _last_error()
        return int(handle)

    def _extended_path(path: str) -> str:
        """Return ``path`` (an absolute drive path) in the literal ``\\\\?\\`` namespace."""
        return path if path.startswith("\\\\?\\") else "\\\\?\\" + path

    def _check_name(name: str) -> str:
        if (
            not name
            or name in {".", ".."}
            or name[-1] in {".", " "}
            or any(
                ord(character) < 32 or character in _FORBIDDEN_NAME_CHARACTERS for character in name
            )
        ):
            raise _unsafe()
        return name

    def _to_fd(handle: int, flags: int) -> int:
        try:
            return msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
        except BaseException:
            _close_handle(handle)
            raise

    def fd_state(descriptor: int) -> FileState:
        """Metadata of an open descriptor produced by this module."""
        return _state(msvcrt.get_osfhandle(descriptor))

    def require_private_fd(descriptor: int) -> None:
        """Reject a descriptor whose owner or DACL is not the current user's alone."""
        if not principals().is_private_object(_security_state(msvcrt.get_osfhandle(descriptor))):
            raise _unsafe()

    def _handle_is_private(handle: int) -> bool:
        try:
            return principals().is_private_object(_security_state(handle))
        except OSError:
            return False

    def fd_stat(descriptor: int) -> os.stat_result:
        """``os.fstat`` for a descriptor from this module, with synthesized privacy bits."""
        handle = msvcrt.get_osfhandle(descriptor)
        return _stat_result(_state(handle), private=_handle_is_private(handle))

    def _open_any(path: str, *, security: bool = False) -> int:
        """Open any object for attribute inspection without following a reparse point."""
        return _create_file(
            path,
            _FILE_READ_ATTRIBUTES | _SYNCHRONIZE | (_READ_CONTROL if security else 0),
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )

    def path_state(path: str) -> FileState:
        """Metadata of the named object itself (a reparse point is not followed)."""
        handle = _open_any(path)
        try:
            return _state(handle)
        finally:
            _close_handle(handle)

    def open_regular_file(path: str, *, private: bool = False) -> int:
        """Open an existing regular file for reading and return a descriptor.

        A reparse point, directory, pipe or device is rejected from its own
        handle before any byte is read. ``private`` additionally requires the
        file's owner and DACL to name only the current user.
        """
        handle = _create_file(
            path,
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        descriptor = _to_fd(handle, os.O_RDONLY)
        try:
            if not fd_state(descriptor).is_regular:
                raise _unsafe()
            if private:
                require_private_fd(descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    class PinnedDirectory:
        """A checked directory chain held open so no component can be swapped.

        Names addressed through it are single path components, resolved by path, so
        soundness depends on every ancestor staying open. A directory opened with
        ``open_child_directory`` therefore holds a reference on its parent, and the
        parent's handles are released only after the last reference is gone: closing
        the parent first does not unpin the chain below it.

        Close it (or use it as a context manager) to release its reference. After
        that every operation on it raises ``UnsafeObjectError``.
        ``close`` closes every handle whose last reference it dropped and then
        raises ``OSError`` if any ``CloseHandle`` failed; it never leaves a handle
        open because another one failed to close.
        """

        def __init__(
            self,
            path: Path,
            handles: list[int],
            state: FileState,
            parent: PinnedDirectory | None = None,
        ) -> None:
            self.path = path
            self.state = state
            self._handles = handles
            self._parent = parent
            self._references = 1
            self._closed = False
            self._prefix = _extended_path(str(path)).rstrip("\\")

        def _release(self) -> bool:
            """Drop one reference; close the handles at the last one. True if a close failed."""
            self._references -= 1
            if self._references > 0:
                return False
            handles, self._handles = self._handles, []
            failed = _close_all(handles)
            parent, self._parent = self._parent, None
            if parent is not None:
                failed = parent._release() or failed
            return failed

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            if self._release():
                raise _close_failed()

        def __enter__(self) -> PinnedDirectory:
            return self

        def __exit__(self, kind: object, *exception: object) -> None:
            try:
                self.close()
            except OSError:
                # A close failure must not replace the error that ended the block.
                if kind is None:
                    raise

        def _require_open(self) -> None:
            """Refuse use after close: a released directory no longer pins the chain."""
            if self._closed:
                raise _unsafe()

        def _own_handle(self) -> int:
            self._require_open()
            return self._handles[-1]

        def child_path(self, name: str) -> str:
            self._require_open()
            return f"{self._prefix}\\{_check_name(name)}"

        def stat(self, name: str) -> FileState:
            """Metadata of a child itself; ``FileNotFoundError`` when it is absent."""
            return path_state(self.child_path(name))

        def lstat(self, name: str) -> os.stat_result:
            """``os.stat`` of a child without following links, with synthesized privacy bits."""
            handle = _open_any(self.child_path(name), security=True)
            try:
                return _stat_result(_state(handle), private=_handle_is_private(handle))
            finally:
                _close_handle(handle)

        def current_state(self) -> FileState:
            """Metadata of this directory read afresh from its open handle."""
            return _state(self._own_handle())

        def directory_stat(self) -> os.stat_result:
            """``os.fstat`` of this directory, with synthesized privacy bits."""
            handle = self._own_handle()
            return _stat_result(_state(handle), private=_handle_is_private(handle))

        def open_child_directory(
            self, name: str, *, create: bool = False, private: bool = False
        ) -> PinnedDirectory:
            """Open one child directory (never a reparse point) and pin it.

            A created child gets the protected private DACL. ``private`` requires
            the child to be private to the current user, new or existing.
            """
            self._require_open()
            path = self.path / _check_name(name)
            handle = _open_directory_handle(str(path), create=create)
            try:
                state = _state(handle)
                if not state.is_plain_directory or (private and not _handle_is_private(handle)):
                    raise _unsafe()
            except BaseException as error:
                _close_after_error([handle], error)
                raise
            self._references += 1
            return PinnedDirectory(path, [handle], state, parent=self)

        def names(self) -> list[str]:
            self._require_open()
            with os.scandir(f"{self._prefix}\\") as listing:
                return [entry.name for entry in listing]

        def open_file(self, name: str, *, private: bool = False) -> int:
            return open_regular_file(self.child_path(name), private=private)

        def create_file(self, name: str) -> int:
            """Exclusively create a private file and return a write descriptor."""
            with _private_attributes(directory=False) as attributes:
                handle = _create_file(
                    self.child_path(name),
                    _GENERIC_WRITE | _READ_CONTROL,
                    _FILE_SHARE_READ,
                    _CREATE_NEW,
                    _FILE_ATTRIBUTE_NORMAL,
                    attributes,
                )
            return _to_fd(handle, os.O_WRONLY)

        def open_lock_file(self, name: str) -> int:
            """Create or reopen a private single-link lock file for read and write."""
            path = self.child_path(name)
            share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
            access = _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL
            try:
                with _private_attributes(directory=False) as attributes:
                    handle = _create_file(
                        path, access, share, _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL, attributes
                    )
            except FileExistsError:
                handle = _create_file(
                    path,
                    access,
                    share,
                    _OPEN_EXISTING,
                    _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                )
            descriptor = _to_fd(handle, os.O_RDWR)
            try:
                state = fd_state(descriptor)
                if not state.is_regular or state.links != 1:
                    raise _unsafe()
                require_private_fd(descriptor)
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

        def link(self, existing: str, new: str) -> None:
            """Publish ``new`` as a hard link to ``existing``; fails if ``new`` exists."""
            os.link(self.child_path(existing), self.child_path(new))

        def replace(self, source: str, target: str) -> None:
            """Atomically replace ``target`` with ``source`` and write the change through."""
            if not _MoveFileExW(
                self.child_path(source),
                self.child_path(target),
                _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
            ):
                raise _last_error()

        def unlink(self, name: str) -> None:
            os.unlink(self.child_path(name))

    def _open_directory_handle(path: str, *, create: bool) -> int:
        extended = _extended_path(path)
        flags = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
        access = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE

        def attempt() -> int:
            return _create_file(extended, access, _SHARE_PINNED, _OPEN_EXISTING, flags)

        try:
            return attempt()
        except FileNotFoundError:
            if not create:
                raise
        with _private_attributes(directory=True) as attributes:
            if not _CreateDirectoryW(extended, ctypes.byref(attributes)):
                error = _last_error()
                if not isinstance(error, FileExistsError):
                    raise error
        return attempt()

    def open_directory(
        path: Path,
        *,
        create: bool = False,
        private_tail: int = 0,
        trusted_ancestors: bool = False,
        depth: int | None = None,
    ) -> PinnedDirectory:
        """Open and pin an absolute local directory chain, component by component.

        Every component must be a plain directory (no reparse point). The last
        ``private_tail`` components must be private to the current user;
        ``trusted_ancestors`` additionally requires every other component to be
        undeletable by untrusted principals. With ``create``, missing components
        are created with a protected private DACL. Nothing is created before an
        unsafe existing component has been rejected, because creation proceeds
        only below components that have already passed.

        ``depth`` limits the individually checked and pinned components to the
        last ``depth``; the components above them are resolved by the operating
        system in a single open, so an alias there remains usable. This is the
        counterpart of opening only the final component with ``O_NOFOLLOW``.
        """
        drive = path.drive
        if (
            not path.is_absolute()
            or ".." in path.parts
            or len(drive) != 2
            or drive[1] != ":"
            or _GetDriveTypeW(path.anchor) in {_DRIVE_UNKNOWN, _DRIVE_NO_ROOT_DIR, _DRIVE_REMOTE}
        ):
            raise _unsafe()
        names = path.parts[1:]
        handles: list[int] = []
        try:
            current = path.anchor
            state = FileState(file_type=0)
            first = 0 if depth is None else max(0, len(names) + 1 - depth)
            for index in range(len(names) + 1):
                if index:
                    current = f"{current.rstrip(chr(92))}\\{_check_name(names[index - 1])}"
                if index < first:
                    continue
                handle = _open_directory_handle(current, create=create and index > 0)
                handles.append(handle)
                state = _state(handle)
                if not state.is_plain_directory:
                    raise _unsafe()
                private = index > 0 and index > len(names) - private_tail
                if private:
                    if not principals().is_private_object(_security_state(handle)):
                        raise _unsafe()
                elif trusted_ancestors and not principals().is_trusted_directory(
                    _security_state(handle)
                ):
                    raise _unsafe()
            return PinnedDirectory(path, handles, state)
        except BaseException as error:
            _close_after_error(handles, error)
            raise

    def local_app_data() -> Path:
        """Return the current user's local application data directory."""
        # FOLDERID_LocalAppData {F1B32785-6FBA-4FCF-9D55-7B8E7F157091}
        identifier = _Guid(
            0xF1B32785,
            0x6FBA,
            0x4FCF,
            (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
        )
        buffer = ctypes.c_void_p()
        try:
            if _SHGetKnownFolderPath(ctypes.byref(identifier), 0, None, ctypes.byref(buffer)) != 0:
                raise _unsafe()
            if not buffer.value:
                raise _unsafe()
            return Path(ctypes.wstring_at(buffer.value))
        finally:
            _CoTaskMemFree(buffer.value)

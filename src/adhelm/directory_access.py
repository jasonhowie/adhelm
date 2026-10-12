"""Name-based access to the children of an already-opened, checked directory.

POSIX callers hold a directory descriptor and address children with ``dir_fd``.
Windows has no equivalent, so ``windows_filesystem`` pins the directory chain
with handles and addresses children by name inside it. This module gives both
the same small set of operations so the artifact and asset code can share one
publication, verification and race-detection flow.

Windows reports synthesized permission bits (see ``windows_filesystem``): a file
or directory reads as ``0o600``/``0o700`` exactly when its owner and DACL are
private to the current user.
"""

from __future__ import annotations

import os
import sys
from typing import Protocol

if sys.platform == "win32":
    from adhelm import windows_filesystem


class PinnedDirectory(Protocol):
    """The Windows directory handle API used here; see ``windows_filesystem``."""

    def close(self) -> None: ...

    def directory_stat(self) -> os.stat_result: ...

    def open_child_directory(
        self, name: str, *, create: bool = False, private: bool = False
    ) -> PinnedDirectory: ...

    def lstat(self, name: str) -> os.stat_result: ...

    def open_file(self, name: str, *, private: bool = False) -> int: ...

    def create_file(self, name: str) -> int: ...

    def link(self, existing: str, new: str) -> None: ...

    def replace(self, source: str, target: str) -> None: ...

    def unlink(self, name: str) -> None: ...


# A POSIX directory descriptor, or a pinned directory on Windows.
Directory = int | PinnedDirectory


def stat_child(directory: Directory, name: str) -> os.stat_result:
    """``stat`` one child without following a link; ``FileNotFoundError`` if absent."""
    if isinstance(directory, int):
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    return directory.lstat(name)


def fstat(descriptor: int) -> os.stat_result:
    """``os.fstat`` with the Windows privacy bits for a descriptor this module opened."""
    if sys.platform == "win32":
        return windows_filesystem.fd_stat(descriptor)
    return os.fstat(descriptor)


def open_child_for_read(directory: Directory, name: str) -> int:
    """Open one child for reading without following a link or blocking on a pipe."""
    if isinstance(directory, int):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        return os.open(name, flags | getattr(os, "O_CLOEXEC", 0), dir_fd=directory)
    return directory.open_file(name)


def create_private_child(directory: Directory, name: str) -> int:
    """Exclusively create a private child (mode ``0o600`` or a private DACL) for writing."""
    if isinstance(directory, int):
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        return os.open(name, flags, 0o600, dir_fd=directory)
    return directory.create_file(name)


def link_child(directory: Directory, existing: str, new: str) -> None:
    """Publish ``new`` as a hard link to ``existing``; ``FileExistsError`` if it exists."""
    if isinstance(directory, int):
        os.link(
            existing,
            new,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        return
    directory.link(existing, new)


def replace_child(directory: Directory, source: str, target: str) -> None:
    """Atomically rename ``source`` over ``target`` inside the directory."""
    if isinstance(directory, int):
        os.rename(source, target, src_dir_fd=directory, dst_dir_fd=directory)
        return
    directory.replace(source, target)


def unlink_child(directory: Directory, name: str) -> None:
    if isinstance(directory, int):
        os.unlink(name, dir_fd=directory)
        return
    directory.unlink(name)

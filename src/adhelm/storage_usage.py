"""Bounded, root-scoped filesystem metadata observations without cleanup authority.

No file contents are read. Counts describe directory entries and apparent file
lengths, not unique physical allocation, reclaimable space or verified history.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from adhelm.errors import AdhelmError

if sys.platform == "win32":
    from adhelm import windows_filesystem

_MAX_ENTRIES = 25_000
_MAX_DEPTH = 64
_MAX_SECONDS = 30.0
_MAX_INTEGER = 9_007_199_254_740_991
_CATEGORIES = ("changes", "performance", "creative", "assets", "exports", "caches", "other")


def _failure(code: str) -> AdhelmError:
    messages = {
        "STORAGE_OBSERVATION_UNSUPPORTED": "Safe directory metadata inspection is unavailable.",
        "STORAGE_ROOT_INVALID": "Storage inspection requires an existing regular directory tree.",
        "STORAGE_OBSERVATION_CHANGED": "Local storage metadata changed during inspection.",
        "STORAGE_OBSERVATION_LIMIT": "Local storage inspection exceeded its installed bound.",
        "STORAGE_OBSERVATION_FAILED": "Local storage metadata could not be inspected completely.",
    }
    return AdhelmError(
        code,
        messages[code],
        next_action="Inspect the selected local data directory; this command changes no files.",
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _identity(value: os.stat_result) -> tuple[object, ...]:
    # atime is intentionally excluded: an observation can itself update it.
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_nlink,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_platform() -> None:
    if sys.platform == "win32":
        return
    if (
        os.name != "posix"
        or not all(getattr(os, name, 0) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
        or not all(operation in os.supports_dir_fd for operation in (os.open, os.stat))
        or os.stat not in os.supports_follow_symlinks
        or os.scandir not in os.supports_fd
    ):
        raise _failure("STORAGE_OBSERVATION_UNSUPPORTED")


def _os_flag(name: str) -> int:
    value = getattr(os, name, 0)
    return value if type(value) is int else 0


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | _os_flag("O_DIRECTORY")
        | _os_flag("O_NOFOLLOW")
        | _os_flag("O_NONBLOCK")
        | _os_flag("O_CLOEXEC")
    )


def _absolute_root(root: Path) -> Path:
    if not isinstance(root, Path) or ".." in root.parts:
        raise _failure("STORAGE_ROOT_INVALID")
    absolute = root.absolute()
    if len(absolute.parts) > _MAX_DEPTH:
        raise _failure("STORAGE_OBSERVATION_LIMIT")
    return absolute


@contextmanager
def _open_root(root: Path) -> Iterator[int]:
    absolute = _absolute_root(root)
    descriptors: list[int] = []
    links: list[tuple[int, str, int]] = []
    try:
        parent = os.open(absolute.anchor, _directory_flags())
        descriptors.append(parent)
        for name in absolute.parts[1:]:
            selected = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(selected.st_mode):
                raise _failure("STORAGE_ROOT_INVALID")
            child = os.open(name, _directory_flags(), dir_fd=parent)
            descriptors.append(child)
            if _identity(os.fstat(child)) != _identity(selected):
                raise _failure("STORAGE_OBSERVATION_CHANGED")
            links.append((parent, name, child))
            parent = child
        yield parent
        for directory, name, child in links:
            selected = os.stat(name, dir_fd=directory, follow_symlinks=False)
            opened = os.fstat(child)
            if not stat.S_ISDIR(selected.st_mode) or (
                selected.st_dev,
                selected.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise _failure("STORAGE_OBSERVATION_CHANGED")
    except FileNotFoundError:
        raise _failure("STORAGE_ROOT_INVALID") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@dataclass(frozen=True)
class _Entry:
    identity: tuple[object, ...]
    kind: str
    apparent_bytes: int
    hardlinked: bool


def _check_deadline(deadline: float) -> None:
    if monotonic() > deadline:
        raise _failure("STORAGE_OBSERVATION_LIMIT")


def _scan(directory: int, deadline: float) -> dict[tuple[str, ...], _Entry]:
    entries: dict[tuple[str, ...], _Entry] = {}
    directories: set[tuple[int, int]] = set()

    def walk(current: int, relative: tuple[str, ...]) -> None:
        _check_deadline(deadline)
        before = os.fstat(current)
        directory_identity = (before.st_dev, before.st_ino)
        if directory_identity in directories:
            raise _failure("STORAGE_ROOT_INVALID")
        directories.add(directory_identity)
        with os.scandir(current) as listing:
            for item in listing:
                _check_deadline(deadline)
                if len(entries) >= _MAX_ENTRIES:
                    raise _failure("STORAGE_OBSERVATION_LIMIT")
                path = (*relative, item.name)
                value = os.stat(item.name, dir_fd=current, follow_symlinks=False)
                size = 0
                kind = "special"
                if stat.S_ISREG(value.st_mode):
                    kind = "file"
                    size = value.st_size
                    if not 0 <= size <= _MAX_INTEGER:
                        raise _failure("STORAGE_OBSERVATION_LIMIT")
                elif stat.S_ISLNK(value.st_mode):
                    kind = "symlink"
                elif stat.S_ISDIR(value.st_mode):
                    kind = "directory"
                entries[path] = _Entry(_identity(value), kind, size, value.st_nlink > 1)
                if kind == "directory":
                    if len(path) > _MAX_DEPTH:
                        raise _failure("STORAGE_OBSERVATION_LIMIT")
                    child = os.open(item.name, _directory_flags(), dir_fd=current)
                    try:
                        if _identity(os.fstat(child)) != _identity(value):
                            raise _failure("STORAGE_OBSERVATION_CHANGED")
                        walk(child, path)
                        named = os.stat(item.name, dir_fd=current, follow_symlinks=False)
                        if _identity(named) != _identity(value):
                            raise _failure("STORAGE_OBSERVATION_CHANGED")
                    finally:
                        os.close(child)
        if _identity(os.fstat(current)) != _identity(before):
            raise _failure("STORAGE_OBSERVATION_CHANGED")

    walk(directory, ())
    return entries


if sys.platform == "win32":

    @contextmanager
    def _open_root_windows(root: Path) -> Iterator[windows_filesystem.PinnedDirectory]:
        """Pin the root chain; a link, junction or other reparse point is not a root."""
        try:
            pinned = windows_filesystem.open_directory(_absolute_root(root))
        except (FileNotFoundError, windows_filesystem.UnsafeObjectError):
            raise _failure("STORAGE_ROOT_INVALID") from None
        with pinned:
            yield pinned

    def _scan_windows(
        directory: windows_filesystem.PinnedDirectory, deadline: float
    ) -> dict[tuple[str, ...], _Entry]:
        """Windows counterpart of ``_scan``: metadata only, no link or file is followed."""
        entries: dict[tuple[str, ...], _Entry] = {}
        directories: set[tuple[int, bytes]] = set()

        def walk(current: windows_filesystem.PinnedDirectory, relative: tuple[str, ...]) -> None:
            _check_deadline(deadline)
            before = current.current_state()
            if before.object_id in directories:
                raise _failure("STORAGE_ROOT_INVALID")
            directories.add(before.object_id)
            for name in current.names():
                _check_deadline(deadline)
                if len(entries) >= _MAX_ENTRIES:
                    raise _failure("STORAGE_OBSERVATION_LIMIT")
                path = (*relative, name)
                value = current.stat(name)
                size = 0
                kind = "special"
                if value.is_reparse_point:
                    kind = "symlink"
                elif value.is_regular:
                    kind = "file"
                    size = value.size
                    if not 0 <= size <= _MAX_INTEGER:
                        raise _failure("STORAGE_OBSERVATION_LIMIT")
                elif value.is_plain_directory:
                    kind = "directory"
                entries[path] = _Entry(value.fingerprint, kind, size, value.links > 1)
                if kind == "directory":
                    if len(path) > _MAX_DEPTH:
                        raise _failure("STORAGE_OBSERVATION_LIMIT")
                    with current.open_child_directory(name) as child:
                        if child.state.fingerprint != value.fingerprint:
                            raise _failure("STORAGE_OBSERVATION_CHANGED")
                        walk(child, path)
                        if current.stat(name).fingerprint != value.fingerprint:
                            raise _failure("STORAGE_OBSERVATION_CHANGED")
            if current.current_state().fingerprint != before.fingerprint:
                raise _failure("STORAGE_OBSERVATION_CHANGED")

        walk(directory, ())
        return entries


def _observe_entries(root: Path, deadline: float) -> dict[tuple[str, ...], _Entry]:
    """Return the entries of two traversals that agree, or fail as changed."""
    if sys.platform == "win32":
        with _open_root_windows(root) as pinned:
            first = _scan_windows(pinned, deadline)
            second = _scan_windows(pinned, deadline)
    else:
        with _open_root(root) as descriptor:
            first = _scan(descriptor, deadline)
            second = _scan(descriptor, deadline)
    if first != second:
        raise _failure("STORAGE_OBSERVATION_CHANGED")
    return first


def _export_prefix(root: Path, output_directory: str | None) -> tuple[str, ...] | None:
    if output_directory is None:
        return None
    if type(output_directory) is not str or not output_directory or "\x00" in output_directory:
        raise _failure("STORAGE_ROOT_INVALID")
    output = Path(output_directory)
    # Lexical classification only. Never resolve/open an external destination.
    if ".." in output.parts:
        raise _failure("STORAGE_ROOT_INVALID")
    try:
        relative = output.absolute().relative_to(root.absolute())
    except ValueError:
        return None
    return relative.parts


def _category(path: tuple[str, ...], export_prefix: tuple[str, ...] | None) -> str:
    # Known canonical/internal paths take precedence over an overlapping export
    # preference. Classification never asserts that an object is safe to prune.
    if len(path) >= 2 and path[0] == "ledger":
        if path[1] in {"changes", "performance", "creative"}:
            return path[1]
        if len(path) == 2 and (
            path[1] in {"index.sqlite3", "index.sqlite3-journal", ".index-rebuild.lock"}
            or (path[1].startswith(".index-") and path[1].endswith(".rebuild"))
        ):
            return "caches"
    if path[0] == "assets":
        return "assets"
    if export_prefix and path[: len(export_prefix)] == export_prefix:
        return "exports"
    # Output at the data root can coexist with config and canonical data, so
    # claiming every other root file as an export would be misleading.
    if path[0] == "exports":
        return "exports"
    if path[0] in {"cache", "artifact-cache"}:
        return "caches"
    return "other"


def _export_destination_status(
    entries: dict[tuple[str, ...], _Entry],
    *,
    configured: bool,
    prefix: tuple[str, ...] | None,
) -> str:
    if not configured:
        return "NOT_CONFIGURED"
    if prefix is None:
        return "OUTSIDE_SCOPE_NOT_SCANNED"
    if not prefix:
        return "DATA_ROOT_OVERLAP_NOT_CLASSIFIED"
    for length in range(1, len(prefix) + 1):
        entry = entries.get(prefix[:length])
        if entry is None:
            return "NOT_PRESENT_IN_SCOPE"
        if entry.kind == "symlink":
            return "SYMLINK_NOT_FOLLOWED"
        if entry.kind != "directory":
            return "NOT_A_DIRECTORY"
    return "WITHIN_SCOPE"


def observe_local_storage(root: Path, *, output_directory: str | None = None) -> dict[str, Any]:
    """Return redacted totals from two agreeing bounded metadata traversals.

    The selected root must already exist. Symbolic links and special files are
    counted but never followed/opened; inaccessible descendants fail the whole
    observation. Results contain no filenames, paths, contents or inode IDs.
    This is not an atomic filesystem snapshot or a ledger integrity check.
    """
    _require_platform()
    if not isinstance(root, Path):
        raise _failure("STORAGE_ROOT_INVALID")
    export_prefix = _export_prefix(root, output_directory)
    started = _now()
    deadline = monotonic() + _MAX_SECONDS
    try:
        first = _observe_entries(root, deadline)
        _check_deadline(deadline)
    except (OSError, ValueError):
        raise _failure("STORAGE_OBSERVATION_FAILED") from None
    finished = _now()
    if (
        type(started) is not datetime
        or type(finished) is not datetime
        or started.tzinfo != UTC
        or finished.tzinfo != UTC
        or finished < started
    ):
        raise _failure("STORAGE_OBSERVATION_CHANGED")
    categories = {
        name: {
            "file_count": 0,
            "apparent_bytes": 0,
            "hardlinked_file_entries": 0,
            "symlink_count": 0,
            "special_file_count": 0,
        }
        for name in _CATEGORIES
    }
    for path, entry in first.items():
        category = categories[_category(path, export_prefix)]
        if entry.kind == "file":
            category["file_count"] += 1
            category["apparent_bytes"] += entry.apparent_bytes
            category["hardlinked_file_entries"] += int(entry.hardlinked)
        elif entry.kind == "symlink":
            category["symlink_count"] += 1
        elif entry.kind == "special":
            category["special_file_count"] += 1
    total = sum(category["apparent_bytes"] for category in categories.values())
    if total > _MAX_INTEGER:
        raise _failure("STORAGE_OBSERVATION_LIMIT")
    _check_deadline(deadline)
    return {
        "schema_version": 1,
        "status": "OBSERVED_STABLE_METADATA",
        "observation_started_at": started.isoformat(),
        "observation_finished_at": finished.isoformat(),
        "scope": "SELECTED_DATA_DIRECTORY_ONLY",
        "categories": categories,
        "total_apparent_bytes": total,
        "entry_count": len(first),
        "byte_semantics": "REGULAR_FILE_LENGTHS_PER_DIRECTORY_ENTRY",
        "physical_allocation": "NOT_MEASURED",
        "reclaimable_bytes": None,
        "growth": "NOT_MEASURED_SINGLE_OBSERVATION",
        "history_integrity": "NOT_EVALUATED",
        "cleanup_authorized": False,
        "saved_export_destination": _export_destination_status(
            first, configured=output_directory is not None, prefix=export_prefix
        ),
        "limits": {"entries": _MAX_ENTRIES, "depth": _MAX_DEPTH, "seconds": _MAX_SECONDS},
    }

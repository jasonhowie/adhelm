"""Bounded regular-file input for noninteractive JSON and local image commands.

These checks protect the read boundary, not the meaning or authority of the
document. Callers still validate their closed schema before using its contents.
Private OAuth client files retain their separate credential-file checks.
"""

import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adhelm.errors import AdhelmError
from adhelm.strict_json import validate_json_input

if sys.platform == "win32":
    from adhelm import windows_filesystem

_JSON_MAXIMUM_BYTES = 1_048_576
_MAXIMUM_ALLOWED_BOUND = 67_108_864
_CHUNK_BYTES = 65_536


def _invalid_file() -> AdhelmError:
    return AdhelmError(
        "INPUT_FILE_INVALID",
        "Input must be a readable regular file, not a symbolic link or special file.",
        exit_code=2,
        next_action="Save the complete input in a regular file and select that file.",
    )


def _changed_file() -> AdhelmError:
    return AdhelmError(
        "INPUT_FILE_CHANGED",
        "The input file changed while it was being read.",
        exit_code=2,
        next_action="Finish writing the request file before running the command again.",
    )


def _too_large(maximum_bytes: int) -> AdhelmError:
    return AdhelmError(
        "INPUT_TOO_LARGE",
        f"Input exceeds the {maximum_bytes}-byte limit.",
        exit_code=2,
        next_action="Use a smaller request matching the installed schema.",
    )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@dataclass(frozen=True)
class _Observation:
    """What one stat of the selected file reports, in platform-neutral terms.

    ``identity`` covers the object and its content stamps; two observations of the
    same unchanged file compare equal.
    """

    regular: bool
    size: int
    identity: tuple[object, ...]


def _observe_posix(value: os.stat_result) -> _Observation:
    return _Observation(stat.S_ISREG(value.st_mode), value.st_size, _file_identity(value))


def _read_bounded(descriptor: int, maximum_bytes: int) -> bytes:
    """Read to end of file, refusing more than ``maximum_bytes`` without buffering it all."""
    chunks: list[bytes] = []
    length = 0
    while length <= maximum_bytes:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, maximum_bytes + 1 - length))
        if not chunk:
            break
        chunks.append(chunk)
        length += len(chunk)
    if length > maximum_bytes:
        raise _too_large(maximum_bytes)
    return b"".join(chunks)


def _read_checked(
    maximum_bytes: int,
    selected: _Observation,
    open_selected: Callable[[], int],
    observe_descriptor: Callable[[int], _Observation],
    observe_path: Callable[[], _Observation],
    path_lost: tuple[type[OSError], ...],
) -> bytes:
    """The platform-independent bounded read; only the primitives passed in differ.

    ``selected`` was observed by path before ``open_selected`` ran. The opened
    descriptor must describe the same unchanged file, and the descriptor and the
    path must still agree after the last byte is read.
    """
    if not selected.regular:
        raise _invalid_file()
    if selected.size > maximum_bytes:
        raise _too_large(maximum_bytes)
    descriptor = open_selected()
    try:
        opened = observe_descriptor(descriptor)
        if not opened.regular:
            raise _invalid_file()
        if opened.size > maximum_bytes:
            raise _too_large(maximum_bytes)
        if opened.identity != selected.identity:
            raise _changed_file()
        content = _read_bounded(descriptor, maximum_bytes)
        finished = observe_descriptor(descriptor)
        try:
            current = observe_path()
        except path_lost:
            # The selected path existed when this descriptor was opened. Losing it
            # during the read is an observed change, distinct from selecting an
            # initially missing input file.
            raise _changed_file() from None
        if (
            not current.regular
            or finished.identity != opened.identity
            or current.identity != opened.identity
            or len(content) != opened.size
        ):
            raise _changed_file()
        return content
    finally:
        os.close(descriptor)


def read_regular_input_file(path: Path, *, maximum_bytes: int) -> bytes:
    """Read from one checked descriptor up to the installed command's byte bound.

    The final path component must be a regular file. Parent directory aliases
    remain usable; this is a caller-selected input, not traversal of a trusted
    private directory. Nonblocking/no-follow open plus descriptor checks reject
    a final-component swap. Observed replacement or content-metadata changes
    fail before the caller can use the bytes. This is not a filesystem lock or
    an assurance that network filesystems have bounded latency.

    On Windows the final component is opened as a reparse point and rejected if
    it is a symbolic link, junction or any other reparse point, a directory, a
    pipe or a device, and the file ID plus size and write/change stamps must match
    before and after the read. A network share is accepted when its volume
    advertises stable file IDs; no latency bound is implied. A volume that does not
    (for example FAT) or an object that reports a zero ID is refused.
    """
    if (
        not isinstance(path, Path)
        or type(maximum_bytes) is not int
        or not 1 <= maximum_bytes <= _MAXIMUM_ALLOWED_BOUND
    ):
        raise _invalid_file()
    if sys.platform == "win32":
        return _read_regular_input_file_windows(path, maximum_bytes)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        raise AdhelmError(
            "INPUT_FILE_PLATFORM_UNSUPPORTED",
            "This platform lacks the required safe input-file primitives.",
            exit_code=2,
            next_action="Use an installation with supported regular-file input checks.",
        )
    flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
    try:
        return _read_checked(
            maximum_bytes,
            _observe_posix(path.lstat()),
            lambda: os.open(path, flags),
            lambda descriptor: _observe_posix(os.fstat(descriptor)),
            lambda: _observe_posix(path.lstat()),
            (FileNotFoundError, NotADirectoryError),
        )
    except (OSError, ValueError):
        # Never expose a caller path, OS exception text, or input bytes through
        # the structured result. The command has not received any input yet.
        raise _invalid_file() from None


if sys.platform == "win32":

    def _observe_windows(state: windows_filesystem.FileState) -> _Observation:
        return _Observation(state.is_regular, state.size, state.fingerprint)

    def _read_regular_input_file_windows(path: Path, maximum_bytes: int) -> bytes:
        selected_path = os.fspath(path)
        try:
            # A colon in the final component selects an NTFS alternate data stream,
            # which is not the file's regular content.
            if ":" in path.name:
                raise _invalid_file()
            return _read_checked(
                maximum_bytes,
                _observe_windows(windows_filesystem.path_state(selected_path)),
                lambda: windows_filesystem.open_regular_file(selected_path),
                lambda descriptor: _observe_windows(windows_filesystem.fd_state(descriptor)),
                lambda: _observe_windows(windows_filesystem.path_state(selected_path)),
                # A deleted file that is still open reports access denied until the
                # last handle closes, so any failure to reopen the name counts.
                (OSError,),
            )
        except (OSError, ValueError):
            raise _invalid_file() from None


def read_json_input_file(path: Path) -> bytes:
    """Read a JSON request using the fixed existing 1 MiB input limit."""
    return read_regular_input_file(path, maximum_bytes=_JSON_MAXIMUM_BYTES)


def validate_json_file[T: BaseModel](model: type[T], path: Path) -> T:
    """Apply the existing precision-preserving JSON schema boundary to a file."""
    return validate_json_input(model, read_json_input_file(path))

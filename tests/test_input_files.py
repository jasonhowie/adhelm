"""Regular JSON input cannot block on pipes or normalize forbidden scalars."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm import input_files
from adhelm.errors import AdhelmError
from adhelm.models import Money

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: this case instruments POSIX os.open or O_NOFOLLOW. The Windows counterparts "
        "(reparse-point, pipe and swap rejection) are in tests/test_windows_filesystem.py."
    ),
)


def forbid_data_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the checked data open is reached."""

    def forbidden(*args, **kwargs):
        pytest.fail("The input must be rejected before it is opened for reading")

    if sys.platform == "win32":
        from adhelm import windows_filesystem

        monkeypatch.setattr(windows_filesystem, "open_regular_file", forbidden)
    else:
        monkeypatch.setattr(input_files.os, "open", forbidden)


def test_file_validation_preserves_raw_number_types_and_duplicate_rejection(tmp_path: Path):
    request = tmp_path / "request.json"
    request.write_bytes(b'{"minor_units":5,"currency":"USD"}')
    assert input_files.validate_json_file(Money, request) == Money(minor_units=5, currency="USD")
    for scalar in (b"5.0", b"true", b"false", b"-0.0"):
        request.write_bytes(b'{"minor_units":' + scalar + b',"currency":"USD"}')
        with pytest.raises(ValidationError):
            input_files.validate_json_file(Money, request)
    request.write_bytes(b'{"minor_units":5,"minor_units":6,"currency":"USD"}')
    with pytest.raises(AdhelmError) as error:
        input_files.validate_json_file(Money, request)
    assert error.value.code == "INVALID_JSON"


@pytest.mark.parametrize("kind", ["directory", "symlink", "pipe", "device", "missing"])
def test_nonregular_inputs_reject_before_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, make_link, make_pipe
):
    selected = tmp_path / "private-input-name"
    if kind == "directory":
        selected.mkdir()
    elif kind == "symlink":
        target = tmp_path / "private-target"
        target.write_text("private-content")
        make_link(selected, target)
    elif kind == "pipe":
        selected = make_pipe("private-input-name")
    elif kind == "device":
        selected = Path(os.devnull)

    forbid_data_open(monkeypatch)
    with pytest.raises(AdhelmError) as error:
        input_files.read_json_input_file(selected)
    assert error.value.code == "INPUT_FILE_INVALID"
    assert error.value.exit_code == 2
    assert "private-" not in str(error.value)
    assert str(selected) not in str(error.value)


def test_real_subprocess_fifo_input_finishes_with_bounded_failure(make_pipe):
    pipe = make_pipe("request.json")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "import sys\n"
            "from adhelm.input_files import read_json_input_file\n"
            "from adhelm.errors import AdhelmError\n"
            "try:\n"
            "    read_json_input_file(Path(sys.argv[1]))\n"
            "except AdhelmError as error:\n"
            "    print(error.code)\n"
            "    sys.exit(error.exit_code)\n",
            str(pipe),
        ],
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert result.stdout.splitlines() == [b"INPUT_FILE_INVALID"]
    assert result.stderr == b""


@_POSIX_ONLY
def test_final_component_pipe_swap_cannot_block_or_reach_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    selected = tmp_path / "request.json"
    selected.write_text("{}")
    original_open = os.open
    original_close = os.close
    opened = []
    closed = []

    def swapped_open(path, flags, *args, **kwargs):
        selected.unlink()
        os.mkfifo(selected)
        assert flags & os.O_NONBLOCK
        assert flags & os.O_NOFOLLOW
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor):
        closed.append(descriptor)
        return original_close(descriptor)

    def forbidden(*args):
        pytest.fail("A replaced pipe must never be read")

    monkeypatch.setattr(input_files.os, "open", swapped_open)
    monkeypatch.setattr(input_files.os, "close", tracked_close)
    monkeypatch.setattr(input_files.os, "read", forbidden)
    with pytest.raises(AdhelmError) as error:
        input_files.read_json_input_file(selected)
    assert error.value.code == "INPUT_FILE_INVALID"
    assert opened and closed == opened


def test_bound_is_bytes_and_oversized_file_never_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    selected = tmp_path / "request.json"
    content = "日".encode() * (1_048_576 // 3) + b" "
    assert len(content) == 1_048_576
    selected.write_bytes(content)
    assert input_files.read_json_input_file(selected) == content
    selected.write_bytes(content + b" ")
    forbid_data_open(monkeypatch)
    with pytest.raises(AdhelmError) as error:
        input_files.read_json_input_file(selected)
    assert error.value.code == "INPUT_TOO_LARGE"


@pytest.mark.parametrize(
    "change",
    [
        "append",
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
        "rewrite",
    ],
)
def test_observed_content_changes_reject_bytes_before_the_caller_uses_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
):
    selected = tmp_path / "request.json"
    selected.write_bytes(b'{"value":1}')
    original_read = os.read
    changed = False

    def changed_read(descriptor, count):
        nonlocal changed
        raw = original_read(descriptor, count)
        if not changed:
            changed = True
            if change == "replace":
                replacement = tmp_path / "replacement.json"
                replacement.write_bytes(b'{"value":2}')
                replacement.replace(selected)
            elif change == "append":
                with selected.open("ab") as handle:
                    handle.write(b" ")
            else:
                selected.write_bytes(b'{"value":2}')
                metadata = selected.stat()
                os.utime(selected, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
        return raw

    monkeypatch.setattr(input_files.os, "read", changed_read)
    with pytest.raises(AdhelmError) as error:
        input_files.read_json_input_file(selected)
    assert error.value.code == "INPUT_FILE_CHANGED"


@_POSIX_ONLY
def test_unsupported_primitive_fails_before_any_input_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(input_files.os, "O_NOFOLLOW", 0)
    with pytest.raises(AdhelmError) as error:
        input_files.read_json_input_file(tmp_path / "absent")
    assert error.value.code == "INPUT_FILE_PLATFORM_UNSUPPORTED"


def test_binary_input_uses_its_installed_bound_without_json_decoding(tmp_path: Path):
    selected = tmp_path / "image.bin"
    binary = bytes(range(256)) * 8192
    selected.write_bytes(binary)
    assert input_files.read_regular_input_file(selected, maximum_bytes=len(binary)) == binary
    with pytest.raises(AdhelmError) as error:
        input_files.read_regular_input_file(selected, maximum_bytes=len(binary) - 1)
    assert error.value.code == "INPUT_TOO_LARGE"


@pytest.mark.parametrize("bound", [True, 1.0, 0, -1, 67_108_865])
def test_bound_cannot_be_widened_by_noninteger_or_unbounded_values(tmp_path: Path, bound):
    with pytest.raises(AdhelmError) as error:
        input_files.read_regular_input_file(tmp_path / "missing", maximum_bytes=bound)
    assert error.value.code == "INPUT_FILE_INVALID"

"""Adversarial synthetic filesystem checks for bounded storage observation."""

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import storage_usage as module
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences

_SCAN = "_scan_windows" if sys.platform == "win32" else "_scan"
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: this case renames a directory the observation holds open, which POSIX allows. "
        "On Windows the pinned root cannot be renamed; see tests/test_windows_filesystem.py."
    ),
)


def _put(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("suffix", [(), ("nested",)])
def test_in_scope_export_symlink_prefix_is_not_traversed_or_claimed_within_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: tuple[str, ...], make_link
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = _put(tmp_path, "outside/private-customer-export.txt", b"private")
    make_link(root / "exports", outside.parent)
    output_directory = root.joinpath("exports", *suffix)

    original_open = os.open
    opened: list[object] = []

    def no_followed_export(path: object, *args: object, **kwargs: object) -> int:
        opened.append(path)
        if path == "exports":
            raise AssertionError("The configured symlink must not be opened")
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {no_followed_export})
    monkeypatch.setattr(os, "open", no_followed_export)

    result = module.observe_local_storage(root, output_directory=str(output_directory))

    assert result["saved_export_destination"] == "SYMLINK_NOT_FOLLOWED"
    assert result["categories"]["exports"]["symlink_count"] == 1
    assert "private-customer-export" not in json.dumps(result)
    assert "exports" not in opened


def test_in_scope_export_destination_statuses_are_specific_and_redacted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    absent = module.observe_local_storage(root, output_directory=str(root / "missing-private"))
    assert absent["saved_export_destination"] == "NOT_PRESENT_IN_SCOPE"

    private_file = _put(root, "private-export-file", b"data")
    regular = module.observe_local_storage(root, output_directory=str(private_file))
    assert regular["saved_export_destination"] == "NOT_A_DIRECTORY"
    assert "private-export-file" not in json.dumps(regular)

    private_file.unlink()
    private_file.mkdir()
    directory = module.observe_local_storage(root, output_directory=str(private_file))
    assert directory["saved_export_destination"] == "WITHIN_SCOPE"


@_POSIX_ONLY
def test_final_root_path_swap_to_external_symlink_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _put(root, "local/file", b"local")
    outside = _put(tmp_path, "outside/private-target", b"private")
    original_scan = module._scan
    calls = 0

    def swap_after_second_scan(*args: object) -> dict[tuple[str, ...], object]:
        nonlocal calls
        result = original_scan(*args)  # type: ignore[arg-type]
        calls += 1
        if calls == 2:
            root.rename(tmp_path / "former-root")
            root.symlink_to(outside.parent, target_is_directory=True)
        return result  # type: ignore[return-value]

    monkeypatch.setattr(module, "_scan", swap_after_second_scan)

    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(root)

    assert caught.value.code == "STORAGE_OBSERVATION_CHANGED"
    assert "private-target" not in str(caught.value)


def test_minimum_cli_bound_returns_only_a_sanitized_limit_diagnostic(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    _put(tmp_path, "private-customer-name", b"content")

    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "storage", "usage"])

    parsed = json.loads(result.output)
    assert result.exit_code == 3
    assert parsed["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert parsed["data"] == {}
    assert "categories" not in result.output
    assert "private-customer-name" not in result.output
    assert str(tmp_path) not in result.output
    assert len(result.output.encode("utf-8")) <= 1024

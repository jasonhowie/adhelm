"""Local state directories must be private even under a permissive umask."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from adhelm.ledger import Ledger
from adhelm.private_directories import ensure_private_directory

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: these cases assert POSIX mode bits and umask handling. The Windows counterpart "
        "(a private DACL on every created directory) is in tests/test_windows_filesystem.py."
    ),
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def group_writable_umask() -> object:
    previous = os.umask(0o002)
    try:
        yield None
    finally:
        os.umask(previous)


def test_missing_ancestors_are_created_privately(
    tmp_path: Path, group_writable_umask: object
) -> None:
    target = tmp_path / "data" / "nested" / "leaf"
    ensure_private_directory(target)
    for directory in (tmp_path / "data", tmp_path / "data" / "nested", target):
        assert directory.is_dir()
        assert _mode(directory) == 0o700


def test_existing_directories_keep_their_mode(tmp_path: Path, group_writable_umask: object) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o750)
    ensure_private_directory(existing / "child")
    assert _mode(existing) == 0o750
    assert _mode(existing / "child") == 0o700


def test_ledger_creates_a_private_data_directory(
    tmp_path: Path, group_writable_umask: object
) -> None:
    data_dir = tmp_path / "data-dir"
    ledger = Ledger(data_dir, profile_ref="profile")
    ledger._ensure_dirs("changes")
    assert _mode(data_dir) == 0o700
    assert _mode(data_dir / "ledger") == 0o700

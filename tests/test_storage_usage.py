"""Filesystem observations use synthetic temporary data, never customer paths."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adhelm import storage_usage as module
from adhelm.errors import AdhelmError

_SCAN = "_scan_windows" if sys.platform == "win32" else "_scan"
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: this case drives FIFOs or POSIX descriptor instrumentation. The Windows "
        "counterparts (reparse points, pinned roots, non-regular entries) are in "
        "tests/test_windows_filesystem.py."
    ),
)


def put(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_reports_all_categories_without_reading_contents(tmp_path, monkeypatch):
    samples = {
        "ledger/changes/00000000000000000001.jsonl": b"a" * 17,
        "ledger/performance/00000000000000000001.jsonl": b"b" * 19,
        "ledger/creative/00000000000000000001.jsonl": b"c" * 23,
        "assets/sha256/synthetic": b"d" * 29,
        "reports/secret-customer-name.html": b"e" * 31,
        "ledger/index.sqlite3": b"f" * 37,
        "config.json": b"g" * 41,
    }
    for name, content in samples.items():
        put(tmp_path, name, content)

    def forbidden(*args, **kwargs):
        raise AssertionError("File contents must not be read")

    monkeypatch.setattr(os, "read", forbidden)
    result = module.observe_local_storage(tmp_path, output_directory=str(tmp_path / "reports"))
    assert result["total_apparent_bytes"] == 197
    assert result["categories"] == {
        category: {
            "file_count": 1,
            "apparent_bytes": length,
            "hardlinked_file_entries": 0,
            "symlink_count": 0,
            "special_file_count": 0,
        }
        for category, length in zip(module._CATEGORIES, (17, 19, 23, 29, 31, 37, 41), strict=True)
    }
    rendered = json.dumps(result)
    assert "secret-customer-name" not in rendered
    assert str(tmp_path) not in rendered
    assert result["cleanup_authorized"] is False
    assert result["reclaimable_bytes"] is None
    assert result["history_integrity"] == "NOT_EVALUATED"


def test_empty_existing_root_has_zero_usage_and_no_created_files(tmp_path):
    result = module.observe_local_storage(tmp_path)
    assert result["total_apparent_bytes"] == result["entry_count"] == 0
    assert list(tmp_path.iterdir()) == []
    assert result["growth"] == "NOT_MEASURED_SINGLE_OBSERVATION"


def test_missing_root_fails_without_creating_it(tmp_path):
    absent = tmp_path / "absent"
    with pytest.raises(AdhelmError, match="existing regular directory"):
        module.observe_local_storage(absent)
    assert not absent.exists()


@_POSIX_ONLY
def test_symlink_targets_and_special_files_are_never_opened(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = put(tmp_path, "outside/private-name", b"sensitive" * 10)
    (root / "link-file").symlink_to(outside)
    (root / "link-directory").symlink_to(outside.parent, target_is_directory=True)
    (root / "dangling").symlink_to(tmp_path / "missing")
    os.mkfifo(root / "fifo")
    result = module.observe_local_storage(root)
    assert result["total_apparent_bytes"] == 0
    assert result["categories"]["other"]["symlink_count"] == 3
    assert result["categories"]["other"]["special_file_count"] == 1
    assert "private-name" not in json.dumps(result)


def test_hardlink_lengths_are_explicitly_per_entry(tmp_path):
    first = put(tmp_path, "assets/one", b"12345")
    os.link(first, tmp_path / "assets/two")
    result = module.observe_local_storage(tmp_path)
    assert result["total_apparent_bytes"] == 10
    assert result["categories"]["assets"]["hardlinked_file_entries"] == 2
    assert result["physical_allocation"] == "NOT_MEASURED"


def test_installed_artifact_cache_is_counted_without_claiming_rebuildability(tmp_path):
    put(tmp_path, "artifact-cache/report-artifacts.json", b"123456")
    result = module.observe_local_storage(tmp_path)
    assert result["categories"]["caches"]["apparent_bytes"] == 6
    assert result["reclaimable_bytes"] is None


def test_external_export_destination_is_not_touched(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = put(tmp_path, "outside/private-name", b"private")
    original = os.open
    opened = []

    def record(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)

    # Preserve the feature-detection set when instrumenting the syscall.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {record})
    monkeypatch.setattr(os, "open", record)
    result = module.observe_local_storage(root, output_directory=str(outside.parent))
    assert result["saved_export_destination"] == "OUTSIDE_SCOPE_NOT_SCANNED"
    assert result["total_apparent_bytes"] == 0
    assert "outside" not in opened


def test_data_root_export_overlap_does_not_reclassify_configuration(tmp_path):
    put(tmp_path, "config.json", b"123")
    put(tmp_path, "ledger/changes/segment.jsonl", b"12345")
    result = module.observe_local_storage(tmp_path, output_directory=str(tmp_path))
    assert result["categories"]["other"]["apparent_bytes"] == 3
    assert result["categories"]["changes"]["apparent_bytes"] == 5
    assert result["saved_export_destination"] == "DATA_ROOT_OVERLAP_NOT_CLASSIFIED"


def test_ledger_stream_precedes_overlapping_export_destination(tmp_path):
    put(tmp_path, "ledger/changes/segment.jsonl", b"12345")
    result = module.observe_local_storage(tmp_path, output_directory=str(tmp_path / "ledger"))
    assert result["categories"]["changes"]["apparent_bytes"] == 5
    assert result["categories"]["exports"]["apparent_bytes"] == 0


def test_metadata_change_between_passes_rejects_partial_result(tmp_path, monkeypatch):
    selected = put(tmp_path, "changing-file", b"1")
    original = getattr(module, _SCAN)
    calls = 0

    def changed(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            selected.write_bytes(b"12345")
        return result

    monkeypatch.setattr(module, _SCAN, changed)
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path)
    assert caught.value.code == "STORAGE_OBSERVATION_CHANGED"


def test_entry_limit_rejects_instead_of_truncating(tmp_path, monkeypatch):
    put(tmp_path, "one", b"1")
    put(tmp_path, "two", b"2")
    monkeypatch.setattr(module, "_MAX_ENTRIES", 1)
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path)
    assert caught.value.code == "STORAGE_OBSERVATION_LIMIT"


def test_depth_limit_rejects_instead_of_skipping_descendants(tmp_path, monkeypatch):
    put(tmp_path, "one/two/three/four/file", b"1")
    # Root traversal has its own bound; make the synthetic relative path deeper.
    depth = len(tmp_path.parts) + 2
    put(tmp_path, "/".join(["nested"] * (depth + 1)) + "/file", b"1")
    monkeypatch.setattr(module, "_MAX_DEPTH", depth)
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path)
    assert caught.value.code == "STORAGE_OBSERVATION_LIMIT"


def test_deadline_rejects_late_observation(tmp_path, monkeypatch):
    times = iter((0.0, 31.0))
    monkeypatch.setattr(module, "monotonic", lambda: next(times))
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path)
    assert caught.value.code == "STORAGE_OBSERVATION_LIMIT"


def test_clock_rollback_rejects_observation(tmp_path, monkeypatch):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    times = iter((now, now - timedelta(seconds=1)))
    monkeypatch.setattr(module, "_now", lambda: next(times))
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path)
    assert caught.value.code == "STORAGE_OBSERVATION_CHANGED"


@_POSIX_ONLY
def test_directory_symlink_swap_fails_without_following_target(tmp_path, monkeypatch):
    root = tmp_path / "root"
    selected = put(root, "child/file", b"local")
    outside = put(tmp_path, "outside/private-file", b"private")
    original = os.open

    def replaced(path, *args, **kwargs):
        if path == "child":
            selected.unlink()
            selected.parent.rmdir()
            selected.parent.symlink_to(outside.parent, target_is_directory=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {replaced})
    monkeypatch.setattr(os, "open", replaced)
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(root)
    assert caught.value.code == "STORAGE_OBSERVATION_FAILED"
    assert "private-file" not in str(caught.value)


def test_root_ancestor_symlink_is_refused(tmp_path, make_link):
    selected = tmp_path / "alias"
    actual = tmp_path / "actual"
    actual.mkdir()
    make_link(selected, actual)
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(selected)
    assert caught.value.code == "STORAGE_ROOT_INVALID"


@_POSIX_ONLY
def test_missing_platform_primitive_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_fd", set())
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path)
    assert caught.value.code == "STORAGE_OBSERVATION_UNSUPPORTED"


@pytest.mark.parametrize("destination", ["", "a/../b", "bad\x00path"])
def test_ambiguous_export_scope_is_rejected(tmp_path, destination):
    with pytest.raises(AdhelmError) as caught:
        module.observe_local_storage(tmp_path, output_directory=destination)
    assert caught.value.code == "STORAGE_ROOT_INVALID"

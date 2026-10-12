"""Adversarial regressions for optional local artifact safety boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_saved_reports import retain
from typer.testing import CliRunner

from adhelm import __version__, artifact_cli, artifact_workflows, artifacts, workflows
from adhelm.artifacts import (
    ArtifactContext,
    ArtifactGenerationError,
    ArtifactMetadata,
    ArtifactTaskOverride,
    RetainedArtifactInput,
    generate_artifacts,
    resolve_artifact_policy,
)
from adhelm.canonical import canonical_bytes
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences, ResultEnvelope
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_reports import load_report

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "O3.1: this case renames or closes a descriptor that the POSIX implementation holds. "
        "On Windows the destination is pinned by handles and cannot be renamed; see "
        "tests/test_windows_filesystem.py."
    ),
)
_KEY = b"k" * 32
_SOURCE = "a" * 64
_CHECKPOINT = "b" * 64


def _retained() -> RetainedArtifactInput:
    return RetainedArtifactInput(
        NormalizedReport(
            envelope=ReportEnvelope(
                report_id="report-local",
                provider="google_ads",
                source_account_ref="account-local",
                query_contract_hash="c" * 64,
                window_start=date(2026, 1, 1),
                window_end=date(2026, 1, 1),
                source_timezone="UTC",
                report_timezone="UTC",
                currency="USD",
                attribution_model="provider",
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                data_quality_statuses=["COMPLETE"],
                provider_api_version="v1",
                connector_version="v1",
            ),
            performance_rows=[
                PerformanceRow(
                    report_id="report-local",
                    resource_level="CAMPAIGN",
                    metric_grain="CAMPAIGN",
                    resource_type="campaign",
                    resource_ref="campaign-local",
                    impressions=1,
                    clicks=0,
                    spend={"minor_units": 0, "currency": "USD"},
                )
            ],
        ),
        _SOURCE,
        _CHECKPOINT,
    )


def _decision(directory: Path, *, maximum_bytes: int = 65_536):
    return resolve_artifact_policy(
        ArtifactContext("report", "completion", "result", "google_ads"),
        OutputPreferences(max_output_bytes=maximum_bytes),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(directory)),
    )


def test_blank_task_destination_does_not_fall_back_to_saved_directory(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="absolute local path"):
        resolve_artifact_policy(
            ArtifactContext("report", "completion", "result", "google_ads"),
            OutputPreferences(output_directory=str(tmp_path)),
            task_override=ArtifactTaskOverride(formats=("json",), output_directory=""),
        )


def test_retry_after_directory_fsync_failure_reconfirms_reused_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(tmp_path)
    original = artifacts._fsync_directory
    calls = 0

    def fail_once(directory):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic directory durability failure")
        original(directory)

    monkeypatch.setattr(artifacts, "_fsync_directory", fail_once)
    with pytest.raises(ArtifactGenerationError) as first:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert first.value.failure_code == "DIRECTORY_FSYNC_FAILED"
    assert len(first.value.completed) == 1

    retry = generate_artifacts(_retained(), decision, key=_KEY)
    assert retry[0].reused is True
    assert calls == 2


def test_post_link_temp_cleanup_failure_keeps_published_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = artifacts.unlink_child

    def fail_only_artifact_temp(directory, name):
        if name.startswith(".artifact-") and ".tmp-" in name:
            raise OSError("synthetic cleanup failure")
        return original(directory, name)

    monkeypatch.setattr(artifacts, "unlink_child", fail_only_artifact_temp)
    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), _decision(tmp_path), key=_KEY)
    assert caught.value.failure_code == "TEMPORARY_CLEANUP_FAILED"
    assert len(caught.value.completed) == 1
    assert caught.value.completed[0].path.is_file()


def test_raced_exclusive_publication_rechecks_same_name_for_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = artifacts._create_bytes
    winner_created = False

    def winner_then_collision(directory, name, contents):
        nonlocal winner_created
        if not winner_created:
            winner_created = True
            original(directory, name, contents)
            raise FileExistsError("simulated competing publisher")
        return original(directory, name, contents)

    monkeypatch.setattr(artifacts, "_create_bytes", winner_then_collision)
    result = generate_artifacts(_retained(), _decision(tmp_path), key=_KEY)
    assert result[0].reused is True
    assert len(list(tmp_path.glob("artifact-*.json"))) == 1


def test_transient_link_during_concurrent_publication_retries_then_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(tmp_path)
    generate_artifacts(_retained(), decision, key=_KEY)
    original = artifacts._existing_file_matches
    calls = 0

    def transient_then_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise artifacts._ArtifactLinkInProgress()
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_existing_file_matches", transient_then_read)
    result = generate_artifacts(_retained(), decision, key=_KEY)
    assert result[0].reused is True
    assert calls >= 2


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason=(
        "O3.1: a FIFO cannot live at a directory path on Windows (os.mkfifo does not exist; a "
        "named pipe lives in the \\\\.\\pipe\\ namespace). Pipe and device inputs are rejected by "
        "the native tests in tests/test_windows_filesystem.py."
    ),
)
def test_fifo_at_deterministic_target_fails_closed_without_a_blocking_read(tmp_path: Path) -> None:
    decision = _decision(tmp_path)
    generated = generate_artifacts(_retained(), decision, key=_KEY)[0]
    generated.path.unlink()
    os.mkfifo(generated.path)

    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert caught.value.failure_code == "UNSAFE_ARTIFACT_DESTINATION"


def test_world_readable_reuse_target_fails_closed(tmp_path: Path) -> None:
    decision = _decision(tmp_path)
    generated = generate_artifacts(_retained(), decision, key=_KEY)[0]
    if sys.platform == "win32":
        # The Windows counterpart of a world-readable mode: an ACE for Everyone.
        subprocess.run(
            ["icacls", str(generated.path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True
        )
    else:
        generated.path.chmod(0o644)

    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert caught.value.failure_code == "UNSAFE_ARTIFACT_DESTINATION"


@_POSIX_ONLY
def test_destination_symlink_swap_during_rendering_cannot_redirect_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    decision = _decision(selected)
    original = artifacts._document

    def swap_then_render(*args, **kwargs):
        selected.rename(tmp_path / "selected-before-swap")
        selected.symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_document", swap_then_render)
    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert caught.value.failure_code == "UNSAFE_ARTIFACT_DESTINATION"
    assert list(outside.iterdir()) == []


def test_target_swap_after_publication_never_returns_redirected_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = artifacts._fsync_directory

    def swap_after_fsync(directory):
        original(directory)
        target = next(tmp_path.glob("artifact-*.json"))
        target.unlink()
        target.symlink_to(outside / "unrelated", target_is_directory=False)

    monkeypatch.setattr(artifacts, "_fsync_directory", swap_after_fsync)
    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert caught.value.failure_code == "UNSAFE_ARTIFACT_DESTINATION"
    assert caught.value.completed == ()
    assert list(outside.iterdir()) == []


def _fail_the_close_of(
    monkeypatch: pytest.MonkeyPatch, directory: artifacts._ArtifactDirectory
) -> None:
    """Make closing this opened directory fail once, after it has really been closed.

    A POSIX directory is a descriptor, so ``os.close`` reports the failure. A Windows
    directory is a pinned handle chain, so the first ``CloseHandle`` of one of its
    handles reports failure; ``PinnedDirectory.close`` still closes every handle and
    then raises ``OSError``. Either way the code under test sees an ``OSError`` from
    ``directory.close()``, exactly once.
    """
    failed = False
    if sys.platform == "win32":
        from adhelm import windows_filesystem

        chain = set(directory.handle._handles)  # type: ignore[union-attr]
        real_close_handle = windows_filesystem._CloseHandle

        def close_handle_then_fail(handle: int) -> int:
            nonlocal failed
            closed = real_close_handle(handle)
            if handle in chain and not failed:
                failed = True
                return 0
            return int(closed)

        monkeypatch.setattr(windows_filesystem, "_CloseHandle", close_handle_then_fail)
        return
    descriptor = directory.handle
    real_close = artifacts.os.close

    def close_then_fail(closing: int) -> None:
        nonlocal failed
        real_close(closing)
        if closing == descriptor and not failed:
            failed = True
            raise OSError("synthetic final descriptor close failure")

    monkeypatch.setattr(artifacts.os, "close", close_then_fail)


def test_held_directory_close_failure_preserves_published_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(tmp_path)
    original_open = artifacts._open_artifact_directory
    opens = 0

    def track_held(path: Path):
        nonlocal opens
        directory = original_open(path)
        opens += 1
        if opens == 2:  # _safe_existing_directory, then the held publication directory
            _fail_the_close_of(monkeypatch, directory)
        return directory

    monkeypatch.setattr(artifacts, "_open_artifact_directory", track_held)
    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert caught.value.failure_code == "DIRECTORY_CLOSE_FAILED"
    assert len(caught.value.completed) == 1
    assert caught.value.completed[0].path.is_file()


def test_post_fsync_verifier_close_failure_keeps_completed_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(tmp_path)
    original_open = artifacts._open_artifact_directory
    opens = 0

    def track_verifier(path: Path):
        nonlocal opens
        directory = original_open(path)
        opens += 1
        # The fifth directory open is _assert_directory_current after directory fsync.
        if opens == 5:
            _fail_the_close_of(monkeypatch, directory)
        return directory

    monkeypatch.setattr(artifacts, "_open_artifact_directory", track_verifier)
    with pytest.raises(ArtifactGenerationError) as caught:
        generate_artifacts(_retained(), decision, key=_KEY)
    assert caught.value.failure_code == "DIRECTORY_CLOSE_FAILED"
    assert len(caught.value.completed) == 1
    assert caught.value.completed[0].path.is_file()


def _nested_output_directory(tmp_path: Path) -> Path:
    directory = tmp_path
    for index in range(3):
        directory = directory / (f"part{index}-" + "x" * 100)
        directory.mkdir()
    return directory


def test_direct_cli_reserves_longest_partial_failure_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    output = _nested_output_directory(tmp_path)
    selected = retain(Ledger(root), "a")
    _, provenance = load_report(Ledger(root), selected)
    decision = _decision(output, maximum_bytes=65_536)
    summary = {
        "source_record_digest": selected,
        "checkpoint_digest": provenance["checkpoint_digest"],
        "effective_policy": artifact_cli._decision_data(decision),
    }
    old_preview = {
        **summary,
        "artifact_failure_code": "TEMPORARY_CLEANUP_FAILED",
        "retained_source_unchanged": True,
        "artifacts": [
            {
                "path": str(output / ("artifact-" + "f" * 16 + "-" + "f" * 16 + ".v9999.json")),
                "format": "json",
                "reused": False,
                "artifact_digest": "f" * 64,
            }
        ],
    }
    old_maximum = len(
        canonical_bytes(
            ResultEnvelope(
                tool_version=__version__, command="artifacts report", outcome="OK", data=old_preview
            )
        )
    )
    assert old_maximum >= 1024
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=old_maximum)))
    called = False

    def must_not_generate(*args: object, **kwargs: object) -> tuple[ArtifactMetadata, ...]:
        nonlocal called
        called = True
        raise AssertionError("preflight must reject before artifact generation")

    monkeypatch.setattr(artifact_cli, "generate_artifacts", must_not_generate)
    monkeypatch.setattr(
        "adhelm.credentials.export_key", lambda: pytest.fail("key should not be read")
    )
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"formats": ["json"], "output_directory": str(output)}))
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(root), "artifacts", "report", selected, "--input", str(request)],
    )
    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert called is False


@_POSIX_ONLY
def test_cache_path_swap_cannot_redirect_descriptor_relative_cache_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = artifact_workflows._open_cache_directory(root)
    cache_path = root / "artifact-cache"
    moved = root / "cache-before-swap"
    try:
        cache_path.rename(moved)
        cache_path.symlink_to(outside, target_is_directory=True)
        with pytest.raises(AdhelmError) as caught:
            artifact_workflows._write_cache_payload(
                root, directory, b'{"entries":[],"schema_version":1}\n'
            )
    finally:
        directory.close()
    assert caught.value.code == "UNSAFE_ARTIFACT_CACHE"
    assert list(outside.iterdir()) == []
    assert list(moved.iterdir()) == []


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason=(
        "O3.1: a FIFO cannot live at a directory path on Windows (os.mkfifo does not exist; a "
        "named pipe lives in the \\\\.\\pipe\\ namespace). Pipe and device inputs are rejected by "
        "the native tests in tests/test_windows_filesystem.py."
    ),
)
def test_cache_fifo_fails_closed_without_a_blocking_read(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    directory = artifact_workflows._open_cache_directory(root)
    try:
        os.mkfifo(root / "artifact-cache" / "report-artifacts.json")
        with pytest.raises(AdhelmError) as caught:
            artifact_workflows._read_cache_payload(directory)
    finally:
        directory.close()
    assert caught.value.code == "ARTIFACT_CACHE_INVALID"


def test_report_cli_keeps_retained_and_artifact_references_under_partial_output_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    long_path = "/" + "x" * 2_000
    artifact_digest = "d" * 64

    def failed_artifact_report(*args, **kwargs):
        return {
            "performance_rows": [{"resource_ref": "opaque-row"}],
            "conversion_rows": [],
            "retained_record_digest": _SOURCE,
            "artifact_result": {
                "effective_policy": {"outcome": "GENERATE", "reason": "MATCHING_SAVED_RULE"},
                "source_record_digest": _SOURCE,
                "checkpoint_digest": _CHECKPOINT,
                "reused_prior": False,
                "generation_failure_code": "DIRECTORY_FSYNC_FAILED",
                "artifacts": [
                    {
                        "path": long_path,
                        "format": "json",
                        "reused": False,
                        "artifact_digest": artifact_digest,
                    }
                ],
            },
        }

    monkeypatch.setattr(workflows, "run_report", failed_artifact_report)
    input_file = tmp_path / "query.json"
    input_file.write_text(json.dumps({}))
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(root),
            "report",
            "google-ads",
            "reader",
            "--input",
            str(input_file),
        ],
    )
    assert result.exit_code == 5, result.stdout
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "PARTIAL"
    assert payload["data"]["retained_record_digest"] == _SOURCE
    assert payload["data"]["artifact_result"]["generation_failure_code"] == (
        "DIRECTORY_FSYNC_FAILED"
    )
    assert payload["artifact_refs"] == ["artifact:sha256:" + artifact_digest]
    assert long_path not in result.stdout


def test_report_cli_minimum_bound_preserves_three_completed_artifact_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    artifact_digests = [character * 64 for character in "def"]

    def failed_artifact_report(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "performance_rows": [{} for _ in range(500)],
            "conversion_rows": [{} for _ in range(500)],
            "retained_record_digest": _SOURCE,
            "artifact_result": {
                "effective_policy": {
                    "outcome": "NEEDS_CONFIGURATION",
                    "reason": "Y" * 80,
                },
                "source_record_digest": _SOURCE,
                "checkpoint_digest": _CHECKPOINT,
                "reused_prior": False,
                "generation_failure_code": "X" * 80,
                "artifacts": [
                    {
                        "path": "/" + "z" * 2_000,
                        "format": "json",
                        "reused": False,
                        "artifact_digest": artifact_digest,
                    }
                    for artifact_digest in artifact_digests
                ],
            },
        }

    monkeypatch.setattr(workflows, "run_report", failed_artifact_report)
    input_file = tmp_path / "query.json"
    input_file.write_text(json.dumps({}))
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(root),
            "report",
            "google-ads",
            "reader",
            "--input",
            str(input_file),
        ],
    )
    assert result.exit_code == 5, result.stdout
    assert len(result.stdout.encode()) <= 1024
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "PARTIAL"
    assert payload["data"]["retained_record_digest"] == _SOURCE
    assert payload["data"]["artifact_result"] == {
        "artifact_count": 3,
        "generation_failure_code": "X" * 80,
    }
    assert payload["artifact_refs"] == ["artifact:sha256:" + value for value in artifact_digests]

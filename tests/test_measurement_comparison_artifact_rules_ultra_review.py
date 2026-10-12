"""Independent adversarial checks for saved comparison-artifact rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_measurement_comparison_cli import _pair
from typer.testing import CliRunner

from adhelm import artifacts, measurement_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import ArtifactRule, Configuration, OutputPreferences


def _invoke(root: Path, before: str, after: str):
    return CliRunner().invoke(
        app, ["--data-dir", str(root), "measurement", "compare", before, after]
    )


def _matching_preferences(output: Path, *, formats: list[str], maximum: int = 65_536):
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=["comparison"],
        output_directory=str(output),
        workflow_rules=[
            ArtifactRule(
                workflow="diagnostics",
                stage="completion",
                providers=["ga4_admin"],
                profiles=["private.measurement.profile"],
                formats=formats,
            )
        ],
        max_output_bytes=maximum,
    )


@pytest.mark.parametrize("mode", ["ON_REQUEST", "NONMATCHING_RULE", "PURPOSE_DISABLED"])
def test_saved_rules_are_the_only_path_that_creates_comparison_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    before, after = _pair(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    if mode == "ON_REQUEST":
        preferences = OutputPreferences(output_directory=str(output))
    elif mode == "NONMATCHING_RULE":
        preferences = OutputPreferences(
            artifact_mode="RULES",
            purposes=["comparison"],
            output_directory=str(output),
            workflow_rules=[
                ArtifactRule(
                    workflow="diagnostics",
                    stage="completion",
                    providers=["openai_ads"],
                    profiles=["private.measurement.profile"],
                    formats=["json"],
                )
            ],
        )
    else:
        preferences = OutputPreferences(
            artifact_mode="RULES",
            output_directory=str(output),
            workflow_rules=[
                ArtifactRule(
                    workflow="diagnostics",
                    stage="completion",
                    providers=["ga4_admin"],
                    profiles=["private.measurement.profile"],
                    formats=["json"],
                )
            ],
        )
    save_configuration(tmp_path, Configuration(output=preferences))
    key_calls: list[bytes] = []

    def key() -> bytes:
        key_calls.append(b"k")
        return b"k" * 32

    monkeypatch.setattr(measurement_cli, "export_key", key)
    result = _invoke(tmp_path, before, after)

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "HISTORICAL_CONFIGURATION_COMPARISON"
    assert envelope["artifact_refs"] == []
    assert "artifact_result" not in envelope["data"]
    assert key_calls == [b"k"]  # Existing inline pseudonymization still needs one key.
    assert list(output.iterdir()) == []


def test_matching_rule_preflights_full_receipt_then_reuses_one_key_for_output_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        Configuration(output=_matching_preferences(output, formats=["json", "html"])),
    )
    key_calls: list[bytes] = []

    def key() -> bytes:
        key_calls.append(b"k")
        return b"k" * 32

    monkeypatch.setattr(measurement_cli, "export_key", key)
    result = _invoke(tmp_path, before, after)

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    artifact_result = envelope["data"]["artifact_result"]
    assert envelope["outcome"] == "HISTORICAL_CONFIGURATION_COMPARISON"
    assert artifact_result["effective_policy"]["outcome"] == "GENERATE"
    assert artifact_result["effective_policy"]["reason"] == "MATCHING_SAVED_RULE"
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 2
    assert {path.suffix for path in output.iterdir()} == {".json", ".html"}
    assert key_calls == [b"k"]


def test_matching_rule_that_cannot_fit_complete_receipt_fails_before_key_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    # A JSON artifact itself fits 4096 bytes for this fixed pair, but the full
    # inline result plus its worst-case artifact receipt does not. This proves
    # the command preflights the complete result envelope before key access.
    save_configuration(
        tmp_path,
        Configuration(output=_matching_preferences(output, formats=["json"], maximum=4096)),
    )

    def forbidden() -> bytes:
        raise AssertionError("A full receipt preflight must precede sharing-key access")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    result = _invoke(tmp_path, before, after)

    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_invalid_matching_rule_and_source_drift_fail_closed_before_any_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    invalid = OutputPreferences(
        artifact_mode="RULES",
        purposes=["comparison"],
        workflow_rules=[
            ArtifactRule(
                workflow="diagnostics",
                stage="completion",
                providers=["ga4_admin"],
                profiles=["private.measurement.profile"],
                formats=["json"],
            )
        ],
    )
    save_configuration(tmp_path, Configuration(output=invalid))

    def forbidden() -> bytes:
        raise AssertionError("Invalid automatic configuration cannot access a sharing key")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    invalid_result = _invoke(tmp_path, before, after)
    assert invalid_result.exit_code != 0, invalid_result.output
    assert json.loads(invalid_result.output)["diagnostics"][0]["code"] == (
        "ARTIFACT_CONFIGURATION_REQUIRED"
    )

    output = tmp_path / "drift-artifacts"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        Configuration(output=_matching_preferences(output, formats=["json"])),
        replace=True,
    )

    def change_checkpoint() -> bytes:
        _pair(tmp_path, provider="openai_ads")
        return b"k" * 32

    monkeypatch.setattr(measurement_cli, "export_key", change_checkpoint)
    drift_result = _invoke(tmp_path, before, after)
    assert drift_result.exit_code != 0, drift_result.output
    assert json.loads(drift_result.output)["diagnostics"][0]["code"] == (
        "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"
    )
    assert list(output.iterdir()) == []


def test_partial_writer_failure_keeps_full_comparison_and_completed_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        Configuration(output=_matching_preferences(output, formats=["json", "html"])),
    )
    key_calls: list[bytes] = []

    def key() -> bytes:
        key_calls.append(b"k")
        return b"k" * 32

    original_write = artifacts._write_versioned
    write_calls = 0

    def fail_second(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise OSError("fixture-only second artifact failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(measurement_cli, "export_key", key)
    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = _invoke(tmp_path, before, after)

    assert result.exit_code == 5, result.output
    envelope = json.loads(result.output)
    artifact_result = envelope["data"]["artifact_result"]
    assert envelope["outcome"] == "PARTIAL"
    assert len(envelope["data"]["facts"]) == envelope["total_count"]
    assert artifact_result["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 1
    assert len(list(output.iterdir())) == 1
    assert key_calls == [b"k"]
    assert "fixture-only second artifact failure" not in result.output

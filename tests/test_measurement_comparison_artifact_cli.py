"""Installed comparison artifact composition uses synthetic history and sharing keys."""

import json
from pathlib import Path

import pytest
from test_measurement_comparison_cli import _pair
from typer.testing import CliRunner

from adhelm import credentials
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import compare_measurements
from adhelm.measurement_comparison_artifacts import (
    revalidate_measurement_comparison_artifact_source,
)
from adhelm.models import Configuration, OutputPreferences


def _request(root: Path, *, formats: list[str] | None = None) -> tuple[Path, Path]:
    output = root / "artifacts"
    output.mkdir(mode=0o700)
    request = root / "render-request.json"
    request.write_text(
        json.dumps({"formats": formats or ["json", "csv", "html"], "output_directory": str(output)})
    )
    return request, output


def _render(root: Path, before: str, after: str, request: Path):
    return CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(root),
            "artifacts",
            "measurement-comparison",
            before,
            after,
            "--input",
            str(request),
        ],
    )


@pytest.mark.parametrize("provider", ["ga4_admin", "openai_ads"])
def test_rendered_formats_keep_pair_and_caveats_without_mutating_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    before, after = _pair(tmp_path, provider=provider)
    request, output = _request(tmp_path)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=131072)))
    monkeypatch.setattr(credentials, "export_key", lambda: b"k" * 32)
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    result = _render(tmp_path, before, after, request)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]
    assert data["before_record_digest"] == before and data["after_record_digest"] == after
    assert data["checkpoint_digest"] == checkpoint
    assert data["execution_authority"] is data["remote_mutations"] is False
    assert data["opening_or_editing_authorizes_execution"] is False
    assert len(envelope["artifact_refs"]) == 3
    files = sorted(output.iterdir())
    assert {file.suffix for file in files} == {".json", ".csv", ".html"}
    original_bytes = {file.name: file.read_bytes() for file in files}
    for file in files:
        text = file.read_text()
        assert before in text and after in text and data["comparison_digest"] in text
        assert "NOT_EVALUATED" in text and "UNKNOWN" in text
        assert "FIXTURE_VERIFIED_LIVE_UNVERIFIED" in text
        for private in (
            "private.measurement.profile",
            "acct_fixture",
            "key_event.701",
            "property.123",
        ):
            assert private not in text
    again = _render(tmp_path, before, after, request)
    assert again.exit_code == 0, again.output
    assert {file.name: file.read_bytes() for file in output.iterdir()} == original_bytes
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint


@pytest.mark.parametrize("bound", ["rows", "bytes"])
def test_complete_content_bound_fails_before_key_or_any_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    before, after = _pair(tmp_path)
    request, output = _request(tmp_path)
    preferences = (
        OutputPreferences(max_rows=1, max_output_bytes=131072)
        if bound == "rows"
        else OutputPreferences(max_output_bytes=1024)
    )
    save_configuration(tmp_path, Configuration(output=preferences))

    def forbidden():
        raise AssertionError("Invalid artifact output cannot access a sharing key")

    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _render(tmp_path, before, after, request)
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["outcome"] == "FAILED_CLOSED"
    assert not list(output.iterdir())


def test_checkpoint_change_after_preflight_fails_before_first_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    request, output = _request(tmp_path)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=131072)))

    def change_checkpoint():
        _pair(tmp_path, provider="openai_ads")
        return b"k" * 32

    monkeypatch.setattr(credentials, "export_key", change_checkpoint)
    result = _render(tmp_path, before, after, request)
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["outcome"] == "FAILED_CLOSED"
    assert not list(output.iterdir())


def test_constructed_float_schema_cannot_be_laundered_by_artifact_revalidation(
    tmp_path: Path,
) -> None:
    pair = _pair(tmp_path)
    ledger = Ledger(tmp_path)
    comparison = compare_measurements(ledger, *pair)
    with pytest.raises(AdhelmError):
        revalidate_measurement_comparison_artifact_source(
            ledger, comparison.model_copy(update={"schema_version": 1.0})
        )


def test_artifact_command_and_closed_request_are_discoverable() -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert "artifacts measurement-comparison" in discovery["commands"]
    result = runner.invoke(app, ["schema", "measurement-comparison-artifact-render"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "schema_version",
        "formats",
        "output_directory",
        "replacement_authorized",
    }

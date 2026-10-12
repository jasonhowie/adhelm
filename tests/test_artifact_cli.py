"""Actual local CLI preference/artifact workflows use verified retained evidence."""

import json
from pathlib import Path

import pytest
from test_saved_reports import retain
from typer.testing import CliRunner

from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences

runner = CliRunner()


def _input(path: Path, value: object) -> str:
    path.write_text(json.dumps(value))
    return str(path)


def test_explicit_artifact_request_overrides_off_without_persisting_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"a" * 32)
    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(root, Configuration(output=OutputPreferences(artifact_mode="OFF")))
    selected = retain(Ledger(root), "a")
    request = _input(
        tmp_path / "request.json", {"formats": ["json", "html"], "output_directory": str(output)}
    )
    command = ["--data-dir", str(root), "artifacts", "report", selected, "--input", request]
    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.stdout
    data = json.loads(first.stdout)["data"]
    assert data["source_record_digest"] == selected
    assert data["effective_policy"]["reason"] == "EXPLICIT_USER_REQUEST"
    assert {item["format"] for item in data["artifacts"]} == {"json", "html"}
    for item in data["artifacts"]:
        content = Path(item["path"]).read_text()
        assert "account:private" not in content
        assert "campaign:private" not in content
        assert "opaque:" in content
        assert selected in content
    assert load_configuration(root).output.artifact_mode == "OFF"
    again = runner.invoke(app, command)
    assert again.exit_code == 0, again.stdout
    assert all(item["reused"] for item in json.loads(again.stdout)["data"]["artifacts"])
    assert len(list(output.iterdir())) == 2


def test_saved_preferences_and_policy_preview_create_no_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    setting = _input(
        tmp_path / "settings.json",
        {
            "artifact_mode": "RULES",
            "purposes": ["result"],
            "formats": ["json"],
            "output_directory": str(output),
            "workflow_rules": [
                {
                    "workflow": "report",
                    "stage": "completion",
                    "providers": ["google_ads"],
                    "formats": ["json"],
                }
            ],
        },
    )
    base = ["--data-dir", str(root), "artifacts"]
    saved = runner.invoke(app, [*base, "configure", "--input", setting])
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(saved.stdout)["data"]["provider_operations_authorized"] is False
    query = _input(
        tmp_path / "context.json",
        {
            "workflow": "report",
            "stage": "completion",
            "purpose": "result",
            "provider": "google_ads",
        },
    )
    preview = runner.invoke(app, [*base, "policy", "--input", query])
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["outcome"] == "GENERATE"
    assert list(output.iterdir()) == []
    assert not (root / "history").exists()


def test_reference_output_bound_fails_before_artifact_write_or_export_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    def forbidden() -> bytes:
        pytest.fail("Artifact prerequisite failure must precede credential-store access")

    monkeypatch.setattr(adhelm.credentials, "export_key", forbidden)
    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    selected = retain(Ledger(root), "a")
    request = _input(
        tmp_path / "request.json",
        {"formats": ["json", "html", "csv"], "output_directory": str(output)},
    )
    result = runner.invoke(
        app, ["--data-dir", str(root), "artifacts", "report", selected, "--input", request]
    )
    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_unretained_report_never_creates_an_artifact(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    request = _input(
        tmp_path / "request.json", {"formats": ["json"], "output_directory": str(output)}
    )
    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "artifacts", "report", "a" * 64, "--input", request],
    )
    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "REPORT_NOT_RETAINED"
    assert list(output.iterdir()) == []


def test_local_write_failure_reports_completed_artifact_and_retained_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.artifacts
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"a" * 32)
    original_write = adhelm.artifacts._write_versioned
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private OS failure detail")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(adhelm.artifacts, "_write_versioned", fail_second)
    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    selected = retain(Ledger(root), "a")
    request = _input(
        tmp_path / "request.json", {"formats": ["json", "html"], "output_directory": str(output)}
    )
    result = runner.invoke(
        app, ["--data-dir", str(root), "artifacts", "report", selected, "--input", request]
    )
    assert result.exit_code == 5, result.stdout
    body = json.loads(result.stdout)
    assert body["outcome"] == "PARTIAL"
    assert body["data"]["source_record_digest"] == selected
    assert body["data"]["retained_source_unchanged"] is True
    assert len(body["data"]["artifacts"]) == 1
    assert Path(body["data"]["artifacts"][0]["path"]).is_file()
    assert "private OS failure detail" not in result.stdout
    assert Ledger(root).checkpoint()["status"] == "VERIFIED_CHECKPOINT"

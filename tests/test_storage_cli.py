"""Installed storage command uses temporary local data and no provider effects."""

import json

from typer.testing import CliRunner

from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences


def test_installed_storage_usage_emits_complete_redacted_observation(tmp_path, monkeypatch):
    from adhelm import credentials

    def forbidden(*args, **kwargs):
        raise AssertionError("Storage inspection must not access credentials")

    monkeypatch.setattr(credentials, "_backend", forbidden)
    monkeypatch.setattr(credentials, "export_key", forbidden)
    save_configuration(tmp_path, Configuration())
    secret_named = tmp_path / "private-customer-name"
    secret_named.write_bytes(b"private file content")
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "storage", "usage"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["command"] == "storage usage"
    assert parsed["outcome"] == "LOCAL_OBSERVATION"
    assert parsed["data"]["total_apparent_bytes"] == sum(map(len, before.values()))
    assert parsed["data"]["cleanup_authorized"] is False
    assert parsed["artifact_refs"] == []
    assert "private-customer-name" not in result.output
    assert "private file content" not in result.output
    assert before == {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }


def test_small_output_bound_refuses_without_partial_category_totals(tmp_path):
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "storage", "usage"])
    parsed = json.loads(result.output)
    assert result.exit_code == 3
    assert parsed["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert "Increase saved max_output_bytes" in parsed["diagnostics"][0]["next_action"]
    assert "categories" not in parsed["data"]
    assert len(result.output.encode()) <= 1024


def test_discovery_registers_storage_usage_without_scanning_data(tmp_path, monkeypatch):
    from adhelm import storage_cli

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not scan storage")

    monkeypatch.setattr(storage_cli, "observe_local_storage", forbidden)
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path / "absent"), "discover"])
    assert result.exit_code == 0, result.output
    assert "storage usage" in result.output
    assert not (tmp_path / "absent").exists()

"""Root report/image commands preserve side-effect and error boundaries."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import creative, workflows
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, Profile


def test_report_pipe_rejects_before_credential_history_or_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_pipe
):
    directory = tmp_path / "data"
    profile = Profile(
        profile_id="openai-reader",
        provider="openai_ads",
        account_ref="account_fixture",
        principal_ref="principal",
        client_ref="client",
        scopes=["ads:read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="credential-reference",
    )
    save_configuration(directory, Configuration(profiles=[profile]))
    before = (directory / "config.json").read_bytes()
    pipe = make_pipe("private-report-query")

    def forbidden(*args, **kwargs):
        pytest.fail("The invalid query file cannot reach credentials or ledger work")

    monkeypatch.setattr(workflows, "credential", forbidden)
    monkeypatch.setattr(workflows, "export_key", forbidden)
    monkeypatch.setattr(workflows, "Ledger", forbidden)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "report",
            "openai-ads",
            profile.profile_id,
            "--input",
            str(pipe),
        ],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INPUT_FILE_INVALID"
    assert "private-report-query" not in result.output
    assert (directory / "config.json").read_bytes() == before
    assert not (directory / "ledger").exists()


def test_oversized_image_preserves_existing_diagnostic_without_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    selected = tmp_path / "private-image-name"
    with selected.open("wb") as handle:
        handle.truncate(creative.MAX_IMAGE_BYTES + 1)

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized image metadata must reject before decoding")

    monkeypatch.setattr(creative, "analyze_image", forbidden)
    directory = tmp_path / "data"
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(directory), "creative", "analyze", "--input", str(selected)],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "CREATIVE_MEDIA_TOO_LARGE"
    assert "private-image-name" not in result.output
    assert not directory.exists()


def test_discovery_reports_input_limits_and_does_not_promise_filesystem_latency():
    result = CliRunner().invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    contract = json.loads(result.output)["data"]["input_files"]
    assert contract["json_maximum_bytes"] == 1_048_576
    assert contract["local_image_maximum_bytes"] == creative.MAX_IMAGE_BYTES
    assert contract["required_type"] == "REGULAR_FILE"
    assert contract["pipes_devices_sockets"] == "REJECTED"
    assert contract["filesystem_latency_guaranteed"] is False

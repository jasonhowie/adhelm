"""CLI boundaries for explicit static Google Ads campaign PAUSE plan artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_google_ads_pause_cli import _action
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import credentials, google_ads_pause_artifact_cli
from adhelm.config import load_configuration, save_configuration
from adhelm.ledger import Ledger
from adhelm.models import OutputPreferences
from adhelm.saved_google_ads_pause_plans import retain_google_ads_campaign_pause_plan

_PROFILE = "google-ads-edit"


def _retain(directory: Path) -> str:
    """Retain the exact closed fixture action without a provider boundary."""
    stored = retain_google_ads_campaign_pause_plan(Ledger(directory), _action())
    return cast(str, stored["record_digest"])


def _artifact_request(
    directory: Path,
    destination: Path,
    *,
    formats: list[str] | None = None,
) -> Path:
    request = directory / "google-ads-pause-artifact-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "formats": formats or ["json", "csv", "html"],
                "output_directory": str(destination),
            }
        ),
        encoding="utf-8",
    )
    return request


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "artifacts", "google-ads-campaign-pause", *arguments],
    )


def _data(result: Any) -> dict[str, Any]:
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _widen_output(directory: Path) -> None:
    configuration = load_configuration(directory)
    save_configuration(
        directory,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=131_072)}),
        replace=True,
    )


def test_explicit_artifact_renders_verified_static_history_without_new_provider_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "google-ads-pause-artifacts"
    destination.mkdir(mode=0o700)
    _widen_output(tmp_path)
    request = _artifact_request(tmp_path, destination)
    before_checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)

    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code == 0, result.output
    envelope = _data(result)
    data = cast(dict[str, Any], envelope["data"])
    artifact_result = cast(dict[str, Any], data["artifact_result"])
    assert envelope["outcome"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_ARTIFACT_NO_EXECUTION_AUTHORITY"
    assert data["source_record_digest"] == record_digest
    assert data["execution_authority"] is data["remote_mutations"] is False
    assert data["opening_or_editing_authorizes_execution"] is False
    assert data["historical_evidence_status"] == "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    assert artifact_result["generation_failure_code"] is None
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 3
    assert {item["format"] for item in artifact_result["artifacts"]} == {"json", "csv", "html"}
    assert {path.suffix for path in destination.iterdir()} == {".json", ".csv", ".html"}
    rendered = result.output + "\n" + "\n".join(path.read_text() for path in destination.iterdir())
    for private in (
        _PROFILE,
        "google_ads.customer.1234567890",
        "1234567890",
        "0987654321",
        "credential-binding:sha256:",
        "google_oidc_principal_v1.",
        "fixture-google-ads.apps.googleusercontent.com",
    ):
        assert private not in rendered
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == before_checkpoint


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (b'{"schema_version":1.0}', "SCHEMA_VALIDATION_FAILED"),
        (b'{"schema_version":true}', "SCHEMA_VALIDATION_FAILED"),
        (
            b'{"schema_version":1,"formats":["json","json"],"output_directory":"/tmp"}',
            "SCHEMA_VALIDATION_FAILED",
        ),
        (b'{"schema_version":1,"output_directory":"/tmp"}', "SCHEMA_VALIDATION_FAILED"),
        pytest.param(b"x" * 1_048_577, "INPUT_TOO_LARGE", id="oversized"),
    ],
)
def test_explicit_request_is_closed_before_configuration_history_or_sharing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
    expected: str,
) -> None:
    request = tmp_path / "private-artifact-input.json"
    request.write_bytes(contents)

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "invalid artifact input reached configuration, history, or a sharing key"
        )

    monkeypatch.setattr(google_ads_pause_artifact_cli, "load_configuration", prohibited)
    monkeypatch.setattr(google_ads_pause_artifact_cli, "_load_source", prohibited)
    monkeypatch.setattr(credentials, "export_key", prohibited)
    result = _invoke(tmp_path, "0" * 64, "--input", str(request))

    assert result.exit_code != 0, result.output
    diagnostic = _data(result)["diagnostics"][0]
    assert diagnostic["code"] == expected
    assert "private-artifact-input.json" not in result.output


def test_missing_retained_plan_is_refused_before_a_key_or_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "google-ads-pause-artifacts"
    destination.mkdir(mode=0o700)
    request = _artifact_request(tmp_path, destination, formats=["json"])

    def forbidden() -> bytes:
        raise AssertionError("an unretained selection must precede sharing-key access")

    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _invoke(tmp_path, "0" * 64, "--input", str(request))

    assert result.exit_code == 3, result.output
    assert _data(result)["diagnostics"][0]["code"] == "GOOGLE_ADS_PAUSE_PLAN_NOT_RETAINED"
    assert list(destination.iterdir()) == []


def test_complete_document_preflight_precedes_sharing_key_and_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "google-ads-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(tmp_path)
    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=1_024)}),
        replace=True,
    )
    request = _artifact_request(tmp_path, destination, formats=["json"])

    def forbidden() -> bytes:
        raise AssertionError("complete-document preflight must precede sharing-key access")

    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code != 0, result.output
    assert _data(result)["diagnostics"][0]["code"] == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert list(destination.iterdir()) == []


def test_source_snapshot_drift_blocks_render_before_key_or_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "google-ads-pause-artifacts"
    destination.mkdir(mode=0o700)
    _widen_output(tmp_path)
    request = _artifact_request(tmp_path, destination, formats=["json"])
    original_load = google_ads_pause_artifact_cli._load_source
    calls = 0

    def changed_second_load(
        directory: Path, selected: str
    ) -> tuple[object, dict[str, Any], object]:
        nonlocal calls
        action, provenance, source = original_load(directory, selected)
        calls += 1
        if calls == 2:
            return action, {**provenance, "checkpoint_digest": "f" * 64}, source
        return action, provenance, source

    def forbidden() -> bytes:
        raise AssertionError("source drift must precede sharing-key access")

    monkeypatch.setattr(google_ads_pause_artifact_cli, "_load_source", changed_second_load)
    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code != 0, result.output
    assert (
        _data(result)["diagnostics"][0]["code"] == "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
    )
    assert calls == 2
    assert list(destination.iterdir()) == []


def test_selection_must_be_an_exact_record_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "google-ads-pause-artifacts"
    destination.mkdir(mode=0o700)
    request = _artifact_request(tmp_path, destination, formats=["json"])

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an invalid selection reached retained history or a sharing key")

    monkeypatch.setattr(google_ads_pause_artifact_cli, "_load_source", prohibited)
    monkeypatch.setattr(credentials, "export_key", prohibited)
    result = _invoke(tmp_path, "not-a-digest", "--input", str(request))

    assert result.exit_code == 2, result.output
    assert _data(result)["diagnostics"][0]["code"] == "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION"
    assert list(destination.iterdir()) == []


def test_discovery_and_closed_artifact_schemas_are_installed() -> None:
    runner = CliRunner()
    discovery = runner.invoke(root_cli.app, ["discover"])

    assert discovery.exit_code == 0, discovery.output
    assert "artifacts google-ads-campaign-pause" in _data(discovery)["data"]["commands"]
    for schema_name in (
        "google-ads-campaign-pause-plan-artifact-render",
        "google-ads-campaign-pause-plan-artifact-document",
    ):
        result = runner.invoke(root_cli.app, ["schema", schema_name, "--root-only"])
        assert result.exit_code == 0, result.output
        document = _data(result)["data"]["schema"]
        assert document["additionalProperties"] is False
    request_schema = _data(
        runner.invoke(
            root_cli.app,
            ["schema", "google-ads-campaign-pause-plan-artifact-render", "--root-only"],
        )
    )["data"]["schema"]
    assert set(request_schema["properties"]) == {
        "schema_version",
        "formats",
        "output_directory",
        "replacement_authorized",
    }
    assert "formats" in request_schema["required"]
    assert request_schema["properties"]["formats"]["minItems"] == 1

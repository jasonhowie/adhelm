"""Installed explicit artifacts for retained GA4 key-event CREATE plans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_ga4_admin_execution_observation import _ACCESS, _KEY, _REFRESH, _SUBJECT
from test_ga4_create_workflow import _setup
from typer.testing import CliRunner

from adhelm import credentials
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
from adhelm.models import OutputPreferences


def _request(directory: Path, output: Path, *, formats: list[str] | None = None) -> Path:
    path = directory / "ga4-plan-artifact-request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "formats": formats or ["json", "csv", "html"],
                "output_directory": str(output),
            }
        )
    )
    return path


def _render(directory: Path, record_digest: str, request: Path):
    return CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "artifacts",
            "ga4-key-event-plan",
            record_digest,
            "--input",
            str(request),
        ],
    )


def test_explicit_artifact_command_renders_verified_static_history_without_new_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    output = tmp_path / "artifact-output"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=131_072)}),
        replace=True,
    )
    artifact_request = _request(tmp_path, output)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    prior_calls = list(calls)
    monkeypatch.setattr(credentials, "export_key", lambda: _KEY)

    result = _render(tmp_path, retained.plan_record_digest, artifact_request)

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]
    artifact_result = data["artifact_result"]
    assert envelope["outcome"] == "GA4_KEY_EVENT_CREATE_PLAN_ARTIFACT_NO_EXECUTION_AUTHORITY"
    assert data["source_record_digest"] == retained.plan_record_digest
    assert data["checkpoint_digest"] == checkpoint
    assert data["execution_authority"] is data["remote_mutations"] is False
    assert data["opening_or_editing_authorizes_execution"] is False
    assert artifact_result["generation_failure_code"] is None
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 3
    files = sorted(output.iterdir())
    assert {item.suffix for item in files} == {".json", ".csv", ".html"}
    rendered = result.output + "\n" + "\n".join(item.read_text() for item in files)
    for private in (
        _ACCESS,
        _REFRESH,
        _SUBJECT,
        profile.profile_id,
        profile.principal_ref,
        profile.client_ref,
        "qualified_request",
        "properties/123",
        "eventName",
    ):
        assert private not in rendered
    assert calls == prior_calls
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (b'{"schema_version":1.0}', "SCHEMA_VALIDATION_FAILED"),
        (b'{"schema_version":true}', "SCHEMA_VALIDATION_FAILED"),
        pytest.param(b"x" * 1_048_577, "INPUT_TOO_LARGE", id="oversized"),
    ],
)
def test_explicit_request_is_closed_before_history_or_sharing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
    expected: str,
) -> None:
    request = tmp_path / "artifact-private-input.json"
    request.write_bytes(contents)

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid artifact request reached local history or a sharing key")

    monkeypatch.setattr("adhelm.artifact_cli.load_configuration", prohibited)
    monkeypatch.setattr(credentials, "export_key", prohibited)
    result = _render(tmp_path, "0" * 64, request)

    assert result.exit_code != 0, result.output
    diagnostic = json.loads(result.output)["diagnostics"][0]
    assert diagnostic["code"] == expected
    assert "artifact-private-input.json" not in result.output


def test_full_document_preflight_fails_before_sharing_key_or_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ledger, _store, _profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    output = tmp_path / "artifact-output"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
        replace=True,
    )
    artifact_request = _request(tmp_path, output, formats=["json"])
    prior_calls = list(calls)

    def forbidden() -> bytes:
        raise AssertionError("full-document preflight must precede sharing-key access")

    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _render(tmp_path, retained.plan_record_digest, artifact_request)

    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert calls == prior_calls
    assert list(output.iterdir()) == []


def test_installed_artifact_command_and_closed_schemas_are_discoverable() -> None:
    runner = CliRunner()
    discovery = runner.invoke(app, ["discover"])
    assert discovery.exit_code == 0, discovery.output
    assert "artifacts ga4-key-event-plan" in json.loads(discovery.output)["data"]["commands"]
    for name in (
        "ga4-key-event-create-plan-artifact-render",
        "ga4-key-event-create-plan-artifact-document",
    ):
        result = runner.invoke(app, ["schema", name, "--root-only"])
        assert result.exit_code == 0, result.output
        schema = json.loads(result.output)["data"]["schema"]
        assert schema["additionalProperties"] is False
    request_result = runner.invoke(
        app,
        ["schema", "ga4-key-event-create-plan-artifact-render", "--root-only"],
    )
    request_schema = json.loads(request_result.output)["data"]["schema"]
    assert set(request_schema["properties"]) == {
        "schema_version",
        "formats",
        "output_directory",
        "replacement_authorized",
    }

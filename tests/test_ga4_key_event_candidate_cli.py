"""GA4 candidate CLI review/retention through the canonical ledger and projection."""

import json
from pathlib import Path

import pytest
from test_saved_ga4_key_event_candidates import _definition, _request
from typer.testing import CliRunner

from adhelm import ga4_key_event_candidate_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import export_verified
from adhelm.saved_conversion_contracts import retain_conversion_contract


def _input(root: Path) -> tuple[Path, str]:
    source = retain_conversion_contract(Ledger(root), _definition())
    request = root / "candidate.json"
    request.write_text(_request(source["record_digest"]).model_dump_json())
    return request, source["record_digest"]


def _invoke(root: Path, *args: str):
    return CliRunner().invoke(
        app, ["--data-dir", str(root), "measurement", "key-event-candidate", *args]
    )


def test_preview_is_full_redacted_non_executable_and_does_not_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, source_digest = _input(tmp_path)
    before = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    monkeypatch.setattr(ga4_key_event_candidate_cli, "export_key", lambda: b"k" * 32)
    result = _invoke(tmp_path, "compile", "--input", str(request))
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    candidate = envelope["data"]["candidate"]["compilation"]
    assert candidate["execution_authority"] is candidate["remote_side_effects"] is False
    assert candidate["requires_new_compilation_for_planning"] is True
    assert candidate["requires_exact_authorization"] is True
    binding = candidate["conversion_binding"]
    assert binding["conversion_contract_record_digest"] == source_digest
    assert binding["bound_boolean_field_count"] == candidate["declared_field_count"] == 1
    assert binding["effective_time_status"] == "DECLARED_NOT_EVALUATED"
    assert all(
        value is True for name, value in candidate["planning_boundary"].items() if name != "status"
    )
    for private in (
        "qualified_request",
        '"qualified"',
        "properties/123",
        "property.123",
        "web_stream.456",
        "ga4.fixture",
        "consent.fixture",
        "Count each declared occurrence",
        '"request"',
        '"target"',
    ):
        assert private not in result.output
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == before


def test_minimum_receipt_and_catalog_are_key_free_then_show_and_exports_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _input(tmp_path)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))

    def forbidden():
        raise AssertionError("Retention receipts and digest catalogs need no private key")

    monkeypatch.setattr(ga4_key_event_candidate_cli, "export_key", forbidden)
    retained = _invoke(tmp_path, "compile", "--input", str(request), "--retain")
    assert retained.exit_code == 0, retained.output
    assert len(retained.output.encode()) <= 1024
    record_digest = json.loads(retained.output)["data"]["record_digest"]
    save_configuration(tmp_path, Configuration(), replace=True)
    catalog = _invoke(tmp_path, "catalog")
    assert catalog.exit_code == 0, catalog.output
    assert record_digest in catalog.output
    monkeypatch.setattr(ga4_key_event_candidate_cli, "export_key", lambda: b"k" * 32)
    shown = _invoke(tmp_path, "show", record_digest)
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["data"]["current_definition_status"] == "NOT_EVALUATED"
    export_verified(
        Ledger(tmp_path), tmp_path / "exports", opaque_key=b"k" * 32, include_marketing_text=True
    )
    records = [
        json.loads(line) for line in (tmp_path / "exports/records.jsonl").read_text().splitlines()
    ]
    candidate = next(record for record in records if record["record_digest"] == record_digest)
    for output in (shown.output, json.dumps(candidate)):
        for private in (
            "qualified_request",
            '"qualified"',
            "properties/123",
            "ga4_admin.property.123",
        ):
            assert private not in output


def test_preview_limit_fails_before_key_without_omitting_any_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _input(tmp_path)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]

    def forbidden():
        raise AssertionError("Complete-review preflight must precede key access")

    monkeypatch.setattr(ga4_key_event_candidate_cli, "export_key", forbidden)
    result = _invoke(tmp_path, "compile", "--input", str(request))
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint


def test_unsupported_field_binding_fails_before_key_or_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _input(tmp_path)
    data = json.loads(request.read_text())
    data["key_event"]["event_contract"]["fields"][0]["value_type"] = "INTEGER"
    request.write_text(json.dumps(data))
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]

    def forbidden():
        raise AssertionError("Unsupported binding must not access a sharing key")

    monkeypatch.setattr(ga4_key_event_candidate_cli, "export_key", forbidden)
    result = _invoke(tmp_path, "compile", "--input", str(request), "--retain")
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["outcome"] == "FAILED_CLOSED"
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint


def test_candidate_commands_and_schemas_are_discoverable() -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert {
        "measurement key-event-candidate compile",
        "measurement key-event-candidate catalog",
        "measurement key-event-candidate show",
    } <= set(discovery["commands"])
    for name in (
        "measurement-ga4-key-event-candidate-input",
        "measurement-ga4-key-event-candidate-record",
    ):
        result = runner.invoke(app, ["schema", name, "--root-only"])
        assert result.exit_code == 0, result.output
        schema = json.loads(result.output)["data"]["schema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == 1

"""Floats cannot become typed integers before validation or durable history."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_conversion_contracts import _input as conversion_input
from test_ledger import change
from test_measurement import inventory
from typer.testing import CliRunner

from adhelm import workflows
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, Profile
from adhelm.providers.openai_ads import OpenAIAdsReportQuery
from adhelm.strict_json import validate_json_input, validate_json_value


@pytest.mark.parametrize("value", [2500.0, True, False, -0.0])
def test_both_text_and_mapping_boundaries_reject_non_integer_money(value: object) -> None:
    data = {"minor_units": value, "currency": "USD"}
    with pytest.raises(ValidationError):
        validate_json_input(Money, json.dumps(data))
    with pytest.raises(ValidationError):
        validate_json_value(Money, data)


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"minor_units":1,"minor_units":2,"currency":"USD"}', "INVALID_JSON"),
        ('{"minor_units":NaN,"currency":"USD"}', "INVALID_JSON"),
        pytest.param(" " * 1_048_577, "INPUT_TOO_LARGE", id="oversized"),
    ],
)
def test_screening_preserves_duplicate_nonfinite_and_size_rejection(raw: str, code: str) -> None:
    with pytest.raises(AdhelmError) as error:
        validate_json_input(Money, raw)
    assert error.value.code == code


def test_json_mapping_keeps_legacy_normalized_model_and_canonical_digest() -> None:
    original = conversion_input()
    data = original.model_dump(mode="json")
    validated = validate_json_value(type(original), data)
    assert validated == original
    assert validated.effective_from == original.effective_from
    assert isinstance(validated.exclusions, tuple)
    assert canonical_bytes(validated) == canonical_bytes(original)
    assert digest(validated) == digest(original)
    with pytest.raises(AdhelmError):
        validate_json_value(Money, {"minor_units": 2**53, "currency": "USD"})
    with pytest.raises(AdhelmError):
        validate_json_value(Money, {1: "invalid-nonstring-key"})


@pytest.mark.parametrize("case", ["artifact", "conversion", "measurement", "audit"])
def test_invalid_input_fails_before_local_state_or_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid typed JSON must not access a sharing key")

    monkeypatch.setattr("adhelm.credentials.export_key", forbidden)
    monkeypatch.setattr("adhelm.conversion_cli.export_key", forbidden)
    if case == "artifact":
        data = {"max_output_bytes": 2048.0}
        command = ["artifacts", "configure"]
    elif case == "conversion":
        data = conversion_input().model_dump(mode="json")
        data["version"] = 1.0
        command = ["conversion", "register"]
    elif case == "measurement":
        data = inventory().model_dump(mode="json")
        data["workspaces"][0]["pending_change_count"] = 0.0
        command = ["measurement", "check-stage"]
    else:
        data = {"maximum_observation_age_seconds": 3600.0, "require_conversion_evidence": True}
        command = ["audit", "a" * 64]
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path / "data"), *command, "--input", str(path)]
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("bad_value", [1.0, True])
def test_report_query_rejects_wrong_integer_type_before_key_history_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: object
) -> None:
    profile = Profile(
        profile_id="reader",
        provider="openai_ads",
        account_ref="account_fixture",
        principal_ref="fixture-principal",
        client_ref="fixture-client",
        scopes=["fixture.scope"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="fixture-credential",
    )
    root = tmp_path / "data"
    save_configuration(root, Configuration(profiles=[profile]))
    data = {
        "account_ref": "account_fixture",
        "report_id": "report.fixture",
        "query_contract_hash": "a" * 64,
        "window_start": "2026-09-01",
        "window_end": "2026-09-01",
        "source_timezone": "UTC",
        "currency": "USD",
        "maximum_rows": bad_value,
    }
    path = tmp_path / "query.json"
    path.write_text(json.dumps(data))

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid query must not access keys or provider transport")

    monkeypatch.setattr(workflows, "export_key", forbidden)
    monkeypatch.setattr(workflows, "credential", forbidden)
    monkeypatch.setattr(workflows.httpx, "Client", forbidden)
    with pytest.raises(ValidationError):
        workflows.run_report(root, "openai_ads", "reader", path)
    assert sorted(p.name for p in root.iterdir()) == ["config.json"]
    # The intended JSON date boundary remains valid without float normalization.
    fixed = {**data, "maximum_rows": 1}
    query = validate_json_input(OpenAIAdsReportQuery, json.dumps(fixed))
    assert query.window_start.isoformat() == "2026-09-01"


@pytest.mark.parametrize("bad_value", [2500.0, True, -0.0])
def test_public_ledger_append_rejects_float_and_bool_before_record_creation(
    tmp_path: Path, bad_value: object
) -> None:
    ledger = Ledger(tmp_path / "data")
    with pytest.raises(AdhelmError) as error:
        ledger.append(
            "changes", "CHANGE_EVENT", change(spend_impact_minor=bad_value, currency="USD")
        )
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"
    assert ledger.records("changes") == []
    valid = ledger.append(
        "changes", "CHANGE_EVENT", change(spend_impact_minor=2500, currency="USD")
    )
    assert valid["payload"]["spend_impact_minor"] == 2500
    assert ledger.verify()["status"] == "VERIFIED"

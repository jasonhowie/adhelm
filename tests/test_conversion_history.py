"""Durable conversion-definition versions and bounded CLI review, without provider access."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import conversion_cli
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.conversion_contracts import ConversionContractInput, compile_conversion_contract
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import export_verified
from adhelm.saved_conversion_contracts import (
    conversion_catalog,
    load_conversion_contract,
    retain_conversion_contract,
)

START = datetime(2026, 9, 4, tzinfo=UTC)


def _definition(
    *, version: int = 1, supersedes: str | None = None, effective: datetime = START
) -> ConversionContractInput:
    value = {
        "contract_id": "conversion.fixture-private",
        "version": version,
        "supersedes_ref": supersedes,
        "business_outcome": {
            "outcome_ref": "outcome.fixture",
            "statement": {
                "statement": "A qualified consultation request confirmed by the customer"
            },
        },
        "authoritative_source": {
            "source_kind": "CRM_AGGREGATE",
            "source_ref": "source.fixture",
            "statement": {"statement": "Customer-configured fixed aggregate report"},
        },
        "qualifying_occurrence": {
            "occurrence_ref": "occurrence.fixture",
            "statement": {"statement": "Source confirms the declared qualification rule"},
        },
        "exclusions": [
            {
                "exclusion_ref": "exclusion.fixture",
                "statement": {"statement": "Exclude synthetic checks"},
            }
        ],
        "counting_and_deduplication": {
            "counting_semantics": "ONCE_PER_QUALIFYING_OCCURRENCE",
            "deduplication_semantics": "SOURCE_MANAGED",
            "duplicate_occurrence_handling": "EXCLUDE_DUPLICATES",
            "declared_window": "NOT_APPLICABLE",
        },
        "value_and_currency": {
            "value_semantics": "SOURCE_REPORTED_VALUE",
            "fixed_value": None,
            "currency": "USD",
            "null_value_semantics": "PRESERVE_AS_UNAVAILABLE",
        },
        "allowed_parameter_schema": [
            {
                "name": name,
                "value_type": "LOW_CARDINALITY_ENUM",
                "required": True,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": ["qualified", "not_qualified"],
            }
            for name in ("qualification", "category", "segment")
        ],
        "prohibited_parameters": [
            "PERSONAL_IDENTIFIER",
            "AUTHENTICATION_SECRET",
            "CLICK_IDENTIFIER",
            "URL_OR_QUERY",
            "DOM_OR_PAGE_TEXT",
            "FORM_VALUE",
            "CRM_RECORD_IDENTIFIER",
        ],
        "consent_contract_ref": "consent.fixture-private",
        "destination_profiles": ["ga4.fixture-private", "meta.fixture-private"],
        "effective_from": effective.isoformat(),
    }
    return ConversionContractInput.model_validate_json(canonical_bytes(value))


def test_versions_retain_exact_definitions_and_never_rewrite_prior_history(tmp_path: Path) -> None:
    one = retain_conversion_contract(Ledger(tmp_path), _definition())
    path = next((tmp_path / "ledger/changes").glob("*.jsonl"))
    prefix = path.read_bytes()
    first, _ = load_conversion_contract(Ledger(tmp_path), one["record_digest"])
    two = retain_conversion_contract(
        Ledger(tmp_path),
        _definition(
            version=2, supersedes=first.content_digest, effective=START + timedelta(days=1)
        ),
    )
    assert path.read_bytes().startswith(prefix)
    second, provenance = load_conversion_contract(Ledger(tmp_path), two["record_digest"])
    assert second.content_digest != first.content_digest
    assert second.definition.supersedes_ref == first.content_digest
    assert provenance["record_digest"] == two["record_digest"]
    assert not second.execution_authority
    assert one["provider"] is None and one["account_ref"] is None and one["run_id"] is None
    assert one["source"] == "LOCAL_CLI"
    assert conversion_catalog(Ledger(tmp_path), limit=1)["next_cursor"] == "1"
    assert not Ledger(tmp_path).unresolved_runs()


@pytest.mark.parametrize("kind", ["duplicate", "missing_parent", "skipped_version", "earlier_time"])
def test_version_chain_rejects_forks_before_another_record_is_written(
    tmp_path: Path, kind: str
) -> None:
    initial = _definition()
    first = compile_conversion_contract(initial)
    retain_conversion_contract(Ledger(tmp_path), initial)
    before = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    bad = (
        initial
        if kind == "duplicate"
        else _definition(
            version=3 if kind == "skipped_version" else 2,
            supersedes="0" * 64 if kind == "missing_parent" else first.content_digest,
            effective=START if kind == "earlier_time" else START + timedelta(days=1),
        )
    )
    with pytest.raises(AdhelmError) as error:
        retain_conversion_contract(Ledger(tmp_path), bad)
    assert error.value.code == "CONVERSION_VERSION_CONFLICT"
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == before


def test_concurrent_registration_cannot_fork_one_contract_version(tmp_path: Path) -> None:
    first = compile_conversion_contract(_definition())
    retain_conversion_contract(Ledger(tmp_path), first.definition)
    next_version = _definition(
        version=2, supersedes=first.content_digest, effective=START + timedelta(days=1)
    )

    def register() -> str:
        try:
            retain_conversion_contract(Ledger(tmp_path), next_version)
            return "retained"
        except AdhelmError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: register(), range(2)))
    assert sorted(outcomes) == ["CONVERSION_VERSION_CONFLICT", "retained"]
    assert conversion_catalog(Ledger(tmp_path))["total_count"] == 2


def test_fully_rehashed_parent_tamper_still_fails_verified_history(tmp_path: Path) -> None:
    first = compile_conversion_contract(_definition())
    retain_conversion_contract(Ledger(tmp_path), first.definition)
    retain_conversion_contract(
        Ledger(tmp_path),
        _definition(
            version=2, supersedes=first.content_digest, effective=START + timedelta(days=1)
        ),
    )
    path = next((tmp_path / "ledger/changes").glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    records[-1]["payload"]["definition"]["supersedes_ref"] = "0" * 64
    records[-1]["payload"]["content_digest"] = digest(records[-1]["payload"]["definition"])
    records[-1]["record_digest"] = digest(
        {key: value for key, value in records[-1].items() if key != "record_digest"}
    )
    path.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))
    with pytest.raises(AdhelmError) as error:
        conversion_catalog(Ledger(tmp_path))
    assert error.value.code == "CONVERSION_VERSION_CONFLICT"


@pytest.mark.parametrize(
    "field,value",
    [("provider", "ga4"), ("account_ref", "123"), ("run_id", "run.x"), ("source", "PROVIDER_UI")],
)
def test_declared_contract_cannot_claim_provider_execution_provenance(
    tmp_path: Path, field: str, value: str
) -> None:
    contract = compile_conversion_contract(_definition())
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path).append(
            "changes", "CONVERSION_CONTRACT", contract.model_dump(mode="json"), **{field: value}
        )
    assert error.value.code == "INVALID_CONVERSION_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def test_register_receipt_is_key_free_and_review_export_redact_profile_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    source = tmp_path / "definition.json"
    source.write_bytes(canonical_bytes(_definition()))
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=1024)))

    def forbidden_key():
        raise AssertionError("registration must not need credentials or a sharing key")

    monkeypatch.setattr(conversion_cli, "export_key", forbidden_key)
    runner = CliRunner()
    base = ["--data-dir", str(root), "conversion"]
    registered = runner.invoke(app, [*base, "register", "--input", str(source)])
    assert registered.exit_code == 0, registered.output
    assert len(registered.output.encode()) <= 1024
    receipt = json.loads(registered.output)["data"]
    assert receipt["execution_authority"] is False
    assert "fixture-private" not in registered.output
    monkeypatch.setattr(conversion_cli, "export_key", lambda: b"k" * 32)
    save_configuration(
        root, Configuration(output=OutputPreferences(max_output_bytes=4096)), replace=True
    )
    rows, cursor = [], "0"
    while True:
        shown = runner.invoke(app, [*base, "show", receipt["record_digest"], "--cursor", cursor])
        assert shown.exit_code == 0, shown.output
        assert len(shown.output.encode()) <= 4096 and "fixture-private" not in shown.output
        envelope = json.loads(shown.output)
        assert envelope["data"]["execution_authority"] is False
        rows.extend(envelope["data"]["rows"])
        if envelope["next_cursor"] is None:
            break
        assert int(envelope["next_cursor"]) == len(rows)
        cursor = envelope["next_cursor"]
    definition = {row["field"]: row["value"] for row in rows}
    assert len(definition) == len(ConversionContractInput.model_fields)
    assert all(value.startswith("opaque:") for value in definition["destination_profiles"])
    catalog = runner.invoke(app, [*base, "catalog"])
    assert catalog.exit_code == 0 and "fixture-private" not in catalog.output
    export_verified(Ledger(root), tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export/records.jsonl").read_text()
    assert "fixture-private" not in exported and receipt["content_digest"] in exported


def test_cli_surfaces_uncertain_local_write_and_invalid_definition_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(canonical_bytes(_definition()))

    def uncertain(*args, **kwargs):
        raise OSError("private path must not be returned")

    monkeypatch.setattr(conversion_cli, "retain_conversion_contract", uncertain)
    args = ["--data-dir", str(tmp_path / "data"), "conversion", "register", "--input", str(source)]
    failed = CliRunner().invoke(app, args)
    assert failed.exit_code != 0
    assert json.loads(failed.output)["diagnostics"][0]["code"] == "CONVERSION_RETENTION_UNVERIFIED"
    assert "private path" not in failed.output
    invalid = _definition().model_dump(mode="json")
    invalid["email"] = "synthetic@example.test"
    source.write_text(json.dumps(invalid))
    invalid_result = CliRunner().invoke(app, args)
    assert invalid_result.exit_code != 0 and "synthetic@example" not in invalid_result.output
    assert not (tmp_path / "data/ledger").exists()


def test_installed_conversion_schema_and_empty_catalog_need_no_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversion_cli, "export_key", lambda: pytest.fail("key not needed"))
    runner = CliRunner()
    for schema in ("conversion-definition", "conversion-contract"):
        result = runner.invoke(app, ["schema", schema])
        assert result.exit_code == 0, result.output
    empty = runner.invoke(app, ["--data-dir", str(tmp_path), "conversion", "catalog"])
    assert empty.exit_code == 0 and json.loads(empty.output)["total_count"] == 0


def test_missing_conversion_meaning_has_actionable_diagnostic_before_retention(
    tmp_path: Path,
) -> None:
    definition = _definition().model_dump(mode="json")
    del definition["business_outcome"]
    source = tmp_path / "input.json"
    source.write_bytes(canonical_bytes(definition))
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "conversion", "register", "--input", str(source)],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "USER_DEFINITION_REQUIRED"
    assert not (tmp_path / "data/ledger").exists()

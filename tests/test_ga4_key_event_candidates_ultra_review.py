"""Independent adversarial review of retained GA4 key-event candidates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm import ga4_key_event_candidate_cli
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.conversion_contracts import (
    ConversionContract,
    ConversionContractInput,
    compile_conversion_contract,
)
from adhelm.errors import AdhelmError
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    compile_ga4_key_event_candidate,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import export_verified
from adhelm.saved_conversion_contracts import load_conversion_contract, retain_conversion_contract
from adhelm.saved_ga4_key_event_candidates import (
    load_ga4_key_event_candidate,
    retain_ga4_key_event_candidate,
)
from adhelm.strict_json import validate_json_input

_START = datetime(2034, 2, 3, tzinfo=UTC)


def _definition_data(
    *,
    version: int = 1,
    supersedes: str | None = None,
    effective_from: datetime = _START,
    parameters: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "contract_id": "conversion.ga4-ultra",
        "version": version,
        "supersedes_ref": supersedes,
        "business_outcome": {
            "outcome_ref": "outcome.ga4-ultra",
            "statement": {"statement": "A customer-declared qualified interaction."},
        },
        "authoritative_source": {
            "source_kind": "CRM_AGGREGATE",
            "source_ref": "source.ga4-ultra",
            "statement": {"statement": "A customer-declared aggregate source."},
        },
        "qualifying_occurrence": {
            "occurrence_ref": "occurrence.ga4-ultra",
            "statement": {"statement": "A declared qualifying occurrence."},
        },
        "exclusions": [],
        "counting_and_deduplication": {
            "counting_semantics": "EACH_QUALIFYING_OCCURRENCE",
            "deduplication_semantics": "NOT_APPLICABLE",
            "duplicate_occurrence_handling": "COUNT_EACH",
            "declared_window": "NOT_APPLICABLE",
        },
        "value_and_currency": {
            "value_semantics": "NO_MONETARY_VALUE",
            "fixed_value": None,
            "currency": None,
            "null_value_semantics": "NO_MONETARY_VALUE",
        },
        "allowed_parameter_schema": parameters
        if parameters is not None
        else [
            {
                "name": "qualified",
                "value_type": "BOOLEAN",
                "required": True,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": [],
            },
            {
                "name": "follow_up",
                "value_type": "BOOLEAN",
                "required": False,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": [],
            },
            {
                "name": "quantity",
                "value_type": "INTEGER",
                "required": False,
                "integer_minimum": 1,
                "integer_maximum": 3,
                "allowed_values": [],
            },
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
        "consent_contract_ref": "consent.ga4-ultra",
        "destination_profiles": ["ga4.ga4-ultra"],
        "effective_from": effective_from.isoformat(),
    }


def _definition(**kwargs: object) -> ConversionContractInput:
    return validate_json_input(
        ConversionContractInput,
        json.dumps(_definition_data(**kwargs), separators=(",", ":")),
    )


def _request_data(
    conversion_contract_record_digest: str,
    *,
    fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "conversion_contract_record_digest": conversion_contract_record_digest,
        "declared_destination_profile_ref": "ga4.ga4-ultra",
        "key_event": {
            "event_contract": {
                "event_contract_ref": "event.ga4-ultra",
                "event_name": "qualified_request",
                "semantics": {
                    "semantics_ref": "semantics.ga4-ultra",
                    "customer_definition": "A declared qualified interaction event.",
                    "counting_rule": "Count each declared occurrence.",
                },
                "fields": fields
                if fields is not None
                else [{"name": "qualified", "value_type": "BOOLEAN", "required": True}],
            },
            "identity": {
                "property_ref": "ga4_admin.property.123",
                "property_client_ref": "123",
                "web_stream_ref": "ga4_admin.web_stream.456",
                "web_stream_client_ref": "456",
            },
            "counting_method": "ONCE_PER_EVENT",
            "default_value": None,
        },
    }


def _request(conversion_contract_record_digest: str, **kwargs: object) -> Ga4KeyEventCandidateInput:
    return validate_json_input(
        Ga4KeyEventCandidateInput,
        json.dumps(
            _request_data(conversion_contract_record_digest, **kwargs), separators=(",", ":")
        ),
    )


def _retained_contract(ledger: Ledger) -> tuple[dict[str, Any], ConversionContract]:
    envelope = retain_conversion_contract(ledger, _definition())
    contract, _ = load_conversion_contract(ledger, envelope["record_digest"])
    return envelope, contract


def _compiled_record(source: dict[str, Any], contract: ConversionContract) -> dict[str, Any]:
    return compile_ga4_key_event_candidate(
        _request(source["record_digest"]),
        retained_contract=contract,
        retained_contract_record_digest=source["record_digest"],
    ).model_dump(mode="json")


def _rehash_candidate(record: dict[str, Any]) -> None:
    compilation = record["compilation"]
    compiler = compilation["compiler_candidate"]
    compilation["compiler_candidate_digest"] = digest(compiler)
    record["candidate_digest"] = digest(compilation)


def _set_path(value: dict[str, Any], path: tuple[str, ...], replacement: object) -> None:
    target: dict[str, Any] = value
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = replacement


def _rewrite_last_changes_record(root: Path, mutate: Any) -> None:
    path = next((root / "ledger" / "changes").glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(records[-1])
    records[-1]["record_digest"] = digest(
        {name: item for name, item in records[-1].items() if name != "record_digest"}
    )
    # Do not use canonical_bytes here: RFC 8785 would turn a test's physical
    # JSON ``1.0`` into ``1`` before the historical raw-type guard sees it.
    path.write_bytes(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records).encode()
    )


@pytest.mark.parametrize(
    ("identity_field", "replacement", "account_ref"),
    [
        ("property_ref", "ga4_admin.property.relabelled", "ga4_admin.property.relabelled"),
        ("web_stream_ref", "ga4_admin.web_stream.relabelled", "ga4_admin.property.123"),
    ],
)
def test_rehashed_candidate_cannot_relabel_native_identity_at_ledger_boundary(
    tmp_path: Path, identity_field: str, replacement: str, account_ref: str
) -> None:
    ledger = Ledger(tmp_path)
    source, contract = _retained_contract(ledger)
    forged = _compiled_record(source, contract)
    forged["compilation"]["compiler_candidate"]["identity"][identity_field] = replacement
    _rehash_candidate(forged)
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as error:
        ledger.append(
            "changes",
            "GA4_KEY_EVENT_CANDIDATE_COMPILED",
            forged,
            provider="ga4_admin",
            account_ref=account_ref,
            source="LOCAL_CLI",
            run_id=None,
            occurred_at=None,
            observed_at=None,
        )

    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"
    assert ledger.checkpoint()["checkpoint_digest"] == before

    history_root = tmp_path / "history"
    history = Ledger(history_root)
    historical_source, historical_contract = _retained_contract(history)
    retain_ga4_key_event_candidate(
        history,
        _request(historical_source["record_digest"]),
        retained_contract=historical_contract,
        retained_contract_record_digest=historical_source["record_digest"],
    )

    def mutate(envelope: dict[str, Any]) -> None:
        candidate = envelope["payload"]
        candidate["compilation"]["compiler_candidate"]["identity"][identity_field] = replacement
        _rehash_candidate(candidate)
        envelope["account_ref"] = account_ref

    _rewrite_last_changes_record(history_root, mutate)
    with pytest.raises(AdhelmError) as error:
        Ledger(history_root).checkpoint()
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("definition", "schema_version"), True),
        (("definition", "schema_version"), 1.0),
        (("execution_authority",), 0),
        (("execution_authority",), 0.0),
    ],
)
def test_conversion_source_aliases_fail_direct_append_and_fully_rehashed_history(
    tmp_path: Path, path: tuple[str, ...], replacement: object
) -> None:
    payload = compile_conversion_contract(_definition()).model_dump(mode="json")
    _set_path(payload, path, replacement)
    if path[0] == "definition":
        payload["content_digest"] = digest(payload["definition"])

    with pytest.raises(AdhelmError):
        Ledger(tmp_path / "direct").append("changes", "CONVERSION_CONTRACT", payload)

    root = tmp_path / "history"
    retain_conversion_contract(Ledger(root), _definition())

    def mutate(envelope: dict[str, Any]) -> None:
        stored = envelope["payload"]
        _set_path(stored, path, replacement)
        if path[0] == "definition":
            stored["content_digest"] = digest(stored["definition"])

    _rewrite_last_changes_record(root, mutate)
    with pytest.raises(AdhelmError):
        Ledger(root).checkpoint()


def test_raw_nested_aliases_and_constructed_request_fail_before_retention(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    source, contract = _retained_contract(ledger)
    before = ledger.checkpoint()["checkpoint_digest"]
    raw = _request_data(source["record_digest"])
    raw["schema_version"] = True
    raw["key_event"]["event_contract"]["fields"][0]["required"] = 1.0

    with pytest.raises(AdhelmError) as error:
        compile_ga4_key_event_candidate(
            raw,  # type: ignore[arg-type]
            retained_contract=contract,
            retained_contract_record_digest=source["record_digest"],
        )
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"

    constructed = Ga4KeyEventCandidateInput.model_construct(**raw)
    with pytest.raises(AdhelmError) as error:
        compile_ga4_key_event_candidate(
            constructed,
            retained_contract=contract,
            retained_contract_record_digest=source["record_digest"],
        )
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"
    assert ledger.checkpoint()["checkpoint_digest"] == before


def test_closed_boolean_mapping_preserves_uncertainty_and_never_activates_future_definition(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    source = retain_conversion_contract(
        ledger,
        _definition(effective_from=_START + timedelta(days=365)),
    )
    contract, _ = load_conversion_contract(ledger, source["record_digest"])
    record = compile_ga4_key_event_candidate(
        _request(source["record_digest"]),
        retained_contract=contract,
        retained_contract_record_digest=source["record_digest"],
    )
    binding = record.compilation.conversion_binding
    candidate = record.compilation.compiler_candidate

    assert [field.model_dump(mode="json") for field in binding.bound_boolean_fields] == [
        {"name": "qualified", "event_value_type": "BOOLEAN", "event_required": True}
    ]
    assert binding.counting_binding == "EACH_QUALIFYING_OCCURRENCE_TO_ONCE_PER_EVENT"
    assert binding.value_binding == "NO_MONETARY_VALUE_TO_NO_DEFAULT_VALUE"
    assert binding.effective_time_status == "DECLARED_NOT_EVALUATED"
    assert record.compilation.execution_status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert record.compilation.remote_side_effects is False
    assert record.compilation.execution_authority is False
    assert candidate.request.countingMethod == "ONCE_PER_EVENT"
    assert candidate.request.defaultValue is None
    assert all(
        value is True for name, value in candidate.planning_boundary if name.startswith("requires_")
    )
    assert candidate.prospective_effects.historical_backfill is False
    assert candidate.prospective_effects.native_conversion_mapping == "NONE_CREATED"

    before = ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as error:
        compile_ga4_key_event_candidate(
            _request(
                source["record_digest"],
                fields=[{"name": "quantity", "value_type": "INTEGER", "required": False}],
            ),
            retained_contract=contract,
            retained_contract_record_digest=source["record_digest"],
        )
    assert error.value.code in {
        "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT",
        "INVALID_GA4_KEY_EVENT_CANDIDATE",
    }
    assert ledger.checkpoint()["checkpoint_digest"] == before


def test_stale_contract_rejection_is_atomic_while_old_candidate_stays_historical(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    first, first_contract = _retained_contract(ledger)
    retained = retain_ga4_key_event_candidate(
        ledger,
        _request(first["record_digest"]),
        retained_contract=first_contract,
        retained_contract_record_digest=first["record_digest"],
    )
    second = _definition(
        version=2,
        supersedes=first_contract.content_digest,
        effective_from=_START + timedelta(days=1),
    )
    retain_conversion_contract(ledger, second)
    before = ledger.checkpoint()["checkpoint_digest"]

    with pytest.raises(AdhelmError) as error:
        retain_ga4_key_event_candidate(
            ledger,
            _request(first["record_digest"]),
            retained_contract=first_contract,
            retained_contract_record_digest=first["record_digest"],
        )
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT"
    assert ledger.checkpoint()["checkpoint_digest"] == before
    historical, metadata = load_ga4_key_event_candidate(ledger, retained["record_digest"])
    assert historical.compilation.conversion_binding.contract_version == 1
    assert metadata["effective_time_status"] == "DECLARED_NOT_EVALUATED"


def test_fully_rehashed_candidate_binding_mismatch_fails_verified_history(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    source, contract = _retained_contract(ledger)
    retain_ga4_key_event_candidate(
        ledger,
        _request(source["record_digest"]),
        retained_contract=contract,
        retained_contract_record_digest=source["record_digest"],
    )

    def mutate(envelope: dict[str, Any]) -> None:
        candidate = envelope["payload"]
        candidate["compilation"]["conversion_binding"]["bound_boolean_fields"][0][
            "event_required"
        ] = False
        _rehash_candidate(candidate)

    _rewrite_last_changes_record(tmp_path, mutate)
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path).checkpoint()
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT"


def test_export_projects_only_closed_redacted_review_even_with_marketing_text(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "data")
    source, contract = _retained_contract(ledger)
    retained = retain_ga4_key_event_candidate(
        ledger,
        _request(source["record_digest"]),
        retained_contract=contract,
        retained_contract_record_digest=source["record_digest"],
    )
    export_dir = tmp_path / "export"
    export_verified(ledger, export_dir, opaque_key=b"k" * 32, include_marketing_text=True)
    rows = [json.loads(line) for line in (export_dir / "records.jsonl").read_text().splitlines()]
    exported = next(row for row in rows if row["record_digest"] == retained["record_digest"])
    payload = exported["payload"]
    compilation = payload["compilation"]
    rendered = json.dumps(payload, sort_keys=True)

    assert not any(f'"{name}":' in rendered for name in ("identity", "target", "request"))
    assert "qualified_request" not in rendered and "event.ga4-ultra" not in rendered
    assert "properties/123" not in rendered and "ga4_admin.property.123" not in rendered
    assert "customer_definition" not in rendered and "declared_field_names" not in rendered
    assert payload["candidate_digest"]
    assert compilation["compiler_candidate_digest"]
    assert (
        compilation["conversion_binding"]["conversion_contract_record_digest"]
        == source["record_digest"]
    )
    assert compilation["conversion_binding"]["contract_ref"].startswith("opaque:")
    assert compilation["conversion_binding"]["declared_destination_profile_ref"].startswith(
        "opaque:"
    )
    assert compilation["execution_authority"] is False
    assert compilation["planning_boundary"]["requires_exact_authorization"] is True
    assert compilation["prospective_effects"]["native_conversion_mapping"] == "NONE_CREATED"


def test_cli_retain_and_catalog_are_key_free_while_show_uses_closed_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    ledger = Ledger(root)
    source, _ = _retained_contract(ledger)
    input_path = tmp_path / "candidate.json"
    input_path.write_text(
        json.dumps(_request_data(source["record_digest"]), separators=(",", ":")), encoding="utf-8"
    )
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=4096)))
    monkeypatch.setattr(
        ga4_key_event_candidate_cli,
        "export_key",
        lambda: pytest.fail("retention and catalog must not read a sharing key"),
    )
    runner = CliRunner()
    base = ["--data-dir", str(root), "measurement", "key-event-candidate"]
    retained = runner.invoke(app, [*base, "compile", "--input", str(input_path), "--retain"])
    assert retained.exit_code == 0, retained.output
    receipt = json.loads(retained.output)["data"]
    assert receipt["persisted"] is True
    assert receipt["execution_authority"] is False
    assert receipt["remote_mutations"] is False
    catalog = runner.invoke(app, [*base, "catalog"])
    assert catalog.exit_code == 0, catalog.output
    catalog_data = json.loads(catalog.output)["data"]
    assert catalog_data["total_count"] == 1
    assert "qualified_request" not in catalog.output and "properties/123" not in catalog.output

    monkeypatch.setattr(ga4_key_event_candidate_cli, "export_key", lambda: b"k" * 32)
    shown = runner.invoke(app, [*base, "show", receipt["record_digest"]])
    assert shown.exit_code == 0, shown.output
    review = json.loads(shown.output)["data"]
    rendered = json.dumps(review, sort_keys=True)
    assert "qualified_request" not in rendered and "properties/123" not in rendered
    assert review["historical_status"] == "RETAINED_OFFLINE_OBSERVATION"
    assert review["current_definition_status"] == "NOT_EVALUATED"
    assert review["candidate"]["compilation"]["execution_authority"] is False

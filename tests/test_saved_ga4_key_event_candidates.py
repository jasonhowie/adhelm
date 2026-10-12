from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adhelm.canonical import canonical_bytes
from adhelm.conversion_contracts import ConversionContractInput
from adhelm.errors import AdhelmError
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput
from adhelm.ledger import Ledger
from adhelm.saved_conversion_contracts import load_conversion_contract, retain_conversion_contract
from adhelm.saved_ga4_key_event_candidates import (
    ga4_key_event_candidate_catalog,
    load_ga4_key_event_candidate,
    retain_ga4_key_event_candidate,
)

START = datetime(2026, 9, 1, tzinfo=UTC)


def _definition(
    *, version: int = 1, supersedes: str | None = None, effective_from: datetime = START
) -> ConversionContractInput:
    return ConversionContractInput.model_validate_json(
        canonical_bytes(
            {
                "schema_version": 1,
                "contract_id": "conversion.fixture",
                "version": version,
                "supersedes_ref": supersedes,
                "business_outcome": {
                    "outcome_ref": "outcome.fixture",
                    "statement": {"statement": "A qualified request confirmed by the customer"},
                },
                "authoritative_source": {
                    "source_kind": "CRM_AGGREGATE",
                    "source_ref": "source.fixture",
                    "statement": {"statement": "A fixed customer aggregate report"},
                },
                "qualifying_occurrence": {
                    "occurrence_ref": "occurrence.fixture",
                    "statement": {"statement": "The source confirms each qualified occurrence"},
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
                "allowed_parameter_schema": [
                    {
                        "name": "qualified",
                        "value_type": "BOOLEAN",
                        "required": True,
                        "integer_minimum": None,
                        "integer_maximum": None,
                        "allowed_values": [],
                    }
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
                "consent_contract_ref": "consent.fixture",
                "destination_profiles": ["ga4.fixture"],
                "effective_from": effective_from.isoformat(),
            }
        )
    )


def _request(record_digest: str) -> Ga4KeyEventCandidateInput:
    return Ga4KeyEventCandidateInput.model_validate_json(
        canonical_bytes(
            {
                "schema_version": 1,
                "conversion_contract_record_digest": record_digest,
                "declared_destination_profile_ref": "ga4.fixture",
                "key_event": {
                    "event_contract": {
                        "event_contract_ref": "event.fixture",
                        "event_name": "qualified_request",
                        "semantics": {
                            "semantics_ref": "semantics.fixture",
                            "customer_definition": "A customer-defined qualified request event.",
                            "counting_rule": "Count each declared occurrence.",
                        },
                        "fields": [
                            {"name": "qualified", "value_type": "BOOLEAN", "required": True}
                        ],
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
        )
    )


def _retained_contract(ledger: Ledger) -> tuple[dict[str, object], object]:
    envelope = retain_conversion_contract(ledger, _definition())
    contract, _ = load_conversion_contract(ledger, envelope["record_digest"])
    return envelope, contract


def test_retain_load_and_catalog_are_verified_and_redaction_safe(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    contract_envelope, contract = _retained_contract(ledger)
    envelope = retain_ga4_key_event_candidate(
        ledger,
        _request(contract_envelope["record_digest"]),
        retained_contract=contract,
        retained_contract_record_digest=contract_envelope["record_digest"],
    )

    assert envelope["record_kind"] == "GA4_KEY_EVENT_CANDIDATE_COMPILED"
    assert envelope["provider"] == "ga4_admin"
    assert envelope["source"] == "LOCAL_CLI"
    assert envelope["account_ref"] == "ga4_admin.property.123"
    assert envelope["run_id"] is None
    assert envelope["occurred_at"] is None
    assert envelope["observed_at"] is None

    record, metadata = load_ga4_key_event_candidate(ledger, envelope["record_digest"])
    assert record.compilation.conversion_binding.contract_version == 1
    assert metadata["record_digest"] == envelope["record_digest"]
    assert metadata["conversion_contract_record_digest"] == contract_envelope["record_digest"]
    json.dumps(metadata)

    catalog = ga4_key_event_candidate_catalog(ledger)
    assert catalog["total_count"] == 1
    row = catalog["candidates"][0]
    assert row["bound_boolean_field_count"] == 1
    assert row["execution_status"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert row["semantic_accuracy"] == "NOT_VERIFIED"
    assert row["effective_time_status"] == "DECLARED_NOT_EVALUATED"
    serialized = json.dumps(catalog, sort_keys=True)
    for private_value in (
        "qualified_request",
        "qualified",
        "ga4_admin.property.123",
        "properties/123",
        "customer-defined qualified request event",
        "eventName",
    ):
        assert private_value not in serialized


def test_append_requires_latest_preceding_definition_but_preserves_history(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    first_envelope, first_contract = _retained_contract(ledger)
    first_candidate = retain_ga4_key_event_candidate(
        ledger,
        _request(first_envelope["record_digest"]),
        retained_contract=first_contract,
        retained_contract_record_digest=first_envelope["record_digest"],
    )
    second_definition = _definition(
        version=2,
        supersedes=first_contract.content_digest,
        effective_from=START + timedelta(days=1),
    )
    retain_conversion_contract(ledger, second_definition)

    with pytest.raises(AdhelmError) as error:
        retain_ga4_key_event_candidate(
            ledger,
            _request(first_envelope["record_digest"]),
            retained_contract=first_contract,
            retained_contract_record_digest=first_envelope["record_digest"],
        )
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT"

    historical, _ = load_ga4_key_event_candidate(ledger, first_candidate["record_digest"])
    assert historical.compilation.conversion_binding.contract_version == 1
    assert ga4_key_event_candidate_catalog(ledger)["total_count"] == 1


def test_preflight_failure_prevents_candidate_append(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    contract_envelope, contract = _retained_contract(ledger)
    before = ledger.checkpoint()["checkpoint_digest"]

    def reject(_: object) -> None:
        raise AdhelmError("OUTPUT_LIMIT_EXCEEDED", "Test preflight failure.", exit_code=2)

    with pytest.raises(AdhelmError) as error:
        retain_ga4_key_event_candidate(
            ledger,
            _request(contract_envelope["record_digest"]),
            retained_contract=contract,
            retained_contract_record_digest=contract_envelope["record_digest"],
            before_append=reject,
        )
    assert error.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert ledger.checkpoint()["checkpoint_digest"] == before


@pytest.mark.parametrize("cursor, limit", [(-1, 1), (0, 0), (0, 501), (True, 1), (0, True)])
def test_catalog_pagination_is_strict(tmp_path: Path, cursor: object, limit: object) -> None:
    with pytest.raises(AdhelmError) as error:
        ga4_key_event_candidate_catalog(Ledger(tmp_path), cursor=cursor, limit=limit)
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE_SELECTION"


def test_load_requires_verified_selected_record(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as error:
        load_ga4_key_event_candidate(Ledger(tmp_path), "f" * 64)
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_NOT_RETAINED"

    with pytest.raises(AdhelmError) as error:
        load_ga4_key_event_candidate(Ledger(tmp_path), "not-a-digest")
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE_SELECTION"


def test_saved_retention_does_not_accept_an_asserted_compiled_record(tmp_path: Path) -> None:
    """The only append route recompiles the original typed candidate input."""
    ledger = Ledger(tmp_path)
    contract_envelope, contract = _retained_contract(ledger)
    request = _request(contract_envelope["record_digest"])
    record = retain_ga4_key_event_candidate(
        ledger,
        request,
        retained_contract=contract,
        retained_contract_record_digest=contract_envelope["record_digest"],
    )
    assert record["record_kind"] == "GA4_KEY_EVENT_CANDIDATE_COMPILED"
    assert len(ga4_key_event_candidate_catalog(ledger)["candidates"]) == 1

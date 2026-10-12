from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.conversion_contracts import (
    ConversionContract,
    ConversionContractInput,
    compile_conversion_contract,
)
from adhelm.errors import AdhelmError
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    compile_ga4_key_event_candidate,
    revalidate_ga4_key_event_candidate_record,
    validate_ga4_key_event_candidate_binding,
)

SOURCE_RECORD_DIGEST = "a" * 64
FUTURE = datetime(2031, 1, 1, tzinfo=UTC)


def _definition_data(
    *,
    parameters: list[dict[str, object]] | None = None,
    counting: str = "EACH_QUALIFYING_OCCURRENCE",
    value: str = "NO_MONETARY_VALUE",
    effective_from: datetime = datetime(2026, 9, 1, tzinfo=UTC),
) -> dict[str, object]:
    counting_values: dict[str, object]
    if counting == "EACH_QUALIFYING_OCCURRENCE":
        counting_values = {
            "counting_semantics": counting,
            "deduplication_semantics": "NOT_APPLICABLE",
            "duplicate_occurrence_handling": "COUNT_EACH",
            "declared_window": "NOT_APPLICABLE",
        }
    else:
        counting_values = {
            "counting_semantics": counting,
            "deduplication_semantics": "SOURCE_MANAGED",
            "duplicate_occurrence_handling": "EXCLUDE_DUPLICATES",
            "declared_window": "NOT_APPLICABLE",
        }
    if value == "NO_MONETARY_VALUE":
        value_values: dict[str, object] = {
            "value_semantics": value,
            "fixed_value": None,
            "currency": None,
            "null_value_semantics": "NO_MONETARY_VALUE",
        }
    else:
        value_values = {
            "value_semantics": "SOURCE_REPORTED_VALUE",
            "fixed_value": None,
            "currency": "USD",
            "null_value_semantics": "PRESERVE_AS_UNAVAILABLE",
        }
    return {
        "schema_version": 1,
        "contract_id": "conversion.fixture",
        "version": 1,
        "supersedes_ref": None,
        "business_outcome": {
            "outcome_ref": "outcome.fixture",
            "statement": {"statement": "A qualified request confirmed by the customer"},
        },
        "authoritative_source": {
            "source_kind": "CRM_AGGREGATE",
            "source_ref": "source.fixture",
            "statement": {"statement": "A customer-configured fixed aggregate report"},
        },
        "qualifying_occurrence": {
            "occurrence_ref": "occurrence.fixture",
            "statement": {"statement": "The source confirms each qualified occurrence"},
        },
        "exclusions": [],
        "counting_and_deduplication": counting_values,
        "value_and_currency": value_values,
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
        "destination_profiles": ["ga4.fixture", "meta.fixture"],
        "effective_from": effective_from.isoformat(),
    }


def _contract(**kwargs: object) -> ConversionContract:
    return compile_conversion_contract(
        ConversionContractInput.model_validate_json(canonical_bytes(_definition_data(**kwargs)))
    )


def _request_data(
    *,
    contract_record_digest: str = SOURCE_RECORD_DIGEST,
    fields: list[dict[str, object]] | None = None,
    destination: str = "ga4.fixture",
    counting_method: str = "ONCE_PER_EVENT",
    default_value: object = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "conversion_contract_record_digest": contract_record_digest,
        "declared_destination_profile_ref": destination,
        "key_event": {
            "event_contract": {
                "event_contract_ref": "event.fixture",
                "event_name": "qualified_request",
                "semantics": {
                    "semantics_ref": "semantics.fixture",
                    "customer_definition": "A customer-defined qualified request event.",
                    "counting_rule": "Count each declared event occurrence.",
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
            "counting_method": counting_method,
            "default_value": default_value,
        },
    }


def _request(**kwargs: object) -> Ga4KeyEventCandidateInput:
    return Ga4KeyEventCandidateInput.model_validate_json(canonical_bytes(_request_data(**kwargs)))


def _compile(
    *, contract: ConversionContract | None = None, request: Ga4KeyEventCandidateInput | None = None
):
    selected = contract or _contract()
    return compile_ga4_key_event_candidate(
        request or _request(),
        retained_contract=selected,
        retained_contract_record_digest=SOURCE_RECORD_DIGEST,
    )


def test_compiles_private_candidate_with_closed_structural_binding() -> None:
    record = _compile()
    compilation = record.compilation
    binding = compilation.conversion_binding

    assert record.candidate_digest == compilation.candidate_digest
    assert compilation.compiler_candidate_digest == compilation.compiler_candidate.candidate_digest
    assert binding.conversion_contract_record_digest == SOURCE_RECORD_DIGEST
    assert binding.contract_id == "conversion.fixture"
    assert binding.bound_boolean_fields[0].model_dump(mode="json") == {
        "name": "qualified",
        "event_value_type": "BOOLEAN",
        "event_required": True,
    }
    assert binding.counting_binding == "EACH_QUALIFYING_OCCURRENCE_TO_ONCE_PER_EVENT"
    assert binding.value_binding == "NO_MONETARY_VALUE_TO_NO_DEFAULT_VALUE"
    assert binding.semantic_accuracy == "NOT_VERIFIED"
    assert binding.source_event_mapping == "NOT_PROVEN"
    assert binding.effective_time_status == "DECLARED_NOT_EVALUATED"
    assert compilation.execution_status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert compilation.remote_side_effects is False
    assert compilation.execution_authority is False
    assert compilation.compiler_candidate.planning_boundary.requires_exact_authorization
    assert compilation.compiler_candidate.planning_boundary.requires_processed_report_verification

    persisted = canonical_bytes(record)
    assert b"customer-defined qualified request event" not in persisted
    assert b"Count each declared event occurrence" not in persisted
    assert b"qualified request confirmed by the customer" not in persisted


@pytest.mark.parametrize(
    ("contract", "candidate_request"),
    [
        (_contract(counting="ONCE_PER_QUALIFYING_OCCURRENCE"), _request()),
        (_contract(value="SOURCE_REPORTED_VALUE"), _request()),
        (
            _contract(),
            _request(fields=[{"name": "qualified", "value_type": "INTEGER", "required": True}]),
        ),
        (
            _contract(),
            _request(destination="ga4.unlisted"),
        ),
    ],
)
def test_rejects_unproven_count_value_field_or_destination_mapping(
    contract: ConversionContract, candidate_request: Ga4KeyEventCandidateInput
) -> None:
    with pytest.raises(AdhelmError) as error:
        _compile(contract=contract, request=candidate_request)
    assert error.value.code in {
        "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT",
        "INVALID_GA4_KEY_EVENT_CANDIDATE",
    }


def test_required_boolean_requirement_is_exact_and_required_nonboolean_fails_closed() -> None:
    optional = _contract(
        parameters=[
            {
                "name": "qualified",
                "value_type": "BOOLEAN",
                "required": False,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": [],
            }
        ]
    )
    with pytest.raises(AdhelmError):
        _compile(contract=optional)

    required_integer = _contract(
        parameters=[
            {
                "name": "qualified",
                "value_type": "BOOLEAN",
                "required": True,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": [],
            },
            {
                "name": "quantity",
                "value_type": "INTEGER",
                "required": True,
                "integer_minimum": 1,
                "integer_maximum": 3,
                "allowed_values": [],
            },
        ]
    )
    with pytest.raises(AdhelmError) as error:
        _compile(contract=required_integer)
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT"


def test_future_contract_time_is_declared_not_evaluated() -> None:
    record = _compile(contract=_contract(effective_from=FUTURE))
    assert record.compilation.conversion_binding.contract_effective_from == FUTURE
    assert record.compilation.effective_time_status == "DECLARED_NOT_EVALUATED"


@pytest.mark.parametrize(
    "path, value",
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("compilation", "remote_side_effects"), 0),
        (("compilation", "conversion_binding", "contract_version"), True),
        (("compilation", "conversion_binding", "bound_boolean_fields", 0, "event_required"), 1),
        (
            (
                "compilation",
                "compiler_candidate",
                "planning_boundary",
                "requires_exact_authorization",
            ),
            1,
        ),
    ],
)
def test_record_revalidation_rejects_coercible_or_constructed_nested_scalars(
    path: tuple[str | int, ...], value: object
) -> None:
    data = _compile().model_dump(mode="json")
    target: object = data
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_key_event_candidate_record(data)
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"


def test_record_revalidation_rejects_python_only_decimal_and_constructed_model() -> None:
    data = _compile().model_dump(mode="json")
    data["compilation"]["conversion_binding"]["contract_version"] = Decimal("1")
    with pytest.raises(AdhelmError):
        revalidate_ga4_key_event_candidate_record(data)

    record = _compile()
    forged_binding = record.compilation.conversion_binding.model_construct(contract_version=True)
    forged_compilation = record.compilation.model_construct(conversion_binding=forged_binding)
    forged_record = record.model_construct(compilation=forged_compilation)
    with pytest.raises(AdhelmError):
        revalidate_ga4_key_event_candidate_record(forged_record)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), True),
        (("definition", "schema_version"), 1.0),
        (("definition", "allowed_parameter_schema", 0, "required"), 1),
    ],
)
def test_binding_rejects_tampered_contract_or_source_reference(
    path: tuple[str | int, ...], replacement: object
) -> None:
    contract = _contract()
    record = _compile(contract=contract)
    validate_ga4_key_event_candidate_binding(record, contract, SOURCE_RECORD_DIGEST)

    bad_source = "b" * 64
    with pytest.raises(AdhelmError) as error:
        validate_ga4_key_event_candidate_binding(record, contract, bad_source)
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT"

    raw = contract.model_dump(mode="json")
    target: object = raw
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index]
    target[path[-1]] = replacement  # type: ignore[index]
    constructed = ConversionContract.model_construct(**raw)
    with pytest.raises(AdhelmError) as error:
        validate_ga4_key_event_candidate_binding(record, constructed, SOURCE_RECORD_DIGEST)
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"


def test_compile_rejects_constructed_input_with_nonboolean_event_requirement() -> None:
    request = _request()
    raw = request.model_dump(mode="json")
    raw["key_event"]["event_contract"]["fields"][0]["required"] = 1
    constructed = Ga4KeyEventCandidateInput.model_construct(**raw)
    with pytest.raises(AdhelmError) as error:
        _compile(request=constructed)
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"


def test_record_revalidation_rejects_rehashed_binding_mismatch() -> None:
    data = deepcopy(_compile().model_dump(mode="json"))
    data["compilation"]["conversion_binding"]["content_digest"] = "b" * 64
    # A new outer digest cannot make a self-consistent source binding truthful.
    with pytest.raises(AdhelmError):
        revalidate_ga4_key_event_candidate_record(data)


def test_record_revalidation_preserves_event_name_privacy_screening_after_rehash() -> None:
    data = deepcopy(_compile().model_dump(mode="json"))
    compiler = data["compilation"]["compiler_candidate"]
    compiler["request"]["eventName"] = "email_received"
    data["compilation"]["compiler_candidate_digest"] = digest(compiler)
    data["candidate_digest"] = digest(data["compilation"])

    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_key_event_candidate_record(data)
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"


@pytest.mark.parametrize(
    ("identity_field", "replacement"),
    [
        ("property_ref", "ga4_admin.property.relabelled"),
        ("web_stream_ref", "ga4_admin.web_stream.relabelled"),
    ],
)
def test_record_revalidation_rejects_rehashed_native_identity_relabeling(
    identity_field: str, replacement: str
) -> None:
    data = deepcopy(_compile().model_dump(mode="json"))
    compiler = data["compilation"]["compiler_candidate"]
    compiler["identity"][identity_field] = replacement
    data["compilation"]["compiler_candidate_digest"] = digest(compiler)
    data["candidate_digest"] = digest(data["compilation"])

    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_key_event_candidate_record(data)
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CANDIDATE"

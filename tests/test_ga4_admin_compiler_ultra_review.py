"""Adversarial boundaries for the offline GA4 Admin key-event compiler."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
    compile_ga4_admin_key_event_create,
)


def _input(
    *,
    event_name: str = "demo_request",
    default: Decimal | None = Decimal("1.25"),
    property_ref: str = "ga4_admin.property.123",
    web_stream_ref: str = "ga4_admin.web_stream.456",
    fields: tuple[EventField, ...] = (
        EventField(name="form_state", value_type="LOW_CARDINALITY_STRING"),
    ),
) -> Ga4AdminKeyEventCreateInput:
    return Ga4AdminKeyEventCreateInput(
        event_contract=EventContract(
            event_contract_ref="event/demo-request",
            event_name=event_name,
            semantics=ConversionSemantics(
                semantics_ref="semantics/demo-request-v1",
                customer_definition="A visitor explicitly requests a sales demonstration.",
                counting_rule="Count each qualifying request event in its own session.",
            ),
            fields=fields,
        ),
        identity=Ga4AdminKeyEventIdentity(
            property_ref=property_ref,
            property_client_ref="123",
            web_stream_ref=web_stream_ref,
            web_stream_client_ref="456",
        ),
        counting_method="ONCE_PER_EVENT",
        default_value=(
            Ga4AdminKeyEventDefaultValueInput(numeric_value=default, currency_code="USD")
            if default is not None
            else None
        ),
    )


def test_key_event_name_uses_ga4_40_character_limit_at_all_boundaries() -> None:
    boundary = compile_ga4_admin_key_event_create(_input(event_name="a" * 40))
    assert boundary.request.eventName == "a" * 40

    with pytest.raises(ValidationError, match="40 characters"):
        _input(event_name="a" * 41)

    forged_contract = _input().event_contract.model_copy(update={"event_name": "a" * 41})
    forged_input = _input().model_copy(update={"event_contract": forged_contract})
    with pytest.raises(ValidationError, match="40 characters"):
        compile_ga4_admin_key_event_create(forged_input)


def test_declared_event_fields_follow_ga4_parameter_name_and_count_limits() -> None:
    with pytest.raises(ValidationError, match="parameter names must be 40"):
        _input(fields=(EventField(name="p" * 41, value_type="LOW_CARDINALITY_STRING"),))

    maximum = tuple(
        EventField(name=f"parameter_{index}", value_type="LOW_CARDINALITY_STRING")
        for index in range(25)
    )
    assert (
        len(compile_ga4_admin_key_event_create(_input(fields=maximum)).declared_field_names) == 25
    )

    too_many = tuple(
        EventField(name=f"parameter_{index}", value_type="LOW_CARDINALITY_STRING")
        for index in range(26)
    )
    with pytest.raises(ValidationError, match="at most 25"):
        _input(fields=too_many)

    forged_contract = _input().event_contract.model_copy(update={"fields": too_many})
    forged_input = _input().model_copy(update={"event_contract": forged_contract})
    with pytest.raises(ValidationError, match="at most 25"):
        compile_ga4_admin_key_event_create(forged_input)


def test_compiler_revalidates_forged_native_identifiers_before_candidate_creation() -> None:
    forged_identity = _input().identity.model_copy(update={"property_client_ref": "not-numeric"})
    forged_input = _input().model_copy(update={"identity": forged_identity})

    with pytest.raises(ValidationError):
        compile_ga4_admin_key_event_create(forged_input)


def test_identity_refs_must_bind_to_native_ids_but_configured_mapping_can_be_used() -> None:
    mismatched = _input(property_ref="ga4_admin.property.999")
    with pytest.raises(AdhelmError) as error:
        compile_ga4_admin_key_event_create(mismatched)
    assert error.value.code == "IDENTITY_MISMATCH"

    custom = _input(property_ref="local.property.123", web_stream_ref="local.stream.456")
    compiled = compile_ga4_admin_key_event_create(
        custom,
        reference_for=lambda kind, value: (
            f"local.{'stream' if kind == 'web_stream' else kind}.{value}"
        ),
    )
    assert compiled.target.parent == "properties/123"


def test_numeric_default_rejects_signed_zero_and_canonicalizes_exact_equal_inputs() -> None:
    with pytest.raises(ValidationError, match="signed zero"):
        Ga4AdminKeyEventDefaultValueInput(numeric_value=Decimal("-0"), currency_code="USD")

    forged_default = _input().default_value.model_copy(update={"numeric_value": Decimal("0.1")})
    forged_input = _input().model_copy(update={"default_value": forged_default})
    with pytest.raises(ValidationError, match="exactly representable"):
        compile_ga4_admin_key_event_create(forged_input)

    integer = compile_ga4_admin_key_event_create(_input(default=Decimal("1")))
    scaled = compile_ga4_admin_key_event_create(_input(default=Decimal("1.0")))
    assert integer.request.model_dump(mode="json") == scaled.request.model_dump(mode="json")
    assert integer.input_digest == scaled.input_digest
    assert integer.candidate_digest == scaled.candidate_digest

    zero = compile_ga4_admin_key_event_create(_input(default=Decimal("0")))
    exponent_zero = compile_ga4_admin_key_event_create(_input(default=Decimal("0E+2")))
    assert zero.input_digest == exponent_zero.input_digest
    assert zero.candidate_digest == exponent_zero.candidate_digest


@pytest.mark.parametrize("unsafe_field_name", ("emailaddress", "ssn_hash", "formvalues"))
def test_sensitive_concatenated_event_and_field_names_fail_closed(unsafe_field_name: str) -> None:
    with pytest.raises(ValidationError, match="person-level"):
        _input(event_name="emailreceived")

    unsafe_field = EventField(name=unsafe_field_name, value_type="LOW_CARDINALITY_STRING")
    unsafe_contract = _input().event_contract.model_copy(update={"fields": (unsafe_field,)})
    unsafe_input = _input().model_copy(update={"event_contract": unsafe_contract})
    with pytest.raises(ValidationError, match="person-level"):
        compile_ga4_admin_key_event_create(unsafe_input)


def test_compiler_rejects_person_level_semantics_before_candidate_hashing() -> None:
    unsafe_semantics = _input().event_contract.semantics.model_copy(
        update={"customer_definition": "A person@example.test explicitly requests a demo."}
    )
    unsafe_contract = _input().event_contract.model_copy(update={"semantics": unsafe_semantics})
    unsafe_input = _input().model_copy(update={"event_contract": unsafe_contract})

    with pytest.raises(AdhelmError) as error:
        compile_ga4_admin_key_event_create(unsafe_input)
    assert error.value.code == "SENSITIVE_DATA_RISK"


def test_candidate_retains_semantics_reference_and_digest_not_freeform_definition() -> None:
    definition = "A deliberately distinctive but nonsensitive demonstration request definition."
    semantics = _input().event_contract.semantics.model_copy(
        update={"customer_definition": definition}
    )
    event_contract = _input().event_contract.model_copy(update={"semantics": semantics})
    candidate = compile_ga4_admin_key_event_create(
        _input().model_copy(update={"event_contract": event_contract})
    )

    rendered = json.dumps(candidate.model_dump(mode="json"), sort_keys=True)
    assert definition not in rendered
    assert candidate.semantics_ref == "semantics/demo-request-v1"


def test_default_event_candidate_does_not_bypass_later_collection_eligibility_check() -> None:
    # The KeyEvent API resource itself names ``click`` as an example, so an
    # offline compiler cannot treat every collection-reserved/default name as
    # invalid. It must require a later live eligibility and inventory check.
    candidate = compile_ga4_admin_key_event_create(_input(event_name="click"))

    assert candidate.request.eventName == "click"
    assert candidate.planning_boundary.requires_verified_event_collection_and_reserved_name_check
    assert candidate.prospective_effects.configuration_surface == "LIVE_CONFIGURATION_WRITE"
    assert candidate.prospective_effects.browser_or_receipt_proven is False

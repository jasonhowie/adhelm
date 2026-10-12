from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
    compile_ga4_admin_key_event_create,
)


def _input(*, default: Decimal | None = Decimal("1.25")) -> Ga4AdminKeyEventCreateInput:
    return Ga4AdminKeyEventCreateInput(
        event_contract=EventContract(
            event_contract_ref="event/demo-request",
            event_name="demo_request",
            semantics=ConversionSemantics(
                semantics_ref="semantics/demo-request-v1",
                customer_definition="A visitor explicitly requests a sales demonstration.",
                counting_rule="Count each qualifying request event in its own session.",
            ),
            fields=(EventField(name="form_state", value_type="LOW_CARDINALITY_STRING"),),
        ),
        identity=Ga4AdminKeyEventIdentity(
            property_ref="ga4_admin.property.123",
            property_client_ref="123",
            web_stream_ref="ga4_admin.web_stream.456",
            web_stream_client_ref="456",
        ),
        counting_method="ONCE_PER_EVENT",
        default_value=(
            Ga4AdminKeyEventDefaultValueInput(numeric_value=default, currency_code="USD")
            if default is not None
            else None
        ),
    )


def test_compiles_only_the_documented_property_scoped_create_body() -> None:
    compiled = compile_ga4_admin_key_event_create(_input())

    assert compiled.target.host == "analyticsadmin.googleapis.com"
    assert compiled.target.method == "POST"
    assert compiled.target.parent == "properties/123"
    assert compiled.target.path == "/v1alpha/properties/123/keyEvents"
    assert compiled.request.model_dump(mode="json") == {
        "eventName": "demo_request",
        "countingMethod": "ONCE_PER_EVENT",
        "defaultValue": {"numericValue": 1.25, "currencyCode": "USD"},
    }
    assert compiled.declared_field_names == ("form_state",)
    assert compiled.prospective_effects.configuration_surface == "LIVE_CONFIGURATION_WRITE"
    assert compiled.prospective_effects.historical_backfill is False
    assert compiled.prospective_effects.native_conversion_mapping == "NONE_CREATED"
    assert compiled.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert compiled.planning_boundary.requires_exact_authorization is True
    assert compiled.planning_boundary.requires_processed_report_verification is True


def test_compilation_is_deterministic_and_default_is_optional() -> None:
    first = compile_ga4_admin_key_event_create(_input())
    second = compile_ga4_admin_key_event_create(_input())
    without_default = compile_ga4_admin_key_event_create(_input(default=None))

    assert first.candidate_digest == second.candidate_digest
    assert first.input_digest == second.input_digest
    assert first.candidate_digest != without_default.candidate_digest
    assert without_default.request.defaultValue is None


def test_default_value_must_be_exact_finite_api_double() -> None:
    with pytest.raises(ValidationError, match="exactly representable"):
        Ga4AdminKeyEventDefaultValueInput(numeric_value=Decimal("0.1"), currency_code="USD")
    with pytest.raises(ValidationError, match="finite"):
        Ga4AdminKeyEventDefaultValueInput(numeric_value=Decimal("NaN"), currency_code="USD")
    with pytest.raises(ValidationError, match="exactly representable"):
        Ga4AdminKeyEventDefaultValueInput(numeric_value=Decimal("1e-1000"), currency_code="USD")


def test_person_level_events_and_generic_fields_are_rejected_before_candidate() -> None:
    unsafe_contract = _input().event_contract.model_copy(update={"event_name": "email_received"})
    unsafe_input = _input().model_dump(mode="python")
    unsafe_input["event_contract"] = unsafe_contract.model_dump(mode="python")
    with pytest.raises(ValidationError, match="person-level"):
        Ga4AdminKeyEventCreateInput.model_validate(unsafe_input)

    with pytest.raises(ValidationError, match="Extra inputs"):
        Ga4AdminKeyEventCreateInput.model_validate(
            {**_input().model_dump(mode="python"), "parameterMutations": [{"name": "x"}]}
        )

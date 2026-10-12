from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest
from adhelm.conversion_contracts import (
    AuthoritativeSource,
    BusinessOutcome,
    ConversionContract,
    ConversionContractInput,
    ConversionParameter,
    CountingAndDeduplication,
    CustomerDeclaredStatement,
    Exclusion,
    QualifyingOccurrence,
    ValueAndCurrency,
    compile_conversion_contract,
)
from adhelm.errors import AdhelmError
from adhelm.models import Money

_PROHIBITED = (
    "PERSONAL_IDENTIFIER",
    "AUTHENTICATION_SECRET",
    "CLICK_IDENTIFIER",
    "URL_OR_QUERY",
    "DOM_OR_PAGE_TEXT",
    "FORM_VALUE",
    "CRM_RECORD_IDENTIFIER",
)


def _statement(value: str) -> CustomerDeclaredStatement:
    return CustomerDeclaredStatement(statement=value)


def _parameter(
    *,
    name: str = "consent_state",
    value_type: Literal["BOOLEAN", "INTEGER", "LOW_CARDINALITY_ENUM"] = "LOW_CARDINALITY_ENUM",
    required: bool = True,
    integer_minimum: int | None = None,
    integer_maximum: int | None = None,
    allowed_values: tuple[str, ...] = ("granted", "denied"),
) -> ConversionParameter:
    return ConversionParameter(
        name=name,
        value_type=value_type,
        required=required,
        integer_minimum=integer_minimum,
        integer_maximum=integer_maximum,
        allowed_values=allowed_values,
    )


def _input(
    *,
    version: int = 1,
    supersedes_ref: str | None = None,
    effective_from: datetime = datetime(2026, 9, 4, 12, tzinfo=UTC),
    outcome_statement: str = "A customer-declared qualified demonstration request.",
) -> ConversionContractInput:
    return ConversionContractInput(
        contract_id="conversion.demo-request",
        version=version,
        supersedes_ref=supersedes_ref,
        business_outcome=BusinessOutcome(
            outcome_ref="outcome.demo-request",
            statement=_statement(outcome_statement),
        ),
        authoritative_source=AuthoritativeSource(
            source_kind="CRM_AGGREGATE",
            source_ref="source.crm.aggregate",
            statement=_statement("Customer-declared aggregate CRM source."),
        ),
        qualifying_occurrence=QualifyingOccurrence(
            occurrence_ref="occurrence.qualified-demo",
            statement=_statement("Customer-declared qualified occurrence."),
        ),
        exclusions=(
            Exclusion(
                exclusion_ref="exclusion.synthetic-test",
                statement=_statement("Synthetic test activity is excluded."),
            ),
        ),
        counting_and_deduplication=CountingAndDeduplication(
            counting_semantics="ONCE_PER_QUALIFYING_OCCURRENCE",
            deduplication_semantics="SOURCE_MANAGED",
            duplicate_occurrence_handling="EXCLUDE_DUPLICATES",
            declared_window="NOT_APPLICABLE",
        ),
        value_and_currency=ValueAndCurrency(
            value_semantics="FIXED_VALUE",
            fixed_value=Money(minor_units=12_500, currency="USD"),
            currency="USD",
            null_value_semantics="NOT_ALLOWED",
        ),
        allowed_parameter_schema=(
            _parameter(),
            _parameter(
                name="is_qualified",
                value_type="BOOLEAN",
                integer_minimum=None,
                integer_maximum=None,
                allowed_values=(),
            ),
            _parameter(
                name="plan_tier",
                value_type="INTEGER",
                required=False,
                integer_minimum=0,
                integer_maximum=3,
                allowed_values=(),
            ),
        ),
        prohibited_parameters=_PROHIBITED,
        consent_contract_ref="consent.contract.v1",
        destination_profiles=("profile.ga4", "profile.google-ads"),
        effective_from=effective_from,
    )


def test_compiles_only_a_customer_declared_non_executable_wrapper() -> None:
    definition = _input()
    first = compile_conversion_contract(definition)
    second = compile_conversion_contract(_input())

    assert first.schema_version == 1
    assert first.definition == definition
    assert first.content_digest == digest(definition)
    assert first.content_digest == second.content_digest
    assert first.provenance == "CUSTOMER_DECLARED"
    assert first.execution_authority is False
    assert set(first.model_dump(mode="json")) == {
        "schema_version",
        "definition",
        "content_digest",
        "provenance",
        "execution_authority",
    }


def test_canonical_json_definition_revalidates_at_the_public_boundary() -> None:
    definition = _input()
    raw = canonical_bytes(definition.model_dump(mode="json"))

    assert ConversionContractInput.model_validate_json(raw) == definition


def test_digest_covers_exact_revalidated_definition_and_wrapper_rejects_forgery() -> None:
    compiled = compile_conversion_contract(_input())
    changed = compile_conversion_contract(_input(outcome_statement="A declared pipeline outcome."))
    forged = compiled.model_copy(update={"content_digest": "0" * 64})

    assert changed.content_digest != compiled.content_digest
    with pytest.raises(ValidationError, match="exact canonical definition"):
        ConversionContract.model_validate(forged.model_dump(mode="python"))
    with pytest.raises(ValidationError):
        ConversionContract.model_validate(
            {**compiled.model_dump(mode="python"), "execution_authority": True}
        )


def test_compiler_revalidates_model_copy_and_nested_values_before_digesting() -> None:
    unsafe_parameter = _parameter().model_copy(update={"name": "customer_id"})
    forged_definition = _input().model_copy(
        update={"allowed_parameter_schema": (unsafe_parameter,)}
    )

    with pytest.raises(ValidationError, match="sensitive, identifier, URL, or DOM"):
        compile_conversion_contract(forged_definition)


def test_versions_require_explicit_prior_digest_but_do_not_enforce_ledger_chain() -> None:
    with pytest.raises(ValidationError, match="cannot supersede"):
        _input(supersedes_ref="a" * 64)
    with pytest.raises(ValidationError, match="require a prior content digest"):
        _input(version=2)

    later = _input(version=2, supersedes_ref="a" * 64)

    assert later.version == 2
    assert later.supersedes_ref == "a" * 64


def test_effective_time_is_aware_utc_and_statement_whitespace_is_not_rewritten() -> None:
    local_time = datetime(2026, 9, 4, 7, tzinfo=timezone(timedelta(hours=-5)))
    definition = _input(effective_from=local_time)

    assert definition.effective_from == datetime(2026, 9, 4, 12, tzinfo=UTC)
    assert _statement("  Customer wording remains exact.  ").statement == (
        "  Customer wording remains exact.  "
    )
    with pytest.raises(ValidationError, match="timezone-aware"):
        _input(effective_from=datetime(2026, 9, 4, 12))
    with pytest.raises(ValidationError, match="nonblank"):
        _statement("   ")


def test_parameter_schema_preserves_requiredness_and_only_allows_finite_domains() -> None:
    boolean = _parameter(
        name="has_consent",
        value_type="BOOLEAN",
        integer_minimum=None,
        integer_maximum=None,
        allowed_values=(),
    )
    integer = _parameter(
        name="plan_tier",
        value_type="INTEGER",
        required=False,
        integer_minimum=0,
        integer_maximum=3,
        allowed_values=(),
    )
    enum = _parameter()

    assert boolean.required is True
    assert integer.required is False
    assert enum.allowed_values == ("granted", "denied")
    with pytest.raises(ValidationError, match="Field required"):
        ConversionParameter.model_validate(
            {
                "name": "has_consent",
                "value_type": "BOOLEAN",
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": (),
            }
        )
    with pytest.raises(ValidationError, match="Integer parameters"):
        _parameter(
            name="plan_tier",
            value_type="INTEGER",
            integer_minimum=None,
            integer_maximum=3,
            allowed_values=(),
        )
    with pytest.raises(ValidationError, match="Enum parameters"):
        _parameter(
            value_type="LOW_CARDINALITY_ENUM",
            integer_minimum=None,
            integer_maximum=None,
            allowed_values=(),
        )


@pytest.mark.parametrize(
    "name",
    ("email", "customer_id", "landing_url", "dom_text", "visitor_uuid", "form_values"),
)
def test_parameter_names_reject_sensitive_identifier_url_and_dom_surfaces(name: str) -> None:
    with pytest.raises(ValidationError, match="sensitive, identifier, URL, or DOM"):
        _parameter(name=name)


def test_prohibited_categories_and_closed_fields_are_required() -> None:
    with pytest.raises(ValidationError, match="required prohibited"):
        ConversionContractInput.model_validate(
            {
                **_input().model_dump(mode="python"),
                "prohibited_parameters": _PROHIBITED[:-1] + (_PROHIBITED[0],),
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConversionParameter.model_validate(
            {
                **_parameter().model_dump(mode="python"),
                "json_schema": {"type": "object"},
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConversionContractInput.model_validate(
            {**_input().model_dump(mode="python"), "profile_discovery": True}
        )


def test_value_null_and_counting_semantics_cannot_be_ambiguous() -> None:
    source_reported = ValueAndCurrency(
        value_semantics="SOURCE_REPORTED_VALUE",
        fixed_value=None,
        currency="USD",
        null_value_semantics="PRESERVE_AS_UNAVAILABLE",
    )
    non_monetary = ValueAndCurrency(
        value_semantics="NO_MONETARY_VALUE",
        fixed_value=None,
        currency=None,
        null_value_semantics="NO_MONETARY_VALUE",
    )

    assert source_reported.fixed_value is None
    assert non_monetary.currency is None
    with pytest.raises(ValidationError, match="Fixed value semantics"):
        ValueAndCurrency(
            value_semantics="FIXED_VALUE",
            fixed_value=Money(minor_units=1, currency="USD"),
            currency="EUR",
            null_value_semantics="NOT_ALLOWED",
        )
    with pytest.raises(ValidationError, match="Windowed counting"):
        CountingAndDeduplication(
            counting_semantics="ONCE_PER_DECLARED_WINDOW",
            deduplication_semantics="SOURCE_MANAGED",
            duplicate_occurrence_handling="EXCLUDE_DUPLICATES",
            declared_window="NOT_APPLICABLE",
        )


def test_declarations_do_not_accept_person_level_values() -> None:
    with pytest.raises(AdhelmError, match="Prohibited sensitive text"):
        _statement("A customer declaration for person@example.test")

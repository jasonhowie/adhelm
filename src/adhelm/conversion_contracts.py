"""Closed, local-only customer declarations for conversion meaning.

This module records an explicit customer definition.  It does not inspect a
site or CRM, discover profiles, retrieve records, decide whether a declaration
is true, or authorize an operation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.models import Currency, Money, SafeRef, StrictModel

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_PARAMETER_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
_ENUM_VALUE_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
_MAX_PARAMETERS = 25
_MAX_ENUM_VALUES = 20
_MAX_EXCLUSIONS = 20
_MAX_DESTINATION_PROFILES = 20

Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]
ParameterName = Annotated[
    str,
    Field(min_length=1, max_length=40, pattern=_PARAMETER_NAME_PATTERN),
]
LowCardinalityEnumValue = Annotated[
    str,
    Field(min_length=1, max_length=32, pattern=_ENUM_VALUE_PATTERN),
]
BoundedInteger = Annotated[int, Field(ge=-1_000_000, le=1_000_000)]

ProhibitedParameterCategory = Literal[
    "PERSONAL_IDENTIFIER",
    "AUTHENTICATION_SECRET",
    "CLICK_IDENTIFIER",
    "URL_OR_QUERY",
    "DOM_OR_PAGE_TEXT",
    "FORM_VALUE",
    "CRM_RECORD_IDENTIFIER",
]

_REQUIRED_PROHIBITED_PARAMETER_CATEGORIES = frozenset(
    {
        "PERSONAL_IDENTIFIER",
        "AUTHENTICATION_SECRET",
        "CLICK_IDENTIFIER",
        "URL_OR_QUERY",
        "DOM_OR_PAGE_TEXT",
        "FORM_VALUE",
        "CRM_RECORD_IDENTIFIER",
    }
)
_FORBIDDEN_PARAMETER_COMPONENTS = frozenset(
    {
        "account",
        "address",
        "auth",
        "authorization",
        "click",
        "client",
        "condition",
        "contact",
        "cookie",
        "credit",
        "customer",
        "data",
        "debt",
        "device",
        "diagnosis",
        "disease",
        "disability",
        "dom",
        "domain",
        "email",
        "element",
        "eligibility",
        "employee",
        "ethnicity",
        "field",
        "financial",
        "form",
        "fragment",
        "gender",
        "genetic",
        "health",
        "host",
        "html",
        "href",
        "id",
        "identifier",
        "identity",
        "input",
        "income",
        "ip",
        "key",
        "lead",
        "mail",
        "member",
        "medical",
        "medication",
        "name",
        "page",
        "password",
        "path",
        "patient",
        "payload",
        "person",
        "phone",
        "query",
        "race",
        "referrer",
        "religion",
        "secret",
        "selector",
        "session",
        "sex",
        "sexual",
        "ssn",
        "symptom",
        "text",
        "token",
        "treatment",
        "underwriting",
        "uri",
        "url",
        "user",
        "uuid",
        "value",
    }
)
_FORBIDDEN_ENUM_VALUE_COMPONENTS = _FORBIDDEN_PARAMETER_COMPONENTS | frozenset(
    {
        "aids",
        "cancer",
        "diabetes",
        "hiv",
        "oncology",
        "pregnancy",
        "pregnant",
    }
)
_FORBIDDEN_COMPACT_PARAMETER_NAMES = frozenset(
    {
        "access_token",
        "apikey",
        "clientid",
        "contactid",
        "customerid",
        "data_layer",
        "datalayer",
        "domtext",
        "emailaddress",
        "fbclid",
        "formvalue",
        "formvalues",
        "gclid",
        "leadid",
        "msclkid",
        "pagetext",
        "refresh_token",
        "ttclid",
        "userid",
        "visitorid",
    }
)


class CustomerDeclaredStatement(StrictModel):
    """Bounded statement supplied by the customer, not semantic proof."""

    statement: str = Field(min_length=3, max_length=320)

    @field_validator("statement")
    @classmethod
    def printable_statement(cls, value: str) -> str:
        # Preserve user-supplied printable whitespace exactly for the definition
        # digest; only an all-whitespace value means no declaration at all.
        if not value.isprintable() or not value.strip():
            raise ValueError("Customer-declared statements must be printable and nonblank")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, (dict, list)):
                raise ValueError(
                    "Customer-declared statements cannot embed a raw event or person payload"
                )
        return value


class BusinessOutcome(StrictModel):
    outcome_ref: SafeRef
    statement: CustomerDeclaredStatement


class AuthoritativeSource(StrictModel):
    source_kind: Literal[
        "CRM_AGGREGATE",
        "ANALYTICS_AGGREGATE",
        "NATIVE_DESTINATION",
        "CUSTOMER_DECLARED_BUSINESS_SYSTEM",
    ]
    source_ref: SafeRef
    statement: CustomerDeclaredStatement


class QualifyingOccurrence(StrictModel):
    occurrence_ref: SafeRef
    statement: CustomerDeclaredStatement


class Exclusion(StrictModel):
    exclusion_ref: SafeRef
    statement: CustomerDeclaredStatement


class CountingAndDeduplication(StrictModel):
    """Explicit counting behavior without a person-level deduplication key."""

    counting_semantics: Literal[
        "ONCE_PER_QUALIFYING_OCCURRENCE",
        "ONCE_PER_DECLARED_WINDOW",
        "EACH_QUALIFYING_OCCURRENCE",
    ]
    deduplication_semantics: Literal["SOURCE_MANAGED", "NOT_APPLICABLE"]
    duplicate_occurrence_handling: Literal["EXCLUDE_DUPLICATES", "COUNT_EACH"]
    declared_window: Literal["NOT_APPLICABLE", "CALENDAR_DAY", "CONVERSION_WINDOW"]

    @model_validator(mode="after")
    def explicit_counting_combination(self) -> CountingAndDeduplication:
        if self.counting_semantics == "ONCE_PER_QUALIFYING_OCCURRENCE":
            if (
                self.deduplication_semantics != "SOURCE_MANAGED"
                or self.duplicate_occurrence_handling != "EXCLUDE_DUPLICATES"
                or self.declared_window != "NOT_APPLICABLE"
            ):
                raise ValueError("Once-per-occurrence counting requires source-managed exclusion")
        elif self.counting_semantics == "ONCE_PER_DECLARED_WINDOW":
            if (
                self.deduplication_semantics != "SOURCE_MANAGED"
                or self.duplicate_occurrence_handling != "EXCLUDE_DUPLICATES"
                or self.declared_window == "NOT_APPLICABLE"
            ):
                raise ValueError("Windowed counting requires a declared source-managed window")
        elif (
            self.deduplication_semantics != "NOT_APPLICABLE"
            or self.duplicate_occurrence_handling != "COUNT_EACH"
            or self.declared_window != "NOT_APPLICABLE"
        ):
            raise ValueError("Each-occurrence counting cannot claim deduplication or a window")
        return self


class ValueAndCurrency(StrictModel):
    """Declared monetary behavior; missing source values are never converted to zero."""

    value_semantics: Literal["FIXED_VALUE", "SOURCE_REPORTED_VALUE", "NO_MONETARY_VALUE"]
    fixed_value: Money | None
    currency: Currency | None
    null_value_semantics: Literal[
        "NOT_ALLOWED",
        "PRESERVE_AS_UNAVAILABLE",
        "NO_MONETARY_VALUE",
    ]

    @model_validator(mode="after")
    def explicit_value_and_null_combination(self) -> ValueAndCurrency:
        if self.value_semantics == "FIXED_VALUE":
            if (
                self.fixed_value is None
                or self.currency is None
                or self.currency != self.fixed_value.currency
                or self.null_value_semantics != "NOT_ALLOWED"
            ):
                raise ValueError("Fixed value semantics require one matching non-null Money value")
        elif self.value_semantics == "SOURCE_REPORTED_VALUE":
            if (
                self.fixed_value is not None
                or self.currency is None
                or self.null_value_semantics not in {"NOT_ALLOWED", "PRESERVE_AS_UNAVAILABLE"}
            ):
                raise ValueError(
                    "Source-reported values require a currency and explicit null handling"
                )
        elif (
            self.fixed_value is not None
            or self.currency is not None
            or self.null_value_semantics != "NO_MONETARY_VALUE"
        ):
            raise ValueError("No-monetary-value semantics cannot carry Money or currency")
        return self


class ConversionParameter(StrictModel):
    """One permitted nonsensitive parameter with an intentionally finite domain."""

    name: ParameterName
    value_type: Literal["BOOLEAN", "INTEGER", "LOW_CARDINALITY_ENUM"]
    required: bool
    integer_minimum: BoundedInteger | None
    integer_maximum: BoundedInteger | None
    allowed_values: tuple[LowCardinalityEnumValue, ...] = Field(max_length=_MAX_ENUM_VALUES)

    @field_validator("name")
    @classmethod
    def non_sensitive_name(cls, value: str) -> str:
        components = frozenset(value.split("_"))
        compact = "".join(value.split("_"))
        if (
            components & _FORBIDDEN_PARAMETER_COMPONENTS
            or compact in _FORBIDDEN_COMPACT_PARAMETER_NAMES
        ):
            raise ValueError(
                "Conversion parameter names cannot denote sensitive, identifier, URL, or DOM data"
            )
        return value

    @field_validator("allowed_values")
    @classmethod
    def unique_enum_values(
        cls, values: tuple[LowCardinalityEnumValue, ...]
    ) -> tuple[LowCardinalityEnumValue, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Allowed enum values must be unique")
        for value in values:
            components = frozenset(value.replace("-", "_").split("_"))
            if components & _FORBIDDEN_ENUM_VALUE_COMPONENTS:
                raise ValueError(
                    "Conversion parameter enum values cannot denote sensitive or identifier data"
                )
        return values

    @model_validator(mode="after")
    def finite_value_domain(self) -> ConversionParameter:
        if self.value_type == "BOOLEAN":
            if (
                self.integer_minimum is not None
                or self.integer_maximum is not None
                or self.allowed_values
            ):
                raise ValueError("Boolean parameters cannot declare integer bounds or enum values")
        elif self.value_type == "INTEGER":
            if (
                self.integer_minimum is None
                or self.integer_maximum is None
                or self.integer_minimum > self.integer_maximum
                or self.integer_maximum - self.integer_minimum > 100_000
                or self.allowed_values
            ):
                raise ValueError("Integer parameters require a bounded range and no enum values")
        elif (
            self.integer_minimum is not None
            or self.integer_maximum is not None
            or not self.allowed_values
        ):
            raise ValueError("Enum parameters require a finite allowed-value set only")
        return self


class ConversionContractInput(StrictModel):
    """The entire customer-declared definition used for one content digest."""

    schema_version: Literal[1] = 1
    contract_id: SafeRef
    version: int = Field(ge=1, le=1_000_000)
    supersedes_ref: Digest | None
    business_outcome: BusinessOutcome
    authoritative_source: AuthoritativeSource
    qualifying_occurrence: QualifyingOccurrence
    exclusions: tuple[Exclusion, ...] = Field(max_length=_MAX_EXCLUSIONS)
    counting_and_deduplication: CountingAndDeduplication
    value_and_currency: ValueAndCurrency
    allowed_parameter_schema: tuple[ConversionParameter, ...] = Field(max_length=_MAX_PARAMETERS)
    prohibited_parameters: tuple[ProhibitedParameterCategory, ...] = Field(
        min_length=len(_REQUIRED_PROHIBITED_PARAMETER_CATEGORIES),
        max_length=len(_REQUIRED_PROHIBITED_PARAMETER_CATEGORIES),
    )
    consent_contract_ref: SafeRef
    destination_profiles: tuple[SafeRef, ...] = Field(
        min_length=1, max_length=_MAX_DESTINATION_PROFILES
    )
    effective_from: datetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("effective_from")
    @classmethod
    def aware_utc_effective_from(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Conversion-contract effective time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def internally_consistent_customer_declaration(self) -> ConversionContractInput:
        if self.version == 1 and self.supersedes_ref is not None:
            raise ValueError("Version one conversion contracts cannot supersede another definition")
        if self.version > 1 and self.supersedes_ref is None:
            raise ValueError("Later conversion-contract versions require a prior content digest")
        exclusion_refs = tuple(exclusion.exclusion_ref for exclusion in self.exclusions)
        if len(exclusion_refs) != len(set(exclusion_refs)):
            raise ValueError("Conversion-contract exclusion references must be unique")
        parameter_names = tuple(parameter.name for parameter in self.allowed_parameter_schema)
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("Conversion-contract parameter names must be unique")
        if len(self.destination_profiles) != len(set(self.destination_profiles)):
            raise ValueError("Conversion-contract destination profiles must be unique")
        if set(self.prohibited_parameters) != _REQUIRED_PROHIBITED_PARAMETER_CATEGORIES:
            raise ValueError(
                "Conversion contracts must declare every required prohibited parameter category"
            )
        return self


class ConversionContract(StrictModel):
    """Immutable wrapper with no authority beyond recording a declaration."""

    schema_version: Literal[1] = 1
    definition: ConversionContractInput
    content_digest: Digest
    provenance: Literal["CUSTOMER_DECLARED"] = "CUSTOMER_DECLARED"
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution_authority must be the JSON boolean false")
        return value

    @model_validator(mode="after")
    def digest_matches_exact_canonical_definition(self) -> ConversionContract:
        if self.content_digest != digest(self.definition):
            raise ValueError("Conversion-contract digest must match its exact canonical definition")
        return self


def compile_conversion_contract(contract_input: ConversionContractInput) -> ConversionContract:
    """Revalidate and content-address one customer declaration without side effects."""

    # ``model_copy`` and ``model_construct`` bypass Pydantic validators. Rebuild
    # every nested value before calculating a durable content digest.
    definition = ConversionContractInput.model_validate(contract_input.model_dump(mode="python"))
    return ConversionContract(definition=definition, content_digest=digest(definition))


__all__ = [
    "AuthoritativeSource",
    "BoundedInteger",
    "BusinessOutcome",
    "ConversionContract",
    "ConversionContractInput",
    "ConversionParameter",
    "CountingAndDeduplication",
    "CustomerDeclaredStatement",
    "Digest",
    "Exclusion",
    "LowCardinalityEnumValue",
    "ParameterName",
    "ProhibitedParameterCategory",
    "QualifyingOccurrence",
    "ValueAndCurrency",
    "compile_conversion_contract",
]

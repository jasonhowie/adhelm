"""Offline-only compiler for one fixed GA4 Admin key-event create operation."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement import EventContract
from adhelm.models import Currency, SafeRef, StrictModel
from adhelm.privacy import validate_data

_EDIT_SCOPE: Literal["https://www.googleapis.com/auth/analytics.edit"] = (
    "https://www.googleapis.com/auth/analytics.edit"
)
_PERSON_EVENT_TERMS = frozenset(
    {
        "email",
        "phone",
        "user",
        "client",
        "patient",
        "member",
        "account",
        "eligibility",
        "underwriting",
        "health",
        "financial",
        "gclid",
        "fbclid",
        # These terms identify prohibited raw values even when a caller removes
        # an underscore from a generic EventField's exact-name denylist.
        "ssn",
        "password",
        "token",
        "cookie",
        "secret",
        "contactid",
        "leadid",
        "customerid",
        "ttclid",
        "msclkid",
        "formvalue",
        "formvalues",
        "domtext",
        "pagetext",
        "url",
    }
)
_GA4_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_MAX_GA4_EVENT_PARAMETERS = 25

ReferenceFor = Callable[[Literal["property", "web_stream"], str], str]


def _default_reference_for(kind: Literal["property", "web_stream"], value: str) -> str:
    return f"ga4_admin.{kind}.{value}"


class Ga4AdminKeyEventIdentity(StrictModel):
    """Exact customer-declared GA4 property and Web stream for a later live write."""

    property_ref: SafeRef
    property_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    web_stream_ref: SafeRef
    web_stream_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    api_version: Literal["v1alpha"] = "v1alpha"
    required_scope: Literal["https://www.googleapis.com/auth/analytics.edit"] = _EDIT_SCOPE


class Ga4AdminKeyEventDefaultValueInput(StrictModel):
    """A decimal supplied locally; compilation emits it only as an exact API double."""

    numeric_value: Decimal
    currency_code: Currency

    @field_validator("numeric_value")
    @classmethod
    def must_be_finite_exact_binary64(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or len(value.as_tuple().digits) > 100:
            raise ValueError("GA4 key-event default value is unsupported")
        if value.is_zero() and value.is_signed():
            raise ValueError("GA4 key-event default value cannot be signed zero")
        try:
            encoded = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                "GA4 key-event default value cannot be represented by the API"
            ) from exc
        if not math.isfinite(encoded) or Decimal.from_float(encoded) != value:
            raise ValueError("GA4 key-event default value must be exactly representable by the API")
        # Retain the single exact binary64 value, not a cosmetic Decimal scale
        # that RFC 8785 will erase from the request body but retain in an input
        # digest.  This makes equivalent local candidates content-addressed alike.
        return Decimal.from_float(encoded)


class Ga4AdminKeyEventCreateInput(StrictModel):
    event_contract: EventContract
    identity: Ga4AdminKeyEventIdentity
    counting_method: Literal["ONCE_PER_EVENT", "ONCE_PER_SESSION"]
    default_value: Ga4AdminKeyEventDefaultValueInput | None = None

    @model_validator(mode="after")
    def event_is_not_a_person_level_surface(self) -> Ga4AdminKeyEventCreateInput:
        if _GA4_EVENT_NAME.fullmatch(self.event_contract.event_name) is None:
            raise ValueError("GA4 key-event names must be 40 characters or fewer")
        if len(self.event_contract.fields) > _MAX_GA4_EVENT_PARAMETERS:
            raise ValueError("GA4 key-event contracts can declare at most 25 event parameters")
        if any(
            _GA4_EVENT_NAME.fullmatch(field.name) is None for field in self.event_contract.fields
        ):
            raise ValueError("GA4 key-event parameter names must be 40 characters or fewer")
        values_to_screen = (self.event_contract.event_name,) + tuple(
            field.name for field in self.event_contract.fields
        )
        if any(
            sensitive_term in value
            for value in values_to_screen
            for sensitive_term in _PERSON_EVENT_TERMS
        ):
            raise ValueError(
                "GA4 key-event contracts cannot denote person-level or sensitive events"
            )
        return self


class Ga4AdminKeyEventCreateTarget(StrictModel):
    host: Literal["analyticsadmin.googleapis.com"] = "analyticsadmin.googleapis.com"
    api_version: Literal["v1alpha"] = "v1alpha"
    method: Literal["POST"] = "POST"
    parent: str = Field(pattern=r"^properties/[0-9]{1,20}$")
    path: str = Field(pattern=r"^/v1alpha/properties/[0-9]{1,20}/keyEvents$")


class Ga4AdminKeyEventDefaultValueWire(StrictModel):
    numericValue: float
    currencyCode: Currency

    @field_validator("numericValue")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("GA4 key-event API value must be finite")
        return value


class Ga4AdminKeyEventCreateRequest(StrictModel):
    """The create body excludes output-only fields and every generic parameter surface."""

    eventName: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    countingMethod: Literal["ONCE_PER_EVENT", "ONCE_PER_SESSION"]
    defaultValue: Ga4AdminKeyEventDefaultValueWire | None = None


class Ga4AdminKeyEventProspectiveEffects(StrictModel):
    configuration_surface: Literal["LIVE_CONFIGURATION_WRITE"] = "LIVE_CONFIGURATION_WRITE"
    applies_to: Literal["FUTURE_EVENT_OCCURRENCES"] = "FUTURE_EVENT_OCCURRENCES"
    historical_backfill: Literal[False] = False
    native_conversion_mapping: Literal["NONE_CREATED"] = "NONE_CREATED"
    browser_or_receipt_proven: Literal[False] = False


class Ga4AdminKeyEventPlanningBoundary(StrictModel):
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    requires_verified_live_property_and_stream_identity: Literal[True] = True
    requires_verified_event_collection_and_reserved_name_check: Literal[True] = True
    requires_existing_key_event_inventory: Literal[True] = True
    requires_immutable_event_name_and_duplicate_check: Literal[True] = True
    requires_pre_apply_drift_recheck: Literal[True] = True
    requires_semantics_privacy_and_consent_validation: Literal[True] = True
    requires_exact_authorization: Literal[True] = True
    requires_configuration_readback: Literal[True] = True
    requires_browser_network_verification: Literal[True] = True
    requires_realtime_verification: Literal[True] = True
    requires_processed_report_verification: Literal[True] = True
    requires_separate_native_conversion_mapping: Literal[True] = True


class Ga4AdminKeyEventCreateCandidate(StrictModel):
    """Immutable local candidate, deliberately without a transport or approval capability."""

    schema_version: Literal[1] = 1
    provider: Literal["ga4_admin"] = "ga4_admin"
    capability_version: Literal["ga4-admin-key-event-create-contract-2026-09-04"] = (
        "ga4-admin-key-event-create-contract-2026-09-04"
    )
    event_contract_ref: SafeRef
    semantics_ref: SafeRef
    identity: Ga4AdminKeyEventIdentity
    target: Ga4AdminKeyEventCreateTarget
    request: Ga4AdminKeyEventCreateRequest
    declared_field_names: tuple[str, ...] = Field(max_length=_MAX_GA4_EVENT_PARAMETERS)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    prospective_effects: Ga4AdminKeyEventProspectiveEffects
    planning_boundary: Ga4AdminKeyEventPlanningBoundary

    @property
    def candidate_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def compile_ga4_admin_key_event_create(
    candidate_input: Ga4AdminKeyEventCreateInput,
    *,
    reference_for: ReferenceFor = _default_reference_for,
) -> Ga4AdminKeyEventCreateCandidate:
    """Compile a fixed, non-executable GA4 key-event create candidate offline."""

    # ``model_copy`` and ``model_construct`` can bypass Pydantic validators.
    # This public boundary must rebuild the complete candidate input before it
    # creates a digest or names a future remote property.
    candidate_input = Ga4AdminKeyEventCreateInput.model_validate(
        candidate_input.model_dump(mode="python")
    )
    # The compiler retains only references and a digest of these semantics; it
    # must still decline person-level source text before making that digest.
    validate_data(candidate_input.event_contract.model_dump(mode="python"))
    _assert_identity_binding(candidate_input.identity, reference_for=reference_for)

    wire_default = None
    if candidate_input.default_value is not None:
        wire_default = Ga4AdminKeyEventDefaultValueWire(
            numericValue=float(candidate_input.default_value.numeric_value),
            currencyCode=candidate_input.default_value.currency_code,
        )
    identity = candidate_input.identity
    return Ga4AdminKeyEventCreateCandidate(
        event_contract_ref=candidate_input.event_contract.event_contract_ref,
        semantics_ref=candidate_input.event_contract.semantics.semantics_ref,
        identity=identity,
        target=Ga4AdminKeyEventCreateTarget(
            parent=f"properties/{identity.property_client_ref}",
            path=f"/v1alpha/properties/{identity.property_client_ref}/keyEvents",
        ),
        request=Ga4AdminKeyEventCreateRequest(
            eventName=candidate_input.event_contract.event_name,
            countingMethod=candidate_input.counting_method,
            defaultValue=wire_default,
        ),
        declared_field_names=tuple(field.name for field in candidate_input.event_contract.fields),
        input_digest=digest(candidate_input),
        prospective_effects=Ga4AdminKeyEventProspectiveEffects(),
        planning_boundary=Ga4AdminKeyEventPlanningBoundary(),
    )


def _assert_identity_binding(
    identity: Ga4AdminKeyEventIdentity, *, reference_for: ReferenceFor
) -> None:
    try:
        property_ref = reference_for("property", identity.property_client_ref)
        web_stream_ref = reference_for("web_stream", identity.web_stream_client_ref)
    except Exception as exc:
        raise AdhelmError(
            "IDENTITY_MISMATCH",
            "GA4 Admin identity references cannot be bound to their native IDs.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from exc
    if identity.property_ref != property_ref or identity.web_stream_ref != web_stream_ref:
        raise AdhelmError(
            "IDENTITY_MISMATCH",
            "GA4 Admin identity references differ from their configured native IDs.",
            exit_code=ExitCode.INVALID_INPUT,
        )

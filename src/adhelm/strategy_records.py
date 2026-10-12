"""Closed, non-executable multi-provider strategy compilation observations.

The source strategy brief and native specifications deliberately do not enter
this record.  Each child instead uses the already-reviewed, normalized
candidate-compilation record shape.  A strategy observation therefore proves
only that every selected offline compiler completed together at one local
observation point; it cannot be replayed as a plan or authorization.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.models import Money, StrictModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_MONEY_TUPLE_ADAPTER: TypeAdapter[tuple[Money, ...]] = TypeAdapter(tuple[Money, ...])


def _reparse_money_instances(value: object) -> object:
    """Reject constructed nested money values before a record reaches history."""
    if not isinstance(value, (tuple, list)):
        return value
    try:
        raw = json.dumps(
            [
                item.model_dump(mode="json", warnings="error")
                if isinstance(item, BaseModel)
                else item
                for item in value
            ],
            allow_nan=False,
        )
        parsed = parse_json(raw)
        canonical_bytes(parsed)
        return _MONEY_TUPLE_ADAPTER.validate_json(raw)
    except Exception:
        raise ValueError("Strategy money values violate their closed JSON contract") from None


def _reparse_child_record(value: object) -> CandidateCompilationRecord:
    """Take every nested candidate record through its hardened JSON boundary."""
    try:
        if isinstance(value, BaseModel):
            raw = value.model_dump_json(warnings="error")
        elif isinstance(value, dict):
            # Preserve original scalar types for strict validation.  RFC 8785
            # would otherwise turn a JSON ``1.0`` into ``1``.
            canonical_bytes(value)
            raw = json.dumps(value, allow_nan=False)
        else:
            raise TypeError("child compilation must be a record object")
        parsed = parse_json(raw)
        canonical_bytes(parsed)
        return CandidateCompilationRecord.model_validate_json(raw)
    except Exception:
        raise ValueError("Strategy child compilation violates its closed JSON contract") from None


class StrategyCandidateCompilationRecord(StrictModel):
    """One atomic observation of one through six normalized candidates.

    The two strategy digests identify the original closed source and internal
    aggregate without persisting either.  Child compilation records preserve
    exact reviewed spending units and every provider-specific unmet boundary.
    """

    schema_version: Literal[1] = 1
    event_type: Literal["STRATEGY_CANDIDATE_COMPILED"] = "STRATEGY_CANDIDATE_COMPILED"
    strategy_brief_digest: Digest
    input_digest: Digest
    strategy_spec_digest: Digest
    expires_at: datetime
    total_caps_by_currency: tuple[Money, ...] = Field(min_length=1, max_length=6)
    maximum_spend_by_currency: tuple[Money, ...] = Field(min_length=1, max_length=6)
    child_compilations: tuple[CandidateCompilationRecord, ...] = Field(min_length=1, max_length=6)
    ordered_child_candidate_digests: tuple[Digest, ...] = Field(min_length=1, max_length=6)
    ordered_child_spec_digests: tuple[Digest, ...] = Field(min_length=1, max_length=6)
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = (
        "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    )
    fixture_only_status: Literal["FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    unmet_live_proof: Literal["ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET"] = (
        "ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET"
    )
    requires_new_compilation_for_planning: Literal[True] = True
    activation_requires_new_digest_and_authorization: Literal[True] = True
    remote_side_effects: Literal[False] = False
    authorization_status: Literal["NO_AUTHORITY_GRANTED"] = "NO_AUTHORITY_GRANTED"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("strategy record schema_version must be the integer 1")
        return value

    @field_validator(
        "requires_new_compilation_for_planning",
        "activation_requires_new_digest_and_authorization",
        "remote_side_effects",
        mode="before",
    )
    @classmethod
    def exact_control_boolean(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("Strategy control flags require JSON booleans")
        return value

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("strategy record expiry must be timezone-aware UTC")
        return value

    @field_validator("total_caps_by_currency", "maximum_spend_by_currency", mode="before")
    @classmethod
    def reparse_money_values(cls, value: object) -> object:
        return _reparse_money_instances(value)

    @field_validator("child_compilations", mode="before")
    @classmethod
    def reparse_child_compilations(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        return tuple(_reparse_child_record(item) for item in value)

    @model_validator(mode="after")
    def exact_children_caps_and_boundaries(self) -> StrategyCandidateCompilationRecord:
        children = self.child_compilations
        providers = tuple(child.compilation.provider for child in children)
        candidate_digests = tuple(child.candidate_digest for child in children)
        spec_digests = tuple(child.compilation.spec_digest for child in children)
        if (
            len(set(providers)) != len(providers)
            or self.ordered_child_candidate_digests != candidate_digests
            or self.ordered_child_spec_digests != spec_digests
        ):
            raise ValueError("strategy children or ordered digests are invalid")
        if any(
            child.compilation.execution_status != self.execution_status
            or not child.compilation.requires_new_compilation_for_planning
            or not child.compilation.activation_requires_new_digest_and_authorization
            or child.compilation.remote_side_effects
            for child in children
        ):
            raise ValueError("strategy children must preserve every offline planning boundary")

        caps = {item.currency: item.minor_units for item in self.total_caps_by_currency}
        if len(caps) != len(self.total_caps_by_currency) or any(
            amount < 1 for amount in caps.values()
        ):
            raise ValueError("strategy currency caps must be positive and unique")
        totals: dict[str, int] = {}
        for child in children:
            spend = child.compilation.maximum_spend
            totals[spend.currency] = totals.get(spend.currency, 0) + spend.minor_units
        expected_totals = tuple(
            Money(currency=currency, minor_units=totals[currency]) for currency in sorted(totals)
        )
        if (
            self.maximum_spend_by_currency != expected_totals
            or set(caps) != set(totals)
            or any(totals[currency] > caps[currency] for currency in totals)
        ):
            raise ValueError("strategy currency caps or exact maximum spends are invalid")
        return self


def validate_strategy_candidate_record(value: object) -> StrategyCandidateCompilationRecord:
    """Detach a possibly constructed record through its strict JSON contract."""
    if isinstance(value, BaseModel):
        raw = value.model_dump_json(warnings="error")
        return StrategyCandidateCompilationRecord.model_validate_json(raw)
    if isinstance(value, dict):
        # Keep the noncanonical source JSON for strict scalar validation.
        canonical_bytes(value)
        raw = json.dumps(value, allow_nan=False)
        return StrategyCandidateCompilationRecord.model_validate_json(raw)
    return StrategyCandidateCompilationRecord.model_validate(cast(Any, value))

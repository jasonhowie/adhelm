"""Closed GA4 key-event candidate reviews bound to a retained conversion definition.

This module composes the fixed offline GA4 Admin compiler with a deliberately
narrow structural link to a customer-declared conversion contract.  It never
calls GA4, reads credentials, evaluates a source event, or grants authority to
create a key event.  The retained shape contains the compiler candidate needed
for exact historical validation, but deliberately excludes the source
``EventContract`` and every customer statement.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.conversion_contracts import ConversionContract, ParameterName
from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateCandidate,
    Ga4AdminKeyEventCreateInput,
    compile_ga4_admin_key_event_create,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _invalid_candidate(message: str = "GA4 key-event candidate data is invalid.") -> AdhelmError:
    return AdhelmError("INVALID_GA4_KEY_EVENT_CANDIDATE", message, exit_code=ExitCode.INVALID_INPUT)


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be an exact digest")
    return value


def _require_exact_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _require_exact_int(value: object, *, label: str, expected: int | None = None) -> int:
    if type(value) is not int or (expected is not None and value != expected):
        raise ValueError(f"{label} has an invalid integer type")
    return value


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _require_list(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array")
    return cast(list[object], value)


def _optional(data: Mapping[str, object], name: str) -> object | None:
    return data.get(name) if name in data else None


def _assert_json_shaped(value: object) -> None:
    """Reject Python-only values before JSON serialization can coerce them."""
    pending = [value]
    while pending:
        current = pending.pop()
        if current is None or type(current) in {str, bool, int}:
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError("JSON numbers must be finite")
            continue
        if type(current) in {list, tuple}:
            pending.extend(cast(tuple[object, ...] | list[object], current))
            continue
        if type(current) is dict:
            mapping = cast(dict[object, object], current)
            if any(type(key) is not str for key in mapping):
                raise ValueError("JSON object keys must be strings")
            pending.extend(mapping.values())
            continue
        raise TypeError("candidate values must be JSON-shaped")


def _json_object(value: object, *, label: str) -> tuple[str, dict[str, object]]:
    """Detach a model or a raw JSON-shaped dict without normalizing scalar types."""
    try:
        if isinstance(value, BaseModel):
            raw = value.model_dump_json(warnings="error")
        elif type(value) is dict:
            _assert_json_shaped(value)
            raw = json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
        else:
            raise TypeError(f"{label} must be a model or JSON object")
        parsed = parse_json(raw)
        data = _require_mapping(parsed, label=label)
        canonical_bytes(data)
        return raw, data
    except (AdhelmError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{label} violates its closed JSON contract") from exc


def _expect_string_if_present(data: Mapping[str, object], name: str, *, label: str) -> None:
    value = _optional(data, name)
    if value is not None and type(value) is not str:
        raise ValueError(f"{label}.{name} must be a string")


def _validate_key_event_input_raw_types(data: Mapping[str, object]) -> None:
    if "schema_version" in data:
        _require_exact_int(
            data["schema_version"], label="candidate input schema_version", expected=1
        )
    _expect_string_if_present(data, "conversion_contract_record_digest", label="candidate input")
    _expect_string_if_present(data, "declared_destination_profile_ref", label="candidate input")
    key_event = _require_mapping(data.get("key_event"), label="candidate input key_event")
    _expect_string_if_present(key_event, "counting_method", label="key_event")
    identity = _require_mapping(key_event.get("identity"), label="key_event identity")
    for name in (
        "property_ref",
        "property_client_ref",
        "web_stream_ref",
        "web_stream_client_ref",
        "api_version",
        "required_scope",
    ):
        _expect_string_if_present(identity, name, label="key_event identity")
    event_contract = _require_mapping(key_event.get("event_contract"), label="event contract")
    for name in ("event_contract_ref", "event_name"):
        _expect_string_if_present(event_contract, name, label="event contract")
    semantics = _require_mapping(event_contract.get("semantics"), label="event semantics")
    for name in ("semantics_ref", "customer_definition", "counting_rule"):
        _expect_string_if_present(semantics, name, label="event semantics")
    fields = _optional(event_contract, "fields")
    if fields is not None:
        for field in _require_list(fields, label="event fields"):
            item = _require_mapping(field, label="event field")
            _expect_string_if_present(item, "name", label="event field")
            _expect_string_if_present(item, "value_type", label="event field")
            if "required" in item:
                _require_exact_bool(item["required"], label="event field required")
    default = _optional(key_event, "default_value")
    if default is not None:
        item = _require_mapping(default, label="key-event default value")
        _expect_string_if_present(item, "currency_code", label="key-event default value")
        if "numeric_value" in item and type(item["numeric_value"]) not in {int, float}:
            raise ValueError("key-event default value must use a JSON number")


def _validate_compiler_candidate_raw_types(data: Mapping[str, object]) -> None:
    _require_exact_int(
        data.get("schema_version"), label="compiler candidate schema_version", expected=1
    )
    for name in (
        "provider",
        "capability_version",
        "event_contract_ref",
        "semantics_ref",
        "input_digest",
    ):
        _expect_string_if_present(data, name, label="compiler candidate")
    identity = _require_mapping(data.get("identity"), label="compiler candidate identity")
    for name in (
        "property_ref",
        "property_client_ref",
        "web_stream_ref",
        "web_stream_client_ref",
        "api_version",
        "required_scope",
    ):
        _expect_string_if_present(identity, name, label="compiler candidate identity")
    target = _require_mapping(data.get("target"), label="compiler candidate target")
    for name in ("host", "api_version", "method", "parent", "path"):
        _expect_string_if_present(target, name, label="compiler candidate target")
    request = _require_mapping(data.get("request"), label="compiler candidate request")
    for name in ("eventName", "countingMethod"):
        _expect_string_if_present(request, name, label="compiler candidate request")
    default = _optional(request, "defaultValue")
    if default is not None:
        value = _require_mapping(default, label="compiler candidate default value")
        _expect_string_if_present(value, "currencyCode", label="compiler candidate default value")
        if "numericValue" in value and type(value["numericValue"]) not in {int, float}:
            raise ValueError("compiler candidate numeric value must be a JSON number")
    for field_name in _require_list(
        data.get("declared_field_names"), label="compiler candidate fields"
    ):
        if type(field_name) is not str:
            raise ValueError("compiler candidate field names must be strings")
    effects = _require_mapping(data.get("prospective_effects"), label="compiler candidate effects")
    for name in (
        "configuration_surface",
        "applies_to",
        "native_conversion_mapping",
    ):
        _expect_string_if_present(effects, name, label="compiler candidate effects")
    for name in ("historical_backfill", "browser_or_receipt_proven"):
        if name in effects:
            _require_exact_bool(effects[name], label=f"compiler candidate effects {name}")
    boundary = _require_mapping(data.get("planning_boundary"), label="compiler candidate boundary")
    _expect_string_if_present(boundary, "status", label="compiler candidate boundary")
    for boundary_name, boundary_value in boundary.items():
        if boundary_name.startswith("requires_"):
            _require_exact_bool(
                boundary_value, label=f"compiler candidate boundary {boundary_name}"
            )


def _reparse_key_event_input(value: object) -> Ga4AdminKeyEventCreateInput:
    raw, data = _json_object(value, label="GA4 key-event input")
    _validate_key_event_input_raw_types({"key_event": data})
    try:
        return Ga4AdminKeyEventCreateInput.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("GA4 key-event input violates its closed schema") from exc


def _reparse_compiler_candidate(value: object) -> Ga4AdminKeyEventCreateCandidate:
    raw, data = _json_object(value, label="compiler candidate")
    _validate_compiler_candidate_raw_types(data)
    try:
        candidate = Ga4AdminKeyEventCreateCandidate.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("compiler candidate violates its closed schema") from exc
    _validate_compiler_candidate_invariants(candidate)
    return candidate


class Ga4KeyEventBooleanFieldBinding(StrictModel):
    """The non-sensitive structural part of one original EventContract field."""

    name: ParameterName
    event_value_type: Literal["BOOLEAN"] = "BOOLEAN"
    event_required: bool

    @field_validator("event_required", mode="before")
    @classmethod
    def exact_event_required(cls, value: object) -> bool:
        return _require_exact_bool(value, label="event_required")


def _reparse_bound_boolean_field(value: object) -> Ga4KeyEventBooleanFieldBinding:
    raw, data = _json_object(value, label="bound Boolean field")
    _expect_string_if_present(data, "name", label="bound Boolean field")
    _expect_string_if_present(data, "event_value_type", label="bound Boolean field")
    if "event_required" in data:
        _require_exact_bool(data["event_required"], label="bound Boolean event_required")
    try:
        return Ga4KeyEventBooleanFieldBinding.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("bound Boolean field violates its closed schema") from exc


class Ga4KeyEventConversionBinding(StrictModel):
    """Exact structural link to a prior customer-declared definition.

    The timestamp is retained only as a declaration from the contract.  This
    candidate makes no assertion that it is currently effective or scheduled.
    """

    schema_version: Literal[1] = 1
    conversion_contract_record_digest: Digest
    contract_id: SafeRef
    content_digest: Digest
    contract_version: int = Field(ge=1, le=1_000_000)
    contract_effective_from: datetime
    declared_destination_profile_ref: SafeRef
    consent_contract_ref: SafeRef
    bound_boolean_fields: tuple[Ga4KeyEventBooleanFieldBinding, ...] = Field(max_length=25)
    counting_binding: Literal["EACH_QUALIFYING_OCCURRENCE_TO_ONCE_PER_EVENT"] = (
        "EACH_QUALIFYING_OCCURRENCE_TO_ONCE_PER_EVENT"
    )
    value_binding: Literal["NO_MONETARY_VALUE_TO_NO_DEFAULT_VALUE"] = (
        "NO_MONETARY_VALUE_TO_NO_DEFAULT_VALUE"
    )
    semantic_accuracy: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    source_event_mapping: Literal["NOT_PROVEN"] = "NOT_PROVEN"
    effective_time_status: Literal["DECLARED_NOT_EVALUATED"] = "DECLARED_NOT_EVALUATED"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _require_exact_int(value, label="conversion binding schema_version", expected=1)

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_contract_version(cls, value: object) -> int:
        return _require_exact_int(value, label="contract_version")

    @field_validator("contract_effective_from")
    @classmethod
    def utc_effective_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("conversion effective time must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("bound_boolean_fields", mode="before")
    @classmethod
    def rebuild_boolean_fields(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        return tuple(_reparse_bound_boolean_field(item) for item in value)

    @model_validator(mode="after")
    def unique_bound_fields(self) -> Ga4KeyEventConversionBinding:
        names = tuple(field.name for field in self.bound_boolean_fields)
        if len(names) != len(set(names)):
            raise ValueError("bound Boolean field names must be unique")
        return self


def _reparse_conversion_binding(value: object) -> Ga4KeyEventConversionBinding:
    raw, data = _json_object(value, label="conversion binding")
    _require_exact_int(
        data.get("schema_version"), label="conversion binding schema_version", expected=1
    )
    for name in (
        "conversion_contract_record_digest",
        "contract_id",
        "content_digest",
        "contract_effective_from",
        "declared_destination_profile_ref",
        "consent_contract_ref",
        "counting_binding",
        "value_binding",
        "semantic_accuracy",
        "source_event_mapping",
        "effective_time_status",
    ):
        _expect_string_if_present(data, name, label="conversion binding")
    _require_exact_int(data.get("contract_version"), label="conversion binding contract_version")
    fields = _require_list(data.get("bound_boolean_fields"), label="conversion binding fields")
    for field in fields:
        item = _require_mapping(field, label="conversion binding field")
        _expect_string_if_present(item, "name", label="conversion binding field")
        _expect_string_if_present(item, "event_value_type", label="conversion binding field")
        if "event_required" in item:
            _require_exact_bool(item["event_required"], label="conversion binding field required")
    try:
        return Ga4KeyEventConversionBinding.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("conversion binding violates its closed schema") from exc


class Ga4KeyEventCandidateCompilation(StrictModel):
    """Private exact compiler result plus public uncertainty and no-authority facts."""

    schema_version: Literal[1] = 1
    provider: Literal["ga4_admin"] = "ga4_admin"
    compiler_candidate: Ga4AdminKeyEventCreateCandidate
    compiler_candidate_digest: Digest
    conversion_binding: Ga4KeyEventConversionBinding
    compiler_evidence: Literal["FIXTURE_VERIFIED_LIVE_UNVERIFIED"] = (
        "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    )
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = (
        "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    )
    semantic_accuracy: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    source_event_mapping: Literal["NOT_PROVEN"] = "NOT_PROVEN"
    effective_time_status: Literal["DECLARED_NOT_EVALUATED"] = "DECLARED_NOT_EVALUATED"
    requires_new_compilation_for_planning: Literal[True] = True
    requires_exact_authorization: Literal[True] = True
    remote_side_effects: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _require_exact_int(value, label="candidate compilation schema_version", expected=1)

    @field_validator(
        "requires_new_compilation_for_planning",
        "requires_exact_authorization",
        "remote_side_effects",
        "execution_authority",
        mode="before",
    )
    @classmethod
    def exact_control_booleans(cls, value: object) -> bool:
        return _require_exact_bool(value, label="candidate compilation control flag")

    @field_validator("compiler_candidate", mode="before")
    @classmethod
    def rebuild_compiler_candidate(cls, value: object) -> Ga4AdminKeyEventCreateCandidate:
        return _reparse_compiler_candidate(value)

    @field_validator("conversion_binding", mode="before")
    @classmethod
    def rebuild_conversion_binding(cls, value: object) -> Ga4KeyEventConversionBinding:
        return _reparse_conversion_binding(value)

    @model_validator(mode="after")
    def exact_compiler_and_uncertainty_facts(self) -> Ga4KeyEventCandidateCompilation:
        if self.compiler_candidate_digest != self.compiler_candidate.candidate_digest:
            raise ValueError("compiler candidate digest does not match its exact candidate")
        if (
            self.execution_status != self.compiler_candidate.planning_boundary.status
            or self.semantic_accuracy != self.conversion_binding.semantic_accuracy
            or self.source_event_mapping != self.conversion_binding.source_event_mapping
            or self.effective_time_status != self.conversion_binding.effective_time_status
        ):
            raise ValueError("candidate uncertainty facts do not match the conversion binding")
        _validate_compiler_candidate_invariants(self.compiler_candidate)
        return self

    @property
    def candidate_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def _reparse_compilation(value: object) -> Ga4KeyEventCandidateCompilation:
    raw, data = _json_object(value, label="candidate compilation")
    _require_exact_int(
        data.get("schema_version"), label="candidate compilation schema_version", expected=1
    )
    for name in (
        "provider",
        "compiler_candidate_digest",
        "compiler_evidence",
        "execution_status",
        "semantic_accuracy",
        "source_event_mapping",
        "effective_time_status",
    ):
        _expect_string_if_present(data, name, label="candidate compilation")
    for name in (
        "requires_new_compilation_for_planning",
        "requires_exact_authorization",
        "remote_side_effects",
        "execution_authority",
    ):
        if name in data:
            _require_exact_bool(data[name], label=f"candidate compilation {name}")
    _validate_compiler_candidate_raw_types(
        _require_mapping(
            data.get("compiler_candidate"), label="candidate compilation compiler candidate"
        )
    )
    _reparse_conversion_binding(data.get("conversion_binding"))
    try:
        return Ga4KeyEventCandidateCompilation.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("candidate compilation violates its closed schema") from exc


class Ga4KeyEventCandidateRecord(StrictModel):
    """One non-executable GA4 key-event candidate linked to one exact definition."""

    schema_version: Literal[1] = 1
    event_type: Literal["GA4_KEY_EVENT_CANDIDATE_COMPILED"] = "GA4_KEY_EVENT_CANDIDATE_COMPILED"
    compilation: Ga4KeyEventCandidateCompilation
    candidate_digest: Digest

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _require_exact_int(value, label="candidate record schema_version", expected=1)

    @field_validator("compilation", mode="before")
    @classmethod
    def rebuild_compilation(cls, value: object) -> Ga4KeyEventCandidateCompilation:
        return _reparse_compilation(value)

    @model_validator(mode="after")
    def exact_compilation_digest(self) -> Ga4KeyEventCandidateRecord:
        if self.candidate_digest != self.compilation.candidate_digest:
            raise ValueError("candidate digest does not match its exact compilation")
        return self


def _validate_record_raw_types(data: Mapping[str, object]) -> None:
    _require_exact_int(
        data.get("schema_version"), label="candidate record schema_version", expected=1
    )
    _expect_string_if_present(data, "event_type", label="candidate record")
    _expect_string_if_present(data, "candidate_digest", label="candidate record")
    _reparse_compilation(data.get("compilation"))


def _validate_compiler_candidate_invariants(candidate: Ga4AdminKeyEventCreateCandidate) -> None:
    """Validate every compiler fact still available after source prose is discarded."""
    identity = candidate.identity
    if (
        identity.property_ref != f"ga4_admin.property.{identity.property_client_ref}"
        or identity.web_stream_ref != f"ga4_admin.web_stream.{identity.web_stream_client_ref}"
        or candidate.target.parent != f"properties/{identity.property_client_ref}"
        or candidate.target.path != f"/v1alpha/properties/{identity.property_client_ref}/keyEvents"
        or candidate.target.api_version != identity.api_version
        or candidate.request.defaultValue is not None
        or candidate.request.countingMethod != "ONCE_PER_EVENT"
        or len(candidate.declared_field_names) != len(set(candidate.declared_field_names))
    ):
        raise ValueError("compiler candidate does not retain the fixed GA4 key-event contract")
    # The source EventContract prose is intentionally absent from history, but
    # its name/field safety contract must not disappear with it. Rebuild only
    # those retained structural values through the public compiler input model
    # so a rehashed constructed candidate cannot introduce a person-level name.
    try:
        Ga4AdminKeyEventCreateInput(
            event_contract=EventContract(
                event_contract_ref=candidate.event_contract_ref,
                event_name=candidate.request.eventName,
                semantics=ConversionSemantics(
                    semantics_ref=candidate.semantics_ref,
                    customer_definition="Retained structural candidate validation.",
                    counting_rule="Validate the fixed candidate structure.",
                ),
                fields=tuple(
                    EventField(name=name, value_type="BOOLEAN", required=False)
                    for name in candidate.declared_field_names
                ),
            ),
            identity=identity,
            counting_method=candidate.request.countingMethod,
            default_value=None,
        )
    except ValidationError as exc:
        raise ValueError("compiler candidate violates the retained event safety contract") from exc


def revalidate_ga4_key_event_candidate_record(value: object) -> Ga4KeyEventCandidateRecord:
    """Detach a typed or raw candidate record through its strict original JSON shape."""
    try:
        raw, data = _json_object(value, label="GA4 key-event candidate record")
        _validate_record_raw_types(data)
        record = Ga4KeyEventCandidateRecord.model_validate_json(raw)
        # Retention writes only the complete canonical model shape.  Reject an
        # abbreviated or coercible mapping instead of silently filling defaults.
        if canonical_bytes(data) != canonical_bytes(record.model_dump(mode="json")):
            raise ValueError("candidate record is not canonical complete JSON")
        return record
    except (AdhelmError, TypeError, ValueError, ValidationError) as exc:
        raise _invalid_candidate() from exc


class Ga4KeyEventCandidateInput(StrictModel):
    """Unpersisted request that explicitly selects one retained conversion definition."""

    schema_version: Literal[1] = 1
    conversion_contract_record_digest: Digest
    declared_destination_profile_ref: SafeRef
    key_event: Ga4AdminKeyEventCreateInput

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _require_exact_int(value, label="candidate input schema_version", expected=1)

    @field_validator("key_event", mode="before")
    @classmethod
    def rebuild_key_event(cls, value: object) -> Ga4AdminKeyEventCreateInput:
        return _reparse_key_event_input(value)


def _revalidate_candidate_input(value: object) -> Ga4KeyEventCandidateInput:
    try:
        raw, data = _json_object(value, label="GA4 key-event candidate input")
        _validate_key_event_input_raw_types(data)
        return Ga4KeyEventCandidateInput.model_validate_json(raw)
    except (AdhelmError, TypeError, ValueError, ValidationError) as exc:
        raise _invalid_candidate("GA4 key-event candidate input is invalid.") from exc


def _revalidate_contract(value: object) -> ConversionContract:
    try:
        raw, data = _json_object(value, label="conversion contract")
        _require_exact_int(
            data.get("schema_version"), label="conversion contract schema_version", expected=1
        )
        definition = _require_mapping(data.get("definition"), label="conversion definition")
        _require_exact_int(
            definition.get("schema_version"),
            label="conversion definition schema_version",
            expected=1,
        )
        if "execution_authority" in data:
            _require_exact_bool(data["execution_authority"], label="conversion execution authority")
        parameters = _require_list(
            definition.get("allowed_parameter_schema"), label="conversion parameters"
        )
        for parameter in parameters:
            item = _require_mapping(parameter, label="conversion parameter")
            if "required" in item:
                _require_exact_bool(item["required"], label="conversion parameter required")
            for name in ("integer_minimum", "integer_maximum"):
                if name in item and item[name] is not None:
                    _require_exact_int(item[name], label=f"conversion parameter {name}")
        contract = ConversionContract.model_validate_json(raw)
        if canonical_bytes(data) != canonical_bytes(contract.model_dump(mode="json")):
            raise ValueError("conversion contract is not canonical complete JSON")
        return contract
    except (AdhelmError, TypeError, ValueError, ValidationError) as exc:
        raise _invalid_candidate("Referenced conversion contract is invalid.") from exc


def _build_binding(
    request: Ga4KeyEventCandidateInput,
    contract: ConversionContract,
    source_record_digest: str,
) -> Ga4KeyEventConversionBinding:
    event_fields = request.key_event.event_contract.fields
    fields = tuple(
        Ga4KeyEventBooleanFieldBinding(
            name=field.name,
            event_value_type=cast(Literal["BOOLEAN"], field.value_type),
            event_required=field.required,
        )
        for field in event_fields
    )
    return Ga4KeyEventConversionBinding(
        conversion_contract_record_digest=source_record_digest,
        contract_id=contract.definition.contract_id,
        content_digest=contract.content_digest,
        contract_version=contract.definition.version,
        contract_effective_from=contract.definition.effective_from,
        declared_destination_profile_ref=request.declared_destination_profile_ref,
        consent_contract_ref=contract.definition.consent_contract_ref,
        bound_boolean_fields=fields,
    )


def validate_ga4_key_event_candidate_binding(
    record: Ga4KeyEventCandidateRecord,
    contract: ConversionContract,
    source_record_digest: str,
) -> None:
    """Require the closed candidate to match one exact retained definition.

    The caller supplies the definition selected from verified history.  This
    pure helper intentionally does not load a ledger or decide whether this is
    the latest predecessor; the ledger calls it while holding its changes lock.
    """
    try:
        checked_record = revalidate_ga4_key_event_candidate_record(record)
        checked_contract = _revalidate_contract(contract)
        source_digest = _require_digest(source_record_digest, label="conversion contract record")
        binding = checked_record.compilation.conversion_binding
        definition = checked_contract.definition
        if (
            binding.conversion_contract_record_digest != source_digest
            or binding.contract_id != definition.contract_id
            or binding.content_digest != checked_contract.content_digest
            or binding.contract_version != definition.version
            or binding.contract_effective_from != definition.effective_from
            or binding.consent_contract_ref != definition.consent_contract_ref
            or binding.declared_destination_profile_ref not in definition.destination_profiles
        ):
            raise ValueError("candidate does not match the selected conversion definition")
        if (
            definition.counting_and_deduplication.counting_semantics != "EACH_QUALIFYING_OCCURRENCE"
            or definition.value_and_currency.value_semantics != "NO_MONETARY_VALUE"
        ):
            raise ValueError("conversion semantics are outside the GA4 structural binding")

        compiler = checked_record.compilation.compiler_candidate
        if (
            compiler.request.countingMethod != "ONCE_PER_EVENT"
            or compiler.request.defaultValue is not None
        ):
            raise ValueError("GA4 candidate does not preserve the narrow count/value mapping")

        parameters = {
            parameter.name: parameter for parameter in definition.allowed_parameter_schema
        }
        bound = checked_record.compilation.conversion_binding.bound_boolean_fields
        bound_names = tuple(field.name for field in bound)
        if compiler.declared_field_names != bound_names:
            raise ValueError("compiler field names do not match their structural bindings")
        for field in bound:
            parameter = parameters.get(field.name)
            if (
                parameter is None
                or parameter.value_type != "BOOLEAN"
                or field.event_value_type != "BOOLEAN"
                or parameter.required != field.event_required
            ):
                raise ValueError("event field cannot be bound to the declared Boolean parameter")
        if any(
            parameter.required and parameter.value_type != "BOOLEAN"
            for parameter in definition.allowed_parameter_schema
        ) or any(
            parameter.required and parameter.name not in set(bound_names)
            for parameter in definition.allowed_parameter_schema
            if parameter.value_type == "BOOLEAN"
        ):
            raise ValueError("required conversion parameters are not fully bound by the event")
        _validate_compiler_candidate_invariants(compiler)
    except AdhelmError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise AdhelmError(
            "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT",
            "Candidate compilation does not match the retained conversion definition.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Compile against the latest retained definition using the supported GA4 "
            "Boolean, no-value, each-occurrence binding.",
        ) from exc


def compile_ga4_key_event_candidate(
    request: Ga4KeyEventCandidateInput,
    *,
    retained_contract: ConversionContract,
    retained_contract_record_digest: str,
) -> Ga4KeyEventCandidateRecord:
    """Compile one offline GA4 candidate and bind it to one exact declaration."""
    try:
        checked_request = _revalidate_candidate_input(request)
        checked_contract = _revalidate_contract(retained_contract)
        source_digest = _require_digest(
            retained_contract_record_digest, label="conversion contract record"
        )
        if checked_request.conversion_contract_record_digest != source_digest:
            raise ValueError("input contract record selection does not match the retained contract")
        compiler_candidate = compile_ga4_admin_key_event_create(checked_request.key_event)
        binding = _build_binding(checked_request, checked_contract, source_digest)
        compilation = Ga4KeyEventCandidateCompilation(
            compiler_candidate=compiler_candidate,
            compiler_candidate_digest=compiler_candidate.candidate_digest,
            conversion_binding=binding,
        )
        record = Ga4KeyEventCandidateRecord(
            compilation=compilation, candidate_digest=compilation.candidate_digest
        )
        validate_ga4_key_event_candidate_binding(record, checked_contract, source_digest)
        return revalidate_ga4_key_event_candidate_record(record)
    except AdhelmError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise _invalid_candidate("GA4 key-event candidate compilation is invalid.") from exc


__all__ = [
    "Ga4KeyEventBooleanFieldBinding",
    "Ga4KeyEventCandidateCompilation",
    "Ga4KeyEventCandidateInput",
    "Ga4KeyEventCandidateRecord",
    "Ga4KeyEventConversionBinding",
    "compile_ga4_key_event_candidate",
    "revalidate_ga4_key_event_candidate_record",
    "validate_ga4_key_event_candidate_binding",
]

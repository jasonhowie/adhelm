"""Pure duplicate preconditions for one freshly compiled GA4 key-event create.

This module deliberately works only on a caller-supplied, normalized Admin
inventory.  It neither reads GA4 nor treats an inventory as current provider
state, permission, capacity, consent, a plan, or execution authority.
"""

from __future__ import annotations

import hmac
import re
from typing import Literal, cast

from pydantic import BaseModel, Field, ValidationError, field_validator

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import StrictModel
from adhelm.providers.ga4_admin import (
    Ga4AdminInventory,
    _fingerprint,
    _fingerprint_inventory_content_marker,
    _fingerprint_key_marker,
)
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventIdentity,
    compile_ga4_admin_key_event_create,
)

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_NUMERIC_ID = re.compile(r"^[0-9]+$")
_DECIMAL = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_SIGNED_INT64 = re.compile(r"^(?:0|-[1-9][0-9]{0,18}|[1-9][0-9]{0,18})$")
_MAX_MODEL_JSON_BYTES = 8_388_608

_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_FINGERPRINT_SCHEME = "HMAC_SHA256_LOCAL_V1"


class Ga4AdminKeyEventInventoryPreconditions(StrictModel):
    """Redaction-safe facts from one supplied-inventory duplicate check.

    The result intentionally contains neither the candidate nor an inventory,
    event name, native identifier, reference, or request.  Its digest lets a
    caller independently compare a fresh compilation without granting any
    authority to use it.
    """

    schema_version: Literal[1] = 1
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_scope: Literal["COMPLETE_SUPPLIED_INVENTORY"] = "COMPLETE_SUPPLIED_INVENTORY"
    fingerprint_key_continuity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"
    )
    inventory_content_integrity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    )
    duplicate_status: Literal["NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY"] = (
        "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY"
    )
    current_provider_state: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    capacity_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    permission_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    remote_side_effects: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("candidate_digest", mode="before")
    @classmethod
    def exact_candidate_digest(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("candidate_digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("remote_side_effects", "execution_authority", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("precondition control flags must be false")
        return value


def evaluate_ga4_key_event_create_preconditions(
    create_input: Ga4AdminKeyEventCreateInput,
    supplied_inventory: Ga4AdminInventory,
    *,
    fingerprint_key: bytes,
) -> Ga4AdminKeyEventInventoryPreconditions:
    """Check one fresh candidate against one complete supplied inventory.

    ``fingerprint_key`` is transient caller-owned key material.  The function
    first proves the inventory's marker was made with this exact key, then
    compares only HMAC event-name fingerprints.  It has no transport,
    credential, ledger, file, provider, or execution surface.
    """

    _require_fingerprint_key(fingerprint_key)
    checked_input = _revalidate_create_input(create_input)
    checked_inventory = _revalidate_inventory(supplied_inventory)
    try:
        candidate = compile_ga4_admin_key_event_create(checked_input)
    except AdhelmError:
        # The compiler's established privacy diagnostics are already sanitized.
        raise
    except (TypeError, ValueError, ValidationError):
        raise _invalid_precondition() from None

    _assert_same_deterministic_identity(candidate.identity, checked_inventory)
    marker = checked_inventory.fingerprint_key_marker
    if marker is None:
        raise AdhelmError(
            "GA4_FINGERPRINT_KEY_CONTINUITY_NOT_EVIDENCED",
            "The supplied GA4 Admin inventory has no fingerprint-key continuity marker.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Collect a new normalized GA4 Admin inventory with the configured local key."
            ),
        )
    expected_marker = _fingerprint_key_marker(fingerprint_key)
    if not hmac.compare_digest(marker, expected_marker):
        raise AdhelmError(
            "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH",
            "The supplied GA4 Admin inventory was not fingerprinted with the current local key.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Collect a new normalized GA4 Admin inventory with the configured local key."
            ),
        )
    content_marker = checked_inventory.inventory_content_marker
    if content_marker is None:
        raise AdhelmError(
            "GA4_FINGERPRINT_INVENTORY_CONTENT_NOT_EVIDENCED",
            "The supplied GA4 Admin inventory has no keyed content-integrity marker.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Collect a new normalized GA4 Admin inventory with the configured local key."
            ),
        )
    expected_content_marker = _fingerprint_inventory_content_marker(
        fingerprint_key,
        checked_inventory,
    )
    if not hmac.compare_digest(content_marker, expected_content_marker):
        raise AdhelmError(
            "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH",
            "The supplied GA4 Admin inventory content does not match the current local key.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Collect a new normalized GA4 Admin inventory with the configured local key."
            ),
        )

    event_name_fingerprint = _fingerprint(
        fingerprint_key,
        "key-event-name",
        candidate.request.eventName,
    )
    if any(
        hmac.compare_digest(key_event.event_name_fingerprint, event_name_fingerprint)
        for key_event in checked_inventory.key_events
    ):
        raise AdhelmError(
            "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY",
            "The proposed key event is already observed in the complete supplied inventory.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Select a distinct declared event or collect fresh evidence before planning."
            ),
        )

    return Ga4AdminKeyEventInventoryPreconditions(candidate_digest=candidate.candidate_digest)


def _require_fingerprint_key(value: object) -> bytes:
    if type(value) is not bytes or not 32 <= len(value) <= 4_096:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "GA4 duplicate preconditions require a private local fingerprint key.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Configure the local GA4 fingerprint key before comparing inventories.",
        )
    return value


def _invalid_precondition() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_PRECONDITION",
        "GA4 key-event precondition input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _model_json_object(
    value: object, *, expected: type[BaseModel]
) -> tuple[str, dict[str, object]]:
    """Detach an exact model through JSON without laundering control scalars.

    We deliberately inspect the parsed original JSON before RFC 8785
    canonicalization.  This makes a constructed ``true`` or ``1.0`` fail
    rather than turn into integer ``1`` during a later digest operation.
    """

    if type(value) is not expected:
        raise TypeError("unexpected model type")
    raw = value.model_dump_json(warnings="error")
    if len(raw.encode("utf-8")) > _MAX_MODEL_JSON_BYTES:
        raise ValueError("model JSON exceeds the precondition bound")
    parsed = parse_json(raw, maximum_bytes=_MAX_MODEL_JSON_BYTES)
    if type(parsed) is not dict:
        raise ValueError("model JSON must be an object")
    return raw, cast(dict[str, object], parsed)


def _revalidate_create_input(value: object) -> Ga4AdminKeyEventCreateInput:
    try:
        raw, data = _model_json_object(value, expected=Ga4AdminKeyEventCreateInput)
        _validate_create_input_raw(data)
        checked = Ga4AdminKeyEventCreateInput.model_validate_json(raw)
        _require_complete_canonical_shape(data, checked)
        return checked
    except (AdhelmError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_precondition() from None


def _revalidate_inventory(value: object) -> Ga4AdminInventory:
    try:
        raw, data = _model_json_object(value, expected=Ga4AdminInventory)
        _validate_inventory_raw(data)
        checked = Ga4AdminInventory.model_validate_json(raw)
        _require_complete_canonical_shape(data, checked)
        return checked
    except (AdhelmError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_precondition() from None


def _require_complete_canonical_shape(data: dict[str, object], model: BaseModel) -> None:
    """Reject abbreviated/constructed models after raw types have been inspected."""

    normalized = model.model_dump(mode="json", warnings="error")
    if canonical_bytes(data) != canonical_bytes(normalized):
        raise ValueError("model JSON is not complete canonical shape")


def _require_keys(
    data: dict[str, object],
    required: frozenset[str],
    *,
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(data)
    if not required <= keys or keys - required - optional:
        raise ValueError("object has an invalid closed shape")


def _require_object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("expected object")
    return cast(dict[str, object], value)


def _require_array(value: object, *, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise ValueError("expected bounded array")
    return cast(list[object], value)


def _require_string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("expected string")
    return value


def _require_exact_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("expected bounded integer")
    return value


def _require_exact_bool(value: object, *, expected: bool | None = None) -> bool:
    if type(value) is not bool or (expected is not None and value is not expected):
        raise ValueError("expected exact JSON boolean")
    return value


def _require_literal(value: object, allowed: frozenset[str]) -> str:
    rendered = _require_string(value)
    if rendered not in allowed:
        raise ValueError("unexpected literal")
    return rendered


def _require_digest(value: object) -> str:
    rendered = _require_string(value)
    if _DIGEST.fullmatch(rendered) is None:
        raise ValueError("expected lowercase digest")
    return rendered


def _require_numeric_id(value: object) -> str:
    rendered = _require_string(value)
    if _NUMERIC_ID.fullmatch(rendered) is None:
        raise ValueError("expected native numeric identifier")
    return rendered


def _validate_create_input_raw(data: dict[str, object]) -> None:
    _require_keys(
        data,
        frozenset({"event_contract", "identity", "counting_method", "default_value"}),
    )
    contract = _require_object(data["event_contract"])
    _require_keys(contract, frozenset({"event_contract_ref", "event_name", "semantics", "fields"}))
    _require_string(contract["event_contract_ref"])
    _require_string(contract["event_name"])
    semantics = _require_object(contract["semantics"])
    _require_keys(semantics, frozenset({"semantics_ref", "customer_definition", "counting_rule"}))
    for name in ("semantics_ref", "customer_definition", "counting_rule"):
        _require_string(semantics[name])
    for field in _require_array(contract["fields"], maximum=25):
        _validate_event_field_raw(_require_object(field))

    _validate_create_identity_raw(_require_object(data["identity"]))
    _require_literal(data["counting_method"], frozenset({"ONCE_PER_EVENT", "ONCE_PER_SESSION"}))
    default_value = data["default_value"]
    if default_value is not None:
        _validate_create_default_value_raw(_require_object(default_value))


def _validate_event_field_raw(data: dict[str, object]) -> None:
    _require_keys(data, frozenset({"name", "value_type", "required"}))
    _require_string(data["name"])
    _require_literal(
        data["value_type"],
        frozenset({"BOOLEAN", "INTEGER", "LOW_CARDINALITY_STRING"}),
    )
    _require_exact_bool(data["required"])


def _validate_create_identity_raw(data: dict[str, object]) -> None:
    _require_keys(
        data,
        frozenset(
            {
                "property_ref",
                "property_client_ref",
                "web_stream_ref",
                "web_stream_client_ref",
                "api_version",
                "required_scope",
            }
        ),
    )
    property_client_ref = _require_numeric_id(data["property_client_ref"])
    web_stream_client_ref = _require_numeric_id(data["web_stream_client_ref"])
    if (
        _require_string(data["property_ref"]) != f"ga4_admin.property.{property_client_ref}"
        or _require_string(data["web_stream_ref"])
        != f"ga4_admin.web_stream.{web_stream_client_ref}"
        or data["api_version"] != "v1alpha"
        or data["required_scope"] != _EDIT_SCOPE
    ):
        raise ValueError("create identity is not deterministic")


def _validate_create_default_value_raw(data: dict[str, object]) -> None:
    _require_keys(data, frozenset({"numeric_value", "currency_code"}))
    numeric_value = _require_string(data["numeric_value"])
    if _DECIMAL.fullmatch(numeric_value) is None:
        raise ValueError("default value must use canonical decimal serialization")
    _require_string(data["currency_code"])


def _validate_inventory_raw(data: dict[str, object]) -> None:
    _require_keys(
        data,
        frozenset(
            {
                "schema_version",
                "identity",
                "key_events",
                "event_create_rules",
                "event_edit_rules",
                "pagination_verified",
            }
        ),
        optional=frozenset({"fingerprint_key_marker", "inventory_content_marker"}),
    )
    _require_exact_int(data["schema_version"], minimum=1, maximum=1)
    identity = _validate_inventory_identity_raw(_require_object(data["identity"]))
    _require_exact_bool(data["pagination_verified"], expected=True)
    for marker_field in ("fingerprint_key_marker", "inventory_content_marker"):
        if marker_field in data:
            _require_digest(data[marker_field])

    key_event_refs: set[str] = set()
    for item in _require_array(data["key_events"], maximum=1_000):
        _validate_key_event_raw(_require_object(item), identity, key_event_refs)

    create_rule_refs: set[str] = set()
    for item in _require_array(data["event_create_rules"], maximum=1_000):
        _validate_event_rule_raw(
            _require_object(item),
            identity,
            create_rule_refs,
            expected_kind="EVENT_CREATE_RULE",
        )

    edit_rule_refs: set[str] = set()
    for item in _require_array(data["event_edit_rules"], maximum=1_000):
        _validate_event_rule_raw(
            _require_object(item),
            identity,
            edit_rule_refs,
            expected_kind="EVENT_EDIT_RULE",
        )


def _validate_inventory_identity_raw(data: dict[str, object]) -> tuple[str, str, str, str]:
    _require_keys(
        data,
        frozenset(
            {
                "schema_version",
                "property_ref",
                "property_client_ref",
                "web_stream_ref",
                "web_stream_client_ref",
                "admin_api_version",
                "required_scope",
            }
        ),
    )
    _require_exact_int(data["schema_version"], minimum=1, maximum=1)
    property_client_ref = _require_numeric_id(data["property_client_ref"])
    web_stream_client_ref = _require_numeric_id(data["web_stream_client_ref"])
    property_ref = _require_string(data["property_ref"])
    web_stream_ref = _require_string(data["web_stream_ref"])
    if (
        property_ref != f"ga4_admin.property.{property_client_ref}"
        or web_stream_ref != f"ga4_admin.web_stream.{web_stream_client_ref}"
        or data["admin_api_version"] != "v1alpha"
        or data["required_scope"] != _READONLY_SCOPE
    ):
        raise ValueError("inventory identity is not deterministic")
    return property_ref, property_client_ref, web_stream_ref, web_stream_client_ref


def _validate_key_event_raw(
    data: dict[str, object],
    identity: tuple[str, str, str, str],
    seen_resource_refs: set[str],
) -> None:
    _require_keys(
        data,
        frozenset(
            {
                "resource_ref",
                "resource_client_ref",
                "property_ref",
                "configuration_fingerprint",
                "event_name_fingerprint",
                "fingerprint_scheme",
                "counting_method",
                "default_value",
                "custom",
                "deletable",
            }
        ),
    )
    property_ref, _, _, _ = identity
    resource_client_ref = _require_numeric_id(data["resource_client_ref"])
    if (
        _require_string(data["resource_ref"]) != f"ga4_admin.key_event.{resource_client_ref}"
        or _require_string(data["property_ref"]) != property_ref
        or data["fingerprint_scheme"] != _FINGERPRINT_SCHEME
    ):
        raise ValueError("key event membership is invalid")
    if resource_client_ref in seen_resource_refs:
        raise ValueError("key event resource is duplicated")
    seen_resource_refs.add(resource_client_ref)
    _require_digest(data["configuration_fingerprint"])
    _require_digest(data["event_name_fingerprint"])
    _require_literal(
        data["counting_method"],
        frozenset({"COUNTING_METHOD_UNSPECIFIED", "ONCE_PER_EVENT", "ONCE_PER_SESSION"}),
    )
    _require_exact_bool(data["custom"])
    _require_exact_bool(data["deletable"])
    default_value = data["default_value"]
    if default_value is not None:
        _validate_inventory_default_value_raw(_require_object(default_value))


def _validate_inventory_default_value_raw(data: dict[str, object]) -> None:
    _require_keys(data, frozenset({"numeric_value", "currency"}))
    numeric_value = _require_string(data["numeric_value"])
    if _DECIMAL.fullmatch(numeric_value) is None:
        raise ValueError("inventory default value must use decimal serialization")
    _require_string(data["currency"])


def _validate_event_rule_raw(
    data: dict[str, object],
    identity: tuple[str, str, str, str],
    seen_resource_refs: set[str],
    *,
    expected_kind: Literal["EVENT_CREATE_RULE", "EVENT_EDIT_RULE"],
) -> None:
    _require_keys(
        data,
        frozenset(
            {
                "resource_ref",
                "resource_client_ref",
                "property_ref",
                "web_stream_ref",
                "rule_kind",
                "configuration_fingerprint",
                "fingerprint_scheme",
                "condition_count",
                "parameter_mutation_count",
                "source_copy_parameters",
                "processing_order",
                "inspection_status",
            }
        ),
    )
    property_ref, _, web_stream_ref, _ = identity
    resource_client_ref = _require_numeric_id(data["resource_client_ref"])
    resource_kind = (
        "event_create_rule" if expected_kind == "EVENT_CREATE_RULE" else "event_edit_rule"
    )
    if (
        _require_string(data["resource_ref"]) != f"ga4_admin.{resource_kind}.{resource_client_ref}"
        or _require_string(data["property_ref"]) != property_ref
        or _require_string(data["web_stream_ref"]) != web_stream_ref
        or data["rule_kind"] != expected_kind
        or data["fingerprint_scheme"] != _FINGERPRINT_SCHEME
        or data["inspection_status"] != "OPAQUE_RULE_CONTENT_UNSUPPORTED"
    ):
        raise ValueError("event rule membership is invalid")
    if resource_client_ref in seen_resource_refs:
        raise ValueError("event rule resource is duplicated")
    seen_resource_refs.add(resource_client_ref)
    _require_digest(data["configuration_fingerprint"])
    _require_exact_int(data["condition_count"], minimum=1, maximum=10)
    _require_exact_int(data["parameter_mutation_count"], minimum=0, maximum=20)

    source_copy = data["source_copy_parameters"]
    processing_order = data["processing_order"]
    if expected_kind == "EVENT_CREATE_RULE":
        _require_exact_bool(source_copy)
        if processing_order is not None:
            raise ValueError("event create rule has processing order")
    else:
        if source_copy is not None:
            raise ValueError("event edit rule has source-copy setting")
        parsed_order = _require_string(processing_order)
        if (
            _SIGNED_INT64.fullmatch(parsed_order) is None
            or not -(2**63) <= int(parsed_order) <= 2**63 - 1
        ):
            raise ValueError("event edit rule processing order is invalid")


def _assert_same_deterministic_identity(
    create_identity: Ga4AdminKeyEventIdentity,
    inventory: Ga4AdminInventory,
) -> None:
    """Require the same native property and stream without a resolver seam."""

    inventory_identity = inventory.identity
    if (
        create_identity.property_ref != inventory_identity.property_ref
        or create_identity.property_client_ref != inventory_identity.property_client_ref
        or create_identity.web_stream_ref != inventory_identity.web_stream_ref
        or create_identity.web_stream_client_ref != inventory_identity.web_stream_client_ref
    ):
        raise AdhelmError(
            "GA4_ADMIN_SUPPLIED_INVENTORY_IDENTITY_MISMATCH",
            "The supplied GA4 Admin inventory is for a different declared property or web stream.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Collect an inventory for the exact declared GA4 property and web stream.",
        )


__all__ = [
    "Ga4AdminKeyEventInventoryPreconditions",
    "evaluate_ga4_key_event_create_preconditions",
]

"""Lossless, local presentation detail transforms for redacted artifact documents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError

ArtifactDetail = Literal["compact", "standard", "detailed"]
type JsonValue = Any

_COMPACT_MARKER = "$artifact_compact_rows"
_COMPACT_VERSION = 1
_DETAILED_MARKER = "$artifact_detail"
_COMPACT_INHERITANCE = (
    "Each per_row_values entry inherits shared_fields; its own keys take precedence."
)


def present_document(
    document: Mapping[str, object],
    detail: ArtifactDetail,
    source_schema: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a non-mutating presentation of an already-redacted JSON document.

    Compact list wrappers declare that every ``per_row_values`` object inherits
    all ``shared_fields`` before its own keys are applied. The wrapper is
    emitted only when it makes canonical JSON smaller, so compact output
    remains a lossless encoding rather than a summary.
    """
    if detail not in {"compact", "standard", "detailed"}:
        raise AdhelmError("UNSUPPORTED_ARTIFACT_DETAIL", "Artifact detail is unsupported.")
    copied = _copy_value(document)
    if not isinstance(copied, dict):  # Mapping values are copied into a dict above.
        raise AdhelmError("INVALID_ARTIFACT_DOCUMENT", "Artifact document must be a JSON object.")
    if detail == "standard":
        return copied
    if detail == "compact":
        compacted = _compact_value(copied)
        if not isinstance(compacted, dict):  # The input contract fixes the root as an object.
            raise AdhelmError(
                "INVALID_ARTIFACT_DOCUMENT", "Artifact document must be a JSON object."
            )
        return compacted
    if not isinstance(source_schema, Mapping):
        raise AdhelmError(
            "ARTIFACT_SOURCE_SCHEMA_REQUIRED",
            "Detailed artifact presentation requires a static source schema.",
        )
    return {
        _DETAILED_MARKER: {"version": _COMPACT_VERSION, "detail": "detailed"},
        "document": copied,
        "field_guide": _field_guide(source_schema),
    }


def _copy_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise AdhelmError(
                    "INVALID_ARTIFACT_DOCUMENT", "Artifact document keys must be strings."
                )
            copied[key] = _copy_value(child)
        return copied
    if isinstance(value, list):
        return [_copy_value(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise AdhelmError(
        "INVALID_ARTIFACT_DOCUMENT", "Artifact document must contain JSON-compatible values."
    )


def _compact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _compact_value(child) for key, child in value.items()}
    if not isinstance(value, list):
        return value
    rows = [_compact_value(child) for child in value]
    if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
        return rows
    common_keys = set(rows[0])
    for row in rows[1:]:
        common_keys.intersection_update(row)
    shared_fields = {
        key: rows[0][key]
        for key in sorted(common_keys)
        if all(_same_json_value(row[key], rows[0][key]) for row in rows[1:])
    }
    if not shared_fields:
        return rows
    per_row_values = [
        {key: value for key, value in row.items() if key not in shared_fields} for row in rows
    ]
    candidate: JsonValue = {
        _COMPACT_MARKER: {
            "version": _COMPACT_VERSION,
            "inheritance": _COMPACT_INHERITANCE,
        },
        "shared_fields": shared_fields,
        "per_row_values": per_row_values,
    }
    return candidate if len(canonical_bytes(candidate)) < len(canonical_bytes(rows)) else rows


def _same_json_value(left: JsonValue, right: JsonValue) -> bool:
    """Compare values using their canonical JSON representation.

    Python equality considers ``True == 1`` (and recurses that rule through
    containers), while JSON distinguishes the boolean and number values. A
    compact wrapper must retain a field whenever its rows differ in either
    representation, or restoring the wrapper changes the redacted document.
    """
    return canonical_bytes(left) == canonical_bytes(right)


def _field_guide(source_schema: Mapping[str, object]) -> JsonValue:
    definitions = source_schema.get("$defs")
    definitions_map = definitions if isinstance(definitions, Mapping) else {}
    fields: list[JsonValue] = []
    _append_fields(
        source_schema,
        definitions_map,
        path="$",
        required=True,
        fields=fields,
        resolving=frozenset(),
    )
    return {
        "kind": "static_schema_field_guide",
        "fields": fields,
        "value_policy": "Contains structural field names and JSON types only; no source values.",
    }


def _append_fields(
    schema: object,
    definitions: Mapping[str, object],
    *,
    path: str,
    required: bool,
    fields: list[JsonValue],
    resolving: frozenset[str],
) -> None:
    if not isinstance(schema, Mapping):
        return
    resolved, next_resolving = _resolve_reference(schema, definitions, resolving)
    fields.append({"path": path, "required": required, "type": _schema_type(resolved, definitions)})
    properties = resolved.get("properties")
    required_names = resolved.get("required")
    required_set = (
        {name for name in required_names if isinstance(name, str)}
        if isinstance(required_names, list)
        else set()
    )
    if isinstance(properties, Mapping):
        for name in sorted(key for key in properties if isinstance(key, str)):
            _append_fields(
                properties[name],
                definitions,
                path=f"{path}.{name}",
                required=name in required_set,
                fields=fields,
                resolving=next_resolving,
            )
    items = resolved.get("items")
    if isinstance(items, Mapping):
        _append_fields(
            items,
            definitions,
            path=f"{path}[]",
            required=False,
            fields=fields,
            resolving=next_resolving,
        )


def _resolve_reference(
    schema: Mapping[str, object], definitions: Mapping[str, object], resolving: frozenset[str]
) -> tuple[Mapping[str, object], frozenset[str]]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return schema, resolving
    name = reference.removeprefix("#/$defs/")
    target = definitions.get(name)
    if name in resolving or not isinstance(target, Mapping):
        return schema, resolving
    return target, resolving | {name}


def _schema_type(schema: Mapping[str, object], definitions: Mapping[str, object]) -> str:
    resolved, _ = _resolve_reference(schema, definitions, frozenset())
    direct_type = resolved.get("type")
    if isinstance(direct_type, str):
        return direct_type
    alternatives = resolved.get("anyOf")
    if not isinstance(alternatives, list):
        alternatives = resolved.get("oneOf")
    if isinstance(alternatives, list):
        names = sorted(
            {
                _schema_type(option, definitions)
                for option in alternatives
                if isinstance(option, Mapping)
            }
        )
        if names:
            return "|".join(names)
    if "properties" in resolved:
        return "object"
    if "items" in resolved:
        return "array"
    return "unspecified"

"""Generic deterministic semantic-hierarchy state projection.

A provider CREATE, ACTIVATE, or MAINTAIN operation usually cannot predict the
identifiers a provider assigns.  A useful before/after state therefore projects
only the request-controllable semantics of a hierarchy: the configured names and
configuration values a plan can name.  Provider-native resource identifiers,
creation timestamps, and other provider-assigned metadata must stay out, because
they are unknowable before the write and would make the predicted ``after``
state unusable.

Choosing what a member's configuration contains is the caller's decision: this
module cannot know which of a provider's fields are request-controllable.  It is
not a silent trust boundary either.  ``member_fingerprint`` rejects the
identifier-shaped keys and provider resource-path values listed in
``_RESERVED_MEMBER_KEYS`` and ``_NATIVE_REFERENCE``, which catches the common
mistakes; a caller that hashes a native identifier under some other field name
still breaks its own projection, and its own golden test is what proves it did
not.

Every value here is a local keyed HMAC or a canonical digest.  A matching
fingerprint proves neither provider origin, freshness, permission, reservation,
execution authority, nor a remote mutation outcome.  The keyed construction
exists so that a retained projection cannot expose a guessable hash of a
customer's configured names.

This module is deliberately provider-neutral: ``SemanticHierarchySpec`` carries
the caller's HMAC domain-separation context and purpose labels, so two providers
never collide in one keyed namespace.  It was generalized from the GA4 Admin
key-event CREATE projection and keeps that module's exact byte-level output.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from pydantic import BaseModel

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.safety import StateRef

DIGEST_PATTERN = r"^[a-f0-9]{64}$"

#: Field names that almost always carry a provider-assigned identifier.  A
#: projection keyed on one of these could never match a post-create read.  Keys
#: are normalized from camelCase first, so ``campaignId``, ``campaign_id`` and
#: ``CAMPAIGN_ID`` are all the same name here.
_RESERVED_MEMBER_KEYS = frozenset({"id", "name", "resource_id", "resource_name", "resource_ref"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
#: A provider resource path such as ``properties/123/keyEvents/456`` or
#: ``customers/1234567890/campaigns/77``.
_NATIVE_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*/[^\s]+$")

_DEFAULT_MAX_ITEMS = 1_000
_DEFAULT_MAX_STATE_BYTES = 1_048_576
_DEFAULT_MAX_STATE_NODES = 2_000
_DEFAULT_MAX_STATE_DEPTH = 12
_DEFAULT_MAX_STRING_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class SemanticStateBounds:
    """Closed bounds applied before any canonicalization or schema work."""

    max_items: int = _DEFAULT_MAX_ITEMS
    max_state_bytes: int = _DEFAULT_MAX_STATE_BYTES
    max_state_nodes: int = _DEFAULT_MAX_STATE_NODES
    max_state_depth: int = _DEFAULT_MAX_STATE_DEPTH
    max_string_bytes: int = _DEFAULT_MAX_STRING_BYTES

    def __post_init__(self) -> None:
        for value in (
            self.max_items,
            self.max_state_bytes,
            self.max_state_nodes,
            self.max_state_depth,
            self.max_string_bytes,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("semantic state bounds must be positive integers")


DEFAULT_BOUNDS = SemanticStateBounds()


@dataclass(frozen=True, slots=True)
class SemanticHierarchySpec:
    """One provider's fixed keyed namespace for a semantic hierarchy projection.

    ``context`` is the provider's HMAC domain separator.  ``state_kind`` and
    ``semantic_projection`` are recorded inside the hashed material so that a
    fingerprint from one projection version can never be replayed as another.
    """

    context: bytes
    state_kind: str
    semantic_projection: str
    target_purpose: str
    member_purpose: str
    collection_purpose: str
    bounds: SemanticStateBounds = DEFAULT_BOUNDS

    def __post_init__(self) -> None:
        if type(self.context) is not bytes or not self.context:
            raise ValueError("semantic hierarchy context must be non-empty bytes")
        for label in (
            self.state_kind,
            self.semantic_projection,
            self.target_purpose,
            self.member_purpose,
            self.collection_purpose,
        ):
            if type(label) is not str or not label or not label.isascii():
                raise ValueError("semantic hierarchy labels must be non-empty ASCII")


def keyed_fingerprint(key: bytes, *, context: bytes, purpose: str, value: Any) -> str:
    """Key transient provider content so projections expose no guessable hash."""

    if type(key) is not bytes or len(key) < 16:
        raise ValueError("semantic fingerprints require a local key")
    material = context + purpose.encode("ascii") + b"\x00"
    try:
        material += canonical_bytes(_fingerprint_value(value))
    except AdhelmError:
        # Canonical JSON rejects a non-representable value, an integer beyond
        # 2**53, or a lone surrogate.  Report it as this module's bounded input
        # failure rather than leaking a canonicalization error to callers that
        # catch only ``ValueError``.
        raise ValueError("semantic value cannot be canonically serialized") from None
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def target_binding_fingerprint(
    spec: SemanticHierarchySpec, target: dict[str, Any], *, fingerprint_key: bytes
) -> str:
    """Bind a projection to the exact configured target it was read from."""

    if type(target) is not dict:
        raise ValueError("semantic target must be a JSON object")
    return keyed_fingerprint(
        fingerprint_key,
        context=spec.context,
        purpose=spec.target_purpose,
        value={
            "schema_version": 1,
            "state_kind": spec.state_kind,
            "target": target,
        },
    )


def member_fingerprint(
    spec: SemanticHierarchySpec,
    *,
    identity_field: str,
    identity_fingerprint: str,
    configuration: dict[str, Any],
    fingerprint_key: bytes,
) -> str:
    """Fingerprint one hierarchy member from its name binding and configuration.

    ``identity_fingerprint`` is already a keyed fingerprint of the member's
    request-controllable name.  ``configuration`` holds the remaining normalized
    request-controllable values.  Neither may contain a provider-assigned
    resource identifier; that is the caller's contract and the reason a
    projection stays comparable across a create.
    """

    if type(configuration) is not dict:
        raise ValueError("semantic member configuration must be a JSON object")
    if type(identity_field) is not str or not identity_field or identity_field.isdecimal():
        raise ValueError("semantic member identity field must be a name")
    reserved = {"schema_version", "semantic_projection", identity_field}
    if reserved & set(configuration):
        raise ValueError("semantic member configuration cannot restate reserved fields")
    _reject_native_identifiers(configuration)
    return keyed_fingerprint(
        fingerprint_key,
        context=spec.context,
        purpose=spec.member_purpose,
        value={
            "schema_version": 1,
            "semantic_projection": spec.semantic_projection,
            identity_field: exact_digest(identity_fingerprint),
            **configuration,
        },
    )


def _reject_native_identifiers(value: Any, *, depth: int = 0) -> None:
    """Refuse the identifier shapes a semantic projection must never hash."""

    if depth > 8:
        raise ValueError("semantic member configuration is too deeply nested")
    if type(value) is dict:
        for key, child in cast(dict[str, Any], value).items():
            normalized = _normalized_key(str(key))
            if normalized in _RESERVED_MEMBER_KEYS or normalized.endswith(("_id", "_ids")):
                raise ValueError("semantic member configuration cannot carry a native identifier")
            _reject_native_identifiers(child, depth=depth + 1)
    elif type(value) is list:
        for child in value:
            _reject_native_identifiers(child, depth=depth + 1)
    elif type(value) is str and _NATIVE_REFERENCE.fullmatch(value) is not None:
        raise ValueError("semantic member configuration cannot carry a provider resource path")


def _normalized_key(key: str) -> str:
    """Fold camelCase and separator variants onto one snake_case name."""

    return _CAMEL_BOUNDARY.sub("_", key).replace("-", "_").replace(" ", "_").lower()


def collection_state(
    spec: SemanticHierarchySpec,
    *,
    binding_fingerprint: str,
    fingerprint_key_marker: str,
    member_fingerprints: tuple[str, ...],
    fingerprint_key: bytes,
) -> StateRef:
    """Project a sorted member multiset into one opaque comparable state.

    Duplicate member fingerprints are retained on purpose.  A set would lose the
    distinction between one matching member and two resources sharing the same
    request-controllable semantics.
    """

    if (
        _digest_or_none(binding_fingerprint) is None
        or _digest_or_none(fingerprint_key_marker) is None
        or type(member_fingerprints) is not tuple
        or len(member_fingerprints) > spec.bounds.max_items + 1
        or tuple(sorted(member_fingerprints)) != member_fingerprints
        or any(_digest_or_none(value) is None for value in member_fingerprints)
    ):
        raise ValueError("semantic collection input is invalid")
    return StateRef(
        fingerprint=keyed_fingerprint(
            fingerprint_key,
            context=spec.context,
            purpose=spec.collection_purpose,
            value={
                "schema_version": 1,
                "state_kind": spec.state_kind,
                "semantic_projection": spec.semantic_projection,
                "target_binding_fingerprint": binding_fingerprint,
                "fingerprint_key_marker": fingerprint_key_marker,
                "member_fingerprints": list(member_fingerprints),
            },
        ),
        revision=spec.state_kind,
    )


def state_json_mapping(
    value: object, *, expected: type[BaseModel], bounds: SemanticStateBounds = DEFAULT_BOUNDS
) -> dict[str, object]:
    """Reduce an installed model or a supplied mapping to bounded JSON."""

    try:
        if type(value) is expected:
            raw_json = value.model_dump_json(warnings="error")
            if len(raw_json.encode("utf-8")) > bounds.max_state_bytes:
                raise ValueError("state size")
            parsed = parse_json(raw_json, maximum_bytes=bounds.max_state_bytes)
            if type(parsed) is not dict:
                raise ValueError("state shape")
            return cast(dict[str, object], parsed)
        if type(value) is not dict:
            raise ValueError("state type")
        validate_json_shape(cast(dict[str, object], value), bounds=bounds)
        return cast(dict[str, object], value)
    except (OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise ValueError("state cannot be safely serialized") from None


def validate_json_shape(
    value: dict[str, object], *, bounds: SemanticStateBounds = DEFAULT_BOUNDS
) -> None:
    """Bound a direct mapping before schema/canonical processing."""

    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    string_bytes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > bounds.max_state_nodes or depth > bounds.max_state_depth:
            raise ValueError("state nesting")
        if type(current) is dict:
            mapping = cast(dict[object, object], current)
            if any(type(key) is not str for key in mapping):
                raise ValueError("state key")
            pending.extend((child, depth + 1) for child in mapping.values())
        elif type(current) is list:
            if len(current) > bounds.max_items + 1:
                raise ValueError("state list")
            pending.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            try:
                encoded_size = len(current.encode("utf-8"))
            except UnicodeError:
                # A lone surrogate is not encodable JSON text.  Report it as this
                # module's bounded failure instead of leaking an encoding error.
                raise ValueError("state string") from None
            string_bytes += encoded_size
            if encoded_size > bounds.max_string_bytes or string_bytes > bounds.max_state_bytes:
                raise ValueError("state string")
        elif type(current) is int:
            if current.bit_length() > 53:
                raise ValueError("state integer")
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("state float")
        elif current is None or type(current) is bool:
            continue
        else:
            raise ValueError("state JSON scalar")


def require_exact_model_shape(data: dict[str, object], model: BaseModel) -> None:
    try:
        normalized = model.model_dump(mode="json", warnings="error")
        if type(normalized) is not dict or canonical_bytes(data) != canonical_bytes(normalized):
            raise ValueError("model is not its complete canonical shape")
    except AdhelmError:
        raise ValueError("model cannot be canonically serialized") from None


def require_keys(data: dict[str, object], expected: frozenset[str]) -> None:
    if set(data) != expected:
        raise ValueError("closed object shape")


def require_mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("expected object")
    return cast(dict[str, object], value)


def require_literal(value: object, expected: str) -> str:
    if type(value) is not str or value != expected:
        raise ValueError("unexpected control value")
    return value


def exact_schema_version(value: object) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")
    return value


def exact_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("expected bounded JSON integer")
    return value


def exact_digest(value: object) -> str:
    digest_value = _digest_or_none(value)
    if digest_value is None:
        raise ValueError("expected lowercase SHA-256 digest")
    return digest_value


def exact_false(value: object) -> bool:
    if type(value) is not bool or value is not False:
        raise ValueError("expected false control")
    return value


def _digest_or_none(value: object) -> str | None:
    if type(value) is not str or len(value) != 64:
        return None
    return value if all(character in "0123456789abcdef" for character in value) else None


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, dict):
        return {key: _fingerprint_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_fingerprint_value(child) for child in value]
    return value


__all__ = [
    "DEFAULT_BOUNDS",
    "DIGEST_PATTERN",
    "SemanticHierarchySpec",
    "SemanticStateBounds",
    "collection_state",
    "exact_digest",
    "exact_false",
    "exact_int",
    "exact_schema_version",
    "keyed_fingerprint",
    "member_fingerprint",
    "require_exact_model_shape",
    "require_keys",
    "require_literal",
    "require_mapping",
    "state_json_mapping",
    "target_binding_fingerprint",
    "validate_json_shape",
]

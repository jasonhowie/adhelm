"""Pure semantic state reconstruction for one GA4 Admin key-event CREATE.

GA4 assigns the key-event resource identifier at creation time.  This module
therefore never predicts a resource ID or hashes the full mutable Admin
collection into a post-create state.  It projects only the bounded semantics
that a fixed CREATE request can name: an HMAC of the event name, counting
method, and an exact normalized default value.  Resource IDs, create times,
``custom``, and ``deletable`` remain in the separately retained full-inventory
digest used for pre-apply drift detection.

The functions here are local, pure planning helpers.  A matching local HMAC
proves neither provider origin, freshness, permission, reservation, execution
authority, nor a remote mutation outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import StrictModel
from adhelm.providers._semantic_state import (
    SemanticHierarchySpec,
    SemanticStateBounds,
)
from adhelm.providers._semantic_state import (
    collection_state as _semantic_collection_state,
)
from adhelm.providers._semantic_state import (
    exact_digest as _exact_digest,
)
from adhelm.providers._semantic_state import (
    exact_false as _exact_false,
)
from adhelm.providers._semantic_state import (
    exact_int as _exact_int,
)
from adhelm.providers._semantic_state import (
    exact_schema_version as _exact_schema_version,
)
from adhelm.providers._semantic_state import (
    member_fingerprint as _semantic_member_fingerprint_value,
)
from adhelm.providers._semantic_state import (
    require_exact_model_shape as _require_exact_model_shape,
)
from adhelm.providers._semantic_state import (
    require_keys as _require_keys,
)
from adhelm.providers._semantic_state import (
    require_literal as _require_literal,
)
from adhelm.providers._semantic_state import (
    require_mapping as _require_mapping,
)
from adhelm.providers._semantic_state import (
    state_json_mapping as _semantic_state_json_mapping,
)
from adhelm.providers._semantic_state import (
    target_binding_fingerprint as _semantic_target_binding_fingerprint,
)
from adhelm.providers.ga4_admin import (
    _FINGERPRINT_CONTEXT,
    Ga4AdminDefaultValue,
    Ga4AdminKeyEvent,
    _fingerprint,
)
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateCandidate,
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    compile_ga4_admin_key_event_create,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.providers.ga4_admin_edit_preconditions import (
    Ga4AdminEditKeyEventCreatePreconditions,
    evaluate_ga4_edit_key_event_create_preconditions,
)
from adhelm.providers.ga4_admin_preconditions import _revalidate_create_input
from adhelm.safety import StateRef

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_DECIMAL_STRING = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_MAX_ITEMS = 1_000
_MAX_STATE_BYTES = 1_048_576
_MAX_STATE_NODES = 2_000
_MAX_STATE_DEPTH = 12
_MAX_STRING_BYTES = 16_384

_STATE_KIND: Literal["GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1"] = (
    "GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1"
)
_SEMANTIC_PROJECTION: Literal["EVENT_NAME_HMAC_COUNTING_DEFAULT_VALUE_EXACT_V1"] = (
    "EVENT_NAME_HMAC_COUNTING_DEFAULT_VALUE_EXACT_V1"
)
_TARGET_PURPOSE = "key-event-create-semantic-target"
_MEMBER_PURPOSE = "key-event-create-semantic-member"
_COLLECTION_PURPOSE = "key-event-create-semantic-collection"

# The generic projection kernel owns the keyed construction.  The labels and
# bounds below are the exact values this module used before that extraction, so
# every retained GA4 fingerprint stays byte-identical.
_SEMANTIC_SPEC = SemanticHierarchySpec(
    context=_FINGERPRINT_CONTEXT,
    state_kind=_STATE_KIND,
    semantic_projection=_SEMANTIC_PROJECTION,
    target_purpose=_TARGET_PURPOSE,
    member_purpose=_MEMBER_PURPOSE,
    collection_purpose=_COLLECTION_PURPOSE,
    bounds=SemanticStateBounds(
        max_items=_MAX_ITEMS,
        max_state_bytes=_MAX_STATE_BYTES,
        max_state_nodes=_MAX_STATE_NODES,
        max_state_depth=_MAX_STATE_DEPTH,
        max_string_bytes=_MAX_STRING_BYTES,
    ),
)

_PROJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "projection_kind",
        "semantic_projection",
        "target_binding_fingerprint",
        "inventory_digest",
        "state",
        "member_count",
        "inventory_scope",
        "fingerprint_key_continuity",
        "inventory_content_integrity",
        "current_provider_state",
        "reservation_status",
        "permission_status",
        "remote_side_effects",
        "execution_authority",
    }
)
_CREATE_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "state_kind",
        "semantic_projection",
        "target_binding_fingerprint",
        "candidate_digest",
        "inventory_digest",
        "before",
        "after",
        "before_member_count",
        "after_member_count",
        "preconditions",
    }
)
_PRECONDITION_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_digest",
        "inventory_digest",
        "inventory_scope",
        "fingerprint_key_continuity",
        "inventory_content_integrity",
        "observed_service_level",
        "complete_observed_key_event_total",
        "documented_key_event_cap",
        "remaining_documented_slots",
        "capacity_status",
        "duplicate_status",
        "current_provider_state",
        "reservation_status",
        "permission_status",
        "remote_side_effects",
        "execution_authority",
    }
)


class Ga4AdminEditKeyEventSemanticProjection(StrictModel):
    """Opaque semantic projection of one complete supplied EDIT inventory.

    This intentionally has no duplicate, capacity, candidate, provider-read,
    or authority assertion.  A post-create reader can use it for exact
    semantic-state comparison even though the proposed event is now present.
    """

    schema_version: Literal[1] = 1
    projection_kind: Literal["GA4_ADMIN_EDIT_KEY_EVENT_SEMANTIC_PROJECTION"] = (
        "GA4_ADMIN_EDIT_KEY_EVENT_SEMANTIC_PROJECTION"
    )
    semantic_projection: Literal["EVENT_NAME_HMAC_COUNTING_DEFAULT_VALUE_EXACT_V1"] = (
        _SEMANTIC_PROJECTION
    )
    target_binding_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: StateRef
    member_count: int = Field(ge=0, le=_MAX_ITEMS)
    inventory_scope: Literal["COMPLETE_SUPPLIED_EDIT_INVENTORY"] = (
        "COMPLETE_SUPPLIED_EDIT_INVENTORY"
    )
    fingerprint_key_continuity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"
    )
    inventory_content_integrity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    )
    current_provider_state: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    reservation_status: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    permission_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    remote_side_effects: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version(value)

    @field_validator("target_binding_fingerprint", "inventory_digest", mode="before")
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("member_count", mode="before")
    @classmethod
    def exact_member_count(cls, value: object) -> int:
        return _exact_int(value, minimum=0, maximum=_MAX_ITEMS)

    @field_validator("state", mode="before")
    @classmethod
    def exact_state_ref(cls, value: object) -> StateRef:
        if type(value) is not StateRef:
            raise ValueError("semantic projection requires an installed state reference")
        _validate_state_ref_instance(value)
        return value

    @field_validator("remote_side_effects", "execution_authority", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("semantic projection control flags must be false")
        return value

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_projection(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Keep the agent-visible projection structurally closed.

        The current private fingerprint key is intentionally not retained in
        this value.  ``revalidate_ga4_admin_edit_key_event_semantic_projection``
        performs keyed reconstruction before a projection can be trusted as a
        source binding.
        """

        try:
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("projection serialization")
            _validate_projection_raw(cast(dict[str, object], output))
            return output
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 Admin semantic projection is invalid") from None


class Ga4AdminKeyEventCreateSemanticState(StrictModel):
    """Predicted before/after semantic collection for one non-executable CREATE.

    The full inventory digest is deliberately distinct from ``before`` and
    ``after``.  It detects broad pre-apply inventory drift; the state refs can
    still be compared after GA4 assigns an otherwise unknown resource ID.
    """

    schema_version: Literal[1] = 1
    state_kind: Literal["GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1"] = _STATE_KIND
    semantic_projection: Literal["EVENT_NAME_HMAC_COUNTING_DEFAULT_VALUE_EXACT_V1"] = (
        _SEMANTIC_PROJECTION
    )
    target_binding_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    before: StateRef
    after: StateRef
    before_member_count: int = Field(ge=0, le=_MAX_ITEMS)
    after_member_count: int = Field(ge=1, le=_MAX_ITEMS + 1)
    preconditions: Ga4AdminEditKeyEventCreatePreconditions

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version(value)

    @field_validator(
        "target_binding_fingerprint", "candidate_digest", "inventory_digest", mode="before"
    )
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("before_member_count", mode="before")
    @classmethod
    def exact_before_count(cls, value: object) -> int:
        return _exact_int(value, minimum=0, maximum=_MAX_ITEMS)

    @field_validator("after_member_count", mode="before")
    @classmethod
    def exact_after_count(cls, value: object) -> int:
        return _exact_int(value, minimum=1, maximum=_MAX_ITEMS + 1)

    @field_validator("before", "after", mode="before")
    @classmethod
    def exact_state_refs(cls, value: object) -> StateRef:
        if type(value) is not StateRef:
            raise ValueError("semantic create state requires installed state references")
        _validate_state_ref_instance(value)
        return value

    @field_validator("preconditions", mode="before")
    @classmethod
    def exact_preconditions(cls, value: object) -> Ga4AdminEditKeyEventCreatePreconditions:
        if type(value) is not Ga4AdminEditKeyEventCreatePreconditions:
            raise ValueError("semantic create state requires installed EDIT preconditions")
        return value

    @model_validator(mode="after")
    def exact_transition_binding(self) -> Ga4AdminKeyEventCreateSemanticState:
        if (
            self.after_member_count != self.before_member_count + 1
            or self.before_member_count != self.preconditions.complete_observed_key_event_total
            or self.before.fingerprint == self.after.fingerprint
            or self.preconditions.candidate_digest != self.candidate_digest
            or self.preconditions.inventory_digest != self.inventory_digest
        ):
            raise ValueError("semantic create state transition is inconsistent")
        _validate_state_ref_instance(self.before)
        _validate_state_ref_instance(self.after)
        return self

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_state(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Reject incomplete or structurally forged agent-visible state."""

        try:
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("state serialization")
            _validate_create_state_raw(cast(dict[str, object], output))
            return output
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 Admin semantic create state is invalid") from None


@dataclass(frozen=True, slots=True)
class _ProjectionComponents:
    target_binding_fingerprint: str
    inventory_digest: str
    member_fingerprints: tuple[str, ...]
    state: StateRef


def project_ga4_admin_edit_key_event_semantic_collection(
    inventory: Ga4AdminEditKeyEventInventory,
    *,
    fingerprint_key: bytes,
) -> Ga4AdminEditKeyEventSemanticProjection:
    """Project a complete keyed EDIT inventory without CREATE preconditions.

    This is intentionally suitable for a later post-create reread: it does
    not reject a duplicate proposed event or an inventory at its documented
    capacity.  It verifies only the installed normalized inventory and local
    key/content markers before deriving an opaque semantic collection.
    """

    checked_inventory = _checked_inventory(inventory, fingerprint_key=fingerprint_key)
    components = _projection_components(checked_inventory, fingerprint_key=fingerprint_key)
    return Ga4AdminEditKeyEventSemanticProjection(
        target_binding_fingerprint=components.target_binding_fingerprint,
        inventory_digest=components.inventory_digest,
        state=components.state,
        member_count=len(components.member_fingerprints),
    )


def build_ga4_admin_key_event_create_semantic_state(
    fresh: Ga4AdminKeyEventCreateInput,
    inventory: Ga4AdminEditKeyEventInventory,
    *,
    fingerprint_key: bytes,
) -> Ga4AdminKeyEventCreateSemanticState:
    """Build the exact local semantic transition for one proposed key event.

    Both inputs are revalidated before canonicalization.  The EDIT
    precondition is deliberately invoked here, so a duplicate event, unknown
    capacity, missing/mismatched marker, or mismatched configured target fails
    before the before/after pair is created.
    """

    checked_fresh = _checked_fresh_input(fresh)
    checked_inventory = _checked_inventory(inventory, fingerprint_key=fingerprint_key)
    preconditions = evaluate_ga4_edit_key_event_create_preconditions(
        checked_fresh,
        checked_inventory,
        fingerprint_key=fingerprint_key,
    )
    try:
        candidate = compile_ga4_admin_key_event_create(checked_fresh)
        _require_candidate_target(candidate, checked_inventory.target)
        components = _projection_components(checked_inventory, fingerprint_key=fingerprint_key)
        proposed_member = _proposed_member_fingerprint(
            checked_fresh,
            fingerprint_key=fingerprint_key,
        )
        after_members = tuple(sorted((*components.member_fingerprints, proposed_member)))
        after = _collection_state(
            target_binding_fingerprint=components.target_binding_fingerprint,
            fingerprint_key_marker=checked_inventory.fingerprint_key_marker,
            member_fingerprints=after_members,
            fingerprint_key=fingerprint_key,
        )
        if (
            preconditions.candidate_digest != candidate.candidate_digest
            or preconditions.inventory_digest != components.inventory_digest
            or len(after_members) != len(components.member_fingerprints) + 1
        ):
            raise ValueError("precondition reconstruction")
        return Ga4AdminKeyEventCreateSemanticState(
            target_binding_fingerprint=components.target_binding_fingerprint,
            candidate_digest=candidate.candidate_digest,
            inventory_digest=components.inventory_digest,
            before=components.state,
            after=after,
            before_member_count=len(components.member_fingerprints),
            after_member_count=len(after_members),
            preconditions=preconditions,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def revalidate_ga4_admin_edit_key_event_semantic_projection(
    value: object,
    *,
    inventory: Ga4AdminEditKeyEventInventory,
    fingerprint_key: bytes,
) -> Ga4AdminEditKeyEventSemanticProjection:
    """Rebuild a projection from its inventory instead of trusting its wire form."""

    expected = project_ga4_admin_edit_key_event_semantic_collection(
        inventory,
        fingerprint_key=fingerprint_key,
    )
    try:
        actual = _parse_projection(value)
        if canonical_bytes(actual.model_dump(mode="json", warnings="error")) != canonical_bytes(
            expected.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("projection differs from keyed reconstruction")
        return expected
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_projection() from None


def revalidate_ga4_admin_key_event_create_semantic_state(
    value: object,
    *,
    fresh: Ga4AdminKeyEventCreateInput,
    inventory: Ga4AdminEditKeyEventInventory,
    fingerprint_key: bytes,
) -> Ga4AdminKeyEventCreateSemanticState:
    """Rebuild the exact CREATE transition before trusting retained state."""

    expected = build_ga4_admin_key_event_create_semantic_state(
        fresh,
        inventory,
        fingerprint_key=fingerprint_key,
    )
    try:
        actual = _parse_create_state(value)
        if canonical_bytes(actual.model_dump(mode="json", warnings="error")) != canonical_bytes(
            expected.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("state differs from keyed reconstruction")
        return expected
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state() from None


def _checked_fresh_input(value: object) -> Ga4AdminKeyEventCreateInput:
    if type(value) is not Ga4AdminKeyEventCreateInput:
        raise _invalid_state_input()
    try:
        return _revalidate_create_input(value)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _checked_inventory(value: object, *, fingerprint_key: bytes) -> Ga4AdminEditKeyEventInventory:
    if type(value) is not Ga4AdminEditKeyEventInventory:
        raise _invalid_state_input()
    try:
        return revalidate_ga4_admin_edit_key_event_inventory(
            value,
            fingerprint_key=fingerprint_key,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _projection_components(
    inventory: Ga4AdminEditKeyEventInventory,
    *,
    fingerprint_key: bytes,
) -> _ProjectionComponents:
    target_binding_fingerprint = _target_binding_fingerprint(
        inventory.target,
        fingerprint_key=fingerprint_key,
    )
    members = tuple(
        sorted(
            _observed_member_fingerprint(item, fingerprint_key=fingerprint_key)
            for item in inventory.key_events
        )
    )
    return _ProjectionComponents(
        target_binding_fingerprint=target_binding_fingerprint,
        inventory_digest=digest(inventory),
        member_fingerprints=members,
        state=_collection_state(
            target_binding_fingerprint=target_binding_fingerprint,
            fingerprint_key_marker=inventory.fingerprint_key_marker,
            member_fingerprints=members,
            fingerprint_key=fingerprint_key,
        ),
    )


def _target_binding_fingerprint(
    target: Ga4AdminEditInventoryTarget,
    *,
    fingerprint_key: bytes,
) -> str:
    try:
        data = target.model_dump(mode="json", warnings="error")
        if type(data) is not dict:
            raise ValueError("target serialization")
        return _semantic_target_binding_fingerprint(
            _SEMANTIC_SPEC, data, fingerprint_key=fingerprint_key
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _observed_member_fingerprint(item: Ga4AdminKeyEvent, *, fingerprint_key: bytes) -> str:
    if type(item) is not Ga4AdminKeyEvent:
        raise _invalid_state_input()
    return _semantic_member_fingerprint(
        event_name_fingerprint=_exact_digest(item.event_name_fingerprint),
        counting_method=_observed_counting_method(item.counting_method),
        default_value=_observed_default_value(item.default_value),
        fingerprint_key=fingerprint_key,
    )


def _proposed_member_fingerprint(
    fresh: Ga4AdminKeyEventCreateInput, *, fingerprint_key: bytes
) -> str:
    try:
        event_name = fresh.event_contract.event_name
        if type(event_name) is not str:
            raise ValueError("event name")
        event_name_fingerprint = _fingerprint(fingerprint_key, "key-event-name", event_name)
        return _semantic_member_fingerprint(
            event_name_fingerprint=event_name_fingerprint,
            counting_method=_proposed_counting_method(fresh.counting_method),
            default_value=_proposed_default_value(fresh.default_value),
            fingerprint_key=fingerprint_key,
        )
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _semantic_member_fingerprint(
    *,
    event_name_fingerprint: str,
    counting_method: str,
    default_value: dict[str, str] | None,
    fingerprint_key: bytes,
) -> str:
    """Project one key event by its keyed name and exact configuration only.

    The GA4-assigned resource identifier and create time are excluded, so a
    predicted ``after`` member equals the one a later read observes.
    """

    try:
        return _semantic_member_fingerprint_value(
            _SEMANTIC_SPEC,
            identity_field="event_name_fingerprint",
            identity_fingerprint=event_name_fingerprint,
            configuration={
                "counting_method": counting_method,
                "default_value": default_value,
            },
            fingerprint_key=fingerprint_key,
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _collection_state(
    *,
    target_binding_fingerprint: str,
    fingerprint_key_marker: str,
    member_fingerprints: tuple[str, ...],
    fingerprint_key: bytes,
) -> StateRef:
    # Duplicate member fingerprints are retained by the generic projection: a
    # set would lose the distinction between one matching key event and two
    # resources with the same request-controllable semantics.
    try:
        return _semantic_collection_state(
            _SEMANTIC_SPEC,
            binding_fingerprint=target_binding_fingerprint,
            fingerprint_key_marker=fingerprint_key_marker,
            member_fingerprints=member_fingerprints,
            fingerprint_key=fingerprint_key,
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _observed_counting_method(value: object) -> str:
    if type(value) is not str or value not in {
        "COUNTING_METHOD_UNSPECIFIED",
        "ONCE_PER_EVENT",
        "ONCE_PER_SESSION",
    }:
        raise _invalid_state_input()
    return value


def _proposed_counting_method(value: object) -> str:
    if type(value) is not str or value not in {"ONCE_PER_EVENT", "ONCE_PER_SESSION"}:
        raise _invalid_state_input()
    return value


def _observed_default_value(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if type(value) is not Ga4AdminDefaultValue:
        raise _invalid_state_input()
    return _default_value_data(value, currency_field="currency")


def _proposed_default_value(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if type(value) is not Ga4AdminKeyEventDefaultValueInput:
        raise _invalid_state_input()
    return _default_value_data(value, currency_field="currency_code")


def _default_value_data(value: object, *, currency_field: str) -> dict[str, str]:
    """Take only exact JSON decimal strings; never compare float aliases."""

    try:
        if (
            type(value) is not Ga4AdminDefaultValue
            and type(value) is not Ga4AdminKeyEventDefaultValueInput
        ):
            raise ValueError("default value type")
        raw = value.model_dump(mode="json", warnings="error")
        if type(raw) is not dict or set(raw) != {"numeric_value", currency_field}:
            raise ValueError("default value shape")
        numeric = raw["numeric_value"]
        currency = raw[currency_field]
        if (
            type(numeric) is not str
            or len(numeric) > 256
            or _DECIMAL_STRING.fullmatch(numeric) is None
            or type(currency) is not str
            or _CURRENCY.fullmatch(currency) is None
        ):
            raise ValueError("default value scalar")
        return {"numeric_value": numeric, "currency": currency}
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state_input() from None


def _require_candidate_target(
    candidate: Ga4AdminKeyEventCreateCandidate,
    target: Ga4AdminEditInventoryTarget,
) -> None:
    identity = candidate.identity
    if (
        identity.property_ref != target.property_ref
        or identity.property_client_ref != target.property_client_ref
        or identity.web_stream_ref != target.web_stream_ref
        or identity.web_stream_client_ref != target.web_stream_client_ref
        or identity.api_version != target.admin_api_version
        or identity.required_scope != _EDIT_SCOPE
        or target.profile_purpose != "EDIT"
        or target.required_scope != _EDIT_SCOPE
        or candidate.target.parent != f"properties/{identity.property_client_ref}"
        or candidate.target.path != f"/v1alpha/properties/{identity.property_client_ref}/keyEvents"
    ):
        raise _identity_mismatch()


def _parse_projection(value: object) -> Ga4AdminEditKeyEventSemanticProjection:
    raw = _state_json_mapping(value, expected=Ga4AdminEditKeyEventSemanticProjection)
    _validate_projection_raw(raw)
    try:
        typed = dict(raw)
        typed["state"] = _parse_state_ref(cast(dict[str, object], raw["state"]))
        projection = Ga4AdminEditKeyEventSemanticProjection.model_validate(typed)
        _require_exact_model_shape(raw, projection)
        return projection
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_projection() from None


def _parse_create_state(value: object) -> Ga4AdminKeyEventCreateSemanticState:
    raw = _state_json_mapping(value, expected=Ga4AdminKeyEventCreateSemanticState)
    _validate_create_state_raw(raw)
    try:
        typed = dict(raw)
        typed["before"] = _parse_state_ref(cast(dict[str, object], raw["before"]))
        typed["after"] = _parse_state_ref(cast(dict[str, object], raw["after"]))
        typed["preconditions"] = _parse_preconditions(cast(dict[str, object], raw["preconditions"]))
        state = Ga4AdminKeyEventCreateSemanticState.model_validate(typed)
        _require_exact_model_shape(raw, state)
        return state
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_state() from None


def _state_json_mapping(value: object, *, expected: type[StrictModel]) -> dict[str, object]:
    return _semantic_state_json_mapping(value, expected=expected, bounds=_SEMANTIC_SPEC.bounds)


def _validate_projection_raw(data: dict[str, object]) -> None:
    _require_keys(data, _PROJECTION_FIELDS)
    if _exact_schema_version(data["schema_version"]) != 1:
        raise ValueError("projection schema version")
    _require_literal(data["projection_kind"], "GA4_ADMIN_EDIT_KEY_EVENT_SEMANTIC_PROJECTION")
    _require_literal(data["semantic_projection"], _SEMANTIC_PROJECTION)
    _exact_digest(data["target_binding_fingerprint"])
    _exact_digest(data["inventory_digest"])
    _validate_state_ref_raw(_require_mapping(data["state"]))
    _exact_int(data["member_count"], minimum=0, maximum=_MAX_ITEMS)
    _require_literal(data["inventory_scope"], "COMPLETE_SUPPLIED_EDIT_INVENTORY")
    _require_literal(data["fingerprint_key_continuity"], "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY")
    _require_literal(
        data["inventory_content_integrity"],
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT",
    )
    _require_literal(data["current_provider_state"], "NOT_ESTABLISHED")
    _require_literal(data["reservation_status"], "NOT_ESTABLISHED")
    _require_literal(data["permission_status"], "NOT_EVALUATED")
    _exact_false(data["remote_side_effects"])
    _exact_false(data["execution_authority"])


def _validate_create_state_raw(data: dict[str, object]) -> None:
    _require_keys(data, _CREATE_STATE_FIELDS)
    if _exact_schema_version(data["schema_version"]) != 1:
        raise ValueError("state schema version")
    _require_literal(data["state_kind"], _STATE_KIND)
    _require_literal(data["semantic_projection"], _SEMANTIC_PROJECTION)
    _exact_digest(data["target_binding_fingerprint"])
    candidate_digest = _exact_digest(data["candidate_digest"])
    inventory_digest = _exact_digest(data["inventory_digest"])
    before = _require_mapping(data["before"])
    after = _require_mapping(data["after"])
    _validate_state_ref_raw(before)
    _validate_state_ref_raw(after)
    if before["fingerprint"] == after["fingerprint"]:
        raise ValueError("state transition fingerprint")
    before_count = _exact_int(data["before_member_count"], minimum=0, maximum=_MAX_ITEMS)
    after_count = _exact_int(data["after_member_count"], minimum=1, maximum=_MAX_ITEMS + 1)
    if after_count != before_count + 1:
        raise ValueError("state member count")
    preconditions = _validate_preconditions_raw(_require_mapping(data["preconditions"]))
    if (
        before_count != preconditions.complete_observed_key_event_total
        or preconditions.candidate_digest != candidate_digest
        or preconditions.inventory_digest != inventory_digest
    ):
        raise ValueError("state precondition binding")


def _validate_preconditions_raw(
    data: dict[str, object],
) -> Ga4AdminEditKeyEventCreatePreconditions:
    _require_keys(data, _PRECONDITION_FIELDS)
    if _exact_schema_version(data["schema_version"]) != 1:
        raise ValueError("precondition schema version")
    _exact_digest(data["candidate_digest"])
    _exact_digest(data["inventory_digest"])
    expected_literals = {
        "inventory_scope": "COMPLETE_SUPPLIED_EDIT_INVENTORY",
        "fingerprint_key_continuity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY",
        "inventory_content_integrity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT",
        "observed_service_level": "GOOGLE_ANALYTICS_STANDARD",
        "capacity_status": "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        "duplicate_status": "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY",
        "current_provider_state": "NOT_ESTABLISHED",
        "reservation_status": "NOT_ESTABLISHED",
        "permission_status": "NOT_EVALUATED",
    }
    for field_name, expected in expected_literals.items():
        _require_literal(data[field_name], expected)
    total = _exact_int(data["complete_observed_key_event_total"], minimum=0, maximum=29)
    cap = _exact_int(data["documented_key_event_cap"], minimum=30, maximum=30)
    remaining = _exact_int(data["remaining_documented_slots"], minimum=1, maximum=30)
    if remaining != cap - total:
        raise ValueError("precondition capacity")
    _exact_false(data["remote_side_effects"])
    _exact_false(data["execution_authority"])
    try:
        checked = Ga4AdminEditKeyEventCreatePreconditions.model_validate(data)
        _require_exact_model_shape(data, checked)
        return checked
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("preconditions") from None


def _parse_preconditions(
    data: dict[str, object],
) -> Ga4AdminEditKeyEventCreatePreconditions:
    return _validate_preconditions_raw(data)


def _parse_state_ref(data: dict[str, object]) -> StateRef:
    _validate_state_ref_raw(data)
    try:
        state = StateRef.model_validate(data)
        _validate_state_ref_instance(state)
        _require_exact_model_shape(data, state)
        return state
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("state reference") from None


def _validate_state_ref_raw(data: dict[str, object]) -> None:
    _require_keys(data, frozenset({"fingerprint", "revision"}))
    _exact_digest(data["fingerprint"])
    _require_literal(data["revision"], _STATE_KIND)


def _validate_state_ref_instance(value: StateRef) -> None:
    if (
        type(value) is not StateRef
        or type(value.fingerprint) is not str
        or _DIGEST.fullmatch(value.fingerprint) is None
        or value.revision != _STATE_KIND
    ):
        raise ValueError("semantic state reference is invalid")


def _identity_mismatch() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_INVENTORY_IDENTITY_MISMATCH",
        "The fresh GA4 key-event input and supplied EDIT inventory target differ.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Collect a matching configured GA4 Admin EDIT inventory before planning.",
    )


def _invalid_state_input() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE_INPUT",
        "GA4 Admin key-event semantic state input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _invalid_projection() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_EDIT_SEMANTIC_PROJECTION",
        "GA4 Admin EDIT semantic projection is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _invalid_state() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE",
        "GA4 Admin key-event semantic state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


__all__ = [
    "Ga4AdminEditKeyEventSemanticProjection",
    "Ga4AdminKeyEventCreateSemanticState",
    "build_ga4_admin_key_event_create_semantic_state",
    "project_ga4_admin_edit_key_event_semantic_collection",
    "revalidate_ga4_admin_edit_key_event_semantic_projection",
    "revalidate_ga4_admin_key_event_create_semantic_state",
]

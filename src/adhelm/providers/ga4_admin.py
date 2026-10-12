"""Fixed, read-only GA4 Admin inventory for key events and web-stream rules."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

import httpx
from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    WithJsonSchema,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.models import Currency, SafeRef, StrictModel
from adhelm.transport import SafeHttpClient

_HOST = "analyticsadmin.googleapis.com"
_API_VERSION = "v1alpha"
_READONLY_SCOPE: Literal["https://www.googleapis.com/auth/analytics.readonly"] = (
    "https://www.googleapis.com/auth/analytics.readonly"
)
_PAGE_SIZE = 200
_MAX_PAGES = 5
_MAX_ITEMS = _PAGE_SIZE * _MAX_PAGES
_FINGERPRINT_CONTEXT = b"adhelm-ga4-admin-fingerprint-v1\x00"
_FINGERPRINT_SCHEME: Literal["HMAC_SHA256_LOCAL_V1"] = "HMAC_SHA256_LOCAL_V1"
_FINGERPRINT_KEY_MARKER_PURPOSE = "inventory-key-marker"
_FINGERPRINT_KEY_MARKER_CONTEXT = {
    "marker_purpose": "GA4_ADMIN_INVENTORY_KEY_CONTINUITY",
    "marker_version": 1,
}
_FINGERPRINT_INVENTORY_CONTENT_MARKER_PURPOSE = "inventory-content-marker"
_FINGERPRINT_KEY_MARKER_RE = re.compile(r"^[a-f0-9]{64}$")
_COMPARISON_TYPES = frozenset(
    {
        "COMPARISON_TYPE_UNSPECIFIED",
        "EQUALS",
        "EQUALS_CASE_INSENSITIVE",
        "CONTAINS",
        "CONTAINS_CASE_INSENSITIVE",
        "STARTS_WITH",
        "STARTS_WITH_CASE_INSENSITIVE",
        "ENDS_WITH",
        "ENDS_WITH_CASE_INSENSITIVE",
        "GREATER_THAN",
        "GREATER_THAN_OR_EQUAL",
        "LESS_THAN",
        "LESS_THAN_OR_EQUAL",
        "REGULAR_EXPRESSION",
        "REGULAR_EXPRESSION_CASE_INSENSITIVE",
    }
)

TokenGetter = Callable[[], str]
IdentityVerifier = Callable[["Ga4AdminIdentity"], bool]
ReferenceFor = Callable[
    [Literal["property", "web_stream", "key_event", "event_create_rule", "event_edit_rule"], str],
    str,
]


def _default_reference_for(
    kind: Literal["property", "web_stream", "key_event", "event_create_rule", "event_edit_rule"],
    value: str,
) -> str:
    return f"ga4_admin.{kind}.{value}"


class Ga4AdminIdentity(StrictModel):
    """Previously verified property and web-stream identity for a fixed Admin read."""

    schema_version: Literal[1] = 1
    property_ref: SafeRef
    property_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    web_stream_ref: SafeRef
    web_stream_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    admin_api_version: Literal["v1alpha"] = "v1alpha"
    required_scope: Literal["https://www.googleapis.com/auth/analytics.readonly"] = _READONLY_SCOPE


class Ga4AdminDefaultValue(StrictModel):
    """GA4's unconverted numeric default; Decimal preserves the reported scale."""

    numeric_value: Decimal
    currency: Currency

    @field_validator("numeric_value")
    @classmethod
    def finite_and_bounded(cls, value: Decimal) -> Decimal:
        if (
            not value.is_finite()
            or len(value.as_tuple().digits) > 100
            or abs(value.adjusted()) > 100
        ):
            raise ValueError("GA4 default numeric value is unsupported")
        return value


class Ga4AdminKeyEvent(StrictModel):
    resource_ref: SafeRef
    resource_client_ref: SafeRef
    property_ref: SafeRef
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_name_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint_scheme: Literal["HMAC_SHA256_LOCAL_V1"] = _FINGERPRINT_SCHEME
    counting_method: Literal["COUNTING_METHOD_UNSPECIFIED", "ONCE_PER_EVENT", "ONCE_PER_SESSION"]
    default_value: Ga4AdminDefaultValue | None = None
    custom: bool
    deletable: bool


class Ga4AdminEventRule(StrictModel):
    resource_ref: SafeRef
    resource_client_ref: SafeRef
    property_ref: SafeRef
    web_stream_ref: SafeRef
    rule_kind: Literal["EVENT_CREATE_RULE", "EVENT_EDIT_RULE"]
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint_scheme: Literal["HMAC_SHA256_LOCAL_V1"] = _FINGERPRINT_SCHEME
    condition_count: int = Field(ge=1, le=10)
    parameter_mutation_count: int = Field(ge=0, le=20)
    source_copy_parameters: bool | None = None
    processing_order: str | None = Field(
        default=None, pattern=r"^(?:0|-[1-9][0-9]{0,18}|[1-9][0-9]{0,18})$"
    )
    inspection_status: Literal["OPAQUE_RULE_CONTENT_UNSUPPORTED"] = (
        "OPAQUE_RULE_CONTENT_UNSUPPORTED"
    )

    @model_validator(mode="after")
    def kind_specific_metadata_is_complete(self) -> Ga4AdminEventRule:
        if self.rule_kind == "EVENT_CREATE_RULE":
            if self.source_copy_parameters is None or self.processing_order is not None:
                raise ValueError("GA4 event create rule metadata is inconsistent")
        elif self.source_copy_parameters is not None or self.processing_order is None:
            raise ValueError("GA4 event edit rule metadata is inconsistent")
        return self


class Ga4AdminInventory(StrictModel):
    """Closed normalized GA4 Admin inventory.

    The reader emits ``fingerprint_key_marker`` and
    ``inventory_content_marker`` only for fresh reads. Their absence remains
    meaningful for historical records written before either marker existed, so
    legacy serialization deliberately omits their default fields.
    """

    schema_version: Literal[1] = 1
    identity: Ga4AdminIdentity
    key_events: tuple[Ga4AdminKeyEvent, ...] = Field(default_factory=tuple, max_length=_MAX_ITEMS)
    event_create_rules: tuple[Ga4AdminEventRule, ...] = Field(
        default_factory=tuple, max_length=_MAX_ITEMS
    )
    event_edit_rules: tuple[Ga4AdminEventRule, ...] = Field(
        default_factory=tuple, max_length=_MAX_ITEMS
    )
    pagination_verified: Literal[True] = True
    # ``None`` is an internal sentinel for field absence in legacy history.
    # The published schema deliberately exposes an optional string field only:
    # explicit JSON null is invalid and must never be advertised as an input.
    fingerprint_key_marker: Annotated[
        str | None,
        WithJsonSchema({"type": "string", "pattern": r"^[a-f0-9]{64}$"}),
    ] = Field(default_factory=lambda: None)
    # ``None`` is an internal sentinel for a marker absent from older history.
    # A fresh reader HMACs the exact normalized inventory surface (including
    # the key marker) under the local fingerprint key; see
    # ``_fingerprint_inventory_content_marker`` below.
    inventory_content_marker: Annotated[
        str | None,
        WithJsonSchema({"type": "string", "pattern": r"^[a-f0-9]{64}$"}),
    ] = Field(default_factory=lambda: None)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("GA4 Admin inventory schema_version must be the integer 1")
        return value

    @field_validator("pagination_verified", mode="before")
    @classmethod
    def exact_pagination_verified(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("GA4 Admin inventory pagination_verified must be true")
        return value

    @field_validator("fingerprint_key_marker", "inventory_content_marker", mode="before")
    @classmethod
    def exact_fingerprint_markers(cls, value: object) -> str | None:
        # The default ``None`` represents an omitted legacy field.  The
        # after-validator below distinguishes it from an explicit JSON null.
        if value is None:
            return None
        if type(value) is not str or _FINGERPRINT_KEY_MARKER_RE.fullmatch(value) is None:
            raise ValueError("GA4 Admin inventory fingerprint marker must be lowercase SHA-256 hex")
        return value

    @model_validator(mode="after")
    def marker_is_absent_or_valid(self) -> Ga4AdminInventory:
        for field_name in ("fingerprint_key_marker", "inventory_content_marker"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError("GA4 Admin inventory marker cannot be explicit null")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_wire_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Keep inventories written before this marker byte-for-byte stable."""
        output = handler(self)
        if not isinstance(output, dict):
            raise ValueError("GA4 Admin inventory serialization must be an object")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("GA4 Admin inventory schema_version is invalid")
        if type(self.pagination_verified) is not bool or self.pagination_verified is not True:
            raise ValueError("GA4 Admin inventory pagination_verified is invalid")
        for field_name in ("fingerprint_key_marker", "inventory_content_marker"):
            marker = getattr(self, field_name)
            marker_supplied = field_name in self.model_fields_set
            if marker is None:
                if marker_supplied:
                    # ``model_construct`` and ``model_copy`` can bypass the
                    # raw validator. They must not silently serialize a new
                    # null field into a historical inventory.
                    raise ValueError("GA4 Admin inventory marker cannot be explicit null")
                output.pop(field_name, None)
            elif type(marker) is not str or _FINGERPRINT_KEY_MARKER_RE.fullmatch(marker) is None:
                raise ValueError("GA4 Admin inventory marker is invalid")
        return output


class Ga4AdminReadClient:
    """Fixture-tested v1alpha inventory; it has no mutation or passthrough surface."""

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        identity_verifier: IdentityVerifier,
        *,
        reference_for: ReferenceFor = _default_reference_for,
        fingerprint_key: bytes,
    ) -> None:
        if _HOST not in transport.allowed_hosts:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "GA4 Admin reader requires its fixed API host."
            )
        if not isinstance(fingerprint_key, bytes) or not 32 <= len(fingerprint_key) <= 4_096:
            raise AdhelmError(
                "FINGERPRINT_KEY_REQUIRED",
                "GA4 Admin inventory requires a private local fingerprint key.",
            )
        self._transport = transport
        self._token_getter = token_getter
        self._identity_verifier = identity_verifier
        self._reference_for = reference_for
        self._fingerprint_key = fingerprint_key

    def inventory(self, *, identity: Ga4AdminIdentity) -> Ga4AdminInventory:
        self._assert_session_identity(identity)
        property_parent = f"properties/{identity.property_client_ref}"
        web_stream_parent = f"{property_parent}/dataStreams/{identity.web_stream_client_ref}"
        headers = {"authorization": f"Bearer {_safe_token(self._token_getter)}"}
        key_events = self._list(
            parent=property_parent,
            collection="keyEvents",
            response_field="keyEvents",
            headers=headers,
            parser=lambda item: self._key_event(item, identity),
        )
        create_rules = self._list(
            parent=web_stream_parent,
            collection="eventCreateRules",
            response_field="eventCreateRules",
            headers=headers,
            parser=lambda item: self._event_rule(item, identity, "EVENT_CREATE_RULE"),
        )
        edit_rules = self._list(
            parent=web_stream_parent,
            collection="eventEditRules",
            response_field="eventEditRules",
            headers=headers,
            parser=lambda item: self._event_rule(item, identity, "EVENT_EDIT_RULE"),
        )
        # The measurement workflow serializes these collections by local
        # resource reference. Keep the reader's fresh shape in that same
        # deterministic order before sealing it, so downstream normalization
        # cannot invalidate the content marker.
        normalized_key_events = tuple(
            sorted(cast(list[Ga4AdminKeyEvent], key_events), key=lambda item: item.resource_ref)
        )
        normalized_create_rules = tuple(
            sorted(cast(list[Ga4AdminEventRule], create_rules), key=lambda item: item.resource_ref)
        )
        normalized_edit_rules = tuple(
            sorted(cast(list[Ga4AdminEventRule], edit_rules), key=lambda item: item.resource_ref)
        )
        inventory = Ga4AdminInventory(
            identity=identity,
            key_events=normalized_key_events,
            event_create_rules=normalized_create_rules,
            event_edit_rules=normalized_edit_rules,
            fingerprint_key_marker=_fingerprint_key_marker(self._fingerprint_key),
        )
        return Ga4AdminInventory(
            identity=inventory.identity,
            key_events=inventory.key_events,
            event_create_rules=inventory.event_create_rules,
            event_edit_rules=inventory.event_edit_rules,
            pagination_verified=inventory.pagination_verified,
            fingerprint_key_marker=inventory.fingerprint_key_marker,
            inventory_content_marker=_fingerprint_inventory_content_marker(
                self._fingerprint_key,
                inventory,
            ),
        )

    def _assert_session_identity(self, identity: Ga4AdminIdentity) -> None:
        try:
            locally_bound = identity.property_ref == self._reference_for(
                "property", identity.property_client_ref
            ) and identity.web_stream_ref == self._reference_for(
                "web_stream", identity.web_stream_client_ref
            )
        except Exception:
            locally_bound = False
        if not locally_bound:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "GA4 Admin identity references differ from their configured native IDs.",
            )
        try:
            verified = self._identity_verifier(identity)
        except Exception:
            verified = False
        if verified is not True:
            raise AdhelmError(
                "IDENTITY_VERIFICATION_REQUIRED",
                "GA4 Admin inventory requires a verified current property and web-stream identity.",
                next_action=(
                    "Verify the configured GA4 property and web stream before Admin inventory."
                ),
            )

    def _list(
        self,
        *,
        parent: str,
        collection: Literal["keyEvents", "eventCreateRules", "eventEditRules"],
        response_field: Literal["keyEvents", "eventCreateRules", "eventEditRules"],
        headers: dict[str, str],
        parser: Callable[[dict[str, Any]], Ga4AdminKeyEvent | Ga4AdminEventRule],
    ) -> list[Ga4AdminKeyEvent | Ga4AdminEventRule]:
        items: list[Ga4AdminKeyEvent | Ga4AdminEventRule] = []
        seen_resources: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
            if page_token is not None:
                params["pageToken"] = page_token
            response = self._transport.request(
                "GET",
                f"https://{_HOST}/{_API_VERSION}/{parent}/{collection}",
                headers=headers,
                params=params,
            )
            payload = _json(response, collection)
            _keys(payload, {response_field, "nextPageToken"}, f"{collection} list")
            page = payload.get(response_field, [])
            if not isinstance(page, list) or len(page) > _PAGE_SIZE:
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 Admin list page is invalid.")
            for raw in page:
                if not isinstance(raw, dict):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "GA4 Admin list item is invalid."
                    )
                item = parser(raw)
                if item.resource_client_ref in seen_resources:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "GA4 Admin list contains a duplicate resource."
                    )
                seen_resources.add(item.resource_client_ref)
                items.append(item)
            if len(items) > _MAX_ITEMS:
                raise AdhelmError("PAGINATION_LIMIT", "GA4 Admin inventory exceeds the item bound.")
            next_token = payload.get("nextPageToken")
            if next_token is None:
                return items
            page_token = _page_token(next_token)
            if page_token in seen_tokens:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 Admin pagination made no safe progress."
                )
            seen_tokens.add(page_token)
        raise AdhelmError("PAGINATION_LIMIT", "GA4 Admin inventory exceeded the page bound.")

    def _key_event(self, value: dict[str, Any], identity: Ga4AdminIdentity) -> Ga4AdminKeyEvent:
        return _parse_ga4_admin_key_event(
            value,
            property_id=identity.property_client_ref,
            property_ref=identity.property_ref,
            fingerprint_key=self._fingerprint_key,
            reference_for=self._reference_for,
        )

    def _event_rule(
        self,
        value: dict[str, Any],
        identity: Ga4AdminIdentity,
        kind: Literal["EVENT_CREATE_RULE", "EVENT_EDIT_RULE"],
    ) -> Ga4AdminEventRule:
        collection = "eventCreateRules" if kind == "EVENT_CREATE_RULE" else "eventEditRules"
        client_ref = _rule_id(
            value.get("name"),
            identity.property_client_ref,
            identity.web_stream_client_ref,
            collection,
        )
        if kind == "EVENT_CREATE_RULE":
            _keys(
                value,
                {
                    "name",
                    "destinationEvent",
                    "eventConditions",
                    "sourceCopyParameters",
                    "parameterMutations",
                },
                "event create rule",
            )
            _destination_event_name(value.get("destinationEvent"))
            source_copy = value.get("sourceCopyParameters", False)
            if not isinstance(source_copy, bool):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 rule source-copy setting is invalid."
                )
            processing: str | None = None
        else:
            _keys(
                value,
                {
                    "name",
                    "displayName",
                    "eventConditions",
                    "parameterMutations",
                    "processingOrder",
                },
                "event edit rule",
            )
            _bounded_string(value.get("displayName"), "event edit display name", maximum=255)
            source_copy = None
            processing = _signed_int64_string(
                value.get("processingOrder"), "event edit processing order"
            )
        conditions = _conditions(value.get("eventConditions"))
        mutations = _mutations(value.get("parameterMutations", []))
        return Ga4AdminEventRule(
            resource_ref=self._reference_for(
                "event_create_rule" if kind == "EVENT_CREATE_RULE" else "event_edit_rule",
                client_ref,
            ),
            resource_client_ref=client_ref,
            property_ref=identity.property_ref,
            web_stream_ref=identity.web_stream_ref,
            rule_kind=kind,
            configuration_fingerprint=_fingerprint(self._fingerprint_key, "event-rule", value),
            condition_count=len(conditions),
            parameter_mutation_count=len(mutations),
            source_copy_parameters=source_copy,
            processing_order=processing,
        )


def _parse_ga4_admin_key_event(
    value: dict[str, Any],
    *,
    property_id: str,
    property_ref: SafeRef,
    fingerprint_key: bytes,
    reference_for: ReferenceFor,
) -> Ga4AdminKeyEvent:
    """Normalize one fixed key-event response for either Admin read role.

    Both the historical READ inventory and the distinct EDIT preparation
    inventory use the same privacy-bounded member representation and HMAC
    domains.  The caller owns role-specific identity validation and paging.
    """

    _keys(
        value,
        {
            "name",
            "eventName",
            "createTime",
            "deletable",
            "custom",
            "countingMethod",
            "defaultValue",
        },
        "key event",
    )
    required = {"name", "eventName", "deletable", "custom", "countingMethod"}
    if not required <= set(value):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 key event response is incomplete.")
    client_ref = _key_event_id(value.get("name"), property_id)
    event_name = _event_name(value.get("eventName"), "key event name")
    counting = value.get("countingMethod")
    if counting not in {"COUNTING_METHOD_UNSPECIFIED", "ONCE_PER_EVENT", "ONCE_PER_SESSION"}:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 key event counting method is invalid.")
    if type(value.get("custom")) is not bool or type(value.get("deletable")) is not bool:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 key event flags are invalid.")
    create_time = value.get("createTime")
    if create_time is not None:
        _timestamp(create_time, "key event create time")
    return Ga4AdminKeyEvent(
        resource_ref=reference_for("key_event", client_ref),
        resource_client_ref=client_ref,
        property_ref=property_ref,
        configuration_fingerprint=_fingerprint(fingerprint_key, "key-event", value),
        event_name_fingerprint=_fingerprint(fingerprint_key, "key-event-name", event_name),
        counting_method=counting,
        default_value=_default_value(value.get("defaultValue")),
        custom=value["custom"],
        deletable=value["deletable"],
    )


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 Admin authorization is unavailable.",
            next_action="Authorize the configured local GA4 Admin read profile.",
        )
    return token


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        payload = parse_json(response.content, maximum_bytes=8_388_608, decimal_numbers=True)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} response is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 Admin response has an invalid shape.")
    return payload


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} included an unsupported field."
        )


def _bounded_string(value: Any, context: str, *, maximum: int, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} is invalid.")
    return value


def _event_name(value: Any, context: str) -> str:
    parsed = _bounded_string(value, context, maximum=40)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", parsed):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} is invalid.")
    return parsed


def _destination_event_name(value: Any) -> str:
    parsed = _bounded_string(value, "event rule destination", maximum=39)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,38}", parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 event rule destination is invalid.")
    return parsed


def _timestamp(value: Any, context: str) -> None:
    parsed = _bounded_string(value, context, maximum=64)
    try:
        timestamp = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except ValueError:
        timestamp = None
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} is invalid.")


def _page_token(value: Any) -> str:
    return _bounded_string(value, "page token", maximum=2_048)


def _key_event_id(value: Any, property_id: str) -> str:
    return _resource_id(value, f"properties/{property_id}/keyEvents/", "key event")


def _rule_id(value: Any, property_id: str, stream_id: str, collection: str) -> str:
    return _resource_id(
        value,
        f"properties/{property_id}/dataStreams/{stream_id}/{collection}/",
        "event rule",
    )


def _resource_id(value: Any, prefix: str, context: str) -> str:
    name = _bounded_string(value, f"{context} name", maximum=256)
    client_ref = name.removeprefix(prefix)
    if not name.startswith(prefix) or not re.fullmatch(r"[0-9]+", client_ref):
        raise AdhelmError(
            "IDENTITY_MISMATCH", f"GA4 Admin {context} is outside the configured parent."
        )
    return client_ref


def _default_value(value: Any) -> Ga4AdminDefaultValue | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 key event default value is invalid.")
    _keys(value, {"numericValue", "currencyCode"}, "key event default value")
    numeric = value.get("numericValue")
    if isinstance(numeric, bool) or not isinstance(numeric, (int, Decimal)):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 key event default number is invalid.")
    amount = Decimal(numeric)
    currency = _bounded_string(value.get("currencyCode"), "key event default currency", maximum=3)
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 key event default currency is invalid.")
    return Ga4AdminDefaultValue(numeric_value=amount, currency=currency)


def _rule_items(value: Any, context: str, *, minimum: int, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} are invalid.")
    if not all(isinstance(item, dict) for item in value):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} are invalid.")
    return value


def _conditions(value: Any) -> list[dict[str, Any]]:
    conditions = _rule_items(value, "event conditions", minimum=1, maximum=10)
    for condition in conditions:
        _keys(condition, {"field", "comparisonType", "value", "negated"}, "event condition")
        field = _bounded_string(condition.get("field"), "event condition field", maximum=40)
        if any(character.isspace() for character in field):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 event condition is invalid.")
        comparison = condition.get("comparisonType")
        if comparison not in _COMPARISON_TYPES:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 event condition is invalid.")
        _bounded_string(
            condition.get("value"), "event condition value", maximum=1_024, allow_empty=True
        )
        if not isinstance(condition.get("negated"), bool):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 event condition is invalid.")
    return conditions


def _mutations(value: Any) -> list[dict[str, Any]]:
    mutations = _rule_items(value, "parameter mutations", minimum=0, maximum=20)
    names: set[str] = set()
    for mutation in mutations:
        _keys(mutation, {"parameter", "parameterValue"}, "parameter mutation")
        parameter = _bounded_string(
            mutation.get("parameter"), "parameter mutation name", maximum=39
        )
        if re.fullmatch(r"[A-Za-z0-9_]+", parameter) is None or parameter in names:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 parameter mutation is invalid.")
        names.add(parameter)
        _bounded_string(
            mutation.get("parameterValue"),
            "parameter mutation value",
            maximum=99,
            allow_empty=True,
        )
    return mutations


def _signed_int64_string(value: Any, context: str) -> str:
    parsed = _bounded_string(value, context, maximum=20)
    if (
        re.fullmatch(r"(?:0|-[1-9][0-9]{0,18}|[1-9][0-9]{0,18})", parsed) is None
        or not -(2**63) <= int(parsed) <= 2**63 - 1
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 Admin {context} is invalid.")
    return parsed


def _fingerprint(key: bytes, purpose: str, value: Any) -> str:
    """Key transient provider content so inventories cannot expose guessable hashes."""
    material = _FINGERPRINT_CONTEXT + purpose.encode("ascii") + b"\x00"
    material += canonical_bytes(_fingerprint_value(value))
    return hmac.new(
        key,
        material,
        hashlib.sha256,
    ).hexdigest()


def _fingerprint_key_marker(key: bytes) -> str:
    """Return a fixed, nonsecret proof that two inventories used one local key.

    The HMAC input is intentionally independent of GA4 property IDs, event
    names, and every provider response.  It is a continuity marker only: it
    neither exposes the fingerprint key nor proves a current remote identity.
    """
    return _fingerprint(
        key,
        _FINGERPRINT_KEY_MARKER_PURPOSE,
        _FINGERPRINT_KEY_MARKER_CONTEXT,
    )


def _fingerprint_inventory_content_marker(key: bytes, inventory: Ga4AdminInventory) -> str:
    """Authenticate the full normalized inventory surface under the local key.

    This is intentionally distinct from the static key-continuity marker. The
    input includes identity, every normalized member, pagination declaration,
    and the key marker, while excluding only this self-referential field.
    Matching it proves local keyed content integrity, not provider origin,
    freshness, current permissions, or any fact outside the supplied object.
    """

    surface = inventory.model_dump(mode="json", warnings="error")
    if not isinstance(surface, dict):
        raise ValueError("GA4 Admin inventory serialization must be an object")
    surface.pop("inventory_content_marker", None)
    return _fingerprint(key, _FINGERPRINT_INVENTORY_CONTENT_MARKER_PURPOSE, surface)


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, dict):
        return {key: _fingerprint_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_fingerprint_value(child) for child in value]
    return value

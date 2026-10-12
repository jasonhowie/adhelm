"""Fixed read-only GA4 Admin EDIT key-event inventory and capacity facts.

This adapter has no mutation method.  It captures one configured EDIT-token
snapshot, proves the selected property and web stream through the fixed access
client, then lists only the bounded key-event collection under that same token.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Currency, SafeRef, StrictModel
from adhelm.providers.ga4_admin import (
    Ga4AdminKeyEvent,
    _fingerprint,
    _fingerprint_key_marker,
    _json,
    _keys,
    _page_token,
    _parse_ga4_admin_key_event,
)
from adhelm.providers.ga4_admin_access import (
    Ga4AdminAccessClient,
    Ga4AdminAccessIdentity,
    Ga4ServiceLevel,
)
from adhelm.strict_json import validate_json_value
from adhelm.transport import SafeHttpClient

_HOST = "analyticsadmin.googleapis.com"
_API_VERSION: Literal["v1alpha"] = "v1alpha"
_EDIT_SCOPE: Literal["https://www.googleapis.com/auth/analytics.edit"] = (
    "https://www.googleapis.com/auth/analytics.edit"
)
_PAGE_SIZE = 200
_MAX_PAGES = 5
_MAX_ITEMS = _PAGE_SIZE * _MAX_PAGES
_STANDARD_KEY_EVENT_CAP: Literal[30] = 30
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_NATIVE_ID = re.compile(r"^[0-9]{1,20}$")
_DECIMAL_STRING = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_CONTENT_MARKER_PURPOSE = "edit-key-event-inventory-content-marker"
_KEY_EVENT_FIELDS = (
    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),"
    "nextPageToken"
)
_MAX_MODEL_JSON_BYTES = 1_048_576
_MAX_JSON_NODES = 20_000
_MAX_JSON_DEPTH = 16
_MAX_JSON_STRING_BYTES = 16_384

TokenGetter = Callable[[], str]


def _exact_schema_version(value: object) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")
    return value


class Ga4AdminEditInventoryTarget(StrictModel):
    """Closed configured target for one non-mutating EDIT-role inventory."""

    schema_version: Literal[1] = 1
    profile_purpose: Literal["EDIT"] = "EDIT"
    property_ref: SafeRef
    property_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    web_stream_ref: SafeRef
    web_stream_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    expected_timezone: str = Field(min_length=1, max_length=80)
    expected_currency: Currency
    admin_api_version: Literal["v1alpha"] = _API_VERSION
    required_scope: Literal["https://www.googleapis.com/auth/analytics.edit"] = _EDIT_SCOPE

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version(value)

    @field_validator("expected_timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("expected_timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def deterministic_local_identity(self) -> Ga4AdminEditInventoryTarget:
        if (
            self.property_ref != f"ga4_admin.property.{self.property_client_ref}"
            or self.web_stream_ref != f"ga4_admin.web_stream.{self.web_stream_client_ref}"
        ):
            raise ValueError("GA4 Admin EDIT references must bind to their native IDs")
        return self


class Ga4AdminEditStandardKeyEventCapacity(StrictModel):
    """Observed complete count against the documented Standard-property limit."""

    schema_version: Literal[1] = 1
    observed_service_level: Literal["GOOGLE_ANALYTICS_STANDARD"] = "GOOGLE_ANALYTICS_STANDARD"
    complete_observed_key_event_total: int = Field(ge=0, le=_MAX_ITEMS)
    documented_key_event_cap: Literal[30] = _STANDARD_KEY_EVENT_CAP
    remaining_documented_slots: int = Field(ge=0, le=_STANDARD_KEY_EVENT_CAP)
    capacity_status: Literal[
        "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        "AT_OR_ABOVE_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
    ]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version(value)

    @model_validator(mode="after")
    def exact_capacity_facts(self) -> Ga4AdminEditStandardKeyEventCapacity:
        expected_slots = max(0, _STANDARD_KEY_EVENT_CAP - self.complete_observed_key_event_total)
        expected_status = (
            "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP"
            if self.complete_observed_key_event_total < _STANDARD_KEY_EVENT_CAP
            else "AT_OR_ABOVE_DOCUMENTED_STANDARD_KEY_EVENT_CAP"
        )
        if (
            self.remaining_documented_slots != expected_slots
            or self.capacity_status != expected_status
        ):
            raise ValueError("Standard key-event capacity facts are inconsistent")
        return self


class Ga4AdminEditKeyEventCapacityNotEvidenced(StrictModel):
    """A complete count with no invented cap for 360 or unavailable service level."""

    schema_version: Literal[1] = 1
    observed_service_level: Literal["GOOGLE_ANALYTICS_360", "NOT_AVAILABLE"]
    complete_observed_key_event_total: int = Field(ge=0, le=_MAX_ITEMS)
    capacity_status: Literal["CAP_NOT_EVIDENCED_FOR_OBSERVED_SERVICE_LEVEL"] = (
        "CAP_NOT_EVIDENCED_FOR_OBSERVED_SERVICE_LEVEL"
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version(value)


Ga4AdminEditKeyEventCapacity = (
    Ga4AdminEditStandardKeyEventCapacity | Ga4AdminEditKeyEventCapacityNotEvidenced
)


class _Ga4AdminEditKeyEventInventorySurface(StrictModel):
    """The content-marker surface, deliberately excluding the marker itself."""

    schema_version: Literal[1] = 1
    inventory_kind: Literal["GA4_ADMIN_EDIT_KEY_EVENT_INVENTORY"] = (
        "GA4_ADMIN_EDIT_KEY_EVENT_INVENTORY"
    )
    target: Ga4AdminEditInventoryTarget
    capacity: Ga4AdminEditKeyEventCapacity
    key_events: tuple[Ga4AdminKeyEvent, ...] = Field(max_length=_MAX_ITEMS)
    pagination_verified: Literal[True] = True
    fingerprint_key_marker: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version(value)

    @field_validator("fingerprint_key_marker", mode="before")
    @classmethod
    def exact_marker(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("fingerprint_key_marker must be lowercase SHA-256 hex")
        return value

    @field_validator("pagination_verified", mode="before")
    @classmethod
    def exact_pagination_verified(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("pagination_verified must be true")
        return value

    @model_validator(mode="after")
    def complete_count_and_members_agree(self) -> _Ga4AdminEditKeyEventInventorySurface:
        if len(self.key_events) != self.capacity.complete_observed_key_event_total:
            raise ValueError("complete key-event count differs from members")
        expected_property_ref = self.target.property_ref
        seen_client_refs: set[str] = set()
        for key_event in self.key_events:
            if (
                key_event.property_ref != expected_property_ref
                or key_event.resource_ref != f"ga4_admin.key_event.{key_event.resource_client_ref}"
                or key_event.resource_client_ref in seen_client_refs
            ):
                raise ValueError("key-event membership is inconsistent")
            seen_client_refs.add(key_event.resource_client_ref)
        return self


class Ga4AdminEditKeyEventInventory(_Ga4AdminEditKeyEventInventorySurface):
    """Fresh, bounded EDIT-role read facts; never a create permission or plan."""

    inventory_content_marker: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("inventory_content_marker", mode="before")
    @classmethod
    def exact_content_marker(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("inventory_content_marker must be lowercase SHA-256 hex")
        return value

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_state(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Do not serialize a forged model-copy state with an old valid marker."""

        _validate_edit_inventory_components(self)
        output = handler(self)
        if type(output) is not dict:
            raise ValueError("GA4 Admin EDIT inventory serialization must be an object")
        return output


class Ga4AdminEditKeyEventInventoryClient:
    """Fixed v1alpha key-event GET inventory under one captured EDIT token."""

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        *,
        fingerprint_key: bytes,
    ) -> None:
        if transport.allowed_hosts != frozenset({_HOST}) or transport.client.params:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "GA4 Admin EDIT inventory requires its fixed host and no default query parameters.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        _require_fingerprint_key(fingerprint_key)
        self._transport = transport
        self._token_getter = token_getter
        self._fingerprint_key = fingerprint_key

    def read_inventory(self, target: Ga4AdminEditInventoryTarget) -> Ga4AdminEditKeyEventInventory:
        """Verify current fixed identity, then list key events with the same token.

        This verifies only the property and selected web-stream GET results
        immediately before the collection list. It does not establish OAuth
        grant completeness, create permission, a durable reservation, current
        state after return, authorization, or any mutation outcome.
        """

        checked_target = _revalidate_target(target)
        token = _safe_edit_token(self._token_getter)
        access_identity = _revalidate_access_identity(
            Ga4AdminAccessClient(self._transport, lambda: token).read_identity(
                property_id=checked_target.property_client_ref,
                stream_id=checked_target.web_stream_client_ref,
            )
        )
        _require_matching_access_identity(checked_target, access_identity)
        key_events = self._list_key_events(checked_target, token)
        ordered_events = tuple(sorted(key_events, key=lambda item: item.resource_ref))
        capacity = _capacity(access_identity.service_level, len(ordered_events))
        surface = _Ga4AdminEditKeyEventInventorySurface(
            target=checked_target,
            capacity=capacity,
            key_events=ordered_events,
            fingerprint_key_marker=_fingerprint_key_marker(self._fingerprint_key),
        )
        return Ga4AdminEditKeyEventInventory(
            **surface.model_dump(mode="python", warnings="error"),
            inventory_content_marker=_fingerprint_edit_inventory_content_marker(
                self._fingerprint_key,
                surface,
            ),
        )

    def _list_key_events(
        self, target: Ga4AdminEditInventoryTarget, token: str
    ) -> list[Ga4AdminKeyEvent]:
        headers = {"authorization": f"Bearer {token}"}
        items: list[Ga4AdminKeyEvent] = []
        seen_resources: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"pageSize": _PAGE_SIZE, "fields": _KEY_EVENT_FIELDS}
            if page_token is not None:
                params["pageToken"] = page_token
            payload = _json(
                self._transport.request(
                    "GET",
                    f"https://{_HOST}/{_API_VERSION}/properties/{target.property_client_ref}/keyEvents",
                    headers=headers,
                    params=params,
                ),
                "EDIT key-event list",
            )
            _keys(payload, {"keyEvents", "nextPageToken"}, "EDIT key-event list")
            page = payload.get("keyEvents", [])
            if type(page) is not list or len(page) > _PAGE_SIZE:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 Admin EDIT key-event list page is invalid."
                )
            for value in page:
                if type(value) is not dict:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID",
                        "GA4 Admin EDIT key-event list item is invalid.",
                    )
                item = _parse_ga4_admin_key_event(
                    cast(dict[str, Any], value),
                    property_id=target.property_client_ref,
                    property_ref=target.property_ref,
                    fingerprint_key=self._fingerprint_key,
                    reference_for=_default_reference_for,
                )
                if item.resource_client_ref in seen_resources:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID",
                        "GA4 Admin EDIT key-event list contains a duplicate resource.",
                    )
                seen_resources.add(item.resource_client_ref)
                items.append(item)
            if len(items) > _MAX_ITEMS:
                raise AdhelmError(
                    "PAGINATION_LIMIT", "GA4 Admin EDIT key-event inventory exceeds the item bound."
                )
            next_token = payload.get("nextPageToken")
            if next_token is None:
                return items
            page_token = _page_token(next_token)
            if page_token in seen_tokens:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "GA4 Admin EDIT key-event pagination made no safe progress.",
                )
            seen_tokens.add(page_token)
        raise AdhelmError(
            "PAGINATION_LIMIT", "GA4 Admin EDIT key-event inventory exceeded the page bound."
        )


def _default_reference_for(
    kind: Literal["property", "web_stream", "key_event", "event_create_rule", "event_edit_rule"],
    value: str,
) -> str:
    if kind != "key_event":
        raise ValueError("EDIT key-event parser cannot resolve another resource kind")
    return f"ga4_admin.{kind}.{value}"


def _capacity(service_level: Ga4ServiceLevel, count: int) -> Ga4AdminEditKeyEventCapacity:
    if service_level == "GOOGLE_ANALYTICS_STANDARD":
        return Ga4AdminEditStandardKeyEventCapacity(
            complete_observed_key_event_total=count,
            remaining_documented_slots=max(0, _STANDARD_KEY_EVENT_CAP - count),
            capacity_status=(
                "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP"
                if count < _STANDARD_KEY_EVENT_CAP
                else "AT_OR_ABOVE_DOCUMENTED_STANDARD_KEY_EVENT_CAP"
            ),
        )
    return Ga4AdminEditKeyEventCapacityNotEvidenced(
        observed_service_level=service_level,
        complete_observed_key_event_total=count,
    )


def _fingerprint_edit_inventory_content_marker(
    key: bytes, surface: _Ga4AdminEditKeyEventInventorySurface
) -> str:
    """Bind this distinct EDIT inventory surface under the local fingerprint key."""

    value = surface.model_dump(mode="json", warnings="error")
    if type(value) is not dict:
        raise ValueError("GA4 Admin EDIT inventory serialization must be an object")
    value.pop("inventory_content_marker", None)
    return _fingerprint(key, _CONTENT_MARKER_PURPOSE, value)


def revalidate_ga4_admin_edit_key_event_inventory(
    value: object, *, fingerprint_key: bytes
) -> Ga4AdminEditKeyEventInventory:
    """Strictly rebuild and authenticate a retained EDIT inventory.

    This boundary accepts only a JSON-shaped mapping or the exact typed
    inventory model. It checks original scalar types before model validation
    and recomputes the content marker under the supplied transient key. A
    matching marker is local keyed-content integrity only; it is never a
    provider-origin, freshness, permission, or execution-authority claim.
    """

    _require_fingerprint_key(fingerprint_key)
    try:
        raw = _inventory_json_mapping(value)
        inventory = validate_json_value(Ga4AdminEditKeyEventInventory, raw)
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise AdhelmError(
            "INVALID_GA4_ADMIN_EDIT_INVENTORY",
            "GA4 Admin EDIT inventory is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None
    expected_key_marker = _fingerprint_key_marker(fingerprint_key)
    if not hmac.compare_digest(inventory.fingerprint_key_marker, expected_key_marker):
        raise AdhelmError(
            "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH",
            "GA4 Admin EDIT inventory was not fingerprinted with the current local key.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Collect a new normalized GA4 Admin EDIT key-event inventory.",
        )
    expected_marker = _fingerprint_edit_inventory_content_marker(fingerprint_key, inventory)
    if not hmac.compare_digest(inventory.inventory_content_marker, expected_marker):
        raise AdhelmError(
            "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH",
            "GA4 Admin EDIT inventory content does not match the current local fingerprint key.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Collect a new normalized GA4 Admin EDIT key-event inventory.",
        )
    return inventory


def _safe_edit_token(getter: TokenGetter) -> str:
    try:
        token = getter()
    except Exception:
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 Admin EDIT authorization is unavailable.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Authorize the configured local GA4 Admin EDIT profile.",
        ) from None
    if (
        type(token) is not str
        or not token
        or len(token) > 16_384
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 Admin EDIT authorization is unavailable.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Authorize the configured local GA4 Admin EDIT profile.",
        )
    return token


def _require_fingerprint_key(value: object) -> bytes:
    if type(value) is not bytes or not 32 <= len(value) <= 4_096:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "GA4 Admin EDIT inventory requires a private local fingerprint key.",
            exit_code=ExitCode.PRECONDITION,
        )
    return value


def _inventory_json_mapping(value: object) -> dict[str, Any]:
    if type(value) is Ga4AdminEditKeyEventInventory:
        raw = value.model_dump_json(warnings="error")
        if len(raw.encode("utf-8")) > _MAX_MODEL_JSON_BYTES:
            raise ValueError("inventory size")
        parsed = parse_json(raw, maximum_bytes=_MAX_MODEL_JSON_BYTES)
        if type(parsed) is not dict:
            raise ValueError("inventory shape")
        return cast(dict[str, Any], parsed)
    if type(value) is not dict:
        raise ValueError("inventory type")
    expected_root = {
        "schema_version",
        "inventory_kind",
        "target",
        "capacity",
        "key_events",
        "pagination_verified",
        "fingerprint_key_marker",
        "inventory_content_marker",
    }
    if set(value) != expected_root:
        raise ValueError("inventory keys")
    pending: list[tuple[object, int]] = [(value, 0)]
    node_count = 0
    string_bytes = 0
    while pending:
        current, depth = pending.pop()
        node_count += 1
        if node_count > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("inventory nesting")
        if type(current) is dict:
            if any(type(key) is not str for key in current):
                raise ValueError("inventory key")
            pending.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            if len(current) > _MAX_ITEMS:
                raise ValueError("inventory array")
            pending.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            encoded_size = len(current.encode("utf-8"))
            string_bytes += encoded_size
            if encoded_size > _MAX_JSON_STRING_BYTES or string_bytes > _MAX_MODEL_JSON_BYTES:
                raise ValueError("inventory string bound")
        elif type(current) is int:
            if current.bit_length() > 53:
                raise ValueError("inventory integer bound")
        elif type(current) not in {str, int, float, bool, type(None)}:
            raise ValueError("inventory JSON scalar")
    _validate_edit_inventory_raw(cast(dict[str, object], value))
    return cast(dict[str, Any], value)


def _require_raw_keys(data: dict[str, object], expected: frozenset[str]) -> None:
    if set(data) != expected:
        raise ValueError("inventory object has an invalid closed shape")


def _require_raw_string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("inventory expected a JSON string")
    return value


def _require_raw_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("inventory expected a JSON integer")
    return value


def _require_raw_bool(value: object, *, expected: bool | None = None) -> bool:
    if type(value) is not bool or (expected is not None and value is not expected):
        raise ValueError("inventory expected an exact JSON boolean")
    return value


def _require_raw_digest(value: object) -> str:
    rendered = _require_raw_string(value)
    if _DIGEST.fullmatch(rendered) is None:
        raise ValueError("inventory expected a lowercase digest")
    return rendered


def _require_raw_native_id(value: object) -> str:
    rendered = _require_raw_string(value)
    if _NATIVE_ID.fullmatch(rendered) is None:
        raise ValueError("inventory expected a native numeric identifier")
    return rendered


def _require_raw_mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("inventory expected an object")
    return cast(dict[str, object], value)


def _require_raw_list(value: object, *, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise ValueError("inventory expected a bounded array")
    return cast(list[object], value)


def _validate_edit_inventory_raw(data: dict[str, object]) -> None:
    """Reject raw type aliases before model parsing or marker verification."""

    _require_raw_keys(
        data,
        frozenset(
            {
                "schema_version",
                "inventory_kind",
                "target",
                "capacity",
                "key_events",
                "pagination_verified",
                "fingerprint_key_marker",
                "inventory_content_marker",
            }
        ),
    )
    if _require_raw_int(data["schema_version"]) != 1:
        raise ValueError("inventory schema version")
    if _require_raw_string(data["inventory_kind"]) != "GA4_ADMIN_EDIT_KEY_EVENT_INVENTORY":
        raise ValueError("inventory kind")
    _validate_edit_target_raw(_require_raw_mapping(data["target"]))
    _validate_edit_capacity_raw(_require_raw_mapping(data["capacity"]))
    _require_raw_bool(data["pagination_verified"], expected=True)
    _require_raw_digest(data["fingerprint_key_marker"])
    _require_raw_digest(data["inventory_content_marker"])
    for item in _require_raw_list(data["key_events"], maximum=_MAX_ITEMS):
        _validate_edit_key_event_raw(_require_raw_mapping(item))


def _validate_edit_target_raw(data: dict[str, object]) -> None:
    _require_raw_keys(
        data,
        frozenset(
            {
                "schema_version",
                "profile_purpose",
                "property_ref",
                "property_client_ref",
                "web_stream_ref",
                "web_stream_client_ref",
                "expected_timezone",
                "expected_currency",
                "admin_api_version",
                "required_scope",
            }
        ),
    )
    if _require_raw_int(data["schema_version"]) != 1:
        raise ValueError("target schema version")
    if _require_raw_string(data["profile_purpose"]) != "EDIT":
        raise ValueError("target profile purpose")
    property_client_ref = _require_raw_native_id(data["property_client_ref"])
    web_stream_client_ref = _require_raw_native_id(data["web_stream_client_ref"])
    if (
        _require_raw_string(data["property_ref"]) != f"ga4_admin.property.{property_client_ref}"
        or _require_raw_string(data["web_stream_ref"])
        != f"ga4_admin.web_stream.{web_stream_client_ref}"
        or _require_raw_string(data["admin_api_version"]) != _API_VERSION
        or _require_raw_string(data["required_scope"]) != _EDIT_SCOPE
    ):
        raise ValueError("target controls")
    _require_raw_string(data["expected_timezone"])
    if _CURRENCY.fullmatch(_require_raw_string(data["expected_currency"])) is None:
        raise ValueError("target currency")


def _validate_edit_capacity_raw(data: dict[str, object]) -> None:
    service_level = _require_raw_string(data.get("observed_service_level"))
    if service_level == "GOOGLE_ANALYTICS_STANDARD":
        _require_raw_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "observed_service_level",
                    "complete_observed_key_event_total",
                    "documented_key_event_cap",
                    "remaining_documented_slots",
                    "capacity_status",
                }
            ),
        )
        if (
            _require_raw_int(data["schema_version"]) != 1
            or _require_raw_int(data["documented_key_event_cap"]) != _STANDARD_KEY_EVENT_CAP
        ):
            raise ValueError("Standard capacity controls")
        _require_raw_int(data["complete_observed_key_event_total"])
        _require_raw_int(data["remaining_documented_slots"])
        _require_raw_string(data["capacity_status"])
        return
    if service_level in {"GOOGLE_ANALYTICS_360", "NOT_AVAILABLE"}:
        _require_raw_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "observed_service_level",
                    "complete_observed_key_event_total",
                    "capacity_status",
                }
            ),
        )
        if _require_raw_int(data["schema_version"]) != 1:
            raise ValueError("capacity schema version")
        _require_raw_int(data["complete_observed_key_event_total"])
        if (
            _require_raw_string(data["capacity_status"])
            != "CAP_NOT_EVIDENCED_FOR_OBSERVED_SERVICE_LEVEL"
        ):
            raise ValueError("capacity status")
        return
    raise ValueError("capacity service level")


def _validate_edit_key_event_raw(data: dict[str, object]) -> None:
    _require_raw_keys(
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
    resource_client_ref = _require_raw_native_id(data["resource_client_ref"])
    if (
        _require_raw_string(data["resource_ref"]) != f"ga4_admin.key_event.{resource_client_ref}"
        or _require_raw_string(data["fingerprint_scheme"]) != "HMAC_SHA256_LOCAL_V1"
        or _require_raw_string(data["counting_method"])
        not in {"COUNTING_METHOD_UNSPECIFIED", "ONCE_PER_EVENT", "ONCE_PER_SESSION"}
    ):
        raise ValueError("key-event controls")
    _require_raw_string(data["property_ref"])
    _require_raw_digest(data["configuration_fingerprint"])
    _require_raw_digest(data["event_name_fingerprint"])
    _require_raw_bool(data["custom"])
    _require_raw_bool(data["deletable"])
    default_value = data["default_value"]
    if default_value is not None:
        _validate_edit_default_value_raw(_require_raw_mapping(default_value))


def _validate_edit_default_value_raw(data: dict[str, object]) -> None:
    _require_raw_keys(data, frozenset({"numeric_value", "currency"}))
    numeric_value = _require_raw_string(data["numeric_value"])
    if _DECIMAL_STRING.fullmatch(numeric_value) is None:
        raise ValueError("default value must use a decimal string")
    if _CURRENCY.fullmatch(_require_raw_string(data["currency"])) is None:
        raise ValueError("default value currency")


def _invalid_target() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_EDIT_INVENTORY_INPUT",
        "GA4 Admin EDIT inventory target is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _revalidate_target(value: object) -> Ga4AdminEditInventoryTarget:
    try:
        if type(value) is not Ga4AdminEditInventoryTarget:
            raise ValueError("target type")
        raw = value.model_dump_json(warnings="error")
        if len(raw.encode("utf-8")) > _MAX_MODEL_JSON_BYTES:
            raise ValueError("target size")
        data = parse_json(raw, maximum_bytes=_MAX_MODEL_JSON_BYTES)
        if type(data) is not dict:
            raise ValueError("target shape")
        expected = {
            "schema_version",
            "profile_purpose",
            "property_ref",
            "property_client_ref",
            "web_stream_ref",
            "web_stream_client_ref",
            "expected_timezone",
            "expected_currency",
            "admin_api_version",
            "required_scope",
        }
        if set(data) != expected:
            raise ValueError("target keys")
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or data["profile_purpose"] != "EDIT"
            or data["admin_api_version"] != _API_VERSION
            or data["required_scope"] != _EDIT_SCOPE
        ):
            raise ValueError("target controls")
        for name in (
            "property_ref",
            "property_client_ref",
            "web_stream_ref",
            "web_stream_client_ref",
            "expected_timezone",
            "expected_currency",
        ):
            if type(data[name]) is not str:
                raise ValueError("target scalar")
        if (
            _NATIVE_ID.fullmatch(cast(str, data["property_client_ref"])) is None
            or _NATIVE_ID.fullmatch(cast(str, data["web_stream_client_ref"])) is None
            or cast(str, data["property_ref"])
            != f"ga4_admin.property.{data['property_client_ref']}"
            or cast(str, data["web_stream_ref"])
            != f"ga4_admin.web_stream.{data['web_stream_client_ref']}"
        ):
            raise ValueError("target identity")
        return Ga4AdminEditInventoryTarget.model_validate_json(raw)
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_target() from None


def _revalidate_access_identity(value: object) -> Ga4AdminAccessIdentity:
    try:
        if type(value) is not Ga4AdminAccessIdentity:
            raise ValueError("access identity type")
        raw = value.model_dump_json(warnings="error")
        data = parse_json(raw, maximum_bytes=_MAX_MODEL_JSON_BYTES)
        if type(data) is not dict:
            raise ValueError("access identity shape")
        expected = {
            "property_client_ref",
            "web_stream_client_ref",
            "timezone",
            "currency",
            "service_level",
        }
        if set(data) != expected:
            raise ValueError("access identity keys")
        for name in ("property_client_ref", "web_stream_client_ref", "timezone", "currency"):
            if type(data[name]) is not str:
                raise ValueError("access identity scalar")
        if (
            _NATIVE_ID.fullmatch(cast(str, data["property_client_ref"])) is None
            or _NATIVE_ID.fullmatch(cast(str, data["web_stream_client_ref"])) is None
            or data["service_level"]
            not in {"GOOGLE_ANALYTICS_STANDARD", "GOOGLE_ANALYTICS_360", "NOT_AVAILABLE"}
        ):
            raise ValueError("access identity controls")
        return Ga4AdminAccessIdentity.model_validate_json(raw)
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            "GA4 Admin EDIT identity observation is invalid.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _require_matching_access_identity(
    target: Ga4AdminEditInventoryTarget, identity: Ga4AdminAccessIdentity
) -> None:
    if (
        identity.property_client_ref != target.property_client_ref
        or identity.web_stream_client_ref != target.web_stream_client_ref
        or identity.timezone != target.expected_timezone
        or identity.currency != target.expected_currency
    ):
        raise AdhelmError(
            "IDENTITY_MISMATCH",
            "GA4 Admin EDIT property or selected web-stream settings differ from configuration.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Resolve configured GA4 property, web stream, currency or timezone before planning."
            ),
        )


def _validate_edit_inventory_components(inventory: Ga4AdminEditKeyEventInventory) -> None:
    """Validate model-construct/model-copy state before emitting any JSON."""

    if (
        type(inventory.schema_version) is not int
        or inventory.schema_version != 1
        or type(inventory.inventory_kind) is not str
        or inventory.inventory_kind != "GA4_ADMIN_EDIT_KEY_EVENT_INVENTORY"
        or type(inventory.pagination_verified) is not bool
        or inventory.pagination_verified is not True
        or type(inventory.fingerprint_key_marker) is not str
        or _DIGEST.fullmatch(inventory.fingerprint_key_marker) is None
        or type(inventory.inventory_content_marker) is not str
        or _DIGEST.fullmatch(inventory.inventory_content_marker) is None
    ):
        raise ValueError("GA4 Admin EDIT inventory controls are invalid")
    try:
        target = _revalidate_target(inventory.target)
    except AdhelmError:
        raise ValueError("GA4 Admin EDIT inventory target is invalid") from None
    capacity = inventory.capacity
    checked_capacity: Ga4AdminEditKeyEventCapacity
    if type(capacity) is Ga4AdminEditStandardKeyEventCapacity:
        try:
            raw_capacity = capacity.model_dump(mode="json", warnings="error")
            if type(raw_capacity) is not dict:
                raise ValueError("capacity serialization")
            _validate_edit_capacity_raw(cast(dict[str, object], raw_capacity))
            checked_capacity = Ga4AdminEditStandardKeyEventCapacity.model_validate_json(
                capacity.model_dump_json(warnings="error")
            )
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 Admin EDIT Standard capacity is invalid") from None
    elif type(capacity) is Ga4AdminEditKeyEventCapacityNotEvidenced:
        try:
            raw_capacity = capacity.model_dump(mode="json", warnings="error")
            if type(raw_capacity) is not dict:
                raise ValueError("capacity serialization")
            _validate_edit_capacity_raw(cast(dict[str, object], raw_capacity))
            checked_capacity = Ga4AdminEditKeyEventCapacityNotEvidenced.model_validate_json(
                capacity.model_dump_json(warnings="error")
            )
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 Admin EDIT capacity is invalid") from None
    else:
        raise ValueError("GA4 Admin EDIT capacity is invalid")
    if type(inventory.key_events) is not tuple or len(inventory.key_events) > _MAX_ITEMS:
        raise ValueError("GA4 Admin EDIT key-event members are invalid")
    if len(inventory.key_events) != checked_capacity.complete_observed_key_event_total:
        raise ValueError("GA4 Admin EDIT member count is invalid")
    seen_client_refs: set[str] = set()
    for item in inventory.key_events:
        if type(item) is not Ga4AdminKeyEvent:
            raise ValueError("GA4 Admin EDIT key-event member is invalid")
        try:
            raw_item = item.model_dump(mode="json", warnings="error")
            if type(raw_item) is not dict:
                raise ValueError("key-event serialization")
            _validate_edit_key_event_raw(cast(dict[str, object], raw_item))
            checked_item = Ga4AdminKeyEvent.model_validate_json(
                item.model_dump_json(warnings="error")
            )
        except (
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 Admin EDIT key-event member is invalid") from None
        if (
            _NATIVE_ID.fullmatch(checked_item.resource_client_ref) is None
            or checked_item.resource_ref
            != f"ga4_admin.key_event.{checked_item.resource_client_ref}"
            or checked_item.property_ref != target.property_ref
            or checked_item.resource_client_ref in seen_client_refs
        ):
            raise ValueError("GA4 Admin EDIT key-event membership is invalid")
        seen_client_refs.add(checked_item.resource_client_ref)


__all__ = [
    "Ga4AdminEditInventoryTarget",
    "Ga4AdminEditKeyEventCapacity",
    "Ga4AdminEditKeyEventCapacityNotEvidenced",
    "Ga4AdminEditKeyEventInventory",
    "Ga4AdminEditKeyEventInventoryClient",
    "Ga4AdminEditStandardKeyEventCapacity",
    "revalidate_ga4_admin_edit_key_event_inventory",
]

"""Fixture-only coverage for the distinct GA4 Admin EDIT key-event inventory."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from pydantic_core import PydanticSerializationError

from adhelm.errors import AdhelmError
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventCapacityNotEvidenced,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
    Ga4AdminEditStandardKeyEventCapacity,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32
_FIELDS = (
    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),"
    "nextPageToken"
)


def _target(**changes: object) -> Ga4AdminEditInventoryTarget:
    values: dict[str, object] = {
        "property_ref": "ga4_admin.property.123",
        "property_client_ref": "123",
        "web_stream_ref": "ga4_admin.web_stream.456",
        "web_stream_client_ref": "456",
        "expected_timezone": "America/Chicago",
        "expected_currency": "USD",
    }
    values.update(changes)
    return Ga4AdminEditInventoryTarget.model_validate(values)


def _property(
    *, level: str | None = "GOOGLE_ANALYTICS_STANDARD", **changes: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "properties/123",
        "timeZone": "America/Chicago",
        "currencyCode": "USD",
        "deleteTime": None,
    }
    if level is not None:
        value["serviceLevel"] = level
    value.update(changes)
    return value


def _stream(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "properties/123/dataStreams/456",
        "type": "WEB_DATA_STREAM",
    }
    value.update(changes)
    return value


def _key_event(
    identifier: str,
    event_name: str = "signup",
    *,
    default_value: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "name": f"properties/123/keyEvents/{identifier}",
        "eventName": event_name,
        "createTime": "2026-09-05T12:00:00Z",
        "deletable": True,
        "custom": True,
        "countingMethod": "ONCE_PER_EVENT",
    }
    if default_value is not None:
        value["defaultValue"] = default_value
    return value


def _constructed_target(**changes: object) -> Ga4AdminEditInventoryTarget:
    values = _target().model_dump(mode="python")
    values.update(changes)
    return Ga4AdminEditInventoryTarget.model_construct(**values)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> SafeHttpClient:
    return SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"analyticsadmin.googleapis.com"},
        read_attempts=1,
    )


def _identity_then_list(
    request: httpx.Request,
    *,
    key_events: list[dict[str, object]],
    level: str | None = "GOOGLE_ANALYTICS_STANDARD",
) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "analyticsadmin.googleapis.com"
    assert not request.content
    if request.url.path == "/v1beta/properties/123":
        assert dict(request.url.params) == {
            "fields": "name,timeZone,currencyCode,serviceLevel,deleteTime"
        }
        return httpx.Response(200, json=_property(level=level))
    if request.url.path == "/v1beta/properties/123/dataStreams/456":
        assert dict(request.url.params) == {"fields": "name,type"}
        return httpx.Response(200, json=_stream())
    assert request.url.path == "/v1alpha/properties/123/keyEvents"
    assert dict(request.url.params) == {"pageSize": "200", "fields": _FIELDS}
    return httpx.Response(200, json={"keyEvents": key_events})


def test_captures_one_edit_token_then_reads_identity_and_fixed_key_event_list() -> None:
    token_calls: list[None] = []
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers["authorization"]))
        return _identity_then_list(
            request,
            key_events=[_key_event("702"), _key_event("701", "trial")],
        )

    def token() -> str:
        token_calls.append(None)
        return "fixture-edit-token"

    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(handler),
        token,
        fingerprint_key=_KEY,
    ).read_inventory(_target())

    assert token_calls == [None]
    assert calls == [
        ("/v1beta/properties/123", "Bearer fixture-edit-token"),
        ("/v1beta/properties/123/dataStreams/456", "Bearer fixture-edit-token"),
        ("/v1alpha/properties/123/keyEvents", "Bearer fixture-edit-token"),
    ]
    assert [item.resource_client_ref for item in inventory.key_events] == ["701", "702"]
    assert isinstance(inventory.capacity, Ga4AdminEditStandardKeyEventCapacity)
    assert inventory.capacity.complete_observed_key_event_total == 2
    assert inventory.capacity.documented_key_event_cap == 30
    assert inventory.capacity.remaining_documented_slots == 28
    assert inventory.capacity.capacity_status == "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP"
    assert inventory.pagination_verified is True
    assert inventory.fingerprint_key_marker != inventory.inventory_content_marker
    encoded = inventory.model_dump_json()
    for private in ("signup", "trial", "fixture-edit-token", _KEY.decode("ascii")):
        assert private not in encoded


def test_pagination_uses_fixed_fields_and_binds_all_complete_members_before_capacity() -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1alpha/properties/123/keyEvents":
            return _identity_then_list(request, key_events=[])
        params = dict(request.url.params)
        calls.append(params)
        if "pageToken" not in params:
            assert params == {"pageSize": "200", "fields": _FIELDS}
            return httpx.Response(
                200,
                json={"keyEvents": [_key_event("702", "two")], "nextPageToken": "next-page"},
            )
        assert params == {"pageSize": "200", "fields": _FIELDS, "pageToken": "next-page"}
        return httpx.Response(200, json={"keyEvents": [_key_event("701", "one")]})

    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(handler), lambda: "fixture-edit-token", fingerprint_key=_KEY
    ).read_inventory(_target())

    assert len(calls) == 2
    assert [item.resource_client_ref for item in inventory.key_events] == ["701", "702"]
    assert inventory.capacity.complete_observed_key_event_total == 2


@pytest.mark.parametrize(
    ("level", "expected_level"),
    [
        ("GOOGLE_ANALYTICS_360", "GOOGLE_ANALYTICS_360"),
        (None, "NOT_AVAILABLE"),
    ],
)
def test_nonstandard_or_unknown_service_level_does_not_invent_capacity(
    level: str | None, expected_level: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _identity_then_list(request, key_events=[_key_event("701")], level=level)

    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(handler),
        lambda: "fixture-edit-token",
        fingerprint_key=_KEY,
    ).read_inventory(_target())

    assert isinstance(inventory.capacity, Ga4AdminEditKeyEventCapacityNotEvidenced)
    assert inventory.capacity.observed_service_level == expected_level
    assert inventory.capacity.complete_observed_key_event_total == 1
    assert inventory.capacity.capacity_status == "CAP_NOT_EVIDENCED_FOR_OBSERVED_SERVICE_LEVEL"
    assert "documented_key_event_cap" not in inventory.capacity.model_dump()


def test_standard_capacity_at_or_above_cap_never_reports_negative_headroom() -> None:
    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(
            lambda request: _identity_then_list(
                request,
                key_events=[_key_event(str(700 + index), f"event_{index}") for index in range(31)],
            )
        ),
        lambda: "fixture-edit-token",
        fingerprint_key=_KEY,
    ).read_inventory(_target())

    assert isinstance(inventory.capacity, Ga4AdminEditStandardKeyEventCapacity)
    assert inventory.capacity.complete_observed_key_event_total == 31
    assert inventory.capacity.remaining_documented_slots == 0
    assert inventory.capacity.capacity_status == "AT_OR_ABOVE_DOCUMENTED_STANDARD_KEY_EVENT_CAP"


@pytest.mark.parametrize(
    "target",
    [
        _constructed_target(schema_version=True),
        _constructed_target(profile_purpose="READ"),
        _constructed_target(required_scope="https://www.googleapis.com/auth/analytics.readonly"),
        _constructed_target(property_ref="ga4_admin.property.relabelled"),
    ],
)
def test_constructed_target_bypasses_stop_before_token_or_http(
    target: Ga4AdminEditInventoryTarget,
) -> None:
    calls: list[str] = []

    def token() -> str:
        calls.append("token")
        return "fixture-edit-token"

    client = Ga4AdminEditKeyEventInventoryClient(
        _transport(lambda _request: pytest.fail("HTTP must not run")),
        token,
        fingerprint_key=_KEY,
    )

    with pytest.raises(AdhelmError) as error:
        client.read_inventory(target)
    assert error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY_INPUT"
    assert calls == []


@pytest.mark.parametrize(
    ("property_changes", "expected_code"),
    [
        ({"currencyCode": "CAD"}, "IDENTITY_MISMATCH"),
        ({"timeZone": "UTC"}, "IDENTITY_MISMATCH"),
        ({"name": "properties/999"}, "IDENTITY_MISMATCH"),
    ],
)
def test_identity_or_expected_settings_mismatch_stops_before_list(
    property_changes: dict[str, object], expected_code: str
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property(**property_changes))  # type: ignore[arg-type]
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(200, json=_stream())
        pytest.fail("key-event list must not run")

    with pytest.raises(AdhelmError) as error:
        Ga4AdminEditKeyEventInventoryClient(
            _transport(handler), lambda: "fixture-edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())
    assert error.value.code == expected_code
    assert "/v1alpha/properties/123/keyEvents" not in calls


def test_duplicate_member_id_or_pagination_cycle_fails_closed() -> None:
    duplicate_calls = 0

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        nonlocal duplicate_calls
        if request.url.path != "/v1alpha/properties/123/keyEvents":
            return _identity_then_list(request, key_events=[])
        duplicate_calls += 1
        if duplicate_calls == 1:
            return httpx.Response(
                200,
                json={"keyEvents": [_key_event("701")], "nextPageToken": "again"},
            )
        return httpx.Response(200, json={"keyEvents": [_key_event("701", "other")]})

    with pytest.raises(AdhelmError) as error:
        Ga4AdminEditKeyEventInventoryClient(
            _transport(duplicate_handler), lambda: "fixture-edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())
    assert error.value.code == "PROVIDER_RESPONSE_INVALID"

    def cycle_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1alpha/properties/123/keyEvents":
            return _identity_then_list(request, key_events=[])
        return httpx.Response(200, json={"keyEvents": [], "nextPageToken": "same"})

    with pytest.raises(AdhelmError) as error:
        Ga4AdminEditKeyEventInventoryClient(
            _transport(cycle_handler), lambda: "fixture-edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())
    assert error.value.code == "PROVIDER_RESPONSE_INVALID"


def test_invalid_projected_member_or_provider_field_is_rejected_without_reflection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1alpha/properties/123/keyEvents":
            return _identity_then_list(request, key_events=[])
        return httpx.Response(
            200,
            json={"keyEvents": [{**_key_event("701"), "displayName": "private response value"}]},
        )

    with pytest.raises(AdhelmError) as error:
        Ga4AdminEditKeyEventInventoryClient(
            _transport(handler), lambda: "fixture-edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())
    assert error.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "private response" not in str(error.value)


@pytest.mark.parametrize("invalid_key", [b"x" * 31, bytearray(b"x" * 32), "not-bytes"])
def test_invalid_fingerprint_key_or_transport_config_stops_before_token(
    invalid_key: object,
) -> None:
    token_calls: list[None] = []

    def token() -> str:
        token_calls.append(None)
        return "fixture-edit-token"

    with pytest.raises(AdhelmError) as error:
        Ga4AdminEditKeyEventInventoryClient(
            _transport(lambda _request: pytest.fail("HTTP must not run")),
            token,
            fingerprint_key=invalid_key,  # type: ignore[arg-type]
        )
    assert error.value.code == "FINGERPRINT_KEY_REQUIRED"
    assert token_calls == []

    client = httpx.Client(params={"unexpected": "1"}, trust_env=False)
    with pytest.raises(AdhelmError) as error:
        Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}, read_attempts=1),
            token,
            fingerprint_key=_KEY,
        )
    client.close()
    assert error.value.code == "TRANSPORT_CONFIG_INVALID"
    assert token_calls == []


def test_content_marker_is_bound_to_edit_inventory_shape_and_changes_with_member_or_key() -> None:
    def inventory(key: bytes, event_name: str) -> Ga4AdminEditKeyEventInventory:
        def handler(request: httpx.Request) -> httpx.Response:
            return _identity_then_list(request, key_events=[_key_event("701", event_name)])

        return Ga4AdminEditKeyEventInventoryClient(
            _transport(handler),
            lambda: "fixture-edit-token",
            fingerprint_key=key,
        ).read_inventory(_target())

    first = inventory(_KEY, "signup")
    changed_member = inventory(_KEY, "trial")
    changed_key = inventory(_OTHER_KEY, "signup")
    assert first.fingerprint_key_marker == changed_member.fingerprint_key_marker
    assert first.fingerprint_key_marker != changed_key.fingerprint_key_marker
    assert first.inventory_content_marker != changed_member.inventory_content_marker
    assert first.inventory_content_marker != changed_key.inventory_content_marker
    assert first.inventory_content_marker != first.fingerprint_key_marker

    # The edit seal's canonical surface is distinct from the historical READ
    # inventory type: no model serialization leaks the input event name.
    wire = json.loads(first.model_dump_json())
    assert wire["inventory_kind"] == "GA4_ADMIN_EDIT_KEY_EVENT_INVENTORY"
    assert "event_name_fingerprint" in wire["key_events"][0]
    assert "signup" not in first.model_dump_json()


def test_public_revalidation_rejects_constructed_or_raw_tampering_before_consumer_use() -> None:
    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(lambda request: _identity_then_list(request, key_events=[_key_event("701")])),
        lambda: "fixture-edit-token",
        fingerprint_key=_KEY,
    ).read_inventory(_target())
    assert (
        revalidate_ga4_admin_edit_key_event_inventory(inventory, fingerprint_key=_KEY) == inventory
    )
    assert (
        revalidate_ga4_admin_edit_key_event_inventory(
            json.loads(inventory.model_dump_json()), fingerprint_key=_KEY
        )
        == inventory
    )

    forged_member = inventory.key_events[0].model_copy(update={"event_name_fingerprint": "f" * 64})
    forged = inventory.model_copy(update={"key_events": (forged_member,)})
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(forged, fingerprint_key=_KEY)
    assert error.value.code == "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH"

    raw = json.loads(inventory.model_dump_json())
    raw["schema_version"] = True
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(raw, fingerprint_key=_KEY)
    assert error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"

    raw = json.loads(inventory.model_dump_json())
    raw["key_events"][0]["event_name_fingerprint"] = Decimal("1")
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(raw, fingerprint_key=_KEY)
    assert error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"

    oversized = json.loads(inventory.model_dump_json())
    oversized["unknown"] = "x" * 1_048_577
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(oversized, fingerprint_key=_KEY)
    assert error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"

    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(inventory, fingerprint_key=_OTHER_KEY)
    assert error.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"


def test_copy_serialization_and_deep_raw_mappings_fail_closed() -> None:
    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(lambda request: _identity_then_list(request, key_events=[_key_event("701")])),
        lambda: "fixture-edit-token",
        fingerprint_key=_KEY,
    ).read_inventory(_target())
    assert isinstance(inventory.capacity, Ga4AdminEditStandardKeyEventCapacity)
    forged_capacity = inventory.capacity.model_copy(
        update={
            "remaining_documented_slots": 0,
            "capacity_status": "AT_OR_ABOVE_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        }
    )
    forged = inventory.model_copy(update={"capacity": forged_capacity})
    with pytest.raises(PydanticSerializationError):
        forged.model_dump_json()

    raw = json.loads(inventory.model_dump_json())
    deep: dict[str, object] = {}
    current = deep
    for _ in range(1_500):
        child: dict[str, object] = {}
        current["nested"] = child
        current = child
    raw["target"] = deep
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(raw, fingerprint_key=_KEY)
    assert error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"


@pytest.mark.parametrize("numeric_value", [1, 1.0, True])
def test_revalidation_rejects_raw_default_value_number_even_with_matching_marker(
    numeric_value: int | float | bool,
) -> None:
    """A rehashed wire form cannot launder a JSON number into Decimal text."""

    inventory = Ga4AdminEditKeyEventInventoryClient(
        _transport(
            lambda request: _identity_then_list(
                request,
                key_events=[
                    _key_event(
                        "701",
                        default_value={"numericValue": 1.0, "currencyCode": "USD"},
                    )
                ],
            )
        ),
        lambda: "fixture-edit-token",
        fingerprint_key=_KEY,
    ).read_inventory(_target())
    raw = json.loads(inventory.model_dump_json())
    default_value = raw["key_events"][0]["default_value"]
    assert isinstance(default_value, dict)
    assert default_value["numeric_value"] == "1.0"
    # The original marker is still what a coerced Decimal model would compute.
    default_value["numeric_value"] = numeric_value
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_inventory(raw, fingerprint_key=_KEY)
    assert error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"


def test_token_getter_failure_is_sanitized_before_any_http_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("the token failure must stop before HTTP")

    def failing_token_getter() -> str:
        raise RuntimeError("private-token-backend-detail")

    client = Ga4AdminEditKeyEventInventoryClient(
        _transport(handler), failing_token_getter, fingerprint_key=_KEY
    )
    with pytest.raises(AdhelmError) as error:
        client.read_inventory(_target())
    assert error.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert "private-token-backend-detail" not in str(error.value)
    assert calls == []

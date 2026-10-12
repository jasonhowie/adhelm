"""Independent adversarial checks for the sealed GA4 Admin EDIT inventory."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

import adhelm.providers.ga4_admin_edit_inventory as edit_inventory
from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.providers.ga4_admin import Ga4AdminIdentity, Ga4AdminReadClient
from adhelm.providers.ga4_admin_access import Ga4AdminAccessIdentity
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_KEY_EVENT_FIELDS = (
    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),"
    "nextPageToken"
)


def _read_identity() -> Ga4AdminIdentity:
    return Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )


def _target(**changes: object) -> edit_inventory.Ga4AdminEditInventoryTarget:
    values: dict[str, object] = {
        "property_ref": "ga4_admin.property.123",
        "property_client_ref": "123",
        "web_stream_ref": "ga4_admin.web_stream.456",
        "web_stream_client_ref": "456",
        "expected_timezone": "America/Chicago",
        "expected_currency": "USD",
    }
    values.update(changes)
    return edit_inventory.Ga4AdminEditInventoryTarget.model_validate(values)


def _property(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "properties/123",
        "timeZone": "America/Chicago",
        "currencyCode": "USD",
        "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
        "deleteTime": None,
    }
    values.update(changes)
    return values


def _stream(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "properties/123/dataStreams/456",
        "type": "WEB_DATA_STREAM",
    }
    values.update(changes)
    return values


def _key_event(identifier: str, event_name: str = "signup") -> dict[str, object]:
    return {
        "name": f"properties/123/keyEvents/{identifier}",
        "eventName": event_name,
        "createTime": "2026-09-05T12:00:00Z",
        "deletable": True,
        "custom": True,
        "countingMethod": "ONCE_PER_EVENT",
    }


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> SafeHttpClient:
    return SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"analyticsadmin.googleapis.com"},
        read_attempts=1,
    )


def _inventory_response(
    request: httpx.Request, *, key_events: list[dict[str, object]]
) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "analyticsadmin.googleapis.com"
    assert request.content == b""
    if request.url.path == "/v1beta/properties/123":
        assert tuple(request.url.params.multi_items()) == (
            ("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),
        )
        return httpx.Response(200, json=_property())
    if request.url.path == "/v1beta/properties/123/dataStreams/456":
        assert tuple(request.url.params.multi_items()) == (("fields", "name,type"),)
        return httpx.Response(200, json=_stream())
    assert request.url.path == "/v1alpha/properties/123/keyEvents"
    assert tuple(request.url.params.multi_items()) == (
        ("pageSize", "200"),
        ("fields", _KEY_EVENT_FIELDS),
    )
    return httpx.Response(200, json={"keyEvents": key_events})


def _fresh_inventory(
    *,
    token: str = "edit-token",
    key_events: list[dict[str, object]] | None = None,
) -> edit_inventory.Ga4AdminEditKeyEventInventory:
    return edit_inventory.Ga4AdminEditKeyEventInventoryClient(
        _transport(
            lambda request: _inventory_response(
                request, key_events=key_events if key_events is not None else [_key_event("701")]
            )
        ),
        lambda: token,
        fingerprint_key=_KEY,
    ).read_inventory(_target())


def test_shared_key_event_parser_keeps_legacy_read_inventory_bytes_and_fingerprints() -> None:
    """The EDIT extraction must not change pre-existing READ inventory history."""

    raw_key_events = (
        b'{"keyEvents":[{"name":"properties/123/keyEvents/789",'
        b'"eventName":"purchase","createTime":"2026-09-04T01:02:03.120Z",'
        b'"deletable":true,"custom":true,"countingMethod":"ONCE_PER_EVENT",'
        b'"defaultValue":{"numericValue":1.2500,"currencyCode":"USD"}}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, content=raw_key_events)
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": []})
        assert request.url.path.endswith("/eventEditRules")
        return httpx.Response(200, json={"eventEditRules": []})

    inventory = Ga4AdminReadClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
            {"analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "fixture-token",
        lambda _: True,
        fingerprint_key=b"f" * 32,
    ).inventory(identity=_read_identity())

    event = inventory.key_events[0]
    expected_configuration_fingerprint = (
        "8e75e742728320006a69ac282dddd039cb70dd006a65eb8c6b2948bf26e05a31"
    )
    expected_event_name_fingerprint = (
        "ae4078b39567fac4175d7b80501d10e8e14e8da55d2af6605525e093e6b2c3e7"
    )
    expected_key_marker = "10c559ca56b934c84fd4a25c520fa90b81e1330687f63f034867910ef649305e"
    expected_content_marker = "6a07134a79e7276c408032461b3031382de179edf653d0f54fb0193d757f1071"
    assert event.configuration_fingerprint == expected_configuration_fingerprint
    assert event.event_name_fingerprint == expected_event_name_fingerprint
    assert event.default_value is not None
    assert event.default_value.numeric_value.as_tuple().exponent == -4
    assert inventory.fingerprint_key_marker == expected_key_marker
    assert inventory.inventory_content_marker == expected_content_marker
    assert digest(inventory) == "edf8d0ea4b4363f49d0cb6ea0cd886c5632aa897f723b586c81e8e4930ab906b"


def test_edit_inventory_uses_one_token_snapshot_and_only_three_fixed_gets() -> None:
    token_reads: list[str] = []
    calls: list[tuple[str, str, tuple[tuple[str, str], ...], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.path,
                tuple(request.url.params.multi_items()),
                request.headers["authorization"],
            )
        )
        return _inventory_response(request, key_events=[_key_event("702", "trial")])

    inventory = edit_inventory.Ga4AdminEditKeyEventInventoryClient(
        _transport(handler),
        lambda: token_reads.append("read") or "single-edit-token",
        fingerprint_key=_KEY,
    ).read_inventory(_target())

    assert token_reads == ["read"]
    assert calls == [
        (
            "GET",
            "/v1beta/properties/123",
            (("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),),
            "Bearer single-edit-token",
        ),
        (
            "GET",
            "/v1beta/properties/123/dataStreams/456",
            (("fields", "name,type"),),
            "Bearer single-edit-token",
        ),
        (
            "GET",
            "/v1alpha/properties/123/keyEvents",
            (("pageSize", "200"), ("fields", _KEY_EVENT_FIELDS)),
            "Bearer single-edit-token",
        ),
    ]
    assert inventory.capacity.complete_observed_key_event_total == 1
    rendered = inventory.model_dump_json()
    for private in ("trial", "single-edit-token", _KEY.decode("ascii")):
        assert private not in rendered
    for unsupported_claim in (
        "execution_authority",
        "remote_mutations",
        "granted_scopes",
        "create_permission",
    ):
        assert unsupported_claim not in rendered


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"profile_purpose": "READ"},
        {"required_scope": "https://www.googleapis.com/auth/analytics.readonly"},
        {"property_ref": "ga4_admin.property.999"},
        {"expected_currency": "usd"},
    ],
)
def test_constructed_edit_target_controls_stop_before_token_or_http(
    updates: dict[str, object],
) -> None:
    forged = _target().model_copy(update=updates)
    token_reads: list[str] = []
    client = edit_inventory.Ga4AdminEditKeyEventInventoryClient(
        _transport(lambda _request: pytest.fail("provider egress")),
        lambda: token_reads.append("token") or "edit-token",
        fingerprint_key=_KEY,
    )

    with pytest.raises(AdhelmError) as raised:
        client.read_inventory(forged)

    assert raised.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY_INPUT"
    assert token_reads == []


@pytest.mark.parametrize(
    ("path", "response"),
    [
        ("/v1beta/properties/123", _property(currencyCode="CAD")),
        ("/v1beta/properties/123/dataStreams/456", _stream(type="ANDROID_APP_DATA_STREAM")),
        ("/v1beta/properties/123/dataStreams/456", _stream(name="properties/123/dataStreams/999")),
    ],
)
def test_current_identity_drift_stops_before_the_key_event_list(
    path: str, response: dict[str, object]
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == path:
            return httpx.Response(200, json=response)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property())
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(200, json=_stream())
        pytest.fail("key-event list must not run after identity drift")

    with pytest.raises(AdhelmError) as raised:
        edit_inventory.Ga4AdminEditKeyEventInventoryClient(
            _transport(handler), lambda: "edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())

    assert raised.value.code == "IDENTITY_MISMATCH"
    assert "/v1alpha/properties/123/keyEvents" not in calls


def test_constructed_access_observation_is_revalidated_before_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = Ga4AdminAccessIdentity.model_construct(
        property_client_ref="123",
        web_stream_client_ref="456",
        timezone="America/Chicago",
        currency="USD",
        service_level="UNTRUSTED_LEVEL",
    )
    monkeypatch.setattr(
        edit_inventory.Ga4AdminAccessClient,
        "read_identity",
        lambda _self, **_kwargs: forged,
    )
    token_reads: list[str] = []

    with pytest.raises(AdhelmError) as raised:
        edit_inventory.Ga4AdminEditKeyEventInventoryClient(
            _transport(lambda _request: pytest.fail("key-event list must not run")),
            lambda: token_reads.append("token") or "edit-token",
            fingerprint_key=_KEY,
        ).read_inventory(_target())

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert token_reads == ["token"]


def test_pagination_duplicate_and_private_extra_fields_fail_closed() -> None:
    calls = 0

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path != "/v1alpha/properties/123/keyEvents":
            return _inventory_response(request, key_events=[])
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"keyEvents": [_key_event("701")], "nextPageToken": "next"},
            )
        return httpx.Response(200, json={"keyEvents": [_key_event("701", "other")]})

    with pytest.raises(AdhelmError) as duplicate:
        edit_inventory.Ga4AdminEditKeyEventInventoryClient(
            _transport(duplicate_handler), lambda: "edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())
    assert duplicate.value.code == "PROVIDER_RESPONSE_INVALID"

    private = "private provider field"

    def extra_field_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1alpha/properties/123/keyEvents":
            return _inventory_response(request, key_events=[])
        return httpx.Response(
            200,
            json={"keyEvents": [{**_key_event("702"), "displayName": private}]},
        )

    with pytest.raises(AdhelmError) as extra:
        edit_inventory.Ga4AdminEditKeyEventInventoryClient(
            _transport(extra_field_handler), lambda: "edit-token", fingerprint_key=_KEY
        ).read_inventory(_target())
    assert extra.value.code == "PROVIDER_RESPONSE_INVALID"
    assert private not in str(extra.value)


@pytest.mark.parametrize("service_level", ["GOOGLE_ANALYTICS_360", None])
def test_nonstandard_or_unknown_level_has_no_inferred_capacity(service_level: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1beta/properties/123":
            property_data = _property()
            if service_level is None:
                property_data.pop("serviceLevel")
            else:
                property_data["serviceLevel"] = service_level
            return httpx.Response(200, json=property_data)
        return _inventory_response(request, key_events=[_key_event("701")])

    inventory = edit_inventory.Ga4AdminEditKeyEventInventoryClient(
        _transport(handler), lambda: "edit-token", fingerprint_key=_KEY
    ).read_inventory(_target())

    assert isinstance(inventory.capacity, edit_inventory.Ga4AdminEditKeyEventCapacityNotEvidenced)
    wire = inventory.capacity.model_dump(mode="json")
    assert wire["capacity_status"] == "CAP_NOT_EVIDENCED_FOR_OBSERVED_SERVICE_LEVEL"
    assert "documented_key_event_cap" not in wire
    assert "remaining_documented_slots" not in wire


def test_keyed_revalidation_and_serializer_reject_forged_capacity_members_and_markers() -> None:
    inventory = _fresh_inventory()
    forged_capacity = inventory.capacity.model_copy(
        update={
            "remaining_documented_slots": 0,
            "capacity_status": "AT_OR_ABOVE_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        }
    )
    forged = inventory.model_copy(update={"capacity": forged_capacity})

    with pytest.raises(ValueError, match="capacity is invalid"):
        forged.model_dump_json()
    with pytest.raises(AdhelmError) as structurally_invalid:
        edit_inventory.revalidate_ga4_admin_edit_key_event_inventory(forged, fingerprint_key=_KEY)
    assert structurally_invalid.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"

    wire = inventory.model_dump(mode="json")
    capacity = wire["capacity"]
    assert isinstance(capacity, dict)
    capacity.update(
        {
            "complete_observed_key_event_total": 0,
            "remaining_documented_slots": 30,
            "capacity_status": "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        }
    )
    wire["key_events"] = []
    with pytest.raises(AdhelmError) as content_mismatch:
        edit_inventory.revalidate_ga4_admin_edit_key_event_inventory(wire, fingerprint_key=_KEY)
    assert content_mismatch.value.code == "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH"

    with pytest.raises(AdhelmError) as wrong_key:
        edit_inventory.revalidate_ga4_admin_edit_key_event_inventory(
            inventory, fingerprint_key=b"z" * 32
        )
    assert wrong_key.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"


def test_revalidator_rejects_rehashed_controls_and_deep_untrusted_mappings() -> None:
    inventory = _fresh_inventory()
    float_control = inventory.model_dump(mode="json")
    float_control["schema_version"] = 1.0
    with pytest.raises(AdhelmError) as float_error:
        edit_inventory.revalidate_ga4_admin_edit_key_event_inventory(
            float_control, fingerprint_key=_KEY
        )
    assert float_error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"

    nested: dict[str, object] = {"private": "private sentinel"}
    for _ in range(32):
        nested = {"nested": nested}
    deep_mapping = inventory.model_dump(mode="json")
    target = deep_mapping["target"]
    assert isinstance(target, dict)
    target["unexpected"] = nested
    with pytest.raises(AdhelmError) as deep_error:
        edit_inventory.revalidate_ga4_admin_edit_key_event_inventory(
            deep_mapping, fingerprint_key=_KEY
        )
    assert deep_error.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"
    assert "private sentinel" not in str(deep_error.value)


@pytest.mark.parametrize("alias", [1, 1.0, True])
def test_revalidator_rejects_nested_default_value_type_aliases_before_marker_use(
    alias: object,
) -> None:
    """A rehashed raw mapping cannot normalize a JSON number into Decimal text."""

    inventory = _fresh_inventory(
        key_events=[
            {
                **_key_event("701"),
                "defaultValue": {"numericValue": 1.0, "currencyCode": "USD"},
            }
        ]
    )
    wire = inventory.model_dump(mode="json")
    default_value = wire["key_events"][0]["default_value"]
    assert isinstance(default_value, dict)
    assert default_value["numeric_value"] == "1.0"
    # The existing marker was made from that same normalized decimal value.
    # The raw boundary must still reject a number/boolean alias before it can
    # become a valid typed Decimal under the local content MAC.
    default_value["numeric_value"] = alias

    with pytest.raises(AdhelmError) as raised:
        edit_inventory.revalidate_ga4_admin_edit_key_event_inventory(wire, fingerprint_key=_KEY)

    assert raised.value.code == "INVALID_GA4_ADMIN_EDIT_INVENTORY"


def test_token_getter_exception_is_sanitized_before_any_http() -> None:
    secret = "private-token-backend-sentinel"
    requests: list[httpx.Request] = []

    def getter() -> str:
        raise RuntimeError(secret)

    with pytest.raises(AdhelmError) as raised:
        edit_inventory.Ga4AdminEditKeyEventInventoryClient(
            _transport(lambda request: requests.append(request) or pytest.fail("provider egress")),
            getter,
            fingerprint_key=_KEY,
        ).read_inventory(_target())

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert secret not in str(raised.value)
    assert requests == []


def test_readonly_bearer_read_does_not_create_a_grant_or_mutation_claim() -> None:
    inventory = _fresh_inventory(token="readonly-fixture-token")
    data = inventory.model_dump(mode="json")

    assert data["target"]["required_scope"] == _EDIT_SCOPE
    for absent in (
        "granted_scope",
        "granted_scopes",
        "create_permission",
        "execution_authority",
        "remote_mutations",
        "mutation_readiness",
    ):
        assert absent not in data
    assert "readonly-fixture-token" not in inventory.model_dump_json()

"""Regression coverage for GA4 inventory fingerprint-key continuity markers."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from adhelm.canonical import canonical_bytes, digest
from adhelm.measurement_records import Ga4MeasurementSnapshot, MeasurementObservation
from adhelm.providers.ga4_admin import Ga4AdminIdentity, Ga4AdminInventory, Ga4AdminReadClient
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32


def _identity() -> Ga4AdminIdentity:
    return Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )


def _read_inventory(
    key: bytes,
    *,
    include_key_event: bool = False,
    unordered_collections: bool = False,
) -> Ga4AdminInventory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            if unordered_collections:
                key_events = [
                    {
                        "name": f"properties/123/keyEvents/{resource_id}",
                        "eventName": event_name,
                        "deletable": True,
                        "custom": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                    for resource_id, event_name in (("701", "second_event"), ("700", "first_event"))
                ]
            elif include_key_event:
                key_events = [
                    {
                        "name": "properties/123/keyEvents/700",
                        "eventName": "purchase",
                        "deletable": True,
                        "custom": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                ]
            else:
                key_events = []
            return httpx.Response(
                200,
                json={"keyEvents": key_events},
            )
        if request.url.path.endswith("/eventCreateRules"):
            create_rules = (
                [
                    {
                        "name": f"properties/123/dataStreams/456/eventCreateRules/{resource_id}",
                        "destinationEvent": "derived_event",
                        "eventConditions": [
                            {
                                "field": "eventName",
                                "comparisonType": "EQUALS",
                                "value": "first_event",
                                "negated": False,
                            }
                        ],
                        "sourceCopyParameters": False,
                        "parameterMutations": [],
                    }
                    for resource_id in ("801", "800")
                ]
                if unordered_collections
                else []
            )
            return httpx.Response(200, json={"eventCreateRules": create_rules})
        if request.url.path.endswith("/eventEditRules"):
            edit_rules = (
                [
                    {
                        "name": f"properties/123/dataStreams/456/eventEditRules/{resource_id}",
                        "displayName": "fixture_rule",
                        "eventConditions": [
                            {
                                "field": "eventName",
                                "comparisonType": "EQUALS",
                                "value": "first_event",
                                "negated": False,
                            }
                        ],
                        "parameterMutations": [],
                        "processingOrder": "1",
                    }
                    for resource_id in ("901", "900")
                ]
                if unordered_collections
                else []
            )
            return httpx.Response(200, json={"eventEditRules": edit_rules})
        raise AssertionError(request.url.path)

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"analyticsadmin.googleapis.com"},
        read_attempts=1,
    )
    return Ga4AdminReadClient(
        transport,
        lambda: "fixture-token",
        lambda candidate: candidate == _identity(),
        fingerprint_key=key,
    ).inventory(identity=_identity())


def _legacy_inventory_wire() -> dict[str, Any]:
    """M16 inventory wire fixture, deliberately lacking the new marker."""
    return {
        "schema_version": 1,
        "identity": {
            "schema_version": 1,
            "property_ref": "ga4_admin.property.123",
            "property_client_ref": "123",
            "web_stream_ref": "ga4_admin.web_stream.456",
            "web_stream_client_ref": "456",
            "admin_api_version": "v1alpha",
            "required_scope": "https://www.googleapis.com/auth/analytics.readonly",
        },
        "key_events": [
            {
                "resource_ref": "ga4_admin.key_event.700",
                "resource_client_ref": "700",
                "property_ref": "ga4_admin.property.123",
                "configuration_fingerprint": "a" * 64,
                "event_name_fingerprint": "b" * 64,
                "fingerprint_scheme": "HMAC_SHA256_LOCAL_V1",
                "counting_method": "ONCE_PER_EVENT",
                "default_value": None,
                "custom": True,
                "deletable": True,
            }
        ],
        "event_create_rules": [],
        "event_edit_rules": [],
        "pagination_verified": True,
    }


def _legacy_snapshot_wire() -> dict[str, Any]:
    return {
        "provider": "ga4_admin",
        "capability_version": "ga4-admin-fixed-inventory-1",
        "inventory": _legacy_inventory_wire(),
    }


def test_reader_emits_stable_marker_for_one_key_and_distinct_markers_for_other_keys() -> None:
    first = _read_inventory(_KEY)
    same_key_different_inventory = _read_inventory(_KEY, include_key_event=True)
    different_key = _read_inventory(_OTHER_KEY)

    assert first.fingerprint_key_marker is not None
    assert first.inventory_content_marker is not None
    assert first.fingerprint_key_marker == same_key_different_inventory.fingerprint_key_marker
    assert first.fingerprint_key_marker != different_key.fingerprint_key_marker
    assert first.inventory_content_marker != same_key_different_inventory.inventory_content_marker
    assert first.inventory_content_marker != different_key.inventory_content_marker
    assert len(first.fingerprint_key_marker) == 64
    assert all(character in "0123456789abcdef" for character in first.fingerprint_key_marker)
    assert len(same_key_different_inventory.key_events) == 1


def test_marker_uses_fixed_versioned_domain_separated_hmac_context() -> None:
    inventory = _read_inventory(_KEY)
    expected_material = (
        b"adhelm-ga4-admin-fingerprint-v1\x00"
        b"inventory-key-marker\x00"
        b'{"marker_purpose":"GA4_ADMIN_INVENTORY_KEY_CONTINUITY","marker_version":1}'
    )
    event_name_material = b'adhelm-ga4-admin-fingerprint-v1\x00key-event-name\x00"purchase"'
    expected = hmac.new(_KEY, expected_material, hashlib.sha256).hexdigest()
    event_name_fingerprint = hmac.new(_KEY, event_name_material, hashlib.sha256).hexdigest()

    assert inventory.fingerprint_key_marker == expected
    assert inventory.fingerprint_key_marker != event_name_fingerprint


def test_content_marker_binds_the_complete_normalized_surface_in_its_own_domain() -> None:
    inventory = _read_inventory(_KEY, include_key_event=True)
    surface = inventory.model_dump(mode="json")
    content_marker = surface.pop("inventory_content_marker")
    expected_material = b"adhelm-ga4-admin-fingerprint-v1\x00inventory-content-marker\x00"
    expected = hmac.new(
        _KEY,
        expected_material + canonical_bytes(surface),
        hashlib.sha256,
    ).hexdigest()

    assert inventory.inventory_content_marker == expected
    assert content_marker == expected
    assert inventory.inventory_content_marker != inventory.fingerprint_key_marker


def test_reader_sorts_all_collections_before_sealing_the_normalized_inventory() -> None:
    inventory = _read_inventory(_KEY, unordered_collections=True)

    for collection in (
        inventory.key_events,
        inventory.event_create_rules,
        inventory.event_edit_rules,
    ):
        assert [item.resource_ref for item in collection] == sorted(
            item.resource_ref for item in collection
        )

    surface = inventory.model_dump(mode="json")
    content_marker = surface.pop("inventory_content_marker")
    expected = hmac.new(
        _KEY,
        b"adhelm-ga4-admin-fingerprint-v1\x00inventory-content-marker\x00"
        + canonical_bytes(surface),
        hashlib.sha256,
    ).hexdigest()
    assert content_marker == expected


def test_reader_does_not_accept_or_persist_a_caller_selected_marker_or_key() -> None:
    inventory = _read_inventory(_KEY)
    wire = inventory.model_dump(mode="json")
    rendered = json.dumps(wire, sort_keys=True)

    assert "fingerprint_key_marker" in wire
    assert "inventory_content_marker" in wire
    assert _KEY.decode("ascii") not in rendered
    assert _KEY.hex() not in rendered
    assert inspect.signature(Ga4AdminReadClient.inventory).parameters.keys() == {
        "self",
        "identity",
    }
    with pytest.raises(TypeError):
        Ga4AdminReadClient.inventory(
            object(),  # type: ignore[arg-type]
            identity=_identity(),
            fingerprint_key_marker="a" * 64,
        )
    with pytest.raises(TypeError):
        Ga4AdminReadClient.inventory(
            object(),  # type: ignore[arg-type]
            identity=_identity(),
            inventory_content_marker="a" * 64,
        )


def test_published_inventory_schema_makes_marker_optional_but_never_nullable() -> None:
    schema = Ga4AdminInventory.model_json_schema()
    for field_name in ("fingerprint_key_marker", "inventory_content_marker"):
        marker_schema = schema["properties"][field_name]

        assert field_name not in schema.get("required", [])
        assert marker_schema["type"] == "string"
        assert marker_schema["pattern"] == "^[a-f0-9]{64}$"
        assert "default" not in marker_schema
        assert "anyOf" not in marker_schema


def test_m16_markerless_fixture_preserves_exact_bytes_and_digests() -> None:
    legacy_inventory_wire = _legacy_inventory_wire()
    legacy_snapshot_wire = _legacy_snapshot_wire()
    inventory = Ga4AdminInventory.model_validate_json(json.dumps(legacy_inventory_wire))
    snapshot = Ga4MeasurementSnapshot.model_validate_json(json.dumps(legacy_snapshot_wire))
    observation_wire = {
        "schema_version": 1,
        "event_type": "MEASUREMENT_OBSERVATION",
        "profile_ref": "measurement.fixture",
        "read_started_at": "2026-09-04T00:00:00Z",
        "read_finished_at": "2026-09-04T00:00:02Z",
        "snapshot": legacy_snapshot_wire,
        "snapshot_digest": "db50435f60ac841b3e1e59019a35305a905ccb60ce777fb6a7cab832e248e002",
        "evidence_kind": "CONFIGURATION_READ",
        "consistency": "SEQUENTIAL_NONATOMIC",
        "runtime_firing": "NOT_EVALUATED",
        "processed_receipt": "NOT_EVALUATED",
        "native_optimization_association": "NOT_EVALUATED",
        "change_attribution": "NOT_EVALUATED",
        "execution_authority": False,
    }
    observation = MeasurementObservation.model_validate_json(json.dumps(observation_wire))

    assert inventory.fingerprint_key_marker is None
    assert inventory.inventory_content_marker is None
    assert "fingerprint_key_marker" not in inventory.model_dump()
    assert "inventory_content_marker" not in inventory.model_dump()
    assert "fingerprint_key_marker" not in inventory.model_dump(mode="json")
    assert "fingerprint_key_marker" not in inventory.model_dump(mode="json", exclude_none=False)
    assert "inventory_content_marker" not in inventory.model_dump(mode="json", exclude_none=False)
    assert canonical_bytes(inventory) == canonical_bytes(legacy_inventory_wire)
    assert digest(inventory) == "2f608e31e033591117f341fb8f81a0e22e3ca50ad142f181f85a0bf5f00fd403"
    assert canonical_bytes(snapshot) == canonical_bytes(legacy_snapshot_wire)
    assert digest(snapshot) == observation.snapshot_digest
    assert canonical_bytes(observation) == canonical_bytes(observation_wire)
    assert digest(observation) == "9a0cc18947e3d678b0e068a9954f9d67951c1bcdbaab580b40e86208f6464b80"


def test_m18_key_marker_only_inventory_preserves_its_exact_existing_wire_shape() -> None:
    fresh = _read_inventory(_KEY, include_key_event=True)
    m18_wire = fresh.model_dump(mode="json")
    m18_wire.pop("inventory_content_marker")
    reparsed = Ga4AdminInventory.model_validate_json(json.dumps(m18_wire))

    assert reparsed.fingerprint_key_marker == m18_wire["fingerprint_key_marker"]
    assert reparsed.inventory_content_marker is None
    assert reparsed.model_dump(mode="json") == m18_wire
    assert canonical_bytes(reparsed) == canonical_bytes(m18_wire)
    assert digest(reparsed) == "f0fbc56b12ac68b54cc2631daabfd87cc30ab77f590815e4b7f9949e0302f2d9"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("pagination_verified", 1),
        ("pagination_verified", 1.0),
        ("pagination_verified", False),
        ("fingerprint_key_marker", None),
        ("fingerprint_key_marker", True),
        ("fingerprint_key_marker", 1.0),
        ("fingerprint_key_marker", Decimal("1")),
        ("fingerprint_key_marker", "A" * 64),
        ("fingerprint_key_marker", "a" * 63),
        ("inventory_content_marker", None),
        ("inventory_content_marker", True),
        ("inventory_content_marker", 1.0),
        ("inventory_content_marker", Decimal("1")),
        ("inventory_content_marker", "A" * 64),
        ("inventory_content_marker", "a" * 63),
    ],
)
def test_inventory_rejects_noncanonical_marker_and_control_scalar_types(
    field: str, value: object
) -> None:
    raw = _legacy_inventory_wire()
    raw[field] = value

    with pytest.raises(ValidationError):
        Ga4AdminInventory.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "encoded_value"),
    [
        ("schema_version", "true"),
        ("schema_version", "1.0"),
        ("pagination_verified", "1"),
        ("pagination_verified", "1.0"),
        ("fingerprint_key_marker", "null"),
        ("inventory_content_marker", "null"),
    ],
)
def test_inventory_rejects_noncanonical_original_json_scalar_types(
    field: str, encoded_value: str
) -> None:
    raw = _legacy_inventory_wire()
    raw[field] = "__ADHELM_REPLACE_ME__"
    encoded = json.dumps(raw, separators=(",", ":")).replace(
        '"__ADHELM_REPLACE_ME__"', encoded_value, 1
    )

    with pytest.raises(ValidationError):
        Ga4AdminInventory.model_validate_json(encoded)


@pytest.mark.parametrize(
    "updates",
    [
        {"fingerprint_key_marker": None},
        {"fingerprint_key_marker": "z" * 64},
        {"inventory_content_marker": None},
        {"inventory_content_marker": "z" * 64},
        {"schema_version": True},
        {"pagination_verified": 1},
    ],
)
def test_constructed_or_copied_invalid_inventory_cannot_serialize(
    updates: dict[str, object],
) -> None:
    legacy = Ga4AdminInventory.model_validate_json(json.dumps(_legacy_inventory_wire()))
    forged = legacy.model_copy(update=updates)

    with pytest.raises(PydanticSerializationError):
        forged.model_dump(mode="json")

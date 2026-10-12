"""Independent adversarial review of GA4 key-event semantic CREATE state."""

from __future__ import annotations

import builtins
import inspect
import json
import socket
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import adhelm.providers.ga4_admin_create_state as semantic_state
from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
)
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32
_PROPERTY_ID = "987654321"
_STREAM_ID = "123456789"
_KEY_EVENT_FIELDS = (
    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),"
    "nextPageToken"
)


def _fresh(
    *,
    event_name: str = "planned_event",
    default_value: Decimal | None = None,
) -> Ga4AdminKeyEventCreateInput:
    return Ga4AdminKeyEventCreateInput(
        event_contract=EventContract(
            event_contract_ref="event.fixture",
            event_name=event_name,
            semantics=ConversionSemantics(
                semantics_ref="semantics.fixture",
                customer_definition="A customer-declared qualified request.",
                counting_rule="Count each declared event occurrence.",
            ),
            fields=(EventField(name="qualified", value_type="BOOLEAN", required=True),),
        ),
        identity=Ga4AdminKeyEventIdentity(
            property_ref=f"ga4_admin.property.{_PROPERTY_ID}",
            property_client_ref=_PROPERTY_ID,
            web_stream_ref=f"ga4_admin.web_stream.{_STREAM_ID}",
            web_stream_client_ref=_STREAM_ID,
        ),
        counting_method="ONCE_PER_EVENT",
        default_value=(
            None
            if default_value is None
            else Ga4AdminKeyEventDefaultValueInput(
                numeric_value=default_value,
                currency_code="USD",
            )
        ),
    )


def _target() -> Ga4AdminEditInventoryTarget:
    return Ga4AdminEditInventoryTarget(
        property_ref=f"ga4_admin.property.{_PROPERTY_ID}",
        property_client_ref=_PROPERTY_ID,
        web_stream_ref=f"ga4_admin.web_stream.{_STREAM_ID}",
        web_stream_client_ref=_STREAM_ID,
        expected_timezone="America/Chicago",
        expected_currency="USD",
    )


def _event(
    resource_id: str,
    event_name: str,
    *,
    counting_method: str = "ONCE_PER_EVENT",
    default_value: object | None = None,
    custom: bool = True,
    deletable: bool = True,
    create_time: str | None = "2026-09-05T12:00:00Z",
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": f"properties/{_PROPERTY_ID}/keyEvents/{resource_id}",
        "eventName": event_name,
        "countingMethod": counting_method,
        "custom": custom,
        "deletable": deletable,
    }
    if create_time is not None:
        result["createTime"] = create_time
    if default_value is not None:
        result["defaultValue"] = default_value
    return result


def _inventory(
    key_events: list[dict[str, object]],
    *,
    fingerprint_key: bytes = _KEY,
) -> Ga4AdminEditKeyEventInventory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1beta/properties/{_PROPERTY_ID}":
            assert tuple(request.url.params.multi_items()) == (
                ("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),
            )
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{_PROPERTY_ID}",
                    "timeZone": "America/Chicago",
                    "currencyCode": "USD",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                },
            )
        if request.url.path == (f"/v1beta/properties/{_PROPERTY_ID}/dataStreams/{_STREAM_ID}"):
            assert tuple(request.url.params.multi_items()) == (("fields", "name,type"),)
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{_PROPERTY_ID}/dataStreams/{_STREAM_ID}",
                    "type": "WEB_DATA_STREAM",
                },
            )
        assert request.url.path == f"/v1alpha/properties/{_PROPERTY_ID}/keyEvents"
        assert tuple(request.url.params.multi_items()) == (
            ("pageSize", "200"),
            ("fields", _KEY_EVENT_FIELDS),
        )
        return httpx.Response(200, json={"keyEvents": key_events})

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}, read_attempts=1),
            lambda: "fixture-edit-token",
            fingerprint_key=fingerprint_key,
        ).read_inventory(_target())
    finally:
        client.close()


def _build(
    fresh: Ga4AdminKeyEventCreateInput,
    inventory: Ga4AdminEditKeyEventInventory,
) -> semantic_state.Ga4AdminKeyEventCreateSemanticState:
    return semantic_state.build_ga4_admin_key_event_create_semantic_state(
        fresh,
        inventory,
        fingerprint_key=_KEY,
    )


def _assert_invalid_state(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE"
    assert error.value.exit_code is ExitCode.INVALID_INPUT
    assert "planned_event" not in error.value.message
    assert _PROPERTY_ID not in error.value.message
    assert _KEY.hex() not in error.value.message


def test_build_is_pure_redacted_and_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh(default_value=Decimal("1.25"))
    inventory = _inventory([_event("701", "private_existing_event")])
    fresh_before = fresh.model_dump(mode="json")
    inventory_before = inventory.model_dump(mode="json")

    def prohibited_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("semantic-state builder must not perform I/O")

    monkeypatch.setattr(builtins, "open", prohibited_io)
    monkeypatch.setattr(socket, "socket", prohibited_io)
    state = _build(fresh, inventory)
    rendered = state.model_dump(mode="json")

    assert fresh.model_dump(mode="json") == fresh_before
    assert inventory.model_dump(mode="json") == inventory_before
    assert state.before_member_count == 1
    assert state.after_member_count == 2
    assert state.preconditions.complete_observed_key_event_total == state.before_member_count
    assert state.before.fingerprint != state.after.fingerprint
    assert state.preconditions.current_provider_state == "NOT_ESTABLISHED"
    assert state.preconditions.reservation_status == "NOT_ESTABLISHED"
    assert state.preconditions.permission_status == "NOT_EVALUATED"
    assert state.preconditions.remote_side_effects is False
    assert state.preconditions.execution_authority is False
    redacted = json.dumps(rendered, sort_keys=True)
    for private in (
        "planned_event",
        "private_existing_event",
        f"properties/{_PROPERTY_ID}",
        f"dataStreams/{_STREAM_ID}",
        "keyEvents/701",
        _KEY.hex(),
        inventory.fingerprint_key_marker,
        inventory.inventory_content_marker,
        inventory.key_events[0].event_name_fingerprint,
    ):
        assert private not in redacted
    build_signature = inspect.signature(
        semantic_state.build_ga4_admin_key_event_create_semantic_state
    )
    assert set(build_signature.parameters) == {
        "fresh",
        "inventory",
        "fingerprint_key",
    }


def test_constructor_and_serializer_bind_before_count_to_precondition_total() -> None:
    state = _build(_fresh(), _inventory([_event("701", "existing")]))

    with pytest.raises(ValidationError):
        semantic_state.Ga4AdminKeyEventCreateSemanticState(
            target_binding_fingerprint=state.target_binding_fingerprint,
            candidate_digest=state.candidate_digest,
            inventory_digest=state.inventory_digest,
            before=state.before,
            after=state.after,
            before_member_count=0,
            after_member_count=1,
            preconditions=state.preconditions,
        )

    forged = state.model_copy(update={"before_member_count": 0, "after_member_count": 1})
    with pytest.raises(ValueError, match="semantic create state is invalid"):
        forged.model_dump_json()


def test_serializer_rejects_equal_or_incomplete_constructed_state_without_disclosure() -> None:
    state = _build(_fresh(), _inventory([_event("701", "existing")]))

    with pytest.raises(ValidationError):
        semantic_state.Ga4AdminKeyEventCreateSemanticState(
            target_binding_fingerprint=state.target_binding_fingerprint,
            candidate_digest=state.candidate_digest,
            inventory_digest=state.inventory_digest,
            before=state.before,
            after=state.before,
            before_member_count=state.before_member_count,
            after_member_count=state.after_member_count,
            preconditions=state.preconditions,
        )

    equal_refs = state.model_copy(update={"after": state.before})
    with pytest.raises(ValueError, match="semantic create state is invalid"):
        equal_refs.model_dump(mode="json")

    incomplete = semantic_state.Ga4AdminKeyEventCreateSemanticState.model_construct(
        schema_version=1,
        target_binding_fingerprint="private-constructed-sentinel",
    )
    with pytest.raises(ValueError, match="semantic create state is invalid") as raised:
        incomplete.model_dump_json()
    assert "private-constructed-sentinel" not in str(raised.value)


def test_serializers_reject_constructed_or_copied_authority_and_scalar_bypasses() -> None:
    inventory = _inventory([_event("701", "existing")])
    state = _build(_fresh(), inventory)
    projection = semantic_state.project_ga4_admin_edit_key_event_semantic_collection(
        inventory,
        fingerprint_key=_KEY,
    )

    forged_projection = projection.model_copy(update={"execution_authority": True})
    with pytest.raises(ValueError, match="semantic projection is invalid"):
        forged_projection.model_dump_json()

    forged_preconditions = state.preconditions.model_copy(update={"remote_side_effects": True})
    forged_state = state.model_copy(update={"preconditions": forged_preconditions})
    with pytest.raises(ValueError, match="semantic create state is invalid"):
        forged_state.model_dump_json()

    incomplete_projection = semantic_state.Ga4AdminEditKeyEventSemanticProjection.model_construct(
        target_binding_fingerprint="private-projection-sentinel"
    )
    with pytest.raises(ValueError, match="semantic projection is invalid") as raised:
        incomplete_projection.model_dump_json()
    assert "private-projection-sentinel" not in str(raised.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.__setitem__("schema_version", True),
        lambda raw: raw.__setitem__("before_member_count", 1.0),
        lambda raw: raw.__setitem__("after_member_count", True),
        lambda raw: raw["before"].__setitem__("revision", True),  # type: ignore[index,union-attr]
        lambda raw: raw["preconditions"].__setitem__(  # type: ignore[index,union-attr]
            "complete_observed_key_event_total", 1.0
        ),
    ],
)
def test_wire_revalidator_rejects_exact_scalar_aliases(
    mutate: Any,
) -> None:
    fresh = _fresh()
    inventory = _inventory([_event("701", "existing")])
    state = _build(fresh, inventory)
    forged: dict[str, Any] = state.model_dump(mode="json")
    mutate(forged)

    with pytest.raises(AdhelmError) as raised:
        semantic_state.revalidate_ga4_admin_key_event_create_semantic_state(
            forged,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_invalid_state(raised)


def test_revalidator_rejects_forged_count_and_equal_state_refs() -> None:
    fresh = _fresh()
    inventory = _inventory([_event("701", "existing")])
    state = _build(fresh, inventory)

    forged_count: dict[str, Any] = state.model_dump(mode="json")
    forged_count["before_member_count"] = 0
    forged_count["after_member_count"] = 1
    with pytest.raises(AdhelmError) as raised:
        semantic_state.revalidate_ga4_admin_key_event_create_semantic_state(
            forged_count,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_invalid_state(raised)

    equal_state: dict[str, Any] = state.model_dump(mode="json")
    equal_state["after"] = dict(equal_state["before"])
    with pytest.raises(AdhelmError) as raised:
        semantic_state.revalidate_ga4_admin_key_event_create_semantic_state(
            equal_state,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_invalid_state(raised)


def test_direct_typed_model_is_not_a_generic_wire_parser_but_revalidator_is() -> None:
    fresh = _fresh()
    inventory = _inventory([_event("701", "existing")])
    state = _build(fresh, inventory)
    wire = state.model_dump(mode="json")

    with pytest.raises(ValidationError):
        semantic_state.Ga4AdminKeyEventCreateSemanticState.model_validate(wire)

    assert (
        semantic_state.revalidate_ga4_admin_key_event_create_semantic_state(
            wire,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
        == state
    )


def test_projection_matches_post_create_semantics_across_arbitrary_provider_ids() -> None:
    baseline = _inventory([_event("701", "existing")])
    fresh = _fresh(default_value=Decimal("1.25"))
    planned = _build(fresh, baseline)

    after_one = _inventory(
        [
            _event("701", "existing"),
            _event(
                "900",
                "planned_event",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
                custom=False,
                deletable=False,
                create_time="2026-09-05T13:00:00Z",
            ),
        ]
    )
    after_two = _inventory(
        [
            _event("702", "existing", custom=False, deletable=False),
            _event(
                "987654320",
                "planned_event",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
                custom=True,
                deletable=True,
                create_time="2026-09-06T13:00:00Z",
            ),
        ]
    )

    first = semantic_state.project_ga4_admin_edit_key_event_semantic_collection(
        after_one,
        fingerprint_key=_KEY,
    )
    second = semantic_state.project_ga4_admin_edit_key_event_semantic_collection(
        after_two,
        fingerprint_key=_KEY,
    )
    assert first.state == planned.after
    assert second.state == planned.after
    assert first.inventory_digest != second.inventory_digest
    assert first.member_count == planned.after_member_count
    assert second.member_count == planned.after_member_count


def test_projection_detects_semantic_drift_and_never_promotes_inventory_authority() -> None:
    baseline = _inventory([_event("701", "existing")])
    planned = _build(_fresh(default_value=Decimal("1.25")), baseline)
    drifted = _inventory(
        [
            _event("701", "existing"),
            _event(
                "900",
                "planned_event",
                counting_method="ONCE_PER_SESSION",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
            ),
        ]
    )

    projection = semantic_state.project_ga4_admin_edit_key_event_semantic_collection(
        drifted,
        fingerprint_key=_KEY,
    )
    assert projection.state != planned.after
    assert projection.current_provider_state == "NOT_ESTABLISHED"
    assert projection.reservation_status == "NOT_ESTABLISHED"
    assert projection.permission_status == "NOT_EVALUATED"
    assert projection.remote_side_effects is False
    assert projection.execution_authority is False


def test_state_revalidation_requires_current_keyed_inventory_and_exact_typed_inputs() -> None:
    fresh = _fresh()
    inventory = _inventory([_event("701", "existing")])
    state = _build(fresh, inventory)

    with pytest.raises(AdhelmError) as raised:
        semantic_state.revalidate_ga4_admin_key_event_create_semantic_state(
            state.model_dump(mode="json"),
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_OTHER_KEY,
        )
    assert raised.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"

    with pytest.raises(AdhelmError) as raised:
        semantic_state.build_ga4_admin_key_event_create_semantic_state(
            fresh.model_dump(mode="json"),  # type: ignore[arg-type]
            inventory,
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE_INPUT"


def test_keyed_revalidation_rebuilds_opaque_target_and_state_bindings() -> None:
    fresh = _fresh()
    inventory = _inventory([_event("701", "existing")])
    state = _build(fresh, inventory)
    forged = state.model_copy(update={"target_binding_fingerprint": "a" * 64})

    with pytest.raises(AdhelmError) as raised:
        semantic_state.revalidate_ga4_admin_key_event_create_semantic_state(
            forged,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_invalid_state(raised)

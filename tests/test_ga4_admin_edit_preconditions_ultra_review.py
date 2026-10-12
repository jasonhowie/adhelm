"""Independent adversarial review of pure GA4 Admin EDIT create preconditions."""

from __future__ import annotations

import builtins
import inspect
import json
import socket
from decimal import Decimal

import httpx
import pytest

import adhelm.providers.ga4_admin_edit_preconditions as preconditions
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
    Ga4AdminEditStandardKeyEventCapacity,
)
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_KEY_EVENT_FIELDS = (
    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),"
    "nextPageToken"
)


def _fresh(
    *,
    event_name: str = "qualified_request",
    property_client_ref: str = "123",
    web_stream_client_ref: str = "456",
    default_value: bool = False,
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
            property_ref=f"ga4_admin.property.{property_client_ref}",
            property_client_ref=property_client_ref,
            web_stream_ref=f"ga4_admin.web_stream.{web_stream_client_ref}",
            web_stream_client_ref=web_stream_client_ref,
        ),
        counting_method="ONCE_PER_EVENT",
        default_value=(
            Ga4AdminKeyEventDefaultValueInput(numeric_value=Decimal("1.25"), currency_code="USD")
            if default_value
            else None
        ),
    )


def _inventory(
    key: bytes,
    *,
    property_client_ref: str = "123",
    web_stream_client_ref: str = "456",
    service_level: str = "GOOGLE_ANALYTICS_STANDARD",
    event_names: tuple[str, ...] = (),
) -> Ga4AdminEditKeyEventInventory:
    target = Ga4AdminEditInventoryTarget(
        property_ref=f"ga4_admin.property.{property_client_ref}",
        property_client_ref=property_client_ref,
        web_stream_ref=f"ga4_admin.web_stream.{web_stream_client_ref}",
        web_stream_client_ref=web_stream_client_ref,
        expected_timezone="UTC",
        expected_currency="USD",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1beta/properties/{property_client_ref}":
            assert tuple(request.url.params.multi_items()) == (
                ("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),
            )
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{property_client_ref}",
                    "timeZone": "UTC",
                    "currencyCode": "USD",
                    "serviceLevel": service_level,
                },
            )
        if request.url.path == (
            f"/v1beta/properties/{property_client_ref}/dataStreams/{web_stream_client_ref}"
        ):
            assert tuple(request.url.params.multi_items()) == (("fields", "name,type"),)
            return httpx.Response(
                200,
                json={
                    "name": (
                        f"properties/{property_client_ref}/dataStreams/{web_stream_client_ref}"
                    ),
                    "type": "WEB_DATA_STREAM",
                },
            )
        assert request.url.path == f"/v1alpha/properties/{property_client_ref}/keyEvents"
        assert tuple(request.url.params.multi_items()) == (
            ("pageSize", "200"),
            ("fields", _KEY_EVENT_FIELDS),
        )
        return httpx.Response(
            200,
            json={
                "keyEvents": [
                    {
                        "name": f"properties/{property_client_ref}/keyEvents/{index + 700}",
                        "eventName": name,
                        "custom": True,
                        "deletable": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                    for index, name in enumerate(event_names)
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: "fixture-edit-token",
            fingerprint_key=key,
        ).read_inventory(target)
    finally:
        client.close()


def _assert_code(error: pytest.ExceptionInfo[AdhelmError], code: str) -> None:
    assert error.value.code == code
    assert error.value.exit_code in {ExitCode.INVALID_INPUT, ExitCode.PRECONDITION}
    assert "qualified_request" not in error.value.message
    assert "123" not in error.value.message
    assert _KEY.hex() not in error.value.message


def _evaluate(
    fresh: Ga4AdminKeyEventCreateInput,
    supplied: Ga4AdminEditKeyEventInventory,
    *,
    fingerprint_key: bytes = _KEY,
) -> preconditions.Ga4AdminEditKeyEventCreatePreconditions:
    return preconditions.evaluate_ga4_edit_key_event_create_preconditions(
        fresh,
        supplied,
        fingerprint_key=fingerprint_key,
    )


def test_success_is_pure_redacted_and_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _fresh(default_value=True)
    supplied = _inventory(_KEY, event_names=("already_different",))
    before_fresh = fresh.model_dump(mode="json")
    before_inventory = supplied.model_dump(mode="json")

    def prohibited_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure preconditions must not perform I/O")

    monkeypatch.setattr(builtins, "open", prohibited_io)
    monkeypatch.setattr(socket, "socket", prohibited_io)
    result = _evaluate(fresh, supplied)
    rendered = result.model_dump(mode="json")

    assert fresh.model_dump(mode="json") == before_fresh
    assert supplied.model_dump(mode="json") == before_inventory
    assert rendered["complete_observed_key_event_total"] == 1
    assert rendered["remaining_documented_slots"] == 29
    assert rendered["current_provider_state"] == "NOT_ESTABLISHED"
    assert rendered["reservation_status"] == "NOT_ESTABLISHED"
    assert rendered["permission_status"] == "NOT_EVALUATED"
    assert rendered["remote_side_effects"] is False
    assert rendered["execution_authority"] is False
    redacted = json.dumps(rendered, sort_keys=True)
    for private_value in (
        "qualified_request",
        "already_different",
        "ga4_admin.property.123",
        "456",
        _KEY.hex(),
        supplied.fingerprint_key_marker,
        supplied.inventory_content_marker,
        supplied.key_events[0].event_name_fingerprint,
    ):
        assert private_value not in redacted
    assert set(
        inspect.signature(preconditions.evaluate_ga4_edit_key_event_create_preconditions).parameters
    ) == {
        "fresh",
        "supplied",
        "fingerprint_key",
    }


def test_only_the_exact_typed_edit_inventory_can_enter_the_gate() -> None:
    supplied = _inventory(_KEY)

    class InventorySubclass(Ga4AdminEditKeyEventInventory):
        pass

    subclass = InventorySubclass.model_validate(supplied.model_dump(mode="python"))
    for forged in (supplied.model_dump(mode="json"), subclass):
        with pytest.raises(AdhelmError) as raised:
            _evaluate(_fresh(), forged)  # type: ignore[arg-type]
        _assert_code(raised, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")


@pytest.mark.parametrize(
    "forged_fresh",
    [
        lambda: _fresh().model_copy(update={"counting_method": True}),
        lambda: _fresh().model_copy(
            update={"identity": _fresh().identity.model_copy(update={"api_version": "v1beta"})}
        ),
        lambda: _fresh().model_copy(
            update={
                "event_contract": _fresh().event_contract.model_copy(
                    update={
                        "fields": (
                            _fresh().event_contract.fields[0].model_copy(update={"required": 1}),
                        )
                    }
                )
            }
        ),
        lambda: _fresh(default_value=True).model_copy(
            update={
                "default_value": Ga4AdminKeyEventDefaultValueInput.model_construct(
                    numeric_value=1.0,
                    currency_code="USD",
                )
            }
        ),
    ],
)
def test_fresh_compiler_input_rejects_constructed_scalar_and_identity_bypasses(
    forged_fresh: object,
) -> None:
    with pytest.raises(AdhelmError) as raised:
        _evaluate(forged_fresh(), _inventory(_KEY))  # type: ignore[operator]
    _assert_code(raised, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")


def test_target_identity_api_scope_and_edit_profile_must_match() -> None:
    with pytest.raises(AdhelmError) as property_mismatch:
        _evaluate(_fresh(property_client_ref="999"), _inventory(_KEY))
    _assert_code(property_mismatch, "GA4_ADMIN_EDIT_INVENTORY_IDENTITY_MISMATCH")

    with pytest.raises(AdhelmError) as stream_mismatch:
        _evaluate(_fresh(web_stream_client_ref="999"), _inventory(_KEY))
    _assert_code(stream_mismatch, "GA4_ADMIN_EDIT_INVENTORY_IDENTITY_MISMATCH")

    supplied = _inventory(_KEY)
    for target_update in (
        {"required_scope": "https://www.googleapis.com/auth/analytics.readonly"},
        {"profile_purpose": "READ"},
        {"admin_api_version": "v1beta"},
    ):
        forged = supplied.model_copy(
            update={"target": supplied.target.model_copy(update=target_update)}
        )
        with pytest.raises(AdhelmError) as raised:
            _evaluate(_fresh(), forged)
        _assert_code(raised, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")


def test_key_rotation_content_tamper_and_raw_capacity_aliases_fail_before_duplicate_logic() -> None:
    rotated = _inventory(_OTHER_KEY, event_names=("qualified_request",))
    with pytest.raises(AdhelmError) as rotation:
        _evaluate(_fresh(), rotated)
    _assert_code(rotation, "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH")

    supplied = _inventory(_KEY, event_names=("first", "second"))
    reordered = supplied.model_copy(update={"key_events": tuple(reversed(supplied.key_events))})
    with pytest.raises(AdhelmError) as content:
        _evaluate(_fresh(), reordered)
    _assert_code(content, "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH")

    capacity_values = supplied.capacity.model_dump(mode="python")
    capacity_values["complete_observed_key_event_total"] = 2.0
    capacity_values["remaining_documented_slots"] = 28
    forged_capacity = Ga4AdminEditStandardKeyEventCapacity.model_construct(**capacity_values)
    with pytest.raises(AdhelmError) as raw_scalar:
        _evaluate(_fresh(), supplied.model_copy(update={"capacity": forged_capacity}))
    _assert_code(raw_scalar, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")


def test_duplicate_and_standard_capacity_gates_do_not_invent_permission_or_reservation() -> None:
    with pytest.raises(AdhelmError) as duplicate:
        _evaluate(_fresh(), _inventory(_KEY, event_names=("qualified_request",)))
    _assert_code(duplicate, "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY")

    below_cap = _evaluate(
        _fresh(),
        _inventory(_KEY, event_names=tuple(f"existing_{index}" for index in range(29))),
    )
    assert below_cap.complete_observed_key_event_total == 29
    assert below_cap.remaining_documented_slots == 1
    assert below_cap.permission_status == "NOT_EVALUATED"
    assert below_cap.reservation_status == "NOT_ESTABLISHED"

    with pytest.raises(AdhelmError) as at_cap:
        _evaluate(
            _fresh(),
            _inventory(_KEY, event_names=tuple(f"existing_{index}" for index in range(30))),
        )
    _assert_code(at_cap, "GA4_KEY_EVENT_DOCUMENTED_CAP_REACHED")

    with pytest.raises(AdhelmError) as nonstandard:
        _evaluate(_fresh(), _inventory(_KEY, service_level="GOOGLE_ANALYTICS_360"))
    _assert_code(nonstandard, "GA4_KEY_EVENT_CAPACITY_NOT_EVIDENCED")


@pytest.mark.parametrize(
    "update",
    [
        {"execution_authority": True},
        {"remote_side_effects": True},
        {"current_provider_state": "CURRENT"},
        {"reservation_status": "RESERVED"},
        {"permission_status": "GRANTED"},
        {"complete_observed_key_event_total": 1.0},
        {"candidate_digest": "private-result-digest-sentinel"},
    ],
)
def test_agent_visible_result_cannot_serialize_copied_authority_or_private_data(
    update: dict[str, object],
) -> None:
    result = _evaluate(_fresh(), _inventory(_KEY))
    forged = result.model_copy(update=update)

    with pytest.raises(ValueError) as raised:
        forged.model_dump(mode="json")

    rendered_error = str(raised.value)
    assert "private-result-digest-sentinel" not in rendered_error
    assert "qualified_request" not in rendered_error
    assert _KEY.hex() not in rendered_error


def test_agent_visible_result_rejects_incomplete_constructed_shape_without_raw_field_errors() -> (
    None
):
    forged = preconditions.Ga4AdminEditKeyEventCreatePreconditions.model_construct(
        candidate_digest="private-result-digest-sentinel"
    )

    with pytest.raises(ValueError) as raised:
        forged.model_dump(mode="json")

    rendered_error = str(raised.value)
    assert "private-result-digest-sentinel" not in rendered_error
    assert "AttributeError" not in rendered_error
    assert "candidate_digest" not in rendered_error

"""Fixture-only tests for supplied-inventory GA4 Admin EDIT CREATE preconditions."""

from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
    compile_ga4_admin_key_event_create,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
    Ga4AdminEditStandardKeyEventCapacity,
)
from adhelm.providers.ga4_admin_edit_preconditions import (
    Ga4AdminEditKeyEventCreatePreconditions,
    evaluate_ga4_edit_key_event_create_preconditions,
)
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"


def _fresh(
    *,
    property_client_ref: str = "123",
    web_stream_client_ref: str = "456",
    include_default_value: bool = False,
) -> Ga4AdminKeyEventCreateInput:
    return Ga4AdminKeyEventCreateInput(
        event_contract=EventContract(
            event_contract_ref="event.fixture",
            event_name="qualified_request",
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
            if include_default_value
            else None
        ),
    )


def _inventory(
    key: bytes,
    *,
    property_client_ref: str = "123",
    web_stream_client_ref: str = "456",
    service_level: str = "GOOGLE_ANALYTICS_STANDARD",
    key_event_names: tuple[str, ...] = (),
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
        return httpx.Response(
            200,
            json={
                "keyEvents": [
                    {
                        "name": f"properties/{property_client_ref}/keyEvents/{700 + index}",
                        "eventName": name,
                        "custom": True,
                        "deletable": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                    for index, name in enumerate(key_event_names)
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
    assert "qualified_request" not in error.value.message


def test_returns_redacted_standard_capacity_facts_without_authority() -> None:
    supplied = _inventory(_KEY, key_event_names=("existing_event",))

    result = evaluate_ga4_edit_key_event_create_preconditions(
        _fresh(include_default_value=True),
        supplied,
        fingerprint_key=_KEY,
    )

    assert result.model_dump(mode="json") == {
        "schema_version": 1,
        "candidate_digest": compile_ga4_admin_key_event_create(
            _fresh(include_default_value=True)
        ).candidate_digest,
        "inventory_digest": digest(supplied),
        "inventory_scope": "COMPLETE_SUPPLIED_EDIT_INVENTORY",
        "fingerprint_key_continuity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY",
        "inventory_content_integrity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT",
        "observed_service_level": "GOOGLE_ANALYTICS_STANDARD",
        "complete_observed_key_event_total": 1,
        "documented_key_event_cap": 30,
        "remaining_documented_slots": 29,
        "capacity_status": "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        "duplicate_status": "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY",
        "current_provider_state": "NOT_ESTABLISHED",
        "reservation_status": "NOT_ESTABLISHED",
        "permission_status": "NOT_EVALUATED",
        "remote_side_effects": False,
        "execution_authority": False,
    }
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    for confidential in (
        "qualified_request",
        "existing_event",
        "ga4_admin.property.123",
        "456",
        _KEY.hex(),
        supplied.fingerprint_key_marker,
        supplied.inventory_content_marker,
        supplied.key_events[0].event_name_fingerprint,
    ):
        assert confidential not in rendered
    assert set(inspect.signature(evaluate_ga4_edit_key_event_create_preconditions).parameters) == {
        "fresh",
        "supplied",
        "fingerprint_key",
    }


def test_marker_validation_precedes_duplicate_comparison_and_member_tamper() -> None:
    duplicate = _inventory(_OTHER_KEY, key_event_names=("qualified_request",))
    with pytest.raises(AdhelmError) as wrong_key:
        evaluate_ga4_edit_key_event_create_preconditions(_fresh(), duplicate, fingerprint_key=_KEY)
    _assert_code(wrong_key, "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH")

    source = _inventory(_KEY, key_event_names=("existing_one", "existing_two"))
    reordered = source.model_copy(update={"key_events": tuple(reversed(source.key_events))})
    with pytest.raises(AdhelmError) as reordered_error:
        evaluate_ga4_edit_key_event_create_preconditions(_fresh(), reordered, fingerprint_key=_KEY)
    _assert_code(reordered_error, "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH")

    original = source.key_events[0]
    tampered_member = original.model_copy(
        update={"event_name_fingerprint": hashlib.sha256(b"qualified_request").hexdigest()}
    )
    tampered = source.model_copy(update={"key_events": (tampered_member, source.key_events[1])})
    with pytest.raises(AdhelmError) as tampered_error:
        evaluate_ga4_edit_key_event_create_preconditions(_fresh(), tampered, fingerprint_key=_KEY)
    _assert_code(tampered_error, "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH")


def test_missing_marker_and_matching_duplicate_fail_closed() -> None:
    source = _inventory(_KEY)
    markerless_values = source.model_dump(mode="python")
    markerless_values["fingerprint_key_marker"] = None
    markerless = Ga4AdminEditKeyEventInventory.model_construct(**markerless_values)
    with pytest.raises(AdhelmError) as markerless_error:
        evaluate_ga4_edit_key_event_create_preconditions(_fresh(), markerless, fingerprint_key=_KEY)
    _assert_code(markerless_error, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")

    duplicate = _inventory(_KEY, key_event_names=("qualified_request",))
    with pytest.raises(AdhelmError) as duplicate_error:
        evaluate_ga4_edit_key_event_create_preconditions(_fresh(), duplicate, fingerprint_key=_KEY)
    _assert_code(duplicate_error, "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY")
    assert duplicate_error.value.exit_code == ExitCode.PRECONDITION


def test_rejects_target_mismatch_and_nonstandard_or_exhausted_capacity() -> None:
    with pytest.raises(AdhelmError) as mismatch:
        evaluate_ga4_edit_key_event_create_preconditions(
            _fresh(property_client_ref="999"), _inventory(_KEY), fingerprint_key=_KEY
        )
    _assert_code(mismatch, "GA4_ADMIN_EDIT_INVENTORY_IDENTITY_MISMATCH")

    with pytest.raises(AdhelmError) as unknown_cap:
        evaluate_ga4_edit_key_event_create_preconditions(
            _fresh(),
            _inventory(_KEY, service_level="GOOGLE_ANALYTICS_360"),
            fingerprint_key=_KEY,
        )
    _assert_code(unknown_cap, "GA4_KEY_EVENT_CAPACITY_NOT_EVIDENCED")

    at_cap = _inventory(_KEY, key_event_names=tuple(f"existing_{index}" for index in range(30)))
    with pytest.raises(AdhelmError) as exhausted:
        evaluate_ga4_edit_key_event_create_preconditions(_fresh(), at_cap, fingerprint_key=_KEY)
    _assert_code(exhausted, "GA4_KEY_EVENT_DOCUMENTED_CAP_REACHED")


def test_rejects_constructed_raw_type_and_mapping_bypasses() -> None:
    source = _fresh()
    forged_fresh = Ga4AdminKeyEventCreateInput.model_construct(
        event_contract=source.event_contract,
        identity=source.identity,
        counting_method=True,
        default_value=source.default_value,
    )
    with pytest.raises(AdhelmError) as fresh_error:
        evaluate_ga4_edit_key_event_create_preconditions(
            forged_fresh, _inventory(_KEY), fingerprint_key=_KEY
        )
    _assert_code(fresh_error, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")

    forged_default = Ga4AdminKeyEventDefaultValueInput.model_construct(
        numeric_value=1.0,
        currency_code="USD",
    )
    with pytest.raises(AdhelmError) as default_error:
        evaluate_ga4_edit_key_event_create_preconditions(
            source.model_copy(update={"default_value": forged_default}),
            _inventory(_KEY),
            fingerprint_key=_KEY,
        )
    _assert_code(default_error, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")

    supplied = _inventory(_KEY, key_event_names=("existing_event",))
    capacity_values = supplied.capacity.model_dump(mode="python")
    capacity_values["complete_observed_key_event_total"] = 1.0
    capacity_values["remaining_documented_slots"] = 29
    forged_capacity = Ga4AdminEditStandardKeyEventCapacity.model_construct(**capacity_values)
    forged_inventory = supplied.model_copy(update={"capacity": forged_capacity})
    with pytest.raises(AdhelmError) as inventory_error:
        evaluate_ga4_edit_key_event_create_preconditions(
            _fresh(), forged_inventory, fingerprint_key=_KEY
        )
    _assert_code(inventory_error, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")

    with pytest.raises(AdhelmError) as mapping_error:
        evaluate_ga4_edit_key_event_create_preconditions(
            _fresh(),
            supplied.model_dump(mode="json"),
            fingerprint_key=_KEY,  # type: ignore[arg-type]
        )
    _assert_code(mapping_error, "INVALID_GA4_ADMIN_EDIT_PRECONDITION")


@pytest.mark.parametrize("fingerprint_key", [b"short", bytearray(_KEY), True])
def test_rejects_non_bytes_or_short_fingerprint_keys(fingerprint_key: object) -> None:
    with pytest.raises(AdhelmError) as key_error:
        evaluate_ga4_edit_key_event_create_preconditions(
            _fresh(),
            _inventory(_KEY),
            fingerprint_key=fingerprint_key,  # type: ignore[arg-type]
        )
    _assert_code(key_error, "FINGERPRINT_KEY_REQUIRED")


def test_result_rejects_numeric_and_boolean_aliases() -> None:
    result = evaluate_ga4_edit_key_event_create_preconditions(
        _fresh(), _inventory(_KEY), fingerprint_key=_KEY
    )
    raw = result.model_dump(mode="json")
    for name, replacement in (
        ("schema_version", 1.0),
        ("complete_observed_key_event_total", False),
        ("documented_key_event_cap", 30.0),
        ("remaining_documented_slots", 30.0),
        ("execution_authority", 0),
    ):
        altered = dict(raw)
        altered[name] = replacement
        with pytest.raises(ValidationError):
            Ga4AdminEditKeyEventCreatePreconditions.model_validate(altered)


def test_result_serializer_rejects_constructed_or_copied_result_bypasses() -> None:
    result = evaluate_ga4_edit_key_event_create_preconditions(
        _fresh(), _inventory(_KEY), fingerprint_key=_KEY
    )
    forged_results = (
        result.model_copy(update={"execution_authority": True}),
        result.model_copy(update={"remote_side_effects": True}),
        result.model_copy(update={"current_provider_state": "CURRENT"}),
        result.model_copy(update={"reservation_status": "RESERVED"}),
        result.model_copy(update={"permission_status": "GRANTED"}),
        result.model_copy(update={"complete_observed_key_event_total": 1}),
        Ga4AdminEditKeyEventCreatePreconditions.model_construct(
            **{**result.__dict__, "candidate_digest": "private-sentinel"}
        ),
        Ga4AdminEditKeyEventCreatePreconditions.model_construct(
            **{**result.__dict__, "schema_version": 1.0}
        ),
        Ga4AdminEditKeyEventCreatePreconditions.model_construct(),
    )

    for forged in forged_results:
        with pytest.raises(ValueError) as raised:
            forged.model_dump(mode="json")
        assert "private-sentinel" not in str(raised.value)

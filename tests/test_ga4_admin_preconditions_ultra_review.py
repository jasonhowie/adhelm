"""Independent adversarial checks for GA4 supplied-inventory preconditions.

Every fixture is constructed locally. These tests do not instantiate a client,
read an OS credential, or make an HTTP request.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers import ga4_admin_preconditions as preconditions
from adhelm.providers.ga4_admin import (
    Ga4AdminIdentity,
    Ga4AdminInventory,
    Ga4AdminKeyEvent,
    _fingerprint,
    _fingerprint_inventory_content_marker,
    _fingerprint_key_marker,
)
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
)
from adhelm.providers.ga4_admin_preconditions import (
    Ga4AdminKeyEventInventoryPreconditions,
    evaluate_ga4_key_event_create_preconditions,
)

_KEY = b"a" * 32
_FOREIGN_KEY = b"b" * 32
_EVENT_NAME = "qualified_request"


def _identity() -> Ga4AdminIdentity:
    return Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )


def _create_input() -> Ga4AdminKeyEventCreateInput:
    return Ga4AdminKeyEventCreateInput(
        event_contract=EventContract(
            event_contract_ref="event.ultra",
            event_name=_EVENT_NAME,
            semantics=ConversionSemantics(
                semantics_ref="semantics.ultra",
                customer_definition="Customer-declared qualification event.",
                counting_rule="Count each declared event occurrence.",
            ),
            fields=(EventField(name="qualified", value_type="BOOLEAN", required=True),),
        ),
        identity=Ga4AdminKeyEventIdentity(
            property_ref="ga4_admin.property.123",
            property_client_ref="123",
            web_stream_ref="ga4_admin.web_stream.456",
            web_stream_client_ref="456",
        ),
        counting_method="ONCE_PER_EVENT",
    )


def _key_event(
    resource_id: str,
    event_name: str,
    *,
    fingerprint_key: bytes = _KEY,
    property_ref: str = "ga4_admin.property.123",
) -> Ga4AdminKeyEvent:
    return Ga4AdminKeyEvent(
        resource_ref=f"ga4_admin.key_event.{resource_id}",
        resource_client_ref=resource_id,
        property_ref=property_ref,
        configuration_fingerprint="1" * 64,
        event_name_fingerprint=_fingerprint(fingerprint_key, "key-event-name", event_name),
        counting_method="ONCE_PER_EVENT",
        custom=True,
        deletable=True,
    )


def _inventory(
    key_events: tuple[Ga4AdminKeyEvent, ...] = (), *, fingerprint_key: bytes = _KEY
) -> Ga4AdminInventory:
    unsigned = Ga4AdminInventory(
        identity=_identity(),
        key_events=key_events,
        fingerprint_key_marker=_fingerprint_key_marker(fingerprint_key),
    )
    return Ga4AdminInventory(
        identity=unsigned.identity,
        key_events=unsigned.key_events,
        event_create_rules=unsigned.event_create_rules,
        event_edit_rules=unsigned.event_edit_rules,
        pagination_verified=unsigned.pagination_verified,
        fingerprint_key_marker=unsigned.fingerprint_key_marker,
        inventory_content_marker=_fingerprint_inventory_content_marker(fingerprint_key, unsigned),
    )


def _assert_invalid_precondition(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "INVALID_GA4_ADMIN_PRECONDITION"
    assert _EVENT_NAME not in error.value.message


def test_foreign_key_marker_fails_before_candidate_event_hmac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(
        (_key_event("701", _EVENT_NAME, fingerprint_key=_FOREIGN_KEY),),
        fingerprint_key=_FOREIGN_KEY,
    )

    def event_hmac_must_not_run(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("candidate event HMAC ran before key-marker continuity")

    monkeypatch.setattr(preconditions, "_fingerprint", event_hmac_must_not_run)

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), inventory, fingerprint_key=_KEY
        )

    assert error.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"
    assert _EVENT_NAME not in error.value.message


def test_duplicate_event_name_is_rejected_even_when_native_ids_differ() -> None:
    inventory = _inventory(
        (
            _key_event("701", "different_event"),
            _key_event("702", _EVENT_NAME),
        )
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), inventory, fingerprint_key=_KEY
        )

    assert error.value.code == "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY"
    assert _EVENT_NAME not in error.value.message


@pytest.mark.parametrize(
    "replacement",
    [
        hashlib.sha256(_EVENT_NAME.encode("ascii")).hexdigest(),
        _fingerprint(_FOREIGN_KEY, "key-event-name", _EVENT_NAME),
    ],
)
def test_unsealed_or_foreign_event_fingerprints_cannot_produce_an_absence_claim(
    replacement: str,
) -> None:
    original = _inventory((_key_event("701", _EVENT_NAME),))
    changed_member = original.key_events[0].model_copy(
        update={"event_name_fingerprint": replacement}
    )
    tampered = original.model_copy(update={"key_events": (changed_member,)})

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(_create_input(), tampered, fingerprint_key=_KEY)

    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH"
    assert _EVENT_NAME not in error.value.message


@pytest.mark.parametrize(
    "key_events",
    [
        (),
        (_key_event("701", "different_event"),),
    ],
)
def test_removed_or_replaced_members_cannot_reuse_an_old_content_marker(
    key_events: tuple[Ga4AdminKeyEvent, ...],
) -> None:
    original = _inventory((_key_event("701", _EVENT_NAME),))
    tampered = original.model_copy(update={"key_events": key_events})

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(_create_input(), tampered, fingerprint_key=_KEY)

    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH"


def test_copied_foreign_key_marker_fails_before_content_or_event_comparison() -> None:
    original = _inventory((_key_event("701", _EVENT_NAME),))
    copied_marker = original.model_copy(
        update={"fingerprint_key_marker": _fingerprint_key_marker(_FOREIGN_KEY)}
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), copied_marker, fingerprint_key=_KEY
        )

    assert error.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"
    assert _EVENT_NAME not in error.value.message


def test_current_key_marker_cannot_be_copied_onto_foreign_sealed_content() -> None:
    foreign = _inventory(
        (_key_event("701", _EVENT_NAME, fingerprint_key=_FOREIGN_KEY),),
        fingerprint_key=_FOREIGN_KEY,
    )
    forged_current_marker = foreign.model_copy(
        update={"fingerprint_key_marker": _fingerprint_key_marker(_KEY)}
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), forged_current_marker, fingerprint_key=_KEY
        )

    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH"
    assert _EVENT_NAME not in error.value.message


def test_key_marker_only_history_cannot_make_an_absence_claim_without_content_seal() -> None:
    identity = _identity()
    key_marker_only = Ga4AdminInventory(
        identity=identity,
        fingerprint_key_marker=_fingerprint_key_marker(_KEY),
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), key_marker_only, fingerprint_key=_KEY
        )

    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_NOT_EVIDENCED"


def test_missing_content_seal_fails_before_candidate_event_hmac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_only = Ga4AdminInventory(
        identity=_identity(),
        key_events=(_key_event("701", _EVENT_NAME),),
        fingerprint_key_marker=_fingerprint_key_marker(_KEY),
    )

    def event_hmac_must_not_run(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("candidate event HMAC ran before content integrity")

    monkeypatch.setattr(preconditions, "_fingerprint", event_hmac_must_not_run)

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), marker_only, fingerprint_key=_KEY
        )

    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_NOT_EVIDENCED"
    assert _EVENT_NAME not in error.value.message


def test_null_content_marker_and_legacy_bytes_fail_closed() -> None:
    identity = _identity()
    legacy = Ga4AdminInventory(identity=identity)
    expected_legacy = (
        '{"schema_version":1,"identity":{"schema_version":1,'
        '"property_ref":"ga4_admin.property.123","property_client_ref":"123",'
        '"web_stream_ref":"ga4_admin.web_stream.456","web_stream_client_ref":"456",'
        '"admin_api_version":"v1alpha",'
        '"required_scope":"https://www.googleapis.com/auth/analytics.readonly"},'
        '"key_events":[],"event_create_rules":[],"event_edit_rules":[],"pagination_verified":true}'
    )
    assert legacy.model_dump_json() == expected_legacy

    with pytest.raises(AdhelmError) as legacy_error:
        evaluate_ga4_key_event_create_preconditions(_create_input(), legacy, fingerprint_key=_KEY)
    assert legacy_error.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_NOT_EVIDENCED"

    forged_null = Ga4AdminInventory.model_construct(
        schema_version=1,
        identity=identity,
        key_events=(),
        event_create_rules=(),
        event_edit_rules=(),
        pagination_verified=True,
        fingerprint_key_marker=_fingerprint_key_marker(_KEY),
        inventory_content_marker=None,
    )
    with pytest.raises(AdhelmError) as null_error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), forged_null, fingerprint_key=_KEY
        )
    _assert_invalid_precondition(null_error)


def test_constructed_member_with_a_foreign_parent_fails_before_duplicate_claim() -> None:
    original = _key_event("701", "different_event")
    forged_member = Ga4AdminKeyEvent.model_construct(
        **{**original.model_dump(mode="python"), "property_ref": "ga4_admin.property.999"}
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), _inventory((forged_member,)), fingerprint_key=_KEY
        )

    _assert_invalid_precondition(error)


def test_constructed_numeric_default_value_does_not_launder_through_json_serialization() -> None:
    source = _create_input()
    forged_default = Ga4AdminKeyEventDefaultValueInput.model_construct(
        numeric_value=1.0,
        currency_code="USD",
    )
    forged_input = Ga4AdminKeyEventCreateInput.model_construct(
        event_contract=source.event_contract,
        identity=source.identity,
        counting_method=source.counting_method,
        default_value=forged_default,
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            forged_input, _inventory(), fingerprint_key=_KEY
        )

    _assert_invalid_precondition(error)


def test_constructed_result_cannot_launder_true_authority_or_side_effect_flags() -> None:
    normal = Ga4AdminKeyEventInventoryPreconditions(candidate_digest="0" * 64)
    forged = normal.model_copy(update={"remote_side_effects": True, "execution_authority": True})

    with pytest.raises(ValidationError):
        Ga4AdminKeyEventInventoryPreconditions.model_validate_json(
            forged.model_dump_json(warnings="error")
        )


def test_result_rejects_schema_float_before_literal_normalization() -> None:
    forged = Ga4AdminKeyEventInventoryPreconditions.model_construct(
        schema_version=1.0,
        candidate_digest="0" * 64,
        inventory_scope="COMPLETE_SUPPLIED_INVENTORY",
        fingerprint_key_continuity="MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY",
        duplicate_status="NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY",
        current_provider_state="NOT_ESTABLISHED",
        capacity_status="NOT_EVALUATED",
        permission_status="NOT_EVALUATED",
        remote_side_effects=False,
        execution_authority=False,
    )

    with pytest.raises(ValidationError):
        Ga4AdminKeyEventInventoryPreconditions.model_validate_json(
            forged.model_dump_json(warnings="error")
        )

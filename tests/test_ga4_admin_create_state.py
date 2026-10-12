"""Focused fake-only tests for GA4 key-event CREATE semantic state."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Literal

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers import _semantic_state as semantic_state
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
)
from adhelm.providers.ga4_admin_create_state import (
    _SEMANTIC_SPEC,
    Ga4AdminKeyEventCreateSemanticState,
    build_ga4_admin_key_event_create_semantic_state,
    project_ga4_admin_edit_key_event_semantic_collection,
    revalidate_ga4_admin_edit_key_event_semantic_projection,
    revalidate_ga4_admin_key_event_create_semantic_state,
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


def _fresh(
    *,
    event_name: str = "planned_event",
    counting_method: Literal["ONCE_PER_EVENT", "ONCE_PER_SESSION"] = "ONCE_PER_EVENT",
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
        counting_method=counting_method,
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
    identifier: str,
    event_name: str,
    *,
    counting_method: str = "ONCE_PER_EVENT",
    default_value: object | None = None,
    custom: bool = True,
    deletable: bool = True,
    create_time: str | None = "2026-09-05T12:00:00Z",
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": f"properties/{_PROPERTY_ID}/keyEvents/{identifier}",
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
    key: bytes,
    key_events: list[dict[str, object]],
    *,
    raw_list_payload: bytes | None = None,
) -> Ga4AdminEditKeyEventInventory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1beta/properties/{_PROPERTY_ID}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{_PROPERTY_ID}",
                    "timeZone": "America/Chicago",
                    "currencyCode": "USD",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                },
            )
        if request.url.path == f"/v1beta/properties/{_PROPERTY_ID}/dataStreams/{_STREAM_ID}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{_PROPERTY_ID}/dataStreams/{_STREAM_ID}",
                    "type": "WEB_DATA_STREAM",
                },
            )
        assert request.url.path == f"/v1alpha/properties/{_PROPERTY_ID}/keyEvents"
        if raw_list_payload is not None:
            return httpx.Response(200, content=raw_list_payload)
        return httpx.Response(200, json={"keyEvents": key_events})

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}, read_attempts=1),
            lambda: "fixture-edit-token",
            fingerprint_key=key,
        ).read_inventory(_target())
    finally:
        client.close()


def _assert_code(error: pytest.ExceptionInfo[AdhelmError], code: str) -> None:
    assert error.value.code == code
    assert "planned_event" not in error.value.message


def test_builds_redacted_deterministic_transition_with_separate_full_inventory_digest() -> None:
    inventory = _inventory(
        _KEY,
        [
            _event(
                "702",
                "existing_two",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
            ),
            _event("701", "existing_one"),
        ],
    )

    state = build_ga4_admin_key_event_create_semantic_state(
        _fresh(default_value=Decimal("1.25")),
        inventory,
        fingerprint_key=_KEY,
    )

    assert state.before.revision == "GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1"
    assert state.after.revision == state.before.revision
    assert state.before.fingerprint != state.after.fingerprint
    assert state.before_member_count == 2
    assert state.after_member_count == 3
    assert state.inventory_digest == state.preconditions.inventory_digest
    assert state.candidate_digest == state.preconditions.candidate_digest
    assert state.preconditions.execution_authority is False
    assert state.preconditions.current_provider_state == "NOT_ESTABLISHED"

    rendered = json.dumps(state.model_dump(mode="json"), sort_keys=True)
    for private in (
        "planned_event",
        "existing_one",
        "existing_two",
        _PROPERTY_ID,
        _STREAM_ID,
        inventory.fingerprint_key_marker,
        inventory.inventory_content_marker,
        _KEY.decode("ascii"),
    ):
        assert private not in rendered


def test_projection_is_sorted_and_preserves_duplicate_semantic_members() -> None:
    first = _inventory(
        _KEY,
        [_event("703", "second"), _event("701", "first")],
    )
    reordered = _inventory(
        _KEY,
        [_event("701", "first"), _event("703", "second")],
    )
    duplicate = _inventory(
        _KEY,
        [_event("701", "same"), _event("702", "same")],
    )
    one = _inventory(_KEY, [_event("701", "same")])

    first_projection = project_ga4_admin_edit_key_event_semantic_collection(
        first,
        fingerprint_key=_KEY,
    )
    reordered_projection = project_ga4_admin_edit_key_event_semantic_collection(
        reordered,
        fingerprint_key=_KEY,
    )
    duplicate_projection = project_ga4_admin_edit_key_event_semantic_collection(
        duplicate,
        fingerprint_key=_KEY,
    )
    one_projection = project_ga4_admin_edit_key_event_semantic_collection(one, fingerprint_key=_KEY)

    assert first_projection.state == reordered_projection.state
    assert first_projection.inventory_digest == reordered_projection.inventory_digest
    assert duplicate_projection.member_count == 2
    assert one_projection.member_count == 1
    assert duplicate_projection.state != one_projection.state


def test_unknown_observed_counting_method_remains_distinct_from_proposed_once_per_event() -> None:
    unknown = _inventory(
        _KEY,
        [_event("701", "existing", counting_method="COUNTING_METHOD_UNSPECIFIED")],
    )
    once_per_event = _inventory(_KEY, [_event("701", "existing")])

    unknown_state = build_ga4_admin_key_event_create_semantic_state(
        _fresh(),
        unknown,
        fingerprint_key=_KEY,
    )
    known_state = build_ga4_admin_key_event_create_semantic_state(
        _fresh(),
        once_per_event,
        fingerprint_key=_KEY,
    )

    assert unknown_state.before != known_state.before
    assert unknown_state.after != known_state.after


def test_post_create_projection_matches_predicted_after_without_provider_assigned_id() -> None:
    baseline = _inventory(_KEY, [_event("701", "existing")])
    fresh = _fresh(default_value=Decimal("1.25"))
    planned = build_ga4_admin_key_event_create_semantic_state(fresh, baseline, fingerprint_key=_KEY)
    baseline_projection = project_ga4_admin_edit_key_event_semantic_collection(
        baseline,
        fingerprint_key=_KEY,
    )

    after_one = _inventory(
        _KEY,
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
        ],
    )
    after_two = _inventory(
        _KEY,
        [
            _event("701", "existing", custom=False, deletable=False),
            _event(
                "901",
                "planned_event",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
                custom=True,
                deletable=True,
                create_time="2026-09-06T13:00:00Z",
            ),
        ],
    )

    projection_one = project_ga4_admin_edit_key_event_semantic_collection(
        after_one,
        fingerprint_key=_KEY,
    )
    projection_two = project_ga4_admin_edit_key_event_semantic_collection(
        after_two,
        fingerprint_key=_KEY,
    )

    assert projection_one.state == planned.after
    assert projection_two.state == planned.after
    assert projection_one.inventory_digest != baseline_projection.inventory_digest
    assert projection_two.inventory_digest != baseline_projection.inventory_digest
    assert projection_one.inventory_digest != projection_two.inventory_digest


def test_post_create_projection_rejects_duplicate_and_semantic_drift() -> None:
    baseline = _inventory(_KEY, [_event("701", "existing")])
    fresh = _fresh(default_value=Decimal("1.25"))
    planned = build_ga4_admin_key_event_create_semantic_state(fresh, baseline, fingerprint_key=_KEY)

    duplicate = _inventory(
        _KEY,
        [
            _event("701", "existing"),
            _event(
                "900",
                "planned_event",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
            ),
            _event(
                "901",
                "planned_event",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
            ),
        ],
    )
    counting_drift = _inventory(
        _KEY,
        [
            _event("701", "existing"),
            _event(
                "900",
                "planned_event",
                counting_method="ONCE_PER_SESSION",
                default_value={"numericValue": 1.25, "currencyCode": "USD"},
            ),
        ],
    )
    default_drift = _inventory(
        _KEY,
        [
            _event("701", "existing"),
            _event(
                "900",
                "planned_event",
                default_value={"numericValue": 1.5, "currencyCode": "USD"},
            ),
        ],
    )

    duplicate_projection = project_ga4_admin_edit_key_event_semantic_collection(
        duplicate,
        fingerprint_key=_KEY,
    )
    assert duplicate_projection.member_count == planned.after_member_count + 1
    assert duplicate_projection.state != planned.after
    assert (
        project_ga4_admin_edit_key_event_semantic_collection(
            counting_drift,
            fingerprint_key=_KEY,
        ).state
        != planned.after
    )
    assert (
        project_ga4_admin_edit_key_event_semantic_collection(
            default_drift,
            fingerprint_key=_KEY,
        ).state
        != planned.after
    )


def test_post_create_projection_requires_exact_normalized_default_number_string() -> None:
    baseline = _inventory(_KEY, [_event("701", "existing")])
    planned = build_ga4_admin_key_event_create_semantic_state(
        _fresh(default_value=Decimal("1.25")),
        baseline,
        fingerprint_key=_KEY,
    )
    # The fixed reader deliberately preserves the provider response's Decimal
    # text in its normalized model. We do not erase a provider's distinct
    # numeric representation during an after-state proof.
    lexical_drift = _inventory(
        _KEY,
        [],
        raw_list_payload=(
            b'{"keyEvents":['
            b'{"name":"properties/987654321/keyEvents/701",'
            b'"eventName":"existing","countingMethod":"ONCE_PER_EVENT",'
            b'"custom":true,"deletable":true},'
            b'{"name":"properties/987654321/keyEvents/900",'
            b'"eventName":"planned_event","countingMethod":"ONCE_PER_EVENT",'
            b'"custom":true,"deletable":true,'
            b'"defaultValue":{"numericValue":1.250,"currencyCode":"USD"}}]}'
        ),
    )

    projection = project_ga4_admin_edit_key_event_semantic_collection(
        lexical_drift,
        fingerprint_key=_KEY,
    )
    assert projection.state != planned.after


def test_create_state_invokes_duplicate_and_capacity_preconditions_but_projection_does_not() -> (
    None
):
    duplicate = _inventory(_KEY, [_event("701", "planned_event")])

    assert (
        project_ga4_admin_edit_key_event_semantic_collection(
            duplicate,
            fingerprint_key=_KEY,
        ).member_count
        == 1
    )
    with pytest.raises(AdhelmError) as error:
        build_ga4_admin_key_event_create_semantic_state(
            _fresh(),
            duplicate,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY")

    at_cap = _inventory(
        _KEY,
        [_event(str(700 + index), f"existing_{index}") for index in range(30)],
    )
    assert (
        project_ga4_admin_edit_key_event_semantic_collection(
            at_cap,
            fingerprint_key=_KEY,
        ).member_count
        == 30
    )
    with pytest.raises(AdhelmError) as error:
        build_ga4_admin_key_event_create_semantic_state(
            _fresh(),
            at_cap,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "GA4_KEY_EVENT_DOCUMENTED_CAP_REACHED")


def test_revalidators_rebuild_keyed_expected_values_and_reject_forged_controls() -> None:
    fresh = _fresh()
    inventory = _inventory(_KEY, [_event("701", "existing")])
    projection = project_ga4_admin_edit_key_event_semantic_collection(
        inventory,
        fingerprint_key=_KEY,
    )
    state = build_ga4_admin_key_event_create_semantic_state(fresh, inventory, fingerprint_key=_KEY)

    assert (
        revalidate_ga4_admin_edit_key_event_semantic_projection(
            projection.model_dump(mode="json"),
            inventory=inventory,
            fingerprint_key=_KEY,
        )
        == projection
    )
    assert (
        revalidate_ga4_admin_key_event_create_semantic_state(
            state.model_dump(mode="json"),
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
        == state
    )

    forged_projection: dict[str, Any] = projection.model_dump(mode="json")
    forged_projection["member_count"] = 1.0
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_edit_key_event_semantic_projection(
            forged_projection,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "INVALID_GA4_ADMIN_EDIT_SEMANTIC_PROJECTION")

    forged_state: dict[str, Any] = state.model_dump(mode="json")
    forged_state["schema_version"] = True
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_key_event_create_semantic_state(
            forged_state,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE")

    forged_digest_state = state.model_copy(
        update={"after": state.after.model_copy(update={"fingerprint": "a" * 64})}
    )
    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_key_event_create_semantic_state(
            forged_digest_state,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE")

    structurally_forged = state.model_copy(update={"after": state.before})
    with pytest.raises(ValueError, match="semantic create state"):
        structurally_forged.model_dump_json()


def test_state_requires_current_marker_but_does_not_claim_key_origin_or_authority() -> None:
    inventory = _inventory(_KEY, [_event("701", "existing")])

    with pytest.raises(AdhelmError) as error:
        project_ga4_admin_edit_key_event_semantic_collection(
            inventory,
            fingerprint_key=_OTHER_KEY,
        )
    _assert_code(error, "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH")

    projection = project_ga4_admin_edit_key_event_semantic_collection(
        inventory,
        fingerprint_key=_KEY,
    )
    assert projection.current_provider_state == "NOT_ESTABLISHED"
    assert projection.reservation_status == "NOT_ESTABLISHED"
    assert projection.permission_status == "NOT_EVALUATED"
    assert projection.remote_side_effects is False
    assert projection.execution_authority is False


def test_constructed_fresh_input_and_state_do_not_bypass_strict_rebuild() -> None:
    inventory = _inventory(_KEY, [_event("701", "existing")])
    valid = _fresh()
    forged_fresh = valid.model_copy(update={"counting_method": True})

    with pytest.raises(AdhelmError) as error:
        build_ga4_admin_key_event_create_semantic_state(
            forged_fresh,
            inventory,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")

    valid_state = build_ga4_admin_key_event_create_semantic_state(
        valid,
        inventory,
        fingerprint_key=_KEY,
    )
    forged_shape = valid_state.model_copy(update={"preconditions": object()})
    with pytest.raises(ValueError, match="semantic create state"):
        forged_shape.model_dump_json()


def test_create_state_binds_before_member_count_to_complete_inventory_fact() -> None:
    inventory = _inventory(_KEY, [_event("701", "existing")])
    state = build_ga4_admin_key_event_create_semantic_state(
        _fresh(), inventory, fingerprint_key=_KEY
    )

    with pytest.raises(ValueError, match="semantic create state is invalid"):
        Ga4AdminKeyEventCreateSemanticState(
            target_binding_fingerprint=state.target_binding_fingerprint,
            candidate_digest=state.candidate_digest,
            inventory_digest=state.inventory_digest,
            before=state.before,
            after=state.after,
            before_member_count=0,
            after_member_count=1,
            preconditions=state.preconditions,
        )

    copied = state.model_copy(update={"before_member_count": 0, "after_member_count": 1})
    with pytest.raises(ValueError, match="semantic create state"):
        copied.model_dump_json()


def test_state_revalidation_bounds_deep_untrusted_mappings_before_reconstruction() -> None:
    fresh = _fresh()
    inventory = _inventory(_KEY, [_event("701", "existing")])
    state = build_ga4_admin_key_event_create_semantic_state(fresh, inventory, fingerprint_key=_KEY)
    forged: dict[str, Any] = state.model_dump(mode="json")
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(20):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    forged["preconditions"] = deep

    with pytest.raises(AdhelmError) as error:
        revalidate_ga4_admin_key_event_create_semantic_state(
            forged,
            fresh=fresh,
            inventory=inventory,
            fingerprint_key=_KEY,
        )
    _assert_code(error, "INVALID_GA4_ADMIN_CREATE_SEMANTIC_STATE")


_GOLDEN_INVENTORY_EVENTS = [
    _event("702", "existing_two", default_value={"numericValue": 1.25, "currencyCode": "USD"}),
    _event("701", "existing_one", counting_method="ONCE_PER_SESSION"),
]
# Captured from this module before the generic semantic-hierarchy projection was
# extracted into ``adhelm.providers._semantic_state``.  Any change here means a
# retained GA4 plan, approval, or ledger record would no longer match its
# recomputed state, so these values are frozen deliberately.
_GOLDEN_TARGET_BINDING = "19a81a16f9c1a13c4875061c03aaa952594bfeabe3ad185b230a60a6a49cc203"
_GOLDEN_COLLECTION_STATE = "b60cce09787ae4f2b0e803e2e4e9b2e382aeeff6a75ae254e8cf5077fab7c7b2"
_GOLDEN_INVENTORY_DIGEST = "f40160ca6d315f7e00618d73046e191b6d5f4e7e3fd759b3af554d30eb3dc212"
_GOLDEN_CREATE_AFTER = "c8a649241b20d58ad23479678194e8bd0c790da38944a1b118fa393a240cbab2"
_GOLDEN_CANDIDATE_DIGEST = "c7e1d7621dbe30101dc5898d0f3c039c0054f8ae1b00cc6f07ac97f5f3b3c101"


def test_generic_semantic_projection_extraction_is_byte_identical() -> None:
    inventory = _inventory(_KEY, _GOLDEN_INVENTORY_EVENTS)

    projection = project_ga4_admin_edit_key_event_semantic_collection(
        inventory, fingerprint_key=_KEY
    )
    state = build_ga4_admin_key_event_create_semantic_state(
        _fresh(default_value=Decimal("1.25")), inventory, fingerprint_key=_KEY
    )

    assert projection.target_binding_fingerprint == _GOLDEN_TARGET_BINDING
    assert projection.state.fingerprint == _GOLDEN_COLLECTION_STATE
    assert projection.inventory_digest == _GOLDEN_INVENTORY_DIGEST
    assert state.before.fingerprint == _GOLDEN_COLLECTION_STATE
    assert state.after.fingerprint == _GOLDEN_CREATE_AFTER
    assert state.candidate_digest == _GOLDEN_CANDIDATE_DIGEST
    assert state.target_binding_fingerprint == _GOLDEN_TARGET_BINDING


def test_ga4_projection_uses_the_shared_keyed_construction() -> None:
    """The GA4 module must delegate, not keep a private copy of the algorithm."""

    inventory = _inventory(_KEY, _GOLDEN_INVENTORY_EVENTS)
    projection = project_ga4_admin_edit_key_event_semantic_collection(
        inventory, fingerprint_key=_KEY
    )

    assert (
        semantic_state.target_binding_fingerprint(
            _SEMANTIC_SPEC,
            _target().model_dump(mode="json"),
            fingerprint_key=_KEY,
        )
        == projection.target_binding_fingerprint
    )
    assert (
        semantic_state.keyed_fingerprint(
            _KEY,
            context=_SEMANTIC_SPEC.context,
            purpose=_SEMANTIC_SPEC.target_purpose,
            value={
                "schema_version": 1,
                "state_kind": _SEMANTIC_SPEC.state_kind,
                "target": _target().model_dump(mode="json"),
            },
        )
        == projection.target_binding_fingerprint
    )

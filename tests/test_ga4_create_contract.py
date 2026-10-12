"""Focused pure-contract checks for a GA4 key-event CREATE action."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.conversion_contracts import (
    ConversionContract,
    ConversionContractInput,
    compile_conversion_contract,
)
from adhelm.errors import AdhelmError
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    compile_ga4_key_event_candidate,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
)
from adhelm.providers.ga4_create_contract import (
    GA4_KEY_EVENT_CREATE_PLAN_STATUS,
    GA4_KEY_EVENT_CREATE_SCOPE,
    Ga4KeyEventCreateExecutionBinding,
    Ga4KeyEventCreateObservationInput,
    Ga4KeyEventCreatePlannedAction,
    Ga4KeyEventCreateSelectionInput,
    build_ga4_key_event_create_plan,
    revalidate_ga4_key_event_create_planned_action,
    validate_ga4_key_event_create_plan_binding,
)
from adhelm.safety import AccountIdentity
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32
_CONTRACT_RECORD_DIGEST = "a" * 64
_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
_PROPERTY_ID = "123"
_STREAM_ID = "456"


def _contract_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "contract_id": "conversion.fixture",
        "version": 1,
        "supersedes_ref": None,
        "business_outcome": {
            "outcome_ref": "outcome.fixture",
            "statement": {"statement": "A qualified request confirmed by the customer"},
        },
        "authoritative_source": {
            "source_kind": "CRM_AGGREGATE",
            "source_ref": "source.fixture",
            "statement": {"statement": "A customer-configured fixed aggregate report"},
        },
        "qualifying_occurrence": {
            "occurrence_ref": "occurrence.fixture",
            "statement": {"statement": "The source confirms each qualified occurrence"},
        },
        "exclusions": [],
        "counting_and_deduplication": {
            "counting_semantics": "EACH_QUALIFYING_OCCURRENCE",
            "deduplication_semantics": "NOT_APPLICABLE",
            "duplicate_occurrence_handling": "COUNT_EACH",
            "declared_window": "NOT_APPLICABLE",
        },
        "value_and_currency": {
            "value_semantics": "NO_MONETARY_VALUE",
            "fixed_value": None,
            "currency": None,
            "null_value_semantics": "NO_MONETARY_VALUE",
        },
        "allowed_parameter_schema": [
            {
                "name": "qualified",
                "value_type": "BOOLEAN",
                "required": True,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": [],
            }
        ],
        "prohibited_parameters": [
            "PERSONAL_IDENTIFIER",
            "AUTHENTICATION_SECRET",
            "CLICK_IDENTIFIER",
            "URL_OR_QUERY",
            "DOM_OR_PAGE_TEXT",
            "FORM_VALUE",
            "CRM_RECORD_IDENTIFIER",
        ],
        "consent_contract_ref": "consent.fixture",
        "destination_profiles": ["ga4.logical.destination"],
        "effective_from": "2026-09-01T00:00:00Z",
    }


def _contract() -> ConversionContract:
    return compile_conversion_contract(
        ConversionContractInput.model_validate_json(canonical_bytes(_contract_data()))
    )


def _fresh() -> Ga4KeyEventCandidateInput:
    data = {
        "schema_version": 1,
        "conversion_contract_record_digest": _CONTRACT_RECORD_DIGEST,
        "declared_destination_profile_ref": "ga4.logical.destination",
        "key_event": {
            "event_contract": {
                "event_contract_ref": "event.fixture",
                "event_name": "qualified_request",
                "semantics": {
                    "semantics_ref": "semantics.fixture",
                    "customer_definition": "A customer-defined qualified request event.",
                    "counting_rule": "Count each declared event occurrence.",
                },
                "fields": [{"name": "qualified", "value_type": "BOOLEAN", "required": True}],
            },
            "identity": {
                "property_ref": f"ga4_admin.property.{_PROPERTY_ID}",
                "property_client_ref": _PROPERTY_ID,
                "web_stream_ref": f"ga4_admin.web_stream.{_STREAM_ID}",
                "web_stream_client_ref": _STREAM_ID,
            },
            "counting_method": "ONCE_PER_EVENT",
            "default_value": None,
        },
    }
    return Ga4KeyEventCandidateInput.model_validate_json(canonical_bytes(data))


def _target() -> Ga4AdminEditInventoryTarget:
    return Ga4AdminEditInventoryTarget(
        property_ref=f"ga4_admin.property.{_PROPERTY_ID}",
        property_client_ref=_PROPERTY_ID,
        web_stream_ref=f"ga4_admin.web_stream.{_STREAM_ID}",
        web_stream_client_ref=_STREAM_ID,
        expected_timezone="UTC",
        expected_currency="USD",
    )


def _inventory(key: bytes, *, event_name: str | None = None) -> Ga4AdminEditKeyEventInventory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1beta/properties/{_PROPERTY_ID}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{_PROPERTY_ID}",
                    "timeZone": "UTC",
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
        events: list[dict[str, object]] = []
        if event_name is not None:
            events.append(
                {
                    "name": f"properties/{_PROPERTY_ID}/keyEvents/701",
                    "eventName": event_name,
                    "countingMethod": "ONCE_PER_EVENT",
                    "custom": True,
                    "deletable": True,
                }
            )
        return httpx.Response(200, json={"keyEvents": events})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: "fixture-edit-token",
            fingerprint_key=key,
        ).read_inventory(_target())


def _facts() -> tuple[
    Ga4KeyEventCreateSelectionInput,
    Ga4KeyEventCreateObservationInput,
    Ga4KeyEventCreateExecutionBinding,
    Ga4KeyEventCandidateInput,
]:
    contract = _contract()
    fresh = _fresh()
    candidate = compile_ga4_key_event_candidate(
        fresh,
        retained_contract=contract,
        retained_contract_record_digest=_CONTRACT_RECORD_DIGEST,
    )
    inventory = _inventory(_KEY)
    selection = Ga4KeyEventCreateSelectionInput(
        candidate=candidate,
        contract=contract,
        candidate_record_digest="b" * 64,
        contract_record_digest=_CONTRACT_RECORD_DIGEST,
        checkpoint_digest="c" * 64,
        evaluated_at=_NOW,
    )
    observation = Ga4KeyEventCreateObservationInput(
        observation_record_digest="d" * 64,
        checkpoint_digest="c" * 64,
        profile_ref="ga4.edit.profile",
        configuration_digest="e" * 64,
        profile_configuration_digest="f" * 64,
        read_started_at=_NOW - timedelta(seconds=3),
        read_finished_at=_NOW - timedelta(seconds=1),
        inventory=inventory,
        inventory_digest=digest(inventory),
    )
    execution = Ga4KeyEventCreateExecutionBinding(
        auth_profile_ref="ga4.edit.profile",
        profile_configuration_digest="f" * 64,
        configuration_digest="e" * 64,
        principal_ref="principal.fixture",
        client_application_ref="client.fixture",
        target=inventory.target,
    )
    return selection, observation, execution, fresh


def _build() -> tuple[
    Ga4KeyEventCreatePlannedAction,
    Ga4KeyEventCreateSelectionInput,
    Ga4KeyEventCreateObservationInput,
    Ga4KeyEventCreateExecutionBinding,
    Ga4KeyEventCandidateInput,
]:
    selection, observation, execution, fresh = _facts()
    action = build_ga4_key_event_create_plan(
        selection,
        observation,
        execution,
        fresh=fresh,
        fingerprint_key=_KEY,
        workspace_profile_ref="workspace.default",
        expires_at=_NOW + timedelta(minutes=5),
    )
    return action, selection, observation, execution, fresh


def test_builds_exact_zero_spend_plan_with_distinct_destination_and_edit_profile() -> None:
    action, selection, observation, execution, fresh = _build()
    subplan = action.plan.subplans[0]

    assert action.status == GA4_KEY_EVENT_CREATE_PLAN_STATUS
    assert action.workspace_binding.workspace_profile_ref == "workspace.default"
    assert action.candidate.compilation.conversion_binding.declared_destination_profile_ref == (
        "ga4.logical.destination"
    )
    assert isinstance(subplan.account, AccountIdentity)
    assert subplan.account.auth_profile_ref == "ga4.edit.profile"
    assert subplan.account.scopes == (GA4_KEY_EVENT_CREATE_SCOPE,)
    assert subplan.max_spend.minor_units == 0
    assert subplan.recovery_actions == ()
    assert subplan.recovery_disposition == "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"
    assert action.declared_destination_relationship == "DECLARED_DESTINATION_SELECTION_ONLY"
    assert action.destination_to_execution_profile_mapping == "NOT_EVIDENCED"
    assert action.execution_authority is False
    assert action.semantic_state.candidate_digest == (
        action.candidate.compilation.compiler_candidate_digest
    )
    assert (
        action.selection_binding.checkpoint_digest == action.observation_binding.checkpoint_digest
    )
    assert revalidate_ga4_key_event_create_planned_action(action.model_dump(mode="json")) == action
    assert (
        validate_ga4_key_event_create_plan_binding(
            action,
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
        )
        == action
    )


@pytest.mark.parametrize("seconds", [59, 901])
def test_builder_rejects_expiry_outside_bounded_window(seconds: int) -> None:
    selection, observation, execution, fresh = _facts()

    with pytest.raises(AdhelmError) as raised:
        build_ga4_key_event_create_plan(
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
            workspace_profile_ref="workspace.default",
            expires_at=_NOW + timedelta(seconds=seconds),
        )
    assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"


@pytest.mark.parametrize(
    "update",
    [
        {"checkpoint_digest": "9" * 64},
        {"profile_ref": "different.profile"},
        {"configuration_digest": "9" * 64},
    ],
)
def test_builder_requires_one_final_checkpoint_and_exact_technical_binding(
    update: dict[str, object],
) -> None:
    selection, observation, execution, fresh = _facts()
    forged_observation = observation.model_copy(update=update)

    with pytest.raises(AdhelmError) as raised:
        build_ga4_key_event_create_plan(
            selection,
            forged_observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
            workspace_profile_ref="workspace.default",
            expires_at=_NOW + timedelta(minutes=5),
        )
    assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"


def test_revalidator_rejects_raw_boolean_float_and_native_digest_tampering() -> None:
    action, *_ = _build()
    raw = action.model_dump(mode="json")
    for path, value in (
        (("schema_version",), True),
        (("selection_binding", "schema_version"), 1.0),
        (("plan", "stop_on_non_verified"), 1),
        (("plan", "subplans", 0, "max_spend", "minor_units"), True),
        (("native_request_binding", "body_digest"), "0" * 64),
    ):
        forged = deepcopy(raw)
        cursor: Any = forged
        for part in path[:-1]:
            cursor = cursor[part]
        cursor[path[-1]] = value
        with pytest.raises(AdhelmError) as raised:
            revalidate_ga4_key_event_create_planned_action(forged)
        assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: (
            raw["observation_binding"].update({"configuration_digest": "9" * 64})
            or raw["execution_binding"].update({"configuration_digest": "9" * 64})
        ),
        lambda raw: (
            raw["observation_binding"].update({"inventory_digest": "8" * 64})
            or raw["semantic_state"].update({"inventory_digest": "8" * 64})
            or raw["semantic_state"]["preconditions"].update({"inventory_digest": "8" * 64})
        ),
        lambda raw: raw["selection_binding"].update({"evaluated_at": "2026-09-05T12:00:01Z"}),
        lambda raw: raw["observation_binding"].update(
            {
                "read_started_at": "2026-09-05T11:59:58Z",
                "read_finished_at": "2026-09-05T11:59:59Z",
            }
        ),
    ],
)
def test_revalidator_binds_outer_source_provenance_into_authorizable_plan(
    mutate: object,
) -> None:
    """A valid-shape outer provenance rewrite must alter the reconstructed plan."""
    action, *_ = _build()
    raw = action.model_dump(mode="json", warnings="error")
    assert callable(mutate)
    mutate(raw)

    with pytest.raises(AdhelmError) as raised:
        revalidate_ga4_key_event_create_planned_action(raw)
    assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"


def test_copied_or_constructed_action_cannot_serialize_as_a_fixed_plan() -> None:
    action, *_ = _build()
    forged = action.model_copy(update={"execution_authority": True})
    with pytest.raises(ValueError, match="GA4 key-event CREATE action is invalid"):
        forged.model_dump(mode="json")

    incomplete = Ga4KeyEventCreatePlannedAction.model_construct(schema_version=1)
    with pytest.raises(ValueError, match="GA4 key-event CREATE action is invalid"):
        incomplete.model_dump(mode="json")


def test_full_binding_requires_current_keyed_inventory_and_nonduplicate_preconditions() -> None:
    action, selection, observation, execution, fresh = _build()
    with pytest.raises(AdhelmError) as wrong_key:
        validate_ga4_key_event_create_plan_binding(
            action,
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=_OTHER_KEY,
        )
    assert wrong_key.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"

    duplicate_inventory = _inventory(_KEY, event_name="qualified_request")
    duplicate_observation = observation.model_copy(
        update={"inventory": duplicate_inventory, "inventory_digest": digest(duplicate_inventory)}
    )
    with pytest.raises(AdhelmError) as duplicate:
        validate_ga4_key_event_create_plan_binding(
            action,
            selection,
            duplicate_observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
        )
    assert duplicate.value.code == "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY"


def test_raw_revalidator_bounds_deep_untrusted_objects_without_recursion_leak() -> None:
    deep: dict[str, object] = {"value": "x"}
    for _ in range(40):
        deep = {"nested": deep}

    with pytest.raises(AdhelmError) as raised:
        revalidate_ga4_key_event_create_planned_action(deep)
    assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"


def test_raw_revalidator_bounds_utf8_mapping_keys_before_structural_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized untrusted object key cannot reach nested action parsing."""
    from adhelm.providers import ga4_create_contract as contract

    reached_parser: list[bool] = []
    monkeypatch.setattr(contract, "_validate_action_raw", lambda _raw: reached_parser.append(True))

    with pytest.raises(AdhelmError) as raised:
        revalidate_ga4_key_event_create_planned_action({"x" * 1_048_577: None})
    assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"
    assert reached_parser == []

"""Independent adversarial review of the pure GA4 key-event CREATE contract.

The shared fixture builds its inventory through ``httpx.MockTransport`` only.
These cases exercise the public plan reconstruction and keyed full-binding
boundaries without a ledger, credential store, network, or provider request.
"""

from __future__ import annotations

import builtins
import json
import socket
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from test_ga4_create_contract import _KEY, _NOW, _build, _inventory

import adhelm.providers.ga4_create_contract as create_contract
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput
from adhelm.safety import StateRef


def _assert_invalid(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"
    assert error.value.exit_code in {ExitCode.INVALID_INPUT, ExitCode.PRECONDITION}
    rendered = str(error.value)
    for private in ("ultra_private_event", "private source declaration", "private-sentinel"):
        assert private not in rendered


def _build_with(
    selection: object,
    observation: object,
    execution: object,
    fresh: Ga4KeyEventCandidateInput,
):
    return create_contract.build_ga4_key_event_create_plan(
        selection,  # type: ignore[arg-type]
        observation,  # type: ignore[arg-type]
        execution,  # type: ignore[arg-type]
        fresh=fresh,
        fingerprint_key=_KEY,
        workspace_profile_ref="workspace.default",
        expires_at=_NOW + timedelta(minutes=5),
    )


def test_exact_plan_is_zero_spend_fixed_egress_and_does_not_predict_a_resource_id() -> None:
    action, _selection, _observation, _execution, _fresh = _build()
    subplan = action.plan.subplans[0]
    plan_data = json.dumps(action.plan.model_dump(mode="json"), sort_keys=True)

    assert action.execution_authority is False
    assert action.current_state_status == "RECHECK_REQUIRED_BEFORE_EGRESS"
    assert action.provider_create_permission == "NOT_PREPROVABLE"
    assert action.configuration_readback == "REQUIRED_AFTER_CREATE"
    assert action.native_conversion_mapping == "NONE_CREATED"
    assert action.native_request_binding.host == "analyticsadmin.googleapis.com"
    assert subplan.operation.value == "CREATE"
    assert subplan.max_spend.minor_units == 0
    assert subplan.recovery_actions == ()
    assert subplan.recovery_disposition == "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"
    assert subplan.before != subplan.after
    assert "keyEvents/" not in plan_data
    assert "post_create" not in plan_data


def test_build_and_both_revalidation_boundaries_are_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _action, selection, observation, execution, fresh = _build()

    def prohibited(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the pure CREATE contract must not perform I/O")

    monkeypatch.setattr(builtins, "open", prohibited)
    monkeypatch.setattr(socket, "socket", prohibited)
    action = _build_with(selection, observation, execution, fresh)

    assert create_contract.revalidate_ga4_key_event_create_planned_action(action) == action
    assert (
        create_contract.validate_ga4_key_event_create_plan_binding(
            action,
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
        )
        == action
    )


@pytest.mark.parametrize(
    "variant",
    [
        "configuration",
        "profile_configuration",
        "checkpoint",
        "selection_time",
        "read_interval",
        "inventory",
    ],
)
def test_complete_source_provenance_changes_the_authorizable_plan_and_blocks_stale_binding(
    variant: str,
) -> None:
    action, selection, observation, execution, fresh = _build()
    changed_selection = selection
    changed_observation = observation
    changed_execution = execution

    if variant == "configuration":
        changed_observation = observation.model_copy(update={"configuration_digest": "9" * 64})
        changed_execution = execution.model_copy(update={"configuration_digest": "9" * 64})
    elif variant == "profile_configuration":
        changed_observation = observation.model_copy(
            update={"profile_configuration_digest": "7" * 64}
        )
        changed_execution = execution.model_copy(update={"profile_configuration_digest": "7" * 64})
    elif variant == "checkpoint":
        changed_selection = selection.model_copy(update={"checkpoint_digest": "6" * 64})
        changed_observation = observation.model_copy(update={"checkpoint_digest": "6" * 64})
    elif variant == "selection_time":
        changed_selection = selection.model_copy(
            update={"evaluated_at": selection.evaluated_at + timedelta(seconds=1)}
        )
    elif variant == "read_interval":
        changed_observation = observation.model_copy(
            update={
                "read_started_at": observation.read_started_at - timedelta(seconds=1),
                "read_finished_at": observation.read_finished_at - timedelta(seconds=1),
            }
        )
    else:
        inventory = _inventory(_KEY, event_name="unrelated_existing_event")
        changed_observation = observation.model_copy(
            update={"inventory": inventory, "inventory_digest": digest(inventory)}
        )

    changed = _build_with(changed_selection, changed_observation, changed_execution, fresh)

    assert changed.plan.plan_digest != action.plan.plan_digest
    assert create_contract.revalidate_ga4_key_event_create_planned_action(changed) == changed
    with pytest.raises(AdhelmError) as stale_original:
        create_contract.validate_ga4_key_event_create_plan_binding(
            action,
            changed_selection,
            changed_observation,
            changed_execution,
            fresh=fresh,
            fingerprint_key=_KEY,
        )
    _assert_invalid(stale_original)
    with pytest.raises(AdhelmError) as stale_changed:
        create_contract.validate_ga4_key_event_create_plan_binding(
            changed,
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
        )
    _assert_invalid(stale_changed)


def test_keyed_full_binding_rejects_a_self_consistent_forged_semantic_transition() -> None:
    """Bare retention parsing is structural; the keyed boundary is authoritative."""

    action, selection, observation, execution, fresh = _build()
    revision = "GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1"
    forged_state = action.semantic_state.model_copy(
        update={
            "before": StateRef(fingerprint="d" * 64, revision=revision),
            "after": StateRef(fingerprint="e" * 64, revision=revision),
        }
    )
    forged_plan = create_contract._build_plan(
        candidate=action.candidate,
        workspace=action.workspace_binding,
        selection=action.selection_binding,
        observation=action.observation_binding,
        execution=action.execution_binding,
        state=forged_state,
        native_request=action.native_request_binding,
        expires_at=action.plan.expires_at,
    )
    forged = action.model_copy(update={"semantic_state": forged_state, "plan": forged_plan})

    checked = create_contract.revalidate_ga4_key_event_create_planned_action(forged)
    assert checked == forged
    assert checked.plan.plan_digest != action.plan.plan_digest
    with pytest.raises(AdhelmError) as raised:
        create_contract.validate_ga4_key_event_create_plan_binding(
            checked,
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=_KEY,
        )
    _assert_invalid(raised)


def test_fresh_input_prose_or_destination_drift_cannot_reconstruct_the_retained_candidate() -> None:
    _action, selection, observation, execution, fresh = _build()
    raw = fresh.model_dump(mode="json")
    raw["key_event"]["event_contract"]["semantics"]["customer_definition"] = (
        "private source declaration"
    )
    changed = Ga4KeyEventCandidateInput.model_validate_json(canonical_bytes(raw))

    with pytest.raises(AdhelmError) as raised:
        _build_with(selection, observation, execution, changed)
    _assert_invalid(raised)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.__setitem__("schema_version", 1.0),
        lambda raw: raw["selection_binding"].__setitem__("schema_version", True),
        lambda raw: raw["execution_binding"].__setitem__(
            "scopes", ["https://www.googleapis.com/auth/analytics.readonly"]
        ),
        lambda raw: raw["semantic_state"].__setitem__("before_member_count", False),
        lambda raw: raw["plan"]["subplans"][0].__setitem__(
            "recovery_actions", [{"action": "private-sentinel"}]
        ),
        lambda raw: raw.__setitem__("private-sentinel", "private-sentinel"),
    ],
)
def test_raw_original_types_closed_shapes_and_controls_reject_aliases_without_disclosure(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    action, _selection, _observation, _execution, _fresh = _build()
    raw: dict[str, Any] = deepcopy(action.model_dump(mode="json"))
    mutate(raw)

    with pytest.raises(AdhelmError) as raised:
        create_contract.revalidate_ga4_key_event_create_planned_action(raw)
    _assert_invalid(raised)


def test_raw_compiler_mutation_is_rejected_without_echoing_event_or_native_identifiers() -> None:
    action, _selection, _observation, _execution, _fresh = _build()
    raw: dict[str, Any] = deepcopy(action.model_dump(mode="json"))
    raw["candidate"]["compilation"]["compiler_candidate"]["request"]["eventName"] = (
        "ultra_private_event"
    )

    with pytest.raises(AdhelmError) as raised:
        create_contract.revalidate_ga4_key_event_create_planned_action(raw)
    _assert_invalid(raised)


def test_raw_key_bytes_are_bounded_before_the_structural_action_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_structural_parser = False

    def unexpected_parser(_data: dict[str, object]) -> None:
        nonlocal reached_structural_parser
        reached_structural_parser = True
        raise AssertionError("oversized input reached structural action parsing")

    monkeypatch.setattr(create_contract, "_validate_action_raw", unexpected_parser)
    with pytest.raises(AdhelmError) as raised:
        create_contract.revalidate_ga4_key_event_create_planned_action({"x" * 1_048_577: None})
    _assert_invalid(raised)
    assert reached_structural_parser is False


@pytest.mark.parametrize(
    "update",
    [
        {"execution_authority": True},
        {"current_state_status": "CURRENT_AND_EXECUTABLE"},
        {"provider_create_permission": "PROVEN"},
        {"native_conversion_mapping": "CREATED"},
    ],
)
def test_constructed_or_copied_action_cannot_promote_authority_or_permission(
    update: dict[str, object],
) -> None:
    action, _selection, _observation, _execution, _fresh = _build()
    forged = action.model_copy(update=update)

    with pytest.raises(ValueError, match="GA4 key-event CREATE action is invalid") as raised:
        forged.model_dump(mode="json")
    assert "CURRENT_AND_EXECUTABLE" not in str(raised.value)


def test_incomplete_constructed_action_has_one_sanitized_serialization_failure() -> None:
    forged = create_contract.Ga4KeyEventCreatePlannedAction.model_construct(
        schema_version=1,
        status="private-sentinel",
    )

    with pytest.raises(ValueError, match="GA4 key-event CREATE action is invalid") as raised:
        forged.model_dump_json()
    assert "private-sentinel" not in str(raised.value)

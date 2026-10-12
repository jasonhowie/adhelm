"""Independent adversarial review of M24 GA4 CREATE plan workflow/history."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_ga4_admin_execution_observation import _KEY
from test_ga4_create_workflow import _setup

from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_create_selection import select_ga4_key_event_create_candidate
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreateExecutionBinding,
    Ga4KeyEventCreateObservationInput,
    Ga4KeyEventCreateSelectionInput,
    build_ga4_key_event_create_plan,
    revalidate_ga4_key_event_create_planned_action,
)
from adhelm.saved_ga4_admin_edit_inventories import retain_ga4_admin_edit_inventory
from adhelm.saved_ga4_create_plans import retain_ga4_key_event_create_plan
from adhelm.strict_json import validate_json_value


def test_direct_rehashed_action_cannot_replace_sealed_oauth_principal(
    tmp_path, monkeypatch
) -> None:
    """A persisted action must bind its execution identity to its source observation."""
    from adhelm import ga4_create_workflow as workflow

    request, _, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    captured = {}

    def capture(_ledger, action, **_kwargs):
        captured["action"] = action
        return {"record_digest": "a" * 64}

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", capture)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    raw = captured["action"].model_dump(mode="json", warnings="error")
    raw["execution_binding"]["principal_ref"] = "forged-principal"
    raw["plan"]["subplans"][0]["account"]["principal_ref"] = "forged-principal"
    # This must fail before a direct retention call: altering a source binding
    # must also alter the reconstructed, authorizable CompositePlan.
    with pytest.raises(AdhelmError) as raised:
        revalidate_ga4_key_event_create_planned_action(raw)
    assert raised.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"


def test_retain_rejects_new_self_consistent_action_with_unobserved_oauth_principal(
    tmp_path, monkeypatch
) -> None:
    """History binds a rebuilt plan to the retained, sealed OAuth observation."""
    from adhelm import ga4_create_workflow as workflow

    request, ledger, _, profile, _, _, _, _ = _setup(tmp_path, monkeypatch)
    captured = {}

    def capture(_ledger, action, **_kwargs):
        captured["action"] = action
        return {"record_digest": "a" * 64}

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", capture)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    original = captured["action"]
    stored = next(
        item["payload"]
        for item in ledger.checkpoint()["records_by_stream"]["changes"]
        if item["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    retained_observation = validate_json_value(Ga4AdminEditInventoryObservation, stored)
    selected = select_ga4_key_event_create_candidate(
        ledger,
        original.selection_binding.candidate_record_digest,
        fresh=request.fresh,
        as_of=original.selection_binding.evaluated_at,
    )
    selection = Ga4KeyEventCreateSelectionInput(
        candidate=selected.candidate,
        contract=selected.contract,
        candidate_record_digest=selected.candidate_record_digest,
        contract_record_digest=selected.contract_record_digest,
        checkpoint_digest=selected.checkpoint_digest,
        evaluated_at=selected.evaluated_at,
    )
    observation = Ga4KeyEventCreateObservationInput(
        observation_record_digest=original.observation_binding.observation_record_digest,
        checkpoint_digest=selected.checkpoint_digest,
        profile_ref=profile.profile_id,
        configuration_digest=retained_observation.configuration_digest,
        profile_configuration_digest=retained_observation.profile_configuration_digest,
        read_started_at=retained_observation.read_started_at,
        read_finished_at=retained_observation.read_finished_at,
        inventory=retained_observation.inventory,
        inventory_digest=retained_observation.inventory_digest,
    )
    execution = Ga4KeyEventCreateExecutionBinding(
        auth_profile_ref=original.execution_binding.auth_profile_ref,
        profile_configuration_digest=original.execution_binding.profile_configuration_digest,
        configuration_digest=original.execution_binding.configuration_digest,
        principal_ref="forged-principal",
        client_application_ref=original.execution_binding.client_application_ref,
        target=original.execution_binding.target,
    )
    forged = build_ga4_key_event_create_plan(
        selection,
        observation,
        execution,
        fresh=request.fresh,
        fingerprint_key=_KEY,
        workspace_profile_ref=original.workspace_binding.workspace_profile_ref,
        expires_at=original.plan.expires_at,
    )

    with pytest.raises(AdhelmError) as raised:
        retain_ga4_key_event_create_plan(
            ledger,
            forged,
            fresh=request.fresh,
            inventory=retained_observation.inventory,
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CONFLICT"


def test_execution_observation_preserves_legacy_omission_but_rejects_constructed_null(
    tmp_path, monkeypatch
) -> None:
    """A missing legacy proof differs from an explicit null proof on the wire."""
    from adhelm import ga4_create_workflow as workflow

    request, ledger, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    stored = next(
        item["payload"]
        for item in ledger.checkpoint()["records_by_stream"]["changes"]
        if item["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    legacy_raw = dict(stored)
    legacy_raw.pop("execution_observation")
    legacy = validate_json_value(Ga4AdminEditInventoryObservation, legacy_raw)
    assert "execution_observation" not in legacy.model_dump(mode="json", warnings="error")

    with pytest.raises(ValidationError):
        validate_json_value(
            Ga4AdminEditInventoryObservation,
            {**legacy_raw, "execution_observation": None},
        )

    forged = Ga4AdminEditInventoryObservation.model_construct(
        **legacy.model_dump(mode="python", warnings="error"),
        execution_observation=None,
    )
    with pytest.raises(ValueError):
        forged.model_dump(mode="json", warnings="error")


def test_newer_edit_observation_blocks_stale_action_retention(tmp_path, monkeypatch) -> None:
    """A plan cannot append after its selected EDIT observation is superseded."""
    from adhelm import ga4_create_workflow as workflow

    request, ledger, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    captured = {}

    def capture(_ledger, action, **_kwargs):
        captured["action"] = action
        return {"record_digest": "a" * 64}

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", capture)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    stored = next(
        item["payload"]
        for item in ledger.checkpoint()["records_by_stream"]["changes"]
        if item["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    observed = validate_json_value(Ga4AdminEditInventoryObservation, stored)
    retain_ga4_admin_edit_inventory(ledger, observed, fingerprint_key=_KEY)

    with pytest.raises(AdhelmError) as raised:
        retain_ga4_key_event_create_plan(
            ledger,
            captured["action"],
            fresh=request.fresh,
            inventory=observed.inventory,
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CHANGED"


def test_unrelated_append_between_checkpoint_precheck_and_append_is_rejected(
    tmp_path, monkeypatch
) -> None:
    """The action's final source checkpoint must remain current under append lock."""
    from adhelm import ga4_create_workflow as workflow

    request, ledger, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    captured = {}

    def capture(_ledger, action, **_kwargs):
        captured["action"] = action
        return {"record_digest": "a" * 64}

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", capture)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    action = captured["action"]
    stored = next(
        item["payload"]
        for item in ledger.checkpoint()["records_by_stream"]["changes"]
        if item["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    observation = validate_json_value(Ga4AdminEditInventoryObservation, stored)
    original_checkpoint = ledger.checkpoint
    calls = 0

    def checkpoint() -> dict[str, object]:
        nonlocal calls
        calls += 1
        snapshot = original_checkpoint()
        if calls == 2:
            # This is a valid but unrelated local compilation.  It lands after
            # the helper's checkpoint precheck and before Ledger.append takes
            # the changes lock.
            ledger.append(
                "changes",
                "GA4_KEY_EVENT_CANDIDATE_COMPILED",
                action.candidate.model_dump(mode="json", warnings="error"),
                provider="ga4_admin",
                account_ref=action.candidate.compilation.compiler_candidate.identity.property_ref,
                source="LOCAL_CLI",
            )
        return snapshot

    monkeypatch.setattr(ledger, "checkpoint", checkpoint)
    with pytest.raises(AdhelmError) as raised:
        retain_ga4_key_event_create_plan(
            ledger,
            action,
            fresh=request.fresh,
            inventory=observation.inventory,
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CHANGED"


def test_cross_stream_append_between_precheck_and_plan_append_is_rejected(
    tmp_path, monkeypatch
) -> None:
    """The source checkpoint is a full ledger vector, not a changes-only head."""
    from adhelm import ga4_create_workflow as workflow

    request, ledger, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    captured = {}

    def capture(_ledger, action, **_kwargs):
        captured["action"] = action
        return {"record_digest": "a" * 64}

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", capture)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    action = captured["action"]
    stored = next(
        item["payload"]
        for item in ledger.checkpoint()["records_by_stream"]["changes"]
        if item["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    observation = validate_json_value(Ga4AdminEditInventoryObservation, stored)
    original_checkpoint = ledger.checkpoint
    calls = 0

    def checkpoint() -> dict[str, object]:
        nonlocal calls
        calls += 1
        snapshot = original_checkpoint()
        if calls == 2:
            ledger.append(
                "creative",
                "CREATIVE_FAMILY",
                {
                    "schema_version": 1,
                    "creative_family_id": "independent-race-family",
                    "family_label": "independent-race-family",
                    "created_at": datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
                    "provenance": "CUSTOMER_LOCAL",
                },
            )
        return snapshot

    monkeypatch.setattr(ledger, "checkpoint", checkpoint)
    with pytest.raises(AdhelmError) as raised:
        retain_ga4_key_event_create_plan(
            ledger,
            action,
            fresh=request.fresh,
            inventory=observation.inventory,
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CHANGED"


def test_provider_list_runs_with_no_changes_stream_lock(tmp_path, monkeypatch) -> None:
    """The full provider read finishes before the workflow takes a stream lock."""
    depth = 0

    def no_lock_during_provider_read() -> None:
        assert depth == 0

    request, _, _, _, _, _, _, _ = _setup(
        tmp_path, monkeypatch, on_list=no_lock_during_provider_read
    )
    original = Ledger._stream_lock

    class TrackingLock:
        def __init__(self, lock: object) -> None:
            self._lock = lock

        def acquire(self) -> object:
            nonlocal depth
            acquired = self._lock.acquire()
            depth += 1
            return acquired

        def release(self) -> None:
            nonlocal depth
            self._lock.release()
            depth -= 1

        def __enter__(self) -> TrackingLock:
            self.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self.release()

    def tracking_stream_lock(self: Ledger, stream: str) -> TrackingLock:
        return TrackingLock(original(self, stream))

    monkeypatch.setattr(Ledger, "_stream_lock", tracking_stream_lock)
    from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan

    prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert depth == 0


def test_final_durable_callback_does_not_query_the_ledger(tmp_path, monkeypatch) -> None:
    """The under-lock callback may only check clock/configuration state."""
    from adhelm import ga4_create_workflow as workflow

    request, _, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)

    def capture(ledger, _action, **kwargs):
        callback = kwargs["before_durable_append"]
        monkeypatch.setattr(
            ledger,
            "checkpoint",
            lambda: pytest.fail("final durable callback queried the ledger"),
        )
        callback()
        return {"record_digest": "a" * 64}

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", capture)
    workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)


def test_configuration_drift_at_final_callback_prevents_plan_append(tmp_path, monkeypatch) -> None:
    """The under-lock handoff repeats the configuration digest check."""
    from adhelm import ga4_create_workflow as workflow
    from adhelm.config import _save_configuration

    request, ledger, _, _, configuration, _, _, _ = _setup(tmp_path, monkeypatch)
    original = workflow.retain_ga4_key_event_create_plan

    def drift_then_retain(ledger_arg, action, **kwargs):
        callback = kwargs["before_durable_append"]

        def drifted_callback() -> None:
            changed = configuration.model_copy(
                update={"output": configuration.output.model_copy(update={"max_rows": 7})}
            )
            _save_configuration(tmp_path, changed, replace=True)
            callback()

        return original(
            ledger_arg,
            action,
            **{**kwargs, "before_durable_append": drifted_callback},
        )

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", drift_then_retain)
    with pytest.raises(AdhelmError) as raised:
        workflow.prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert [item["record_kind"] for item in ledger.checkpoint()["records_by_stream"]["changes"]][
        -1
    ] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"


def test_export_omits_retained_execution_observation_proof(tmp_path, monkeypatch) -> None:
    """The sealed local OAuth snapshot stays local even in a full export."""
    from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan

    request, ledger, _, profile, _, _, _, _ = _setup(tmp_path, monkeypatch)
    prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    export_verified(ledger, tmp_path / "export", opaque_key=_KEY)
    exported = (tmp_path / "export" / "records.jsonl").read_text()

    for private in (
        '"execution_observation":',
        "observation_content_marker",
        profile.principal_ref,
        profile.client_ref,
    ):
        assert private not in exported
    assert "RETAINED_LOCAL_OAUTH_BINDING_NOT_AUTHENTICATED_BY_EXPORT" in exported

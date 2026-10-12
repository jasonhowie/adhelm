"""Pure envelope and predecessor checks for retained GA4 CREATE plans."""

from typing import Any

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_create_plan_records import revalidate_ga4_key_event_create_plan_record
from adhelm.ga4_key_event_candidates import (
    _revalidate_contract,
    validate_ga4_key_event_candidate_binding,
)
from adhelm.strict_json import validate_json_value

_KIND = "GA4_KEY_EVENT_CREATE_PLAN"


def validate_ga4_create_plan_envelope(
    kind: str,
    payload: dict[str, Any],
    workspace_profile_ref: str,
    provider: str | None,
    account_ref: str | None,
    source: str,
    run_id: str | None,
    observed_at: str | None,
    occurred_at: str | None,
) -> None:
    if kind != _KIND:
        return
    record = revalidate_ga4_key_event_create_plan_record(payload)
    action = record.action
    if (
        provider != "ga4_admin"
        or account_ref != action.execution_binding.target.property_ref
        or workspace_profile_ref != action.plan.profile_ref
        or source != "LOCAL_CLI"
        or run_id is not None
        or observed_at is not None
        or occurred_at is not None
    ):
        raise AdhelmError(
            "INVALID_GA4_CREATE_PLAN_PROVENANCE",
            "A GA4 CREATE plan must bind its local workspace and property without "
            "claiming a provider occurrence, observation or execution run.",
            exit_code=ExitCode.INVALID_INPUT,
        )


def validate_ga4_create_plan_history(records: list[dict[str, Any]]) -> None:
    """Verify source references at each plan's original changes-stream prefix.

    The ledger calls this under its changes lock before append and on scans.
    No nested ledger I/O, provider request, private key or credential is used.
    Newer definitions invalidate a new stale plan, while earlier historical
    plans remain readable at the prefix at which they were compiled.
    """

    preceding: dict[str, dict[str, Any]] = {}
    latest_contracts: dict[str, dict[str, Any]] = {}
    latest_inventories: dict[str, dict[str, Any]] = {}
    for envelope in records:
        kind = envelope["record_kind"]
        payload = envelope["payload"]
        if kind == "CONVERSION_CONTRACT":
            latest_contracts[payload["definition"]["contract_id"]] = envelope
        elif kind == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION":
            latest_inventories[payload["profile_ref"]] = envelope
        elif kind == _KIND:
            record = revalidate_ga4_key_event_create_plan_record(payload)
            action = record.action
            selection = action.selection_binding
            observed = action.observation_binding
            conversion = action.candidate.compilation.conversion_binding
            candidate = preceding.get(selection.candidate_record_digest)
            contract = latest_contracts.get(conversion.contract_id)
            inventory = preceding.get(observed.observation_record_digest)
            if (
                candidate is None
                or candidate["record_kind"] != "GA4_KEY_EVENT_CANDIDATE_COMPILED"
                or canonical_bytes(candidate["payload"]) != canonical_bytes(action.candidate)
                or contract is None
                or contract.get("record_digest") != selection.contract_record_digest
                or inventory is None
                or inventory["record_kind"] != "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
                or latest_inventories.get(observed.profile_ref) is not inventory
            ):
                raise _history_conflict()
            checked_contract = _revalidate_contract(contract["payload"])
            validate_ga4_key_event_candidate_binding(
                action.candidate, checked_contract, selection.contract_record_digest
            )
            stored = validate_json_value(Ga4AdminEditInventoryObservation, inventory["payload"])
            proof = stored.execution_observation
            execution = action.execution_binding
            if (
                checked_contract.definition.effective_from > selection.evaluated_at
                or stored.profile_ref != observed.profile_ref
                or stored.configuration_digest != observed.configuration_digest
                or stored.profile_configuration_digest != observed.profile_configuration_digest
                or stored.inventory_digest != observed.inventory_digest
                or stored.read_started_at != observed.read_started_at
                or stored.read_finished_at != observed.read_finished_at
                or stored.inventory.target != action.execution_binding.target
                or stored.inventory_digest != action.semantic_state.inventory_digest
                or proof is None
                or proof.oauth_binding.profile_ref != execution.auth_profile_ref
                or proof.oauth_binding.principal_ref != execution.principal_ref
                or proof.oauth_binding.client_ref != execution.client_application_ref
                or (proof.oauth_binding.api_scope,) != execution.scopes
                or proof.target != execution.target
            ):
                raise _history_conflict()
        if "record_digest" in envelope:
            preceding[envelope["record_digest"]] = envelope


def _history_conflict() -> AdhelmError:
    return AdhelmError(
        "GA4_CREATE_PLAN_HISTORY_CONFLICT",
        "The GA4 CREATE plan does not match its preceding candidate, latest conversion "
        "definition and latest selected EDIT observation.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Prepare a new plan from current configuration and verified local history.",
    )


def validate_ga4_create_start_history(records: list[dict[str, Any]]) -> None:
    """Bind each CREATE start to its retained plan and latest relevant sources.

    Called during physical scans and under the changes lock before append.
    No key, credential, clock, nested ledger read or provider request is used.
    This closes the local predecessor race; keyed and configured execution
    checks remain the responsibility of the configured runtime.
    """
    plans: dict[str, dict[str, Any]] = {}
    duplicate_plans: set[str] = set()
    preceding: list[dict[str, Any]] = []
    started: set[tuple[str, str]] = set()
    for envelope in records:
        kind = envelope["record_kind"]
        payload = envelope["payload"]
        if kind == _KIND:
            if payload["plan_digest"] in plans:
                duplicate_plans.add(payload["plan_digest"])
            plans[payload["plan_digest"]] = payload
        elif kind == "CHANGE_EVENT" and payload["event_type"] == "APPLY_STARTED":
            retained = plans.get(payload.get("plan_digest"))
            is_ga4_create = (
                envelope.get("provider") == "ga4_admin" and payload["operation"] == "CREATE"
            )
            if retained is not None or is_ga4_create:
                if retained is None or payload.get("plan_digest") in duplicate_plans:
                    raise _start_conflict()
                action = revalidate_ga4_key_event_create_plan_record(retained).action
                plan = action.plan
                subplan = plan.subplans[0]
                binding = (plan.plan_digest, subplan.subplan_digest)
                expected = {
                    "operation": "CREATE",
                    "origin": "LOCAL_CLI",
                    "resource_type": "PLAN",
                    "resource_ref": subplan.subplan_id,
                    "plan_ref": plan.plan_id,
                    "plan_digest": plan.plan_digest,
                    "subplan_digest": subplan.subplan_digest,
                    "before_digest": subplan.before.fingerprint,
                    "after_digest": subplan.after.fingerprint,
                    "effect_status": "NO_REMOTE_WRITE",
                    "spend_impact_minor": 0,
                    "currency": subplan.max_spend.currency,
                    "provider_api_version": subplan.provider_api_version,
                    "connector_version": subplan.capability_version,
                    "idempotency_ref": digest(
                        {"plan": plan.plan_digest, "subplan": subplan.subplan_digest}
                    ),
                }
                if (
                    not is_ga4_create
                    or binding in started
                    or envelope.get("account_ref") != subplan.account.account_ref
                    or envelope.get("run_id") != plan.plan_id
                    or envelope.get("source") != "LOCAL_CLI"
                    or any(payload.get(key) != value for key, value in expected.items())
                ):
                    raise _start_conflict()
                validate_ga4_create_plan_history(
                    [
                        *preceding,
                        {"record_kind": _KIND, "payload": retained},
                    ]
                )
                started.add(binding)
        preceding.append(envelope)


def _start_conflict() -> AdhelmError:
    return AdhelmError(
        "GA4_CREATE_START_SOURCE_CONFLICT",
        "A GA4 CREATE start requires its exact retained plan and current source bindings.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect history, reconcile unresolved work and prepare a new exact plan.",
    )

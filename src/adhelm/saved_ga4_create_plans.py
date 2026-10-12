"""Retain and select closed GA4 CREATE plans from verified local history."""

import re
from collections.abc import Callable
from typing import Any

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_execution_observation import revalidate_ga4_admin_edit_execution_observation
from adhelm.ga4_create_plan_records import (
    ga4_key_event_create_plan_record,
    revalidate_ga4_key_event_create_plan_record,
)
from adhelm.ga4_create_selection import select_ga4_key_event_create_candidate
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput, _revalidate_candidate_input
from adhelm.ledger import Ledger
from adhelm.providers.ga4_admin_create_state import (
    revalidate_ga4_admin_key_event_create_semantic_state,
)
from adhelm.providers.ga4_admin_edit_inventory import Ga4AdminEditKeyEventInventory
from adhelm.providers.ga4_create_contract import Ga4KeyEventCreatePlannedAction
from adhelm.strict_json import validate_json_value

_KIND = "GA4_KEY_EVENT_CREATE_PLAN"


def retain_ga4_key_event_create_plan(
    ledger: Ledger,
    action: Ga4KeyEventCreatePlannedAction,
    *,
    fresh: Ga4KeyEventCandidateInput,
    inventory: Ga4AdminEditKeyEventInventory,
    fingerprint_key: bytes,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Recheck fresh compilation/keyed state and append under predecessor checks.

    The optional final callback runs under the changes-stream lock. It must
    perform no ledger I/O; configured callers use it for clock/config checks.
    Retention establishes neither provider origin nor authorization.
    """
    record = ga4_key_event_create_plan_record(action)
    checked = record.action
    try:
        fresh = _revalidate_candidate_input(fresh)
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_SELECTION", "Fresh CREATE input is invalid."
        ) from None
    selected = select_ga4_key_event_create_candidate(
        ledger,
        checked.selection_binding.candidate_record_digest,
        fresh=fresh,
        as_of=checked.selection_binding.evaluated_at,
    )
    if selected.checkpoint_digest != checked.selection_binding.checkpoint_digest or canonical_bytes(
        selected.candidate
    ) != canonical_bytes(checked.candidate):
        raise AdhelmError(
            "GA4_CREATE_PLAN_HISTORY_CHANGED",
            "Local history changed after the CREATE plan was compiled.",
            exit_code=ExitCode.PRECONDITION,
        )
    revalidate_ga4_admin_key_event_create_semantic_state(
        checked.semantic_state,
        fresh=fresh.key_event,
        inventory=inventory,
        fingerprint_key=fingerprint_key,
    )
    checkpoint = ledger.checkpoint()
    if checkpoint["checkpoint_digest"] != checked.selection_binding.checkpoint_digest:
        raise AdhelmError("GA4_CREATE_PLAN_HISTORY_CHANGED", "Local plan source history changed.")
    observations = [
        item
        for item in checkpoint["records_by_stream"]["changes"]
        if item["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
        and item["record_digest"] == checked.observation_binding.observation_record_digest
    ]
    if len(observations) != 1:
        raise AdhelmError("GA4_CREATE_PLAN_HISTORY_CONFLICT", "The source observation is absent.")
    observed = validate_json_value(Ga4AdminEditInventoryObservation, observations[0]["payload"])
    if observed.execution_observation is None:
        raise AdhelmError(
            "GA4_CREATE_OAUTH_PROVENANCE_REQUIRED", "The source lacks a retained OAuth binding."
        )
    revalidate_ga4_admin_edit_execution_observation(
        observed.execution_observation, fingerprint_key=fingerprint_key
    )
    return ledger.append(
        "changes",
        _KIND,
        record.model_dump(mode="json", warnings="error"),
        provider="ga4_admin",
        account_ref=checked.execution_binding.target.property_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def load_ga4_key_event_create_plan(
    ledger: Ledger, record_digest: str
) -> tuple[Ga4KeyEventCreatePlannedAction, dict[str, Any]]:
    if type(record_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", record_digest) is None:
        raise AdhelmError("INVALID_GA4_CREATE_PLAN_SELECTION", "A retained digest is required.")
    checkpoint = ledger.checkpoint()
    records = [
        item
        for item in checkpoint["records_by_stream"]["changes"]
        if item["record_kind"] == _KIND and item["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError("GA4_CREATE_PLAN_NOT_RETAINED", "The GA4 CREATE plan is not retained.")
    stored = records[0]
    record = revalidate_ga4_key_event_create_plan_record(stored["payload"])
    return record.action, {
        "record_digest": stored["record_digest"],
        "plan_digest": record.plan_digest,
        "action_digest": record.action_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": stored["recorded_at"],
        "historical_evidence_status": record.historical_evidence_status,
        "execution_authority": False,
        "requires_current_kernel_checks": True,
    }


def ga4_key_event_create_plan_catalog(
    ledger: Ledger, *, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError("INVALID_GA4_CREATE_PLAN_SELECTION", "Plan pagination is invalid.")
    checkpoint = ledger.checkpoint()
    records = [
        item
        for item in reversed(checkpoint["records_by_stream"]["changes"])
        if item["record_kind"] == _KIND
    ]
    if cursor > len(records):
        raise AdhelmError("INVALID_GA4_CREATE_PLAN_SELECTION", "Cursor exceeds plan count.")
    rows = []
    for stored in records[cursor : cursor + limit]:
        record = revalidate_ga4_key_event_create_plan_record(stored["payload"])
        rows.append(
            {
                "record_digest": stored["record_digest"],
                "plan_digest": record.plan_digest,
                "recorded_at": stored["recorded_at"],
                "expires_at": record.expires_at.isoformat(),
                "capability_status": record.action.status,
                "maximum_incremental_spend": record.maximum_incremental_spend.model_dump(
                    mode="json"
                ),
                "execution_authority": False,
            }
        )
    return {
        "plans": rows,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
    }

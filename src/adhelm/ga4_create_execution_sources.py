"""Current local source checks for a retained GA4 CREATE action.

These checks never resolve a credential, call a provider, or issue authority.
The original full checkpoint is historical: unrelated later records are valid,
while newer relevant conversion definitions or EDIT observations reject.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_execution_observation import revalidate_ga4_admin_edit_execution_observation
from adhelm.ga4_create_plan_history import validate_ga4_create_plan_history
from adhelm.ga4_create_plan_records import ga4_key_event_create_plan_record
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    _revalidate_candidate_input,
    _revalidate_contract,
    compile_ga4_key_event_candidate,
)
from adhelm.providers.ga4_admin_create_state import (
    revalidate_ga4_admin_key_event_create_semantic_state,
)
from adhelm.providers.ga4_create_contract import Ga4KeyEventCreatePlannedAction
from adhelm.strict_json import validate_json_value

if TYPE_CHECKING:
    from adhelm.ledger import Ledger


def validate_current_ga4_create_sources(
    ledger: Ledger,
    action: Ga4KeyEventCreatePlannedAction,
    *,
    fresh: Ga4KeyEventCandidateInput,
    fingerprint_key: bytes,
    now: datetime,
) -> None:
    """Authenticate exact retained sources at one current verified checkpoint.

    The configured execution path owns the supplied clock and current key. A
    successful check does not reserve those sources, establish provider origin,
    or replace the independent latest-source check under the start append lock.
    """
    try:
        record = ga4_key_event_create_plan_record(action)
        checked = record.action
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock")
        current = now.astimezone(UTC)
        if not checked.selection_binding.evaluated_at <= current < checked.plan.expires_at:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained CREATE plan is outside its execution window.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        fresh = _revalidate_candidate_input(fresh)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_EXECUTION_SOURCE", "CREATE execution sources are invalid."
        ) from None

    checkpoint = ledger.checkpoint()
    records = checkpoint["records_by_stream"]["changes"]
    matches = [
        item
        for item in records
        if item["record_kind"] == "GA4_KEY_EVENT_CREATE_PLAN"
        and canonical_bytes(item["payload"]) == canonical_bytes(record)
    ]
    if checked.workspace_binding.workspace_profile_ref != ledger.profile_ref or len(matches) != 1:
        raise AdhelmError(
            "GA4_CREATE_PLAN_NOT_RETAINED", "Execution requires this exact retained CREATE plan."
        )
    # Treat this action as a new local source assertion at the current prefix,
    # without appending another plan or requiring its old global checkpoint.
    validate_ga4_create_plan_history(
        [
            *records,
            {"record_kind": "GA4_KEY_EVENT_CREATE_PLAN", "payload": record.model_dump(mode="json")},
        ]
    )
    by_digest = {item["record_digest"]: item for item in records}
    contract = _revalidate_contract(
        by_digest[checked.selection_binding.contract_record_digest]["payload"]
    )
    if contract.definition.effective_from > current:
        raise AdhelmError("GA4_CREATE_CONTRACT_NOT_EFFECTIVE", "The definition is not effective.")
    rebuilt = compile_ga4_key_event_candidate(
        fresh,
        retained_contract=contract,
        retained_contract_record_digest=checked.selection_binding.contract_record_digest,
    )
    if canonical_bytes(rebuilt) != canonical_bytes(checked.candidate):
        raise AdhelmError(
            "GA4_CREATE_FRESH_INPUT_MISMATCH", "Fresh source input differs from the retained plan."
        )
    observation = validate_json_value(
        Ga4AdminEditInventoryObservation,
        by_digest[checked.observation_binding.observation_record_digest]["payload"],
    )
    if observation.execution_observation is None:
        raise AdhelmError(
            "GA4_CREATE_OAUTH_PROVENANCE_REQUIRED", "A retained OAuth proof is required."
        )
    revalidate_ga4_admin_edit_execution_observation(
        observation.execution_observation, fingerprint_key=fingerprint_key
    )
    revalidate_ga4_admin_key_event_create_semantic_state(
        checked.semantic_state,
        fresh=fresh.key_event,
        inventory=observation.inventory,
        fingerprint_key=fingerprint_key,
    )

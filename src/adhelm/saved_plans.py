"""Verified local retention and retrieval for the fixed OpenAI pause action.

These helpers have no command, authorization, provider, or execution surface.
Retained plans remain historical local records; callers must use the current
safety kernel checks before any future operation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.plan_records import (
    OpenAIPauseExecutionPlanRecord,
    openai_pause_execution_plan_record,
    revalidate_openai_pause_action,
)
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction


def _json_timestamp(value: datetime) -> str:
    """Return an already-validated action time in canonical JSON form."""
    return value.isoformat().replace("+00:00", "Z")


def retain_openai_pause_plan(ledger: Ledger, action: OpenAIPausePlannedAction) -> dict[str, Any]:
    """Append one closed local plan record without authorization or egress."""
    record = openai_pause_execution_plan_record(action)
    try:
        payload = record.model_dump(mode="json", warnings="error")
    except Exception as exc:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN",
            "OpenAI pause retention requires a safely serializable closed action.",
            exit_code=2,
        ) from exc
    return ledger.append(
        "changes",
        "EXECUTION_PLAN",
        payload,
        provider="openai_ads",
        account_ref=record.action.binding.account_ref,
        source="LOCAL_CLI",
    )


def _selected_record(ledger: Ledger, record_digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(record_digest, str) or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"]["changes"]
        if record["record_kind"] == "EXECUTION_PLAN" and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "EXECUTION_PLAN_NOT_RETAINED",
            "Selected OpenAI pause plan is absent from verified local history.",
        )
    return records[0], checkpoint


def _typed_record(record: dict[str, Any]) -> OpenAIPauseExecutionPlanRecord:
    try:
        return OpenAIPauseExecutionPlanRecord.model_validate_json(
            canonical_bytes(record["payload"])
        )
    except (KeyError, ValueError) as exc:
        # A verified checkpoint normally rejects this first. Keep the library
        # error generic if a custom Ledger implementation returns malformed data.
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN", "Retained OpenAI pause plan is not a closed action."
        ) from exc


def load_openai_pause_plan(
    ledger: Ledger, record_digest: str
) -> tuple[OpenAIPausePlannedAction, dict[str, Any]]:
    """Load a full action only after a verified-ledger checkpoint scan."""
    record, checkpoint = _selected_record(ledger, record_digest)
    retained = _typed_record(record)
    action = revalidate_openai_pause_action(retained.action)
    return action, {
        "record_digest": record["record_digest"],
        "action_digest": retained.action_digest,
        "plan_digest": retained.plan_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        # This is local storage time, never an asserted provider event time.
        "recorded_at": record["recorded_at"],
        "expires_at": _json_timestamp(retained.expires_at),
        "capability_status": retained.capability_status,
        "historical_evidence_status": retained.historical_evidence_status,
        "execution_authority": retained.execution_authority,
        "requires_current_kernel_checks": retained.requires_current_kernel_checks,
    }


def openai_pause_plan_catalog(
    ledger: Ledger, *, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    """Return bounded review metadata without action, account, or profile fields."""
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_SELECTION", "Invalid execution-plan pagination.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"]["changes"])
        if record["record_kind"] == "EXECUTION_PLAN"
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_SELECTION",
            "Cursor exceeds retained execution-plan count.",
            exit_code=2,
        )
    entries = []
    for record in records[cursor : cursor + limit]:
        retained = _typed_record(record)
        entries.append(
            {
                "record_digest": record["record_digest"],
                "action_digest": retained.action_digest,
                "plan_digest": retained.plan_digest,
                "provider": retained.provider,
                # This is local storage time, never remote observation time.
                "recorded_at": record["recorded_at"],
                "expires_at": _json_timestamp(retained.expires_at),
                "capability_status": retained.capability_status,
                "maximum_incremental_spend": retained.maximum_incremental_spend.model_dump(
                    mode="json", warnings="error"
                ),
                "historical_evidence_status": retained.historical_evidence_status,
                "execution_authority": retained.execution_authority,
                "requires_current_kernel_checks": retained.requires_current_kernel_checks,
            }
        )
    return {
        "plans": entries,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }

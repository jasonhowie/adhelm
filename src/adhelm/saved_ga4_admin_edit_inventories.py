"""Verified append-only EDIT inventory history; local selection never reads a provider."""

import re
from typing import Any

from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_execution_observation import revalidate_ga4_admin_edit_execution_observation
from adhelm.ledger import Ledger
from adhelm.providers.ga4_admin_edit_inventory import revalidate_ga4_admin_edit_key_event_inventory
from adhelm.strict_json import validate_json_value

_KIND = "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"


def retain_ga4_admin_edit_inventory(
    ledger: Ledger, observation: Ga4AdminEditInventoryObservation, *, fingerprint_key: bytes
) -> dict[str, Any]:
    revalidate_ga4_admin_edit_key_event_inventory(
        observation.inventory, fingerprint_key=fingerprint_key
    )
    observation = validate_json_value(
        Ga4AdminEditInventoryObservation, observation.model_dump(mode="json", warnings="error")
    )
    if observation.execution_observation is not None:
        revalidate_ga4_admin_edit_execution_observation(
            observation.execution_observation, fingerprint_key=fingerprint_key
        )
    return ledger.append(
        "changes",
        _KIND,
        observation.model_dump(mode="json"),
        provider="ga4_admin",
        account_ref=observation.inventory.target.property_ref,
        source="UNKNOWN",
        observed_at=observation.read_finished_at.isoformat(),
    )


def validate_selection(cursor: int, limit: int) -> None:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION", "Invalid inventory pagination.", exit_code=2
        )


def ga4_admin_edit_inventory_catalog(
    ledger: Ledger, *, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    validate_selection(cursor, limit)
    checkpoint = ledger.checkpoint()
    records = [
        r for r in reversed(checkpoint["records_by_stream"]["changes"]) if r["record_kind"] == _KIND
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION", "Cursor exceeds inventory count.", exit_code=2
        )
    return {
        "inventories": [
            {
                "record_digest": r["record_digest"],
                "inventory_digest": r["payload"]["inventory_digest"],
                "read_finished_at": r["payload"]["read_finished_at"],
                "evidence_kind": "EDIT_PROFILE_CONFIGURATION_READ",
            }
            for r in records[cursor : cursor + limit]
        ],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
        "execution_authority": False,
    }


def load_ga4_admin_edit_inventory(
    ledger: Ledger, record_digest: str
) -> tuple[Ga4AdminEditInventoryObservation, dict[str, Any]]:
    if type(record_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    selected = [
        r
        for r in checkpoint["records_by_stream"]["changes"]
        if r["record_kind"] == _KIND and r["record_digest"] == record_digest
    ]
    if len(selected) != 1:
        raise AdhelmError(
            "MEASUREMENT_NOT_RETAINED", "EDIT inventory is absent from verified history."
        )
    record = selected[0]
    return validate_json_value(Ga4AdminEditInventoryObservation, record["payload"]), {
        "record_digest": record_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": record["recorded_at"],
    }

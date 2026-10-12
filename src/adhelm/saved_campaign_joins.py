"""Retained, versioned GA4 <-> provider join maps in verified local history.

A join map records a customer declaration.  It grants no provider authority and
performs no remote read or write.  Selection is bounded to one verified
checkpoint, exactly like the other retained-record selectors.
"""

from __future__ import annotations

import re
from typing import Any

from adhelm.campaign_joins import CampaignJoinMap
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger

JOIN_MAP_RECORD_KIND = "CAMPAIGN_JOIN_MAP_RETAINED"
JOIN_MAP_STREAM = "changes"


def join_map_retention_installed() -> bool:
    """Whether the append-only ledger accepts the join-map record kind yet.

    ``src/adhelm/ledger.py`` is owned by the integration coordinator, so this
    ticket cannot register the record kind itself.  The check is read-only and
    starts returning ``True`` as soon as the coordinator applies the additive
    hunk described in ``docs/implementation/ga4-campaign-joins.md``; nothing
    here silently falls back to an unretained result.
    """
    from adhelm.ledger import _KINDS

    return JOIN_MAP_RECORD_KIND in _KINDS.get(JOIN_MAP_STREAM, {})


def retention_unavailable() -> AdhelmError:
    return AdhelmError(
        "JOIN_MAP_RETENTION_NOT_INSTALLED",
        "The append-only ledger does not yet accept a retained campaign join map.",
        exit_code=3,
        next_action=(
            "Apply the additive CAMPAIGN_JOIN_MAP_RETAINED record kind described in "
            "docs/implementation/ga4-campaign-joins.md, then retain the map again."
        ),
    )


def retain_join_map(ledger: Ledger, join_map: CampaignJoinMap) -> dict[str, Any]:
    """Append one content-addressed join map bound to its GA4 property.

    `Ledger._validate_campaign_join_binding` refuses an envelope that names
    another provider or account, and `Ledger._validate_campaign_join_history`
    validates the version/supersedes chain while holding the stream lock, so a
    competing registration cannot fork it.
    """
    if not join_map_retention_installed():
        raise retention_unavailable()
    return ledger.append(
        JOIN_MAP_STREAM,
        JOIN_MAP_RECORD_KIND,
        join_map.model_dump(mode="json"),
        provider="ga4",
        account_ref=join_map.contract.ga4_property.property_ref,
    )


def load_join_map(ledger: Ledger, record_digest: str) -> tuple[CampaignJoinMap, dict[str, Any]]:
    if not isinstance(record_digest, str) or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_JOIN_MAP_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"][JOIN_MAP_STREAM]
        if record["record_kind"] == JOIN_MAP_RECORD_KIND
        and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "JOIN_MAP_NOT_RETAINED", "The selected join map is absent from verified history."
        )
    record = records[0]
    join_map = CampaignJoinMap.model_validate_json(canonical_bytes(record["payload"]))
    return join_map, {
        "record_digest": record["record_digest"],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": record["recorded_at"],
    }


def join_map_catalog(ledger: Ledger, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_JOIN_MAP_SELECTION", "Invalid join-map catalog pagination.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"][JOIN_MAP_STREAM])
        if record["record_kind"] == JOIN_MAP_RECORD_KIND
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_JOIN_MAP_SELECTION", "Cursor exceeds the retained version count.", exit_code=2
        )
    return {
        "join_maps": [
            {
                "record_digest": record["record_digest"],
                "join_map_digest": record["payload"]["join_map_digest"],
                "content_digest": record["payload"]["content_digest"],
                "contract_ref": record["payload"]["contract"]["contract_id"],
                "version": record["payload"]["contract"]["version"],
                "supersedes_digest": record["payload"]["contract"]["supersedes_ref"],
                "ga4_property_ref": record["payload"]["contract"]["ga4_property"]["property_ref"],
                "boundary_status": record["payload"]["boundary_status"],
                "resolved_row_count": len(record["payload"]["resolutions"]),
                "heuristic_proposal_count": len(record["payload"]["proposals"]),
                "provenance": "CUSTOMER_DECLARED",
                "execution_authority": False,
            }
            for record in records[cursor : cursor + limit]
        ],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "retention_installed": join_map_retention_installed(),
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }


def latest_join_map(ledger: Ledger, contract_ref: str) -> CampaignJoinMap | None:
    """The highest retained version of one join contract, for chain checking."""
    checkpoint = ledger.checkpoint()
    selected = [
        record
        for record in checkpoint["records_by_stream"][JOIN_MAP_STREAM]
        if record["record_kind"] == JOIN_MAP_RECORD_KIND
        and record["payload"]["contract"]["contract_id"] == contract_ref
    ]
    if not selected:
        return None
    newest = max(selected, key=lambda record: record["payload"]["contract"]["version"])
    return CampaignJoinMap.model_validate_json(canonical_bytes(newest["payload"]))


__all__ = [
    "JOIN_MAP_RECORD_KIND",
    "JOIN_MAP_STREAM",
    "join_map_catalog",
    "join_map_retention_installed",
    "latest_join_map",
    "load_join_map",
    "retain_join_map",
    "retention_unavailable",
]

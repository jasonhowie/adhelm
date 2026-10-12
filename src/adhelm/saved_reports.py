"""Bounded selection of retained normalized reports from one verified checkpoint."""

import re
from typing import Any

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import PROVIDERS
from adhelm.reporting import NormalizedReport


def report_catalog(
    ledger: Ledger, *, provider: str | None = None, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    if cursor < 0 or not 1 <= limit <= 500 or (provider is not None and provider not in PROVIDERS):
        raise AdhelmError(
            "INVALID_REPORT_SELECTION", "Invalid report provider or pagination.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    latest: dict[str, dict[str, Any]] = {}
    for record in checkpoint["records_by_stream"]["performance"]:
        if record["record_kind"] == "PERFORMANCE_OBSERVATION":
            latest[record["payload"]["observation_key"]] = record
    selected = [
        record
        for record in sorted(latest.values(), key=lambda row: row["stream_sequence"], reverse=True)
        if provider is None or record["provider"] == provider
    ]
    summaries = []
    for record in selected[cursor : cursor + limit]:
        payload = record["payload"]
        envelope = payload["report_envelope"]
        summaries.append(
            {
                "record_digest": record["record_digest"],
                "provider": record["provider"],
                "revision": payload["observation_revision"],
                "window_start": envelope["window_start"],
                "window_end": envelope["window_end"],
                "currency": envelope["currency"],
                "observed_at": envelope["observed_at"],
                "row_count": len(payload["performance_rows"]),
                "quality": envelope["data_quality_statuses"],
            }
        )
    return {
        "reports": summaries,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(selected),
        "next_cursor": str(cursor + limit) if cursor + limit < len(selected) else None,
    }


def load_report(ledger: Ledger, record_digest: str) -> tuple[NormalizedReport, dict[str, Any]]:
    if re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_REPORT_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"]["performance"]
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
        and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "REPORT_NOT_RETAINED", "Selected report is unavailable in verified local history."
        )
    payload = records[0]["payload"]
    report = NormalizedReport.model_validate_json(
        canonical_bytes(
            {
                "envelope": payload["report_envelope"],
                "performance_rows": payload["performance_rows"],
                "conversion_rows": payload["conversion_rows"],
            }
        )
    )
    return report, {
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "record_digest": record_digest,
        "revision": payload["observation_revision"],
    }

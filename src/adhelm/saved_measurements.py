"""Verified local measurement history, with no reread or mutation on selection."""

import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import MAX_PAYLOAD_BYTES, Ledger
from adhelm.measurement_chunks import (
    CHUNK_KIND,
    CHUNKED_KIND,
    OBSERVATION_KINDS,
    chunk_observation,
    commit_payload,
    resolve_observation,
)
from adhelm.measurement_records import (
    GtmFullInventoryMeasurementSnapshot,
    MeasurementObservation,
    MeasurementSnapshot,
)
from adhelm.strict_json import validate_json_value


def retain_measurement(
    ledger: Ledger,
    snapshot: MeasurementSnapshot,
    *,
    profile_ref: str,
    read_started_at: datetime,
    read_finished_at: datetime,
) -> dict[str, Any]:
    # Revalidate nested models, including objects made with model_copy/construct.
    observation = validate_json_value(
        MeasurementObservation,
        {
            "profile_ref": profile_ref,
            "read_started_at": read_started_at.isoformat(),
            "read_finished_at": read_finished_at.isoformat(),
            "snapshot": snapshot.model_dump(mode="json", warnings="error"),
            "snapshot_digest": digest(snapshot),
        },
    )
    payload = observation.model_dump(mode="json")

    def append(kind: str, record_payload: dict[str, Any]) -> dict[str, Any]:
        return ledger.append(
            "changes",
            kind,
            record_payload,
            provider=observation.snapshot.provider,
            account_ref=observation.snapshot.account_ref,
            source="UNKNOWN",
            observed_at=observation.read_finished_at.isoformat(),
        )

    if (
        isinstance(observation.snapshot, GtmFullInventoryMeasurementSnapshot)
        and len(canonical_bytes(payload)) > MAX_PAYLOAD_BYTES
    ):
        # The inventory grows with its container, so it is retained as bounded
        # chunk records and a final commit. Each chunk may fill a whole record
        # payload: the fewest chunks keep the per-read validation cost lowest.
        # A failure before the commit leaves only unreferenced chunks, so no
        # observation is retained.
        chunk_digests = [
            append(CHUNK_KIND, chunk)["record_digest"]
            for chunk in chunk_observation(observation, maximum_chunk_bytes=MAX_PAYLOAD_BYTES)
        ]
        return append(CHUNKED_KIND, commit_payload(observation, chunk_digests))
    return append("MEASUREMENT_OBSERVATION", payload)


def load_measurement(
    ledger: Ledger,
    record_digest: str,
) -> tuple[MeasurementObservation, dict[str, Any]]:
    if not isinstance(record_digest, str) or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    changes = checkpoint["records_by_stream"]["changes"]
    records = [
        record
        for record in changes
        if record["record_kind"] in OBSERVATION_KINDS and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "MEASUREMENT_NOT_RETAINED", "Selected measurement is absent from verified history."
        )
    record = records[0]
    if record["record_kind"] == CHUNKED_KIND:
        observation = resolve_observation(
            record,
            {item["record_digest"]: item for item in changes if item["record_kind"] == CHUNK_KIND},
        )
    else:
        observation = validate_json_value(MeasurementObservation, record["payload"])
    return observation, {
        "record_digest": record_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": record["recorded_at"],
    }


def measurement_catalog(ledger: Ledger, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION", "Invalid measurement pagination.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"]["changes"])
        if record["record_kind"] in OBSERVATION_KINDS
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION",
            "Cursor exceeds retained measurement count.",
            exit_code=2,
        )
    return {
        "measurements": [
            {
                "record_digest": record["record_digest"],
                "snapshot_digest": record["payload"]["snapshot_digest"],
                "provider": record["provider"],
                "read_finished_at": record["payload"]["read_finished_at"],
                "evidence_kind": "CONFIGURATION_READ",
                "execution_authority": False,
            }
            for record in records[cursor : cursor + limit]
        ],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }


def measurement_rows(observation: MeasurementObservation) -> list[dict[str, Any]]:
    from adhelm.measurement_records import (
        Ga4MeasurementSnapshot,
        GtmFullInventoryMeasurementSnapshot,
        GtmMeasurementSnapshot,
    )

    try:
        observation = validate_json_value(
            MeasurementObservation, observation.model_dump(mode="json", warnings="error")
        )
    except (AdhelmError, AttributeError, TypeError, ValueError, ValidationError) as exc:
        raise AdhelmError(
            "INVALID_MEASUREMENT_SELECTION",
            "Measurement review requires a validated retained observation.",
            exit_code=2,
        ) from exc
    snapshot = observation.snapshot
    if isinstance(snapshot, Ga4MeasurementSnapshot):
        return [
            {"resource_type": kind, **item.model_dump(mode="json")}
            for kind, items in (
                ("KEY_EVENT", snapshot.inventory.key_events),
                ("EVENT_CREATE_RULE", snapshot.inventory.event_create_rules),
                ("EVENT_EDIT_RULE", snapshot.inventory.event_edit_rules),
            )
            for item in items
        ]
    if isinstance(snapshot, GtmMeasurementSnapshot):
        versions = (snapshot.inventory.live, snapshot.inventory.latest)
        return (
            [
                {
                    "resource_type": "GTM_VERSION_SUMMARY",
                    **version.model_dump(mode="json", exclude={"opaque_code"}),
                    "opaque_code_count": len(version.opaque_code),
                }
                for version in versions
            ]
            + [
                {
                    "resource_type": "GTM_OPAQUE_CODE",
                    "context": version.context,
                    "version_ref": version.version_ref,
                    "marker_index": index,
                    **marker.model_dump(mode="json"),
                }
                for version in versions
                for index, marker in enumerate(version.opaque_code)
            ]
            + [
                {"resource_type": "GTM_WORKSPACE_METADATA", **workspace.model_dump(mode="json")}
                for workspace in snapshot.inventory.workspaces
            ]
        )
    if isinstance(snapshot, GtmFullInventoryMeasurementSnapshot):
        inventory = snapshot.inventory
        return (
            [
                {"resource_type": "GTM_COLLECTION_COVERAGE", **item.model_dump(mode="json")}
                for item in inventory.coverage
            ]
            + [
                {"resource_type": "GTM_RESOURCE", **item.model_dump(mode="json")}
                for item in inventory.resources
            ]
            + [
                {"resource_type": "GTM_REFERENCE_EDGE", **item.model_dump(mode="json")}
                for item in inventory.edges
            ]
        )
    return [
        {"resource_type": "CONVERSION_SETTING", **item.model_dump(mode="json")}
        for item in snapshot.settings
    ]

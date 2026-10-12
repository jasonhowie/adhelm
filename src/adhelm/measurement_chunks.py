"""Chunked retention for a measurement observation larger than one ledger record.

A ledger record is bounded (`MAX_RECORD_BYTES`), but a complete GTM Web
inventory grows with its container: it crosses one record at roughly 1,150
resources, far below the reader's own 10,000-resource bound.  Such an
observation is retained as an ordered run of `MEASUREMENT_OBSERVATION_CHUNK`
records, each carrying a bounded slice of the inventory's three record
collections, followed by one `CHUNKED_MEASUREMENT_OBSERVATION` record.  ("Chunk"
keeps this apart from a ledger file segment, which `SEGMENT_STARTED` opens.)

The final record is the commit.  It carries every other fact of the observation
(its read interval, snapshot digest, identity discovery and the inventory
header) and names each chunk by its ledger record digest, so the observation
exists exactly when that record does.  Chunks appended by a read that was
interrupted before its commit are never referenced: they are not listed, not
loadable and not part of any observation.

Reassembly rebuilds the exact `MeasurementObservation` and revalidates it, so a
chunked observation passes the same schema, identity and snapshot-digest checks
as a single-record one.  A missing, reordered, repeated, foreign or altered
chunk is a typed failure, never a shortened inventory.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.gtm_inventory import (
    GtmCollectionCoverage,
    GtmFullInventoryHeader,
    GtmInventoryResource,
    GtmReferenceEdge,
)
from adhelm.measurement_records import (
    Digest,
    GtmFullInventoryMeasurementSnapshot,
    MeasurementObservation,
)
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.gtm_identity import GtmIdentityDiscovery
from adhelm.strict_json import validate_json_value

CHUNK_KIND: Final = "MEASUREMENT_OBSERVATION_CHUNK"
CHUNKED_KIND: Final = "CHUNKED_MEASUREMENT_OBSERVATION"
#: Every ledger record kind whose payload is a retained measurement observation.
OBSERVATION_KINDS: Final = frozenset({"MEASUREMENT_OBSERVATION", CHUNKED_KIND})
#: The reader's bounds cap a complete inventory at tens of megabytes, so a few
#: hundred chunks is the practical ceiling.  The bound only stops a corrupt or
#: hostile commit from naming an unbounded run.
MAXIMUM_CHUNKS: Final = 1_024

_SECTIONS: Final = ("coverage", "resources", "edges")


class MeasurementObservationChunk(StrictModel):
    """One bounded slice of a complete GTM inventory's record collections."""

    schema_version: Literal[1] = 1
    event_type: Literal["MEASUREMENT_OBSERVATION_CHUNK"] = CHUNK_KIND
    provider: Literal["gtm_web_inventory"] = "gtm_web_inventory"
    snapshot_digest: Digest
    chunk_index: int = Field(ge=0, lt=MAXIMUM_CHUNKS)
    chunk_count: int = Field(ge=1, le=MAXIMUM_CHUNKS)
    coverage: tuple[GtmCollectionCoverage, ...] = ()
    resources: tuple[GtmInventoryResource, ...] = ()
    edges: tuple[GtmReferenceEdge, ...] = ()

    @model_validator(mode="after")
    def bound_chunk(self) -> "MeasurementObservationChunk":
        if self.chunk_index >= self.chunk_count:
            raise ValueError("A measurement chunk index must be inside its chunk count")
        if not (self.coverage or self.resources or self.edges):
            raise ValueError("A measurement chunk must carry at least one record")
        return self


class ChunkedMeasurementObservation(StrictModel):
    """The commit record of a complete GTM inventory retained across chunks."""

    schema_version: Literal[1] = 1
    event_type: Literal["CHUNKED_MEASUREMENT_OBSERVATION"] = CHUNKED_KIND
    provider: Literal["gtm_web_inventory"] = "gtm_web_inventory"
    profile_ref: SafeRef
    read_started_at: datetime
    read_finished_at: datetime
    snapshot_digest: Digest
    discovery: GtmIdentityDiscovery
    inventory: GtmFullInventoryHeader
    chunk_record_digests: tuple[Digest, ...] = Field(min_length=1, max_length=MAXIMUM_CHUNKS)

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Measurement read times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound_commit(self) -> "ChunkedMeasurementObservation":
        if self.read_finished_at < self.read_started_at:
            raise ValueError("Measurement read interval cannot be reversed")
        if len(set(self.chunk_record_digests)) != len(self.chunk_record_digests):
            raise ValueError("A measurement chunk can be named only once")
        return self

    @property
    def account_ref(self) -> str:
        return self.discovery.identity.account_ref


def _full_inventory(observation: MeasurementObservation) -> GtmFullInventoryMeasurementSnapshot:
    snapshot = observation.snapshot
    if not isinstance(snapshot, GtmFullInventoryMeasurementSnapshot):
        raise AdhelmError(
            "MEASUREMENT_CHUNKING_UNSUPPORTED",
            "Only a complete GTM inventory observation can be retained in chunks.",
            exit_code=2,
        )
    return snapshot


def chunk_observation(
    observation: MeasurementObservation, *, maximum_chunk_bytes: int
) -> list[dict[str, Any]]:
    """Split a complete GTM inventory into ordered chunk payloads.

    Records are packed greedily in their observed order (coverage, resources,
    then edges), so the same observation always yields the same chunks.  Each
    payload's canonical size stays within `maximum_chunk_bytes`.
    """

    inventory = _full_inventory(observation).inventory

    def payload(index: int, count: int, groups: Mapping[str, list[Any]]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_type": CHUNK_KIND,
            "provider": "gtm_web_inventory",
            "snapshot_digest": observation.snapshot_digest,
            "chunk_index": index,
            "chunk_count": count,
            **groups,
        }

    # The widest index and count bound what the fixed fields of any chunk can
    # cost, so a chunk is sized before the final run length is known.
    fixed_bytes = len(
        canonical_bytes(
            payload(MAXIMUM_CHUNKS - 1, MAXIMUM_CHUNKS, {name: [] for name in _SECTIONS})
        )
    )
    packed: list[dict[str, list[Any]]] = []
    used = 0
    for section in _SECTIONS:
        for record in getattr(inventory, section):
            encoded = record.model_dump(mode="json", warnings="error")
            # One byte for the separating comma keeps the estimate conservative.
            size = len(canonical_bytes(encoded)) + 1
            if fixed_bytes + size > maximum_chunk_bytes:
                raise AdhelmError(
                    "LEDGER_RECORD_TOO_LARGE",
                    "A single inventory record exceeds the bounded chunk size.",
                    exit_code=2,
                )
            if not packed or used + size > maximum_chunk_bytes:
                packed.append({name: [] for name in _SECTIONS})
                used = fixed_bytes
            packed[-1][section].append(encoded)
            used += size
    if len(packed) > MAXIMUM_CHUNKS:
        raise AdhelmError(
            "LEDGER_RECORD_TOO_LARGE",
            "The inventory needs more chunks than the ledger allows.",
            exit_code=2,
        )
    return [payload(index, len(packed), groups) for index, groups in enumerate(packed)]


def commit_payload(
    observation: MeasurementObservation, chunk_record_digests: Sequence[str]
) -> dict[str, Any]:
    """The commit record payload naming the appended chunk records in order."""

    snapshot = _full_inventory(observation)
    return {
        "schema_version": 1,
        "event_type": CHUNKED_KIND,
        "provider": "gtm_web_inventory",
        "profile_ref": observation.profile_ref,
        "read_started_at": observation.read_started_at.isoformat(),
        "read_finished_at": observation.read_finished_at.isoformat(),
        "snapshot_digest": observation.snapshot_digest,
        "discovery": snapshot.discovery.model_dump(mode="json", warnings="error"),
        "inventory": snapshot.inventory.model_dump(
            mode="json", exclude=set(_SECTIONS), warnings="error"
        ),
        "chunk_record_digests": list(chunk_record_digests),
    }


def _invalid(message: str) -> AdhelmError:
    return AdhelmError("MEASUREMENT_CHUNKS_INVALID", message)


def reassemble_observation(
    commit: Mapping[str, Any], chunks: Sequence[Mapping[str, Any]]
) -> MeasurementObservation:
    """Rebuild and revalidate the observation a commit and its chunks retain.

    `chunks` are the payloads of the ledger records the commit names, in that
    order.  Each was schema-validated when the ledger read it, so only its place
    in the run is checked here; its records are then validated once, as part of
    the rebuilt observation, whose snapshot digest proves the run is complete,
    ordered and unaltered.
    """

    try:
        header = validate_json_value(ChunkedMeasurementObservation, dict(commit))
        if len(chunks) != len(header.chunk_record_digests):
            raise _invalid("A chunked measurement must retain every chunk it names.")
        for index, chunk in enumerate(chunks):
            if (
                chunk["chunk_index"] != index
                or chunk["chunk_count"] != len(chunks)
                or chunk["snapshot_digest"] != header.snapshot_digest
            ):
                raise _invalid("A measurement chunk does not belong at its position.")
        # The records came from verified ledger lines, so the canonical-domain
        # screening in `validate_json_value` is skipped: the snapshot digest
        # canonicalizes every record again.
        return MeasurementObservation.model_validate_json(
            json.dumps(
                {
                    "profile_ref": header.profile_ref,
                    "read_started_at": header.read_started_at.isoformat(),
                    "read_finished_at": header.read_finished_at.isoformat(),
                    "snapshot": {
                        "provider": header.provider,
                        "discovery": header.discovery.model_dump(mode="json", warnings="error"),
                        "inventory": {
                            **header.inventory.model_dump(mode="json", warnings="error"),
                            **{
                                section: [record for chunk in chunks for record in chunk[section]]
                                for section in _SECTIONS
                            },
                        },
                    },
                    "snapshot_digest": header.snapshot_digest,
                },
                allow_nan=False,
            )
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise _invalid("A chunked measurement does not reassemble into its observation.") from exc


def _instant(value: Any) -> datetime | None:
    """A record envelope's observation time as a UTC instant, or None if unusable."""

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def resolve_observation(
    commit_record: Mapping[str, Any], chunk_records: Mapping[str, Mapping[str, Any]]
) -> MeasurementObservation:
    """Rebuild the observation a retained commit record names from its chunk records.

    `chunk_records` maps a ledger record digest to a `MEASUREMENT_OBSERVATION_CHUNK`
    record that precedes the commit.  A chunk is accepted only under the same
    provider, account and observation time the commit binds; its content is then
    proven by the commit's snapshot digest.  Binding the observation time keeps
    the ledger's observation interval honest: a commit cannot adopt chunks that
    an earlier, interrupted read of the same snapshot left behind.
    """

    commit_observed_at = _instant(commit_record.get("observed_at"))
    selected: list[Mapping[str, Any]] = []
    for record_digest in commit_record["payload"]["chunk_record_digests"]:
        record = chunk_records.get(record_digest)
        if (
            record is None
            or record.get("provider") != commit_record.get("provider")
            or record.get("account_ref") != commit_record.get("account_ref")
        ):
            raise _invalid("A chunked measurement names a chunk that is not retained here.")
        if commit_observed_at is None or _instant(record.get("observed_at")) != commit_observed_at:
            raise _invalid("A measurement chunk was not observed with the commit that names it.")
        selected.append(record["payload"])
    return reassemble_observation(commit_record["payload"], selected)


def validate_chunked_measurement_history(
    records: Sequence[Mapping[str, Any]], *, verified: int = 0
) -> None:
    """Require every retained commit to name only its own earlier, unclaimed chunks.

    The first `verified` records are history the ledger has already validated
    (an append that follows a stream scan); they are indexed so a new commit can
    be resolved against them, but their commits are not reassembled again.
    """

    chunks: dict[str, Mapping[str, Any]] = {}
    claimed: set[str] = set()
    for position, record in enumerate(records):
        kind = record.get("record_kind")
        if kind == CHUNK_KIND:
            chunks[record["record_digest"]] = record
        elif kind == CHUNKED_KIND:
            named = record["payload"]["chunk_record_digests"]
            if claimed.intersection(named):
                raise _invalid("A measurement chunk is named by more than one observation.")
            if position >= verified:
                resolve_observation(record, chunks)
            claimed.update(named)

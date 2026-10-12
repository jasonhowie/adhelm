"""M01.1.retention: a complete GTM inventory larger than one ledger record.

A complete inventory canonicalizes to roughly 890 bytes per resource, so a
container crosses the 1 MiB record bound at about 1,150 resources.  It is
retained as bounded chunk records plus one commit record.  These tests prove the
observation is complete, ordered and exactly the one that was read, and that a
chunk run that is missing, reordered, repeated, foreign, altered or unfinished
never becomes (or shortens) a retained observation.

Everything uses `httpx.MockTransport`; no provider, credential store or network
is reached.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_gtm_dependency_graph_review import _graph, _items
from test_gtm_inventory import _HTML_SOURCE, _SENTINEL, _Fixture, _resource, _tag
from test_gtm_inventory_retention import _configure, _golden_ga4
from typer.testing import CliRunner

from adhelm import measurement_chunks
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import (
    MAX_PAYLOAD_BYTES,
    MAX_RECORD_BYTES,
    RECORD_ENVELOPE_RESERVE_BYTES,
    Ledger,
)
from adhelm.measurement_chunks import (
    CHUNK_KIND,
    CHUNKED_KIND,
    chunk_observation,
    commit_payload,
    reassemble_observation,
)
from adhelm.measurement_comparison import compare_measurements
from adhelm.measurement_records import (
    GtmFullInventoryMeasurementSnapshot,
    MeasurementObservation,
)
from adhelm.saved_measurements import load_measurement, measurement_catalog, retain_measurement

# 500 tags, triggers and variables, plus one custom HTML tag, are 1,501
# resources: past the 1,150 at which a single record overflows.
_PER_COLLECTION = 500
_CHUNK_BYTES = 700_000


def _large_items() -> dict[str, list[dict[str, Any]]]:
    return _items(
        tags=[
            _tag(
                "10",
                type="html",
                parameter=[{"type": "template", "key": "html", "value": _HTML_SOURCE}],
            ),
            *(
                _tag(
                    str(100_000 + index),
                    firingTriggerId=[str(200_000 + index)],
                    consentSettings={"consentStatus": "notNeeded"},
                )
                for index in range(_PER_COLLECTION)
            ),
        ],
        triggers=[
            _resource("trigger", "triggers", str(200_000 + index), type="pageview")
            for index in range(_PER_COLLECTION)
        ],
        variables=[
            _resource("variable", "variables", str(300_000 + index), type="v")
            for index in range(_PER_COLLECTION)
        ],
    )


def _read_gtm(root: Path, runner: CliRunner) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(root),
            "measurement",
            "gtm",
            "gtm-read",
            "2",
            "--ci-enabled",
            "--full-inventory",
        ],
    )
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.output)["data"])


@dataclass(frozen=True)
class Retained:
    root: Path
    record_digest: str
    observation: MeasurementObservation


@pytest.fixture(scope="module")
def retained(tmp_path_factory: pytest.TempPathFactory) -> Retained:
    root = tmp_path_factory.mktemp("chunked")
    with pytest.MonkeyPatch.context() as monkeypatch:
        runner = _configure(root, monkeypatch, _Fixture(items=_large_items()))
        receipt = _read_gtm(root, runner)
    observation, _ = load_measurement(Ledger(root), receipt["record_digest"])
    return Retained(root, receipt["record_digest"], observation)


@pytest.fixture(scope="module")
def read_twice(
    retained: Retained, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Ledger, str, str]:
    """The same inventory retained by two later reads; for read-only tests."""

    root = tmp_path_factory.mktemp("read-twice") / "data"
    shutil.copytree(retained.root, root)
    ledger = Ledger(root)
    finished = retained.observation.read_finished_at
    later = retain_measurement(
        ledger,
        retained.observation.snapshot,
        profile_ref=retained.observation.profile_ref,
        read_started_at=finished + timedelta(seconds=1),
        read_finished_at=finished + timedelta(seconds=2),
    )
    return ledger, retained.record_digest, str(later["record_digest"])


@pytest.fixture
def copy_of_history(retained: Retained, tmp_path: Path) -> Path:
    """A private copy of the large retained history, for tests that change it."""

    root = tmp_path / "data"
    shutil.copytree(retained.root, root)
    return root


@pytest.fixture
def reviewer(retained: Retained, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    from adhelm import measurement_cli

    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr("adhelm.credentials.export_key", lambda: b"k" * 32)
    return CliRunner()


def _history_lines(root: Path) -> list[bytes]:
    return [
        line
        for path in sorted((root / "ledger/changes").glob("*.jsonl"))
        for line in path.read_bytes().splitlines()
    ]


def _kinds(root: Path) -> Counter[str]:
    return Counter(json.loads(line)["record_kind"] for line in _history_lines(root))


def _append(
    ledger: Ledger,
    kind: str,
    payload: dict[str, Any],
    observation: MeasurementObservation,
    account_ref: str | None = None,
    observed_at: str | None = None,
) -> str:
    record = ledger.append(
        "changes",
        kind,
        payload,
        provider="gtm_web_inventory",
        account_ref=account_ref or observation.snapshot.account_ref,
        source="UNKNOWN",
        observed_at=observed_at or observation.read_finished_at.isoformat(),
    )
    return str(record["record_digest"])


def _forge(
    root: Path,
    observation: MeasurementObservation,
    *,
    chunks: list[dict[str, Any]] | None = None,
    order: Callable[[int], list[int]] | None = None,
    drop: int | None = None,
    chunk_account: str | None = None,
    chunk_observed_at: str | None = None,
) -> Ledger:
    """Append a chunk run, then a commit, with a deliberate defect."""

    ledger = Ledger(root)
    payloads = chunks or chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    digests = [
        _append(ledger, CHUNK_KIND, p, observation, chunk_account, chunk_observed_at)
        for p in payloads
    ]
    if order is not None:
        digests = [digests[index] for index in order(len(digests))]
    if drop is not None:
        digests.pop(drop)
    _append(ledger, CHUNKED_KIND, commit_payload(observation, digests), observation)
    return ledger


def test_an_inventory_past_one_record_is_retained_in_bounded_chunks(retained: Retained) -> None:
    snapshot = retained.observation.snapshot
    assert isinstance(snapshot, GtmFullInventoryMeasurementSnapshot)
    assert snapshot.inventory.counts.resources == 3 * _PER_COLLECTION + 1
    assert len(canonical_bytes(retained.observation)) > MAX_RECORD_BYTES

    kinds = _kinds(retained.root)
    assert kinds[CHUNK_KIND] >= 2
    assert kinds[CHUNKED_KIND] == 1
    assert kinds["MEASUREMENT_OBSERVATION"] == 0
    assert all(len(line) + 1 <= MAX_RECORD_BYTES for line in _history_lines(retained.root))

    assert retained.observation.snapshot_digest == digest(snapshot)
    assert len(snapshot.inventory.resources) == snapshot.inventory.counts.resources
    assert len(snapshot.inventory.edges) == snapshot.inventory.counts.edges
    assert Ledger(retained.root).verify()["status"] == "VERIFIED"

    history = b"".join(_history_lines(retained.root))
    assert _SENTINEL.encode() not in history
    assert b"fixture-token-not-for-output" not in history


def test_a_chunked_observation_is_listed_once_and_reviewed_without_rereading(
    retained: Retained, reviewer: CliRunner
) -> None:
    root = ["--data-dir", str(retained.root)]
    catalog = reviewer.invoke(app, [*root, "measurement", "catalog"])
    assert catalog.exit_code == 0, catalog.output
    listed = json.loads(catalog.output)["data"]["measurements"]
    assert [(item["provider"], item["record_digest"]) for item in listed] == [
        ("gtm_web_inventory", retained.record_digest)
    ]
    assert listed[0]["snapshot_digest"] == retained.observation.snapshot_digest

    shown = reviewer.invoke(
        app, [*root, "measurement", "show", retained.record_digest, "--limit", "3"]
    )
    assert shown.exit_code == 0, shown.output
    envelope = json.loads(shown.output)
    snapshot = retained.observation.snapshot
    assert isinstance(snapshot, GtmFullInventoryMeasurementSnapshot)
    expected_rows = (
        len(snapshot.inventory.coverage)
        + len(snapshot.inventory.resources)
        + len(snapshot.inventory.edges)
    )
    assert envelope["total_count"] == expected_rows
    assert envelope["data"]["snapshot_digest"] == retained.observation.snapshot_digest
    assert envelope["data"]["counts"]["resources"] == len(snapshot.inventory.resources)
    assert _SENTINEL not in shown.output

    graph = _graph(reviewer, retained.root, retained.record_digest, "--limit", "1")
    assert graph.exit_code == 0, graph.output
    data = json.loads(graph.output)["data"]
    assert data["counts"]["nodes"] == len(snapshot.inventory.resources)
    assert data["source"]["record_digest"] == retained.record_digest


def test_history_verify_index_tail_and_export_accept_chunked_records(
    retained: Retained, reviewer: CliRunner, tmp_path: Path
) -> None:
    root = ["--data-dir", str(retained.root)]
    verified = reviewer.invoke(app, [*root, "history", "verify"])
    assert verified.exit_code == 0, verified.output
    assert reviewer.invoke(app, [*root, "history", "index"]).exit_code == 0
    tailed = reviewer.invoke(app, [*root, "history", "tail"])
    assert tailed.exit_code == 0, tailed.output
    tail_kinds = Counter(
        item["record_kind"] for item in json.loads(tailed.output)["data"]["records"]
    )
    assert tail_kinds == _kinds(retained.root)

    export_root = tmp_path / "export"
    exported = reviewer.invoke(app, [*root, "history", "export", "--output", str(export_root)])
    assert exported.exit_code == 0, exported.output
    events = (export_root / "events.csv").read_text()
    assert events.count(CHUNK_KIND) == _kinds(retained.root)[CHUNK_KIND]
    assert events.count(CHUNKED_KIND) == 1
    for path in export_root.rglob("*"):
        if path.is_file():
            assert _SENTINEL not in path.read_text(errors="ignore")


def test_two_reads_of_one_inventory_stay_independent_observations(
    retained: Retained, read_twice: tuple[Ledger, str, str]
) -> None:
    ledger, first, second = read_twice
    assert first != second
    assert _kinds(ledger.root)[CHUNKED_KIND] == 2

    catalog = measurement_catalog(ledger)
    assert catalog["total_count"] == 2
    assert {item["snapshot_digest"] for item in catalog["measurements"]} == {
        retained.observation.snapshot_digest
    }
    for record_digest in (first, second):
        observation, _ = load_measurement(ledger, record_digest)
        assert observation.snapshot == retained.observation.snapshot


def test_a_small_inventory_is_still_one_unchanged_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure(tmp_path, monkeypatch, _Fixture())
    receipt = _read_gtm(tmp_path, runner)
    assert _kinds(tmp_path)["MEASUREMENT_OBSERVATION"] == 1
    assert CHUNK_KIND not in _kinds(tmp_path)
    assert CHUNKED_KIND not in _kinds(tmp_path)
    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    assert isinstance(observation.snapshot, GtmFullInventoryMeasurementSnapshot)


def test_a_read_that_stops_before_its_commit_retains_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure(tmp_path, monkeypatch, _Fixture(items=_large_items()))
    original = Ledger.append

    def stop_at_commit(self: Ledger, stream: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        if kind == CHUNKED_KIND:
            raise OSError("interrupted before the commit record")
        return original(self, stream, kind, *args, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(Ledger, "append", stop_at_commit)
        result = runner.invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "measurement",
                "gtm",
                "gtm-read",
                "2",
                "--ci-enabled",
                "--full-inventory",
            ],
        )
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "MEASUREMENT_RETENTION_UNVERIFIED"

    orphans = _kinds(tmp_path)
    assert orphans[CHUNK_KIND] >= 2
    assert orphans[CHUNKED_KIND] == 0
    ledger = Ledger(tmp_path)
    assert ledger.verify()["status"] == "VERIFIED"
    assert measurement_catalog(ledger)["total_count"] == 0
    orphan = next(
        json.loads(line) for line in _history_lines(tmp_path) if CHUNK_KIND.encode() in line
    )
    with pytest.raises(AdhelmError) as missing:
        load_measurement(ledger, orphan["record_digest"])
    assert missing.value.code == "MEASUREMENT_NOT_RETAINED"

    receipt = _read_gtm(tmp_path, runner)
    assert measurement_catalog(ledger)["total_count"] == 1
    observation, _ = load_measurement(ledger, receipt["record_digest"])
    assert isinstance(observation.snapshot, GtmFullInventoryMeasurementSnapshot)
    assert len(observation.snapshot.inventory.resources) == 3 * _PER_COLLECTION + 1


def _alter_first_chunk(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    altered = json.loads(json.dumps(payloads))
    section = next(name for name in ("resources", "edges", "coverage") if altered[0][name])
    altered[0][section].pop()
    return altered


def _foreign_snapshot_digest(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    foreign = json.loads(json.dumps(payloads))
    foreign[1]["snapshot_digest"] = "f" * 64
    return foreign


def _swap_chunk_labels(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the records in order but label the first two chunks with each other's index."""

    relabelled = json.loads(json.dumps(payloads))
    relabelled[0]["chunk_index"], relabelled[1]["chunk_index"] = 1, 0
    return relabelled


@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        pytest.param({"drop": 1}, "MEASUREMENT_CHUNKS_INVALID", id="a-chunk-is-not-named"),
        pytest.param(
            # Every chunk is named, so only the chunk_index order can refuse it.
            {"order": lambda count: [1, 0, *range(2, count)]},
            "MEASUREMENT_CHUNKS_INVALID",
            id="chunks-out-of-order",
        ),
        pytest.param(
            # The records stay in order, so the snapshot digest cannot notice:
            # only the chunk_index check isolates a mislabelled chunk.
            {"alter": _swap_chunk_labels},
            "MEASUREMENT_CHUNKS_INVALID",
            id="a-chunk-labelled-with-another-index",
        ),
        pytest.param(
            {"order": lambda count: [0, 0, *range(2, count)]},
            "INVALID_LEDGER_PAYLOAD",
            id="a-chunk-named-twice",
        ),
        pytest.param(
            {"chunk_observed_at": "2001-01-01T00:00:00+00:00"},
            "MEASUREMENT_CHUNKS_INVALID",
            id="chunks-from-another-observation-time",
        ),
        pytest.param(
            {"chunk_account": "gtm.account.999"},
            "MEASUREMENT_CHUNKS_INVALID",
            id="chunks-under-another-account",
        ),
        pytest.param(
            {"alter": _alter_first_chunk},
            "MEASUREMENT_CHUNKS_INVALID",
            id="a-record-removed-from-a-chunk",
        ),
        pytest.param(
            {"alter": _foreign_snapshot_digest},
            "MEASUREMENT_CHUNKS_INVALID",
            id="a-chunk-of-another-observation",
        ),
    ],
)
def test_a_defective_chunk_run_is_refused_and_retains_nothing(
    retained: Retained, tmp_path: Path, defect: dict[str, Any], expected: str
) -> None:
    observation = retained.observation
    payloads = chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    # The order cases are permutations of every chunk; pin the size they rely on.
    assert len(payloads) == 3
    alter = defect.pop("alter", None)
    chunks = alter(payloads) if alter is not None else None
    with pytest.raises(AdhelmError) as refused:
        _forge(tmp_path, observation, chunks=chunks, **defect)
    assert refused.value.code == expected
    assert _kinds(tmp_path)[CHUNKED_KIND] == 0
    assert measurement_catalog(Ledger(tmp_path))["total_count"] == 0


def test_chunks_left_by_an_earlier_read_cannot_be_adopted_by_a_later_commit(
    retained: Retained, tmp_path: Path
) -> None:
    observation = retained.observation
    ledger = Ledger(tmp_path)
    orphans = [
        _append(ledger, CHUNK_KIND, payload, observation)
        for payload in chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    ]
    # The same snapshot read again 30 days later: same digest, a different read.
    later = observation.model_copy(
        update={
            "read_started_at": observation.read_started_at + timedelta(days=30),
            "read_finished_at": observation.read_finished_at + timedelta(days=30),
        }
    )
    with pytest.raises(AdhelmError) as adopted:
        _append(ledger, CHUNKED_KIND, commit_payload(later, orphans), later)
    assert adopted.value.code == "MEASUREMENT_CHUNKS_INVALID"
    assert measurement_catalog(ledger)["total_count"] == 0


def _chunk_index_at_count(chunk: dict[str, Any]) -> None:
    chunk["chunk_index"] = chunk["chunk_count"]


def _chunk_without_records(chunk: dict[str, Any]) -> None:
    chunk.update(coverage=[], resources=[], edges=[])


def _chunk_with_unknown_field(chunk: dict[str, Any]) -> None:
    chunk["note"] = "extra"


def _commit_with_reversed_interval(commit: dict[str, Any]) -> None:
    commit["read_started_at"], commit["read_finished_at"] = (
        commit["read_finished_at"],
        commit["read_started_at"],
    )


def _commit_with_unknown_field(commit: dict[str, Any]) -> None:
    commit["note"] = "extra"


@pytest.mark.parametrize(
    ("kind", "malform"),
    [
        pytest.param(CHUNK_KIND, _chunk_index_at_count, id="chunk-index-at-its-count"),
        pytest.param(CHUNK_KIND, _chunk_without_records, id="chunk-without-records"),
        pytest.param(CHUNK_KIND, _chunk_with_unknown_field, id="chunk-with-unknown-field"),
        pytest.param(CHUNKED_KIND, _commit_with_reversed_interval, id="commit-reversed-interval"),
        pytest.param(CHUNKED_KIND, _commit_with_unknown_field, id="commit-with-unknown-field"),
    ],
)
def test_a_malformed_chunk_or_commit_payload_is_refused_and_retains_nothing(
    retained: Retained, tmp_path: Path, kind: str, malform: Callable[[dict[str, Any]], None]
) -> None:
    observation = retained.observation
    if kind == CHUNK_KIND:
        payload = chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)[0]
    else:
        payload = commit_payload(observation, ["a" * 64])
    malform(payload)
    with pytest.raises(AdhelmError) as refused:
        _append(Ledger(tmp_path), kind, payload, observation)
    assert refused.value.code == "INVALID_LEDGER_PAYLOAD"
    assert not (tmp_path / "ledger").exists()


def test_one_chunk_cannot_belong_to_two_observations(retained: Retained, tmp_path: Path) -> None:
    observation = retained.observation
    ledger = Ledger(tmp_path)
    digests = [
        _append(ledger, CHUNK_KIND, payload, observation)
        for payload in chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    ]
    commit = commit_payload(observation, digests)
    _append(ledger, CHUNKED_KIND, commit, observation)
    with pytest.raises(AdhelmError) as claimed:
        _append(ledger, CHUNKED_KIND, commit, observation)
    assert claimed.value.code == "MEASUREMENT_CHUNKS_INVALID"
    assert measurement_catalog(ledger)["total_count"] == 1


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"source": "LOCAL_CLI"}, id="not-an-unknown-source"),
        pytest.param({"account_ref": None}, id="no-account"),
        pytest.param({"observed_at": None}, id="no-observation-time"),
        pytest.param({"provider": "gtm"}, id="another-provider"),
        pytest.param({"run_id": "run-1"}, id="inside-an-execution-run"),
    ],
)
def test_a_chunk_must_bind_provider_account_and_unknown_source(
    retained: Retained, tmp_path: Path, envelope: dict[str, Any]
) -> None:
    chunk = chunk_observation(retained.observation, maximum_chunk_bytes=_CHUNK_BYTES)[0]
    arguments: dict[str, Any] = {
        "provider": "gtm_web_inventory",
        "account_ref": retained.observation.snapshot.account_ref,
        "source": "UNKNOWN",
        "observed_at": "2026-09-09T00:00:00Z",
        **envelope,
    }
    with pytest.raises(AdhelmError) as refused:
        Ledger(tmp_path).append("changes", CHUNK_KIND, chunk, **arguments)
    assert refused.value.code == "INVALID_MEASUREMENT_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def _rehash(root: Path, mutate: Any) -> None:
    """Change a chunk, then re-link the whole chain as an attacker with file access could."""

    (path,) = (root / "ledger/changes").glob("*.jsonl")
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    renamed: dict[str, str] = {}
    previous = None
    for record in records:
        if record["record_kind"] == CHUNK_KIND and record["payload"]["chunk_index"] == 0:
            mutate(record["payload"])
        if record["record_kind"] == CHUNKED_KIND:
            record["payload"]["chunk_record_digests"] = [
                renamed.get(item, item) for item in record["payload"]["chunk_record_digests"]
            ]
        old = record["record_digest"]
        record["previous_record_digest"] = previous
        record["record_digest"] = digest({k: v for k, v in record.items() if k != "record_digest"})
        renamed[old] = previous = record["record_digest"]
    path.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))


def test_a_chunk_changed_on_disk_fails_verified_reads(
    retained: Retained, copy_of_history: Path
) -> None:
    (path,) = (copy_of_history / "ledger/changes").glob("*.jsonl")
    lines = path.read_bytes().splitlines()
    index = next(i for i, line in enumerate(lines) if CHUNK_KIND.encode() in line)
    # Flip one hex digit of a configuration fingerprint: still a valid payload,
    # so only the record digest can notice.
    marker = b'"configuration_fingerprint":"'
    position = lines[index].index(marker) + len(marker)
    flipped = b"1" if lines[index][position : position + 1] == b"0" else b"0"
    lines[index] = lines[index][:position] + flipped + lines[index][position + 1 :]
    path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(AdhelmError) as tampered:
        load_measurement(Ledger(copy_of_history), retained.record_digest)
    assert tampered.value.code == "LEDGER_DIGEST_MISMATCH"


def test_a_rehashed_chunk_with_a_record_removed_is_not_a_shorter_observation(
    retained: Retained, copy_of_history: Path
) -> None:
    def drop_a_resource(payload: dict[str, Any]) -> None:
        payload["resources"].pop()

    _rehash(copy_of_history, drop_a_resource)
    ledger = Ledger(copy_of_history)
    with pytest.raises(AdhelmError) as shortened:
        ledger.checkpoint()
    assert shortened.value.code == "MEASUREMENT_CHUNKS_INVALID"


def test_comparison_refuses_a_chunked_full_inventory_like_a_single_record(
    read_twice: tuple[Ledger, str, str],
) -> None:
    ledger, first, second = read_twice
    with pytest.raises(AdhelmError) as refusal:
        compare_measurements(ledger, first, second)
    assert refusal.value.code == "NOT_COMPARABLE_FULL_INVENTORY"
    assert refusal.value.exit_code == 2


def test_chunks_are_deterministic_bounded_and_reassemble_to_the_exact_observation(
    retained: Retained,
) -> None:
    observation = retained.observation
    first = chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    assert first == chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    assert len(first) > len(chunk_observation(observation, maximum_chunk_bytes=MAX_PAYLOAD_BYTES))
    assert all(len(canonical_bytes(chunk)) <= _CHUNK_BYTES for chunk in first)
    assert [chunk["chunk_index"] for chunk in first] == list(range(len(first)))
    assert {chunk["chunk_count"] for chunk in first} == {len(first)}

    names = [f"{index:064x}" for index in range(len(first))]
    rebuilt = reassemble_observation(commit_payload(observation, names), first)
    assert rebuilt == observation
    assert digest(rebuilt) == digest(observation)


def test_a_record_larger_than_a_chunk_or_too_many_chunks_fail_closed(
    retained: Retained, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AdhelmError) as oversized:
        chunk_observation(retained.observation, maximum_chunk_bytes=2_000)
    assert oversized.value.code == "LEDGER_RECORD_TOO_LARGE"

    monkeypatch.setattr(measurement_chunks, "MAXIMUM_CHUNKS", 2)
    with pytest.raises(AdhelmError) as unbounded:
        chunk_observation(retained.observation, maximum_chunk_bytes=_CHUNK_BYTES)
    assert unbounded.value.code == "LEDGER_RECORD_TOO_LARGE"


def test_only_a_complete_gtm_inventory_can_be_chunked() -> None:
    snapshot = _golden_ga4()
    observation = MeasurementObservation.model_validate(
        {
            "profile_ref": "measurement.fixture",
            "read_started_at": datetime(2026, 9, 9, tzinfo=UTC),
            "read_finished_at": datetime(2026, 9, 9, tzinfo=UTC),
            "snapshot": snapshot.model_dump(mode="json"),
            "snapshot_digest": digest(snapshot),
        },
        strict=False,
    )
    with pytest.raises(AdhelmError) as refused:
        chunk_observation(observation, maximum_chunk_bytes=_CHUNK_BYTES)
    assert refused.value.code == "MEASUREMENT_CHUNKING_UNSUPPORTED"


def test_the_envelope_reserve_covers_the_widest_record_envelope(tmp_path: Path) -> None:
    """A payload at the single-record budget still fits once its envelope is added."""

    ledger = Ledger(tmp_path, profile_ref="p" * 160)
    payload = {"previous_segment_head_digest": None, "previous_segment_name": None}
    record = ledger._make_record(
        "changes",
        "SEGMENT_STARTED",
        payload,
        sequence=999_999_999,
        previous_digest="f" * 64,
        provider="gtm_web_inventory",
        account_ref="a" * 160,
        run_id="r" * 160,
        source="LOCAL_CLI",
        occurred_at="2026-09-09T00:00:00Z",
        observed_at="2026-09-09T00:00:00Z",
    )
    envelope = len(canonical_bytes(record)) - len(canonical_bytes(payload))
    longer_kind = len(CHUNKED_KIND) - len("SEGMENT_STARTED")
    assert envelope + longer_kind < RECORD_ENVELOPE_RESERVE_BYTES
    assert MAX_PAYLOAD_BYTES + RECORD_ENVELOPE_RESERVE_BYTES == MAX_RECORD_BYTES

"""Independent adversarial review of retained GA4 key-continuity comparison."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_saved_measurements import END, START, _ga4_snapshot
from typer.testing import CliRunner

from adhelm import credentials, measurement_cli, measurement_comparison
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import compare_measurements
from adhelm.measurement_comparison_artifacts import (
    explicit_measurement_comparison_artifact_decision,
    render_measurement_comparison_artifacts,
)
from adhelm.models import ArtifactRule, Configuration, OutputPreferences
from adhelm.saved_measurements import load_measurement, retain_measurement


def _pair(root: Path, markers: tuple[str | None, str | None]) -> tuple[str, str]:
    references: list[str] = []
    for index, marker in enumerate(markers):
        snapshot = _ga4_snapshot()
        if marker is not None:
            snapshot = snapshot.model_copy(
                update={
                    "inventory": snapshot.inventory.model_copy(
                        update={"fingerprint_key_marker": marker}
                    )
                }
            )
        retained = retain_measurement(
            Ledger(root),
            snapshot,
            profile_ref="continuity.ultra.profile",
            read_started_at=START + timedelta(minutes=index),
            read_finished_at=END + timedelta(minutes=index),
        )
        references.append(retained["record_digest"])
    return references[0], references[1]


def _rewrite_last_measurement(
    root: Path,
    mutate: Callable[[dict[str, Any]], None],
    *,
    preserve_noncanonical_final_json: bool = False,
) -> str:
    """Apply a self-consistent physical final-record rewrite for scan coverage.

    The last record has no child ``previous_record_digest`` to update.  This
    simulates an attacker who can recompute ordinary unkeyed JSONL digests,
    rather than merely corrupting bytes that the ledger rejects earlier.
    """
    path = next((root / "ledger" / "changes").glob("*.jsonl"))
    records: list[dict[str, Any]] = [json.loads(line) for line in path.read_text().splitlines()]
    record = records[-1]
    payload = record["payload"]
    assert isinstance(payload, dict)
    mutate(payload)
    snapshot = payload["snapshot"]
    assert isinstance(snapshot, dict)
    payload["snapshot_digest"] = digest(snapshot)
    record["record_digest"] = digest(
        {name: value for name, value in record.items() if name != "record_digest"}
    )
    encoded = [canonical_bytes(item) + b"\n" for item in records]
    if preserve_noncanonical_final_json:
        # JCS turns 1.0 into 1.  Preserve the original physical scalar so the
        # scan must reject it before canonicalization can launder it.
        encoded[-1] = (
            json.dumps(record, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
            + b"\n"
        )
    path.write_bytes(b"".join(encoded))
    return record["record_digest"]


def _set_marker(value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(payload: dict[str, Any]) -> None:
        inventory = payload["snapshot"]["inventory"]
        assert isinstance(inventory, dict)
        inventory["fingerprint_key_marker"] = value

    return mutate


def test_rehashed_physical_marker_mismatch_stops_before_fact_generation(tmp_path: Path) -> None:
    before, _ = _pair(tmp_path, ("a" * 64, "a" * 64))
    altered = _rewrite_last_measurement(tmp_path, _set_marker("b" * 64))
    ledger = Ledger(tmp_path)
    assert ledger.checkpoint()["status"] == "VERIFIED_CHECKPOINT"

    def forbidden(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("A differing retained key marker must not reach fact generation")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(measurement_comparison, "_facts", forbidden)
    try:
        with pytest.raises(AdhelmError) as error:
            compare_measurements(ledger, before, altered)
    finally:
        monkeypatch.undo()
    assert error.value.code == "MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH"


@pytest.mark.parametrize(
    ("mutate", "noncanonical"),
    [
        (_set_marker(True), False),
        (_set_marker(None), False),
        (
            lambda payload: payload["snapshot"]["inventory"].__setitem__("schema_version", 1.0),
            True,
        ),
        (lambda payload: payload.__setitem__("execution_authority", 0.0), True),
    ],
)
def test_physical_rehashed_marker_and_literal_aliases_fail_the_ledger_scan(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    noncanonical: bool,
) -> None:
    before, _ = _pair(tmp_path, ("a" * 64, "a" * 64))
    altered = _rewrite_last_measurement(
        tmp_path, mutate, preserve_noncanonical_final_json=noncanonical
    )
    final_line = (
        next((tmp_path / "ledger" / "changes").glob("*.jsonl")).read_bytes().splitlines()[-1]
    )
    if noncanonical:
        assert b"1.0" in final_line or b"0.0" in final_line
    with pytest.raises(AdhelmError) as error:
        compare_measurements(Ledger(tmp_path), before, altered)
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"


def test_markerless_history_stays_unknown_without_injected_null(tmp_path: Path) -> None:
    before, after = _pair(tmp_path, (None, None))
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    raw_records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in raw_records[1:]:
        if record["record_kind"] == "MEASUREMENT_OBSERVATION":
            assert "fingerprint_key_marker" not in record["payload"]["snapshot"]["inventory"]

    loaded, _ = load_measurement(Ledger(tmp_path), before)
    assert "fingerprint_key_marker" not in loaded.model_dump(mode="json")["snapshot"]["inventory"]
    comparison = compare_measurements(Ledger(tmp_path), before, after)
    assert comparison.fingerprint_key_continuity == "NOT_EVIDENCED"
    assert "fingerprint_key_marker" not in comparison.model_dump_json()


def test_saved_rule_marker_mismatch_cannot_access_key_or_create_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, _ = _pair(tmp_path, ("a" * 64, "a" * 64))
    altered = _rewrite_last_measurement(tmp_path, _set_marker("b" * 64))
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    preferences = OutputPreferences(
        artifact_mode="RULES",
        purposes=["comparison"],
        output_directory=str(output),
        workflow_rules=[
            ArtifactRule(
                workflow="diagnostics",
                stage="completion",
                providers=["ga4_admin"],
                profiles=["continuity.ultra.profile"],
                formats=["json"],
            )
        ],
    )
    save_configuration(tmp_path, Configuration(output=preferences))

    def forbidden() -> bytes:
        raise AssertionError("A mismatched marker must fail before key access")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "measurement", "compare", before, altered]
    )
    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output)
    assert envelope["diagnostics"][0]["code"] == "MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH"
    assert list(output.iterdir()) == []
    assert "a" * 64 not in result.output and "b" * 64 not in result.output


@pytest.mark.parametrize(
    ("markers", "forged_status"),
    [
        (("a" * 64, "a" * 64), "NOT_EVIDENCED"),
        ((None, None), "MATCHING_RECORDED_KEY_MARKERS"),
    ],
)
def test_artifact_rebuild_rejects_a_syntactically_valid_forged_continuity_status(
    tmp_path: Path,
    markers: tuple[str | None, str | None],
    forged_status: str,
) -> None:
    before, after = _pair(tmp_path, markers)
    comparison = compare_measurements(Ledger(tmp_path), before, after)
    forged = comparison.model_copy(update={"fingerprint_key_continuity": forged_status})
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    decision = explicit_measurement_comparison_artifact_decision(
        OutputPreferences(output_directory=str(output)),
        provider="ga4_admin",
        profile_id="continuity.ultra.profile",
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )
    with pytest.raises(AdhelmError) as error:
        render_measurement_comparison_artifacts(
            Ledger(tmp_path), forged, decision, key=b"not-needed-before-source-validation"
        )
    assert error.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"
    assert list(output.iterdir()) == []

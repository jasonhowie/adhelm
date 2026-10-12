"""Retained GA4 continuity supports historical comparison, never current authority."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_saved_measurements import END, START, _ga4_snapshot, _openai_snapshot
from typer.testing import CliRunner

from adhelm import credentials, measurement_cli, measurement_comparison
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import MeasurementComparison, compare_measurements
from adhelm.measurement_records import MeasurementObservation
from adhelm.models import Configuration, OutputPreferences
from adhelm.saved_measurements import load_measurement, retain_measurement


def _pair(root: Path, markers: tuple[str | None, str | None], *, count: int = 1):
    references = []
    for index, marker in enumerate(markers):
        snapshot = _ga4_snapshot(count)
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
            profile_ref="ga4.fixture",
            read_started_at=START + timedelta(minutes=index),
            read_finished_at=END + timedelta(minutes=index),
        )
        references.append(retained["record_digest"])
    return references[0], references[1]


@pytest.mark.parametrize(
    ("markers", "expected"),
    [
        ((None, None), "NOT_EVIDENCED"),
        ((None, "a" * 64), "NOT_EVIDENCED"),
        (("a" * 64, None), "NOT_EVIDENCED"),
        (("a" * 64, "a" * 64), "MATCHING_RECORDED_KEY_MARKERS"),
    ],
)
def test_continuity_uses_both_retained_markers_and_preserves_history(
    tmp_path: Path, markers: tuple[str | None, str | None], expected: str
) -> None:
    before, after = _pair(tmp_path, markers)
    ledger = Ledger(tmp_path)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    comparison = compare_measurements(ledger, before, after)
    assert comparison.fingerprint_key_continuity == expected
    assert comparison.facts == ()
    assert comparison.execution_authority is comparison.remote_mutations is False
    assert comparison.observed_actor == "UNKNOWN"
    assert comparison.exact_change_time is None
    assert "fingerprint_key_marker" not in comparison.model_dump_json()
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint
    observation, _ = load_measurement(ledger, before)
    wire = observation.model_dump(mode="json")["snapshot"]["inventory"]
    if markers[0] is None:
        assert "fingerprint_key_marker" not in wire
    else:
        assert wire["fingerprint_key_marker"] == markers[0]


def test_different_recorded_keys_fail_before_diff_even_without_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path, ("a" * 64, "b" * 64), count=0)

    def forbidden(*args):
        raise AssertionError("Differing fingerprint keys cannot reach configuration diff")

    monkeypatch.setattr(measurement_comparison, "_facts", forbidden)
    with pytest.raises(AdhelmError) as error:
        compare_measurements(Ledger(tmp_path), before, after)
    assert error.value.code == "MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH"


def test_cli_mismatch_fails_before_export_key_or_artifact_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path, ("a" * 64, "b" * 64))
    output = tmp_path / "artifacts"
    output.mkdir()
    save_configuration(tmp_path, Configuration(output=OutputPreferences()))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"formats": ["json"], "output_directory": str(output)}))

    def forbidden():
        raise AssertionError("A differing-key comparison cannot obtain a sharing key")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    monkeypatch.setattr(credentials, "export_key", forbidden)
    runner = CliRunner()
    for arguments in (
        ["measurement", "compare", before, after],
        ["artifacts", "measurement-comparison", before, after, "--input", str(request)],
    ):
        result = runner.invoke(app, ["--data-dir", str(tmp_path), *arguments])
        assert result.exit_code != 0, result.output
        envelope = json.loads(result.output)
        assert envelope["diagnostics"][0]["code"] == (
            "MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH"
        )
        assert envelope["artifact_refs"] == []
        assert list(output.iterdir()) == []


def test_matching_marker_status_reaches_inline_and_all_artifact_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "c" * 64
    before, after = _pair(tmp_path, (marker, marker))
    output = tmp_path / "artifacts"
    output.mkdir()
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"formats": ["json", "csv", "html"], "output_directory": str(output)})
    )
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"x" * 32)
    monkeypatch.setattr(credentials, "export_key", lambda: b"x" * 32)
    runner = CliRunner()
    inline = runner.invoke(
        app, ["--data-dir", str(tmp_path), "measurement", "compare", before, after]
    )
    assert inline.exit_code == 0, inline.output
    assert json.loads(inline.output)["data"]["fingerprint_key_continuity"] == (
        "MATCHING_RECORDED_KEY_MARKERS"
    )
    files = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "artifacts",
            "measurement-comparison",
            before,
            after,
            "--input",
            str(request),
        ],
    )
    assert files.exit_code == 0, files.output
    assert len(json.loads(files.output)["artifact_refs"]) == 3
    for path in output.iterdir():
        content = path.read_text()
        assert "MATCHING_RECORDED_KEY_MARKERS" in content
        assert marker not in content
        assert "NOT_EVALUATED" in content
    assert marker not in inline.output + files.output


@pytest.mark.parametrize("value", [True, 1.0])
def test_constructed_inventory_version_is_rejected_before_retention(
    tmp_path: Path, value: object
) -> None:
    snapshot = _ga4_snapshot()
    forged = snapshot.model_copy(
        update={"inventory": snapshot.inventory.model_copy(update={"schema_version": value})}
    )
    ledger = Ledger(tmp_path)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(ValueError):
        retain_measurement(
            ledger, forged, profile_ref="ga4.fixture", read_started_at=START, read_finished_at=END
        )
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("execution_authority", 0),
        ("execution_authority", 0.0),
    ],
)
def test_raw_measurement_controls_do_not_become_canonical_aliases(
    field: str, value: object
) -> None:
    snapshot = _ga4_snapshot()
    payload = MeasurementObservation(
        profile_ref="ga4.fixture",
        read_started_at=START,
        read_finished_at=END,
        snapshot=snapshot,
        snapshot_digest=digest(snapshot),
    ).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        MeasurementObservation.model_validate_json(json.dumps(payload))


def test_other_provider_cannot_claim_ga4_marker_continuity(tmp_path: Path) -> None:
    references = []
    for index in range(2):
        record = retain_measurement(
            Ledger(tmp_path),
            _openai_snapshot(),
            profile_ref="openai.fixture",
            read_started_at=START + timedelta(minutes=index),
            read_finished_at=END + timedelta(minutes=index),
        )
        references.append(record["record_digest"])
    comparison = compare_measurements(Ledger(tmp_path), *references)
    forged = comparison.model_dump(mode="json")
    forged["fingerprint_key_continuity"] = "MATCHING_RECORDED_KEY_MARKERS"
    with pytest.raises(ValidationError):
        MeasurementComparison.model_validate_json(json.dumps(forged))

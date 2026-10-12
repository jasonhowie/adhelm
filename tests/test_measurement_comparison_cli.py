"""CLI comparison checks use retained synthetic records and no credential store."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_gtm_measurement_history import _http_fixture
from test_gtm_measurement_workflow import _profile as gtm_profile
from test_saved_measurements import END, START, _ga4_snapshot, _openai_snapshot
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import MeasurementComparison, compare_measurements
from adhelm.models import Configuration, OutputPreferences
from adhelm.saved_measurements import retain_measurement


def _pair(root: Path, *, provider: str = "ga4_admin") -> tuple[str, str]:
    snapshots = (
        (_ga4_snapshot(1), _ga4_snapshot(3))
        if provider == "ga4_admin"
        else (_openai_snapshot(), _openai_snapshot())
    )
    refs = []
    for index, snapshot in enumerate(snapshots):
        record = retain_measurement(
            Ledger(root),
            snapshot,
            profile_ref="private.measurement.profile",
            read_started_at=START + timedelta(minutes=index),
            read_finished_at=END + timedelta(minutes=index),
        )
        refs.append(record["record_digest"])
    return refs[0], refs[1]


def _invoke(root: Path, before: str, after: str):
    return CliRunner().invoke(
        app, ["--data-dir", str(root), "measurement", "compare", before, after]
    )


@pytest.mark.parametrize("provider", ["ga4_admin", "openai_ads"])
def test_complete_comparison_is_redacted_deterministic_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    before, after = _pair(tmp_path, provider=provider)
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    key_reads = []

    def key():
        key_reads.append(True)
        return b"k" * 32

    monkeypatch.setattr(measurement_cli, "export_key", key)
    first = _invoke(tmp_path, before, after)
    second = _invoke(tmp_path, before, after)
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.output == second.output
    envelope = json.loads(first.output)
    data = envelope["data"]
    assert envelope["outcome"] == "HISTORICAL_CONFIGURATION_COMPARISON"
    assert envelope["next_cursor"] is None
    assert envelope["total_count"] == len(data["facts"]) == (2 if provider == "ga4_admin" else 0)
    assert data["checkpoint_digest"] == checkpoint
    assert data["before"]["record_digest"] == before and data["after"]["record_digest"] == after
    assert data["observed_actor"] == "UNKNOWN" and data["exact_change_time"] is None
    assert data["execution_authority"] is data["remote_mutations"] is False
    assert data["runtime_firing"] == data["processed_receipt"] == "NOT_EVALUATED"
    assert data["implementation_evidence"] == "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    assert len(data["source_completeness"]) == 2
    assert data["before"]["profile_ref"].startswith("opaque:")
    for private in ("private.measurement.profile", "acct_fixture", "key_event.701", "property.123"):
        assert private not in first.output
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint
    assert len(key_reads) == 2


@pytest.mark.parametrize("bound", ["rows", "bytes"])
def test_insufficient_output_bound_fails_before_key_access_without_partial_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    before, after = _pair(tmp_path)
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    preferences = (
        OutputPreferences(max_rows=1)
        if bound == "rows"
        else OutputPreferences(max_output_bytes=1024)
    )
    save_configuration(tmp_path, Configuration(output=preferences))

    def forbidden():
        raise AssertionError("Output preflight must precede key access")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    result = _invoke(tmp_path, before, after)
    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output)
    assert envelope["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert not envelope["data"]
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint


@pytest.mark.parametrize("selection", ["missing", "same", "malformed", "reversed"])
def test_invalid_selection_is_bounded_and_never_accesses_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    before, after = _pair(tmp_path)
    if selection == "missing":
        after = "f" * 64
    elif selection == "same":
        after = before
    elif selection == "malformed":
        after = "private-invalid-input"
    else:
        before, after = after, before

    def forbidden():
        raise AssertionError("Invalid selection must precede key access")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    result = _invoke(tmp_path, before, after)
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["outcome"] == "FAILED_CLOSED"
    assert "private-invalid-input" not in result.output


def test_discovery_exposes_closed_request_and_bounded_non_authorizing_result_schema() -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert "measurement compare" in discovery["commands"]
    request = runner.invoke(app, ["schema", "measurement-comparison-request"])
    assert request.exit_code == 0, request.output
    schema = json.loads(request.output)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "schema_version",
        "before_record_digest",
        "after_record_digest",
    }
    result = runner.invoke(app, ["schema", "measurement-comparison", "--root-only"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["facts"]["maxItems"] == 1000
    assert schema["properties"]["execution_authority"]["const"] is False


def test_gtm_comparison_after_two_full_fixture_reads_preserves_partial_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = gtm_profile()
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    paths = _http_fixture(monkeypatch)
    runner = CliRunner()
    records = []
    for _ in range(2):
        read = runner.invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "measurement",
                "gtm",
                profile.profile_id,
                "2",
                "--ci-enabled",
            ],
        )
        assert read.exit_code == 0, read.output
        records.append(json.loads(read.output)["data"]["record_digest"])
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    before_paths = paths.copy()
    result = _invoke(tmp_path, *records)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["facts"] == [] and paths == before_paths
    assert data["fingerprint_key_continuity"] == "NOT_EVIDENCED"
    for completeness in data["source_completeness"]:
        assert completeness["inventory_coverage"] == "VERSION_COUNTS_AND_WORKSPACE_METADATA"
        assert completeness["dependency_completeness"] == "NOT_PROVEN"
        assert completeness["staging_eligibility"] == "NOT_EVALUATED"
        assert completeness["latest_header_count_status"] == "HEADER_COUNTS_UNAVAILABLE"
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint
    assert "private-" not in result.output and "gtm.container.1.2" not in result.output


@pytest.mark.parametrize("provider", ["ga4_admin", "openai_ads"])
@pytest.mark.parametrize("value", [1, 1.0])
def test_comparison_result_coverage_requires_original_json_booleans(
    tmp_path: Path, provider: str, value: int | float
) -> None:
    before, after = _pair(tmp_path, provider=provider)
    data = compare_measurements(Ledger(tmp_path), before, after).model_dump(mode="json")
    data["source_completeness"][0]["pagination_verified"] = value
    with pytest.raises(ValidationError):
        MeasurementComparison.model_validate_json(json.dumps(data))

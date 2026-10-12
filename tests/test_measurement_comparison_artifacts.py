"""Hermetic regressions for typed retained measurement-comparison artifacts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from test_measurement_comparison import (
    _comparison_records,
    _ga4_snapshot,
    _gtm_snapshot,
)

import adhelm.artifacts as artifacts
from adhelm.artifact_detail import ArtifactDetail
from adhelm.artifacts import ArtifactDecision, ArtifactFormat
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import MeasurementComparison, compare_measurements
from adhelm.measurement_comparison_artifacts import (
    MeasurementComparisonArtifactRequest,
    MeasurementComparisonArtifactResult,
    explicit_measurement_comparison_artifact_decision,
    measurement_comparison_artifact_failure_preview,
    preflight_measurement_comparison_artifact_output,
    render_measurement_comparison_artifacts,
    revalidate_measurement_comparison_artifact_source,
)
from adhelm.models import OutputPreferences

_KEY = b"m" * 32


def _ga4_comparison(tmp_path: Path) -> tuple[Ledger, MeasurementComparison]:
    ledger = Ledger(tmp_path / "data")
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(rows=((700, "ONCE_PER_EVENT"), (701, "ONCE_PER_EVENT"))),
        _ga4_snapshot(rows=((700, "ONCE_PER_SESSION"), (702, "ONCE_PER_EVENT"))),
    )
    return ledger, compare_measurements(ledger, before["record_digest"], after["record_digest"])


def _decision(
    output: Path,
    comparison: MeasurementComparison,
    *,
    formats: tuple[ArtifactFormat, ...] = ("json",),
    detail: ArtifactDetail = "standard",
    max_rows: int = 50,
    max_output_bytes: int = 65_536,
) -> ArtifactDecision:
    return explicit_measurement_comparison_artifact_decision(
        OutputPreferences(
            detail=detail,
            max_rows=max_rows,
            max_output_bytes=max_output_bytes,
        ),
        provider=comparison.scope.provider,
        profile_id=comparison.before.profile_ref,
        formats=formats,
        output_directory=str(output),
        replacement_authorized=None,
    )


def test_artifact_rebuilds_pair_redacts_every_reference_and_reuses(tmp_path: Path) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, comparison, formats=("json", "csv", "html"), detail="detailed")

    checked = preflight_measurement_comparison_artifact_output(ledger, comparison, decision)
    assert checked.model_dump(mode="json") == comparison.model_dump(mode="json")
    preview, references = measurement_comparison_artifact_failure_preview(
        decision,
        before_record_digest=checked.before.record_digest,
        after_record_digest=checked.after.record_digest,
        checkpoint_digest=checked.checkpoint_digest,
        comparison_digest=checked.comparison_digest,
    )
    assert preview["before_record_digest"] == comparison.before.record_digest
    assert len(references) == 3

    first = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)
    assert first.generation_failure_code is None
    assert [item.format for item in first.artifacts] == ["csv", "html", "json"]
    assert all(Path(item.path).is_file() for item in first.artifacts)

    json_item = next(item for item in first.artifacts if item.format == "json")
    wrapped = json.loads(Path(json_item.path).read_text())
    assert wrapped["$artifact_detail"] == {"version": 1, "detail": "detailed"}
    document = wrapped["document"]
    assert document["source_evidence"] == {
        "after_record_digest": comparison.after.record_digest,
        "before_record_digest": comparison.before.record_digest,
        "checkpoint_digest": comparison.checkpoint_digest,
        "comparison_digest": comparison.comparison_digest,
    }
    content = document["content"]
    assert content["historical_review"][
        "as_of"
    ] == comparison.after.recorded_at.isoformat().replace("+00:00", "Z")
    assert content["historical_review"]["observation_interval_start"] == (
        comparison.before.read_started_at.isoformat().replace("+00:00", "Z")
    )
    assert content["historical_review"]["observation_interval_end"] == (
        comparison.after.read_finished_at.isoformat().replace("+00:00", "Z")
    )
    assert content["historical_review"]["observed_actor"] == "UNKNOWN"
    assert content["historical_review"]["exact_change_time"] is None
    assert content["historical_review"]["fact_basis"] == "NORMALIZED_OBSERVATION_DIFFERENCE"
    assert content["historical_review"]["fingerprint_key_continuity"] == "NOT_EVIDENCED"
    assert content["historical_review"]["current_remote_state"] == (
        "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    )
    assert content["authority"] == {
        "execution_authority": False,
        "execution_command": "NOT_INSTALLED",
        "remote_mutations": False,
    }
    assert all(fact["resource_ref"].startswith("opaque:") for fact in content["facts"])
    rendered = "\n".join(Path(item.path).read_text() for item in first.artifacts)
    for raw_value in (
        "measurement.fixture",
        "ga4_admin.property.123",
        "ga4_admin.web_stream.456",
        "ga4_admin.key_event.700",
        "ga4_admin.key_event.701",
        "ga4_admin.key_event.702",
    ):
        assert raw_value not in rendered
    html = next(Path(item.path).read_text() for item in first.artifacts if item.format == "html")
    assert "<script" not in html.lower()
    assert "href=" not in html.lower()

    second = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)
    assert all(item.reused for item in second.artifacts)
    assert second.to_data()["comparison_digest"] == comparison.comparison_digest


def test_gtm_zero_fact_artifact_retains_coverage_and_continuity_caveats(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "data")
    before, after = _comparison_records(ledger, _gtm_snapshot(), _gtm_snapshot())
    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])
    output = tmp_path / "views"
    output.mkdir()
    result = render_measurement_comparison_artifacts(
        ledger,
        comparison,
        _decision(output, comparison),
        key=_KEY,
    )
    document = json.loads(Path(result.artifacts[0].path).read_text())
    content = document["content"]
    assert content["facts"] == []
    assert content["historical_review"]["fingerprint_key_continuity"] == "NOT_EVIDENCED"
    assert [item["inventory_coverage"] for item in content["source_completeness"]] == [
        "VERSION_COUNTS_AND_WORKSPACE_METADATA",
        "VERSION_COUNTS_AND_WORKSPACE_METADATA",
    ]
    assert [item["dependency_completeness"] for item in content["source_completeness"]] == [
        "NOT_PROVEN",
        "NOT_PROVEN",
    ]
    assert "gtm.container.1.2" not in Path(result.artifacts[0].path).read_text()


@pytest.mark.parametrize(
    "forged",
    [
        # ``model_dump(mode="json")`` followed by canonical_bytes would turn
        # this into integer 1.  The artifact boundary must validate raw JSON
        # lexical types before canonical source equality.
        lambda comparison: comparison.model_copy(update={"schema_version": 1.0}),
        lambda comparison: comparison.model_copy(update={"checkpoint_digest": "f" * 64}),
        lambda comparison: comparison.model_copy(
            update={
                "facts": (),
                "changed_count": 0,
                "added_count": 0,
                "observed_absent_count": 0,
            }
        ),
    ],
)
def test_forged_or_stale_typed_comparison_fails_before_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged: Callable[[MeasurementComparison], MeasurementComparison],
) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, comparison)

    monkeypatch.setattr(
        artifacts,
        "_write_versioned",
        lambda *_args, **_kwargs: pytest.fail("forged comparison must fail before writer access"),
    )
    with pytest.raises(AdhelmError) as raised:
        render_measurement_comparison_artifacts(ledger, forged(comparison), decision, key=_KEY)
    assert raised.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"
    assert list(output.iterdir()) == []

    # A second checkpoint also invalidates a formerly valid supplied comparison.
    _comparison_records(ledger, _ga4_snapshot(), _ga4_snapshot())
    with pytest.raises(AdhelmError) as stale:
        revalidate_measurement_comparison_artifact_source(ledger, comparison)
    assert stale.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"


def test_key_free_preflight_rejects_row_and_byte_bounds_before_writer(tmp_path: Path) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    rows_limited = _decision(output, comparison, max_rows=1)
    with pytest.raises(AdhelmError) as rows:
        preflight_measurement_comparison_artifact_output(ledger, comparison, rows_limited)
    assert rows.value.code == "ARTIFACT_ROW_LIMIT_EXCEEDED"
    # Direct library callers also hit that synthetic-key boundary before their
    # supplied key is examined or a writer opens the output directory.
    with pytest.raises(AdhelmError) as direct_rows:
        render_measurement_comparison_artifacts(ledger, comparison, rows_limited, key=b"short")
    assert direct_rows.value.code == "ARTIFACT_ROW_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []

    bytes_limited = _decision(output, comparison, max_output_bytes=1_024)
    with pytest.raises(AdhelmError) as bytes_failure:
        preflight_measurement_comparison_artifact_output(ledger, comparison, bytes_limited)
    assert bytes_failure.value.code == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_synthetic_hmac_preflight_reserves_the_exact_real_rendered_size(tmp_path: Path) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    baseline_output = tmp_path / "baseline"
    baseline_output.mkdir()
    baseline = render_measurement_comparison_artifacts(
        ledger,
        comparison,
        _decision(baseline_output, comparison, formats=("json", "csv", "html")),
        key=_KEY,
    )
    assert baseline.generation_failure_code is None
    maximum = max(Path(item.path).stat().st_size for item in baseline.artifacts)
    assert maximum >= 1_024

    constrained_output = tmp_path / "constrained"
    constrained_output.mkdir()
    constrained = _decision(
        constrained_output,
        comparison,
        formats=("json", "csv", "html"),
        max_output_bytes=maximum,
    )
    # The internal preflight key has a fixed-width HMAC output. If it ever
    # reserved a smaller document than a real export key, the subsequent
    # renderer would return a size failure at this exact limit.
    preflight_measurement_comparison_artifact_output(ledger, comparison, constrained)
    result = render_measurement_comparison_artifacts(ledger, comparison, constrained, key=_KEY)
    assert result.generation_failure_code is None
    assert all(Path(item.path).stat().st_size <= maximum for item in result.artifacts)


def test_partial_writer_failure_keeps_completed_references_and_hides_private_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, comparison, formats=("json", "html"))
    original = cast(Callable[..., tuple[Path, bool]], artifacts._write_versioned)
    calls = 0
    private_error = "private-comparison-artifact-write-0d5c"

    def fail_second(*args: object, **kwargs: object) -> tuple[Path, bool]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(private_error)
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)

    assert result.generation_failure_code == "LOCAL_IO_FAILED"
    assert len(result.artifacts) == 1
    data = result.to_data()
    assert data["generation_failure_code"] == "LOCAL_IO_FAILED"
    rendered_artifacts = data["artifacts"]
    assert isinstance(rendered_artifacts, list)
    assert len(rendered_artifacts) == 1
    assert private_error not in str(data)
    assert Path(result.artifacts[0].path).is_file()


def test_failure_preview_and_result_serialization_reject_forged_metadata_or_policy(
    tmp_path: Path,
) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, comparison)
    with pytest.raises(AdhelmError) as bad_preview:
        measurement_comparison_artifact_failure_preview(
            decision,
            before_record_digest="private-before",
            after_record_digest=comparison.after.record_digest,
            checkpoint_digest=comparison.checkpoint_digest,
            comparison_digest=comparison.comparison_digest,
        )
    assert bad_preview.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"
    assert "private-before" not in str(bad_preview.value)

    result = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)
    item = result.artifacts[0]
    forged_item = replace(item, source_record_digest="f" * 64)
    with pytest.raises(AdhelmError) as metadata_failure:
        replace(result, artifacts=(forged_item,)).to_data()
    assert metadata_failure.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_RESULT_INVALID"

    private_reason = "private-artifact-policy-reason-0d5c"
    forged_decision = artifacts.ArtifactDecision(
        "GENERATE",
        private_reason,
        ("json",),
        output,
        "f" * 64,
        1,
        1_024,
        True,
        False,
        "compact",
    )
    with pytest.raises(AdhelmError) as policy_failure:
        MeasurementComparisonArtifactResult(
            forged_decision,
            comparison.before.record_digest,
            comparison.after.record_digest,
            comparison.checkpoint_digest,
            comparison.comparison_digest,
            (),
        ).to_data()
    assert policy_failure.value.code == "ARTIFACT_POLICY_INVALID"
    assert private_reason not in str(policy_failure.value)


@pytest.mark.parametrize("value", [True, 1.0])
def test_request_and_source_boundary_reject_nonclosed_input_types(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(ValidationError):
        MeasurementComparisonArtifactRequest.model_validate({"schema_version": value})

    ledger, comparison = _ga4_comparison(tmp_path)
    with pytest.raises(AdhelmError) as source:
        revalidate_measurement_comparison_artifact_source(
            ledger,
            comparison.model_dump(mode="json"),  # type: ignore[arg-type]
        )
    assert source.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"

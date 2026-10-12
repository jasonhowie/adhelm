"""Independent adversarial review of retained comparison artifact rendering."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_measurement_comparison import (
    _comparison_records,
    _ga4_snapshot,
    _gtm_snapshot,
    _openai_snapshot,
)

import adhelm.measurement_comparison_artifacts as comparison_artifacts
from adhelm.artifact_detail import ArtifactDetail
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import MeasurementComparison, compare_measurements
from adhelm.measurement_comparison_artifacts import (
    MeasurementComparisonArtifactDocument,
    MeasurementComparisonArtifactRequest,
    explicit_measurement_comparison_artifact_decision,
    render_measurement_comparison_artifacts,
)
from adhelm.models import OutputPreferences

_KEY = b"r" * 32


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
    detail: ArtifactDetail = "standard",
    formats: tuple[str, ...] = ("json",),
    replacement_authorized: bool | None = None,
) -> object:
    return explicit_measurement_comparison_artifact_decision(
        OutputPreferences(detail=detail, max_output_bytes=262_144),
        provider=comparison.scope.provider,
        profile_id=comparison.before.profile_ref,
        formats=formats,  # type: ignore[arg-type]
        output_directory=str(output),
        replacement_authorized=replacement_authorized,
    )


def test_final_writer_handoff_rechecks_checkpoint_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-preflight append cannot publish a stale-checkpoint document."""
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    decision = _decision(output, comparison)
    original_preflight = comparison_artifacts._preflight_checked_document

    def append_after_preflight(checked: MeasurementComparison, resolved: object) -> None:
        original_preflight(checked, resolved)  # type: ignore[arg-type]
        _comparison_records(ledger, _openai_snapshot(), _openai_snapshot())

    monkeypatch.setattr(
        comparison_artifacts,
        "_preflight_checked_document",
        append_after_preflight,
    )
    result = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)  # type: ignore[arg-type]

    assert result.generation_failure_code == "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"
    assert result.artifacts == ()
    assert list(output.iterdir()) == []
    assert result.to_data()["generation_failure_code"] == (
        "MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID"
    )


@pytest.mark.parametrize(
    ("provider", "before_snapshot", "after_snapshot", "private_values", "continuity"),
    [
        (
            "ga4_admin",
            _ga4_snapshot(rows=((700, "ONCE_PER_EVENT"),)),
            _ga4_snapshot(rows=((700, "ONCE_PER_SESSION"),)),
            (
                "measurement.fixture",
                "ga4_admin.property.123",
                "ga4_admin.web_stream.456",
                "ga4_admin.key_event.700",
            ),
            "NOT_EVIDENCED",
        ),
        (
            "openai_ads",
            _openai_snapshot(),
            _openai_snapshot(settings=(("setting_b", 0, 1),)),
            (
                "measurement.fixture",
                "openai_ads.account.acct_fixture",
                "openai_ads.conversion_setting.setting_a",
                "openai_ads.conversion_setting.setting_b",
            ),
            "NOT_APPLICABLE",
        ),
        (
            "gtm",
            _gtm_snapshot(opaque_count=1),
            _gtm_snapshot(opaque_count=2),
            (
                "measurement.fixture",
                "gtm.account.1",
                "gtm.container.1.2",
                "gtm.workspace.1.2.3",
            ),
            "NOT_EVIDENCED",
        ),
    ],
)
def test_all_provider_artifacts_preserve_caveats_without_source_identifiers(
    tmp_path: Path,
    provider: str,
    before_snapshot: object,
    after_snapshot: object,
    private_values: tuple[str, ...],
    continuity: str,
) -> None:
    ledger = Ledger(tmp_path / "data")
    before, after = _comparison_records(  # type: ignore[arg-type]
        ledger, before_snapshot, after_snapshot
    )
    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)

    result = render_measurement_comparison_artifacts(
        ledger,
        comparison,
        _decision(output, comparison, detail="detailed", formats=("json", "csv", "html")),  # type: ignore[arg-type]
        key=_KEY,
    )

    assert result.generation_failure_code is None
    assert {item.format for item in result.artifacts} == {"json", "csv", "html"}
    rendered = {item.format: item.path.read_text() for item in result.artifacts}
    for contents in rendered.values():
        assert all(value not in contents for value in private_values)
    assert "<script" not in rendered["html"].lower()
    assert "href=" not in rendered["html"].lower()

    wrapped = json.loads(rendered["json"])
    document = wrapped["document"]
    content = document["content"]
    assert document["source_evidence"] == {
        "after_record_digest": comparison.after.record_digest,
        "before_record_digest": comparison.before.record_digest,
        "checkpoint_digest": comparison.checkpoint_digest,
        "comparison_digest": comparison.comparison_digest,
    }
    assert content["scope"]["provider"] == provider
    assert content["historical_review"]["fingerprint_key_continuity"] == continuity
    assert content["historical_review"]["fact_basis"] == "NORMALIZED_OBSERVATION_DIFFERENCE"
    assert content["authority"] == {
        "execution_authority": False,
        "execution_command": "NOT_INSTALLED",
        "remote_mutations": False,
    }
    assert all(fact["resource_ref"].startswith("opaque:") for fact in content["facts"])


def test_unapproved_replacement_versions_a_tampered_collision_without_truncating_it(
    tmp_path: Path,
) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    decision = _decision(output, comparison, replacement_authorized=False)
    first = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)  # type: ignore[arg-type]
    original = first.artifacts[0].path
    original.write_bytes(b"unapproved-existing-bytes")

    second = render_measurement_comparison_artifacts(ledger, comparison, decision, key=_KEY)  # type: ignore[arg-type]

    assert second.generation_failure_code is None
    assert len(second.artifacts) == 1
    assert original.read_bytes() == b"unapproved-existing-bytes"
    assert second.artifacts[0].path.name == original.with_suffix("").name + ".v2.json"
    assert second.artifacts[0].reused is False
    assert len(list(output.iterdir())) == 2


def test_result_serializer_blocks_forged_filename_and_hides_failure_text(tmp_path: Path) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    result = render_measurement_comparison_artifacts(
        ledger,
        comparison,
        _decision(output, comparison),
        key=_KEY,  # type: ignore[arg-type]
    )
    item = result.artifacts[0]
    private_failure = "private-comparison-artifact-failure-sentinel"
    safe = replace(result, generation_failure_code=private_failure).to_data()
    assert safe["generation_failure_code"] == "LOCAL_ARTIFACT_FAILURE"
    assert private_failure not in json.dumps(safe)

    forged = replace(
        result,
        artifacts=(
            replace(
                item,
                path=output / ("artifact-" + "f" * 16 + "-" + "e" * 16 + ".json"),
            ),
        ),
    )
    with pytest.raises(AdhelmError) as raised:
        forged.to_data()
    assert raised.value.code == "MEASUREMENT_COMPARISON_ARTIFACT_RESULT_INVALID"


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, "true"])
def test_request_replacement_authorization_requires_a_json_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        MeasurementComparisonArtifactRequest.model_validate({"replacement_authorized": value})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("schema_version", 1.0),
        lambda value: value["content"]["authority"].__setitem__("execution_authority", 0),
        lambda value: value["content"]["authority"].__setitem__("remote_mutations", 0.0),
        lambda value: value["content"]["historical_review"].__setitem__("static_artifact", 1),
        lambda value: value["content"]["historical_review"].__setitem__(
            "opening_or_editing_authorizes_execution", 0.0
        ),
    ],
)
def test_artifact_document_rejects_raw_numeric_control_bypasses(
    tmp_path: Path, mutation: object
) -> None:
    ledger, comparison = _ga4_comparison(tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    result = render_measurement_comparison_artifacts(
        ledger,
        comparison,
        _decision(output, comparison, detail="standard"),
        key=_KEY,  # type: ignore[arg-type]
    )
    document = json.loads(result.artifacts[0].path.read_text())
    altered = deepcopy(document)
    mutation(altered)  # type: ignore[operator]
    with pytest.raises(ValidationError):
        MeasurementComparisonArtifactDocument.model_validate_json(json.dumps(altered))

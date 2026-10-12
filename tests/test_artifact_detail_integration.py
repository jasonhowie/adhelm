import json
from pathlib import Path

import pytest
from test_artifact_detail import _restore_compact
from test_artifacts import _context, _report

from adhelm.artifacts import (
    ArtifactTaskOverride,
    RetainedArtifactInput,
    generate_artifacts,
    resolve_artifact_policy,
)
from adhelm.models import OutputPreferences
from adhelm.reporting import NormalizedReport


@pytest.mark.parametrize("format_name", ["json", "csv", "html"])
def test_saved_detail_changes_presentation_without_changing_evidence_or_review_facts(
    tmp_path: Path, format_name: str
) -> None:
    original = _report(quality=["PROCESSING_PENDING"])
    report = NormalizedReport(
        envelope=original.envelope,
        performance_rows=[
            original.performance_rows[0].model_copy(update={"resource_ref": f"campaign:{index}"})
            for index in range(3)
        ],
    )
    retained = RetainedArtifactInput(report, "a" * 64, "b" * 64)
    inputs = []
    settings = []
    contents = {}
    for detail in ("compact", "standard", "detailed"):
        preferences = OutputPreferences.model_validate(
            {"detail": detail, "max_output_bytes": 1_048_576}
        )
        decision = resolve_artifact_policy(
            _context(),
            preferences,
            task_override=ArtifactTaskOverride(
                formats=(format_name,),  # type: ignore[arg-type]
                output_directory=str(tmp_path),
            ),
        )
        metadata = generate_artifacts(retained, decision, key=b"k" * 32)[0]
        inputs.append(metadata.input_digest)
        settings.append(metadata.settings_digest)
        contents[detail] = metadata.path.read_bytes()
        for fact in ("PROCESSING_PENDING", "USD", "CAMPAIGN", "a" * 64, "b" * 64):
            assert fact.encode() in contents[detail]
        assert b"opaque:" in contents[detail]
        assert b"campaign:0" not in contents[detail]
    assert len(set(inputs)) == 1
    assert len(set(settings)) == 3
    assert len(set(contents.values())) == 3
    if format_name == "json":
        standard = json.loads(contents["standard"])
        assert _restore_compact(json.loads(contents["compact"])) == standard
        assert json.loads(contents["detailed"])["document"] == standard
        assert len(contents["compact"]) < len(contents["standard"])

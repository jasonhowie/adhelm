from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_saved_reports import retain

from adhelm import workflows
from adhelm.artifact_workflows import (
    finalize_report_artifacts,
    preflight_report_artifacts,
)
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, Configuration, OutputPreferences

_KEY = b"a" * 32


def _preferences(output: Path, *, stage: str = "completion") -> OutputPreferences:
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=["result"],
        output_directory=str(output),
        workflow_rules=[
            ArtifactRule(
                workflow="report",
                stage=stage,
                providers=["google_ads"],
                profiles=["reader"],
                formats=["json"],
            )
        ],
    )


def _stored(ledger: Ledger, record_digest: str) -> dict[str, object]:
    return next(
        record
        for record in ledger.records("performance")
        if record["record_digest"] == record_digest
    )


def test_selected_rule_preflight_rejects_missing_destination_without_platform_access() -> None:
    missing = OutputPreferences(
        artifact_mode="RULES",
        purposes=["result"],
        workflow_rules=[
            ArtifactRule(
                workflow="report",
                stage="completion",
                providers=["google_ads"],
                profiles=["reader"],
                formats=["json"],
            )
        ],
    )
    with pytest.raises(AdhelmError, match="local configuration"):
        preflight_report_artifacts(
            missing, provider="google_ads", profile_id="reader", ephemeral=False
        )


def test_selected_rule_preflight_rejects_ephemeral_and_missing_destination(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError, match="verified retained"):
        preflight_report_artifacts(
            _preferences(tmp_path, stage="completion"),
            provider="google_ads",
            profile_id="reader",
            ephemeral=True,
        )
    missing = OutputPreferences(
        artifact_mode="RULES",
        purposes=["result"],
        workflow_rules=[
            ArtifactRule(
                workflow="report",
                stage="completion",
                providers=["google_ads"],
                profiles=["reader"],
                formats=["json"],
            )
        ],
    )
    with pytest.raises(AdhelmError, match="local configuration"):
        preflight_report_artifacts(
            missing, provider="google_ads", profile_id="reader", ephemeral=False
        )


def test_run_report_preflights_rule_before_input_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> str:
        pytest.fail("Automatic artifact policy must fail before credential access")

    monkeypatch.setattr(workflows, "credential", forbidden)
    save_configuration(
        tmp_path,
        Configuration(
            output=OutputPreferences(
                artifact_mode="RULES",
                purposes=["result"],
                workflow_rules=[
                    ArtifactRule(
                        workflow="report",
                        stage="completion",
                        providers=["google_ads"],
                        profiles=["reader"],
                        formats=["json"],
                    )
                ],
            )
        ),
    )
    with pytest.raises(AdhelmError, match="local configuration"):
        workflows.run_report(
            tmp_path,
            "google_ads",
            "reader",
            tmp_path / "input-is-never-read.json",
        )


def test_meaningful_change_reuses_actual_prior_retained_artifact(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    ledger = Ledger(tmp_path)
    preflight = preflight_report_artifacts(
        _preferences(output, stage="meaningful_change"),
        provider="google_ads",
        profile_id="reader",
        ephemeral=False,
    )
    first_digest = retain(ledger, "a")
    first = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, first_digest),
        preflight=preflight,
        key=_KEY,
    )
    assert first.effective_decision.reason == "MATCHING_SAVED_RULE"
    assert len(first.artifacts) == 1

    second_digest = retain(ledger, "a")
    second = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, second_digest),
        preflight=preflight,
        key=_KEY,
    )
    assert second.reused_prior is True
    assert second.source_record_digest == first_digest
    assert second.source_record_digest != second_digest
    assert len(list(output.iterdir())) == 1


def test_invalid_derived_cache_fails_closed_without_mutating_report_history(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    ledger = Ledger(tmp_path)
    preflight = preflight_report_artifacts(
        _preferences(output), provider="google_ads", profile_id="reader", ephemeral=False
    )
    record_digest = retain(ledger, "a")
    cache = tmp_path / "artifact-cache"
    cache.mkdir(mode=0o700)
    cache.chmod(0o700)
    cache_file = cache / "report-artifacts.json"
    cache_file.write_text(json.dumps({"schema_version": 99, "entries": []}))
    cache_file.chmod(0o600)
    result = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, record_digest),
        preflight=preflight,
        key=_KEY,
    )
    assert result.generation_failure_code == "ARTIFACT_CACHE_INVALID"
    assert result.source_record_digest == record_digest
    assert len(ledger.records("performance")) == 2


def test_tampered_cached_file_is_not_returned_as_reused_artifact(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    ledger = Ledger(tmp_path)
    preflight = preflight_report_artifacts(
        _preferences(output, stage="meaningful_change"),
        provider="google_ads",
        profile_id="reader",
        ephemeral=False,
    )
    first_digest = retain(ledger, "a")
    first = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, first_digest),
        preflight=preflight,
        key=_KEY,
    )
    first.artifacts[0].path.write_text("tampered")
    second_digest = retain(ledger, "a")
    second = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, second_digest),
        preflight=preflight,
        key=_KEY,
    )
    assert second.artifacts == ()
    assert second.reused_prior is False
    assert second.source_record_digest == second_digest
    assert second.generation_failure_code == "ARTIFACT_CACHE_INVALID"


def test_regenerate_and_new_destination_do_not_reuse_cached_output(tmp_path: Path) -> None:
    output_a = tmp_path / "views-a"
    output_b = tmp_path / "views-b"
    output_a.mkdir()
    output_b.mkdir()
    ledger = Ledger(tmp_path)
    first_preferences = _preferences(output_a)
    first_preflight = preflight_report_artifacts(
        first_preferences, provider="google_ads", profile_id="reader", ephemeral=False
    )
    first_digest = retain(ledger, "a")
    finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, first_digest),
        preflight=first_preflight,
        key=_KEY,
    )
    second_preferences = OutputPreferences.model_validate(
        {**_preferences(output_b).model_dump(), "regeneration": "REGENERATE"}
    )
    second_preflight = preflight_report_artifacts(
        second_preferences, provider="google_ads", profile_id="reader", ephemeral=False
    )
    second_digest = retain(ledger, "a")
    second = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, second_digest),
        preflight=second_preflight,
        key=_KEY,
    )
    assert second.reused_prior is False
    assert second.source_record_digest == second_digest
    assert len(list(output_a.iterdir())) == 1
    assert len(list(output_b.iterdir())) == 1


def test_post_append_render_limit_failure_returns_retained_source(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    ledger = Ledger(tmp_path)
    preferences = OutputPreferences.model_validate(
        {**_preferences(output).model_dump(), "max_rows": 1}
    )
    preflight = preflight_report_artifacts(
        preferences, provider="google_ads", profile_id="reader", ephemeral=False
    )
    record_digest = retain(ledger, "a")
    result = finalize_report_artifacts(
        tmp_path,
        ledger,
        stored_record=_stored(ledger, record_digest),
        preflight=preflight,
        key=_KEY,
    )
    assert result.generation_failure_code == "ARTIFACT_ROW_LIMIT_EXCEEDED"
    assert result.source_record_digest == record_digest
    assert result.checkpoint_digest is not None
    assert list(output.iterdir()) == []

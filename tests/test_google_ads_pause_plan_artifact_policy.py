"""Saved-rule and one-task artifact policy boundaries for Google Ads PAUSE history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_google_ads_pause_plan_artifact_cli import (
    _artifact_request,
    _data,
    _invoke,
    _retain,
    _widen_output,
)

import adhelm.google_ads_pause_plan_artifacts as plan_artifacts
from adhelm import artifacts, credentials
from adhelm.artifacts import ArtifactTaskOverride
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_google_ads_pause_plans import load_google_ads_campaign_pause_plan

_PROFILE = "google-ads-edit"


def _rule_preferences(
    output: Path,
    *,
    formats: list[str] | None = None,
    providers: list[str] | None = None,
    profiles: list[str] | None = None,
    stage: str = "before_approval",
    purposes: list[str] | None = None,
    directory: str | None = "",
) -> OutputPreferences:
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=purposes if purposes is not None else ["plan_review"],
        output_directory=str(output) if directory == "" else directory,
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage=stage,
                providers=providers if providers is not None else ["google_ads"],
                profiles=profiles if profiles is not None else [_PROFILE],
                formats=formats if formats is not None else ["json"],
            )
        ],
        max_output_bytes=131_072,
    )


def test_off_and_on_request_modes_stay_suppressed_and_file_free(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    for mode, reason in (
        ("OFF", "ARTIFACT_MODE_OFF"),
        ("ON_REQUEST", "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST"),
    ):
        decision = plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
            OutputPreferences(artifact_mode=mode, output_directory=str(output)),
            profile_id=_PROFILE,
        )
        assert decision.outcome == "SUPPRESSED"
        assert decision.reason == reason
        assert decision.formats == ()
        assert decision.output_directory is None
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("keywords", "reason"),
    [
        ({"providers": ["meta_ads"]}, "NO_MATCHING_WORKFLOW_RULE"),
        ({"profiles": ["another-google-profile"]}, "NO_MATCHING_WORKFLOW_RULE"),
        ({"stage": "completion"}, "NO_MATCHING_WORKFLOW_RULE"),
        ({"purposes": ["audit"]}, "PURPOSE_NOT_ENABLED"),
    ],
)
def test_rule_precedence_suppresses_a_nonmatching_selection(
    tmp_path: Path, keywords: dict[str, Any], reason: str
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    decision = plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
        _rule_preferences(output, **keywords),
        profile_id=_PROFILE,
    )
    assert decision.outcome == "SUPPRESSED"
    assert decision.reason == reason
    assert list(output.iterdir()) == []


def test_matching_rule_generates_and_a_task_override_takes_precedence(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    other = tmp_path / "task-views"
    other.mkdir()
    preferences = _rule_preferences(output, formats=["csv", "json"])

    saved = plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
        preferences, profile_id=_PROFILE
    )
    assert saved.outcome == "GENERATE"
    assert saved.reason == "MATCHING_SAVED_RULE"
    assert saved.formats == ("csv", "json")
    assert saved.output_directory == output

    overridden = plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
        preferences,
        profile_id=_PROFILE,
        task_override=ArtifactTaskOverride(
            requested=True,
            formats=("html",),
            output_directory=str(other),
            purpose="plan_review",
        ),
    )
    assert overridden.outcome == "GENERATE"
    assert overridden.reason == "EXPLICIT_USER_REQUEST"
    assert overridden.formats == ("html",)
    assert overridden.output_directory == other

    disabled = plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
        preferences,
        profile_id=_PROFILE,
        task_override=ArtifactTaskOverride(requested=False, purpose="plan_review"),
    )
    assert disabled.outcome == "SUPPRESSED"
    assert disabled.reason == "TASK_OVERRIDE_DISABLED"
    assert list(output.iterdir()) == list(other.iterdir()) == []


def test_matching_rule_without_a_destination_requires_configuration_first(
    tmp_path: Path,
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    with pytest.raises(AdhelmError) as raised:
        plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
            _rule_preferences(output, directory=None),
            profile_id=_PROFILE,
        )
    assert raised.value.code == "ARTIFACT_CONFIGURATION_REQUIRED"
    assert list(output.iterdir()) == []


def test_unsupported_format_and_invalid_context_are_refused(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    with pytest.raises(AdhelmError) as unsupported:
        plan_artifacts.explicit_google_ads_campaign_pause_plan_artifact_decision(
            OutputPreferences(artifact_mode="OFF"),
            profile_id=_PROFILE,
            formats=cast(Any, ("pdf",)),
            output_directory=str(output),
            replacement_authorized=None,
        )
    assert unsupported.value.code == "ARTIFACT_POLICY_INVALID"

    with pytest.raises(AdhelmError) as context:
        plan_artifacts.preflight_google_ads_campaign_pause_plan_artifacts(
            OutputPreferences(artifact_mode="OFF"),
            profile_id="google/../edit",
        )
    assert context.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_CONTEXT"
    assert list(output.iterdir()) == []


def test_explicit_request_uses_the_saved_destination_when_none_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "saved-views"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(tmp_path)
    save_configuration(
        tmp_path,
        configuration.model_copy(
            update={
                "output": OutputPreferences(
                    artifact_mode="OFF",
                    output_directory=str(destination),
                    max_output_bytes=131_072,
                )
            }
        ),
        replace=True,
    )
    request = tmp_path / "saved-destination-request.json"
    request.write_text(json.dumps({"schema_version": 1, "formats": ["json"]}), encoding="utf-8")
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)

    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code == 0, result.output
    produced = _data(result)["data"]["artifact_result"]["artifacts"]
    assert len(produced) == 1
    assert Path(produced[0]["path"]).parent == destination


def test_unchanged_inputs_reuse_identical_bytes_without_regenerating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "views"
    destination.mkdir(mode=0o700)
    _widen_output(tmp_path)
    request = _artifact_request(tmp_path, destination, formats=["json"])
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)

    first = _invoke(tmp_path, record_digest, "--input", str(request))
    assert first.exit_code == 0, first.output
    original = _data(first)["data"]["artifact_result"]["artifacts"][0]
    assert original["reused"] is False
    contents = Path(original["path"]).read_bytes()

    second = _invoke(tmp_path, record_digest, "--input", str(request))
    assert second.exit_code == 0, second.output
    repeated = _data(second)["data"]["artifact_result"]["artifacts"][0]

    assert repeated["reused"] is True
    for field in ("path", "artifact_digest", "input_digest", "settings_digest"):
        assert repeated[field] == original[field]
    assert Path(repeated["path"]).read_bytes() == contents
    assert len(list(destination.iterdir())) == 1


def test_partial_writer_failure_reports_only_completed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "views"
    destination.mkdir(mode=0o700)
    _widen_output(tmp_path)
    request = _artifact_request(tmp_path, destination, formats=["html", "json"])
    original_write = artifacts._write_versioned
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private writer failure sentinel")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)
    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code == 5, result.output
    envelope = _data(result)
    artifact_result = cast(dict[str, Any], envelope["data"]["artifact_result"])
    assert envelope["outcome"] == "PARTIAL"
    assert artifact_result["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 1
    assert artifact_result["artifacts"][0]["format"] == "html"
    assert Path(artifact_result["artifacts"][0]["path"]).is_file()
    assert len(list(destination.iterdir())) == 1
    assert "private writer failure sentinel" not in json.dumps(envelope)
    action, provenance = load_google_ads_campaign_pause_plan(Ledger(tmp_path), record_digest)
    assert provenance["record_digest"] == record_digest
    assert action.execution_authority is False


def test_unavailable_sharing_key_leaves_retained_history_and_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest = _retain(tmp_path)
    destination = tmp_path / "views"
    destination.mkdir(mode=0o700)
    _widen_output(tmp_path)
    request = _artifact_request(tmp_path, destination, formats=["json"])
    checkpoint = Ledger(tmp_path).checkpoint()["checkpoint_digest"]

    def unavailable_key() -> bytes:
        raise AdhelmError("EXPORT_KEY_UNAVAILABLE", "Private export key is unavailable.")

    monkeypatch.setattr(credentials, "export_key", unavailable_key)
    result = _invoke(tmp_path, record_digest, "--input", str(request))

    envelope = _data(result)
    assert result.exit_code != 0, result.output
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "EXPORT_KEY_UNAVAILABLE"
    assert envelope["data"] == {}
    for private in (_PROFILE, "credential-binding:sha256:", "0987654321"):
        assert private not in result.output
    assert list(destination.iterdir()) == []
    assert Ledger(tmp_path).checkpoint()["checkpoint_digest"] == checkpoint

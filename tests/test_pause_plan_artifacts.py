"""Hermetic regressions for retained static OpenAI pause-plan artifacts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_openai_pause import NOW, _credentials, _Scenario
from test_openai_pause import _action as fixture_action
from typer.testing import CliRunner

import adhelm.artifacts as artifacts
import adhelm.pause_plan_artifacts as pause_plan_artifacts
import adhelm.plan_cli as plan_cli
import adhelm.saved_plans as saved_plans
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, Configuration, OpenAIAccountKeyProfile, OutputPreferences
from adhelm.pause_plan_artifacts import (
    PausePlanArtifactResult,
    explicit_openai_pause_plan_artifact_decision,
    pause_plan_artifact_failure_preview,
    render_openai_pause_plan_artifacts,
)
from adhelm.pause_workflow import OpenAIPauseRequest
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction, build_openai_pause_plan
from adhelm.saved_plans import load_openai_pause_plan, retain_openai_pause_plan

_KEY = b"p" * 32
_OUTCOME = "PLAN_REVIEW_NO_EXECUTION_AUTHORITY"
runner = CliRunner()


def _profile() -> OpenAIAccountKeyProfile:
    return OpenAIAccountKeyProfile.model_validate(
        {
            "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
            "profile_id": "pause-artifact-edit",
            "purpose": "EDIT",
            "account_ref": "account_fixture",
            "currency": "USD",
            "timezone": "America/Chicago",
            "credential_ref": "pause-artifact-provider-key",
        }
    )


def _request() -> OpenAIPauseRequest:
    return OpenAIPauseRequest.model_validate(
        {"profile_id": "pause-artifact-edit", "campaign_id": "campaign_fixture"}
    )


def _write(path: Path, value: object) -> str:
    path.write_text(json.dumps(value))
    return str(path)


def _pause_action(
    *, expires_at: datetime | None = None, planning_now: datetime = NOW
) -> OpenAIPausePlannedAction:
    legacy, _ = fixture_action(
        _Scenario(),
        _credentials(profile="pause-artifact-credential-profile"),
        expires_at=expires_at or NOW + timedelta(minutes=10),
        planning_now=planning_now,
    )
    return build_openai_pause_plan(
        legacy.binding,
        expires_at=legacy.plan.expires_at,
        workspace_profile_ref="default",
    )


def _retain(root: Path, action: OpenAIPausePlannedAction | None = None) -> str:
    return retain_openai_pause_plan(Ledger(root), action or _pause_action())["record_digest"]


def _artifact_request(output: Path, formats: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "formats": formats or ["json", "csv", "html"],
        "output_directory": str(output),
    }


def _automatic_configuration(output: Path, *, formats: list[str] | None = None) -> Configuration:
    return Configuration(
        profiles=[_profile()],
        output=OutputPreferences(
            artifact_mode="RULES",
            purposes=["plan_review"],
            output_directory=str(output),
            detail="standard",
            workflow_rules=[
                ArtifactRule(
                    workflow="plan",
                    stage="before_approval",
                    providers=["openai_ads"],
                    profiles=["pause-artifact-edit"],
                    formats=formats or ["json"],
                )
            ],
        ),
    )


def test_explicit_plan_artifact_is_reusable_redacted_static_and_lossless(
    tmp_path: Path, monkeypatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(
        root,
        Configuration(output=OutputPreferences(artifact_mode="OFF", detail="standard")),
    )
    selected = _retain(root)
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)
    request = _write(tmp_path / "artifact.json", _artifact_request(output))
    command = ["--data-dir", str(root), "artifacts", "plan", selected, "--input", request]

    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.stdout
    first_body = json.loads(first.stdout)
    assert load_configuration(root).output.artifact_mode == "OFF"
    assert first_body["outcome"] == _OUTCOME
    assert first_body["data"]["execution_authority"] is False
    assert first_body["data"]["opening_or_editing_authorizes_execution"] is False
    listed = first_body["data"]["artifact_result"]["artifacts"]
    assert {item["format"] for item in listed} == {"json", "csv", "html"}
    assert first_body["artifact_refs"] == [
        "artifact:sha256:" + item["artifact_digest"] for item in listed
    ]

    json_path = next(Path(item["path"]) for item in listed if item["format"] == "json")
    document = json.loads(json_path.read_text())
    content = document["content"]
    loaded, provenance = load_openai_pause_plan(Ledger(root), selected)
    assert content["provenance"]["record_digest"] == selected
    assert content["provenance"]["action_digest"] == provenance["action_digest"]
    assert content["review"]["plan_digest"] == loaded.plan.plan_digest
    assert content["review"]["subplan_digests"] == list(loaded.plan.subplan_digests)
    assert content["review"]["before"]["bidding_type"] == loaded.binding.campaign.bidding_type
    assert (
        content["review"]["before"]["lifetime_spend_limit_micros"]
        == loaded.binding.campaign.lifetime_spend_limit_micros
    )
    assert content["review"]["before"]["start_at"] == (
        loaded.binding.campaign.start_at.isoformat().replace("+00:00", "Z")
    )
    assert content["review"]["after"]["end_at"] == (
        loaded.binding.campaign.end_at.isoformat().replace("+00:00", "Z")
        if loaded.binding.campaign.end_at
        else None
    )
    assert content["review"]["maximum_incremental_spend"] == {"minor_units": 0, "currency": "USD"}
    assert content["review"]["review_authorizes_execution"] is False
    assert content["authority"]["execution_authority"] is False
    assert content["recovery_limits"] == {
        "allowed_recovery_actions": ["PAUSE"],
        "ambiguity_status": "UNRESOLVED_NO_BLIND_RETRY",
        "blind_retry_permitted": False,
        "requires_current_kernel_checks": True,
    }
    assert content["historical_review"]["as_of"] == provenance["recorded_at"]
    assert content["historical_review"]["current_validity"] == "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    assert content["historical_review"]["static_artifact"] is True
    rendered = "\n".join(Path(item["path"]).read_text() for item in listed)
    for private in (
        loaded.binding.account_ref,
        loaded.binding.account_client_ref,
        loaded.binding.campaign.campaign_ref,
        loaded.binding.campaign.campaign_client_ref,
        loaded.binding.profile_ref,
        loaded.binding.credential_binding_ref,
        "fixture-key-not-in-results",
    ):
        assert private not in rendered
    assert "opaque:" in rendered
    html = next(Path(item["path"]).read_text() for item in listed if item["format"] == "html")
    assert "<script" not in html.lower() and "href=" not in html.lower()

    second = runner.invoke(app, command)
    assert second.exit_code == 0, second.stdout
    second_artifacts = json.loads(second.stdout)["data"]["artifact_result"]["artifacts"]
    assert all(item["reused"] for item in second_artifacts)
    json_path.write_text("tampered")
    third = runner.invoke(app, command)
    assert third.exit_code == 0, third.stdout
    renewed = next(
        item
        for item in json.loads(third.stdout)["data"]["artifact_result"]["artifacts"]
        if item["format"] == "json"
    )
    assert renewed["reused"] is False
    assert Path(renewed["path"]).name.endswith(".v2.json")


def test_non_boolean_one_request_replacement_cannot_authorize_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "views"
    output.mkdir()
    with pytest.raises(AdhelmError) as failure:
        explicit_openai_pause_plan_artifact_decision(
            OutputPreferences(detail="standard"),
            profile_id="pause-artifact-edit",
            formats=("json",),
            output_directory=str(output),
            replacement_authorized="yes",  # type: ignore[arg-type]
        )
    assert failure.value.code == "INVALID_ARTIFACT_OVERRIDE"
    assert list(output.iterdir()) == []


def test_forged_artifact_decision_cannot_escape_a_writer_or_leak_its_reason(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    action = _pause_action()
    record_digest = _retain(tmp_path, action)
    loaded, provenance = load_openai_pause_plan(Ledger(tmp_path), record_digest)
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id=loaded.binding.profile_ref,
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )
    private_reason = "private-forged-artifact-decision-reason-03ef20"

    def unexpected_writer(*_args: object, **_kwargs: object) -> tuple[Path, bool]:
        pytest.fail("forged artifact decision must fail before writer access")

    monkeypatch.setattr(artifacts, "_write_versioned", unexpected_writer)
    for forged in (
        replace(decision, preferences_digest="../outside"),
        replace(decision, reason=private_reason),
    ):
        with pytest.raises(AdhelmError) as failure:
            render_openai_pause_plan_artifacts(loaded, provenance, forged, key=_KEY)
        assert failure.value.code == "ARTIFACT_POLICY_INVALID"
        assert private_reason not in str(failure.value)
    assert list(output.iterdir()) == []


def test_pause_plan_artifact_preflight_rejects_unverified_source_references(
    tmp_path: Path,
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id="pause-artifact-edit",
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )
    private_record = "private-source-record-sentinel-03ef20"
    private_checkpoint = "private-checkpoint-sentinel-03ef20"
    with pytest.raises(AdhelmError) as failure:
        pause_plan_artifact_failure_preview(
            decision,
            source_record_digest=private_record,
            checkpoint_digest=private_checkpoint,
        )
    assert failure.value.code == "PLAN_ARTIFACT_SOURCE_INVALID"
    assert private_record not in str(failure.value)
    assert private_checkpoint not in str(failure.value)
    assert list(output.iterdir()) == []


def test_direct_pause_plan_artifact_result_cannot_serialize_a_forged_reason(
    tmp_path: Path,
) -> None:
    private_reason = "private-direct-plan-artifact-result-reason-03ef20"
    forged = artifacts.ArtifactDecision(
        "GENERATE",
        private_reason,
        ("json",),
        tmp_path,
        "f" * 64,
        1,
        1024,
        True,
        False,
        "compact",
    )
    result = PausePlanArtifactResult(forged, "e" * 64, "d" * 64, ())
    with pytest.raises(AdhelmError) as failure:
        result.to_data()
    assert failure.value.code == "ARTIFACT_POLICY_INVALID"
    assert private_reason not in str(failure.value)


def test_direct_pause_plan_artifact_result_binds_metadata_path_to_its_digests(
    tmp_path: Path,
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id="pause-artifact-edit",
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )
    forged = artifacts.ArtifactMetadata(
        path=output / ("artifact-" + "a" * 16 + "-" + "b" * 16 + ".json"),
        format="json",
        reused=False,
        artifact_digest="a" * 64,
        input_digest="d" * 64,
        settings_digest="e" * 64,
        source_record_digest="e" * 64,
        checkpoint_digest="d" * 64,
    )
    result = PausePlanArtifactResult(decision, "e" * 64, "d" * 64, (forged,))
    with pytest.raises(AdhelmError) as failure:
        result.to_data()
    assert failure.value.code == "PLAN_ARTIFACT_RESULT_INVALID"
    assert "artifact-" not in str(failure.value)


def test_direct_pause_plan_artifact_result_requires_a_complete_consistent_success(
    tmp_path: Path,
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id="pause-artifact-edit",
        formats=("json", "csv"),
        output_directory=str(output),
        replacement_authorized=None,
    )

    def metadata(
        format_name: str, input_digest: str, settings_digest: str
    ) -> artifacts.ArtifactMetadata:
        return artifacts.ArtifactMetadata(
            path=output / f"artifact-{input_digest[:16]}-{settings_digest[:16]}.{format_name}",
            format=format_name,  # type: ignore[arg-type]
            reused=False,
            artifact_digest="a" * 64,
            input_digest=input_digest,
            settings_digest=settings_digest,
            source_record_digest="e" * 64,
            checkpoint_digest="d" * 64,
        )

    first = metadata(decision.formats[0], "b" * 64, "c" * 64)
    with pytest.raises(AdhelmError) as failure:
        PausePlanArtifactResult(decision, "e" * 64, "d" * 64, (first,)).to_data()
    assert failure.value.code == "PLAN_ARTIFACT_RESULT_INVALID"

    second = metadata(decision.formats[1], "f" * 64, "0" * 64)
    with pytest.raises(AdhelmError) as failure:
        PausePlanArtifactResult(decision, "e" * 64, "d" * 64, (first, second)).to_data()
    assert failure.value.code == "PLAN_ARTIFACT_RESULT_INVALID"


def test_expired_plan_artifact_stays_historical_and_never_claims_current_validity(
    tmp_path: Path, monkeypatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(root, Configuration(output=OutputPreferences(detail="standard")))
    old = datetime(2020, 1, 1, tzinfo=UTC)
    selected = _retain(root, _pause_action(expires_at=old + timedelta(seconds=1), planning_now=old))
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)
    request = _write(tmp_path / "artifact.json", _artifact_request(output, ["json"]))
    result = runner.invoke(
        app, ["--data-dir", str(root), "artifacts", "plan", selected, "--input", request]
    )
    assert result.exit_code == 0, result.stdout
    item = json.loads(result.stdout)["data"]["artifact_result"]["artifacts"][0]
    content = json.loads(Path(item["path"]).read_text())["content"]
    assert content["review"]["expired_at_review"] is True
    assert content["historical_review"]["expired_at_as_of"] is True
    assert content["historical_review"]["current_validity"] == "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    assert content["authority"]["execution_authority"] is False


def test_explicit_plan_artifact_preflights_worst_partial_envelope_before_key_or_files(
    tmp_path: Path, monkeypatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(root, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    selected = _retain(root)
    request = _write(tmp_path / "artifact.json", _artifact_request(output))

    def forbidden_key() -> bytes:
        pytest.fail("artifact result preflight must precede export-key access")

    monkeypatch.setattr(adhelm.credentials, "export_key", forbidden_key)
    result = runner.invoke(
        app, ["--data-dir", str(root), "artifacts", "plan", selected, "--input", request]
    )
    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_pause_plan_artifact_request_rejects_boolean_schema_version_before_history_or_key(
    tmp_path: Path, monkeypatch
) -> None:
    import adhelm.credentials

    output = tmp_path / "views"
    output.mkdir()
    malformed = _write(
        tmp_path / "artifact.json",
        {
            "schema_version": True,
            "formats": ["json"],
            "output_directory": str(output),
        },
    )

    def forbidden_key() -> bytes:
        pytest.fail("invalid artifact request must not access the export key")

    monkeypatch.setattr(adhelm.credentials, "export_key", forbidden_key)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "missing-data"),
            "artifacts",
            "plan",
            "f" * 64,
            "--input",
            malformed,
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert list(output.iterdir()) == []
    assert not (tmp_path / "missing-data").exists()


def test_pause_plan_artifact_rejects_control_character_destination_before_export_key(
    tmp_path: Path, monkeypatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    selected = _retain(root)
    malformed = _write(
        tmp_path / "artifact.json",
        {
            "schema_version": 1,
            "formats": ["json"],
            "output_directory": str(tmp_path / "views") + "\x00hidden",
        },
    )

    def forbidden_key() -> bytes:
        pytest.fail("unsafe artifact destination must not access the export key")

    monkeypatch.setattr(adhelm.credentials, "export_key", forbidden_key)
    result = runner.invoke(
        app,
        ["--data-dir", str(root), "artifacts", "plan", selected, "--input", malformed],
    )
    assert result.exit_code == 3, result.stdout
    body = json.loads(result.stdout)
    assert body["diagnostics"][0]["code"] == "INVALID_ARTIFACT_DESTINATION"
    assert "hidden" not in result.stdout


def test_artifacts_plan_revalidates_provenance_before_preflight_output_or_key(
    tmp_path: Path, monkeypatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    selected = _retain(root)
    action, provenance = load_openai_pause_plan(Ledger(root), selected)
    private = "private-forged-artifact-provenance-sentinel-03ef20"

    class _PrivateCheckpoint:
        def __repr__(self) -> str:
            return private

    forged: dict[str, object] = {**provenance, "checkpoint_digest": _PrivateCheckpoint()}
    monkeypatch.setattr(saved_plans, "load_openai_pause_plan", lambda *_args: (action, forged))
    monkeypatch.setattr(
        adhelm.credentials,
        "export_key",
        lambda: pytest.fail("invalid retained source must not access the export key"),
    )
    request = _write(tmp_path / "artifact.json", _artifact_request(output, ["json"]))
    result = runner.invoke(
        app, ["--data-dir", str(root), "artifacts", "plan", selected, "--input", request]
    )
    assert result.exit_code == 2, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "PLAN_ARTIFACT_SOURCE_INVALID"
    assert private not in result.stdout
    assert list(output.iterdir()) == []


def test_plan_rule_preflight_blocks_missing_destination_before_pause_preparation(
    tmp_path: Path, monkeypatch
) -> None:
    configuration = Configuration(
        profiles=[_profile()],
        output=OutputPreferences(
            artifact_mode="RULES",
            purposes=["plan_review"],
            workflow_rules=[
                ArtifactRule(
                    workflow="plan",
                    stage="before_approval",
                    providers=["openai_ads"],
                    profiles=["pause-artifact-edit"],
                    formats=["json"],
                )
            ],
        ),
    )
    save_configuration(tmp_path, configuration)
    request = _write(tmp_path / "request.json", _request().model_dump(mode="json"))

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("matching artifact configuration must be resolved before pause preparation")

    monkeypatch.setattr(plan_cli, "prepare_openai_pause", unexpected)
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", request]
    )
    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "ARTIFACT_CONFIGURATION_REQUIRED"
    assert not (tmp_path / "ledger").exists()


def test_saved_plan_rule_creates_artifact_after_retention_and_default_on_request_creates_none(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    configuration = _automatic_configuration(output)
    save_configuration(tmp_path, configuration)
    action = _pause_action()
    request = _write(tmp_path / "request.json", _request().model_dump(mode="json"))
    monkeypatch.setattr(plan_cli, "prepare_openai_pause", lambda *_args: (action, configuration))
    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW)
    monkeypatch.setattr(plan_cli, "export_key", lambda: _KEY)
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", request]
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["outcome"] == _OUTCOME
    artifact_result = body["data"]["artifact_result"]
    assert artifact_result["effective_policy"]["reason"] == "MATCHING_SAVED_RULE"
    assert artifact_result["generation_failure_code"] is None
    assert len(artifact_result["artifacts"]) == 1
    assert Path(artifact_result["artifacts"][0]["path"]).is_file()
    assert (
        len(
            [
                record
                for record in Ledger(tmp_path).records("changes")
                if record["record_kind"] == "EXECUTION_PLAN"
            ]
        )
        == 1
    )

    default_root = tmp_path / "default"
    default_output = tmp_path / "default-views"
    default_output.mkdir()
    default_configuration = Configuration(
        output=OutputPreferences(output_directory=str(default_output))
    )
    save_configuration(default_root, default_configuration)
    default_request = _write(tmp_path / "default-request.json", _request().model_dump(mode="json"))
    monkeypatch.setattr(
        plan_cli, "prepare_openai_pause", lambda *_args: (_pause_action(), default_configuration)
    )
    monkeypatch.setattr(
        artifacts,
        "_write_versioned",
        lambda *_args, **_kwargs: pytest.fail("ON_REQUEST must not create a plan artifact"),
    )
    default = runner.invoke(
        app,
        ["--data-dir", str(default_root), "plan", "openai-pause", "--input", default_request],
    )
    assert default.exit_code == 0, default.stdout
    assert "artifact_result" not in json.loads(default.stdout)["data"]
    assert list(default_output.iterdir()) == []


def test_optional_plan_artifact_failure_preserves_retention_review_and_completed_refs(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    configuration = _automatic_configuration(output, formats=["json", "html"])
    save_configuration(tmp_path, configuration)
    action = _pause_action()
    request = _write(tmp_path / "request.json", _request().model_dump(mode="json"))
    monkeypatch.setattr(plan_cli, "prepare_openai_pause", lambda *_args: (action, configuration))
    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW)
    monkeypatch.setattr(plan_cli, "export_key", lambda: _KEY)
    original = artifacts._write_versioned
    calls = 0
    private_error = "private-plan-artifact-failure-sentinel-03ef20"

    def fail_second(*args: object, **kwargs: object) -> tuple[Path, bool]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(private_error)
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", request]
    )
    assert result.exit_code == 5, result.stdout
    body = json.loads(result.stdout)
    assert body["outcome"] == "PARTIAL"
    assert body["data"]["review_digest"]
    assert body["data"]["provenance"]["record_digest"]
    artifact_result = body["data"]["artifact_result"]
    assert artifact_result["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(artifact_result["artifacts"]) == len(body["artifact_refs"]) == 1
    assert Path(artifact_result["artifacts"][0]["path"]).is_file()
    assert private_error not in result.stdout
    assert Ledger(tmp_path).checkpoint()["status"] == "VERIFIED_CHECKPOINT"


def test_optional_unverified_artifact_reload_preserves_retention_and_review(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    configuration = _automatic_configuration(output)
    save_configuration(tmp_path, configuration)
    request = _write(tmp_path / "request.json", _request().model_dump(mode="json"))
    monkeypatch.setattr(
        plan_cli, "prepare_openai_pause", lambda *_args: (_pause_action(), configuration)
    )
    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW)
    monkeypatch.setattr(plan_cli, "export_key", lambda: _KEY)

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise AdhelmError("PRIVATE_LOAD_ERROR", "private pause artifact source detail")

    monkeypatch.setattr(plan_cli, "load_openai_pause_plan", unavailable)
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", request]
    )
    assert result.exit_code == 5, result.stdout
    body = json.loads(result.stdout)
    assert body["outcome"] == "PARTIAL"
    assert body["data"]["review_digest"]
    assert body["data"]["provenance"]["record_digest"]
    assert body["data"]["artifact_result"]["generation_failure_code"] == (
        "PLAN_ARTIFACT_SOURCE_UNVERIFIED"
    )
    assert body["artifact_refs"] == []
    assert "private pause artifact source detail" not in result.stdout
    assert (
        len(
            [
                record
                for record in Ledger(tmp_path).records("changes")
                if record["record_kind"] == "EXECUTION_PLAN"
            ]
        )
        == 1
    )
    assert list(output.iterdir()) == []


def test_constructed_action_and_mismatched_provenance_cannot_render_a_plan_artifact(
    tmp_path: Path,
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    action = _pause_action()
    record_digest = _retain(tmp_path, action)
    loaded, provenance = load_openai_pause_plan(Ledger(tmp_path), record_digest)
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id=loaded.binding.profile_ref,
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )

    class _PrivateValue:
        def __repr__(self) -> str:
            return "private-constructed-plan-artifact-sentinel-25fd4b"

    forged = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=loaded.status,
        binding=_PrivateValue(),
        plan=loaded.plan,
    )
    with pytest.raises(AdhelmError) as failure:
        render_openai_pause_plan_artifacts(forged, provenance, decision, key=_KEY)
    assert failure.value.code == "INVALID_EXECUTION_PLAN"
    assert "private-constructed-plan-artifact-sentinel-25fd4b" not in str(failure.value)
    assert list(output.iterdir()) == []

    mismatched: dict[str, Any] = {**provenance, "plan_digest": "f" * 64}
    with pytest.raises(AdhelmError) as failure:
        render_openai_pause_plan_artifacts(loaded, mismatched, decision, key=_KEY)
    assert failure.value.code == "PLAN_ARTIFACT_SOURCE_INVALID"
    assert list(output.iterdir()) == []


def test_plan_artifact_writer_never_exposes_an_untrusted_internal_failure_code(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    action = _pause_action()
    record_digest = _retain(tmp_path, action)
    loaded, provenance = load_openai_pause_plan(Ledger(tmp_path), record_digest)
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id=loaded.binding.profile_ref,
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )
    private_code = "private-plan-artifact-writer-sentinel-03ef20"
    private_message = "private plan artifact writer message sentinel-03ef20"

    def unavailable(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise AdhelmError(private_code, private_message)

    monkeypatch.setattr(pause_plan_artifacts, "_generate_typed_artifacts", unavailable)
    result = render_openai_pause_plan_artifacts(loaded, provenance, decision, key=_KEY)
    assert result.generation_failure_code == "LOCAL_ARTIFACT_FAILURE"
    public = json.dumps(result.to_data())
    assert private_code not in public
    assert private_message not in public
    assert list(output.iterdir()) == []


def test_discovery_and_schema_expose_only_the_closed_pause_plan_artifact_request() -> None:
    discovered = runner.invoke(app, ["discover"])
    assert discovered.exit_code == 0, discovered.stdout
    data = json.loads(discovered.stdout)["data"]
    assert "artifacts plan" in data["commands"]
    assert "pause-plan-artifact-render" in data["schemas"]
    schema = runner.invoke(app, ["schema", "pause-plan-artifact-render"])
    assert schema.exit_code == 0, schema.stdout
    properties = json.loads(schema.stdout)["data"]["schema"]["properties"]
    assert set(properties) == {
        "schema_version",
        "formats",
        "output_directory",
        "replacement_authorized",
    }

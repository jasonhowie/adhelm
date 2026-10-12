"""Closed CLI boundaries for configured Meta campaign PAUSE planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import meta_pause_cli, meta_pause_workflow
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import ArtifactRule, Configuration, OutputPreferences


def _request_file(directory: Path) -> Path:
    path = directory / "meta-pause-request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "meta-editor",
                "campaign_id": "987654321",
                "expires_in_seconds": 300,
            }
        ),
        encoding="utf-8",
    )
    return path


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "plan", "meta-campaign-pause", *arguments],
    )


def _error_code(result: Any) -> str:
    value = cast(dict[str, Any], json.loads(result.output))
    diagnostics = cast(list[dict[str, Any]], value["diagnostics"])
    return cast(str, diagnostics[0]["code"])


def _saved_generation_configuration(directory: Path) -> Configuration:
    return Configuration(
        output=OutputPreferences(
            artifact_mode="RULES",
            purposes=["plan_review"],
            workflow_rules=[
                ArtifactRule(
                    workflow="plan",
                    stage="before_approval",
                    providers=["meta_ads"],
                    profiles=["meta-editor"],
                    formats=["json"],
                )
            ],
            output_directory=str(directory),
        )
    )


def test_root_discovery_and_schema_expose_closed_meta_pause_planning_and_artifacts() -> None:
    runner = CliRunner()
    discovery = runner.invoke(root_cli.app, ["discover"])

    assert discovery.exit_code == 0, discovery.output
    data = json.loads(discovery.output)["data"]
    assert {
        "artifacts meta-campaign-pause",
        "plan meta-campaign-pause create",
        "plan meta-campaign-pause catalog",
        "plan meta-campaign-pause show",
    } <= set(data["commands"])
    assert "meta-campaign-pause-plan-request" in data["schemas"]
    assert data["meta_campaign_pause"] == {
        "artifact_generation": "STATIC_REDACTED_LOCAL_HISTORY_ONLY",
        "execution": "UNRELEASED",
        "approval_bridge": "SEPARATE_META_CAMPAIGN_PAUSE_V2_REQUIRED",
        "historical_review": "REDACTED_LOCAL_HISTORY_ONLY",
        "planning": "RETAINED_DEVELOPMENT_FIXTURE_ONLY",
        "default_runtime_provider_mutations": False,
        "live_provider_execution_verified": False,
    }

    schema = runner.invoke(
        root_cli.app,
        ["schema", "meta-campaign-pause-plan-request", "--root-only"],
    )
    assert schema.exit_code == 0, schema.output
    document = json.loads(schema.output)["data"]["schema"]
    assert document["additionalProperties"] is False
    assert document["properties"]["schema_version"]["const"] == 1
    assert set(document["properties"]) == {
        "schema_version",
        "profile_id",
        "campaign_id",
        "expires_in_seconds",
    }


def test_artifact_override_normalizes_order_and_rejects_inconsistent_flags(tmp_path: Path) -> None:
    override = meta_pause_cli._task_artifact_override(
        False,
        ["json", "html"],
        tmp_path,
    )
    assert override is not None
    assert override.requested is True
    assert override.formats == ("html", "json")
    assert override.output_directory == str(tmp_path)

    with pytest.raises(AdhelmError) as duplicate:
        meta_pause_cli._task_artifact_override(False, ["json", "json"], None)
    assert duplicate.value.code == "INVALID_META_PAUSE_ARTIFACT_REQUEST"
    assert "supported formats and a local output directory" in duplicate.value.next_action

    with pytest.raises(AdhelmError) as conflict:
        meta_pause_cli._task_artifact_override(True, ["json"], tmp_path)
    assert conflict.value.code == "INVALID_META_PAUSE_ARTIFACT_REQUEST"


def test_saved_and_task_artifact_policy_are_closed_before_workflow(
    tmp_path: Path,
) -> None:
    configuration = _saved_generation_configuration(tmp_path)
    decision = meta_pause_cli._preflight_artifact_policy(
        configuration.output,
        profile_id="meta-editor",
        task_override=None,
    )
    assert decision.outcome == "GENERATE"
    assert decision.reason == "MATCHING_SAVED_RULE"
    assert meta_pause_cli._artifact_policy_notice(decision).model_dump() == {
        "outcome": "GENERATE",
        "reason": "MATCHING_SAVED_RULE",
        "generation_supported": True,
    }

    suppressed = meta_pause_cli._preflight_artifact_policy(
        configuration.output,
        profile_id="meta-editor",
        task_override=meta_pause_cli._task_artifact_override(True, None, None),
    )
    assert suppressed.outcome == "SUPPRESSED"
    assert suppressed.reason == "TASK_OVERRIDE_DISABLED"


def test_invalid_input_and_receipt_preflight_precede_configuration_or_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "invalid.json"
    malformed.write_text('{"profile_id":"a","profile_id":"b"}', encoding="utf-8")
    monkeypatch.setattr(
        meta_pause_cli,
        "load_configuration",
        lambda _directory: pytest.fail("invalid request must precede configuration"),
    )
    result = _invoke(tmp_path, "create", "--input", str(malformed))
    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(result) == "INVALID_JSON"

    monkeypatch.setattr(meta_pause_cli, "load_configuration", lambda _directory: Configuration())
    request = _request_file(tmp_path)
    monkeypatch.setattr(meta_pause_cli, "_receipt_probe", lambda _policy: {"padding": "x" * 70_000})
    monkeypatch.setattr(
        meta_pause_workflow,
        "plan_configured_meta_campaign_pause",
        lambda *_args, **_kwargs: pytest.fail("receipt preflight must precede planner activity"),
    )
    result = _invoke(tmp_path, "create", "--input", str(request))
    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    assert not (tmp_path / "ledger").exists()


def test_catalog_closes_controls_instead_of_overwriting_forged_values() -> None:
    catalog: dict[str, object] = {
        "plans": [],
        "checkpoint_digest": "a" * 64,
        "total_count": 0,
        "next_cursor": None,
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        "execution_authority": True,
        "remote_mutations": False,
    }
    with pytest.raises(AdhelmError) as forged:
        meta_pause_cli._closed_catalog(catalog)
    assert forged.value.code == "INVALID_META_PAUSE_PLAN_CATALOG"

    catalog["execution_authority"] = False
    catalog["unexpected_raw_action"] = {"private": "not-output"}
    with pytest.raises(AdhelmError) as extra:
        meta_pause_cli._closed_catalog(catalog)
    assert extra.value.code == "INVALID_META_PAUSE_PLAN_CATALOG"


def test_ledger_open_after_retention_is_ambiguous_not_generic_local_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request_file(tmp_path)
    monkeypatch.setattr(meta_pause_cli, "load_configuration", lambda _directory: Configuration())
    monkeypatch.setattr(
        meta_pause_workflow,
        "plan_configured_meta_campaign_pause",
        lambda *_args, **_kwargs: object(),
    )

    def unavailable(_directory: Path) -> object:
        raise OSError("receipt reload private marker")

    monkeypatch.setattr(meta_pause_cli, "Ledger", unavailable)
    result = _invoke(tmp_path, "create", "--input", str(request))

    assert result.exit_code == ExitCode.AMBIGUOUS
    assert _error_code(result) == "META_PAUSE_PLAN_RECEIPT_UNVERIFIED"
    assert "receipt reload private marker" not in result.output
    assert "do not repeat an ambiguous plan preparation" in result.output

"""Saved and one-task artifact policy boundaries for Meta PAUSE planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_meta_pause_planning_integration import _Runtime
from test_meta_pause_planning_integration import runtime as _integration_runtime

from adhelm import artifacts, meta_pause_artifact_cli, meta_pause_cli
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan


@pytest.fixture(name="runtime")
def _runtime_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Runtime:
    return _integration_runtime.__wrapped__(tmp_path, monkeypatch)


def _request(directory: Path) -> Path:
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


def _matching_preferences(output: Path, *, formats: list[str]) -> OutputPreferences:
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        output_directory=str(output),
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["meta_ads"],
                profiles=["meta-editor"],
                formats=formats,
            )
        ],
        max_output_bytes=131_072,
    )


def test_default_on_request_plan_is_file_free_and_does_not_read_a_sharing_key(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "meta-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    save_configuration(
        runtime.root,
        configuration.model_copy(
            update={
                "output": OutputPreferences(
                    artifact_mode="ON_REQUEST", output_directory=str(destination)
                )
            }
        ),
        replace=True,
    )

    def forbidden() -> bytes:
        raise AssertionError("default planning must not obtain an artifact sharing key")

    monkeypatch.setattr(meta_pause_cli, "export_key", forbidden)
    result = runtime.plan()

    assert "artifact_result" not in result["data"]
    assert result["data"]["artifact_policy"] == {
        "outcome": "SUPPRESSED",
        "reason": "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
        "generation_supported": True,
    }
    assert list(destination.iterdir()) == []
    assert len(runtime.paths) == 3


def test_saved_rule_result_preflight_precedes_provider_key_or_retention(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "meta-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    preferences = _matching_preferences(destination, formats=["json"]).model_copy(
        update={"max_output_bytes": 1_024}
    )
    save_configuration(
        runtime.root,
        configuration.model_copy(update={"output": preferences}),
        replace=True,
    )
    request = _request(runtime.root)
    checkpoint = Ledger(runtime.root).checkpoint()["checkpoint_digest"]

    def forbidden() -> bytes:
        raise AssertionError("result-envelope preflight must precede sharing-key access")

    monkeypatch.setattr(meta_pause_cli, "export_key", forbidden)
    result = runtime.call(
        "plan", "meta-campaign-pause", "create", "--input", str(request), expected_exit=3
    )

    assert result["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert runtime.paths == []
    assert Ledger(runtime.root).checkpoint()["checkpoint_digest"] == checkpoint
    assert list(destination.iterdir()) == []


def test_no_artifacts_bypasses_matching_saved_rule_without_opening_destination_or_key(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "meta-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    save_configuration(
        runtime.root,
        configuration.model_copy(
            update={"output": _matching_preferences(destination, formats=["json"])}
        ),
        replace=True,
    )
    request = _request(runtime.root)

    def forbidden() -> bytes:
        raise AssertionError("--no-artifacts must suppress sharing-key access")

    monkeypatch.setattr(meta_pause_cli, "export_key", forbidden)
    result = runtime.call(
        "plan", "meta-campaign-pause", "create", "--input", str(request), "--no-artifacts"
    )

    assert result["data"]["artifact_policy"]["reason"] == "TASK_OVERRIDE_DISABLED"
    assert "artifact_result" not in result["data"]
    assert list(destination.iterdir()) == []
    assert len(runtime.paths) == 3


def test_post_retention_preflight_failure_returns_receipt_without_a_key(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "meta-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    save_configuration(
        runtime.root,
        configuration.model_copy(
            update={"output": _matching_preferences(destination, formats=["json"])}
        ),
        replace=True,
    )

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise AdhelmError("ARTIFACT_SIZE_LIMIT_EXCEEDED", "private fixture failure")

    def forbidden() -> bytes:
        raise AssertionError("post-retention preflight failure must precede key access")

    monkeypatch.setattr(
        "adhelm.meta_pause_plan_artifacts.preflight_meta_campaign_pause_plan_artifact_output",
        fail_preflight,
    )
    monkeypatch.setattr(meta_pause_cli, "export_key", forbidden)
    result = runtime.plan(expected_exit=5)
    data = cast(dict[str, Any], result["data"])
    artifact_result = cast(dict[str, Any], data["artifact_result"])

    assert result["outcome"] == "PARTIAL"
    assert artifact_result["generation_failure_code"] == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert artifact_result["artifacts"] == []
    action, provenance = load_meta_campaign_pause_plan(Ledger(runtime.root), data["record_digest"])
    assert provenance["record_digest"] == data["record_digest"]
    assert action.plan.plan_digest == data["plan_digest"]
    assert list(destination.iterdir()) == []
    assert "private fixture failure" not in json.dumps(result)


def test_final_source_drift_omits_stale_checkpoint_and_never_reads_key(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "meta-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    save_configuration(
        runtime.root,
        configuration.model_copy(
            update={"output": _matching_preferences(destination, formats=["json"])}
        ),
        replace=True,
    )
    original_load = meta_pause_artifact_cli._load_source
    loads = 0

    def drift_second_load(directory: Path, selected: str) -> tuple[object, dict[str, Any], object]:
        nonlocal loads
        action, provenance, source = original_load(directory, selected)
        loads += 1
        if loads == 2:
            return action, {**provenance, "checkpoint_digest": "f" * 64}, source
        return action, provenance, source

    def forbidden() -> bytes:
        raise AssertionError("final source drift must precede sharing-key access")

    monkeypatch.setattr(meta_pause_artifact_cli, "_load_source", drift_second_load)
    monkeypatch.setattr(meta_pause_cli, "export_key", forbidden)
    result = runtime.plan(expected_exit=5)
    artifact_result = cast(dict[str, Any], result["data"]["artifact_result"])

    assert artifact_result["generation_failure_code"] == "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
    assert artifact_result["checkpoint_digest"] is None
    assert artifact_result["artifacts"] == []
    assert loads == 2
    assert list(destination.iterdir()) == []


def test_partial_writer_failure_keeps_receipt_and_completed_artifact_references(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "meta-pause-artifacts"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    save_configuration(
        runtime.root,
        configuration.model_copy(
            update={"output": _matching_preferences(destination, formats=["json", "html"])}
        ),
        replace=True,
    )
    original_write = artifacts._write_versioned
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private writer failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(meta_pause_cli, "export_key", lambda: b"r" * 32)
    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = runtime.plan(expected_exit=5)
    data = cast(dict[str, Any], result["data"])
    artifact_result = cast(dict[str, Any], data["artifact_result"])

    assert result["outcome"] == "PARTIAL"
    assert artifact_result["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(artifact_result["artifacts"]) == len(result["artifact_refs"]) == 1
    assert len(list(destination.iterdir())) == 1
    assert data["record_digest"]
    assert "private writer failure" not in json.dumps(result)

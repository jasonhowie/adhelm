"""Real local Meta planning/artifact boundaries with synthetic HTTP and stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

from adhelm import artifacts, credentials
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan


@pytest.fixture(autouse=True)
def sharing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)


def _saved_rule(runtime: _Runtime, formats: list[str]) -> Path:
    destination = runtime.root / "plan-exports"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(runtime.root)
    preferences = OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        output_directory=str(destination),
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["meta_ads"],
                profiles=["meta-editor"],
                formats=formats,
            )
        ],
        max_output_bytes=262_144,
    )
    save_configuration(
        runtime.root, configuration.model_copy(update={"output": preferences}), replace=True
    )
    return destination


def _assert_only_planning_reads(runtime: _Runtime) -> None:
    assert runtime.paths == [
        "/v26.0/debug_token",
        "/v26.0/act_12345",
        "/v26.0/987654321",
    ]
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    for forbidden in (_SUBJECT.encode(), _INSPECTOR.encode(), b"APPLY_STARTED"):
        assert forbidden not in history


def test_saved_rule_generates_three_redacted_formats_from_actual_retained_plan(
    runtime: _Runtime,
) -> None:
    destination = _saved_rule(runtime, ["json", "csv", "html"])
    result = runtime.plan()
    data = result["data"]
    generated = data["artifact_result"]
    action, provenance = load_meta_campaign_pause_plan(Ledger(runtime.root), data["record_digest"])

    assert len(generated["artifacts"]) == 3
    assert {item["format"] for item in generated["artifacts"]} == {"json", "csv", "html"}
    assert data["plan_digest"] == action.plan.plan_digest
    assert generated["source_record_digest"] == provenance["record_digest"]
    assert generated["checkpoint_digest"] == provenance["checkpoint_digest"]
    assert data["execution_authority"] is False
    for item in generated["artifacts"]:
        path = Path(item["path"])
        assert path.parent == destination
        contents = path.read_text()
        assert action.plan.plan_digest in contents
        assert "NOT_PREDICTED" in contents
        assert "WITH_ISSUES" in contents
        assert "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE" in contents
        for forbidden in (
            _SUBJECT,
            _INSPECTOR,
            "987654321",
            "act_12345",
            "meta-editor",
            action.execution_binding.credential_binding_ref,
            action.execution_binding.principal_ref,
            action.execution_binding.configuration_digest,
            "canonical_receipt",
            "canonical_challenge",
        ):
            assert forbidden not in contents
    assert len(list(destination.iterdir())) == 3
    _assert_only_planning_reads(runtime)


def test_historical_regeneration_reuses_exact_files_without_more_provider_reads(
    runtime: _Runtime,
) -> None:
    destination = _saved_rule(runtime, ["json"])
    first = runtime.plan()
    record_digest = first["data"]["record_digest"]
    first_artifacts = first["data"]["artifact_result"]["artifacts"]
    checkpoint = Ledger(runtime.root).checkpoint()["checkpoint_digest"]
    request = runtime.root / "historical-artifact-request.json"
    request.write_text(
        json.dumps({"schema_version": 1, "formats": ["json"], "output_directory": str(destination)})
    )

    second = runtime.call(
        "artifacts", "meta-campaign-pause", record_digest, "--input", str(request)
    )
    produced = second["data"]["artifact_result"]["artifacts"]

    assert len(produced) == 1
    assert produced[0]["reused"] is True
    for field in ("path", "artifact_digest", "input_digest", "settings_digest"):
        assert produced[0][field] == first_artifacts[0][field]
    assert Ledger(runtime.root).checkpoint()["checkpoint_digest"] == checkpoint
    assert len(list(destination.iterdir())) == 1
    _assert_only_planning_reads(runtime)


def test_second_file_failure_keeps_retained_plan_and_first_verified_file(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _saved_rule(runtime, ["json", "html"])
    write = artifacts._write_versioned
    attempts = 0

    def fail_second(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("synthetic private artifact failure")
        return write(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = runtime.plan(expected_exit=5)
    data = result["data"]
    generated = data["artifact_result"]
    action, provenance = load_meta_campaign_pause_plan(Ledger(runtime.root), data["record_digest"])

    assert result["outcome"] == "PARTIAL"
    assert generated["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(generated["artifacts"]) == 1
    assert Path(generated["artifacts"][0]["path"]).is_file()
    assert len(list(destination.iterdir())) == 1
    assert data["plan_digest"] == action.plan.plan_digest
    assert provenance["execution_authority"] is False
    assert "synthetic private artifact failure" not in json.dumps(result)
    assert runtime.call("plan", "meta-campaign-pause", "catalog")["total_count"] == 1
    _assert_only_planning_reads(runtime)


def test_unavailable_sharing_key_keeps_the_retained_plan_without_files(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm import meta_pause_cli

    destination = _saved_rule(runtime, ["json"])

    def unavailable_key() -> bytes:
        raise AdhelmError("SYNTHETIC_KEY_UNAVAILABLE", "private protected-store detail")

    monkeypatch.setattr(meta_pause_cli, "export_key", unavailable_key)
    result = runtime.plan(expected_exit=5)
    data = result["data"]
    action, provenance = load_meta_campaign_pause_plan(Ledger(runtime.root), data["record_digest"])

    assert data["artifact_result"]["generation_failure_code"] == "LOCAL_ARTIFACT_FAILURE"
    assert data["artifact_result"]["artifacts"] == []
    assert data["plan_digest"] == action.plan.plan_digest
    assert data["artifact_result"]["checkpoint_digest"] == provenance["checkpoint_digest"]
    assert list(destination.iterdir()) == []
    assert "private protected-store detail" not in json.dumps(result)
    assert "SYNTHETIC_KEY_UNAVAILABLE" not in json.dumps(result)
    _assert_only_planning_reads(runtime)

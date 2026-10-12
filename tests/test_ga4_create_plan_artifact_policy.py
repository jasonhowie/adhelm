"""Saved GA4 CREATE plan artifact rules preserve a retained plan receipt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_ga4_admin_execution_observation import _KEY
from test_ga4_create_cli import _install_root, _invoke, _write_request
from test_ga4_create_workflow import _setup

from adhelm import artifacts, ga4_create_cli
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan


def _matching_preferences(
    output: Path, profile_id: str, *, formats: list[str]
) -> OutputPreferences:
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        output_directory=str(output),
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["ga4_admin"],
                profiles=[profile_id],
                formats=formats,
            )
        ],
        max_output_bytes=131_072,
    )


def test_default_on_request_create_is_file_free_and_does_not_read_a_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ledger, _store, _profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    output = tmp_path / "artifact-output"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        configuration.model_copy(
            update={
                "output": OutputPreferences(
                    artifact_mode="ON_REQUEST", output_directory=str(output)
                )
            }
        ),
        replace=True,
    )
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)

    def forbidden() -> bytes:
        raise AssertionError("default plan creation must not obtain an artifact sharing key")

    monkeypatch.setattr(ga4_create_cli, "export_key", forbidden)
    result = _invoke("create", "--input", str(source))

    assert result.exit_code == 0, result.output
    assert "artifact_result" not in json.loads(result.output)["data"]
    assert len(calls) == 3 and all(call.method == "GET" for call in calls)
    assert list(output.iterdir()) == []


def test_saved_rule_full_preflight_bounds_result_before_provider_key_or_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    output = tmp_path / "artifact-output"
    output.mkdir(mode=0o700)
    preferences = _matching_preferences(output, profile.profile_id, formats=["json"])
    preferences = preferences.model_copy(update={"max_output_bytes": 1024})
    save_configuration(
        tmp_path, configuration.model_copy(update={"output": preferences}), replace=True
    )
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]

    def forbidden() -> bytes:
        raise AssertionError("result-envelope preflight must precede sharing-key access")

    monkeypatch.setattr(ga4_create_cli, "export_key", forbidden)
    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint
    assert list(output.iterdir()) == []


def test_post_retention_artifact_preflight_failure_keeps_plan_receipt_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    output = tmp_path / "artifact-output"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        configuration.model_copy(
            update={"output": _matching_preferences(output, profile.profile_id, formats=["json"])}
        ),
        replace=True,
    )
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise AdhelmError("ARTIFACT_SIZE_LIMIT_EXCEEDED", "private fixture failure")

    def forbidden() -> bytes:
        raise AssertionError("post-retention preflight failure must precede sharing-key access")

    monkeypatch.setattr(
        "adhelm.ga4_create_plan_artifacts.preflight_ga4_key_event_create_plan_artifact_output",
        fail_preflight,
    )
    monkeypatch.setattr(ga4_create_cli, "export_key", forbidden)
    result = _invoke("create", "--input", str(source))

    assert result.exit_code == 5, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]
    artifact_result = data["artifact_result"]
    assert envelope["outcome"] == "PARTIAL"
    assert artifact_result["generation_failure_code"] == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert artifact_result["artifacts"] == []
    action, provenance = load_ga4_key_event_create_plan(ledger, data["record_digest"])
    assert provenance["record_digest"] == data["record_digest"]
    assert action.plan.plan_digest == data["plan_digest"]
    assert len(calls) == 3 and all(call.method == "GET" for call in calls)
    assert list(output.iterdir()) == []
    assert "private fixture failure" not in result.output


def test_partial_writer_failure_keeps_receipt_and_completed_artifact_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ledger, _store, profile, configuration, _current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    output = tmp_path / "artifact-output"
    output.mkdir(mode=0o700)
    save_configuration(
        tmp_path,
        configuration.model_copy(
            update={
                "output": _matching_preferences(
                    output, profile.profile_id, formats=["json", "html"]
                )
            }
        ),
        replace=True,
    )
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    original_write = artifacts._write_versioned
    writes = 0

    def fail_second(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("private second artifact failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(ga4_create_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = _invoke("create", "--input", str(source))

    assert result.exit_code == 5, result.output
    envelope = json.loads(result.output)
    artifact_result = envelope["data"]["artifact_result"]
    assert envelope["outcome"] == "PARTIAL"
    assert envelope["data"]["record_digest"]
    assert artifact_result["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 1
    assert len(list(output.iterdir())) == 1
    assert "private second artifact failure" not in result.output

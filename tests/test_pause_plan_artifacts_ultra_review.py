"""Independent adversarial checks for retained OpenAI pause-plan artifacts.

These use only fixture actions, temporary local directories, and in-memory
redaction keys. They never invoke a provider, credential store, approval host,
or external command.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_openai_pause import NOW, _action, _credentials, _Scenario
from typer.testing import CliRunner

import adhelm.artifacts as artifacts
from adhelm import __version__
from adhelm.artifacts import (
    ArtifactContext,
    ArtifactDecision,
    ArtifactMetadata,
    ArtifactTaskOverride,
    resolve_artifact_policy,
)
from adhelm.canonical import canonical_bytes
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences, ResultEnvelope
from adhelm.pause_plan_artifacts import (
    PausePlanArtifactResult,
    explicit_openai_pause_plan_artifact_decision,
    pause_plan_artifact_failure_preview,
    render_openai_pause_plan_artifacts,
)
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction, build_openai_pause_plan
from adhelm.saved_plans import load_openai_pause_plan, retain_openai_pause_plan

_KEY = b"u" * 32
_RUNNER = CliRunner()


def _pause_action() -> OpenAIPausePlannedAction:
    legacy, _ = _action(
        _Scenario(),
        _credentials(profile="pause-plan-artifact-ultra-profile"),
        expires_at=NOW + timedelta(minutes=10),
        planning_now=NOW,
    )
    return build_openai_pause_plan(
        legacy.binding,
        expires_at=legacy.plan.expires_at,
        # The installed artifact command reads the default local workspace
        # ledger.  Its plan record must therefore bind that workspace while
        # retaining the distinct credential profile inside the action binding.
        workspace_profile_ref="default",
    )


def _retained_source(root: Path) -> tuple[OpenAIPausePlannedAction, dict[str, object], str]:
    record_digest = retain_openai_pause_plan(Ledger(root), _pause_action())["record_digest"]
    action, provenance = load_openai_pause_plan(Ledger(root), record_digest)
    return action, provenance, record_digest


def _decision(output: Path) -> ArtifactDecision:
    return explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id="pause-plan-artifact-ultra-profile",
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )


def _write_request(path: Path, formats: list[str]) -> str:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "formats": formats,
                "output_directory": str(path.parent / "views"),
            }
        )
    )
    return str(path)


@pytest.mark.parametrize("value", [1, 0, 1.0, 0.0, "yes", b"true"])
def test_public_one_request_override_rejects_every_non_boolean_authorization(
    tmp_path: Path, value: object
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    context = ArtifactContext("plan", "before_approval", "plan_review", "openai_ads", "profile")

    with pytest.raises(AdhelmError) as failure:
        resolve_artifact_policy(
            context,
            OutputPreferences(detail="standard"),
            task_override=ArtifactTaskOverride(
                formats=("json",),
                output_directory=str(output),
                replacement_authorized=value,  # type: ignore[arg-type]
            ),
        )

    assert failure.value.code == "INVALID_ARTIFACT_OVERRIDE"
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value", "private_text"),
    [
        ("preferences_digest", "../private-artifact-decision", "private-artifact-decision"),
        ("reason", "private-artifact-decision-reason", "private-artifact-decision-reason"),
        ("max_output_bytes", float("inf"), "inf"),
        ("replacement_authorized", "yes", "yes"),
    ],
)
def test_forged_decision_never_reaches_writer_or_agent_visible_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    private_text: str,
) -> None:
    output = tmp_path / "views"
    output.mkdir()
    action, provenance, _ = _retained_source(tmp_path / "data")
    forged = replace(_decision(output), **{field: value})

    def writer_reached(*_args: object, **_kwargs: object) -> tuple[Path, bool]:
        pytest.fail("forged artifact decision reached a filesystem writer")

    monkeypatch.setattr(artifacts, "_write_versioned", writer_reached)
    with pytest.raises(AdhelmError) as failure:
        render_openai_pause_plan_artifacts(action, provenance, forged, key=_KEY)

    assert failure.value.code == "ARTIFACT_POLICY_INVALID"
    assert private_text not in str(failure.value)
    assert list(output.iterdir()) == []


def test_public_result_serializer_rejects_forged_policy_and_metadata_bindings(
    tmp_path: Path,
) -> None:
    """The public result helper cannot create a false agent-visible artifact reference."""
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output)
    private_reason = "private-direct-artifact-result-reason"

    with pytest.raises(AdhelmError) as failure:
        PausePlanArtifactResult(
            replace(decision, reason=private_reason), "e" * 64, "d" * 64, ()
        ).to_data()

    assert failure.value.code == "ARTIFACT_POLICY_INVALID"
    assert private_reason not in str(failure.value)

    mismatched = ArtifactMetadata(
        path=output / ("artifact-" + "a" * 16 + "-" + "b" * 16 + ".json"),
        format="json",
        reused=False,
        artifact_digest="c" * 64,
        input_digest="d" * 64,
        settings_digest="e" * 64,
        source_record_digest="f" * 64,
        checkpoint_digest="0" * 64,
    )
    with pytest.raises(AdhelmError) as failure:
        PausePlanArtifactResult(decision, "f" * 64, "0" * 64, (mismatched,)).to_data()

    assert failure.value.code == "PLAN_ARTIFACT_RESULT_INVALID"


@pytest.mark.parametrize(
    ("source_record_digest", "checkpoint_digest", "private_text"),
    [
        ("private-source-record", "f" * 64, "private-source-record"),
        ("e" * 64, "private-checkpoint", "private-checkpoint"),
        ("E" * 64, "f" * 64, "E" * 64),
    ],
)
def test_preflight_refuses_unverified_digest_text_before_agent_visible_metadata(
    tmp_path: Path,
    source_record_digest: str,
    checkpoint_digest: str,
    private_text: str,
) -> None:
    output = tmp_path / "views"
    output.mkdir()

    with pytest.raises(AdhelmError) as failure:
        pause_plan_artifact_failure_preview(
            _decision(output),
            source_record_digest=source_record_digest,
            checkpoint_digest=checkpoint_digest,
        )

    assert failure.value.code == "PLAN_ARTIFACT_SOURCE_INVALID"
    assert private_text not in str(failure.value)
    assert list(output.iterdir()) == []


def test_explicit_partial_result_keeps_every_completed_artifact_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(root, Configuration(output=OutputPreferences(detail="standard")))
    _, _, record_digest = _retained_source(root)
    request = _write_request(tmp_path / "request.json", ["json", "html"])
    original_write = artifacts._write_versioned
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> tuple[Path, bool]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private-artifact-write-sentinel")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)
    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    result = _RUNNER.invoke(
        app,
        ["--data-dir", str(root), "artifacts", "plan", record_digest, "--input", request],
    )

    assert result.exit_code == 5, result.stdout
    body = json.loads(result.stdout)
    listed = body["data"]["artifact_result"]["artifacts"]
    assert body["outcome"] == "PARTIAL"
    assert body["data"]["artifact_result"]["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert body["artifact_refs"] == [
        "artifact:sha256:" + item["artifact_digest"] for item in listed
    ]
    assert len(listed) == 1
    assert Path(listed[0]["path"]).is_file()
    assert "private-artifact-write-sentinel" not in result.stdout


def test_exact_partial_envelope_and_trailing_lf_preflight_before_key_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late partial result cannot make a completed artifact reference unreportable."""
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    action, provenance, record_digest = _retained_source(root)
    checkpoint_digest = provenance["checkpoint_digest"]
    assert type(checkpoint_digest) is str
    decision = explicit_openai_pause_plan_artifact_decision(
        OutputPreferences(detail="standard"),
        profile_id=action.binding.profile_ref,
        formats=("json", "csv", "html"),
        output_directory=str(output),
        replacement_authorized=None,
    )
    preview_result, artifact_refs = pause_plan_artifact_failure_preview(
        decision,
        source_record_digest=record_digest,
        checkpoint_digest=checkpoint_digest,
    )
    summary = {
        "source_record_digest": record_digest,
        "action_digest": provenance["action_digest"],
        "plan_digest": provenance["plan_digest"],
        "historical_evidence_status": provenance["historical_evidence_status"],
        "execution_authority": False,
        "remote_mutations": False,
        "opening_or_editing_authorizes_execution": False,
        "artifact_result": preview_result,
    }
    exact_limit = (
        len(
            canonical_bytes(
                ResultEnvelope(
                    tool_version=__version__,
                    command="artifacts plan",
                    outcome="PLAN_REVIEW_NO_EXECUTION_AUTHORITY",
                    data=summary,
                    artifact_refs=artifact_refs,
                )
            )
        )
        + 1
    )
    assert exact_limit > 1_024
    save_configuration(
        root,
        Configuration(
            output=OutputPreferences(detail="standard", max_output_bytes=exact_limit - 1)
        ),
    )
    request = _write_request(tmp_path / "request.json", ["json", "csv", "html"])
    monkeypatch.setattr(
        adhelm.credentials,
        "export_key",
        lambda: pytest.fail("result preflight must precede export-key access"),
    )
    monkeypatch.setattr(
        artifacts,
        "_write_versioned",
        lambda *_args, **_kwargs: pytest.fail("result preflight must precede publication"),
    )

    result = _RUNNER.invoke(
        app,
        ["--data-dir", str(root), "artifacts", "plan", record_digest, "--input", request],
    )

    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_static_plan_artifact_redacts_bindings_and_has_no_execution_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    root = tmp_path / "data"
    output = tmp_path / "views"
    output.mkdir()
    save_configuration(root, Configuration(output=OutputPreferences(detail="standard")))
    action, _, record_digest = _retained_source(root)
    request = _write_request(tmp_path / "request.json", ["json", "csv", "html"])
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)

    result = _RUNNER.invoke(
        app,
        ["--data-dir", str(root), "artifacts", "plan", record_digest, "--input", request],
    )

    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    paths = [Path(item["path"]) for item in body["data"]["artifact_result"]["artifacts"]]
    json_path = next(path for path in paths if path.suffix == ".json")
    content = json.loads(json_path.read_text())["content"]
    assert content["authority"] == {
        "execution_authority": False,
        "remote_mutations": False,
        "execution_command": "UNRELEASED",
    }
    assert content["historical_review"]["current_validity"] == "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    assert content["historical_review"]["opening_or_editing_authorizes_execution"] is False
    rendered = "\n".join(path.read_text() for path in paths)
    for raw in (
        action.binding.account_ref,
        action.binding.account_client_ref,
        action.binding.profile_ref,
        action.binding.credential_binding_ref,
        action.binding.campaign.campaign_ref,
        action.binding.campaign.campaign_client_ref,
    ):
        assert raw not in rendered
    assert "opaque:" in rendered
    html = next(path.read_text() for path in paths if path.suffix == ".html")
    assert "<script" not in html.lower()
    assert "href=" not in html.lower()

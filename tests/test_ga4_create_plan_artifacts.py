"""Hermetic checks for static artifacts of retained GA4 CREATE plans."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_ga4_admin_execution_observation import _KEY
from test_ga4_create_workflow import _setup

import adhelm.ga4_create_plan_artifacts as plan_artifacts
from adhelm.artifacts import ArtifactGenerationError, ArtifactMetadata
from adhelm.errors import AdhelmError
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan


def _loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan

    request, ledger, _, profile, _, _, _, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    action, provenance = load_ga4_key_event_create_plan(ledger, retained.plan_record_digest)
    return action, provenance, profile


def _decision(output: Path, profile_id: str):
    return plan_artifacts.explicit_ga4_key_event_create_plan_artifact_decision(
        OutputPreferences(artifact_mode="OFF", detail="standard"),
        profile_id=profile_id,
        formats=("json", "csv", "html"),
        output_directory=str(output),
        replacement_authorized=None,
    )


def test_static_artifact_is_redacted_historical_reusable_and_lossless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, profile.profile_id)
    plan_artifacts.preflight_ga4_key_event_create_plan_artifact_output(action, provenance, decision)
    first = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )
    assert first.generation_failure_code is None
    assert {item.format for item in first.artifacts} == {"json", "csv", "html"}
    document = json.loads(
        next(item.path for item in first.artifacts if item.format == "json").read_text()
    )
    content = document["content"]
    assert document["artifact_kind"] == "ga4_key_event_create_plan_review"
    assert content["provenance"]["record_digest"] == provenance["record_digest"]
    assert content["historical_review"]["as_of"] == provenance["recorded_at"]
    assert content["historical_review"]["current_validity"] == "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    assert content["authority"] == {
        "execution_authority": False,
        "remote_mutations": False,
        "execution_command": "UNRELEASED",
    }
    assert content["recovery_limits"] == {
        "allowed_recovery_actions": [],
        "recovery_disposition": "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION",
        "blind_retry_permitted": False,
        "ambiguity_status": "UNRESOLVED_NO_BLIND_RETRY",
        "requires_current_kernel_checks": True,
    }
    assert (
        content["review"]["before"]["member_count"] + 1
        == content["review"]["after"]["member_count"]
    )
    assert content["review"]["current_state_status"] == "RECHECK_REQUIRED_BEFORE_EGRESS"
    assert content["review"]["provider_create_permission"] == "NOT_PREPROVABLE"
    assert content["review"]["processed_report_verification"] == "NOT_EVALUATED"
    rendered = "\n".join(item.path.read_text() for item in first.artifacts)
    for private in (
        "qualified_request",
        "properties/123",
        "ga4_admin.property.123",
        "ga4_admin.web_stream.456",
        profile.profile_id,
        profile.principal_ref,
        profile.client_ref,
        "eventName",
        "fingerprint_key_marker",
        "inventory_content_marker",
    ):
        assert private not in rendered
    assert "opaque:" in rendered
    html = next(item.path.read_text() for item in first.artifacts if item.format == "html")
    assert "<script" not in html.lower()
    assert "href=" not in html.lower()

    second = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )
    assert all(item.reused for item in second.artifacts)


def test_source_mismatch_and_constructed_action_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, profile.profile_id)
    bad_provenance = {**provenance, "action_digest": "f" * 64}
    with pytest.raises(AdhelmError) as source_error:
        plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(action, bad_provenance)
    assert source_error.value.code == "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID"

    constructed = action.model_copy(update={"execution_authority": True})
    with pytest.raises(AdhelmError) as action_error:
        plan_artifacts.render_ga4_key_event_create_plan_artifacts(
            constructed, provenance, decision, key=_KEY
        )
    assert action_error.value.code == "INVALID_GA4_KEY_EVENT_CREATE_PLAN"
    assert list(output.iterdir()) == []


def test_post_retention_zero_key_preflight_fails_before_writer_or_real_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "views"
    output.mkdir()
    decision = plan_artifacts.explicit_ga4_key_event_create_plan_artifact_decision(
        OutputPreferences(artifact_mode="OFF", detail="standard", max_output_bytes=1024),
        profile_id=profile.profile_id,
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )

    with pytest.raises(AdhelmError) as error:
        plan_artifacts.preflight_ga4_key_event_create_plan_artifact_output(
            action, provenance, decision
        )
    assert error.value.code == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_failure_preview_and_post_retention_failure_result_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, profile.profile_id)
    preview, refs = plan_artifacts.ga4_create_plan_artifact_failure_preview(
        decision,
        source_record_digest=provenance["record_digest"],
        checkpoint_digest=provenance["checkpoint_digest"],
    )
    assert len(preview["artifacts"]) == 3
    assert len(refs) == 3
    assert all(".v9999." in item["path"] for item in preview["artifacts"])

    result = plan_artifacts.ga4_create_plan_artifact_failure_result(
        decision,
        source_record_digest=provenance["record_digest"],
        checkpoint_digest=None,
        failure_code="private-error-with-secret-sentinel",
    )
    data = result.to_data()
    assert data["generation_failure_code"] == "LOCAL_ARTIFACT_FAILURE"
    assert "private-error-with-secret-sentinel" not in json.dumps(data)

    forged = replace(
        result,
        artifacts=(
            ArtifactMetadata(
                path=output / "artifact-not-bound.json",
                format="json",
                reused=False,
                artifact_digest="f" * 64,
                input_digest="f" * 64,
                settings_digest="f" * 64,
                source_record_digest=provenance["record_digest"],
                checkpoint_digest="f" * 64,
            ),
        ),
    )
    with pytest.raises(AdhelmError) as result_error:
        forged.to_data()
    assert result_error.value.code == "GA4_CREATE_PLAN_ARTIFACT_RESULT_INVALID"


def test_policy_context_keeps_default_on_request_file_free_and_matches_saved_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, profile = _loaded(tmp_path, monkeypatch)
    assert (
        plan_artifacts.preflight_ga4_key_event_create_plan_artifacts(
            OutputPreferences(artifact_mode="ON_REQUEST"), profile_id=profile.profile_id
        ).outcome
        == "SUPPRESSED"
    )
    output = tmp_path / "views"
    output.mkdir()
    preferences = OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        output_directory=str(output),
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["ga4_admin"],
                profiles=[profile.profile_id],
                formats=["json"],
            )
        ],
    )
    decision = plan_artifacts.preflight_ga4_key_event_create_plan_artifacts(
        preferences, profile_id=profile.profile_id
    )
    assert decision.outcome == "GENERATE"
    assert decision.reason == "MATCHING_SAVED_RULE"


def test_document_schema_rejects_impossible_counting_and_noncanonical_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.canonical import canonical_bytes

    action, provenance, _ = _loaded(tmp_path, monkeypatch)
    checked, source = plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    document = plan_artifacts._ga4_create_plan_document(  # noqa: SLF001
        checked, source, key=_KEY, max_rows=50
    )

    impossible_counting = json.loads(json.dumps(document))
    impossible_counting["content"]["review"]["counting_method"] = "ONCE_PER_SESSION"
    with pytest.raises(ValidationError):
        plan_artifacts.Ga4CreatePlanArtifactDocument.model_validate_json(
            canonical_bytes(impossible_counting)
        )

    split_as_of = json.loads(json.dumps(document))
    split_as_of["content"]["review"]["reviewed_at"] = "2031-01-01T00:00:00Z"
    with pytest.raises(ValidationError):
        plan_artifacts.Ga4CreatePlanArtifactDocument.model_validate_json(
            canonical_bytes(split_as_of)
        )


@pytest.mark.parametrize(
    ("nested_field", "updates"),
    (
        ("authority", {"execution_authority": True}),
        ("authority", {"remote_mutations": True}),
        ("recovery_limits", {"blind_retry_permitted": True}),
        ("recovery_limits", {"requires_current_kernel_checks": False}),
        ("historical_review", {"static_artifact": False}),
        ("historical_review", {"opening_or_editing_authorizes_execution": True}),
    ),
)
def test_document_serializer_rejects_constructed_nested_control_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested_field: str,
    updates: dict[str, object],
) -> None:
    from adhelm.canonical import canonical_bytes

    action, provenance, _ = _loaded(tmp_path, monkeypatch)
    checked, source = plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    document_data = plan_artifacts._ga4_create_plan_document(  # noqa: SLF001
        checked, source, key=_KEY, max_rows=50
    )
    document = plan_artifacts.Ga4CreatePlanArtifactDocument.model_validate_json(
        canonical_bytes(document_data)
    )
    nested = getattr(document.content, nested_field).model_copy(update=updates)
    forged = document.model_copy(
        update={"content": document.content.model_copy(update={nested_field: nested})}
    )

    with pytest.raises(ValueError, match="GA4 CREATE plan artifact document is invalid"):
        forged.model_dump(mode="json", warnings="error")


@pytest.mark.parametrize(
    ("section", "field", "alias"),
    (
        ("authority", "execution_authority", 0),
        ("authority", "remote_mutations", 0.0),
        ("recovery_limits", "blind_retry_permitted", 0),
        ("recovery_limits", "requires_current_kernel_checks", 1.0),
    ),
)
def test_document_raw_controls_require_json_booleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    alias: object,
) -> None:
    action, provenance, _ = _loaded(tmp_path, monkeypatch)
    checked, source = plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    raw = plan_artifacts._ga4_create_plan_document(  # noqa: SLF001
        checked, source, key=_KEY, max_rows=50
    )
    raw["content"][section][field] = alias  # type: ignore[index]

    with pytest.raises(ValidationError):
        plan_artifacts.Ga4CreatePlanArtifactDocument.model_validate_json(json.dumps(raw))


def test_failure_preview_reserves_a_real_longest_public_failure_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "views"
    output.mkdir()
    preview, _ = plan_artifacts.ga4_create_plan_artifact_failure_preview(
        _decision(output, profile.profile_id),
        source_record_digest=provenance["record_digest"],
        checkpoint_digest=provenance["checkpoint_digest"],
    )
    assert preview["generation_failure_code"] == "GA4_CREATE_PLAN_ARTIFACT_SOURCE_UNVERIFIED"
    assert preview["generation_failure_code"] in plan_artifacts._PUBLIC_FAILURE_CODES  # noqa: SLF001


def test_provenance_accepts_only_loader_or_exact_complete_artifact_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, provenance, _ = _loaded(tmp_path, monkeypatch)
    _, source = plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    complete = source.model_dump(mode="json")
    assert (
        plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(action, complete)[1]
        == source
    )

    with pytest.raises(AdhelmError) as intermediate:
        plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
            action, {**provenance, "expires_at": complete["expires_at"]}
        )
    assert intermediate.value.code == "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID"

    with pytest.raises(AdhelmError) as mismatch:
        plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
            action, {**complete, "expires_at": "2031-01-01T00:05:01Z"}
        )
    assert mismatch.value.code == "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID"


def test_writer_failure_returns_sanitized_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output, profile.profile_id)

    def interrupted(*_args: object, **_kwargs: object):
        raise ArtifactGenerationError((), "internal-secret-failure")

    monkeypatch.setattr(plan_artifacts, "_generate_typed_artifacts", interrupted)
    result = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )
    assert result.artifacts == ()
    assert result.generation_failure_code == "LOCAL_ARTIFACT_FAILURE"
    assert "internal-secret-failure" not in json.dumps(result.to_data())

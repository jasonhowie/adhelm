"""Hermetic static-artifact checks for retained Meta campaign PAUSE plans."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import adhelm.meta_pause_plan_artifacts as plan_artifacts
from adhelm.artifacts import ArtifactDecision, ArtifactMetadata, ArtifactTaskOverride
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)
from adhelm.saved_meta_pause_plans import (
    load_meta_campaign_pause_plan,
    retain_meta_campaign_pause_plan,
)
from adhelm.strict_json import validate_json_value

_START = datetime(2026, 9, 6, 12, tzinfo=UTC)
_KEY = b"m" * 32


def _action(*, minutes: int = 10) -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref="workspace-private-marker",
        account_binding=MetaCampaignPauseAccountBinding(
            account_ref="meta_ads.account.act_123456789",
            account_client_ref="act_123456789",
            timezone="UTC",
            currency="USD",
            account_status="1",
        ),
        execution_binding=MetaCampaignPauseExecutionBinding(
            profile_ref="meta-edit-private-marker",
            client_application_ref="1234567890",
            principal_ref="meta_token_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            inspected_at=_START,
            expires_at=_START + timedelta(hours=1),
            data_access_expires_at=_START + timedelta(hours=2),
        ),
        observation_binding=MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654321",
                campaign_client_ref="987654321",
                account_client_ref="123456789",
                configured_status="ACTIVE",
                effective_status="WITH_ISSUES",
            ),
            read_started_at=_START + timedelta(seconds=1),
            read_finished_at=_START + timedelta(seconds=2),
        ),
        expires_at=_START + timedelta(minutes=minutes),
    )


def _loaded(tmp_path: Path) -> tuple[MetaCampaignPausePlannedAction, dict[str, object]]:
    ledger = Ledger(tmp_path / "data", profile_ref="workspace-private-marker")
    stored = retain_meta_campaign_pause_plan(ledger, _action())
    return load_meta_campaign_pause_plan(ledger, stored["record_digest"])


def _decision(output: Path) -> ArtifactDecision:
    return plan_artifacts.explicit_meta_campaign_pause_plan_artifact_decision(
        OutputPreferences(artifact_mode="OFF", detail="standard"),
        profile_id="meta-edit-private-marker",
        formats=("json", "csv", "html"),
        output_directory=str(output),
        replacement_authorized=None,
    )


def test_static_artifact_is_redacted_historical_reusable_and_lossless(tmp_path: Path) -> None:
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output)

    plan_artifacts.preflight_meta_campaign_pause_plan_artifact_output(action, provenance, decision)
    first = plan_artifacts.render_meta_campaign_pause_plan_artifacts(
        action, provenance, decision, key=_KEY
    )

    assert first.generation_failure_code is None
    assert {item.format for item in first.artifacts} == {"json", "csv", "html"}
    document = json.loads(
        next(item.path for item in first.artifacts if item.format == "json").read_text()
    )
    content = document["content"]
    assert document["artifact_kind"] == "meta_campaign_pause_plan_review"
    assert content["provenance"]["record_digest"] == provenance["record_digest"]
    assert content["historical_review"]["as_of"] == provenance["recorded_at"]
    assert content["historical_review"]["current_validity"] == "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    assert content["authority"] == {
        "execution_authority": False,
        "remote_mutations": False,
        "execution_command": "UNRELEASED",
    }
    assert content["recovery_limits"] == {
        "allowed_recovery_actions": ["PAUSE"],
        "recovery_disposition": "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED",
        "blind_retry_permitted": False,
        "ambiguity_status": "UNRESOLVED_NO_BLIND_RETRY",
        "requires_current_kernel_checks": True,
    }
    assert content["review"]["baseline"] == {
        "configured_status": "ACTIVE",
        "effective_status": "WITH_ISSUES",
    }
    assert content["review"]["postcondition"] == {
        "postcondition_kind": "CONFIGURATION_ONLY",
        "configured_status": "PAUSED",
        "effective_delivery": "NOT_PREDICTED",
        "configuration_only": True,
    }
    assert content["review"]["execution_authority"] is False
    assert content["review"]["remote_side_effects"] is False

    rendered = b"\n".join(item.path.read_bytes() for item in first.artifacts)
    for private in (
        b"workspace-private-marker",
        b"meta_ads.account.act_123456789",
        b"act_123456789",
        b"meta_ads.campaign.987654321",
        b"987654321",
        b"meta-edit-private-marker",
        b"1234567890",
        b"credential-binding:sha256:",
        b"meta_token_principal_v1.",
        b'"configuration_digest"',
        b'"profile_configuration_digest"',
        b'"required_scope"',
    ):
        assert private not in rendered
    assert b"opaque:" in rendered
    html = next(item.path.read_text() for item in first.artifacts if item.format == "html")
    assert "<script" not in html.lower()
    assert "href=" not in html.lower()

    second = plan_artifacts.render_meta_campaign_pause_plan_artifacts(
        action, provenance, decision, key=_KEY
    )
    assert all(item.reused for item in second.artifacts)


def test_source_mismatch_and_constructed_action_fail_before_writer(tmp_path: Path) -> None:
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output)

    with pytest.raises(AdhelmError) as source_error:
        plan_artifacts.revalidate_meta_campaign_pause_plan_artifact_source(
            action,
            {**provenance, "action_digest": "f" * 64},
        )
    assert source_error.value.code == "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"

    with pytest.raises(AdhelmError) as early_source:
        plan_artifacts.revalidate_meta_campaign_pause_plan_artifact_source(
            action,
            {**provenance, "recorded_at": "2026-09-06T12:00:00Z"},
        )
    assert early_source.value.code == "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"

    constructed = action.model_copy(update={"execution_authority": True})
    with pytest.raises(AdhelmError) as action_error:
        plan_artifacts.render_meta_campaign_pause_plan_artifacts(
            constructed, provenance, decision, key=_KEY
        )
    assert action_error.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN"
    assert list(output.iterdir()) == []


def test_zero_key_output_preflight_fails_before_writer_or_real_key(tmp_path: Path) -> None:
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = plan_artifacts.explicit_meta_campaign_pause_plan_artifact_decision(
        OutputPreferences(artifact_mode="OFF", detail="standard", max_output_bytes=1024),
        profile_id="meta-edit-private-marker",
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )

    with pytest.raises(AdhelmError) as error:
        plan_artifacts.preflight_meta_campaign_pause_plan_artifact_output(
            action, provenance, decision
        )
    assert error.value.code == "ARTIFACT_SIZE_LIMIT_EXCEEDED"
    assert list(output.iterdir()) == []


def test_failure_preview_and_post_retention_failure_result_are_closed(tmp_path: Path) -> None:
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = _decision(output)

    preview, refs = plan_artifacts.meta_campaign_pause_plan_artifact_failure_preview(
        decision,
        source_record_digest=provenance["record_digest"],  # type: ignore[arg-type]
        checkpoint_digest=provenance["checkpoint_digest"],  # type: ignore[arg-type]
    )
    preview_artifacts = preview["artifacts"]
    assert isinstance(preview_artifacts, list)
    assert len(preview_artifacts) == 3
    assert len(refs) == 3
    assert all(isinstance(item, dict) and ".v9999." in item["path"] for item in preview_artifacts)

    result = plan_artifacts.meta_campaign_pause_plan_artifact_failure_result(
        decision,
        source_record_digest=provenance["record_digest"],  # type: ignore[arg-type]
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
                source_record_digest=provenance["record_digest"],  # type: ignore[arg-type]
                checkpoint_digest="f" * 64,
            ),
        ),
    )
    with pytest.raises(AdhelmError) as result_error:
        forged.to_data()
    assert result_error.value.code == "META_PAUSE_PLAN_ARTIFACT_RESULT_INVALID"


def test_saved_and_task_policy_preflight_keeps_suppressed_output_file_free(tmp_path: Path) -> None:
    assert (
        plan_artifacts.preflight_meta_campaign_pause_plan_artifacts(
            OutputPreferences(artifact_mode="ON_REQUEST"),
            profile_id="meta-edit-private-marker",
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
                providers=["meta_ads"],
                profiles=["meta-edit-private-marker"],
                formats=["json"],
            )
        ],
    )
    decision = plan_artifacts.preflight_meta_campaign_pause_plan_artifacts(
        preferences,
        profile_id="meta-edit-private-marker",
    )
    assert decision.outcome == "GENERATE"
    assert decision.reason == "MATCHING_SAVED_RULE"

    suppressed = plan_artifacts.preflight_meta_campaign_pause_plan_artifacts(
        preferences,
        profile_id="meta-edit-private-marker",
        task_override=ArtifactTaskOverride(requested=False, purpose="plan_review"),
    )
    assert suppressed.outcome == "SUPPRESSED"
    assert list(output.iterdir()) == []


def test_document_model_rejects_constructed_authority_or_source_mutation(tmp_path: Path) -> None:
    action, provenance = _loaded(tmp_path)
    _, source = plan_artifacts.revalidate_meta_campaign_pause_plan_artifact_source(
        action,
        provenance,
    )
    document_data = plan_artifacts._meta_campaign_pause_plan_document(  # noqa: SLF001
        action,
        source,
        key=_KEY,
        max_rows=50,
    )
    document = validate_json_value(
        plan_artifacts.MetaCampaignPausePlanArtifactDocument,
        document_data,
    )
    forged = document.model_copy(
        update={
            "content": document.content.model_copy(
                update={
                    "authority": document.content.authority.model_copy(
                        update={"remote_mutations": True}
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="Meta campaign PAUSE artifact document is invalid"):
        forged.model_dump(mode="json")


def test_result_serialization_normalizes_missing_constructed_slots(tmp_path: Path) -> None:
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir()
    decision = plan_artifacts.explicit_meta_campaign_pause_plan_artifact_decision(
        OutputPreferences(artifact_mode="OFF"),
        profile_id="meta-edit-private-marker",
        formats=("json",),
        output_directory=str(output),
        replacement_authorized=None,
    )
    with pytest.raises(AdhelmError) as missing_result:
        object.__new__(plan_artifacts.MetaCampaignPausePlanArtifactResult).to_data()
    assert missing_result.value.code == "META_PAUSE_PLAN_ARTIFACT_RESULT_INVALID"

    malformed_metadata = object.__new__(ArtifactMetadata)
    result = plan_artifacts.MetaCampaignPausePlanArtifactResult(
        decision,
        provenance["record_digest"],  # type: ignore[arg-type]
        provenance["checkpoint_digest"],  # type: ignore[arg-type]
        (malformed_metadata,),
    )
    with pytest.raises(AdhelmError) as missing_metadata:
        result.to_data()
    assert missing_metadata.value.code == "META_PAUSE_PLAN_ARTIFACT_RESULT_INVALID"

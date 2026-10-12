from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from adhelm.artifacts import (
    ArtifactContext,
    ArtifactTaskOverride,
    RetainedArtifactInput,
    generate_artifacts,
    resolve_artifact_policy,
)
from adhelm.audit import AuditPolicy, audit_report
from adhelm.errors import AdhelmError
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.reporting import (
    ConversionBreakdownRow,
    NormalizedReport,
    PerformanceRow,
    ReportEnvelope,
)

_DIGEST = "a" * 64
_CHECKPOINT = "b" * 64
_KEY = b"k" * 32


def _report(*, quality: list[str] | None = None) -> NormalizedReport:
    return NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report-1",
            provider="google_ads",
            source_account_ref="local-account-1",
            query_contract_hash=_DIGEST,
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 2),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="provider",
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
            data_quality_statuses=quality or ["COMPLETE"],
            provider_api_version="v1",
            connector_version="v1",
        ),
        performance_rows=[
            PerformanceRow(
                report_id="report-1",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="campaign",
                resource_ref="campaign-secret-local-id",
                dimension_type="placement",
                dimension_value="=formula-looking-value",
                impressions=1,
                clicks=0,
                spend={"minor_units": 0, "currency": "USD"},
            )
        ],
    )


def _context() -> ArtifactContext:
    return ArtifactContext(
        workflow="report",
        stage="completion",
        purpose="result",
        provider="google_ads",
        profile_id="read-profile",
    )


def test_defaults_are_on_request_and_explicit_user_request_needs_a_destination() -> None:
    assert (
        resolve_artifact_policy(_context(), None).reason
        == "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST"
    )
    decision = resolve_artifact_policy(
        _context(), OutputPreferences(), task_override=ArtifactTaskOverride(formats=("json",))
    )
    assert decision.reason == "LOCAL_OUTPUT_DIRECTORY_REQUIRED"


def test_defaults_are_on_request_and_explicit_user_request_overrides_off(tmp_path) -> None:
    assert (
        resolve_artifact_policy(_context(), None).reason
        == "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST"
    )
    preferences = OutputPreferences(artifact_mode="OFF", output_directory=str(tmp_path))
    decision = resolve_artifact_policy(
        _context(),
        preferences,
        task_override=ArtifactTaskOverride(formats=("json",), purpose="result"),
    )
    assert decision.outcome == "GENERATE"
    assert decision.reason == "EXPLICIT_USER_REQUEST"


def test_rules_match_provider_profile_workflow_stage_and_selected_purpose(tmp_path) -> None:
    preferences = OutputPreferences(
        artifact_mode="RULES",
        purposes=["result"],
        output_directory=str(tmp_path),
        workflow_rules=[
            ArtifactRule(
                workflow="report",
                stage="completion",
                providers=["google_ads"],
                profiles=["read-profile"],
                formats=["csv", "html"],
            )
        ],
    )
    decision = resolve_artifact_policy(_context(), preferences)
    assert decision.outcome == "GENERATE"
    assert decision.formats == ("csv", "html")
    no_purpose = resolve_artifact_policy(
        ArtifactContext("report", "completion", "audit", "google_ads", "read-profile"), preferences
    )
    assert no_purpose.reason == "PURPOSE_NOT_ENABLED"


def test_destination_is_required_and_remote_or_symlink_destinations_fail(tmp_path) -> None:
    decision = resolve_artifact_policy(
        _context(), OutputPreferences(), task_override=ArtifactTaskOverride(formats=("json",))
    )
    assert decision.reason == "LOCAL_OUTPUT_DIRECTORY_REQUIRED"
    with pytest.raises(AdhelmError, match="local directory"):
        resolve_artifact_policy(
            _context(),
            OutputPreferences(),
            task_override=ArtifactTaskOverride(
                formats=("json",), output_directory="https://example.test"
            ),
        )


def test_symlink_artifact_destinations_fail(tmp_path, make_link) -> None:
    link = tmp_path / "link"
    make_link(link, tmp_path)
    with pytest.raises(AdhelmError, match="symbolic"):
        resolve_artifact_policy(
            _context(),
            OutputPreferences(),
            task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(link)),
        )


def test_generation_is_deterministic_reused_redacted_and_safe(tmp_path) -> None:
    decision = resolve_artifact_policy(
        _context(),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(
            formats=("json", "csv", "html"), output_directory=str(tmp_path), purpose="result"
        ),
    )
    retained = RetainedArtifactInput(
        _report(quality=["PARTIAL_PAGE_FAILURE"]), _DIGEST, _CHECKPOINT
    )
    first = generate_artifacts(retained, decision, key=_KEY)
    second = generate_artifacts(retained, decision, key=_KEY)
    assert [item.reused for item in first] == [False, False, False]
    assert [item.reused for item in second] == [True, True, True]
    rendered = "\n".join(item.path.read_text() for item in first)
    assert "campaign-secret-local-id" not in rendered
    assert "local-account-1" not in rendered
    assert "PARTIAL_PAGE_FAILURE" in rendered
    csv_text = next(item.path.read_text() for item in first if item.format == "csv")
    assert "'=formula-looking-value" in csv_text
    html_text = next(item.path.read_text() for item in first if item.format == "html")
    assert "<script" not in html_text.lower()
    assert "href=" not in html_text.lower()


def test_tampered_file_creates_new_version_by_default(tmp_path) -> None:
    decision = resolve_artifact_policy(
        _context(),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )
    retained = RetainedArtifactInput(_report(), _DIGEST, _CHECKPOINT)
    original = generate_artifacts(retained, decision, key=_KEY)[0]
    original.path.write_text("tampered")
    fresh = generate_artifacts(retained, decision, key=_KEY)[0]
    assert fresh.path != original.path
    assert fresh.path.name.endswith(".v2.json")


def test_regeneration_and_authorized_replacement_follow_both_controls(tmp_path) -> None:
    retained = RetainedArtifactInput(_report(), _DIGEST, _CHECKPOINT)
    regenerate = resolve_artifact_policy(
        _context(),
        OutputPreferences(regeneration="REGENERATE"),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )
    generate_artifacts(retained, regenerate, key=_KEY)
    second = generate_artifacts(retained, regenerate, key=_KEY)[0]
    assert second.path.name.endswith(".v2.json")
    assert second.reused is False
    replace = resolve_artifact_policy(
        _context(),
        OutputPreferences(overwrite_policy="REPLACE_AUTHORIZED"),
        task_override=ArtifactTaskOverride(
            formats=("csv",), output_directory=str(tmp_path), replacement_authorized=True
        ),
    )
    original = generate_artifacts(retained, replace, key=_KEY)[0]
    original.path.write_text("tampered")
    replaced = generate_artifacts(retained, replace, key=_KEY)[0]
    assert replaced.path == original.path
    assert replaced.path.read_text() != "tampered"


def test_redaction_opaques_ga4_event_and_nested_references(tmp_path) -> None:
    report = _report()
    row = report.performance_rows[0].model_copy(
        update={
            "report_id": "ga4-report-local",
            "resource_ref": "resource-local",
            "parent_resource_ref": "parent-local",
            "campaign_ref": "campaign-local",
            "campaign_join_key": "join-local",
            "creative_family_id": "family-local",
            "creative_version_id": "version-local",
            "creative_deployment_id": "deployment-local",
            "dimension_type": "GA4_EVENT",
            "dimension_value": "ga4-event-local",
            "resource_level": "AD",
            "metric_grain": "AD",
            "data_quality_statuses": ["CREATIVE_NOT_EXPOSED"],
        }
    )
    rendition = row.model_copy(
        update={
            "resource_level": "CREATIVE",
            "metric_grain": "RENDITION",
            "resource_ref": "rendition-resource-local",
            "served_rendition_id": "rendition-local",
            "data_quality_statuses": [],
        }
    )
    component = row.model_copy(
        update={
            "resource_level": "CREATIVE",
            "metric_grain": "COMPONENT",
            "resource_ref": "component-resource-local",
            "component_ref": "component-local",
            "data_quality_statuses": [],
        }
    )
    conversion = ConversionBreakdownRow(
        report_id="ga4-report-local",
        campaign_ref="conversion-campaign-local",
        campaign_join_key="conversion-join-local",
        resource_ref="conversion-resource-local",
        conversion_source="GA4",
        conversion_action_ref="action-local",
        conversion_contract_version_ref="contract-local",
        conversion_mapping_version_ref="mapping-local",
        conversion_kind="lead",
        attribution_model="last_click",
    )
    report = NormalizedReport(
        envelope=report.envelope.model_copy(
            update={
                "report_id": "ga4-report-local",
                "provider": "ga4",
                "source_account_ref": "ga4-account-local",
            }
        ),
        performance_rows=[row, rendition, component],
        conversion_rows=[conversion],
    )
    decision = resolve_artifact_policy(
        _context(),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )
    rendered = generate_artifacts(
        RetainedArtifactInput(report, _DIGEST, _CHECKPOINT), decision, key=_KEY
    )[0].path.read_text()
    for raw_value in (
        "ga4-report-local",
        "resource-local",
        "parent-local",
        "campaign-local",
        "join-local",
        "family-local",
        "version-local",
        "rendition-local",
        "component-local",
        "rendition-resource-local",
        "component-resource-local",
        "deployment-local",
        "ga4-event-local",
        "conversion-campaign-local",
        "conversion-join-local",
        "conversion-resource-local",
        "action-local",
        "contract-local",
        "mapping-local",
        "ga4-account-local",
    ):
        assert raw_value not in rendered
    assert "opaque:" in rendered


def test_size_preflight_writes_no_partial_format_set(tmp_path) -> None:
    source = _report()
    report = NormalizedReport(
        envelope=source.envelope,
        performance_rows=[
            source.performance_rows[0].model_copy(
                update={"resource_ref": f"campaign-{index}", "dimension_value": f"value-{index}"}
            )
            for index in range(50)
        ],
    )
    decision = resolve_artifact_policy(
        _context(),
        OutputPreferences(max_output_bytes=1024),
        task_override=ArtifactTaskOverride(
            formats=("json", "html"), output_directory=str(tmp_path)
        ),
    )
    with pytest.raises(AdhelmError, match="size bound"):
        generate_artifacts(RetainedArtifactInput(report, _DIGEST, _CHECKPOINT), decision, key=_KEY)
    assert list(tmp_path.iterdir()) == []


def test_audit_report_type_is_rendered_from_retained_evidence(tmp_path) -> None:
    audit = audit_report(
        _report(),
        source_record_digest=_DIGEST,
        policy=AuditPolicy(
            maximum_observation_age_seconds=3600,
            minimum_impressions=None,
            maximum_cpc=None,
            require_conversion_evidence=False,
        ),
        as_of=datetime(2026, 1, 3, tzinfo=UTC),
    )
    decision = resolve_artifact_policy(
        ArtifactContext("audit", "completion", "audit", "google_ads", "read-profile"),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )
    artifact = generate_artifacts(
        RetainedArtifactInput(audit, _DIGEST, _CHECKPOINT), decision, key=_KEY
    )[0]
    assert '"artifact_kind":"audit_report"' in artifact.path.read_text()


def test_only_retained_report_or_audit_types_are_supported() -> None:
    with pytest.raises(TypeError, match="NormalizedReport"):
        RetainedArtifactInput(object(), _DIGEST, _CHECKPOINT)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("saved_policy", "task_authorized", "replaces"),
    [
        ("REPLACE_AUTHORIZED", None, True),
        ("REPLACE_AUTHORIZED", False, False),
        ("NEW_VERSION", True, True),
    ],
)
def test_replacement_uses_task_then_saved_authority_without_changing_render_identity(
    tmp_path, saved_policy, task_authorized, replaces
) -> None:
    retained = RetainedArtifactInput(_report(), _DIGEST, _CHECKPOINT)
    initial = resolve_artifact_policy(
        _context(),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )
    original = generate_artifacts(retained, initial, key=_KEY)[0]
    original.path.write_text("edited locally")
    selected = resolve_artifact_policy(
        _context(),
        OutputPreferences(overwrite_policy=saved_policy),
        task_override=ArtifactTaskOverride(
            formats=("json",),
            output_directory=str(tmp_path),
            replacement_authorized=task_authorized,
        ),
    )
    assert selected.preferences_digest == initial.preferences_digest
    result = generate_artifacts(retained, selected, key=_KEY)[0]
    assert (result.path == original.path) is replaces
    assert (original.path.read_text() == "edited locally") is not replaces


def test_failed_temporary_write_publishes_no_partial_artifact(tmp_path, monkeypatch) -> None:
    import adhelm.artifacts

    decision = resolve_artifact_policy(
        _context(),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )

    def fail_fsync(_descriptor):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(adhelm.artifacts.os, "fsync", fail_fsync)
    with pytest.raises(adhelm.artifacts.ArtifactGenerationError) as caught:
        generate_artifacts(
            RetainedArtifactInput(_report(), _DIGEST, _CHECKPOINT), decision, key=_KEY
        )
    assert caught.value.completed == ()
    assert list(tmp_path.iterdir()) == []


def test_directory_durability_failure_keeps_published_artifact_reference(
    tmp_path, monkeypatch
) -> None:
    import adhelm.artifacts

    def fail_directory(_directory):
        raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(adhelm.artifacts, "_fsync_directory", fail_directory)
    decision = resolve_artifact_policy(
        _context(),
        OutputPreferences(),
        task_override=ArtifactTaskOverride(formats=("json",), output_directory=str(tmp_path)),
    )
    with pytest.raises(adhelm.artifacts.ArtifactGenerationError) as caught:
        generate_artifacts(
            RetainedArtifactInput(_report(), _DIGEST, _CHECKPOINT), decision, key=_KEY
        )
    assert caught.value.failure_code == "DIRECTORY_FSYNC_FAILED"
    assert len(caught.value.completed) == 1
    assert caught.value.completed[0].path.is_file()

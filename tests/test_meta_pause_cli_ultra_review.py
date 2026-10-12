"""Independent CLI boundary coverage for Meta campaign PAUSE planning.

The tests use local JSONL fixtures and monkeypatched private seams only.  They
never call Meta, read a real credential store, launch a helper, or request an
execution grant.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import meta_pause_cli, meta_pause_workflow
from adhelm.config import save_configuration
from adhelm.errors import ExitCode
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan

_T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "plan", "meta-campaign-pause", *arguments],
    )


def _data(result: Any) -> dict[str, Any]:
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _error_code(result: Any) -> str:
    diagnostics = _data(result)["diagnostics"]
    assert type(diagnostics) is list and len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert type(diagnostic) is dict and type(diagnostic.get("code")) is str
    return cast(str, diagnostic["code"])


def _request_file(directory: Path) -> Path:
    path = directory / "request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "meta-edit",
                "campaign_id": "987654321",
                "expires_in_seconds": 300,
            }
        ),
        encoding="utf-8",
    )
    return path


def _retained_action() -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref="default",
        account_binding=MetaCampaignPauseAccountBinding(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone="UTC",
            currency="USD",
            account_status="1",
        ),
        execution_binding=MetaCampaignPauseExecutionBinding(
            profile_ref="private-profile-marker",
            client_application_ref="123456789",
            principal_ref="meta_token_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            inspected_at=_T0,
            expires_at=_T0 + timedelta(hours=1),
            data_access_expires_at=_T0 + timedelta(hours=2),
        ),
        observation_binding=MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654321",
                campaign_client_ref="987654321",
                account_client_ref="123456",
                configured_status="ACTIVE",
                effective_status="WITH_ISSUES",
            ),
            read_started_at=_T0 + timedelta(seconds=1),
            read_finished_at=_T0 + timedelta(seconds=2),
        ),
        expires_at=_T0 + timedelta(minutes=10),
    )


def test_show_rejects_invalid_selection_before_configuration_or_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        meta_pause_cli,
        "load_configuration",
        lambda _directory: pytest.fail("selection must precede configuration access"),
    )
    monkeypatch.setattr(
        meta_pause_cli,
        "export_key",
        lambda: pytest.fail("selection must precede sharing-key access"),
    )

    result = _invoke(tmp_path, "show", "not-a-digest")

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(result) == "INVALID_META_PAUSE_PLAN_SELECTION"
    assert "not-a-digest" not in result.output


def test_show_bounds_full_redacted_review_before_obtaining_real_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1_024)))
    record = retain_meta_campaign_pause_plan(Ledger(tmp_path), _retained_action())
    record_digest = cast(str, record["record_digest"])
    monkeypatch.setattr(meta_pause_cli, "_now_utc", lambda: _T0 + timedelta(minutes=5))
    monkeypatch.setattr(
        meta_pause_cli,
        "export_key",
        lambda: pytest.fail("bounded probe must precede sharing-key access"),
    )

    result = _invoke(tmp_path, "show", record_digest)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    assert "private-profile-marker" not in result.output
    assert "987654321" not in result.output


def test_constructed_workflow_result_is_rejected_without_serializing_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request_file(tmp_path)
    forged = object.__new__(meta_pause_workflow.RetainedMetaCampaignPausePlan)
    object.__setattr__(forged, "plan_record_digest", "f" * 64)
    object.__setattr__(forged, "action", "workflow-private-marker")
    monkeypatch.setattr(
        meta_pause_workflow,
        "plan_configured_meta_campaign_pause",
        lambda *_args, **_kwargs: forged,
    )

    result = _invoke(tmp_path, "create", "--input", str(request))

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _error_code(result) == "META_PAUSE_PLAN_RESULT_INVALID"
    assert "workflow-private-marker" not in result.output
    assert "f" * 64 not in result.output


def test_special_artifact_target_is_not_opened_before_unsupported_policy_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_pipe
) -> None:
    request = _request_file(tmp_path)
    target = make_pipe("artifact-target.fifo")
    calls: list[Path] = []
    original_open = Path.open

    def tracked_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == target:
            calls.append(path)
            pytest.fail("unsupported artifact target must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(
        meta_pause_workflow,
        "plan_configured_meta_campaign_pause",
        lambda *_args, **_kwargs: pytest.fail("artifact refusal must precede workflow"),
    )

    result = _invoke(
        tmp_path,
        "create",
        "--input",
        str(request),
        "--artifact-format",
        "json",
        "--artifact-output",
        str(target),
    )

    assert result.exit_code == ExitCode.PRECONDITION
    assert _error_code(result) == "UNSAFE_ARTIFACT_DESTINATION"
    assert calls == []


def test_static_discovery_and_schema_do_not_open_meta_workflow_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> object:
        pytest.fail("static command must not open the Meta planning workflow")

    monkeypatch.setattr(meta_pause_workflow, "plan_configured_meta_campaign_pause", unexpected)
    runner = CliRunner()

    discovery = runner.invoke(root_cli.app, ["discover"])
    schema = runner.invoke(root_cli.app, ["schema", "meta-campaign-pause-plan-request"])

    assert discovery.exit_code == 0, discovery.output
    assert schema.exit_code == 0, schema.output
    assert _data(discovery)["data"]["meta_campaign_pause"]["execution"] == "UNRELEASED"
    assert _data(schema)["data"]["schema"]["additionalProperties"] is False

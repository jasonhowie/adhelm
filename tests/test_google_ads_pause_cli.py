"""Public-boundary coverage for Google Ads PAUSE planning.

All provider-facing work is replaced by the existing retained-plan fixture.  No
test opens a credential store or sends a provider request.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_google_ads_campaign_state import _campaign_response, _identity_response
from test_google_ads_pause_credentials import _ACCESS, _GENERIC_SERVICE, _installed
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import google_ads_pause_cli, google_ads_pause_workflow
from adhelm import google_ads_pause_planning_session as sessions
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_credentials import GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, Configuration, OutputPreferences
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseCampaignBaseline,
    GoogleAdsCampaignPauseExecutionBinding,
    GoogleAdsCampaignPauseObservationBinding,
    build_google_ads_campaign_pause_plan,
)
from adhelm.saved_google_ads_pause_plans import retain_google_ads_campaign_pause_plan

_T0 = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "plan", "google-ads-campaign-pause", *arguments],
    )


def _payload(result: Any) -> dict[str, Any]:
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _code(result: Any) -> str:
    diagnostics = _payload(result)["diagnostics"]
    assert type(diagnostics) is list and len(diagnostics) == 1
    return cast(str, diagnostics[0]["code"])


def _request(directory: Path) -> Path:
    value = directory / "request.json"
    value.write_text(
        json.dumps({"schema_version": 1, "profile_id": "google-edit", "campaign_id": "42"}),
        encoding="utf-8",
    )
    return value


def _action() -> Any:
    return build_google_ads_campaign_pause_plan(
        workspace_profile_ref="default",
        account_binding=GoogleAdsCampaignPauseAccountBinding(
            account_ref="google_ads.customer.1234567890",
            account_client_ref="1234567890",
            currency="USD",
            timezone="UTC",
            account_status="CLOSED",
        ),
        execution_binding=GoogleAdsCampaignPauseExecutionBinding(
            profile_ref="google-ads-edit",
            client_application_ref="fixture-google-ads.apps.googleusercontent.com",
            principal_ref="google_oidc_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            login_customer_client_ref="0987654321",
            resolved_at=_T0,
            expires_at=_T0 + timedelta(hours=1),
        ),
        observation_binding=GoogleAdsCampaignPauseObservationBinding(
            campaign=GoogleAdsCampaignPauseCampaignBaseline(
                campaign_ref="google_ads.campaign.1234567890.42",
                campaign_client_ref="42",
                account_ref="google_ads.customer.1234567890",
                account_client_ref="1234567890",
            ),
            read_started_at=_T0 + timedelta(seconds=1),
            read_finished_at=_T0 + timedelta(seconds=2),
        ),
        expires_at=_T0 + timedelta(minutes=10),
    )


def test_discovery_and_schema_expose_closed_google_pause_planning() -> None:
    runner = CliRunner()
    discovery = _payload(runner.invoke(root_cli.app, ["discover"]))["data"]
    assert {
        "plan google-ads-campaign-pause create",
        "plan google-ads-campaign-pause catalog",
        "plan google-ads-campaign-pause show",
    } <= set(discovery["commands"])
    assert discovery["google_ads_campaign_pause"] == {
        "planning": "RETAINED_DEVELOPMENT_FIXTURE_ONLY",
        "historical_review": "REDACTED_LOCAL_HISTORY_ONLY",
        "artifact_generation": "STATIC_REDACTED_LOCAL_HISTORY_ONLY",
        "execution": "UNRELEASED",
        "default_runtime_provider_mutations": False,
        "live_provider_execution_verified": False,
    }
    schema = _payload(
        runner.invoke(
            root_cli.app, ["schema", "google-ads-campaign-pause-plan-request", "--root-only"]
        )
    )["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "schema_version",
        "profile_id",
        "campaign_id",
        "expires_in_seconds",
    }


def test_invalid_input_and_incomplete_artifact_policy_precede_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "bad.json"
    malformed.write_text('{"profile_id":"a","profile_id":"b"}', encoding="utf-8")
    monkeypatch.setattr(
        google_ads_pause_cli,
        "load_configuration",
        lambda _: pytest.fail("request parse must precede configuration"),
    )
    result = _invoke(tmp_path, "create", "--input", str(malformed))
    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _code(result) == "INVALID_JSON"

    monkeypatch.setattr(
        google_ads_pause_cli,
        "load_configuration",
        lambda _: Configuration(),
    )
    monkeypatch.setattr(
        google_ads_pause_workflow,
        "plan_configured_google_ads_campaign_pause",
        lambda *_args, **_kwargs: pytest.fail("artifact refusal must precede protected workflow"),
    )
    # A requested format without any configured local destination cannot resolve
    # to a complete policy, and must fail before the protected planner.
    result = _invoke(
        tmp_path,
        "create",
        "--input",
        str(_request(tmp_path)),
        "--artifact-format",
        "json",
    )
    assert result.exit_code == ExitCode.PRECONDITION
    assert _code(result) == "ARTIFACT_CONFIGURATION_REQUIRED"
    next_action = _payload(result)["diagnostics"][0]["next_action"]
    assert "artifacts google-ads-campaign-pause" in next_action


def test_create_no_artifacts_composes_retained_workflow_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _action()
    stored = retain_google_ads_campaign_pause_plan(Ledger(tmp_path), action)
    retained = google_ads_pause_workflow.RetainedGoogleAdsCampaignPausePlan(
        action=action, plan_record_digest=stored["record_digest"]
    )
    configuration = Configuration(output=OutputPreferences())
    monkeypatch.setattr(google_ads_pause_cli, "load_configuration", lambda _: configuration)
    monkeypatch.setattr(
        google_ads_pause_workflow,
        "plan_configured_google_ads_campaign_pause",
        lambda *_args, **_kwargs: retained,
    )
    result = _invoke(tmp_path, "create", "--input", str(_request(tmp_path)), "--no-artifacts")
    assert result.exit_code == 0, result.output
    data = _payload(result)["data"]
    assert data["record_digest"] == stored["record_digest"]
    assert data["maximum_incremental_spend"] == {"minor_units": 0, "currency": "USD"}
    assert data["artifact_policy"] == {
        "outcome": "SUPPRESSED",
        "reason": "TASK_OVERRIDE_DISABLED",
        "generation_supported": True,
    }
    assert "artifact_result" not in data
    assert "google-ads-edit" not in result.output
    assert "credential-binding" not in result.output


def test_catalog_and_show_are_history_only_and_show_bounds_before_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = retain_google_ads_campaign_pause_plan(Ledger(tmp_path), _action())
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1_024)))
    catalog = _invoke(tmp_path, "catalog")
    assert catalog.exit_code == 0, catalog.output
    assert _payload(catalog)["data"]["plans"][0]["record_digest"] == stored["record_digest"]
    monkeypatch.setattr(
        google_ads_pause_cli,
        "export_key",
        lambda: pytest.fail("complete bound must precede sharing key"),
    )
    show = _invoke(tmp_path, "show", stored["record_digest"])
    assert show.exit_code == ExitCode.PRECONDITION
    assert _code(show) == "OUTPUT_LIMIT_EXCEEDED"
    assert "google-ads-edit" not in show.output


def _before_approval_rule(output: Path | None) -> OutputPreferences:
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        output_directory=str(output) if output is not None else None,
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["google_ads"],
                profiles=["google-edit"],
                formats=["json"],
            )
        ],
    )


def test_before_approval_saved_artifact_rule_resolves_to_generation(tmp_path: Path) -> None:
    """A complete matching rule now drives inline generation instead of failing."""
    output = tmp_path / "views"
    output.mkdir(mode=0o700)

    notice = google_ads_pause_cli._artifact_notice(
        _before_approval_rule(output), "google-edit", None
    )

    assert notice.outcome == "GENERATE"
    assert notice.reason == "MATCHING_SAVED_RULE"
    assert notice.generation_supported is True
    assert list(output.iterdir()) == []


def test_incomplete_saved_artifact_rule_names_the_retained_plan_command() -> None:
    """The one remaining pre-planning refusal must route the agent truthfully."""
    with pytest.raises(AdhelmError) as incomplete:
        google_ads_pause_cli._artifact_notice(_before_approval_rule(None), "google-edit", None)

    assert incomplete.value.code == "ARTIFACT_CONFIGURATION_REQUIRED"
    assert "not available yet" not in incomplete.value.message
    assert "artifacts google-ads-campaign-pause" in incomplete.value.next_action


def test_post_retention_receipt_reload_corruption_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _action()
    stored = retain_google_ads_campaign_pause_plan(Ledger(tmp_path), action)
    retained = google_ads_pause_workflow.RetainedGoogleAdsCampaignPausePlan(
        action=action, plan_record_digest=stored["record_digest"]
    )
    monkeypatch.setattr(
        google_ads_pause_cli,
        "load_google_ads_campaign_pause_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("corrupt retained result")),
    )
    artifact = google_ads_pause_cli._ArtifactNotice(
        outcome="SUPPRESSED", reason="ARTIFACT_MODE_OFF"
    )
    with pytest.raises(AdhelmError) as uncertain:
        google_ads_pause_cli._receipt_from_retained(Ledger(tmp_path), retained, artifact)
    assert uncertain.value.code == "GOOGLE_ADS_PAUSE_PLAN_RECEIPT_UNVERIFIED"


@pytest.mark.parametrize("change", ["none", "key-after-pair"])
def test_create_composes_real_planner_with_fake_store_and_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The public command retains only the reviewed receipt from real fake-edge planning."""
    monkeypatch.delenv("CI", raising=False)
    store, profile, current = _installed(monkeypatch)
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    monkeypatch.setattr(sessions, "_now", lambda: current[0])
    monkeypatch.setattr(google_ads_pause_workflow, "_now", lambda: current[0])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert "developer-token" not in request.headers
        body = json.loads(request.content)
        current[0] += timedelta(seconds=1)
        if "FROM customer" in body["query"]:
            return httpx.Response(200, json=_identity_response())
        assert "WHERE campaign.id = 42" in body["query"]
        if change == "key-after-pair":
            store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = "c" * 64
        return httpx.Response(200, json=_campaign_response())

    monkeypatch.setattr(
        sessions,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    request = tmp_path / "real-edge-request.json"
    request.write_text(
        json.dumps({"schema_version": 1, "profile_id": profile.profile_id, "campaign_id": "42"}),
        encoding="utf-8",
    )
    result = _invoke(tmp_path, "create", "--input", str(request), "--no-artifacts")
    if change == "none":
        assert result.exit_code == 0, result.output
        assert _payload(result)["data"]["maximum_incremental_spend"]["minor_units"] == 0
        for secret in (_ACCESS, "m56-google-ads-refresh-token"):
            assert secret not in result.output
    else:
        assert result.exit_code == ExitCode.AUTHORIZATION
        assert _code(result) == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
        assert "c" * 64 not in result.output

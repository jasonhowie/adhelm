from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from test_openai_pause import NOW, _Scenario
from typer.testing import CliRunner

import adhelm.pause_workflow as workflow
import adhelm.plan_cli as plan_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OpenAIAccountKeyProfile, OutputPreferences
from adhelm.pause_workflow import OpenAIPauseRequest, prepare_openai_pause
from adhelm.saved_plans import load_openai_pause_plan, retain_openai_pause_plan

_SECRET = "synthetic-provider-secret-never-output"
_BINDING_KEY = b"b" * 32
_SHARING_KEY = b"s" * 32


def profile(**updates: Any) -> OpenAIAccountKeyProfile:
    return OpenAIAccountKeyProfile.model_validate(
        {
            "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
            "profile_id": "openai-edit",
            "purpose": "EDIT",
            "account_ref": "account_fixture",
            "currency": "USD",
            "timezone": "America/Chicago",
            "credential_ref": "synthetic-openai-key",
            **updates,
        }
    )


def request(**updates: Any) -> OpenAIPauseRequest:
    return OpenAIPauseRequest.model_validate(
        {"profile_id": "openai-edit", "campaign_id": "campaign_fixture", **updates}
    )


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> _Scenario:
    value = _Scenario()
    real_client = httpx.Client

    def factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(value.handler), trust_env=False)

    monkeypatch.setattr(workflow.httpx, "Client", factory)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: _SECRET)
    monkeypatch.setattr(
        workflow, "openai_pause_binding_key", lambda *_args, **_kwargs: _BINDING_KEY
    )
    monkeypatch.setattr(plan_cli, "export_key", lambda: _SHARING_KEY)
    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW)
    monkeypatch.setattr(
        plan_cli,
        "prepare_openai_pause",
        lambda directory, value: prepare_openai_pause(directory, value, clock=lambda: NOW),
    )
    return value


def test_workflow_binds_shared_workspace_and_edit_key_without_persistence(
    tmp_path: Path,
    scenario: _Scenario,
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[profile()]))
    action, configuration = prepare_openai_pause(tmp_path, request(), clock=lambda: NOW)
    assert configuration.profiles[0].purpose == "EDIT"
    assert action.plan.profile_ref == "default"
    assert action.binding.profile_ref == "openai-edit"
    assert action.plan.subplans[0].account.auth_profile_ref == "openai-edit"
    assert action.plan.expires_at == NOW + timedelta(seconds=300)
    assert scenario.requests == [
        "GET /v1/ad_account",
        "GET /v1/campaigns",
        "GET /v1/campaigns/campaign_fixture",
    ]
    assert not Ledger(tmp_path).records("changes")
    assert scenario.posts == 0


def test_cli_prepares_catalogs_and_reviews_same_ledger_without_raw_identity(
    tmp_path: Path,
    scenario: _Scenario,
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[profile()]))
    input_file = tmp_path / "request.json"
    input_file.write_text(request().model_dump_json())
    runner = CliRunner()
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", str(input_file)]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "PLAN_REVIEW_NO_EXECUTION_AUTHORITY"
    data = envelope["data"]
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["execution_command"] == "UNRELEASED"
    assert data["review"]["before"]["status"] == "active"
    assert data["review"]["after"]["status"] == "paused"
    assert data["review"]["maximum_incremental_spend"] == {"currency": "USD", "minor_units": 0}
    for private in (
        _SECRET,
        "account_fixture",
        "campaign_fixture",
        "openai-edit",
        "credential-binding:",
    ):
        assert private not in result.stdout
    records = Ledger(tmp_path).records("changes")
    assert [row["record_kind"] for row in records] == ["SEGMENT_STARTED", "EXECUTION_PLAN"]
    records = records[1:]
    assert records[0]["profile_ref"] == "default"
    assert data["provenance"]["record_digest"] == records[0]["record_digest"]
    action, _ = load_openai_pause_plan(Ledger(tmp_path), records[0]["record_digest"])
    assert data["review"]["plan_digest"] == action.plan.plan_digest
    prior_requests = list(scenario.requests)
    listed = runner.invoke(app, ["--data-dir", str(tmp_path), "plan", "catalog"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["total_count"] == 1
    shown = runner.invoke(
        app, ["--data-dir", str(tmp_path), "plan", "show", records[0]["record_digest"]]
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["data"]["review"] == data["review"]
    assert scenario.requests == prior_requests
    assert scenario.posts == 0


@pytest.mark.parametrize(
    "updates",
    [
        {"purpose": "READ"},
        {"currency": None},
        {"credential_source": "EXPLICIT_CI", "credential_ref": "ADHELM_SYNTHETIC_TOKEN"},
    ],
)
def test_invalid_profile_preconditions_do_not_touch_keys_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    updates: dict[str, Any],
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[profile(**updates)]))

    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("private key was touched")

    monkeypatch.setattr(workflow, "credential", blocked)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", blocked)
    with pytest.raises(AdhelmError):
        prepare_openai_pause(tmp_path, request(), clock=lambda: NOW)
    assert scenario.requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("maximum_inventory_records", 1.0),
        ("maximum_inventory_records", True),
        ("maximum_inventory_records", 5001),
        ("expires_in_seconds", 59),
        ("expires_in_seconds", 901),
        ("expires_in_seconds", False),
        ("campaign_id", "../campaign"),
        ("campaign_id", "a?key=secret"),
    ],
)
def test_request_rejects_noncanonical_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        request(**{field: value})


def test_constructed_request_fails_before_configuration_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow, "load_configuration", lambda _: pytest.fail("configuration read"))
    with pytest.raises(AdhelmError) as error:
        prepare_openai_pause(tmp_path, request().model_copy(update={"expires_in_seconds": True}))
    assert error.value.code == "INVALID_PLAN_INPUT"


@pytest.mark.parametrize(
    "field,value", [("currency_code", "EUR"), ("timezone", "UTC"), ("id", "other_account")]
)
def test_remote_identity_drift_fails_before_campaign_reads(
    tmp_path: Path,
    scenario: _Scenario,
    field: str,
    value: str,
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[profile()]))
    scenario.account[field] = value
    with pytest.raises(AdhelmError):
        prepare_openai_pause(tmp_path, request(), clock=lambda: NOW)
    assert scenario.requests == ["GET /v1/ad_account"]
    assert not Ledger(tmp_path).records("changes")


def test_expiry_during_read_is_not_retained(tmp_path: Path, scenario: _Scenario) -> None:
    save_configuration(tmp_path, Configuration(profiles=[profile()]))
    moments = iter([NOW, NOW, NOW + timedelta(seconds=301)])
    with pytest.raises(AdhelmError) as error:
        prepare_openai_pause(tmp_path, request(), clock=lambda: next(moments))
    assert error.value.code == "PLAN_EXPIRED"
    assert not Ledger(tmp_path).records("changes")


def test_output_overflow_does_not_append_or_touch_sharing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
) -> None:
    save_configuration(
        tmp_path,
        Configuration(profiles=[profile()], output=OutputPreferences(max_output_bytes=1024)),
    )
    source = tmp_path / "request.json"
    source.write_text(request().model_dump_json())
    monkeypatch.setattr(plan_cli, "export_key", lambda: pytest.fail("sharing key read"))
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", str(source)]
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert not Ledger(tmp_path).records("changes")


def test_expired_saved_plan_remains_reviewable_without_provider_or_binding_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[profile()]))
    action, _ = prepare_openai_pause(tmp_path, request(), clock=lambda: NOW)
    record = retain_openai_pause_plan(Ledger(tmp_path), action)
    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW + timedelta(days=1))
    monkeypatch.setattr(workflow, "credential", lambda _: pytest.fail("credential read"))
    prior = list(scenario.requests)
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "plan", "show", record["record_digest"]]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["review"]["expired_at_review"] is True
    assert data["execution_authority"] is False
    assert scenario.requests == prior


def test_catalog_does_not_acquire_any_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_cli, "export_key", lambda: pytest.fail("sharing key read"))
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "plan", "catalog"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["plans"] == []


def test_duplicate_json_fails_before_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "duplicate.json"
    source.write_text('{"profile_id":"a","profile_id":"b","campaign_id":"c"}')
    monkeypatch.setattr(
        plan_cli, "prepare_openai_pause", lambda *_args: pytest.fail("workflow entered")
    )
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", str(source)]
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["outcome"] == "FAILED_CLOSED"

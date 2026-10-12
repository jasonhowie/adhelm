"""Adversarial checks for the fixture-only OpenAI pause planning CLI."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_openai_pause import NOW, _action, _credentials, _Scenario
from typer.testing import CliRunner

import adhelm.pause_workflow as workflow
import adhelm.plan_cli as plan_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OpenAIAccountKeyProfile, OutputPreferences
from adhelm.pause_workflow import OpenAIPauseRequest, prepare_openai_pause
from adhelm.plan_views import preview_openai_pause
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction, build_openai_pause_plan
from adhelm.saved_plans import retain_openai_pause_plan

_SECRET = "ultra-review-provider-secret-never-output"
_BINDING_KEY = b"b" * 32
_SHARING_KEY = b"s" * 32
_OUTCOME = "PLAN_REVIEW_NO_EXECUTION_AUTHORITY"
_HTTPX_CLIENT = httpx.Client


def _profile(**updates: Any) -> OpenAIAccountKeyProfile:
    return OpenAIAccountKeyProfile.model_validate(
        {
            "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
            "profile_id": "ultra-edit-profile",
            "purpose": "EDIT",
            "account_ref": "account_fixture",
            "currency": "USD",
            "timezone": "America/Chicago",
            "credential_ref": "ultra-provider-key",
            **updates,
        }
    )


def _request(**updates: Any) -> OpenAIPauseRequest:
    return OpenAIPauseRequest.model_validate(
        {
            "profile_id": "ultra-edit-profile",
            "campaign_id": "campaign_fixture",
            **updates,
        }
    )


def _write_request(directory: Path) -> Path:
    source = directory / "pause-request.json"
    source.write_text(_request().model_dump_json())
    return source


def _workspace_action() -> OpenAIPausePlannedAction:
    """Use only the existing in-memory fixture transport to form one action."""
    legacy, _ = _action(
        _Scenario(),
        _credentials(profile="ultra-credential-profile"),
    )
    return build_openai_pause_plan(
        legacy.binding,
        expires_at=legacy.plan.expires_at,
        workspace_profile_ref="default",
    )


def _install_fixture_transport(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    authorization_headers: list[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("authorization")
        assert header is not None
        authorization_headers.append(header)
        return scenario.handler(request)

    def factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return _HTTPX_CLIENT(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(httpx, "Client", factory)


def test_constructed_request_stops_before_configuration_credentials_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _PrivateCampaignValue:
        def __repr__(self) -> str:
            return "private-constructed-request-sentinel-36ec55c9"

    forged = OpenAIPauseRequest.model_construct(
        schema_version=1,
        profile_id="ultra-edit-profile",
        campaign_id=_PrivateCampaignValue(),
        maximum_inventory_records=1,
        expires_in_seconds=60,
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("closed request validation must precede configuration, keys, and HTTP")

    monkeypatch.setattr(workflow, "pause_configuration", unexpected)
    monkeypatch.setattr(workflow, "credential", unexpected)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", unexpected)
    monkeypatch.setattr(httpx, "Client", unexpected)

    with pytest.raises(AdhelmError) as failure:
        prepare_openai_pause(tmp_path, forged)

    sentinel = "private-constructed-request-sentinel-36ec55c9"
    assert failure.value.code == "INVALID_PLAN_INPUT"
    assert sentinel not in str(failure.value)
    captured = capsys.readouterr()
    assert sentinel not in captured.out and sentinel not in captured.err


def test_model_copy_request_stops_before_configuration_credentials_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = _request().model_copy(update={"maximum_inventory_records": True})

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("model-copy bypass must stop before configuration, keys, and HTTP")

    monkeypatch.setattr(workflow, "pause_configuration", unexpected)
    monkeypatch.setattr(workflow, "credential", unexpected)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", unexpected)
    monkeypatch.setattr(httpx, "Client", unexpected)

    with pytest.raises(AdhelmError) as failure:
        prepare_openai_pause(tmp_path, forged)

    assert failure.value.code == "INVALID_PLAN_INPUT"


def test_recovery_checkpoint_precedes_any_private_key_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    configuration = Configuration(profiles=[_profile()])

    class _DirtyLedger:
        profile_ref = "workspace-ultra-review"

        def __init__(self, directory: Path) -> None:
            assert directory == tmp_path
            events.append("ledger")

        def checkpoint(self) -> dict[str, object]:
            events.append("checkpoint")
            return {}

        def unresolved_starts(self) -> list[dict[str, object]]:
            events.append("unresolved_starts")
            return [{}]

        def unresolved_runs(self) -> list[dict[str, object]]:
            events.append("unresolved_runs")
            return []

    def configuration_read(directory: Path) -> Configuration:
        assert directory == tmp_path
        events.append("configuration")
        return configuration

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("recovery gating must precede private key and HTTP access")

    monkeypatch.setattr(workflow, "pause_configuration", configuration_read)
    monkeypatch.setattr(workflow, "Ledger", _DirtyLedger)
    monkeypatch.setattr(workflow, "credential", unexpected)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", unexpected)
    monkeypatch.setattr(httpx, "Client", unexpected)

    with pytest.raises(AdhelmError) as failure:
        prepare_openai_pause(tmp_path, _request(), clock=lambda: NOW)

    assert failure.value.code == "RECOVERY_REQUIRED"
    assert events == ["configuration", "ledger", "checkpoint", "unresolved_starts"]


def test_one_secret_snapshot_is_used_for_all_reads_and_identity_mismatch_stops_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    scenario = _Scenario()
    headers: list[str] = []
    _install_fixture_transport(monkeypatch, scenario, headers)
    secret_reads = 0
    binding_reads = 0

    def credential_read(*_args: object, **_kwargs: object) -> str:
        nonlocal secret_reads
        secret_reads += 1
        return _SECRET

    def binding_key(*_args: object, **_kwargs: object) -> bytes:
        nonlocal binding_reads
        binding_reads += 1
        return _BINDING_KEY

    monkeypatch.setattr(workflow, "credential", credential_read)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", binding_key)
    prepare_openai_pause(tmp_path, _request(), clock=lambda: NOW)

    assert secret_reads == binding_reads == 1
    assert headers == [f"Bearer {_SECRET}"] * 3
    assert scenario.requests == [
        "GET /v1/ad_account",
        "GET /v1/campaigns",
        "GET /v1/campaigns/campaign_fixture",
    ]

    mismatch_directory = tmp_path / "mismatch"
    save_configuration(mismatch_directory, Configuration(profiles=[_profile()]))
    mismatch = _Scenario()
    mismatch.account["currency_code"] = "EUR"
    mismatch_headers: list[str] = []
    _install_fixture_transport(monkeypatch, mismatch, mismatch_headers)
    with pytest.raises(AdhelmError) as failure:
        prepare_openai_pause(mismatch_directory, _request(), clock=lambda: NOW)

    assert failure.value.code == "IDENTITY_MISMATCH"
    assert mismatch.requests == ["GET /v1/ad_account"]
    assert mismatch_headers == [f"Bearer {_SECRET}"]


def test_expiry_after_slow_binding_key_prevents_provider_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    scenario = _Scenario()
    headers: list[str] = []
    _install_fixture_transport(monkeypatch, scenario, headers)
    moment = NOW

    def clock() -> datetime:
        return moment

    def slow_binding_key(*_args: object, **_kwargs: object) -> bytes:
        nonlocal moment
        moment = NOW + timedelta(seconds=60)
        return _BINDING_KEY

    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: _SECRET)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", slow_binding_key)

    with pytest.raises(AdhelmError) as failure:
        prepare_openai_pause(tmp_path, _request(expires_in_seconds=60), clock=clock)

    assert failure.value.code == "PLAN_EXPIRED"
    assert scenario.requests == []
    assert headers == []
    assert not Ledger(tmp_path).records("changes")


def test_expiry_after_slow_provider_reads_returns_no_retained_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    scenario = _Scenario()
    moment = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal moment
        response = scenario.handler(request)
        if request.url.path == "/v1/campaigns/campaign_fixture":
            moment = NOW + timedelta(seconds=60)
        return response

    def factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return _HTTPX_CLIENT(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: _SECRET)
    monkeypatch.setattr(workflow, "openai_pause_binding_key", lambda *_args: _BINDING_KEY)

    with pytest.raises(AdhelmError) as failure:
        prepare_openai_pause(
            tmp_path,
            _request(expires_in_seconds=60),
            clock=lambda: moment,
        )

    assert failure.value.code == "PLAN_EXPIRED"
    assert scenario.requests == [
        "GET /v1/ad_account",
        "GET /v1/campaigns",
        "GET /v1/campaigns/campaign_fixture",
    ]
    assert not Ledger(tmp_path).records("changes")


def test_output_probe_stops_before_sharing_key_or_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _workspace_action()
    configuration = Configuration(output=OutputPreferences(max_output_bytes=1024))
    source = _write_request(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW)
    monkeypatch.setattr(plan_cli, "prepare_openai_pause", lambda *_args: (action, configuration))

    def unexpected_key() -> bytes:
        calls.append("sharing-key")
        pytest.fail("output probe must precede sharing-key access")

    def unexpected_retain(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("retain")
        pytest.fail("output probe must precede local retention")

    monkeypatch.setattr(plan_cli, "export_key", unexpected_key)
    monkeypatch.setattr(plan_cli, "retain_openai_pause_plan", unexpected_retain)
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", str(source)]
    )

    assert result.exit_code != 0
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == []


def test_ambiguous_local_retention_is_not_reported_as_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _workspace_action()
    configuration = Configuration()
    source = _write_request(tmp_path)
    raw_failure = "private-retention-error-sentinel-3a6b8a5c"

    class _NoWriteLedger:
        def __init__(self, directory: Path) -> None:
            assert directory == tmp_path

    def uncertain_retention(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError(raw_failure)

    monkeypatch.setattr(plan_cli, "now_utc", lambda: NOW)
    monkeypatch.setattr(plan_cli, "prepare_openai_pause", lambda *_args: (action, configuration))
    monkeypatch.setattr(plan_cli, "export_key", lambda: _SHARING_KEY)
    monkeypatch.setattr(plan_cli, "Ledger", _NoWriteLedger)
    monkeypatch.setattr(plan_cli, "retain_openai_pause_plan", uncertain_retention)
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "plan", "openai-pause", "--input", str(source)]
    )

    assert result.exit_code != 0
    output = json.loads(result.stdout)
    assert output["outcome"] == "FAILED_CLOSED"
    assert output["diagnostics"][0]["code"] == "PLAN_RETENTION_UNVERIFIED"
    assert raw_failure not in result.stdout


def test_catalog_and_show_are_bounded_redacted_and_keep_workspace_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _workspace_action()
    save_configuration(tmp_path, Configuration())
    receipt = retain_openai_pause_plan(Ledger(tmp_path), action)

    def no_sharing_key() -> bytes:
        pytest.fail("catalog must not access the sharing key")

    monkeypatch.setattr(plan_cli, "export_key", no_sharing_key)
    runner = CliRunner()
    catalog = runner.invoke(app, ["--data-dir", str(tmp_path), "plan", "catalog"])
    assert catalog.exit_code == 0, catalog.output
    catalog_output = json.loads(catalog.stdout)
    assert catalog_output["outcome"] == _OUTCOME
    assert catalog_output["data"]["total_count"] == 1
    for private in (
        "account_fixture",
        "campaign_fixture",
        "ultra-credential-profile",
        "credential-binding:",
    ):
        assert private not in catalog.stdout

    monkeypatch.setattr(plan_cli, "export_key", lambda: _SHARING_KEY)
    shown = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "plan", "show", receipt["record_digest"]],
    )
    assert shown.exit_code == 0, shown.output
    output = json.loads(shown.stdout)
    review = output["data"]["review"]
    assert output["outcome"] == _OUTCOME
    assert review["profile_ref"] != review["workspace_profile_ref"]
    for private in (
        "account_fixture",
        "campaign_fixture",
        "ultra-credential-profile",
        "default",
        "credential-binding:",
    ):
        assert private not in shown.stdout

    legacy, _ = _action(_Scenario())
    legacy_review = preview_openai_pause(legacy, opaque_key=_SHARING_KEY, now=NOW)
    assert legacy_review.profile_ref == legacy_review.workspace_profile_ref


def test_show_checks_output_bound_before_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _workspace_action()
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    receipt = retain_openai_pause_plan(Ledger(tmp_path), action)

    monkeypatch.setattr(plan_cli, "export_key", lambda: pytest.fail("sharing key read"))
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path), "plan", "show", receipt["record_digest"]],
    )

    assert result.exit_code != 0
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"

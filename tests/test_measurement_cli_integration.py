"""Installed command composition through real parsers and synthetic HTTP, never live APIs."""

import json
from pathlib import Path

import httpx
import pytest
from test_ga4_measurement_workflow import (
    _identity_response,
    _install_transport,
    _stream_response,
)
from test_ga4_measurement_workflow import (
    _profile as ga4_profile,
)
from test_openai_measurement_workflow import _account, _page, _setting
from test_openai_measurement_workflow import _profile as openai_profile
from typer.testing import CliRunner

from adhelm import ga4_measurement_workflow, measurement_cli, openai_measurement_workflow
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.ledger import Ledger
from adhelm.models import Configuration
from adhelm.saved_measurements import load_measurement, measurement_catalog


@pytest.mark.parametrize("provider", ["ga4_admin", "openai_ads"])
def test_command_composes_identity_read_normalization_retention_and_redacted_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    profile = ga4_profile() if provider == "ga4_admin" else openai_profile()
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    calls: list[str] = []
    credential_profiles: list[str] = []

    def getter(selected, *, ci_enabled):
        assert ci_enabled is True
        credential_profiles.append(selected.profile_id)
        return "synthetic-test-credential"

    monkeypatch.setattr(ga4_measurement_workflow, "credential", getter)
    monkeypatch.setattr(openai_measurement_workflow, "credential", getter)
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer synthetic-test-credential"
        if provider == "ga4_admin":
            assert request.url.host == "analyticsadmin.googleapis.com"
            if request.url.path == "/v1beta/properties/123":
                return httpx.Response(200, json=_identity_response())
            if request.url.path == "/v1beta/properties/123/dataStreams":
                return httpx.Response(200, json=_stream_response())
            if request.url.path.endswith("/keyEvents"):
                return httpx.Response(
                    200,
                    json={
                        "keyEvents": [
                            {
                                "name": "properties/123/keyEvents/789",
                                "eventName": "purchase",
                                "deletable": True,
                                "custom": True,
                                "countingMethod": "ONCE_PER_EVENT",
                                "defaultValue": {"numericValue": 25.5, "currencyCode": "USD"},
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/eventCreateRules"):
                return httpx.Response(200, json={"eventCreateRules": []})
            if request.url.path.endswith("/eventEditRules"):
                return httpx.Response(200, json={"eventEditRules": []})
        else:
            assert request.url.host == "api.ads.openai.com"
            if request.url.path == "/v1/ad_account":
                return httpx.Response(200, json=_account())
            if request.url.path == "/v1/conversions/event_settings":
                if request.url.params.get("after") is None:
                    return httpx.Response(200, json=_page([_setting("setting_one")], has_more=True))
                assert request.url.params["after"] == "setting_one"
                return httpx.Response(200, json=_page([_setting("setting_two")]))
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    base = ["--data-dir", str(tmp_path), "measurement"]
    args = (
        ["ga4-admin", profile.profile_id, "456"]
        if provider == "ga4_admin"
        else [
            "openai-conversions",
            profile.profile_id,
            "--page-size",
            "1",
            "--maximum-records",
            "2",
        ]
    )
    runner = CliRunner()
    result = runner.invoke(app, [*base, *args, "--ci-enabled"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)["data"]
    assert receipt["persisted"] is True
    assert len(calls) == (5 if provider == "ga4_admin" else 3)
    assert set(credential_profiles) == {profile.profile_id}
    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    assert observation.read_finished_at >= observation.read_started_at
    assert observation.snapshot.provider == provider
    canonical_history = next((tmp_path / "ledger/changes").glob("*.jsonl")).read_text()
    for private in (
        "synthetic-test-credential",
        "Fixture conversion name",
        "Source name discarded",
        "purchase",
    ):
        assert private not in canonical_history
    calls_before_review = list(calls)
    shown = runner.invoke(app, [*base, "show", receipt["record_digest"], "--limit", "1"])
    assert shown.exit_code == 0, shown.output
    assert calls == calls_before_review
    assert "setting_one" not in shown.output and "ga4_admin.key_event.789" not in shown.output
    data = json.loads(shown.output)["data"]
    assert data["execution_authority"] is False and data["processed_receipt"] == "NOT_EVALUATED"
    if provider == "ga4_admin":
        assert data["rows"][0]["default_value"] == {"numeric_value": "25.5", "currency": "USD"}
    else:
        assert json.loads(shown.output)["next_cursor"] == "1"


def test_later_openai_page_failure_does_not_retain_partial_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[openai_profile()]))
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr(openai_measurement_workflow, "credential", lambda *a, **kw: "fixture-value")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/conversions/event_settings"
        if request.url.params.get("after") is None:
            return httpx.Response(200, json=_page([_setting("setting_one")], has_more=True))
        return httpx.Response(
            200, json=_page([_setting("setting_bad", account_id="another_account")])
        )

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "openai-conversions",
            "openai-conversions-read",
            "--page-size",
            "1",
            "--maximum-records",
            "2",
            "--ci-enabled",
        ],
    )
    assert result.exit_code != 0
    assert not measurement_catalog(Ledger(tmp_path))["measurements"]
    assert "another_account" not in result.output
    assert not Ledger(tmp_path).unresolved_starts()

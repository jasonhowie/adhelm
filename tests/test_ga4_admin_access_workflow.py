"""Synthetic fixed-request coverage for the paired GA4 resource-access diagnostic."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import adhelm.ga4_admin_access_workflow as workflow
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.providers.ga4_admin_access import Ga4AdminAccessClient
from adhelm.transport import SafeHttpClient


def _profile(role: str, **changes: object) -> Profile:
    scope_suffix = "readonly" if role == "READ" else "edit"
    values: dict[str, object] = {
        "provider": "ga4_admin",
        "profile_id": f"admin-{role.lower()}",
        "purpose": role,
        "account_ref": "123",
        "principal_ref": f"principal.{role.lower()}",
        "client_ref": f"client.{role.lower()}",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": f"GA4_{role}_TOKEN",
        "scopes": [f"https://www.googleapis.com/auth/analytics.{scope_suffix}"],
        "currency": "USD",
        "timezone": "America/Chicago",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _configure(directory: Path, *profiles: Profile) -> None:
    save_configuration(
        directory,
        Configuration(
            profiles=list(profiles) or [_profile("READ"), _profile("EDIT")],
            output=OutputPreferences(max_output_bytes=1024),
        ),
    )


def _request(**changes: object) -> workflow.Ga4AdminAccessRequest:
    return workflow.Ga4AdminAccessRequest.model_validate(
        {
            "read_profile_id": "admin-read",
            "edit_profile_id": "admin-edit",
            "stream_id": "456",
            "ci_enabled": True,
            **changes,
        }
    )


def _property(**changes: object) -> dict[str, object]:
    return {
        "name": "properties/123",
        "timeZone": "America/Chicago",
        "currencyCode": "USD",
        "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
        **changes,
    }


def _stream(**changes: object) -> dict[str, object]:
    return {
        "name": "properties/123/dataStreams/456",
        "type": "WEB_DATA_STREAM",
        **changes,
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[str]:
    real_client = httpx.Client
    token_reads: list[str] = []

    def client_factory(*, trust_env: bool, follow_redirects: bool) -> httpx.Client:
        assert trust_env is False and follow_redirects is False
        return real_client(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    def token(profile: Profile, *, ci_enabled: bool) -> str:
        assert ci_enabled is True
        token_reads.append(profile.purpose)
        return f"fixture-{profile.purpose.lower()}"

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)
    monkeypatch.setattr(workflow, "credential", token)
    return token_reads


def _ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "analyticsadmin.googleapis.com"
    assert not request.content
    if request.url.path == "/v1beta/properties/123":
        assert dict(request.url.params) == {
            "fields": "name,timeZone,currencyCode,serviceLevel,deleteTime"
        }
        return httpx.Response(200, json=_property())
    assert request.url.path == "/v1beta/properties/123/dataStreams/456"
    assert dict(request.url.params) == {"fields": "name,type"}
    return httpx.Response(200, json=_stream())


def test_pair_uses_four_fixed_gets_with_separate_single_token_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers["authorization"]))
        return _ok_handler(request)

    token_reads = _install(monkeypatch, handler)
    result = workflow.check_ga4_admin_access(tmp_path, _request())
    assert token_reads == ["READ", "EDIT"]
    assert calls == [
        (path, f"Bearer fixture-{role}")
        for role in ("read", "edit")
        for path in ("/v1beta/properties/123", "/v1beta/properties/123/dataStreams/456")
    ]
    assert result.observed_service_level == "GOOGLE_ANALYTICS_STANDARD"
    assert result.granted_scopes == "NOT_EVALUATED"
    assert result.create_permission == "NOT_PREPROVABLE"
    assert result.mutation_readiness == "NOT_EVIDENCED"
    assert result.observations_atomic is False
    assert result.persisted is False and result.execution_authority is False
    output = result.model_dump_json()
    for private in ("fixture-read", "principal.read", "client.edit", "admin-read", "123", "456"):
        assert private not in output


@pytest.mark.parametrize(
    "profile_changes",
    [
        {"provider": "ga4"},
        {"purpose": "READ"},
        {"account_ref": "999"},
        {"currency": "CAD"},
        {"timezone": "UTC"},
        {"timezone": "not-a-timezone"},
        {"scopes": ["https://www.googleapis.com/auth/analytics.readonly"]},
        {"scopes": ["https://www.googleapis.com/auth/analytics.edit", "extra"]},
        {"credential_ref": "ga4_read_token"},
    ],
)
def test_invalid_pair_stops_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_changes: dict[str, object]
) -> None:
    _configure(tmp_path, _profile("READ"), _profile("EDIT", **profile_changes))
    calls = _install(monkeypatch, lambda _request: pytest.fail("HTTP must not run"))
    with pytest.raises(AdhelmError):
        workflow.check_ga4_admin_access(tmp_path, _request())
    assert calls == []


def test_ci_must_be_enabled_for_both_profiles_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls = _install(monkeypatch, lambda _request: pytest.fail("HTTP must not run"))
    with pytest.raises(AdhelmError, match="Explicit CI"):
        workflow.check_ga4_admin_access(tmp_path, _request(ci_enabled=False))
    assert calls == []


@pytest.mark.parametrize("role", ["read", "edit"])
def test_provider_denial_is_role_specific_and_does_not_claim_create_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    _configure(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if calls[-1] == f"Bearer fixture-{role}":
            return httpx.Response(403, text="secret-provider-response")
        return _ok_handler(request)

    tokens = _install(monkeypatch, handler)
    with pytest.raises(AdhelmError) as error:
        workflow.check_ga4_admin_access(tmp_path, _request())
    assert error.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert role.upper() in str(error.value) and "secret-provider" not in str(error.value)
    assert "create" not in str(error.value).lower()
    assert tokens == (["READ"] if role == "read" else ["READ", "EDIT"])


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("property", {"name": "properties/999"}),
        ("property", {"currencyCode": "CAD"}),
        ("property", {"deleteTime": "2026-09-05T00:00:00Z"}),
        ("stream", {"name": "properties/999/dataStreams/456"}),
        ("stream", {"name": "properties/123/dataStreams/789"}),
        ("stream", {"type": "ANDROID_APP_DATA_STREAM"}),
        ("stream", {"webStreamData": None}),
    ],
)
def test_wrong_identity_stops_without_edit_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, changes: dict[str, object]
) -> None:
    _configure(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/123"):
            return httpx.Response(
                200, json=_property(**changes) if kind == "property" else _property()
            )
        return httpx.Response(200, json=_stream(**changes) if kind == "stream" else _stream())

    calls = _install(monkeypatch, handler)
    with pytest.raises(AdhelmError):
        workflow.check_ga4_admin_access(tmp_path, _request())
    assert calls == ["READ"]


def test_config_drift_after_read_prevents_edit_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        response = _ok_handler(request)
        if request.url.path.endswith("/456"):
            save_configuration(
                tmp_path,
                Configuration(profiles=[_profile("READ"), _profile("EDIT", account_ref="999")]),
                replace=True,
            )
        return response

    calls = _install(monkeypatch, handler)
    with pytest.raises(AdhelmError) as error:
        workflow.check_ga4_admin_access(tmp_path, _request())
    assert error.value.code == "CONFIGURATION_DRIFT"
    assert calls == ["READ"]


def test_same_token_from_distinct_refs_does_not_run_edit_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _ok_handler(request)

    _install(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: "same-secret")
    with pytest.raises(AdhelmError) as error:
        workflow.check_ga4_admin_access(tmp_path, _request())
    assert error.value.code == "GA4_DISTINCT_CREDENTIALS_REQUIRED" and len(calls) == 2


def test_missing_service_level_is_unknown_without_cap_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/123"):
            prop = _property()
            prop.pop("serviceLevel")
            return httpx.Response(200, json=prop)
        return _ok_handler(request)

    _install(monkeypatch, handler)
    assert (
        workflow.check_ga4_admin_access(tmp_path, _request()).observed_service_level
        == "NOT_AVAILABLE"
    )


@pytest.mark.parametrize("invalid", ["../123", "１２３", "123?x=1", "123\n"])
def test_native_target_validation_precedes_token_access(invalid: str) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("HTTP"))
    ) as client:
        adapter = Ga4AdminAccessClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: pytest.fail("credential"),
        )
        with pytest.raises(AdhelmError):
            adapter.read_identity(property_id=invalid, stream_id="456")


def test_installed_command_is_bounded_non_authorizing_and_key_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    _install(monkeypatch, _ok_handler)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-access",
            "--read-profile",
            "admin-read",
            "--edit-profile",
            "admin-edit",
            "--stream-id",
            "456",
            "--ci-enabled",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(result.stdout.encode()) <= 1024
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "ACCESS_CHECK_ONLY"
    assert envelope["data"]["execution_authority"] is False
    assert not (tmp_path / "changes.jsonl").exists()


def test_output_preflight_precedes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls = _install(monkeypatch, lambda _request: pytest.fail("HTTP"))
    monkeypatch.setattr("adhelm.measurement_cli._fits", lambda *_args, **_kwargs: False)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-access",
            "--read-profile",
            "admin-read",
            "--edit-profile",
            "admin-edit",
            "--stream-id",
            "456",
            "--ci-enabled",
        ],
    )
    assert result.exit_code == 2 and "OUTPUT_LIMIT_EXCEEDED" in result.output
    assert calls == []

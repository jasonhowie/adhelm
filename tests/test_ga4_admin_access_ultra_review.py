"""Independent adversarial review of the paired GA4 Admin access diagnostic."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import adhelm.ga4_admin_access_workflow as workflow
import adhelm.measurement_cli as measurement_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile
from adhelm.providers.ga4_admin_access import Ga4AdminAccessClient
from adhelm.transport import SafeHttpClient

_READ_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"


def _profile(purpose: str, **changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": f"ga4-{purpose.lower()}",
        "provider": "ga4_admin",
        "account_ref": "123",
        "principal_ref": f"principal.{purpose.lower()}",
        "client_ref": f"client.{purpose.lower()}",
        "scopes": [_READ_SCOPE if purpose == "READ" else _EDIT_SCOPE],
        "purpose": purpose,
        "currency": "USD",
        "timezone": "America/Chicago",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": f"GA4_{purpose}_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client = httpx.Client

    def client_factory(*_args: object, **kwargs: object) -> httpx.Client:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)


def _property(*, service_level: str | None = "GOOGLE_ANALYTICS_STANDARD") -> dict[str, object]:
    result: dict[str, object] = {
        "name": "properties/123",
        "timeZone": "America/Chicago",
        "currencyCode": "USD",
    }
    if service_level is not None:
        result["serviceLevel"] = service_level
    return result


def _stream() -> dict[str, object]:
    return {
        "name": "properties/123/dataStreams/456",
        "type": "WEB_DATA_STREAM",
    }


def _request() -> workflow.Ga4AdminAccessRequest:
    return workflow.Ga4AdminAccessRequest(
        read_profile_id="ga4-read", edit_profile_id="ga4-edit", stream_id="456", ci_enabled=True
    )


def _configure(root: Path, read: Profile | None = None, edit: Profile | None = None) -> None:
    save_configuration(
        root,
        Configuration(profiles=[read or _profile("READ"), edit or _profile("EDIT")]),
    )


def test_pair_uses_only_exact_target_gets_and_returns_no_private_response_or_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls: list[tuple[str, str, tuple[tuple[str, str], ...], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.path,
                tuple(request.url.params.multi_items()),
                request.headers["authorization"],
            )
        )
        assert request.url.host == "analyticsadmin.googleapis.com"
        assert request.content == b""
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property())
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(200, json=_stream())
        raise AssertionError(f"unexpected fixed-read path: {request.url.path}")

    _install_transport(monkeypatch, handler)
    tokens = {"ga4-read": "read-secret-token", "ga4-edit": "edit-secret-token"}
    monkeypatch.setattr(
        workflow, "credential", lambda profile, **_kwargs: tokens[profile.profile_id]
    )

    result = workflow.check_ga4_admin_access(tmp_path, _request())

    assert calls == [
        (
            "GET",
            "/v1beta/properties/123",
            (("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),),
            "Bearer read-secret-token",
        ),
        (
            "GET",
            "/v1beta/properties/123/dataStreams/456",
            (("fields", "name,type"),),
            "Bearer read-secret-token",
        ),
        (
            "GET",
            "/v1beta/properties/123",
            (("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),),
            "Bearer edit-secret-token",
        ),
        (
            "GET",
            "/v1beta/properties/123/dataStreams/456",
            (("fields", "name,type"),),
            "Bearer edit-secret-token",
        ),
    ]
    assert result.profile_separation == "VERIFIED_LOCALLY"
    assert result.read_profile_resource_access == result.edit_profile_resource_access == "VERIFIED"
    assert result.observed_service_level == "GOOGLE_ANALYTICS_STANDARD"
    assert result.granted_scopes == "NOT_EVALUATED"
    assert result.create_permission == "NOT_PREPROVABLE"
    assert result.key_event_inventory == "NOT_EVALUATED"
    assert result.mutation_readiness == "NOT_EVIDENCED"
    assert result.execution_authority is result.remote_mutations is result.persisted is False
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    for private in (
        "read-secret-token",
        "edit-secret-token",
        "properties/123",
    ):
        assert private not in rendered


def test_absent_service_level_is_explicitly_not_available_not_inferred_standard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property(service_level=None))
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(200, json=_stream())
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", lambda profile, **_kwargs: profile.profile_id)

    assert (
        workflow.check_ga4_admin_access(tmp_path, _request()).observed_service_level
        == "NOT_AVAILABLE"
    )


def test_same_resolved_credential_blocks_before_the_edit_profile_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property())
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(200, json=_stream())
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    credential_calls: list[str] = []

    def same_token(profile: Profile, **_kwargs: object) -> str:
        credential_calls.append(profile.profile_id)
        return "same-secret-token"

    monkeypatch.setattr(workflow, "credential", same_token)
    with pytest.raises(AdhelmError) as raised:
        workflow.check_ga4_admin_access(tmp_path, _request())

    assert raised.value.code == "GA4_DISTINCT_CREDENTIALS_REQUIRED"
    assert credential_calls == ["ga4-read", "ga4-edit"]
    assert calls == ["/v1beta/properties/123", "/v1beta/properties/123/dataStreams/456"]
    assert "same-secret-token" not in str(raised.value)


def test_same_profile_role_request_fails_before_any_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("READ")]))
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("provider egress")),
    )
    request = workflow.Ga4AdminAccessRequest(
        read_profile_id="ga4-read", edit_profile_id="GA4-READ", stream_id="456", ci_enabled=True
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.check_ga4_admin_access(tmp_path, request)

    assert raised.value.code == "GA4_DISTINCT_PROFILES_REQUIRED"


def test_configuration_drift_after_read_observation_blocks_edit_credential_and_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _profile("READ")
    edit = _profile("EDIT")
    _configure(tmp_path, read, edit)
    calls: list[str] = []
    credentials: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property())
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            save_configuration(
                tmp_path,
                Configuration(profiles=[read, _profile("EDIT", client_ref="client.changed")]),
                replace=True,
            )
            return httpx.Response(200, json=_stream())
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)

    def credential_value(profile: Profile, **_kwargs: object) -> str:
        credentials.append(profile.profile_id)
        return f"{profile.profile_id}-token"

    monkeypatch.setattr(workflow, "credential", credential_value)
    with pytest.raises(AdhelmError) as raised:
        workflow.check_ga4_admin_access(tmp_path, _request())

    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert credentials == ["ga4-read"]
    assert calls == ["/v1beta/properties/123", "/v1beta/properties/123/dataStreams/456"]


def test_malformed_target_stops_before_token_or_provider_read() -> None:
    calls: list[str] = []
    transport = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(AssertionError("provider egress"))
            ),
            trust_env=False,
        ),
        {"analyticsadmin.googleapis.com"},
        read_attempts=1,
    )
    client = Ga4AdminAccessClient(transport, lambda: calls.append("token") or "token")

    with pytest.raises(AdhelmError) as raised:
        client.read_identity(property_id="123/../456", stream_id="456")

    assert raised.value.code == "INVALID_WORKFLOW_INPUT"
    assert calls == []


def test_default_client_query_parameters_are_rejected_before_token_or_http() -> None:
    calls: list[str] = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(AssertionError("provider egress"))
        ),
        params={"unexpected": "injected"},
        trust_env=False,
    ) as raw_client:
        transport = SafeHttpClient(
            raw_client,
            {"analyticsadmin.googleapis.com"},
            read_attempts=1,
        )
        with pytest.raises(AdhelmError) as raised:
            Ga4AdminAccessClient(transport, lambda: calls.append("token") or "token")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert calls == []


@pytest.mark.parametrize(
    ("path", "body", "private"),
    [
        (
            "/v1beta/properties/123",
            {
                **_property(),
                "displayName": "private customer property display name",
            },
            "private customer property display name",
        ),
        (
            "/v1beta/properties/123/dataStreams/456",
            {
                **_stream(),
                "webStreamData": {
                    "defaultUri": "https://customer.example/private?email=person@example.test"
                },
            },
            "customer.example",
        ),
    ],
)
def test_response_outside_fixed_projection_fails_closed_without_private_reflection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, object],
    private: str,
) -> None:
    _configure(tmp_path)
    calls: list[str] = []
    credential_reads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == path:
            return httpx.Response(200, json=body)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_property())
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(200, json=_stream())
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)

    def token(profile: Profile, **_kwargs: object) -> str:
        credential_reads.append(profile.profile_id)
        return f"{profile.profile_id}-token"

    monkeypatch.setattr(workflow, "credential", token)
    with pytest.raises(AdhelmError) as raised:
        workflow.check_ga4_admin_access(tmp_path, _request())

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert credential_reads == ["ga4-read"]
    assert calls == (
        ["/v1beta/properties/123"]
        if path == "/v1beta/properties/123"
        else ["/v1beta/properties/123", "/v1beta/properties/123/dataStreams/456"]
    )
    assert private not in str(raised.value)


def test_constructed_non_boolean_ci_request_is_revalidated_before_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    request = workflow.Ga4AdminAccessRequest.model_construct(
        schema_version=1,
        read_profile_id="ga4-read",
        edit_profile_id="ga4-edit",
        stream_id="456",
        ci_enabled=1,
    )
    credential_reads: list[str] = []
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda profile, **_kwargs: credential_reads.append(profile.profile_id) or "token",
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("provider egress")),
    )

    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        with pytest.raises(AdhelmError) as raised:
            workflow.check_ga4_admin_access(tmp_path, request)

    assert raised.value.code == "INVALID_WORKFLOW_INPUT"
    assert credential_reads == []


def test_installed_discovery_schema_and_option_only_access_command_are_closed() -> None:
    runner = CliRunner()
    discovered = runner.invoke(app, ["discover"])
    assert discovered.exit_code == 0, discovered.output
    discovery = json.loads(discovered.stdout)
    assert "measurement ga4-admin-access" in discovery["data"]["commands"]
    assert "measurement-ga4-access" in discovery["data"]["schemas"]

    schema_result = runner.invoke(app, ["schema", "measurement-ga4-access"])
    assert schema_result.exit_code == 0, schema_result.output
    schema = json.loads(schema_result.stdout)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["read_profile_id", "edit_profile_id", "stream_id"]
    assert schema["properties"]["ci_enabled"]["type"] == "boolean"
    assert schema["properties"]["stream_id"]["pattern"] == "^[0-9]{1,20}$"

    missing_option = runner.invoke(
        app,
        [
            "measurement",
            "ga4-admin-access",
            "--read-profile",
            "read",
            "--edit-profile",
            "edit",
        ],
    )
    assert missing_option.exit_code == 2

    positional_target = runner.invoke(
        app,
        ["measurement", "ga4-admin-access", "read", "edit", "456"],
    )
    assert positional_target.exit_code == 2


def test_cli_output_preflight_blocks_before_sharing_key_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    key_reads: list[str] = []
    credential_reads: list[str] = []
    monkeypatch.setattr(
        measurement_cli,
        "export_key",
        lambda: key_reads.append("sharing-key") or (_ for _ in ()).throw(AssertionError("key")),
    )
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda profile, **_kwargs: (
            credential_reads.append(profile.profile_id)
            or (_ for _ in ()).throw(AssertionError("credential"))
        ),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("provider egress")),
    )
    monkeypatch.setattr(measurement_cli, "_fits", lambda *_args, **_kwargs: False)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-access",
            "--read-profile",
            "ga4-read",
            "--edit-profile",
            "ga4-edit",
            "--stream-id",
            "456",
            "--ci-enabled",
        ],
    )

    assert result.exit_code == 2
    assert "OUTPUT_LIMIT_EXCEEDED" in result.output
    assert key_reads == credential_reads == []

"""Adversarial boundaries for the fixed GA4 Admin measurement read workflow."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.ga4_measurement_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile

_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "ga4-admin-read",
        "provider": "ga4_admin",
        "account_ref": "123",
        "principal_ref": "principal.fixture",
        "client_ref": "client.fixture",
        "scopes": [_SCOPE],
        "purpose": "READ",
        "currency": "USD",
        "timezone": "America/Chicago",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": "GA4_ADMIN_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client = httpx.Client

    def client_factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)


def _identity_response() -> dict[str, str]:
    return {"name": "properties/123", "timeZone": "America/Chicago", "currencyCode": "USD"}


def _stream_response() -> dict[str, object]:
    return {
        "dataStreams": [
            {
                "name": "properties/123/dataStreams/456",
                "type": "WEB_DATA_STREAM",
            }
        ]
    }


def test_public_ga4_request_schema_is_versioned_and_preserves_defaults() -> None:
    request = workflow.Ga4AdminReadRequest(profile_id="ga4-admin-read", stream_id="456")
    assert request.schema_version == 1
    assert request.ci_enabled is False
    assert (
        workflow.Ga4AdminReadRequest.model_json_schema()["properties"]["schema_version"]["const"]
        == 1
    )


def test_nonboolean_ci_opt_in_fails_before_configuration_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[str] = []

    def unexpected_configuration(_: Path) -> Configuration:
        touched.append("configuration")
        raise AssertionError("invalid CI input reached configuration")

    monkeypatch.setattr(workflow, "load_configuration", unexpected_configuration)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("HTTP request")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.validate_ga4_admin_read(
            tmp_path,
            "ga4-admin-read",
            stream_id="456",
            ci_enabled="true",  # type: ignore[arg-type]
        )

    assert raised.value.code == "INVALID_WORKFLOW_INPUT"
    assert touched == []


def test_public_preflight_returns_detached_profile_without_key_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("HTTP request")),
    )

    profile = workflow.validate_ga4_admin_read(
        tmp_path,
        "ga4-admin-read",
        stream_id="456",
    )

    assert profile.profile_id == "ga4-admin-read"
    assert profile.account_ref == "123"
    assert profile.scopes == [_SCOPE]


@pytest.mark.parametrize("field", ["principal_ref", "client_ref"])
def test_forged_profile_identity_fields_fail_before_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    forged_profile = _profile().model_copy(update={field: "invalid identity value"})
    forged_configuration = Configuration(profiles=[_profile()]).model_copy(
        update={"profiles": [forged_profile]}
    )
    monkeypatch.setattr(workflow, "load_configuration", lambda _root: forged_configuration)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("HTTP request")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin(
            tmp_path,
            "ga4-admin-read",
            stream_id="456",
            fingerprint_key=b"f" * 32,
        )

    assert raised.value.code == "CONFIGURATION_INVALID"


def test_forged_pathlike_property_fails_before_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged_profile = _profile().model_copy(update={"account_ref": "123/../999"})
    forged_configuration = Configuration(profiles=[_profile()]).model_copy(
        update={"profiles": [forged_profile]}
    )
    monkeypatch.setattr(workflow, "load_configuration", lambda _root: forged_configuration)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("HTTP request")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin(
            tmp_path,
            "ga4-admin-read",
            stream_id="456",
            fingerprint_key=b"f" * 32,
        )

    assert raised.value.code == "INVALID_GA4_PROPERTY"


def test_all_session_requests_use_one_credential_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    headers: list[str] = []
    credential_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers["authorization"])
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_identity_response())
        if request.url.path == "/v1beta/properties/123/dataStreams":
            return httpx.Response(200, json=_stream_response())
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": []})
        if request.url.path.endswith("/eventEditRules"):
            return httpx.Response(200, json={"eventEditRules": []})
        raise AssertionError(request.url.path)

    def credential_once(profile: Profile, *, ci_enabled: bool) -> str:
        nonlocal credential_calls
        credential_calls += 1
        assert profile.profile_id == "ga4-admin-read"
        assert ci_enabled is False
        if credential_calls > 1:
            raise AssertionError("workflow reread a credential during one session")
        return "fixed-session-token"

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", credential_once)
    inventory = workflow.read_ga4_admin(
        tmp_path,
        "ga4-admin-read",
        stream_id="456",
        fingerprint_key=b"f" * 32,
    )

    assert credential_calls == 1
    assert headers == ["Bearer fixed-session-token"] * 5
    assert inventory.identity.property_client_ref == "123"
    assert inventory.identity.web_stream_client_ref == "456"


@pytest.mark.parametrize("unsafe_token", ["token space", "token\x7f", "token\u0100"])
def test_nonprintable_or_space_credential_fails_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_token: str
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    calls: list[str] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("unsafe credential reached HTTP")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: unsafe_token)

    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin(
            tmp_path,
            "ga4-admin-read",
            stream_id="456",
            fingerprint_key=b"f" * 32,
        )

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert unsafe_token not in str(raised.value)
    assert calls == []


def test_late_inventory_failure_returns_no_partial_result_or_raw_provider_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    calls: list[str] = []
    private_text = "https://customer.example/private?email=person@example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_identity_response())
        if request.url.path == "/v1beta/properties/123/dataStreams":
            return httpx.Response(200, json=_stream_response())
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": []})
        if request.url.path.endswith("/eventEditRules"):
            return httpx.Response(500, content=private_text.encode())
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: "fixture-token")

    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin(
            tmp_path,
            "ga4-admin-read",
            stream_id="456",
            fingerprint_key=b"f" * 32,
        )

    assert raised.value.code == "PROVIDER_REQUEST_FAILED"
    assert private_text not in str(raised.value)
    assert calls == [
        "/v1beta/properties/123",
        "/v1beta/properties/123/dataStreams",
        "/v1alpha/properties/123/keyEvents",
        "/v1alpha/properties/123/dataStreams/456/eventCreateRules",
        "/v1alpha/properties/123/dataStreams/456/eventEditRules",
    ]

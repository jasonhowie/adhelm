"""End-to-end boundaries for the bounded GA4 Admin read workflow."""

from __future__ import annotations

import json
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


def _configure(root: Path, profile: Profile) -> None:
    save_configuration(root, Configuration(profiles=[profile]))


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client = httpx.Client

    def client_factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr("adhelm.ga4_measurement_workflow.httpx.Client", client_factory)


def _identity_response(
    *, property_name: str = "properties/123", currency: str = "USD"
) -> dict[str, str]:
    return {"name": property_name, "timeZone": "America/Chicago", "currencyCode": currency}


def _stream_response(*, stream_id: str = "456") -> dict[str, object]:
    return {
        "dataStreams": [
            {
                "name": f"properties/123/dataStreams/{stream_id}",
                "type": "WEB_DATA_STREAM",
            }
        ]
    }


def test_read_uses_one_fixed_session_and_observed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    calls: list[str] = []
    credentials: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.host == "analyticsadmin.googleapis.com"
        assert request.headers["authorization"] == "Bearer fixture-token"
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
                        }
                    ]
                },
            )
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": []})
        if request.url.path.endswith("/eventEditRules"):
            return httpx.Response(200, json={"eventEditRules": []})
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)

    def fixture_credential(profile: Profile, *, ci_enabled: bool) -> str:
        credentials.append(profile)
        assert ci_enabled is True
        return "fixture-token"

    monkeypatch.setattr(workflow, "credential", fixture_credential)
    inventory = workflow.read_ga4_admin(
        tmp_path,
        "ga4-admin-read",
        stream_id="456",
        fingerprint_key=b"f" * 32,
        ci_enabled=True,
    )

    assert calls == [
        "/v1beta/properties/123",
        "/v1beta/properties/123/dataStreams",
        "/v1alpha/properties/123/keyEvents",
        "/v1alpha/properties/123/dataStreams/456/eventCreateRules",
        "/v1alpha/properties/123/dataStreams/456/eventEditRules",
    ]
    assert credentials and all(profile.profile_id == "ga4-admin-read" for profile in credentials)
    assert inventory.identity.property_ref == "ga4_admin.property.123"
    assert inventory.identity.web_stream_ref == "ga4_admin.web_stream.456"
    assert inventory.key_events[0].resource_ref == "ga4_admin.key_event.789"


def test_profile_purpose_and_scope_fail_before_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def unexpected_request(_: httpx.Request) -> httpx.Response:
        calls.append("http")
        raise AssertionError("invalid profile escaped")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    for profile in (_profile(purpose="EDIT"), _profile(scopes=[_SCOPE, "scope.extra"])):
        root = tmp_path / profile.purpose / str(len(profile.scopes))
        _configure(root, profile)
        with pytest.raises(AdhelmError) as raised:
            workflow.read_ga4_admin(
                root, "ga4-admin-read", stream_id="456", fingerprint_key=b"f" * 32
            )
        assert raised.value.code in {"READ_PROFILE_REQUIRED", "READ_SCOPE_REQUIRED"}
    assert calls == []


def test_public_input_and_fingerprint_key_fail_before_configuration_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid public input escaped")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential read")),
    )
    with pytest.raises(AdhelmError) as invalid_stream:
        workflow.read_ga4_admin(
            tmp_path, "ga4-admin-read", stream_id="not-a-stream", fingerprint_key=b"f" * 32
        )
    assert invalid_stream.value.code == "INVALID_WORKFLOW_INPUT"
    with pytest.raises(AdhelmError) as invalid_key:
        workflow.read_ga4_admin(
            tmp_path, "ga4-admin-read", stream_id="456", fingerprint_key=b"short"
        )
    assert invalid_key.value.code == "FINGERPRINT_KEY_REQUIRED"


@pytest.mark.parametrize(
    ("property_name", "currency", "stream", "expected_code"),
    [
        ("properties/999", "USD", "456", "IDENTITY_MISMATCH"),
        ("properties/123", "CAD", "456", "IDENTITY_MISMATCH"),
        ("properties/123", "USD", "999", "NOT_CONFIGURED_OPTIONAL"),
    ],
)
def test_identity_property_currency_and_stream_mismatches_stop_before_admin_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    property_name: str,
    currency: str,
    stream: str,
    expected_code: str,
) -> None:
    _configure(tmp_path, _profile())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(
                200, json=_identity_response(property_name=property_name, currency=currency)
            )
        if request.url.path == "/v1beta/properties/123/dataStreams":
            return httpx.Response(200, json=_stream_response(stream_id=stream))
        raise AssertionError("Admin inventory must not run after identity failure")

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: "fixture-token")
    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin(
            tmp_path, "ga4-admin-read", stream_id="456", fingerprint_key=b"f" * 32
        )
    assert raised.value.code == expected_code
    assert all("/v1alpha/" not in call for call in calls)


def test_admin_page_failure_is_sanitized_and_does_not_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    calls: list[str] = []
    private_value = "https://customer.example/private?email=person@example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_identity_response())
        if request.url.path == "/v1beta/properties/123/dataStreams":
            return httpx.Response(200, json=_stream_response())
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(500, content=private_value.encode())
        raise AssertionError("workflow continued after failed Admin page")

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: "fixture-token")
    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin(
            tmp_path, "ga4-admin-read", stream_id="456", fingerprint_key=b"f" * 32
        )
    assert raised.value.code == "PROVIDER_REQUEST_FAILED"
    assert private_value not in str(raised.value)
    assert calls[-1].endswith("/keyEvents")


def test_admin_rule_content_never_enters_inventory_or_error_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    private_value = "https://customer.example/private?email=person@example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(200, json=_identity_response())
        if request.url.path == "/v1beta/properties/123/dataStreams":
            return httpx.Response(200, json=_stream_response())
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(
                200,
                json={
                    "eventCreateRules": [
                        {
                            "name": "properties/123/dataStreams/456/eventCreateRules/789",
                            "destinationEvent": "purchase_complete",
                            "eventConditions": [
                                {
                                    "field": "eventName",
                                    "comparisonType": "EQUALS",
                                    "value": private_value,
                                    "negated": False,
                                }
                            ],
                            "sourceCopyParameters": False,
                            "parameterMutations": [],
                        }
                    ]
                },
            )
        if request.url.path.endswith("/eventEditRules"):
            return httpx.Response(200, json={"eventEditRules": []})
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: "fixture-token")
    inventory = workflow.read_ga4_admin(
        tmp_path, "ga4-admin-read", stream_id="456", fingerprint_key=b"f" * 32
    )
    rendered = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
    assert private_value not in rendered
    assert "customer.example" not in rendered

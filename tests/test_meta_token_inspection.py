"""Fixed token-metadata reads with synthetic credentials and mock HTTP only."""

from __future__ import annotations

import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import adhelm.meta_token_inspection as inspection
from adhelm.errors import AdhelmError
from adhelm.meta_token_inspection import MetaTokenInspectionCredentials, inspect_meta_token

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_VALUE = MetaTokenInspectionCredentials(
    "meta-edit", "synthetic-subject-secret", "synthetic-inspector-secret", b"b" * 32
)


def _response(**updates: Any) -> dict[str, Any]:
    return {
        "data": {
            "app_id": "12345",
            "user_id": "54321",
            "is_valid": True,
            "application": "Synthetic Test App",
            "type": "USER",
            "scopes": ["public_profile", "ads_management"],
            "issued_at": int((_NOW - timedelta(hours=1)).timestamp()),
            "expires_at": int((_NOW + timedelta(hours=1)).timestamp()),
            "data_access_expires_at": int((_NOW + timedelta(days=1)).timestamp()),
            **updates,
        }
    }


def _install(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any] | None = None
) -> list[httpx.Request]:
    calls: list[httpx.Request] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "graph.facebook.com"
        assert request.url.path == "/v26.0/debug_token"
        assert dict(request.url.params) == {"input_token": _VALUE.subject_token}
        assert request.headers["authorization"] == "Bearer " + _VALUE.inspector_token
        assert request.content == b""
        return httpx.Response(200, json=_response() if response is None else response)

    def factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(inspection.httpx, "Client", factory)
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    return calls


def _inspect(supplier: Any = lambda: _VALUE) -> Any:
    return inspect_meta_token(
        credential_supplier=supplier,
        expected_client_application_ref="12345",
        required_scope="ads_management",
    )


def test_fixed_inspection_observes_current_app_principal_and_actual_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch)
    result = _inspect()
    assert len(calls) == 1
    assert result.client_application_ref == "12345"
    assert result.scopes == ("ads_management", "public_profile")
    assert result.principal_ref.startswith("meta_token_principal_v1.")
    assert "54321" not in result.principal_ref
    assert result.credential_binding_ref.startswith("credential-binding:sha256:")
    assert result.expires_at == _NOW + timedelta(hours=1)
    assert _VALUE.subject_token not in repr(result)
    assert _VALUE.inspector_token not in repr(_VALUE)
    assert not hasattr(result, "direct_grant")


@pytest.mark.parametrize(
    "updates",
    [
        {"is_valid": False},
        {"is_valid": 1},
        {"app_id": "999"},
        {"user_id": "١٢٣"},
        {"scopes": ["ads_read"]},
        {"scopes": ["ads_management", "ads_management"]},
        {"type": "PAGE"},
        {"expires_at": 0},
        {"expires_at": int(_NOW.timestamp())},
        {"expires_at": True},
        {"data_access_expires_at": 0},
        {"profile_id": "777"},
        {"metadata": {"unexpected": "source"}},
        {"scopes": ["ads_management\n"]},
        {"issued_at": int(_NOW.timestamp()) + 1},
    ],
)
def test_invalid_or_unreviewed_identity_metadata_fails_without_retry(
    monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any]
) -> None:
    calls = _install(monkeypatch, _response(**updates))
    with pytest.raises(AdhelmError) as error:
        _inspect()
    assert len(calls) == 1
    assert error.value.code.startswith("META_TOKEN_")
    assert _VALUE.subject_token not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject_token", "changed-subject"),
        ("inspector_token", "changed-inspector"),
        ("binding_key", b"r" * 32),
        ("profile_ref", "other-profile"),
    ],
)
def test_credential_change_during_read_invalidates_observation(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    calls = _install(monkeypatch)
    values = iter([_VALUE, replace(_VALUE, **{field: value})])
    with pytest.raises(AdhelmError) as error:
        _inspect(lambda: next(values))
    assert error.value.code == "META_TOKEN_CREDENTIAL_CHANGED"
    assert len(calls) == 1


def test_transport_exception_cannot_expose_tokens_in_public_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError(str(request.url) + _VALUE.inspector_token, request=request)

    monkeypatch.setattr(
        inspection.httpx,
        "Client",
        lambda **_k: real_client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    with pytest.raises(AdhelmError) as error:
        _inspect()
    public = "".join(traceback.format_exception(error.value))
    assert _VALUE.subject_token not in public
    assert _VALUE.inspector_token not in public
    assert len(calls) == 1


@pytest.mark.parametrize("seconds", [-1, 31])
def test_rollback_or_stale_response_cannot_become_an_observation(
    monkeypatch: pytest.MonkeyPatch, seconds: int
) -> None:
    calls = _install(monkeypatch)
    moments = iter([_NOW, _NOW + timedelta(seconds=seconds)])
    monkeypatch.setattr(inspection, "_now", lambda: next(moments))
    with pytest.raises(AdhelmError) as error:
        _inspect()
    assert error.value.code == "META_TOKEN_OBSERVATION_EXPIRED"
    assert len(calls) == 1


def test_malformed_credentials_fail_before_http_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inspection.httpx, "Client", lambda **_k: pytest.fail("HTTP constructed"))
    with pytest.raises(AdhelmError):
        _inspect(lambda: replace(_VALUE, subject_token="secret\n"))

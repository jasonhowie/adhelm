"""Independent fake-only boundary review for Meta token inspection."""

from __future__ import annotations

import hashlib
import hmac
import json
import traceback
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import adhelm.meta_token_inspection as inspection
from adhelm.errors import AdhelmError
from adhelm.meta_token_inspection import MetaTokenInspectionCredentials, inspect_meta_token

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_CREDENTIALS = MetaTokenInspectionCredentials(
    profile_ref="meta-edit",
    subject_token="synthetic-subject-token",
    inspector_token="synthetic-inspector-token",
    binding_key=b"m" * 32,
)


def _response(**updates: Any) -> dict[str, object]:
    return {
        "data": {
            "app_id": "12345",
            "user_id": "54321",
            "is_valid": True,
            "type": "USER",
            "scopes": ["ads_management", "public_profile"],
            "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
            "expires_at": int((_NOW + timedelta(minutes=5)).timestamp()),
            "data_access_expires_at": int((_NOW + timedelta(days=1)).timestamp()),
            **updates,
        }
    }


class _FakeResponse:
    def __init__(self, *, status_code: int, chunks: Iterable[bytes]) -> None:
        self.status_code = status_code
        self._chunks = tuple(chunks)

    def iter_bytes(self) -> Iterable[bytes]:
        return iter(self._chunks)


class _FakeResponseContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *_: object) -> None:
        return None


class _FakeClient:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        follow_redirects: bool,
        timeout: float,
    ) -> _FakeResponseContext:
        self._transport.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "follow_redirects": follow_redirects,
                "timeout": timeout,
            }
        )
        return _FakeResponseContext(self._transport.response)


class _FakeTransport:
    def __init__(self, *, status_code: int = 200, chunks: Iterable[bytes]) -> None:
        self.response = _FakeResponse(status_code=status_code, chunks=chunks)
        self.constructions = 0
        self.requests: list[dict[str, object]] = []

    def client(self, *, trust_env: bool) -> _FakeClient:
        assert trust_env is False
        self.constructions += 1
        return _FakeClient(self)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: object | None = None,
    chunks: Iterable[bytes] | None = None,
) -> _FakeTransport:
    if chunks is None:
        chunks = (json.dumps(_response() if body is None else body).encode("utf-8"),)
    transport = _FakeTransport(chunks=chunks)
    monkeypatch.setattr(inspection.httpx, "Client", transport.client)
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)
    return transport


def _inspect(
    supplier: Callable[[], MetaTokenInspectionCredentials] = lambda: _CREDENTIALS,
    *,
    required_scope: object = "ads_management",
) -> object:
    return inspect_meta_token(
        credential_supplier=supplier,
        expected_client_application_ref="12345",
        required_scope=required_scope,  # type: ignore[arg-type]
    )


def _public_text(error: AdhelmError) -> str:
    return error.code + "\n" + "".join(traceback.format_exception(error))


def test_fixed_request_and_hmac_bound_observation_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _install(monkeypatch)

    first = _inspect()
    second = _inspect()

    assert len(transport.requests) == 2
    for request in transport.requests:
        assert request == {
            "method": "GET",
            "url": "https://graph.facebook.com/v26.0/debug_token",
            "headers": {"authorization": "Bearer " + _CREDENTIALS.inspector_token},
            "params": {"input_token": _CREDENTIALS.subject_token},
            "follow_redirects": False,
            "timeout": 30.0,
        }
    expected_principal = hashlib.sha256(
        b"adhelm/meta/token-principal/v1\x00" + b"12345\x00" + b"54321"
    ).hexdigest()
    expected_binding = hmac.new(
        _CREDENTIALS.binding_key,
        b"adhelm/meta/inspected-credential/v1\x00"
        + b"meta-edit\x00"
        + _CREDENTIALS.subject_token.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    assert first.principal_ref == "meta_token_principal_v1." + expected_principal
    assert first.credential_binding_ref == "credential-binding:sha256:" + expected_binding
    assert first == second
    public = repr(first)
    assert _CREDENTIALS.subject_token not in public
    assert _CREDENTIALS.inspector_token not in public
    assert "54321" not in first.principal_ref


def test_untrusted_meta_prefixed_supplier_code_cannot_escape_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-error-secret"
    transport = _install(monkeypatch)

    def supplier() -> MetaTokenInspectionCredentials:
        raise AdhelmError("META_TOKEN_" + secret, secret, exit_code=2)

    with pytest.raises(AdhelmError) as raised:
        _inspect(supplier)

    assert raised.value.code == "META_TOKEN_INSPECTION_UNVERIFIED"
    assert secret not in _public_text(raised.value)
    assert transport.constructions == 0
    assert transport.requests == []


def test_missing_user_type_cannot_mint_principal_or_scope_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _response()
    assert isinstance(body["data"], dict)
    body["data"].pop("type")
    transport = _install(monkeypatch, body=body)

    with pytest.raises(AdhelmError) as raised:
        _inspect()

    assert raised.value.code == "META_TOKEN_TYPE_UNSUPPORTED"
    assert len(transport.requests) == 1
    assert "54321" not in _public_text(raised.value)


@pytest.mark.parametrize(
    "required_scope",
    [
        type("ScopeSubclass", (str,), {})("ads_management"),
        type(
            "ScopeImpostor",
            (),
            {
                "__hash__": lambda self: hash("ads_management"),
                "__eq__": lambda self, other: other == "ads_management",
            },
        )(),
    ],
)
def test_non_exact_scope_input_fails_before_supplier_or_http(
    monkeypatch: pytest.MonkeyPatch,
    required_scope: object,
) -> None:
    transport = _install(monkeypatch)
    supplied = 0

    def supplier() -> MetaTokenInspectionCredentials:
        nonlocal supplied
        supplied += 1
        return _CREDENTIALS

    with pytest.raises(AdhelmError) as raised:
        _inspect(supplier, required_scope=required_scope)

    assert raised.value.code == "META_TOKEN_INSPECTION_UNVERIFIED"
    assert supplied == 0
    assert transport.constructions == 0
    assert transport.requests == []


def test_post_response_credential_delay_cannot_return_stale_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _install(monkeypatch)
    elapsed = 0.0
    supplied = 0

    def monotonic() -> float:
        return elapsed

    def supplier() -> MetaTokenInspectionCredentials:
        nonlocal elapsed, supplied
        supplied += 1
        if supplied == 2:
            elapsed = 30.0
        return _CREDENTIALS

    monkeypatch.setattr(inspection, "_monotonic", monotonic)
    with pytest.raises(AdhelmError) as raised:
        _inspect(supplier)

    assert raised.value.code == "META_TOKEN_OBSERVATION_EXPIRED"
    assert supplied == 2
    assert len(transport.requests) == 1


def test_final_clock_sample_rejects_token_that_expires_after_http_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _response(expires_at=int((_NOW + timedelta(seconds=1)).timestamp()))
    transport = _install(monkeypatch, body=body)
    times = iter([_NOW, _NOW + timedelta(seconds=1)])
    monkeypatch.setattr(inspection, "_now", lambda: next(times))

    with pytest.raises(AdhelmError) as raised:
        _inspect()

    assert raised.value.code == "META_TOKEN_EXPIRED"
    assert len(transport.requests) == 1


def test_oversized_or_duplicate_response_never_returns_provider_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-provider-response-secret"
    oversized = _install(monkeypatch, chunks=(secret.encode() * (inspection._MAX_BYTES + 1),))

    with pytest.raises(AdhelmError) as raised:
        _inspect()

    assert raised.value.code == "META_TOKEN_INSPECTION_UNVERIFIED"
    assert secret not in _public_text(raised.value)
    assert len(oversized.requests) == 1

    duplicate = _install(
        monkeypatch,
        chunks=(
            b'{"data":{"app_id":"12345","user_id":"54321","is_valid":true,'
            b'"type":"USER","scopes":["ads_management"],"expires_at":1893456300,'
            b'"data_access_expires_at":1893542400},"data":{}}',
        ),
    )
    with pytest.raises(AdhelmError) as repeated:
        _inspect()
    assert repeated.value.code == "META_TOKEN_INSPECTION_UNVERIFIED"
    assert len(duplicate.requests) == 1

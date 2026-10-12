"""Fake-only coverage for the private fixed Meta Ads EDIT observation session."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread

import httpx
import pytest

from adhelm import _meta_ads_token_session as shared_session
from adhelm import config as config_module
from adhelm import meta_ads_token_edit_session as edit_session
from adhelm import meta_ads_token_read_session as read_session
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_profiles import (
    MetaAccountTokenProfileDraft,
    finalize_meta_account_token_profile,
)

_NOW = datetime(2030, 2, 3, 12, 0, tzinfo=UTC)
_SUBJECT = "meta-edit-subject-token"
_INSPECTOR = "meta-edit-inspector-token"
_REAL_HTTP_CLIENT = httpx.Client


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _draft(**updates: object) -> MetaAccountTokenProfileDraft:
    data: dict[str, object] = {
        "profile_id": "meta-edit",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "EDIT",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    data.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(data)


def _wire_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(
        shared_session, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())


def _configured(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, purpose: str = "EDIT"
) -> tuple[_MemoryStore, str]:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    enroll_meta_token_profile(
        root,
        _draft(purpose=purpose),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return store, digest(load_configuration(root))


def _debug_payload() -> dict[str, object]:
    return {
        "data": {
            "app_id": "67890",
            "user_id": "54321",
            "is_valid": True,
            "type": "USER",
            "scopes": ["ads_management", "business_management", "public_profile"],
            "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
            "expires_at": int((_NOW + timedelta(minutes=5)).timestamp()),
            "data_access_expires_at": int((_NOW + timedelta(days=1)).timestamp()),
        }
    }


def _account(*, currency: str = "USD", timezone: str = "UTC") -> dict[str, object]:
    return {"id": "12345", "account_status": 1, "currency": currency, "timezone_name": timezone}


def _campaign(
    *,
    account_id: str = "12345",
    configured: str = "PAUSED",
    effective: str = "PAUSED",
) -> dict[str, object]:
    return {
        "id": "67890",
        "account_id": account_id,
        "configured_status": configured,
        "effective_status": effective,
    }


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    provider_handler: httpx.MockTransport,
    inspections: list[httpx.Request],
    *,
    clock: list[datetime] | None = None,
) -> None:
    current = clock if clock is not None else [_NOW]

    def inspection_client(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False

        def handler(request: httpx.Request) -> httpx.Response:
            inspections.append(request)
            assert request.method == "GET"
            assert request.url.path == "/v26.0/debug_token"
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            return httpx.Response(200, json=_debug_payload())

        return _REAL_HTTP_CLIENT(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(inspection.httpx, "Client", inspection_client)
    monkeypatch.setattr(inspection, "_now", lambda: current[0])
    monkeypatch.setattr(inspection, "_monotonic", lambda: 10.0)
    monkeypatch.setattr(shared_session, "_now", lambda: current[0])
    monkeypatch.setattr(
        shared_session,
        "_meta_http_client",
        lambda: _REAL_HTTP_CLIENT(transport=provider_handler, trust_env=False),
    )


def _open(
    root: Path, expected: str
) -> AbstractContextManager[edit_session.MetaAdsTokenEditSession]:
    return edit_session.configured_meta_ads_token_edit_session(
        root,
        "meta-edit",
        expected_configuration_digest=expected,
    )


def test_edit_session_binds_one_inspection_identity_and_fixed_campaign_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        assert _INSPECTOR not in str(request.headers)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v26.0/67890"
        assert str(request.url.params) == (
            "fields=id%2Caccount_id%2Cconfigured_status%2Ceffective_status"
        )
        return httpx.Response(200, json=_campaign())

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_requests)

    manager = _open(tmp_path, expected)
    with manager as session:
        binding = session.binding
        assert binding.profile_ref == "meta-edit"
        assert binding.client_application_ref == "67890"
        assert binding.required_scope == "ads_management"
        assert binding.configuration_digest == expected
        expected_profile_digest = digest(finalize_meta_account_token_profile(_draft()))
        assert binding.profile_configuration_digest == expected_profile_digest
        assert binding.observed_at == _NOW
        assert binding.expires_at == _NOW + timedelta(minutes=5)
        assert binding.data_access_expires_at == _NOW + timedelta(days=1)
        assert _SUBJECT not in repr(binding)
        assert _INSPECTOR not in repr(binding)
        assert "scopes" not in binding.__dataclass_fields__
        assert session.identity.account_client_ref == "act_12345"
        state = session.read_campaign_state("67890")
        assert state.campaign_client_ref == "67890"
        assert state.account_client_ref == "12345"
        assert state.configured_status == "PAUSED"
        assert state.effective_status == "PAUSED"
        frame = getattr(manager, "gen", None)
        assert frame is not None
        assert not any(
            type(value) is inspection.MetaTokenInspectionCredentials
            for value in frame.gi_frame.f_locals.values()
        )
        assert _SUBJECT not in repr(frame.gi_frame.f_locals)
        assert _INSPECTOR not in repr(frame.gi_frame.f_locals)

    assert [request.url.path for request in inspection_requests] == ["/v26.0/debug_token"]
    assert provider_paths == ["/v26.0/act_12345", "/v26.0/67890"]


def test_edit_factory_refuses_stale_digest_or_read_profile_before_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspections: list[httpx.Request] = []
    _install_http(
        monkeypatch,
        httpx.MockTransport(lambda _request: pytest.fail("no provider HTTP")),
        inspections,
    )

    with pytest.raises(AdhelmError) as stale:
        with _open(tmp_path, "0" * 64):
            pytest.fail("stale composition must fail before inspection")
    assert stale.value.code == "CONFIGURATION_CHANGED"
    assert inspections == []

    with pytest.raises(AdhelmError) as malformed:
        with _open(tmp_path, 1):  # type: ignore[arg-type]
            pytest.fail("bad digest must fail before configuration access")
    assert malformed.value.code == "META_TOKEN_EDIT_SESSION_INVALID"
    assert inspections == []

    read_root = tmp_path / "read"
    _read_store, read_digest = _configured(read_root, monkeypatch, purpose="READ")
    with pytest.raises(AdhelmError) as wrong_purpose:
        with _open(read_root, read_digest):
            pytest.fail("READ profile must not use EDIT observation")
    assert wrong_purpose.value.code == "META_TOKEN_EDIT_PROFILE_REQUIRED"
    assert inspections == []


def test_configuration_or_bundle_drift_blocks_campaign_get_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, expected = _configured(tmp_path, monkeypatch)
    profile = finalize_meta_account_token_profile(_draft())
    inspections: list[httpx.Request] = []
    paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("drift must stop before the campaign GET")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspections)
    with _open(tmp_path, expected) as session:
        changed = load_configuration(tmp_path).model_copy(
            update={
                "output": load_configuration(tmp_path).output.model_copy(
                    update={"max_output_bytes": 4096}
                )
            }
        )
        save_configuration(tmp_path, changed, replace=True)
        with pytest.raises(AdhelmError) as configuration_changed:
            session.read_campaign_state("67890")
        assert configuration_changed.value.code == "CONFIGURATION_CHANGED"

    # Re-open from a separately unchanged configuration to isolate protected
    # bundle replacement from the configuration-drift check above.
    second = tmp_path / "bundle"
    replacement_store, second_expected = _configured(second, monkeypatch)
    second_paths: list[str] = []

    def second_provider(request: httpx.Request) -> httpx.Response:
        second_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("bundle drift must stop before the campaign GET")

    _install_http(monkeypatch, httpx.MockTransport(second_provider), inspections)
    with _open(second, second_expected) as session:
        replacement = token_credentials._new_bundle(
            profile, "replacement-subject", "replacement-inspector"
        )._encoded()
        replacement_store.values[(token_credentials._SERVICE, profile.credential_ref)] = replacement
        with pytest.raises(AdhelmError) as credential_changed:
            session.read_campaign_state("67890")
        assert credential_changed.value.code == "META_TOKEN_CREDENTIAL_CHANGED"

    assert paths == ["/v26.0/act_12345"]
    assert second_paths == ["/v26.0/act_12345"]


def test_invalid_campaign_or_expired_session_stops_before_campaign_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspections: list[httpx.Request] = []
    paths: list[str] = []
    clock = [_NOW]

    def provider(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/v26.0/act_12345"
        return httpx.Response(200, json=_account())

    _install_http(monkeypatch, httpx.MockTransport(provider), inspections, clock=clock)
    with _open(tmp_path, expected) as session:
        with pytest.raises(AdhelmError) as bad_campaign:
            session.read_campaign_state("not-a-campaign")
        assert bad_campaign.value.code == "INVALID_INPUT"
        clock[0] = _NOW + timedelta(minutes=5)
        with pytest.raises(AdhelmError) as expired:
            session.read_campaign_state("67890")
        assert expired.value.code == "META_TOKEN_EXPIRED"

    assert paths == ["/v26.0/act_12345"]


def test_retention_recheck_is_owner_only_repeatable_and_blocks_campaign_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspections: list[httpx.Request] = []
    paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("retention callback must not contact the provider")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspections)
    with _open(tmp_path, expected) as session:
        with session.retention_recheck() as recheck:
            recheck()
            recheck()
            with pytest.raises(AdhelmError) as busy:
                session.read_campaign_state("67890")
            assert busy.value.code == "META_TOKEN_EDIT_SESSION_BUSY"
            outcomes: list[str] = []

            def cross_thread() -> None:
                try:
                    recheck()
                except AdhelmError as error:
                    outcomes.append(error.code)

            thread = Thread(target=cross_thread)
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert outcomes == ["META_TOKEN_EDIT_RETENTION_RECHECK_REQUIRED"]

        with pytest.raises(AdhelmError) as escaped:
            recheck()
        assert escaped.value.code == "META_TOKEN_EDIT_RETENTION_RECHECK_REQUIRED"

    assert paths == ["/v26.0/act_12345"]


def test_edit_identity_mismatch_stops_before_campaign_reader_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspections: list[httpx.Request] = []
    paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/v26.0/act_12345"
        return httpx.Response(200, json=_account(currency="EUR"))

    _install_http(monkeypatch, httpx.MockTransport(provider), inspections)
    with pytest.raises(AdhelmError) as mismatch:
        with _open(tmp_path, expected):
            pytest.fail("mismatched identity cannot yield an EDIT session")
    assert mismatch.value.code == "IDENTITY_MISMATCH"
    assert paths == ["/v26.0/act_12345"]


@pytest.mark.parametrize("field", ["expires_at", "data_access_expires_at"])
def test_detached_edit_binding_rejects_an_expiry_equal_to_observation(field: str) -> None:
    values: dict[str, object] = {
        "profile_ref": "meta-edit",
        "client_application_ref": "67890",
        "principal_ref": "meta_token_principal_v1." + "a" * 64,
        "required_scope": "ads_management",
        "credential_binding_ref": "credential-binding:sha256:" + "b" * 64,
        "configuration_digest": "c" * 64,
        "profile_configuration_digest": "d" * 64,
        "observed_at": _NOW,
        "expires_at": _NOW + timedelta(seconds=1),
        "data_access_expires_at": _NOW + timedelta(seconds=1),
    }
    values[field] = _NOW
    with pytest.raises(AdhelmError) as invalid:
        edit_session._new_binding(**values)  # type: ignore[arg-type]
    assert invalid.value.code == "META_TOKEN_EDIT_SESSION_INVALID"
    forged = edit_session.MetaAdsTokenEditBinding(
        **values,
        _issuer=edit_session._BINDING_ISSUER,
    )
    with pytest.raises(AdhelmError) as revalidated:
        edit_session._checked_binding(forged)
    assert revalidated.value.code == "META_TOKEN_EDIT_SESSION_INVALID"


@pytest.mark.parametrize("field", ["expires_at", "data_access_expires_at"])
def test_shared_observation_guard_rejects_an_expiry_equal_to_observation(field: str) -> None:
    binding_ref = "credential-binding:sha256:" + "b" * 64
    values: dict[str, object] = {
        "client_application_ref": "67890",
        "principal_ref": "meta_token_principal_v1." + "a" * 64,
        "profile_ref": "meta-edit",
        "credential_binding_ref": binding_ref,
        "scopes": ("ads_management",),
        "observed_at": _NOW,
        "expires_at": _NOW + timedelta(seconds=1),
        "data_access_expires_at": _NOW + timedelta(seconds=1),
    }
    values[field] = _NOW
    observation = inspection.MetaTokenObservation(**values)  # type: ignore[arg-type]
    with pytest.raises(AdhelmError) as invalid:
        shared_session._checked_observation(
            observation,
            finalize_meta_account_token_profile(_draft()),
            expected_binding_ref=binding_ref,
            policy=shared_session._EDIT_POLICY,
        )
    assert invalid.value.code == "META_TOKEN_EDIT_SESSION_INVALID"


def test_constructed_session_facades_fail_closed_before_any_operation() -> None:
    malformed_edit = object.__new__(edit_session.MetaAdsTokenEditSession)
    with pytest.raises(AdhelmError) as edit_error:
        malformed_edit.read_campaign_state("67890")
    assert edit_error.value.code == "META_TOKEN_EDIT_SESSION_INVALID"

    malformed_read = object.__new__(read_session.MetaAdsTokenReadSession)
    with pytest.raises(AdhelmError) as read_error:
        malformed_read.list_inventory(1)
    assert read_error.value.code == "META_TOKEN_READ_SESSION_INVALID"

    malformed_binding = object.__new__(edit_session.MetaAdsTokenEditBinding)
    with pytest.raises(AdhelmError) as binding_error:
        edit_session._checked_binding(malformed_binding)
    assert binding_error.value.code == "META_TOKEN_EDIT_SESSION_INVALID"

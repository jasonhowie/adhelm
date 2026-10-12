"""Fake-only coverage for the private Meta Ads token READ session."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Thread

import httpx
import pytest

from adhelm import _meta_ads_token_session as shared_session
from adhelm import config as config_module
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
from adhelm.providers.meta_ads import MetaAdsReportQuery

_NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
_SUBJECT = "subject-fixture-token"
_INSPECTOR = "inspector-fixture-token"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _draft(**updates: object) -> MetaAccountTokenProfileDraft:
    data: dict[str, object] = {
        "profile_id": "meta-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    data.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(data)


def _install_fake_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(
        shared_session, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())


def _configured(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, purpose: str = "READ"
) -> tuple[_MemoryStore, str]:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    enroll_meta_token_profile(
        root,
        _draft(purpose=purpose),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return store, digest(load_configuration(root))


def _debug_response() -> dict[str, object]:
    return {
        "data": {
            "app_id": "67890",
            "user_id": "54321",
            "is_valid": True,
            "type": "USER",
            "scopes": ["ads_read", "public_profile"],
            "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
            "expires_at": int((_NOW + timedelta(minutes=5)).timestamp()),
            "data_access_expires_at": int((_NOW + timedelta(days=1)).timestamp()),
        }
    }


def _account(*, currency: str = "USD", timezone: str = "UTC") -> dict[str, object]:
    return {"id": "12345", "account_status": 1, "currency": currency, "timezone_name": timezone}


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    meta_handler: httpx.MockTransport,
    inspections: list[httpx.Request],
) -> None:
    real_client = httpx.Client

    def inspection_client(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False

        def handler(request: httpx.Request) -> httpx.Response:
            inspections.append(request)
            assert request.url.path == "/v26.0/debug_token"
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            return httpx.Response(200, json=_debug_response())

        return real_client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr("adhelm.meta_token_inspection.httpx.Client", inspection_client)
    monkeypatch.setattr(
        shared_session,
        "_meta_http_client",
        lambda: real_client(transport=meta_handler, trust_env=False),
    )
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 1.0)
    monkeypatch.setattr(shared_session, "_now", lambda: _NOW)


def _session(
    root: Path,
    expected_configuration_digest: str,
) -> AbstractContextManager[read_session.MetaAdsTokenReadSession]:
    return read_session.configured_meta_ads_token_read_session(
        root,
        "meta-read",
        expected_configuration_digest=expected_configuration_digest,
    )


def test_fixed_session_inspects_once_proves_identity_and_hides_inspector_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    provider_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        assert _INSPECTOR not in str(request.headers)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        assert request.url.path in {
            "/v26.0/act_12345/campaigns",
            "/v26.0/act_12345/adsets",
            "/v26.0/act_12345/ads",
        }
        return httpx.Response(200, json={"data": []})

    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(handler),
        inspections=inspection_requests,
    )

    manager = _session(tmp_path, expected)
    with manager as active:
        assert active.identity.account_client_ref == "act_12345"
        campaigns, adsets, ads = active.list_inventory(500)
        assert campaigns == []
        assert adsets == []
        assert ads == []
        assert _SUBJECT not in repr(active)
        assert _INSPECTOR not in repr(active)
        assert "inspector_token" not in active.__slots__
        generator = getattr(manager, "gen", None)
        assert generator is not None
        frame = getattr(generator, "gi_frame", None)
        assert frame is not None
        assert not any(
            type(value) is inspection.MetaTokenInspectionCredentials
            for value in frame.f_locals.values()
        )

    assert len(inspection_requests) == 1
    assert [request.url.path for request in provider_requests] == [
        "/v26.0/act_12345",
        "/v26.0/act_12345/campaigns",
        "/v26.0/act_12345/adsets",
        "/v26.0/act_12345/ads",
    ]


def test_expected_configuration_digest_and_read_purpose_fail_before_store_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(lambda _request: pytest.fail("no account HTTP")),
        inspections=inspection_requests,
    )

    with pytest.raises(AdhelmError) as stale:
        with _session(tmp_path, "0" * 64):
            pytest.fail("a stale composition snapshot must fail before inspection")
    assert stale.value.code == "CONFIGURATION_CHANGED"
    assert inspection_requests == []
    assert store.values

    with pytest.raises(AdhelmError) as malformed:
        with _session(tmp_path, "not-a-configuration-digest"):
            pytest.fail("a malformed composition snapshot must fail before inspection")
    assert malformed.value.code == "META_TOKEN_READ_SESSION_INVALID"
    assert inspection_requests == []

    edit_root = tmp_path / "edit"
    _edit_store, edit_expected = _configured(edit_root, monkeypatch, purpose="EDIT")
    with pytest.raises(AdhelmError) as edit:
        with _session(edit_root, edit_expected):
            pytest.fail("an EDIT declaration cannot use the fixed READ session")
    assert edit.value.code == "META_TOKEN_READ_PROFILE_REQUIRED"
    assert inspection_requests == []


def test_current_full_bundle_drift_blocks_next_fixed_get_before_provider_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, expected = _configured(tmp_path, monkeypatch)
    profile = finalize_meta_account_token_profile(_draft())
    inspection_requests: list[httpx.Request] = []
    provider_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v26.0/act_12345/campaigns":
            replacement = token_credentials._new_bundle(
                profile, "subject-after-drift", "inspector-after-drift"
            )._encoded()
            store.values[(token_credentials._SERVICE, profile.credential_ref)] = replacement
            return httpx.Response(200, json={"data": []})
        pytest.fail("bundle drift must stop before another fixed provider request")

    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(handler),
        inspections=inspection_requests,
    )

    with _session(tmp_path, expected) as active:
        with pytest.raises(AdhelmError) as changed:
            active.list_inventory(10)
    assert changed.value.code == "META_TOKEN_CREDENTIAL_CHANGED"
    assert provider_paths == ["/v26.0/act_12345", "/v26.0/act_12345/campaigns"]
    assert len(inspection_requests) == 1


def test_identity_and_report_inputs_must_match_customer_declarations_before_report_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    provider_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("invalid query must stop before an insights GET")

    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(handler),
        inspections=inspection_requests,
    )
    query = MetaAdsReportQuery(
        account_ref="act_12345",
        report_id="report:fixture",
        query_contract_hash="a" * 64,
        window_start=date(2029, 12, 1),
        window_end=date(2029, 12, 1),
        source_timezone="UTC",
        action_attribution_windows=("7d_click",),
    )

    with _session(tmp_path, expected) as active:
        with pytest.raises(AdhelmError) as mismatch:
            active.run_report(query)
    assert mismatch.value.code == "IDENTITY_MISMATCH"
    assert provider_paths == ["/v26.0/act_12345"]
    assert len(inspection_requests) == 1


def test_expiry_and_closed_session_stop_fixed_reads_before_a_new_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    provider_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        assert request.url.path == "/v26.0/act_12345"
        return httpx.Response(200, json=_account())

    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(handler),
        inspections=inspection_requests,
    )
    current = [_NOW]
    monkeypatch.setattr(shared_session, "_now", lambda: current[0])

    with _session(tmp_path, expected) as active:
        current[0] = _NOW + timedelta(minutes=5)
        with pytest.raises(AdhelmError) as expired:
            active.list_inventory(1)
        assert expired.value.code == "META_TOKEN_EXPIRED"
    with pytest.raises(AdhelmError) as closed:
        active.list_inventory(1)
    assert closed.value.code == "META_TOKEN_READ_SESSION_CLOSED"
    assert provider_paths == ["/v26.0/act_12345"]


def test_retention_recheck_holds_local_state_without_network_or_lock_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    provider_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"data": []})

    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(handler),
        inspections=inspection_requests,
    )
    lock_entries: list[str] = []

    @contextmanager
    def tracked_lock(reference: str) -> Iterator[None]:
        lock_entries.append(reference)
        try:
            yield
        finally:
            lock_entries.pop()

    monkeypatch.setattr(config_module, "credential_mutation_lock", tracked_lock)
    monkeypatch.setattr(shared_session, "credential_mutation_lock", tracked_lock)

    with _session(tmp_path, expected) as active:
        lock_entries.clear()
        before = list(provider_paths)
        with active.retention_recheck() as recheck:
            entries_at_yield = list(lock_entries)
            assert len(entries_at_yield) == 2
            with pytest.raises(AdhelmError) as busy:
                active.list_inventory(1)
            assert busy.value.code == "META_TOKEN_READ_SESSION_BUSY"
            assert provider_paths == before
            recheck()
            assert lock_entries == entries_at_yield
        assert lock_entries == []


def test_retention_recheck_requires_callback_and_detects_configuration_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json=_account())
                if request.url.path == "/v26.0/act_12345"
                else pytest.fail("no further provider request")
            )
        ),
        inspections=inspection_requests,
    )

    with _session(tmp_path, expected) as active:
        with pytest.raises(AdhelmError) as missing:
            with active.retention_recheck():
                pass
        assert missing.value.code == "META_TOKEN_RETENTION_RECHECK_REQUIRED"

        with pytest.raises(AdhelmError) as drift:
            with active.retention_recheck() as recheck:
                current = load_configuration(tmp_path)
                changed = current.model_copy(
                    update={"output": current.output.model_copy(update={"max_output_bytes": 4096})}
                )
                save_configuration(tmp_path, changed, replace=True)
                recheck()
        assert drift.value.code == "CONFIGURATION_CHANGED"


def test_retention_recheck_allows_repeated_owner_calls_and_rejects_escaped_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_requests: list[httpx.Request] = []
    _install_http(
        monkeypatch,
        meta_handler=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json=_account())
                if request.url.path == "/v26.0/act_12345"
                else pytest.fail("retention checks perform no provider HTTP")
            )
        ),
        inspections=inspection_requests,
    )

    with _session(tmp_path, expected) as active:
        with active.retention_recheck() as recheck:
            recheck()
            recheck()
            errors: list[str] = []

            def cross_thread() -> None:
                try:
                    recheck()
                except AdhelmError as error:
                    errors.append(error.code)

            thread = Thread(target=cross_thread)
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert errors == ["META_TOKEN_RETENTION_RECHECK_REQUIRED"]

        with pytest.raises(AdhelmError) as escaped:
            recheck()
        assert escaped.value.code == "META_TOKEN_RETENTION_RECHECK_REQUIRED"

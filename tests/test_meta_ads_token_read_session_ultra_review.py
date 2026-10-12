"""Independent fake-only review of the private Meta Ads token READ session.

The tests use only in-memory credential and HTTP seams.  They never prompt,
open an operating-system store, or contact a provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread, get_ident
from typing import Any

import httpx
import pytest

from adhelm import _meta_ads_token_session as shared_session
from adhelm import config as config_module
from adhelm import inventory, workflows
from adhelm import meta_ads_token_read_session as read_session
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_profiles import (
    MetaAccountTokenProfileDraft,
    finalize_meta_account_token_profile,
)
from adhelm.providers.meta_ads import MetaAdsReportQuery

_NOW = datetime(2030, 1, 3, 12, 0, tzinfo=UTC)
_SUBJECT = "review-subject-token"
_INSPECTOR = "review-inspector-token"
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
        "profile_id": "independent-meta-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    data.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(data)


def _wire_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())
    monkeypatch.setattr(
        shared_session, "credential_mutation_lock", lambda _reference: nullcontext()
    )


def _configured(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_MemoryStore, str]:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    enroll_meta_token_profile(
        root,
        _draft(),
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
    provider_handler: httpx.MockTransport,
    inspection_paths: list[str],
    *,
    clock: list[datetime] | None = None,
) -> None:
    current = clock if clock is not None else [_NOW]

    def inspection_client(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False

        def handler(request: httpx.Request) -> httpx.Response:
            inspection_paths.append(request.url.path)
            assert request.url.path == "/v26.0/debug_token"
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            return httpx.Response(200, json=_debug_payload())

        return _REAL_HTTP_CLIENT(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(inspection.httpx, "Client", inspection_client)
    monkeypatch.setattr(inspection, "_now", lambda: current[0])
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)
    monkeypatch.setattr(shared_session, "_now", lambda: current[0])
    monkeypatch.setattr(
        shared_session,
        "_meta_http_client",
        lambda: _REAL_HTTP_CLIENT(transport=provider_handler, trust_env=False),
    )


def _open(root: Path, expected: str):
    return read_session.configured_meta_ads_token_read_session(
        root,
        "independent-meta-read",
        expected_configuration_digest=expected,
    )


@pytest.mark.parametrize("invalid", [True, 1.0, "A" * 64])
def test_invalid_composition_digest_fails_before_any_local_or_provider_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: object
) -> None:
    prepared: list[object] = []

    def no_prepare(*args: object, **kwargs: object) -> object:
        prepared.append((args, kwargs))
        raise AssertionError("invalid digest must stop before profile preparation")

    monkeypatch.setattr(token_credentials, "_prepare_meta_token_inspection", no_prepare)

    with pytest.raises(AdhelmError) as raised:
        with read_session.configured_meta_ads_token_read_session(
            tmp_path,
            "independent-meta-read",
            expected_configuration_digest=invalid,
        ):
            pytest.fail("invalid composition input must not create a session")

    assert raised.value.code == "META_TOKEN_READ_SESSION_INVALID"
    assert prepared == []


def test_inspector_is_confined_to_debug_token_and_identity_precedes_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
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

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)

    with _open(tmp_path, expected) as session:
        campaigns, adsets, ads = session.list_inventory(1)
        assert campaigns == []
        assert adsets == []
        assert ads == []
        assert _SUBJECT not in repr(session)
        assert _INSPECTOR not in repr(session)
        public_surface = {name for name in dir(type(session)) if not name.startswith("_")}
        assert {"identity", "list_inventory", "retention_recheck", "run_report"} <= public_surface
        assert not {"credential", "token", "provider_client"} & public_surface

    assert inspection_paths == ["/v26.0/debug_token"]
    assert provider_paths == [
        "/v26.0/act_12345",
        "/v26.0/act_12345/campaigns",
        "/v26.0/act_12345/adsets",
        "/v26.0/act_12345/ads",
    ]


@pytest.mark.parametrize("identity_update", [{"currency": "EUR"}, {"timezone": "America/Chicago"}])
def test_declared_account_currency_and_timezone_are_proved_before_any_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_update: dict[str, str]
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        assert request.url.path == "/v26.0/act_12345"
        return httpx.Response(200, json=_account(**identity_update))

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)

    with pytest.raises(AdhelmError) as raised:
        with _open(tmp_path, expected):
            pytest.fail("mismatched remote identity must not yield a read session")

    assert raised.value.code == "IDENTITY_MISMATCH"
    assert inspection_paths == ["/v26.0/debug_token"]
    assert provider_paths == ["/v26.0/act_12345"]


def test_post_inspection_bundle_replacement_blocks_before_account_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, expected = _configured(tmp_path, monkeypatch)
    profile = finalize_meta_account_token_profile(_draft())
    inspection_paths: list[str] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        pytest.fail("bundle replacement must stop before account identity")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)
    original_inspection = shared_session.inspect_meta_token

    def replace_after_inspection(*args: object, **kwargs: object):
        observation = original_inspection(*args, **kwargs)
        store.values[(token_credentials._SERVICE, profile.credential_ref)] = (
            token_credentials._new_bundle(
                profile, "replaced-subject", "replaced-inspector"
            )._encoded()
        )
        return observation

    monkeypatch.setattr(shared_session, "inspect_meta_token", replace_after_inspection)

    with pytest.raises(AdhelmError) as raised:
        with _open(tmp_path, expected):
            pytest.fail("replaced bundle must not create a session")

    assert raised.value.code == "META_TOKEN_CREDENTIAL_CHANGED"
    assert inspection_paths == ["/v26.0/debug_token"]
    assert provider_paths == []


def test_configuration_bundle_and_expiry_are_rechecked_before_each_fixed_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, expected = _configured(tmp_path, monkeypatch)
    profile = finalize_meta_account_token_profile(_draft())
    inspection_paths: list[str] = []
    provider_paths: list[str] = []
    clock = [_NOW]

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("a changed local state must stop before an inventory GET")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths, clock=clock)

    with _open(tmp_path, expected) as session:
        current = load_configuration(tmp_path)
        save_configuration(
            tmp_path,
            current.model_copy(
                update={"output": current.output.model_copy(update={"max_output_bytes": 4096})}
            ),
            replace=True,
        )
        with pytest.raises(AdhelmError) as configuration_changed:
            session.list_inventory(1)
        assert configuration_changed.value.code == "CONFIGURATION_CHANGED"

    assert provider_paths == ["/v26.0/act_12345"]

    replacement_root = tmp_path / "replacement"
    replacement_store, replacement_expected = _configured(replacement_root, monkeypatch)
    replacement_paths: list[str] = []
    _install_http(
        monkeypatch,
        httpx.MockTransport(
            lambda request: (
                replacement_paths.append(request.url.path) or httpx.Response(200, json=_account())
                if request.url.path == "/v26.0/act_12345"
                else pytest.fail("changed bundle must stop before inventory")
            )
        ),
        [],
        clock=clock,
    )
    with _open(replacement_root, replacement_expected) as session:
        replacement_store.values[(token_credentials._SERVICE, profile.credential_ref)] = (
            token_credentials._new_bundle(
                profile, "changed-subject", "changed-inspector"
            )._encoded()
        )
        with pytest.raises(AdhelmError) as bundle_changed:
            session.list_inventory(1)
        assert bundle_changed.value.code == "META_TOKEN_CREDENTIAL_CHANGED"

    expiry_root = tmp_path / "expiry"
    _expiry_store, expiry_expected = _configured(expiry_root, monkeypatch)
    expiry_paths: list[str] = []
    _install_http(
        monkeypatch,
        httpx.MockTransport(
            lambda request: (
                expiry_paths.append(request.url.path) or httpx.Response(200, json=_account())
                if request.url.path == "/v26.0/act_12345"
                else pytest.fail("expired token must stop before inventory")
            )
        ),
        [],
        clock=clock,
    )
    with _open(expiry_root, expiry_expected) as session:
        clock[0] = _NOW + timedelta(minutes=5)
        with pytest.raises(AdhelmError) as expired:
            session.list_inventory(1)
        assert expired.value.code == "META_TOKEN_EXPIRED"

    assert inspection_paths == ["/v26.0/debug_token"]
    assert replacement_paths == ["/v26.0/act_12345"]
    assert expiry_paths == ["/v26.0/act_12345"]


def test_configuration_change_between_fixed_inventory_edges_blocks_the_next_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v26.0/act_12345/campaigns":
            current = load_configuration(tmp_path)
            save_configuration(
                tmp_path,
                current.model_copy(
                    update={"output": current.output.model_copy(update={"max_output_bytes": 4096})}
                ),
                replace=True,
            )
            return httpx.Response(200, json={"data": []})
        pytest.fail("configuration drift must stop before the next inventory edge")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)

    with _open(tmp_path, expected) as session:
        with pytest.raises(AdhelmError) as changed:
            session.list_inventory(1)

    assert changed.value.code == "CONFIGURATION_CHANGED"
    assert inspection_paths == ["/v26.0/debug_token"]
    assert provider_paths == ["/v26.0/act_12345", "/v26.0/act_12345/campaigns"]


def test_retention_recheck_holds_ordered_locks_allows_repeated_owner_calls_and_blocks_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("a read must not overlap the retention window")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)

    with _open(tmp_path, expected) as session:
        entries: list[str] = []

        @contextmanager
        def tracked_lock(reference: str) -> Iterator[None]:
            entries.append(reference)
            try:
                yield
            finally:
                assert entries[-1] == reference
                entries.pop()

        monkeypatch.setattr(config_module, "credential_mutation_lock", tracked_lock)
        monkeypatch.setattr(shared_session, "credential_mutation_lock", tracked_lock)
        before_paths = list(provider_paths)
        with session.retention_recheck() as recheck:
            assert len(entries) == 2
            assert entries[0].startswith("configuration\x00")
            locked = list(entries)
            recheck()
            recheck()
            assert entries == locked
            with pytest.raises(AdhelmError) as busy:
                session.list_inventory(1)
            assert busy.value.code == "META_TOKEN_READ_SESSION_BUSY"
            assert provider_paths == before_paths
        assert entries == []


def test_retention_callback_rejects_cross_thread_and_escaped_uses_without_local_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        provider_paths.append(request.url.path)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(200, json=_account())
        pytest.fail("cross-thread retention misuse must not read a provider")

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)
    verification_threads: list[int] = []
    original_verify = shared_session._verify_current_state_locked

    def tracked_verify(*args: object, **kwargs: object):
        verification_threads.append(get_ident())
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(shared_session, "_verify_current_state_locked", tracked_verify)

    with _open(tmp_path, expected) as session:
        escaped: Callable[[], None]
        before_paths = list(provider_paths)
        with session.retention_recheck() as recheck:
            escaped = recheck
            verification_count = len(verification_threads)
            outcomes: list[str] = []

            def invoke_callback() -> None:
                try:
                    recheck()
                except AdhelmError as error:
                    outcomes.append(error.code)

            callback_thread = Thread(target=invoke_callback)
            callback_thread.start()
            callback_thread.join(timeout=2)
            assert not callback_thread.is_alive()
            assert outcomes == ["META_TOKEN_RETENTION_RECHECK_REQUIRED"]
            assert len(verification_threads) == verification_count

            def invoke_read() -> None:
                try:
                    session.list_inventory(1)
                except AdhelmError as error:
                    outcomes.append(error.code)

            read_thread = Thread(target=invoke_read)
            read_thread.start()
            read_thread.join(timeout=2)
            assert not read_thread.is_alive()
            assert outcomes[-1] == "META_TOKEN_READ_SESSION_BUSY"
            assert provider_paths == before_paths
            recheck()

        assert callable(escaped)
        with pytest.raises(AdhelmError) as after_exit:
            escaped()
        assert after_exit.value.code == "META_TOKEN_RETENTION_RECHECK_REQUIRED"
        assert len(verification_threads) == verification_count + 1


def _complete_provider_response(request: httpx.Request, paths: list[str]) -> httpx.Response:
    paths.append(request.url.path)
    assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
    if request.url.path == "/v26.0/act_12345":
        return httpx.Response(200, json=_account())
    if request.url.path.endswith("/campaigns"):
        return httpx.Response(200, json={"data": [{"id": "11", "effective_status": "PAUSED"}]})
    if request.url.path.endswith("/adsets"):
        return httpx.Response(
            200,
            json={"data": [{"id": "12", "campaign_id": "11", "effective_status": "PAUSED"}]},
        )
    if request.url.path.endswith("/ads"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "13",
                        "campaign_id": "11",
                        "adset_id": "12",
                        "effective_status": "PAUSED",
                    }
                ]
            },
        )
    assert request.url.path == "/v26.0/act_12345/insights"
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "campaign_id": "11",
                    "date_start": "2029-12-01",
                    "date_stop": "2029-12-01",
                    "impressions": "10",
                    "clicks": "2",
                    "spend": "1.25",
                    "reach": "8",
                }
            ]
        },
    )


def _workflow_query_file(root: Path) -> Path:
    path = root / "independent-meta-read-query.json"
    path.write_text(
        json.dumps(
            {
                "account_ref": "12345",
                "report_id": "report:independent",
                "query_contract_hash": "a" * 64,
                "window_start": "2029-12-01",
                "window_end": "2029-12-01",
                "source_timezone": "UTC",
                "action_attribution_windows": ["7d_click"],
                "level": "CAMPAIGN",
            }
        ),
        encoding="utf-8",
    )
    return path


@contextmanager
def _observed_retention(
    original: Callable[[read_session.MetaAdsTokenReadSession], Any],
    session: read_session.MetaAdsTokenReadSession,
    *,
    callback_count: list[int],
    state: dict[str, object],
    provider_paths: list[str],
) -> Iterator[Callable[[], None]]:
    with original(session) as callback:

        def checked() -> None:
            assert state["configuration_locks"] == 1
            assert state["credential_locks"] == 1
            lock_count = state["lock_entries"]
            assert isinstance(lock_count, int)
            path_count = len(provider_paths)
            state["inside_recheck"] = True
            try:
                callback()
            finally:
                state["inside_recheck"] = False
            assert state["lock_entries"] == lock_count
            assert len(provider_paths) == path_count
            callback_count[0] += 1

        yield checked


def _instrument_retention(
    monkeypatch: pytest.MonkeyPatch,
    provider_paths: list[str],
) -> tuple[list[int], dict[str, object]]:
    state: dict[str, object] = {
        "configuration_locks": 0,
        "credential_locks": 0,
        "lock_entries": 0,
        "inside_recheck": False,
    }
    original_transaction = shared_session.configuration_transaction
    original_credential_lock = shared_session.credential_mutation_lock
    original_retention = read_session.MetaAdsTokenReadSession.retention_recheck

    @contextmanager
    def tracked_transaction(root: Path) -> Iterator[config_module.ConfigurationTransaction]:
        with original_transaction(root) as transaction:
            state["configuration_locks"] = int(state["configuration_locks"]) + 1
            try:
                yield transaction
            finally:
                state["configuration_locks"] = int(state["configuration_locks"]) - 1

    @contextmanager
    def tracked_credential(reference: str) -> Iterator[None]:
        assert state["configuration_locks"] == 1
        with original_credential_lock(reference):
            state["credential_locks"] = int(state["credential_locks"]) + 1
            state["lock_entries"] = int(state["lock_entries"]) + 1
            try:
                yield
            finally:
                state["lock_entries"] = int(state["lock_entries"]) - 1
                state["credential_locks"] = int(state["credential_locks"]) - 1

    callbacks = [0]

    @contextmanager
    def tracked_retention(
        session: read_session.MetaAdsTokenReadSession,
    ) -> Iterator[Callable[[], None]]:
        with _observed_retention(
            original_retention,
            session,
            callback_count=callbacks,
            state=state,
            provider_paths=provider_paths,
        ) as callback:
            yield callback

    monkeypatch.setattr(shared_session, "configuration_transaction", tracked_transaction)
    monkeypatch.setattr(shared_session, "credential_mutation_lock", tracked_credential)
    monkeypatch.setattr(
        read_session.MetaAdsTokenReadSession, "retention_recheck", tracked_retention
    )
    return callbacks, state


# A retained read rechecks retention before its own start, before each of the
# four snapshot appends, and before the append that records the read boundary.
@pytest.mark.parametrize(("ephemeral", "expected_callbacks"), [(False, 6), (True, 1)])
def test_inventory_rechecks_under_ordered_locks_before_every_append_and_ephemeral_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ephemeral: bool,
    expected_callbacks: int,
) -> None:
    _store, _expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []
    callback_active = [False]

    def provider(request: httpx.Request) -> httpx.Response:
        assert not callback_active[0]
        return _complete_provider_response(request, provider_paths)

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)
    callbacks, state = _instrument_retention(monkeypatch, provider_paths)
    original_records = Ledger.records

    def no_ledger_read_during_recheck(ledger: Ledger, stream: str) -> list[dict[str, Any]]:
        assert not callback_active[0]
        return original_records(ledger, stream)

    monkeypatch.setattr(Ledger, "records", no_ledger_read_during_recheck)

    original_recheck = read_session.MetaAdsTokenReadSession.retention_recheck

    @contextmanager
    def callback_guard(
        session: read_session.MetaAdsTokenReadSession,
    ) -> Iterator[Callable[[], None]]:
        with original_recheck(session) as callback:

            def guarded() -> None:
                callback_active[0] = True
                try:
                    callback()
                finally:
                    callback_active[0] = False

            yield guarded

    monkeypatch.setattr(read_session.MetaAdsTokenReadSession, "retention_recheck", callback_guard)
    monkeypatch.setattr(inventory, "export_key", lambda: b"r" * 32)
    monkeypatch.setattr(
        inventory,
        "credential",
        lambda *args, **kwargs: pytest.fail("generic credential access must not see a Meta bundle"),
    )

    result = inventory.run_inventory(
        tmp_path, "meta_ads", "independent-meta-read", ephemeral=ephemeral, maximum_records=1
    )

    assert callbacks == [expected_callbacks]
    assert state["configuration_locks"] == 0
    assert state["credential_locks"] == 0
    assert result["ephemeral"] is ephemeral
    assert len(result["snapshot_record_digests"]) == (0 if ephemeral else 4)
    assert provider_paths == [
        "/v26.0/act_12345",
        "/v26.0/act_12345/campaigns",
        "/v26.0/act_12345/adsets",
        "/v26.0/act_12345/ads",
    ]


@pytest.mark.parametrize(
    ("ephemeral", "expected_callbacks", "expected_projection"),
    [(False, 2, True), (True, 1, False)],
)
def test_report_rechecks_before_retention_and_projects_canonical_provenance_after_locks_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ephemeral: bool,
    expected_callbacks: int,
    expected_projection: bool,
) -> None:
    _store, _expected = _configured(tmp_path, monkeypatch)
    inspection_paths: list[str] = []
    provider_paths: list[str] = []
    callback_active = [False]

    def provider(request: httpx.Request) -> httpx.Response:
        assert not callback_active[0]
        return _complete_provider_response(request, provider_paths)

    _install_http(monkeypatch, httpx.MockTransport(provider), inspection_paths)
    callbacks, state = _instrument_retention(monkeypatch, provider_paths)
    original_retention = read_session.MetaAdsTokenReadSession.retention_recheck
    original_records = Ledger.records
    original_checkpoint = Ledger.checkpoint

    def no_ledger_read_during_recheck(ledger: Ledger, stream: str) -> list[dict[str, Any]]:
        assert not callback_active[0]
        return original_records(ledger, stream)

    def no_checkpoint_during_recheck(ledger: Ledger) -> dict[str, Any]:
        assert not callback_active[0]
        return original_checkpoint(ledger)

    monkeypatch.setattr(Ledger, "records", no_ledger_read_during_recheck)
    monkeypatch.setattr(Ledger, "checkpoint", no_checkpoint_during_recheck)

    @contextmanager
    def callback_guard(
        session: read_session.MetaAdsTokenReadSession,
    ) -> Iterator[Callable[[], None]]:
        with original_retention(session) as callback:

            def guarded() -> None:
                callback_active[0] = True
                try:
                    callback()
                finally:
                    callback_active[0] = False

            yield guarded

    monkeypatch.setattr(read_session.MetaAdsTokenReadSession, "retention_recheck", callback_guard)
    monkeypatch.setattr(workflows, "export_key", lambda: b"r" * 32)
    monkeypatch.setattr(
        workflows,
        "credential",
        lambda *args, **kwargs: pytest.fail("generic credential access must not see a Meta bundle"),
    )
    projected = [False]
    original_finalize = workflows.finalize_report_artifacts

    def finalize_after_close(*args: Any, **kwargs: Any) -> Any:
        assert state["configuration_locks"] == 0
        assert state["credential_locks"] == 0
        projected[0] = True
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(workflows, "finalize_report_artifacts", finalize_after_close)
    query_path = _workflow_query_file(tmp_path)

    result = workflows.run_report(
        tmp_path, "meta_ads", "independent-meta-read", query_path, ephemeral=ephemeral
    )

    canonical_query = MetaAdsReportQuery.model_validate_json(query_path.read_text()).model_copy(
        update={"account_ref": "meta_ads.account.act_12345", "currency": "USD"}
    )
    assert result["envelope"]["query_contract_hash"] == digest(
        canonical_query.model_dump(mode="json", exclude={"query_contract_hash", "report_id"})
    )
    assert bool(result["retained_record_digest"]) is not ephemeral
    assert callbacks == [expected_callbacks]
    assert projected == [expected_projection]
    assert state["configuration_locks"] == 0
    assert state["credential_locks"] == 0
    assert provider_paths == ["/v26.0/act_12345", "/v26.0/act_12345/insights"]

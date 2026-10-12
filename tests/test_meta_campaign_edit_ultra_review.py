"""Independent fake-only review for the Meta campaign EDIT-read boundary.

These checks exercise only in-memory HTTP and token callbacks.  They never
prompt, use an operating-system credential store, or contact Meta.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from adhelm import _meta_ads_token_session as shared_session
from adhelm import config as config_module
from adhelm import meta_ads_token_edit_session as edit_session
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
from adhelm.models import MetaAccountTokenProfile
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.providers.meta_campaign_state import MetaCampaignState, MetaCampaignStateReader
from adhelm.transport import SafeHttpClient

_SUBJECT = "independent-meta-edit-subject"
_INSPECTOR = "independent-meta-edit-inspector"
_NOW = datetime(2030, 1, 4, 12, 0, tzinfo=UTC)
_REAL_HTTP_CLIENT = httpx.Client


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _identity() -> MetaAdsIdentity:
    return MetaAdsIdentity(
        account_ref="meta_ads.account.act_12345",
        account_client_ref="act_12345",
        timezone="UTC",
        currency="USD",
        status="1",
    )


def _reader(
    handler: Callable[[httpx.Request], httpx.Response],
    getter: Callable[[], str],
) -> MetaCampaignStateReader:
    return MetaCampaignStateReader(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
            {"graph.facebook.com"},
            maximum_response_bytes=65_536,
            read_attempts=1,
        ),
        getter,
    )


def _response(*, configured: str = "ACTIVE", effective: str = "PAUSED") -> dict[str, str]:
    return {
        "id": "91",
        "account_id": "12345",
        "configured_status": configured,
        "effective_status": effective,
    }


def _draft(*, purpose: str = "EDIT") -> MetaAccountTokenProfileDraft:
    return MetaAccountTokenProfileDraft.model_validate(
        {
            "profile_id": "independent-meta-edit",
            "account_ref": "12345",
            "client_application_ref": "67890",
            "currency": "USD",
            "timezone": "UTC",
            "purpose": purpose,
            "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
        }
    )


def _wire_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())
    monkeypatch.setattr(
        shared_session, "credential_mutation_lock", lambda _reference: nullcontext()
    )


def _configured_edit(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, purpose: str = "EDIT"
) -> tuple[_MemoryStore, str, MetaAccountTokenProfile]:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    profile = finalize_meta_account_token_profile(_draft(purpose=purpose))
    enroll_meta_token_profile(
        root,
        _draft(purpose=purpose),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return store, digest(load_configuration(root)), profile


def _debug_payload(*, scopes: list[str], now: datetime) -> dict[str, object]:
    return {
        "data": {
            "app_id": "67890",
            "user_id": "54321",
            "is_valid": True,
            "type": "USER",
            "scopes": scopes,
            "issued_at": int((now - timedelta(minutes=1)).timestamp()),
            "expires_at": int((now + timedelta(minutes=5)).timestamp()),
            "data_access_expires_at": int((now + timedelta(days=1)).timestamp()),
        }
    }


def _install_session_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    clock: list[datetime],
    scopes: list[str] | None = None,
    campaign: dict[str, object] | None = None,
) -> None:
    inspected_scopes = ["ads_management", "public_profile"] if scopes is None else scopes
    observed_campaign = _response() if campaign is None else campaign

    def inspection_client(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False

        def inspection_handler(request: httpx.Request) -> httpx.Response:
            events.append(request.url.path)
            assert request.method == "GET"
            assert request.url.path == "/v26.0/debug_token"
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            return httpx.Response(200, json=_debug_payload(scopes=inspected_scopes, now=clock[0]))

        return _REAL_HTTP_CLIENT(transport=httpx.MockTransport(inspection_handler), trust_env=False)

    def provider_handler(request: httpx.Request) -> httpx.Response:
        events.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        assert _INSPECTOR not in str(request.headers)
        if request.url.path == "/v26.0/act_12345":
            return httpx.Response(
                200,
                json={
                    "id": "12345",
                    "account_status": 1,
                    "currency": "USD",
                    "timezone_name": "UTC",
                },
            )
        assert request.url.path == "/v26.0/91"
        assert request.url.params["fields"] == "id,account_id,configured_status,effective_status"
        return httpx.Response(200, json=observed_campaign)

    monkeypatch.setattr(inspection.httpx, "Client", inspection_client)
    monkeypatch.setattr(inspection, "_now", lambda: clock[0])
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)
    monkeypatch.setattr(shared_session, "_now", lambda: clock[0])
    monkeypatch.setattr(
        shared_session,
        "_meta_http_client",
        lambda: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(provider_handler), trust_env=False),
    )


def test_constructed_identity_and_noncanonical_campaign_stop_before_token_or_http() -> None:
    """Private/model-constructed input cannot burn a subject-token callback."""

    token_calls = 0
    http_calls = 0

    def getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return _SUBJECT

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json=_response())

    reader = _reader(handler, getter)
    forged_identity = object.__new__(MetaAdsIdentity)

    with pytest.raises(AdhelmError) as identity_error:
        reader.read_campaign_state(forged_identity, "91")
    assert identity_error.value.code == "IDENTITY_BINDING_INVALID"

    with pytest.raises(AdhelmError) as campaign_error:
        reader.read_campaign_state(_identity(), "91\n")
    assert campaign_error.value.code == "INVALID_INPUT"
    assert token_calls == http_calls == 0


def test_constructed_campaign_reader_rejects_before_input_can_reach_a_callback() -> None:
    """The standalone fixed reader has no raw constructed-object execution path."""

    forged = object.__new__(MetaCampaignStateReader)

    with pytest.raises(AdhelmError) as raised:
        forged.read_campaign_state(_identity(), "91")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"


def test_mutated_client_query_is_rejected_before_guarded_token_or_fixed_get() -> None:
    """No caller-supplied query can be smuggled through a mutable transport."""

    token_calls = 0
    http_calls = 0

    def getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return _SUBJECT

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json=_response())

    reader = _reader(handler, getter)
    transport = reader._transport
    transport.client.params = {"access_token": "must-not-be-sent"}

    with pytest.raises(AdhelmError) as raised:
        reader.read_campaign_state(_identity(), "91")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert token_calls == http_calls == 0


def test_single_fixed_get_preserves_distinct_configured_and_effective_statuses() -> None:
    """A one-attempt error never retries and a successful state never collapses statuses."""

    token_calls = 0
    failed_http_calls = 0

    def getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return _SUBJECT

    def unavailable(_request: httpx.Request) -> httpx.Response:
        nonlocal failed_http_calls
        failed_http_calls += 1
        return httpx.Response(503)

    with pytest.raises(AdhelmError) as unavailable_error:
        _reader(unavailable, getter).read_campaign_state(_identity(), "91")
    assert unavailable_error.value.code == "PROVIDER_REQUEST_FAILED"
    assert token_calls == failed_http_calls == 1

    successful_http_calls = 0

    def success(request: httpx.Request) -> httpx.Response:
        nonlocal successful_http_calls
        successful_http_calls += 1
        assert request.url.path == "/v26.0/91"
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        return httpx.Response(200, json=_response(configured="ACTIVE", effective="PAUSED"))

    state = _reader(success, lambda: _SUBJECT).read_campaign_state(_identity(), "91")
    assert successful_http_calls == 1
    assert state.configured_status == "ACTIVE"
    assert state.effective_status == "PAUSED"
    assert set(state.model_dump(mode="json")) == {
        "campaign_client_ref",
        "campaign_ref",
        "account_client_ref",
        "configured_status",
        "effective_status",
    }


def test_campaign_state_rejects_authority_or_current_live_claims() -> None:
    """The retained shape remains an observation rather than an execution result."""

    with pytest.raises(ValidationError):
        MetaCampaignState.model_validate(
            {
                "campaign_client_ref": "91",
                "campaign_ref": "meta_ads.campaign.91",
                "account_client_ref": "12345",
                "configured_status": "ACTIVE",
                "effective_status": "PAUSED",
                "execution_authority": True,
            }
        )


def test_invalid_edit_composition_input_stops_before_bundle_or_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory rejects raw composition aliases before it can select a token slot."""

    prepared: list[object] = []

    def no_prepare(*args: object, **kwargs: object) -> object:
        prepared.append((args, kwargs))
        raise AssertionError("invalid composition input must not prepare a token inspection")

    monkeypatch.setattr(token_credentials, "_prepare_meta_token_inspection", no_prepare)

    with pytest.raises(AdhelmError) as raised:
        with edit_session.configured_meta_ads_token_edit_session(
            tmp_path,
            "independent-meta-edit",
            expected_configuration_digest=True,  # type: ignore[arg-type]
        ):
            pytest.fail("invalid composition input must not yield an EDIT session")

    assert raised.value.code == "META_TOKEN_EDIT_SESSION_INVALID"
    assert prepared == []


def test_constructed_edit_session_rejects_before_any_local_or_provider_access() -> None:
    """A missing private session shape cannot turn into a raw runtime exception."""

    forged = object.__new__(edit_session.MetaAdsTokenEditSession)

    with pytest.raises(AdhelmError) as raised:
        forged.read_campaign_state("91")

    assert raised.value.code == "META_TOKEN_EDIT_SESSION_INVALID"


def test_edit_session_requires_ads_management_before_account_or_campaign_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EDIT declaration cannot treat a read-only inspected scope as editable."""

    _store, expected, _profile = _configured_edit(tmp_path, monkeypatch)
    events: list[str] = []
    _install_session_http(
        monkeypatch,
        events=events,
        clock=[_NOW],
        scopes=["ads_read", "public_profile"],
    )

    with pytest.raises(AdhelmError) as raised:
        with edit_session.configured_meta_ads_token_edit_session(
            tmp_path,
            "independent-meta-edit",
            expected_configuration_digest=expected,
        ):
            pytest.fail("ads_read inspection must not yield an EDIT session")

    assert raised.value.code == "META_TOKEN_SCOPE_REQUIRED"
    assert events == ["/v26.0/debug_token"]


def test_edit_session_keeps_inspector_private_and_state_non_authorizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The yielded session exposes only bounded inspection facts and state."""

    _store, expected, _profile = _configured_edit(tmp_path, monkeypatch)
    events: list[str] = []
    _install_session_http(monkeypatch, events=events, clock=[_NOW])

    with edit_session.configured_meta_ads_token_edit_session(
        tmp_path,
        "independent-meta-edit",
        expected_configuration_digest=expected,
    ) as session:
        state = session.read_campaign_state("91")
        binding = session.binding

        assert state.configured_status == "ACTIVE"
        assert state.effective_status == "PAUSED"
        assert binding.required_scope == "ads_management"
        assert binding.profile_ref == "independent-meta-edit"
        assert binding.configuration_digest == expected
        assert _SUBJECT not in repr(session)
        assert _INSPECTOR not in repr(session)
        assert _SUBJECT not in repr(binding)
        assert _INSPECTOR not in repr(binding)
        assert "54321" not in binding.principal_ref
        assert not {"token", "credential", "adapter", "client"} & {
            name for name in dir(type(session)) if not name.startswith("_")
        }
        assert not {"grant", "current_live", "execution_authority"} & set(
            state.model_dump(mode="json")
        )
        with pytest.raises(TypeError):
            json.dumps(binding)

    assert events == ["/v26.0/debug_token", "/v26.0/act_12345", "/v26.0/91"]


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("configuration", "CONFIGURATION_CHANGED"),
        ("bundle", "META_TOKEN_CREDENTIAL_CHANGED"),
        ("expiry", "META_TOKEN_EXPIRED"),
    ],
)
def test_edit_campaign_get_rechecks_configuration_bundle_and_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected_code: str,
) -> None:
    """A local drift after account proof stops the campaign GET before egress."""

    store, expected, profile = _configured_edit(tmp_path, monkeypatch)
    events: list[str] = []
    clock = [_NOW]
    _install_session_http(monkeypatch, events=events, clock=clock)

    with edit_session.configured_meta_ads_token_edit_session(
        tmp_path,
        "independent-meta-edit",
        expected_configuration_digest=expected,
    ) as session:
        if change == "configuration":
            configuration = load_configuration(tmp_path)
            save_configuration(
                tmp_path,
                configuration.model_copy(
                    update={
                        "output": configuration.output.model_copy(update={"max_output_bytes": 4096})
                    }
                ),
                replace=True,
            )
        elif change == "bundle":
            store.values[(token_credentials._SERVICE, profile.credential_ref)] = (
                token_credentials._new_bundle(
                    profile,
                    "changed-independent-subject",
                    "changed-independent-inspector",
                )._encoded()
            )
        else:
            clock[0] = _NOW + timedelta(minutes=5)

        with pytest.raises(AdhelmError) as raised:
            session.read_campaign_state("91")

    assert raised.value.code == expected_code
    assert _SUBJECT not in raised.value.message
    assert _INSPECTOR not in raised.value.message
    assert events == ["/v26.0/debug_token", "/v26.0/act_12345"]


def test_edit_retention_recheck_holds_ordered_locks_without_network_or_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback is repeatable only in-context and never opens locks or a GET."""

    _store, expected, _profile = _configured_edit(tmp_path, monkeypatch)
    events: list[str] = []
    _install_session_http(monkeypatch, events=events, clock=[_NOW])

    lock_entries: list[str] = []
    original_configuration_transaction = shared_session.configuration_transaction
    original_credential_lock = shared_session.credential_mutation_lock

    @contextmanager
    def tracked_configuration(directory: Path) -> Iterator[object]:
        lock_entries.append("configuration")
        with original_configuration_transaction(directory) as transaction:
            yield transaction

    @contextmanager
    def tracked_credential(reference: str) -> Iterator[None]:
        lock_entries.append("credential")
        with original_credential_lock(reference):
            yield

    with edit_session.configured_meta_ads_token_edit_session(
        tmp_path,
        "independent-meta-edit",
        expected_configuration_digest=expected,
    ) as session:
        monkeypatch.setattr(shared_session, "configuration_transaction", tracked_configuration)
        monkeypatch.setattr(shared_session, "credential_mutation_lock", tracked_credential)
        before_retention = list(events)
        with session.retention_recheck() as recheck:
            assert lock_entries == ["configuration", "credential"]
            recheck()
            recheck()
            assert lock_entries == ["configuration", "credential"]
            with pytest.raises(AdhelmError) as busy:
                session.read_campaign_state("91")
            assert busy.value.code == "META_TOKEN_EDIT_SESSION_BUSY"
            assert events == before_retention
            escaped = recheck

        with pytest.raises(AdhelmError) as escaped_error:
            escaped()

    assert escaped_error.value.code == "META_TOKEN_EDIT_RETENTION_RECHECK_REQUIRED"
    assert lock_entries == ["configuration", "credential"]
    assert events == ["/v26.0/debug_token", "/v26.0/act_12345"]
    assert not (tmp_path / "ledger").exists()

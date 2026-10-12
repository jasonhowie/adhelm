"""EDIT preparation through the actual fixed inspector and campaign reader, offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from adhelm import meta_ads_token_edit_session as edits
from adhelm import meta_token_credentials as credentials
from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile, rotate_meta_token_credentials
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft

_REAL_HTTP = httpx.Client
_SUBJECT = "synthetic-meta-edit-subject"
_INSPECTOR = "synthetic-meta-app-inspector"


class _Store:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _configure(root: Path, monkeypatch: pytest.MonkeyPatch, *, purpose: str = "EDIT") -> str:
    store = _Store()
    monkeypatch.setattr(credentials, "_credential_backend", lambda: store)
    draft = MetaAccountTokenProfileDraft.model_validate(
        {
            "profile_id": "meta-editor",
            "account_ref": "12345",
            "client_application_ref": "67890",
            "purpose": purpose,
            "currency": "USD",
            "timezone": "UTC",
            "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
        }
    )
    enroll_meta_token_profile(
        root,
        draft,
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return digest(load_configuration(root))


def _http(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    campaign_account: str = "12345",
    rotate_after_campaign: bool = False,
) -> list[str]:
    paths: list[str] = []
    now = datetime.now(UTC)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "graph.facebook.com"
        paths.append(request.url.path)
        data: dict[str, Any]
        if request.url.path == "/v26.0/debug_token":
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            data = {
                "data": {
                    "app_id": "67890",
                    "user_id": "99999",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": ["ads_management", "public_profile"],
                    "expires_at": int((now + timedelta(hours=1)).timestamp()),
                    "data_access_expires_at": int((now + timedelta(hours=2)).timestamp()),
                }
            }
        else:
            assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
            assert "input_token" not in request.url.params
            if request.url.path == "/v26.0/act_12345":
                data = {
                    "id": "12345",
                    "account_status": 1,
                    "currency": "USD",
                    "timezone_name": "UTC",
                }
            else:
                assert request.url.path == "/v26.0/91"
                assert (
                    request.url.params["fields"]
                    == "id,account_id,configured_status,effective_status"
                )
                data = {
                    "id": "91",
                    "account_id": campaign_account,
                    "configured_status": "ACTIVE",
                    "effective_status": "WITH_ISSUES",
                }
                if rotate_after_campaign:
                    rotate_meta_token_credentials(
                        root,
                        "meta-editor",
                        subject_token_supplier=lambda: "replacement-subject",
                        inspector_token_supplier=lambda: "replacement-inspector",
                    )
        return httpx.Response(200, json=data)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_HTTP(transport=httpx.MockTransport(handle), **kwargs),
    )
    return paths


def test_edit_preparation_binds_inspection_account_and_campaign_without_writing(
    root_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _configure(root_path, monkeypatch)
    paths = _http(root_path, monkeypatch)
    before = load_configuration(root_path)
    with edits.configured_meta_ads_token_edit_session(
        root_path, "meta-editor", expected_configuration_digest=expected
    ) as session:
        state = session.read_campaign_state("91")
        assert state.campaign_client_ref == "91"
        assert state.account_client_ref == "12345"
        assert state.configured_status == "ACTIVE"
        assert state.effective_status == "WITH_ISSUES"
        binding = session.binding
        assert binding.profile_ref == "meta-editor"
        assert binding.client_application_ref == "67890"
        assert binding.required_scope == "ads_management"
        assert binding.configuration_digest == expected
        assert binding.profile_configuration_digest == digest(before.profiles[0])
        assert binding.credential_binding_ref.startswith("credential-binding:sha256:")
        assert binding.principal_ref.startswith("meta_token_principal_v1.")
        assert "99999" not in binding.principal_ref
        assert binding.observed_at < binding.expires_at
        assert binding.observed_at < binding.data_access_expires_at
        for token in (_SUBJECT, _INSPECTOR):
            assert token not in repr(binding)
            assert token not in repr(session)
        with pytest.raises(TypeError):
            json.dumps(binding)
        with session.retention_recheck() as check:
            check()
            check()
    assert paths == ["/v26.0/debug_token", "/v26.0/act_12345", "/v26.0/91"]
    assert load_configuration(root_path) == before
    assert not (root_path / "ledger").exists()
    with pytest.raises(AdhelmError):
        session.read_campaign_state("91")
    with pytest.raises(AdhelmError):
        check()


@pytest.fixture
def root_path(tmp_path: Path) -> Path:
    return tmp_path


def test_read_profile_cannot_enter_edit_observation_session(
    root_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _configure(root_path, monkeypatch, purpose="READ")
    paths = _http(root_path, monkeypatch)
    with pytest.raises(AdhelmError):
        with edits.configured_meta_ads_token_edit_session(
            root_path, "meta-editor", expected_configuration_digest=expected
        ):
            pytest.fail("READ declaration must not enter the EDIT facade")
    assert paths == []


def test_campaign_parent_mismatch_fails_before_retention(
    root_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _configure(root_path, monkeypatch)
    paths = _http(root_path, monkeypatch, campaign_account="54321")
    with edits.configured_meta_ads_token_edit_session(
        root_path, "meta-editor", expected_configuration_digest=expected
    ) as session:
        with pytest.raises(AdhelmError):
            session.read_campaign_state("91")
    assert len(paths) == 3
    assert not (root_path / "ledger").exists()


def test_rotation_after_campaign_response_invalidates_retention(
    root_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _configure(root_path, monkeypatch)
    paths = _http(root_path, monkeypatch, rotate_after_campaign=True)
    with edits.configured_meta_ads_token_edit_session(
        root_path, "meta-editor", expected_configuration_digest=expected
    ) as session:
        with pytest.raises(AdhelmError):
            session.read_campaign_state("91")
            with session.retention_recheck() as check:
                check()
    assert len(paths) == 3
    assert not (root_path / "ledger").exists()

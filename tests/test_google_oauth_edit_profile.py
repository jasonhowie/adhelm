"""Closed GA4 Admin EDIT OAuth profile regressions with fake local storage only."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from rich.text import Text
from typer.testing import CliRunner

from adhelm import credential_setup, credentials
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm import google_oauth_workflow as workflow
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.google_oauth_cli import app
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.google_oauth_setup import GoogleOAuthProfileDraft
from adhelm.models import Configuration, OutputPreferences, Profile, validate_configured_profile

_CLIENT = "ga4_edit_client.apps.googleusercontent.com"
_SUBJECT = "synthetic-ga4-edit-subject"
_READ_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_NOW = datetime(2029, 1, 1, tzinfo=UTC)


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    writes: int = 0

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes += 1
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _draft(
    *, purpose: str = "EDIT", scopes: list[str] | None = None, provider: str = "ga4_admin"
) -> dict[str, object]:
    return {
        "profile_id": "ga4-admin-edit",
        "provider": provider,
        "account_ref": "12345",
        "client_ref": _CLIENT,
        "scopes": scopes if scopes is not None else [_EDIT_SCOPE],
        "purpose": purpose,
        "currency": "USD",
        "timezone": "UTC",
        "credential_ref": "ga4-admin-edit-oauth",
    }


def _profile(
    purpose: str,
    *,
    credential_ref: str = "ga4-admin-oauth",
) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", purpose)]
    return Profile(
        profile_id=f"ga4-admin-{purpose.lower()}",
        provider="ga4_admin",
        account_ref="12345",
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        client_ref=_CLIENT,
        scopes=[scope],
        purpose=purpose,
        currency="USD",
        timezone="UTC",
        credential_source="OS_OAUTH_GOOGLE",
        credential_ref=credential_ref,
    )


def _grant(
    profile: Profile, *, returned_scopes: tuple[str, ...] | None = None
) -> GoogleOAuthInitialGrant:
    scope = profile.scopes[0]
    identity = VerifiedGoogleOidcIdentity(
        canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
        raw_subject=_SUBJECT,
        principal_ref=profile.principal_ref,
        client_ref=profile.client_ref,
    )
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, scope),
        access_token="fixture-edit-access-token",
        expires_at=_NOW + timedelta(hours=1),
        refresh_token="fixture-edit-refresh-token",
        refresh_token_expires_at=None,
        requested_scopes=("openid", scope),
        returned_scopes=returned_scopes or (scope,),
        identity=identity,
    )


def _fake_lifecycle_store(monkeypatch: pytest.MonkeyPatch) -> _MemoryStore:
    store = _MemoryStore()
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(oauth_credentials, "credential_mutation_lock", lambda _: nullcontext())
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)
    return store


def _private_client_file(tmp_path: Path) -> Path:
    path = tmp_path / "private-client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": _CLIENT,
                    "client_secret": "synthetic-private-client-secret",
                }
            }
        )
    )
    path.chmod(0o600)
    return path


def _install(
    monkeypatch: pytest.MonkeyPatch, profile: Profile
) -> tuple[_MemoryStore, Configuration]:
    store = _fake_lifecycle_store(monkeypatch)
    configuration = Configuration(profiles=[profile])
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile),
        client_secret=None,
        expected_slot=slot,
    )
    return store, configuration


def test_ga4_admin_edit_draft_and_profile_have_one_exact_scope() -> None:
    draft = GoogleOAuthProfileDraft.model_validate(_draft())
    profile = _profile("EDIT")

    assert draft.purpose == "EDIT"
    assert draft.scopes == [_EDIT_SCOPE]
    assert profile.purpose == "EDIT"
    assert profile.scopes == [_EDIT_SCOPE]
    assert GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", "EDIT")] == _EDIT_SCOPE


@pytest.mark.parametrize(
    ("provider", "purpose", "scopes"),
    [
        ("ga4", "EDIT", [_EDIT_SCOPE]),
        ("google_ads", "ACTIVATE", ["https://www.googleapis.com/auth/adwords"]),
        ("gtm", "EDIT", ["https://www.googleapis.com/auth/tagmanager.readonly"]),
        ("ga4_admin", "ACTIVATE", [_EDIT_SCOPE]),
        ("ga4_admin", "PUBLISH", [_EDIT_SCOPE]),
    ],
)
def test_google_oauth_draft_rejects_unsupported_edit_or_higher_targets(
    provider: str, purpose: str, scopes: list[str]
) -> None:
    with pytest.raises(ValidationError):
        GoogleOAuthProfileDraft.model_validate(
            _draft(provider=provider, purpose=purpose, scopes=scopes)
        )


@pytest.mark.parametrize(
    ("provider", "purpose", "scope", "account_ref"),
    [
        ("ga4", "EDIT", _EDIT_SCOPE, "12345"),
        ("google_ads", "ACTIVATE", "https://www.googleapis.com/auth/adwords", "1234567890"),
        ("gtm", "EDIT", "https://www.googleapis.com/auth/tagmanager.readonly", "12345"),
        ("ga4_admin", "ACTIVATE", _EDIT_SCOPE, "12345"),
        ("ga4_admin", "PUBLISH", _EDIT_SCOPE, "12345"),
    ],
)
def test_google_oauth_profile_rejects_unsupported_edit_or_higher_targets(
    provider: str,
    purpose: str,
    scope: str,
    account_ref: str,
) -> None:
    raw = _profile("EDIT").model_dump(mode="python")
    raw.update(
        {
            "provider": provider,
            "purpose": purpose,
            "scopes": [scope],
            "account_ref": account_ref,
        }
    )
    with pytest.raises(ValidationError):
        Profile.model_validate(raw)


@pytest.mark.parametrize(
    "scopes",
    [
        [_READ_SCOPE],
        [_EDIT_SCOPE, _READ_SCOPE],
        ["openid", _EDIT_SCOPE],
        ["https://www.googleapis.com/auth/analytics"],
    ],
)
def test_edit_profile_rejects_read_or_widened_scope_lists(scopes: list[str]) -> None:
    raw = _profile("EDIT").model_dump(mode="python")
    raw["scopes"] = scopes
    with pytest.raises(ValidationError):
        Profile.model_validate(raw)


def test_model_copy_cannot_change_a_read_profile_purpose_without_its_scope() -> None:
    forged = _profile("READ").model_copy(update={"purpose": "EDIT"})
    with pytest.raises(ValidationError):
        validate_configured_profile(forged)


@pytest.mark.parametrize(
    ("stored_purpose", "requested_purpose"), [("READ", "EDIT"), ("EDIT", "READ")]
)
def test_bundle_cannot_cross_read_and_edit_profile_purposes(
    monkeypatch: pytest.MonkeyPatch, stored_purpose: str, requested_purpose: str
) -> None:
    stored_profile = _profile(stored_purpose, credential_ref="shared-adversarial-slot")
    requested_profile = _profile(requested_purpose, credential_ref="shared-adversarial-slot")
    store, _ = _install(monkeypatch, stored_profile)
    writes_before = store.writes

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(requested_profile)

    assert raised.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"
    assert store.writes == writes_before


def test_reconnect_install_rejects_an_edit_grant_for_a_read_profile_before_store_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _fake_lifecycle_store(monkeypatch)
    read_profile = _profile("READ")
    edit_profile = _profile("EDIT")
    configuration = Configuration(profiles=[read_profile])
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, read_profile.profile_id, replace=True
    )

    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.install_google_oauth_grant(
            configuration,
            read_profile.profile_id,
            _grant(edit_profile),
            client_secret=None,
            expected_slot=slot,
            replace=True,
        )

    assert raised.value.code == "GOOGLE_OAUTH_GRANT_BINDING_INVALID"
    assert store.values == {}


def test_workflow_reconnect_never_widens_a_read_profile_to_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    read_profile = _profile("READ")
    configuration = Configuration(profiles=[read_profile])
    save_configuration(directory, configuration)
    store = _fake_lifecycle_store(monkeypatch)
    initial_slot = oauth_credentials.prepare_google_oauth_slot(
        read_profile.credential_ref, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        read_profile.profile_id,
        _grant(read_profile),
        client_secret=None,
        expected_slot=initial_slot,
    )
    before = dict(store.values)
    monkeypatch.setattr(workflow, "_require_interactive", lambda _interactive: None)
    seen_targets: list[GoogleOAuthTarget] = []

    def issue_edit_grant(target: GoogleOAuthTarget, _client: object) -> GoogleOAuthInitialGrant:
        seen_targets.append(target)
        return _grant(_profile("EDIT"))

    monkeypatch.setattr(workflow, "_browser_grant", issue_edit_grant)

    with pytest.raises(AdhelmError) as raised:
        workflow.reconnect_google_oauth(
            directory,
            read_profile.profile_id,
            _private_client_file(tmp_path),
            interactive=True,
        )

    assert raised.value.code == "GOOGLE_OAUTH_GRANT_BINDING_INVALID"
    assert seen_targets == [GoogleOAuthTarget(_CLIENT, _READ_SCOPE)]
    assert store.values == before


def test_edit_grant_rejects_returned_scope_widening() -> None:
    with pytest.raises(ValueError):
        _grant(_profile("EDIT"), returned_scopes=("openid", _EDIT_SCOPE, _READ_SCOPE))


def test_workflow_receipt_reports_the_configured_edit_purpose_without_authority() -> None:
    result = workflow._result(_profile("EDIT"), operation="ENROLL")

    assert result["purpose"] == "EDIT"
    assert result["resource_access_verified"] is False
    assert result["execution_authority"] is False


def test_edit_enrollment_uses_only_the_edit_target_and_keeps_no_authority_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    draft_file = tmp_path / "ga4-admin-edit.json"
    draft_file.write_text(json.dumps(_draft()))
    client_file = _private_client_file(tmp_path)
    store = _fake_lifecycle_store(monkeypatch)
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(workflow, "_require_interactive", lambda _interactive: None)

    def issue_edit_grant(target: GoogleOAuthTarget, _client: object) -> GoogleOAuthInitialGrant:
        assert target.api_scope == _EDIT_SCOPE
        return _grant(_profile("EDIT"))

    monkeypatch.setattr(workflow, "_browser_grant", issue_edit_grant)
    result = workflow.enroll_google_oauth(directory, draft_file, client_file, interactive=True)
    profile = load_configuration(directory).profiles[0]

    assert isinstance(profile, Profile)
    assert profile.purpose == "EDIT"
    assert profile.scopes == [_EDIT_SCOPE]
    assert result["purpose"] == "EDIT"
    assert result["resource_access_verified"] is False
    assert result["execution_authority"] is False
    assert credentials.credential(profile) == "fixture-edit-access-token"
    assert _SUBJECT not in json.dumps(result)
    assert "synthetic-private-client-secret" not in json.dumps(result)


@pytest.mark.parametrize("command", ["enroll", "reconnect"])
def test_cli_help_does_not_imply_an_enrollment_or_reconnect_reads_or_mutates_a_resource(
    command: str,
) -> None:
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0, result.output
    assert "no Google resource read or mutation occurs" in " ".join(
        Text.from_ansi(result.output).plain.split()
    )

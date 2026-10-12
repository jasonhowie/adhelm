"""Independent fake-only adversarial checks for Google OAuth enrollment workflow."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import credential_setup, credentials, google_oauth_cli
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm import google_oauth_workflow as workflow
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.models import Configuration, OutputPreferences, Profile

CLIENT = "123456-workflow-ultra.apps.googleusercontent.com"
ACCESS = "workflow-ultra-access-token"
REFRESH = "workflow-ultra-refresh-token"
CLIENT_SECRET = "workflow-ultra-client-secret"
SUBJECT = "workflow-ultra-raw-subject"


@dataclass
class _FakeStore:
    """In-memory protected-store seam; it never touches an OS credential store."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)
    drop_writes: bool = False
    reads: int = 0
    writes: int = 0
    services: list[str] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads += 1
        self.services.append(service_name)
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes += 1
        self.services.append(service_name)
        if not self.drop_writes:
            self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.services.append(service_name)
        self.values.pop((service_name, username), None)


@dataclass(frozen=True)
class _LocalWorkflow:
    directory: Path
    draft: Path
    client: Path
    store: _FakeStore


def _scope(provider: str) -> str:
    return GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[(provider, "READ")]


def _draft_data(provider: str = "ga4") -> dict[str, object]:
    data: dict[str, object] = {
        "profile_id": f"workflow-{provider}-read",
        "provider": provider,
        "account_ref": "1234567890" if provider == "google_ads" else "12345",
        "client_ref": CLIENT,
        "scopes": [_scope(provider)],
        "credential_ref": f"workflow-{provider}-oauth-primary",
        "currency": "USD",
    }
    if provider == "google_ads":
        data["secondary_credential_ref"] = "workflow-google-ads-secondary"
    return data


def _client_file(tmp_path: Path) -> Path:
    path = tmp_path / "private-workflow-client.json"
    path.write_text(
        json.dumps({"installed": {"client_id": CLIENT, "client_secret": CLIENT_SECRET}})
    )
    path.chmod(0o600)
    return path


def _grant(provider: str = "ga4", subject: str = SUBJECT) -> GoogleOAuthInitialGrant:
    identity = VerifiedGoogleOidcIdentity(
        canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
        raw_subject=subject,
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, subject),
        client_ref=CLIENT,
    )
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(CLIENT, _scope(provider)),
        access_token=ACCESS,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        refresh_token=REFRESH,
        refresh_token_expires_at=None,
        requested_scopes=("openid", _scope(provider)),
        returned_scopes=("openid", _scope(provider)),
        identity=identity,
    )


@pytest.fixture
def local_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _LocalWorkflow:
    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(_draft_data()))
    client = _client_file(tmp_path)
    store = _FakeStore()
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(workflow, "_require_interactive", lambda _: None)
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: _grant())
    return _LocalWorkflow(directory=directory, draft=draft, client=client, store=store)


def _enroll_args(local: _LocalWorkflow) -> list[str]:
    return [
        "--data-dir",
        str(local.directory),
        "auth",
        "google-oauth",
        "enroll",
        "--input",
        str(local.draft),
        "--client-config",
        str(local.client),
        "--interactive",
    ]


def _assert_not_public(rendered: str) -> None:
    for secret in (ACCESS, REFRESH, CLIENT_SECRET, SUBJECT):
        assert secret not in rendered


def _stored_value(store: _FakeStore, reference: str) -> str:
    values = [value for (_service, name), value in store.values.items() if name == reference]
    assert len(values) == 1
    return values[0]


def test_invalid_draft_stops_before_client_slot_or_browser(
    local_workflow: _LocalWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_workflow.draft.write_text(
        json.dumps({**_draft_data(), "schema_version": 1.0, "unexpected_secret": ACCESS})
    )
    monkeypatch.setattr(
        workflow,
        "read_google_installed_client",
        lambda *_args, **_kwargs: pytest.fail("client file read after invalid draft"),
    )
    monkeypatch.setattr(
        workflow,
        "_browser_grant",
        lambda *_: pytest.fail("browser handoff after invalid draft"),
    )
    monkeypatch.setattr(
        oauth_credentials,
        "prepare_google_oauth_slot",
        lambda *_args, **_kwargs: pytest.fail("credential slot read after invalid draft"),
    )

    result = CliRunner().invoke(app, _enroll_args(local_workflow))

    assert result.exit_code == 2, result.output
    output = json.loads(result.output)
    assert output["diagnostics"][0]["code"] == "GOOGLE_OAUTH_DRAFT_INVALID"
    assert local_workflow.store.reads == 0
    assert local_workflow.store.writes == 0
    assert load_configuration(local_workflow.directory).profiles == []
    _assert_not_public(result.output)


def test_explicit_interactive_gate_precedes_configuration_and_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        google_oauth_cli,
        "load_configuration",
        lambda *_: pytest.fail("configuration read before explicit local handoff"),
    )
    monkeypatch.setattr(
        workflow,
        "_browser_grant",
        lambda *_: pytest.fail("browser handoff without explicit local request"),
    )

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "missing"),
            "auth",
            "google-oauth",
            "enroll",
            "--input",
            str(tmp_path / "missing-draft.json"),
            "--client-config",
            str(tmp_path / "missing-client.json"),
        ],
    )

    assert result.exit_code == 4, result.output
    assert (
        json.loads(result.output)["diagnostics"][0]["code"] == "EXPLICIT_BROWSER_HANDOFF_REQUIRED"
    )
    assert not (tmp_path / "missing").exists()


def test_explicit_interactive_handoff_requires_local_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NotTty(StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(workflow.sys, "stdin", _NotTty())
    monkeypatch.setattr(workflow.sys, "stderr", _NotTty())

    with pytest.raises(AdhelmError) as failure:
        workflow._require_interactive(True)

    assert failure.value.code == "INTERACTIVE_TERMINAL_REQUIRED"


def test_post_browser_configuration_drift_prevents_profile_or_bundle_write(
    local_workflow: _LocalWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    def changed_configuration(*_: object) -> GoogleOAuthInitialGrant:
        save_configuration(
            local_workflow.directory,
            Configuration(output=OutputPreferences(artifact_mode="OFF", max_output_bytes=1024)),
            replace=True,
        )
        return _grant()

    monkeypatch.setattr(workflow, "_browser_grant", changed_configuration)

    with pytest.raises(AdhelmError) as failure:
        workflow.enroll_google_oauth(
            local_workflow.directory,
            local_workflow.draft,
            local_workflow.client,
            interactive=True,
        )

    assert failure.value.code == "CONFIGURATION_CHANGED"
    assert load_configuration(local_workflow.directory).output.artifact_mode == "OFF"
    assert load_configuration(local_workflow.directory).profiles == []
    assert local_workflow.store.values == {}
    assert not (local_workflow.directory / "artifacts").exists()


def test_reconnect_cannot_replace_slot_removed_while_browser_is_open(
    local_workflow: _LocalWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow.enroll_google_oauth(
        local_workflow.directory,
        local_workflow.draft,
        local_workflow.client,
        interactive=True,
    )

    def remove_then_return(*_: object) -> GoogleOAuthInitialGrant:
        credential_setup.local_remove(
            load_configuration(local_workflow.directory), "workflow-ga4-read", "PRIMARY"
        )
        return _grant()

    monkeypatch.setattr(workflow, "_browser_grant", remove_then_return)
    with pytest.raises(AdhelmError) as failure:
        workflow.reconnect_google_oauth(
            local_workflow.directory,
            "workflow-ga4-read",
            local_workflow.client,
            interactive=True,
        )

    assert failure.value.code == "CREDENTIAL_CONCURRENT_CHANGE"
    assert local_workflow.store.values == {}


def test_oauth_status_and_remove_use_only_the_dedicated_bundle_service(
    local_workflow: _LocalWorkflow,
) -> None:
    workflow.enroll_google_oauth(
        local_workflow.directory,
        local_workflow.draft,
        local_workflow.client,
        interactive=True,
    )
    configuration = load_configuration(local_workflow.directory)
    before_status = len(local_workflow.store.services)

    present = credential_setup.credential_status(configuration, "workflow-ga4-read")

    assert [result.state for result in present] == ["PRESENT"]
    assert local_workflow.store.services[before_status:] == ["adhelm.google-oauth-v1"]
    credential_setup.local_remove(configuration, "workflow-ga4-read", "PRIMARY")
    before_missing_status = len(local_workflow.store.services)

    missing = credential_setup.credential_status(configuration, "workflow-ga4-read")

    assert [result.state for result in missing] == ["MISSING"]
    assert local_workflow.store.services[before_missing_status:] == ["adhelm.google-oauth-v1"]


def test_config_first_uncertainty_can_recover_only_through_explicit_reconnect(
    local_workflow: _LocalWorkflow,
) -> None:
    local_workflow.store.drop_writes = True
    with pytest.raises(AdhelmError) as initial_failure:
        workflow.enroll_google_oauth(
            local_workflow.directory,
            local_workflow.draft,
            local_workflow.client,
            interactive=True,
        )
    assert initial_failure.value.code == "GOOGLE_OAUTH_ENROLLMENT_LOCAL_STATE_UNVERIFIED"
    configured = load_configuration(local_workflow.directory)
    assert [profile.profile_id for profile in configured.profiles] == ["workflow-ga4-read"]
    assert local_workflow.store.values == {}
    _assert_not_public(str(initial_failure.value))

    local_workflow.store.drop_writes = False
    recovered = workflow.reconnect_google_oauth(
        local_workflow.directory,
        "workflow-ga4-read",
        local_workflow.client,
        interactive=True,
    )
    assert recovered["operation"] == "RECONNECT"
    assert local_workflow.store.values

    credential_setup.local_remove(configured, "workflow-ga4-read", "PRIMARY")
    assert local_workflow.store.values == {}
    after_local_remove = workflow.reconnect_google_oauth(
        local_workflow.directory,
        "workflow-ga4-read",
        local_workflow.client,
        interactive=True,
    )
    assert after_local_remove["operation"] == "RECONNECT"
    assert local_workflow.store.values
    assert not (local_workflow.directory / "artifacts").exists()


def test_reconnect_rejects_different_subject_before_credential_write(
    local_workflow: _LocalWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow.enroll_google_oauth(
        local_workflow.directory,
        local_workflow.draft,
        local_workflow.client,
        interactive=True,
    )
    original = dict(local_workflow.store.values)
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: _grant(subject="another-subject"))

    with pytest.raises(AdhelmError) as failure:
        workflow.reconnect_google_oauth(
            local_workflow.directory,
            "workflow-ga4-read",
            local_workflow.client,
            interactive=True,
        )

    assert failure.value.code == "GOOGLE_OAUTH_PRINCIPAL_MISMATCH"
    assert local_workflow.store.values == original
    _assert_not_public(str(failure.value))


def test_slot_snapshot_has_no_clear_bundle_and_constructed_snapshot_cannot_write(
    local_workflow: _LocalWorkflow,
) -> None:
    workflow.enroll_google_oauth(
        local_workflow.directory,
        local_workflow.draft,
        local_workflow.client,
        interactive=True,
    )
    profile = load_configuration(local_workflow.directory).profiles[0]
    assert isinstance(profile, Profile)
    raw_bundle = _stored_value(local_workflow.store, profile.credential_ref)
    assert all(secret in raw_bundle for secret in (ACCESS, REFRESH, CLIENT_SECRET, SUBJECT))

    snapshot = oauth_credentials.prepare_google_oauth_install(
        load_configuration(local_workflow.directory), profile.profile_id, replace=True
    )
    rendered = repr(snapshot) + str(asdict(snapshot))
    for item in fields(snapshot):
        rendered += str(getattr(snapshot, item.name))
    with pytest.raises(TypeError):
        vars(snapshot)
    _assert_not_public(rendered)
    assert not hasattr(snapshot, "_expected_value")

    constructed = object.__new__(oauth_credentials.GoogleOAuthInstallSlot)
    before = dict(local_workflow.store.values)
    with pytest.raises(AdhelmError) as failure:
        oauth_credentials.install_google_oauth_grant(
            load_configuration(local_workflow.directory),
            profile.profile_id,
            _grant(),
            client_secret=CLIENT_SECRET,
            expected_slot=constructed,
            replace=True,
        )
    assert failure.value.code == "GOOGLE_OAUTH_INSTALL_SNAPSHOT_INVALID"
    assert local_workflow.store.values == before


def test_reconnect_install_requires_configuration_bound_slot(
    local_workflow: _LocalWorkflow,
) -> None:
    workflow.enroll_google_oauth(
        local_workflow.directory,
        local_workflow.draft,
        local_workflow.client,
        interactive=True,
    )
    profile = load_configuration(local_workflow.directory).profiles[0]
    assert isinstance(profile, Profile)
    unbound = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=True)
    before = dict(local_workflow.store.values)

    with pytest.raises(AdhelmError) as failure:
        oauth_credentials.install_google_oauth_grant(
            load_configuration(local_workflow.directory),
            profile.profile_id,
            _grant(),
            client_secret=CLIENT_SECRET,
            expected_slot=unbound,
            replace=True,
        )

    assert failure.value.code == "GOOGLE_OAUTH_INSTALL_SNAPSHOT_INVALID"
    assert local_workflow.store.values == before


def test_google_ads_receipt_is_bounded_truthful_and_secret_free(
    local_workflow: _LocalWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_workflow.draft.write_text(json.dumps(_draft_data("google_ads")))
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: _grant("google_ads"))

    result = CliRunner().invoke(app, _enroll_args(local_workflow))

    assert result.exit_code == 0, result.output
    assert len(result.output.encode("utf-8")) <= 1024
    data = json.loads(result.output)["data"]
    assert data["credential_slot"] == "PRIMARY"
    assert data["credential_state"] == "PRIMARY_OAUTH_READY_SECONDARY_UNVERIFIED"
    assert data["secondary_credential_status"] == "REQUIRED_NOT_EVALUATED"
    assert data["resource_access_verified"] is False
    assert data["execution_authority"] is False
    for private in (
        ACCESS,
        REFRESH,
        CLIENT_SECRET,
        SUBJECT,
        CLIENT,
        "workflow-google_ads-read",
        "workflow-google-ads-secondary",
        "workflow-google_ads-oauth-primary",
    ):
        assert private not in result.output
    assert not (local_workflow.directory / "artifacts").exists()


def test_failed_protected_write_has_sanitized_bounded_cli_outcome(
    local_workflow: _LocalWorkflow,
) -> None:
    local_workflow.store.drop_writes = True

    result = CliRunner().invoke(app, _enroll_args(local_workflow))

    assert result.exit_code == 6, result.output
    assert len(result.output.encode("utf-8")) <= 1024
    assert (
        json.loads(result.output)["diagnostics"][0]["code"]
        == "GOOGLE_OAUTH_ENROLLMENT_LOCAL_STATE_UNVERIFIED"
    )
    _assert_not_public(result.output)
    assert len(load_configuration(local_workflow.directory).profiles) == 1
    assert local_workflow.store.values == {}
    assert not (local_workflow.directory / "artifacts").exists()


def test_direct_invalid_draft_is_strict_before_workflow_side_effects(
    local_workflow: _LocalWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_workflow.draft.write_text(json.dumps({**_draft_data(), "schema_version": True}))
    monkeypatch.setattr(
        workflow,
        "_browser_grant",
        lambda *_: pytest.fail("browser handoff after direct invalid draft"),
    )

    with pytest.raises(AdhelmError) as failure:
        workflow.enroll_google_oauth(
            local_workflow.directory,
            local_workflow.draft,
            local_workflow.client,
            interactive=True,
        )

    assert failure.value.code == "GOOGLE_OAUTH_DRAFT_INVALID"
    assert local_workflow.store.reads == 0
    assert local_workflow.store.writes == 0
    assert load_configuration(local_workflow.directory).profiles == []

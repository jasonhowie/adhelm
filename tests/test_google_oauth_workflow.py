"""Synthetic end-to-end local enrollment: no browser, socket, or OS secret calls."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_google_oauth_setup import CLIENT, client_file, draft_data
from typer.testing import CliRunner

from adhelm import credential_setup, credentials
from adhelm import google_oauth_workflow as workflow
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.models import Configuration, OutputPreferences, Profile

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
SUBJECT = "synthetic-google-subject"
ACCESS = "fixture-access-private-value"
REFRESH = "fixture-refresh-private-value"
OAUTH_SERVICE = "adhelm.google-oauth-v1"


@dataclass
class GoogleWorkflowStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    drop_writes: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if not self.drop_writes:
            self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def grant(subject: str = SUBJECT) -> GoogleOAuthInitialGrant:
    identity = VerifiedGoogleOidcIdentity(
        canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
        raw_subject=subject,
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, subject),
        client_ref=CLIENT,
    )
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(CLIENT, SCOPE),
        access_token=ACCESS,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token=REFRESH,
        refresh_token_expires_at=None,
        requested_scopes=("openid", SCOPE),
        returned_scopes=("openid", SCOPE),
        identity=identity,
    )


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, GoogleWorkflowStore]:
    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(draft_data()))
    client = client_file(tmp_path)
    store = GoogleWorkflowStore()
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: grant())
    monkeypatch.setattr(workflow, "_require_interactive", lambda interactive: None)
    return directory, draft, client, store


def test_enroll_saves_only_derived_profile_and_resolves_protected_access_token(setup) -> None:
    directory, draft, client, store = setup
    result = workflow.enroll_google_oauth(directory, draft, client, interactive=True)
    configuration = load_configuration(directory)
    assert len(configuration.profiles) == 1
    profile = configuration.profiles[0]
    assert isinstance(profile, Profile)
    assert profile.credential_source == "OS_OAUTH_GOOGLE"
    assert profile.principal_ref == grant().identity.principal_ref
    assert credentials.credential(profile) == ACCESS
    public = json.dumps(result) + (directory / "config.json").read_text()
    for private in (SUBJECT, ACCESS, REFRESH, "fixture-private-client-value"):
        assert private not in public
    assert result["resource_access_verified"] is False
    assert result["execution_authority"] is False
    assert len(store.values) == 1


def test_default_cli_requires_handoff_before_reading_files_or_secret_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        credential_setup, "os_store_backend", lambda: pytest.fail("No secret access allowed")
    )
    monkeypatch.setattr(
        workflow, "GoogleOAuthLoopbackReceiver", lambda: pytest.fail("No callback bind allowed")
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
            str(tmp_path / "missing-draft"),
            "--client-config",
            str(tmp_path / "missing-client"),
        ],
    )
    assert result.exit_code == 4, result.output
    assert (
        json.loads(result.output)["diagnostics"][0]["code"] == "EXPLICIT_BROWSER_HANDOFF_REQUIRED"
    )
    assert not (tmp_path / "missing").exists()


def test_cli_enrollment_receipt_is_bounded_and_has_no_customer_aliases(setup) -> None:
    directory, draft, client, _ = setup
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "google-oauth",
            "enroll",
            "--input",
            str(draft),
            "--client-config",
            str(client),
            "--interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 1024
    data = json.loads(result.output)["data"]
    assert data["credential_state"] == "READY"
    for private in (SUBJECT, ACCESS, REFRESH, CLIENT, "google-read-oauth", "ga4-read"):
        assert private not in result.output


def test_configuration_changed_during_browser_is_preserved_before_install(
    setup, monkeypatch
) -> None:
    directory, draft, client, store = setup

    def interrupted(*_):
        save_configuration(
            directory, Configuration(output=OutputPreferences(artifact_mode="OFF")), replace=True
        )
        return grant()

    monkeypatch.setattr(workflow, "_browser_grant", interrupted)
    with pytest.raises(AdhelmError) as error:
        workflow.enroll_google_oauth(directory, draft, client, interactive=True)
    assert error.value.code == "CONFIGURATION_CHANGED"
    assert load_configuration(directory).output.artifact_mode == "OFF"
    assert not load_configuration(directory).profiles
    assert store.values == {}


def test_credential_added_during_browser_is_not_overwritten(setup, monkeypatch) -> None:
    directory, draft, client, store = setup

    def interrupted(*_):
        store.values[(OAUTH_SERVICE, "google-read-oauth")] = "independent-local-credential"
        return grant()

    monkeypatch.setattr(workflow, "_browser_grant", interrupted)
    with pytest.raises(AdhelmError) as error:
        workflow.enroll_google_oauth(directory, draft, client, interactive=True)
    assert error.value.code == "GOOGLE_OAUTH_ENROLLMENT_LOCAL_STATE_UNVERIFIED"
    assert store.values == {(OAUTH_SERVICE, "google-read-oauth"): "independent-local-credential"}


def test_reconnect_rejects_a_different_google_subject_without_replacing_store(
    setup, monkeypatch
) -> None:
    directory, draft, client, store = setup
    workflow.enroll_google_oauth(directory, draft, client, interactive=True)
    before = dict(store.values)
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: grant("another-subject"))
    with pytest.raises(AdhelmError) as error:
        workflow.reconnect_google_oauth(directory, "ga4-read", client, interactive=True)
    assert error.value.code == "GOOGLE_OAUTH_PRINCIPAL_MISMATCH"
    assert store.values == before


def test_reconnect_cannot_resurrect_a_locally_removed_bundle(setup, monkeypatch) -> None:
    directory, draft, client, store = setup
    workflow.enroll_google_oauth(directory, draft, client, interactive=True)

    def interrupted(*_):
        credential_setup.local_remove(load_configuration(directory), "ga4-read")
        return grant()

    monkeypatch.setattr(workflow, "_browser_grant", interrupted)
    with pytest.raises(AdhelmError):
        workflow.reconnect_google_oauth(directory, "ga4-read", client, interactive=True)
    assert store.values == {}


def test_dropped_install_write_leaves_configured_profile_with_explicit_uncertainty(setup) -> None:
    directory, draft, client, store = setup
    store.drop_writes = True
    with pytest.raises(AdhelmError) as error:
        workflow.enroll_google_oauth(directory, draft, client, interactive=True)
    assert error.value.code == "GOOGLE_OAUTH_ENROLLMENT_LOCAL_STATE_UNVERIFIED"
    assert error.value.exit_code == 6
    assert len(load_configuration(directory).profiles) == 1
    assert store.values == {}
    assert ACCESS not in str(error.value)


def test_an_existing_slot_stops_before_browser(setup, monkeypatch) -> None:
    directory, draft, client, store = setup
    store.values[(OAUTH_SERVICE, "google-read-oauth")] = "existing"
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: pytest.fail("Browser must not open"))
    with pytest.raises(AdhelmError):
        workflow.enroll_google_oauth(directory, draft, client, interactive=True)
    assert not load_configuration(directory).profiles


def test_discovery_and_draft_schema_require_no_handoff() -> None:
    runner = CliRunner()
    discovery = runner.invoke(app, ["discover"])
    assert discovery.exit_code == 0
    assert "auth google-oauth enroll" in json.loads(discovery.output)["data"]["commands"]
    schema = runner.invoke(app, ["schema", "google-oauth-profile-draft"])
    assert schema.exit_code == 0
    data = json.loads(schema.output)["data"]["schema"]
    assert data["additionalProperties"] is False
    assert "principal_ref" not in data["properties"]
    assert "client_secret" not in data["properties"]

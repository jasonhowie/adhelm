"""GTM Web through the shared Google OAuth lifecycle.

Every case is local.  The credential store is an in-memory fake, the GTM API is
an `httpx.MockTransport`, the browser grant is a seam, and no Google endpoint is
reached.  The cases follow one GTM read profile from draft to enrollment,
status, read, refresh, refusal and local removal, and pin the diagnostics a
marketer's agent receives when Google refuses the credential.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from test_google_oauth_setup import CLIENT, client_file
from test_google_oauth_status import (
    ACCESS_TOKEN,
    CLIENT_SECRET,
    NOW,
    REFRESH_TOKEN,
    _configured,
    _install,
    _MemoryStore,
    _profile,
)
from test_gtm_measurement_workflow import (
    _KEY,
    _account,
    _container,
    _header,
    _install_transport,
    _version,
)
from typer.testing import CliRunner, Result

import adhelm.gtm_measurement_workflow as workflow
from adhelm import credential_setup, credentials, measurement_cli
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm import google_oauth_workflow as enrollment
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthRefreshGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.google_oauth_setup import GoogleOAuthProfileDraft
from adhelm.google_oauth_workflow import GoogleOAuthProvider, google_oauth_revocation_guidance
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.providers.gtm_identity import GtmIdentityDiscovery
from adhelm.providers.gtm_read import GtmReadInventory, classify_gtm_transport_error

runner = CliRunner()

GTM_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
REFRESHED_TOKEN = "fixture-refreshed-access-token"
SUBJECT = "fixture-subject"


def _gtm_profile(**changes: object) -> Profile:
    base = _profile("gtm").model_dump(mode="python")
    base.update(changes)
    return Profile.model_validate(base)


def _draft(**changes: object) -> dict[str, object]:
    draft: dict[str, object] = {
        "profile_id": "gtm-read",
        "provider": "gtm",
        "account_ref": "12345",
        "client_ref": CLIENT,
        "scopes": [GTM_SCOPE],
        "credential_ref": "gtm-read-oauth",
    }
    draft.update(changes)
    return draft


def _gtm_handler(
    seen: list[httpx.Request], refuse: Callable[[httpx.Request], int | None] = lambda _: None
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the fixed default GTM read, optionally refusing selected requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = refuse(request)
        if status is not None:
            return httpx.Response(status, json={"error": {"message": "provider detail"}})
        path = request.url.path
        if path == "/tagmanager/v2/accounts/12345":
            return httpx.Response(
                200, json=_account() | {"path": "accounts/12345", "accountId": "12345"}
            )
        if path == "/tagmanager/v2/accounts/12345/containers/2":
            return httpx.Response(200, json=_container(account_id="12345"))
        parent = "accounts/12345/containers/2"
        if path.endswith("/version_headers:latest"):
            return httpx.Response(
                200, json=_header() | {"path": f"{parent}/versions/8", "accountId": "12345"}
            )
        if path.endswith("/versions:live"):
            return httpx.Response(
                200, json=_version("7") | {"path": f"{parent}/versions/7", "accountId": "12345"}
            )
        if path.endswith("/versions/8"):
            return httpx.Response(
                200, json=_version("8") | {"path": f"{parent}/versions/8", "accountId": "12345"}
            )
        if path.endswith("/workspaces"):
            return httpx.Response(200, json={"workspace": []})
        raise AssertionError(path)

    return handler


def _read(tmp_path: Path) -> tuple[GtmIdentityDiscovery, GtmReadInventory]:
    return workflow.read_gtm(tmp_path, "oauth-gtm", container_id="2", fingerprint_key=_KEY)


def _no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a refused credential must stop before any GTM request")

    monkeypatch.setattr(workflow.httpx, "Client", refuse)


def _enrolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **install: Any
) -> tuple[Profile, _MemoryStore]:
    profile = _profile("gtm")
    store = _install(monkeypatch, profile, **install)
    _configured(tmp_path, profile)
    return profile, store


def _cli(tmp_path: Path, *arguments: str) -> Result:
    return runner.invoke(app, ["--data-dir", str(tmp_path), *arguments])


# --- the draft and the enrolled profile -----------------------------------------


def test_a_gtm_draft_needs_no_currency_developer_token_or_manager_route() -> None:
    draft = GoogleOAuthProfileDraft.model_validate(_draft())
    assert draft.provider == "gtm"
    assert draft.purpose == "READ"
    assert draft.currency is None
    assert draft.secondary_credential_ref is None
    assert "principal_ref" not in draft.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"scopes": ["https://www.googleapis.com/auth/analytics.readonly"]},
        {"scopes": ["https://www.googleapis.com/auth/tagmanager.edit.containers"]},
        {"scopes": ["https://www.googleapis.com/auth/tagmanager.publish"]},
        {"scopes": [GTM_SCOPE, "https://www.googleapis.com/auth/tagmanager.edit.containers"]},
        {"secondary_credential_ref": "gtm-developer-token"},
        {"login_customer_ref": "1234567890"},
        {"account_ref": "gtm-account"},
        {"account_ref": "1" * 21},
        {"purpose": "PUBLISH"},
    ],
)
def test_a_gtm_draft_outside_the_readonly_target_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GoogleOAuthProfileDraft.model_validate(_draft(**changes))


# --- enrollment and reconnect ---------------------------------------------------


def _grant(subject: str = SUBJECT, access_token: str = ACCESS_TOKEN) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(CLIENT, GTM_SCOPE),
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token=REFRESH_TOKEN,
        refresh_token_expires_at=None,
        requested_scopes=("openid", GTM_SCOPE),
        returned_scopes=("openid", GTM_SCOPE),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=subject,
            principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, subject),
            client_ref=CLIENT,
        ),
    )


@pytest.fixture
def enrolment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, _MemoryStore]:
    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=4096)))
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(_draft()))
    client = client_file(tmp_path)
    store = _MemoryStore()
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(enrollment, "_browser_grant", lambda *_: _grant())
    monkeypatch.setattr(enrollment, "_require_interactive", lambda interactive: None)
    return directory, draft, client, store


def test_enrolling_gtm_stores_one_bound_bundle_and_no_secondary_slot(enrolment) -> None:
    directory, draft, client, store = enrolment
    result = enrollment.enroll_google_oauth(directory, draft, client, interactive=True)
    profile = load_configuration(directory).profiles[0]
    assert isinstance(profile, Profile)
    assert (profile.provider, profile.purpose, profile.credential_source) == (
        "gtm",
        "READ",
        "OS_OAUTH_GOOGLE",
    )
    assert profile.scopes == [GTM_SCOPE]
    assert result["credential_state"] == "READY"
    assert result["secondary_credential_status"] == "NOT_APPLICABLE"
    assert result["resource_access_verified"] is False
    assert result["execution_authority"] is False
    assert len(store.values) == 1
    assert credentials.credential(profile) == ACCESS_TOKEN
    public = json.dumps(result) + (directory / "config.json").read_text()
    for private in (SUBJECT, ACCESS_TOKEN, REFRESH_TOKEN, "fixture-private-client-value"):
        assert private not in public


def test_reconnecting_gtm_replaces_the_grant_only_for_the_same_google_user(
    enrolment, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, draft, client, store = enrolment
    enrollment.enroll_google_oauth(directory, draft, client, interactive=True)
    profile = load_configuration(directory).profiles[0]
    assert isinstance(profile, Profile)
    before = dict(store.values)

    monkeypatch.setattr(enrollment, "_browser_grant", lambda *_: _grant("another-subject"))
    with pytest.raises(AdhelmError) as refused:
        enrollment.reconnect_google_oauth(directory, "gtm-read", client, interactive=True)
    assert refused.value.code == "GOOGLE_OAUTH_PRINCIPAL_MISMATCH"
    assert store.values == before

    monkeypatch.setattr(
        enrollment, "_browser_grant", lambda *_: _grant(access_token="fixture-reconnected-token")
    )
    result = enrollment.reconnect_google_oauth(directory, "gtm-read", client, interactive=True)
    assert result["operation"] == "RECONNECT"
    assert store.values != before
    assert credentials.credential(profile) == "fixture-reconnected-token"


# --- status ---------------------------------------------------------------------


def test_gtm_status_needs_no_developer_token_and_describes_gtm_permissions_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, _ = _enrolled(tmp_path, monkeypatch)

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("status constructed an HTTP client")

    monkeypatch.setattr(httpx, "Client", refuse)
    result = _cli(tmp_path, "auth", "google-oauth", "status", "--profile", profile.profile_id)
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["provider"] == "gtm"
    assert data["local_state"] == "READY"
    assert data["granted_scopes"] == [GTM_SCOPE]
    assert data["developer_token_slot_state"] == "NOT_REQUIRED"
    assert data["developer_token_slot_next_action"] is None
    guidance = data["revocation_guidance"]
    assert guidance == list(google_oauth_revocation_guidance("gtm"))
    assert any("User management" in item and "not by this OAuth grant" in item for item in guidance)
    assert not any("developer token" in item.lower() for item in guidance)
    assert any("never revokes the grant at Google" in item for item in guidance)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, SUBJECT):
        assert secret not in result.stdout


def test_revocation_guidance_names_only_what_applies_to_the_provider() -> None:
    def mentions(provider: GoogleOAuthProvider, text: str) -> bool:
        return any(text in item for item in google_oauth_revocation_guidance(provider))

    assert mentions("google_ads", "developer token")
    assert not mentions("google_ads", "User management")
    assert mentions("gtm", "User management")
    providers: tuple[GoogleOAuthProvider, ...] = ("google_ads", "ga4", "ga4_admin", "gtm")
    for provider in providers[1:]:
        assert not mentions(provider, "developer token")
    for provider in providers:
        assert mentions(provider, "Local removal never revokes the grant at Google")


# --- the read uses the enrolled credential --------------------------------------


def test_a_gtm_read_sends_the_enrolled_bearer_on_every_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enrolled(tmp_path, monkeypatch)
    seen: list[httpx.Request] = []
    clients = _install_transport(monkeypatch, _gtm_handler(seen))
    discovery, inventory = _read(tmp_path)
    assert clients() == 1
    assert len(seen) == 8
    assert {request.headers["authorization"] for request in seen} == {f"Bearer {ACCESS_TOKEN}"}
    assert discovery.identity.account_ref == "gtm.account.12345"
    assert inventory.identity is discovery.identity
    assert ACCESS_TOKEN not in json.dumps(inventory.model_dump(mode="json"))


def test_a_near_expiry_grant_is_refreshed_once_before_the_first_gtm_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, _ = _enrolled(tmp_path, monkeypatch, expires_at=NOW + timedelta(seconds=30))
    refreshes: list[object] = []

    def refreshed(bundle: object) -> GoogleOAuthRefreshGrant:
        refreshes.append(bundle)
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(profile.client_ref, GTM_SCOPE),
            access_token=REFRESHED_TOKEN,
            expires_at=NOW + timedelta(hours=1),
            returned_scopes=(GTM_SCOPE,),
            identity_reverified=True,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", refreshed)
    seen: list[httpx.Request] = []
    _install_transport(monkeypatch, _gtm_handler(seen))
    _read(tmp_path)
    assert len(refreshes) == 1
    assert {request.headers["authorization"] for request in seen} == {f"Bearer {REFRESHED_TOKEN}"}
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.local_state == "READY"
    assert status.access_token_expires_at == NOW + timedelta(hours=1)


def test_a_refresh_google_rejects_blocks_the_read_and_every_later_one_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, _ = _enrolled(tmp_path, monkeypatch, expires_at=NOW + timedelta(seconds=30))
    dispatched = 0

    def rejected(bundle: object) -> GoogleOAuthRefreshGrant:
        nonlocal dispatched
        dispatched += 1
        raise AdhelmError("GOOGLE_OAUTH_REFRESH_REJECTED", "Google rejected the refresh token.")

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", rejected)
    _no_http(monkeypatch)
    for _ in range(2):
        with pytest.raises(AdhelmError) as raised:
            _read(tmp_path)
        assert raised.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
        assert raised.value.exit_code == 4
    # The unresolved refresh is never replayed automatically.
    assert dispatched == 1
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    state = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert state.local_state == "RECONNECT_REQUIRED"


def test_an_expired_refresh_token_stops_the_gtm_read_before_any_token_or_gtm_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enrolled(
        tmp_path,
        monkeypatch,
        expires_at=NOW + timedelta(seconds=30),
        refresh_expires_at=NOW + timedelta(days=1),
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: NOW + timedelta(days=2))
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda bundle: pytest.fail("an expired refresh token must not be dispatched"),
    )
    _no_http(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    assert raised.value.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    rendered = json.dumps({"message": raised.value.message, "next": raised.value.next_action})
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in rendered


def test_a_bundle_enrolled_for_another_provider_cannot_serve_a_gtm_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ga4 = _profile("ga4")
    store = _install(monkeypatch, ga4)
    gtm = _gtm_profile(credential_ref=ga4.credential_ref)
    _configured(tmp_path, gtm)
    _no_http(monkeypatch)
    assert (oauth_credentials._SERVICE, ga4.credential_ref) in store.values
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    assert raised.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"
    assert raised.value.exit_code == 4
    assert ACCESS_TOKEN not in raised.value.message


def test_a_gtm_profile_without_a_stored_grant_stops_before_any_gtm_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile("gtm")
    monkeypatch.setattr(credentials, "_backend", lambda: _MemoryStore())
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    _configured(tmp_path, profile)
    _no_http(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    assert raised.value.code == "AUTHENTICATION_REQUIRED"
    assert raised.value.exit_code == 4


def test_the_measurement_command_retains_nothing_when_the_grant_needs_reconnecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enrolled(
        tmp_path,
        monkeypatch,
        expires_at=NOW + timedelta(seconds=30),
        refresh_expires_at=NOW + timedelta(days=1),
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: NOW + timedelta(days=2))
    monkeypatch.setattr(measurement_cli, "export_key", lambda: _KEY)
    _no_http(monkeypatch)
    result = _cli(tmp_path, "measurement", "gtm", "oauth-gtm", "2")
    assert result.exit_code == 4, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    catalog = _cli(tmp_path, "measurement", "catalog")
    assert json.loads(catalog.output)["data"]["measurements"] == []


# --- Google refuses the credential ----------------------------------------------


@pytest.mark.parametrize(
    "refused_suffix",
    [
        "/accounts/12345",
        "/containers/2",
        "/version_headers:latest",
        "/versions:live",
        "/workspaces",
    ],
)
def test_a_401_at_any_gtm_read_is_an_authorization_rejection_with_a_reconnect_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused_suffix: str
) -> None:
    _enrolled(tmp_path, monkeypatch)
    seen: list[httpx.Request] = []
    _install_transport(
        monkeypatch,
        _gtm_handler(seen, lambda r: 401 if r.url.path.endswith(refused_suffix) else None),
    )
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    error = raised.value
    assert error.code == "GTM_AUTHORIZATION_REJECTED"
    assert error.exit_code == 4
    assert error.status_code == 401
    # Nothing is sent after a 401: the refused request is the last one seen.
    assert seen[-1].url.path.endswith(refused_suffix)
    assert "auth google-oauth status --profile" in error.next_action
    assert "auth google-oauth reconnect --profile" in error.next_action
    rendered = json.dumps({"message": error.message, "next": error.next_action})
    for private in (ACCESS_TOKEN, REFRESH_TOKEN, "provider detail"):
        assert private not in rendered


def test_a_403_is_reported_with_every_cause_google_documents_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enrolled(tmp_path, monkeypatch)
    seen: list[httpx.Request] = []
    _install_transport(
        monkeypatch, _gtm_handler(seen, lambda r: 403 if "/containers/2" in r.url.path else None)
    )
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    error = raised.value
    assert error.code == "GTM_ACCESS_NOT_PERMITTED"
    assert error.exit_code == 4
    assert error.status_code == 403
    # The body is never read, so the diagnostic must not pick one cause.
    for cause in ("at least Read access", "Tag Manager API", "quota", "do not retry in a loop"):
        assert cause in error.next_action
    # A quota 403 clears only with time, so the single repeat must wait first.
    assert "wait at least 100 seconds" in error.next_action
    assert "100-second sliding window" in error.next_action
    assert "midnight Pacific" in error.next_action
    assert "provider detail" not in error.message + error.next_action
    assert [r.url.path for r in seen] == [
        "/tagmanager/v2/accounts/12345",
        "/tagmanager/v2/accounts/12345/containers/2",
    ]


def test_a_403_with_a_retry_after_hint_tells_the_agent_to_wait_that_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enrolled(tmp_path, monkeypatch)
    seen: list[httpx.Request] = []
    base = _gtm_handler(seen)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/containers/2" in request.url.path:
            seen.append(request)
            return httpx.Response(
                403, headers={"Retry-After": "37"}, json={"error": {"message": "provider detail"}}
            )
        return base(request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    error = raised.value
    assert error.code == "GTM_ACCESS_NOT_PERMITTED"
    assert error.retry_after_seconds == 37
    assert "wait of 37 second(s)" in error.next_action
    assert "wait at least 100 seconds" not in error.next_action
    assert "provider detail" not in error.message + error.next_action


@pytest.mark.parametrize("status", [None, 400, 404, 429, 500, 503])
def test_the_classifier_leaves_every_other_refusal_unchanged(status: int | None) -> None:
    original = AdhelmError(
        "PROVIDER_REQUEST_FAILED", "Provider rejected the fixed operation.", status_code=status
    )
    assert classify_gtm_transport_error(original) is original


def test_the_classifier_keeps_the_observed_status_and_retry_hint() -> None:
    transport_error = AdhelmError(
        "PROVIDER_ACCESS_REQUIRED",
        "Provider denied this profile.",
        status_code=403,
        retry_after_seconds=7,
    )
    classified = classify_gtm_transport_error(transport_error)
    assert (classified.status_code, classified.retry_after_seconds) == (403, 7)


# --- local removal --------------------------------------------------------------


def test_removing_the_local_gtm_grant_ends_status_and_reads_without_claiming_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, store = _enrolled(tmp_path, monkeypatch)
    removed = _cli(tmp_path, "auth", "remove-local", "--profile", profile.profile_id)
    assert removed.exit_code == 0, removed.output
    slot = json.loads(removed.output)["data"]["slots"][0]
    assert slot["state"] == "LOCAL_REMOVED"
    assert slot["remote_revocation_verified"] is False
    assert store.values == {}

    status = _cli(tmp_path, "auth", "google-oauth", "status", "--profile", profile.profile_id)
    assert json.loads(status.stdout)["data"]["local_state"] == "MISSING"
    assert json.loads(status.stdout)["data"]["remote_revocation_verified"] is False

    _no_http(monkeypatch)
    with pytest.raises(AdhelmError) as raised:
        _read(tmp_path)
    assert raised.value.code == "AUTHENTICATION_REQUIRED"

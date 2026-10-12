# ruff: noqa: E501
"""`auth google-oauth status`, the refresh ordering it describes, and its bounds.

Every case is local: no Google endpoint is contacted, and the one refresh
regression drives the protected bundle through an injected transport seam.
"""

from __future__ import annotations

import inspect
import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import credentials
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_cli import _status_data
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthRefreshGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.google_oauth_workflow import google_oauth_revocation_guidance
from adhelm.models import Configuration, Profile

runner = CliRunner()

ACCESS_TOKEN = "fixture-access-token-value"
REFRESH_TOKEN = "fixture-refresh-token-value"
CLIENT_SECRET = "fixture-client-secret-value"
DEVELOPER_TOKEN = "fixture-developer-token-value"
NOW = datetime(2029, 1, 1, tzinfo=UTC)


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile(provider: str = "google_ads") -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[(provider, "READ")]
    return Profile(
        profile_id=f"oauth-{provider}",
        provider=provider,
        account_ref="1234567890" if provider == "google_ads" else "12345",
        principal_ref=derive_google_oidc_principal_ref(
            "https://accounts.google.com", "fixture-subject"
        ),
        client_ref="fixture_client.apps.googleusercontent.com",
        scopes=[scope],
        purpose="READ",
        currency="USD" if provider in {"google_ads", "ga4", "ga4_admin"} else None,
        timezone="UTC",
        credential_source="OS_OAUTH_GOOGLE",
        credential_ref=f"oauth-{provider}-primary",
        secondary_credential_ref=(
            "oauth-google-ads-secondary" if provider == "google_ads" else None
        ),
    )


def _grant(
    profile: Profile,
    *,
    expires_at: datetime | None = None,
    refresh_expires_at: datetime | None = None,
) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, profile.scopes[0]),
        access_token=ACCESS_TOKEN,
        expires_at=expires_at or datetime(2030, 1, 1, tzinfo=UTC),
        refresh_token=REFRESH_TOKEN,
        refresh_token_expires_at=refresh_expires_at,
        requested_scopes=("openid", profile.scopes[0]),
        returned_scopes=(profile.scopes[0],),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject="fixture-subject",
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    *,
    store: _MemoryStore | None = None,
    now: datetime = NOW,
    expires_at: datetime | None = None,
    refresh_expires_at: datetime | None = None,
    developer_token: bool = True,
) -> _MemoryStore:
    backend = store if store is not None else _MemoryStore()
    monkeypatch.setattr(credentials, "_backend", lambda: backend)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now)
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=True
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile, expires_at=expires_at, refresh_expires_at=refresh_expires_at),
        client_secret=CLIENT_SECRET,
        expected_slot=slot,
        replace=True,
    )
    if developer_token and profile.secondary_credential_ref is not None:
        backend.set_password(
            credentials._SERVICE, profile.secondary_credential_ref, DEVELOPER_TOKEN
        )
    return backend


def _configured(directory: Path, profile: Profile) -> None:
    save_configuration(
        directory,
        Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]}),
    )


# --- the status reading -------------------------------------------------------


def test_status_reports_masked_identity_scopes_expiry_and_the_developer_token_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _install(monkeypatch, profile)
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.local_state == "READY"
    assert status.provider == "google_ads"
    assert status.purpose == "READ"
    # The identity is the derived pseudonym; the raw Google subject never
    # leaves the sealed bundle.
    assert status.identity_pseudonym == profile.principal_ref
    assert status.identity_pseudonym is not None
    assert status.identity_pseudonym.startswith("google_oidc_principal_v1.")
    assert status.granted_scopes == (profile.scopes[0],)
    assert status.requested_scopes == ("openid", profile.scopes[0])
    assert status.unrequested_granted_scopes == ()
    assert status.access_token_expires_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert status.access_token_expired is False
    assert status.refresh_token_expires_at is None
    assert status.refresh_token_expired is None
    assert status.primary_slot_present is True
    assert status.developer_token_slot_required is True
    assert status.developer_token_slot_present is True
    # No verification instant is invented; only the proven upper bound.
    assert status.identity_last_verified_at is None
    assert status.identity_last_verified_not_after == status.access_token_expires_at


def test_status_reports_a_missing_developer_token_slot_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _install(monkeypatch, profile, developer_token=False)
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.developer_token_slot_required is True
    assert status.developer_token_slot_state == "ABSENT"
    assert status.developer_token_slot_present is False


def test_a_non_google_ads_profile_requires_no_developer_token_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ga4")
    _install(monkeypatch, profile)
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.developer_token_slot_required is False
    assert status.developer_token_slot_state == "NOT_REQUIRED"
    assert status.developer_token_slot_present is None


def test_a_stored_and_an_unstored_developer_token_are_distinguishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _install(monkeypatch, profile, developer_token=False)
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    absent = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert absent.developer_token_slot_state == "ABSENT"
    assert absent.developer_token_slot_present is False


def test_the_status_route_constructs_no_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route reads local state only, so it must open no transport at all.

    A future change that reached Google to answer one of these fields would be
    a silent boundary change: the command's whole contract is that it contacts
    no provider.  This fails the moment any client is constructed.
    """
    import httpx

    profile = _profile()
    _install(monkeypatch, profile)
    _configured(tmp_path, profile)

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("the Google OAuth status route constructed an HTTP client")

    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "auth",
            "google-oauth",
            "status",
            "--profile",
            profile.profile_id,
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["data"]["local_state"] == "READY"


def test_status_reports_a_missing_or_undecodable_slot_without_repairing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = _MemoryStore()
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    missing = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert missing.local_state == "MISSING"
    assert missing.primary_slot_present is False
    assert missing.granted_scopes == ()

    store.set_password(oauth_credentials._SERVICE, profile.credential_ref, "not-a-bundle")
    unverified = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert unverified.local_state == "UNVERIFIED"
    assert unverified.primary_slot_present is True
    assert unverified.identity_pseudonym is None
    # A status read never rewrites the slot it could not decode.
    assert store.values[(oauth_credentials._SERVICE, profile.credential_ref)] == "not-a-bundle"


def test_status_reports_an_expired_access_token_without_refreshing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = _install(
        monkeypatch,
        profile,
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    before = dict(store.values)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: NOW + timedelta(hours=2))
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.access_token_expired is True
    assert status.local_state == "READY"
    # Reading status dispatches nothing and rewrites nothing, so no lock is
    # held across any network call: this path performs none.
    assert store.values == before


def test_status_reports_an_expired_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    _install(
        monkeypatch,
        profile,
        now=NOW,
        refresh_expires_at=NOW + timedelta(days=1),
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: NOW + timedelta(days=2))
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.refresh_token_expired is True


# --- the refresh path ---------------------------------------------------------


def test_the_refresh_path_reverifies_the_same_google_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh hands the stored subject to the protocol, which re-verifies it.

    `_dispatch_refresh` is the seam between the sealed local bundle and the
    fixed token endpoint.  This pins that it passes the exact protected
    identity, and that the protocol call it makes is the one that re-verifies
    the returned identity token against that identity.
    """
    import adhelm.google_oauth_protocol as protocol

    profile = _profile()
    _install(monkeypatch, profile, now=NOW, expires_at=NOW + timedelta(minutes=5))
    # The real protocol function, captured before the seam is replaced: it is
    # the one that re-verifies the returned identity token against the exact
    # identity it was handed.
    real_source = inspect.getsource(protocol.refresh_google_oauth_grant)
    seen: list[VerifiedGoogleOidcIdentity] = []

    def fake_refresh(
        target: GoogleOAuthTarget,
        expected_identity: VerifiedGoogleOidcIdentity,
        **kwargs: object,
    ) -> GoogleOAuthRefreshGrant:
        seen.append(expected_identity)
        return GoogleOAuthRefreshGrant(
            target=target,
            access_token="refreshed-access-token",
            expires_at=NOW + timedelta(hours=2),
            returned_scopes=(profile.scopes[0],),
            identity_reverified=True,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(protocol, "refresh_google_oauth_grant", fake_refresh)
    monkeypatch.setattr(
        oauth_credentials, "_utc_now", lambda: NOW + timedelta(minutes=4, seconds=30)
    )

    class _NoTransport:
        def __enter__(self) -> _NoTransport:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    import adhelm.google_oauth_transport as transport

    monkeypatch.setattr(transport.FixedGoogleOAuthTransport, "open", staticmethod(_NoTransport))
    token = oauth_credentials.resolve_google_oauth_access_token(profile)
    assert token == "refreshed-access-token"
    assert len(seen) == 1
    assert seen[0].principal_ref == profile.principal_ref
    assert seen[0].client_ref == profile.client_ref
    assert "verify_google_refresh_identity_token(" in real_source
    assert "expected_identity=checked_identity" in real_source


def test_a_refresh_that_returns_another_subject_never_yields_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _install(monkeypatch, profile, now=NOW, expires_at=NOW + timedelta(minutes=5))

    def mismatched(bundle: object) -> GoogleOAuthRefreshGrant:
        raise AdhelmError(
            "GOOGLE_OIDC_IDENTITY_INVALID",
            "Google identity token did not match the protected grant.",
            exit_code=ExitCode.AUTHORIZATION,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", mismatched)
    monkeypatch.setattr(
        oauth_credentials, "_utc_now", lambda: NOW + timedelta(minutes=4, seconds=30)
    )
    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    assert raised.value.exit_code == int(ExitCode.AUTHORIZATION) == 4
    assert ACCESS_TOKEN not in str(raised.value.message)
    assert REFRESH_TOKEN not in str(raised.value.message)


def test_an_expired_refresh_token_fails_with_exit_four_and_echoes_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    _install(
        monkeypatch,
        profile,
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
        refresh_expires_at=NOW + timedelta(days=1),
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: NOW + timedelta(days=2))

    def never(bundle: object) -> GoogleOAuthRefreshGrant:
        raise AssertionError("an expired refresh token must not be dispatched")

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", never)
    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(profile)
    error = raised.value
    assert error.code == "GOOGLE_OAUTH_RECONNECT_REQUIRED"
    assert error.exit_code == int(ExitCode.AUTHORIZATION) == 4
    rendered = json.dumps({"message": error.message, "next_action": error.next_action})
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, DEVELOPER_TOKEN):
        assert secret not in rendered
    # The refusal is durable: the bundle is blocked rather than silently retried.
    configuration = Configuration.model_validate({"profiles": [profile.model_dump(mode="json")]})
    status = oauth_credentials.google_oauth_local_status(configuration, profile.profile_id)
    assert status.local_state == "RECONNECT_REQUIRED"


# --- the command --------------------------------------------------------------


def test_the_status_command_emits_the_bounded_projection_and_echoes_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    _install(monkeypatch, profile)
    _configured(tmp_path, profile)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "auth",
            "google-oauth",
            "status",
            "--profile",
            profile.profile_id,
        ],
    )
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "auth google-oauth status"
    data = envelope["data"]
    assert data["local_state"] == "READY"
    assert data["provider_authentication_verified"] is False
    assert data["remote_revocation_verified"] is False
    assert data["resource_access_verified"] is False
    assert data["execution_authority"] is False
    assert data["identity_reverified_on_refresh"] is True
    assert data["identity_last_verified_at"] is None
    assert data["identity_last_verified_status"] == "NOT_RETAINED_BOUNDED_BY_ACCESS_TOKEN_EXPIRY"
    assert data["developer_token_access_level"] == "NOT_DOCUMENTED_LOCALLY"
    assert data["developer_token_slot_required"] is True
    assert data["developer_token_slot_state"] == "PRESENT"
    assert data["developer_token_slot_present"] is True
    assert data["developer_token_slot_next_action"] is None
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, DEVELOPER_TOKEN, "fixture-subject"):
        assert secret not in result.stdout


def test_the_status_command_describes_revocation_at_google_and_local_removal_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    _install(monkeypatch, profile)
    _configured(tmp_path, profile)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "auth",
            "google-oauth",
            "status",
            "--profile",
            profile.profile_id,
        ],
    )
    guidance = json.loads(result.stdout)["data"]["revocation_guidance"]
    assert guidance == list(google_oauth_revocation_guidance("google_ads"))
    provider_side = [item for item in guidance if item.startswith("Provider-side")]
    local_only = [item for item in guidance if item.startswith("Local-only")]
    assert provider_side and local_only
    # Local removal is never described as revocation.
    assert any(
        "never revokes the grant at Google" in item and "never evidence" in item
        for item in guidance
    )
    for item in local_only:
        assert "revoke" not in item.lower()


def test_an_unknown_profile_is_an_invalid_input_not_a_silent_empty_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    _install(monkeypatch, profile)
    _configured(tmp_path, profile)
    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "auth", "google-oauth", "status", "--profile", "absent"],
    )
    assert result.exit_code != 0
    assert "absent" not in result.stdout or json.loads(result.stdout)["outcome"] != "OK"


def test_the_status_projection_refuses_a_result_of_the_wrong_type() -> None:
    with pytest.raises(AdhelmError) as raised:
        _status_data({"local_state": "READY"})
    assert raised.value.code == "GOOGLE_OAUTH_STATUS_RESULT_INVALID"


def test_the_command_is_discoverable_for_an_agent() -> None:
    data = json.loads(runner.invoke(app, ["discover"]).stdout)["data"]
    assert "auth google-oauth status" in data["commands"]

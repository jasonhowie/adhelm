"""Independent fake-only review of Google Ads PAUSE stable credential binding."""

from __future__ import annotations

import json
import secrets
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

from adhelm import config as config_module
from adhelm import credentials
from adhelm import google_ads_pause_planning_session as planning_session
from adhelm import google_ads_pause_workflow as workflow
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthRefreshGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.ledger import Ledger
from adhelm.models import GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE, Configuration, Profile

_NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
_CUSTOMER = "1234567890"
_LOGIN = "0987654321"
_CLIENT = "ultra-google-ads.apps.googleusercontent.com"
_SUBJECT = "ultra-google-ads-subject"
_ACCESS = "ultra-google-ads-access-token"
_REFRESHED_ACCESS = "ultra-google-ads-refreshed-access-token"
_REFRESH = "ultra-google-ads-refresh-token"
_CLIENT_SECRET = "ultra-google-ads-client-secret"
_DEVELOPER = "ultra-google-ads-developer-token"
_GENERIC_SERVICE = "ultra-google-ads-generic-service"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads.append((service_name, username))
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes.append((service_name, username, password))
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


@dataclass
class _LockTracker:
    active: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)

    @contextmanager
    def hold(self, reference: str) -> Iterator[None]:
        self.entries.append(reference)
        self.active.append(reference)
        try:
            yield
        finally:
            assert self.active.pop() == reference


@dataclass
class _Fixture:
    root: Path
    store: _MemoryStore
    profile: Profile
    current: list[datetime]
    locks: _LockTracker
    configuration_digest: str


def _profile(**updates: object) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "EDIT")]
    data: dict[str, object] = {
        "profile_id": "google-ads-pause-ultra-edit",
        "provider": "google_ads",
        "account_ref": _CUSTOMER,
        "principal_ref": derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        "client_ref": _CLIENT,
        "scopes": [scope],
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_OAUTH_GOOGLE",
        "credential_ref": "google-ads-pause-ultra-oauth",
        "secondary_credential_ref": "google-ads-pause-ultra-developer",
        "login_customer_ref": _LOGIN,
    }
    data.update(updates)
    return Profile.model_validate(data)


def _grant(profile: Profile, *, expires_at: datetime) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, profile.scopes[0]),
        access_token=_ACCESS,
        expires_at=expires_at,
        refresh_token=_REFRESH,
        refresh_token_expires_at=_NOW + timedelta(days=1),
        requested_scopes=("openid", profile.scopes[0]),
        returned_scopes=(profile.scopes[0],),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=_SUBJECT,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _configured(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime = _NOW + timedelta(minutes=10),
) -> _Fixture:
    store = _MemoryStore()
    locks = _LockTracker()
    current = [_NOW]
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(oauth_credentials, "credential_mutation_lock", locks.hold)
    monkeypatch.setattr(config_module, "credential_mutation_lock", locks.hold)
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials,
        "_generic_credential_store",
        lambda: (store, _GENERIC_SERVICE),
    )
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])
    monkeypatch.setattr(planning_session, "_now", lambda: current[0])
    monkeypatch.setattr(workflow, "_now", lambda: current[0])

    profile = _profile()
    configuration = Configuration(profiles=[profile])
    save_configuration(root, configuration)
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile, expires_at=expires_at),
        client_secret=_CLIENT_SECRET,
        expected_slot=slot,
    )
    assert profile.secondary_credential_ref is not None
    store.values[(_GENERIC_SERVICE, profile.secondary_credential_ref)] = _DEVELOPER
    store.reads.clear()
    store.writes.clear()
    locks.entries.clear()
    return _Fixture(
        root=root,
        store=store,
        profile=profile,
        current=current,
        locks=locks,
        configuration_digest=digest(load_configuration(root)),
    )


def _identity_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "customer": {
                    "id": _CUSTOMER,
                    "resourceName": f"customers/{_CUSTOMER}",
                    "currencyCode": "USD",
                    "timeZone": "UTC",
                    "status": "CLOSED",
                }
            }
        ],
        "totalResultsCount": "1",
    }


def _campaign_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {
                    "id": "42",
                    "resourceName": f"customers/{_CUSTOMER}/campaigns/42",
                    "status": "ENABLED",
                }
            }
        ],
        "totalResultsCount": "1",
    }


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    fixture: _Fixture,
    *,
    expected_bearer: str,
    after_identity: Callable[[], None] | None = None,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert fixture.locks.active == []
        assert request.method == "POST"
        assert request.url == httpx.URL(
            f"https://googleads.googleapis.com/v25/customers/{_CUSTOMER}/googleAds:search"
        )
        assert request.headers["authorization"] == f"Bearer {expected_bearer}"
        assert request.headers["developer-token"] == _DEVELOPER
        assert request.headers["login-customer-id"] == _LOGIN
        body = json.loads(request.content)
        assert set(body) == {"query", "searchSettings"}
        assert body["searchSettings"] == {"returnTotalResultsCount": True}
        requests.append(request)
        if "FROM customer" in body["query"]:
            if after_identity is not None:
                after_identity()
            return httpx.Response(200, json=_identity_payload())
        assert body["query"].endswith("campaign.id = 42")
        return httpx.Response(200, json=_campaign_payload())

    monkeypatch.setattr(
        planning_session,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    return requests


def _rendered_error(error: BaseException) -> str:
    return "".join(traceback.format_exception(error))


def test_initial_refresh_and_binding_key_creation_are_planning_only_then_pair_is_no_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _configured(tmp_path, monkeypatch, expires_at=_NOW + timedelta(seconds=30))
    refreshes: list[object] = []

    def refreshed(bundle: object) -> GoogleOAuthRefreshGrant:
        refreshes.append(bundle)
        return GoogleOAuthRefreshGrant(
            target=GoogleOAuthTarget(fixture.profile.client_ref, fixture.profile.scopes[0]),
            access_token=_REFRESHED_ACCESS,
            expires_at=_NOW + timedelta(minutes=10),
            returned_scopes=None,
            identity_reverified=False,
            rotated_refresh_token=None,
            rotated_refresh_token_expires_at=None,
        )

    monkeypatch.setattr(oauth_credentials, "_dispatch_refresh", refreshed)
    monkeypatch.setattr(
        secrets,
        "token_hex",
        lambda count: "b" * (count * 2),
    )
    requests = _install_http(monkeypatch, fixture, expected_bearer=_REFRESHED_ACCESS)

    with planning_session.configured_google_ads_pause_planning_session(
        fixture.root,
        fixture.profile.profile_id,
        fixture.configuration_digest,
    ) as session:
        binding = session.execution_binding
        observation = session.observe_campaign("42")

    assert len(refreshes) == 1
    assert len(requests) == 2
    assert observation.account_binding.account_status == "CLOSED"
    assert observation.observation_binding.campaign.status == "ENABLED"
    assert binding.credential_binding_ref.startswith("credential-binding:sha256:")
    assert (
        fixture.store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] == "b" * 64
    )
    assert fixture.profile.secondary_credential_ref is not None
    public = json.dumps(binding.model_dump(mode="json"), sort_keys=True)
    for private in (
        _REFRESHED_ACCESS,
        _DEVELOPER,
        _REFRESH,
        _CLIENT_SECRET,
        fixture.profile.credential_ref,
        fixture.profile.secondary_credential_ref,
        GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
        "b" * 64,
    ):
        assert private not in public


@pytest.mark.parametrize(
    "drift",
    ("bearer", "refresh", "developer", "binding-key", "manager"),
)
def test_drift_after_identity_prevents_the_campaign_request_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    fixture = _configured(tmp_path, monkeypatch)
    secrets_by_drift = {
        "bearer": "ultra-rotated-bearer-token",
        "refresh": "ultra-rotated-refresh-token",
        "developer": "ultra-rotated-developer-token",
        "binding-key": "c" * 64,
        "manager": "1111111111",
    }
    replacement = secrets_by_drift[drift]

    def mutate() -> None:
        if drift in {"bearer", "refresh"}:
            bundle = oauth_credentials._decode_bundle(
                fixture.store.values[(oauth_credentials._SERVICE, fixture.profile.credential_ref)]
            )
            if drift == "bearer":
                rotated_bundle = replace(bundle, access_token=replacement)
            else:
                rotated_bundle = replace(bundle, refresh_token=replacement)
            oauth_credentials._write_bundle(
                fixture.store,
                fixture.profile.credential_ref,
                rotated_bundle,
            )
        elif drift == "developer":
            assert fixture.profile.secondary_credential_ref is not None
            fixture.store.values[(_GENERIC_SERVICE, fixture.profile.secondary_credential_ref)] = (
                replacement
            )
        elif drift == "binding-key":
            fixture.store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = (
                replacement
            )
        else:
            changed_profile = fixture.profile.model_copy(update={"login_customer_ref": replacement})
            save_configuration(
                fixture.root, Configuration(profiles=[changed_profile]), replace=True
            )

    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("M56 no-refresh continuity must not dispatch OAuth"),
    )
    requests = _install_http(
        monkeypatch,
        fixture,
        expected_bearer=_ACCESS,
        after_identity=mutate,
    )

    with planning_session.configured_google_ads_pause_planning_session(
        fixture.root,
        fixture.profile.profile_id,
        fixture.configuration_digest,
    ) as session:
        with pytest.raises(AdhelmError) as raised:
            session.observe_campaign("42")

    expected = (
        "CONFIGURATION_CHANGED" if drift == "manager" else "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED"
    )
    assert raised.value.code == expected
    assert len(requests) == 1
    assert replacement not in str(raised.value)
    assert replacement not in _rendered_error(raised.value)


def test_retention_callback_rereads_held_slots_without_reacquiring_and_blocks_provider_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _configured(tmp_path, monkeypatch)
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("retention must never refresh OAuth"),
    )
    requests = _install_http(monkeypatch, fixture, expected_bearer=_ACCESS)
    thread_errors: list[AdhelmError] = []

    with planning_session.configured_google_ads_pause_planning_session(
        fixture.root,
        fixture.profile.profile_id,
        fixture.configuration_digest,
    ) as session:
        with session.retention_recheck() as recheck:
            reads_before = len(fixture.store.reads)
            lock_entries_before = len(fixture.locks.entries)
            monkeypatch.setattr(
                oauth_credentials,
                "_credential_backend",
                lambda: pytest.fail("retention callback must reuse its captured OAuth handle"),
            )
            monkeypatch.setattr(
                oauth_credentials,
                "_generic_credential_store",
                lambda: pytest.fail("retention callback must reuse its captured generic handle"),
            )
            monkeypatch.setattr(
                oauth_credentials,
                "credential_mutation_lock",
                lambda _reference: pytest.fail("retention callback must not reacquire slot locks"),
            )
            recheck()
            assert len(fixture.store.reads) == reads_before + 3
            assert len(fixture.locks.entries) == lock_entries_before
            with pytest.raises(AdhelmError) as busy:
                session.observe_campaign("42")
            assert busy.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_BUSY"

            def foreign_callback() -> None:
                try:
                    recheck()
                except AdhelmError as error:
                    thread_errors.append(error)

            thread = threading.Thread(target=foreign_callback)
            thread.start()
            thread.join()
            assert not thread.is_alive()

        with pytest.raises(AdhelmError) as escaped:
            recheck()

    assert requests == []
    assert [error.code for error in thread_errors] == [
        "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED"
    ]
    assert escaped.value.code == "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED"


def test_forward_second_no_refresh_clock_sample_at_expiry_is_credential_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expires_at = _NOW + timedelta(minutes=10)
    fixture = _configured(tmp_path, monkeypatch, expires_at=expires_at)
    expected = oauth_credentials._prepare_google_ads_pause_protected_snapshot(
        fixture.profile, fixture.configuration_digest
    )
    writes_before = len(fixture.store.writes)
    samples = [expires_at - timedelta(microseconds=1), expires_at]

    def sampled_now() -> datetime:
        assert samples
        return samples.pop(0)

    monkeypatch.setattr(oauth_credentials, "_utc_now", sampled_now)
    with pytest.raises(AdhelmError) as expired:
        oauth_credentials._current_google_ads_pause_protected_snapshot(
            fixture.profile,
            fixture.configuration_digest,
            expected,
        )

    assert expired.value.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    assert samples == []
    assert len(fixture.store.writes) == writes_before


@pytest.mark.parametrize("drift", ("bundle", "developer", "binding-key", "configuration"))
def test_retention_callback_detects_drift_that_arrives_after_context_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    fixture = _configured(tmp_path, monkeypatch)
    monkeypatch.setattr(
        oauth_credentials,
        "_dispatch_refresh",
        lambda _bundle: pytest.fail("retention must never refresh OAuth"),
    )
    requests = _install_http(monkeypatch, fixture, expected_bearer=_ACCESS)

    with planning_session.configured_google_ads_pause_planning_session(
        fixture.root,
        fixture.profile.profile_id,
        fixture.configuration_digest,
    ) as session:
        with session.retention_recheck() as recheck:
            # Establish the required immediate callback before introducing an
            # uncoordinated local change during the same held-lock window.
            recheck()
            if drift == "bundle":
                bundle = oauth_credentials._decode_bundle(
                    fixture.store.values[
                        (oauth_credentials._SERVICE, fixture.profile.credential_ref)
                    ]
                )
                oauth_credentials._write_bundle(
                    fixture.store,
                    fixture.profile.credential_ref,
                    replace(bundle, access_token="ultra-retention-rotated-bearer"),
                )
            elif drift == "developer":
                assert fixture.profile.secondary_credential_ref is not None
                fixture.store.values[
                    (_GENERIC_SERVICE, fixture.profile.secondary_credential_ref)
                ] = "ultra-retention-rotated-developer"
            elif drift == "binding-key":
                fixture.store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = (
                    "d" * 64
                )
            else:
                changed = fixture.profile.model_copy(update={"timezone": "America/Chicago"})
                save_configuration(fixture.root, Configuration(profiles=[changed]), replace=True)

            with pytest.raises(AdhelmError) as changed_error:
                recheck()

    expected = (
        "CONFIGURATION_CHANGED"
        if drift == "configuration"
        else "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED"
    )
    assert changed_error.value.code == expected
    assert requests == []
    assert not Ledger(fixture.root).records("changes")


@pytest.mark.parametrize("campaign_id", ("01", "9223372036854775808", 42, True))
def test_invalid_campaign_id_stops_before_a_header_or_resource_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campaign_id: object
) -> None:
    fixture = _configured(tmp_path, monkeypatch)
    requests = _install_http(monkeypatch, fixture, expected_bearer=_ACCESS)

    with planning_session.configured_google_ads_pause_planning_session(
        fixture.root,
        fixture.profile.profile_id,
        fixture.configuration_digest,
    ) as session:
        reads_before = len(fixture.store.reads)
        with pytest.raises(AdhelmError) as invalid:
            session.observe_campaign(cast(str, campaign_id))

    assert invalid.value.code == "INVALID_INPUT"
    assert len(fixture.store.reads) == reads_before
    assert requests == []


def test_copied_float_schema_alias_in_session_context_fails_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _configured(tmp_path, monkeypatch)
    requests = _install_http(monkeypatch, fixture, expected_bearer=_ACCESS)

    with planning_session.configured_google_ads_pause_planning_session(
        fixture.root,
        fixture.profile.profile_id,
        fixture.configuration_digest,
    ) as session:
        forged = session._context._execution_binding.model_copy(update={"schema_version": 1.0})
        object.__setattr__(session._context, "_execution_binding", forged)
        with pytest.raises(AdhelmError) as invalid:
            _ = session.execution_binding

    assert invalid.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"
    assert requests == []


def test_actual_workflow_keeps_stable_binding_private_and_rejects_expiry_without_clamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _configured(tmp_path, monkeypatch, expires_at=_NOW + timedelta(minutes=4))
    requests = _install_http(monkeypatch, fixture, expected_bearer=_ACCESS)
    request = workflow.GoogleAdsCampaignPausePlanRequest(
        profile_id=fixture.profile.profile_id,
        campaign_id="42",
        expires_in_seconds=300,
    )

    with pytest.raises(AdhelmError) as expired:
        workflow.plan_configured_google_ads_campaign_pause(fixture.root, request)

    assert expired.value.code == "PLAN_EXPIRED"
    assert len(requests) == 2
    assert not Ledger(fixture.root).records("changes")


def test_actual_workflow_retains_only_the_stable_nonsecret_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _configured(tmp_path, monkeypatch)
    requests = _install_http(monkeypatch, fixture, expected_bearer=_ACCESS)
    request = workflow.GoogleAdsCampaignPausePlanRequest(
        profile_id=fixture.profile.profile_id,
        campaign_id="42",
        expires_in_seconds=300,
    )

    retained = workflow.plan_configured_google_ads_campaign_pause(fixture.root, request)

    assert len(requests) == 2
    action = retained.action
    assert action.execution_authority is False
    assert action.execution_binding.credential_binding_ref.startswith("credential-binding:sha256:")
    assert fixture.profile.secondary_credential_ref is not None
    rendered = json.dumps(action.to_data(), sort_keys=True)
    for private in (
        _ACCESS,
        _DEVELOPER,
        _REFRESH,
        _CLIENT_SECRET,
        fixture.profile.credential_ref,
        fixture.profile.secondary_credential_ref,
        GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
    ):
        assert private not in rendered
    records = [
        row
        for row in Ledger(fixture.root).records("changes")
        if row["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    ]
    assert len(records) == 1
    assert records[0]["record_digest"] == retained.plan_record_digest

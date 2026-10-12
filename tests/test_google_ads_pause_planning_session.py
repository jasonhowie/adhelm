"""Fake-only tests for the sealed Google Ads PAUSE planning session."""

from __future__ import annotations

import json
import pickle
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from adhelm import config as config_module
from adhelm import credentials
from adhelm import google_ads_pause_planning_session as planning_session
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import canonical_bytes, digest
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
from adhelm.models import (
    GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
    Configuration,
    OutputPreferences,
    Profile,
)

_NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
_CUSTOMER = "1234567890"
_LOGIN = "0987654321"
_CAMPAIGN = "42"
_CLIENT = "m56-google-ads.apps.googleusercontent.com"
_SUBJECT = "m56-google-ads-subject"
_ACCESS = "m56-google-ads-access-token"
_GENERIC_SERVICE = "m56-generic-store"


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


def _profile(**updates: object) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "EDIT")]
    values: dict[str, object] = {
        "profile_id": "google-ads-pause-edit",
        "provider": "google_ads",
        "account_ref": _CUSTOMER,
        "principal_ref": derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        "client_ref": _CLIENT,
        "scopes": [scope],
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_OAUTH_GOOGLE",
        "credential_ref": "google-ads-pause-oauth",
        "login_customer_ref": _LOGIN,
    }
    values.update(updates)
    return Profile.model_validate(values)


def _grant(profile: Profile, *, expires_at: datetime | None = None) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, profile.scopes[0]),
        access_token=_ACCESS,
        expires_at=expires_at or _NOW + timedelta(minutes=10),
        refresh_token="m56-google-ads-refresh-token",
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


@contextmanager
def _tracking_lock(reference: str, active: set[str]) -> Iterator[None]:
    active.add(reference)
    try:
        yield
    finally:
        active.remove(reference)


def _install_store(
    monkeypatch: pytest.MonkeyPatch, store: _MemoryStore, active_locks: set[str]
) -> None:
    def lock(reference: str) -> Iterator[None]:
        return _tracking_lock(reference, active_locks)

    monkeypatch.setattr(oauth_credentials, "credential_mutation_lock", lock)
    monkeypatch.setattr(config_module, "credential_mutation_lock", lock)
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "_generic_credential_store", lambda: (store, _GENERIC_SERVICE)
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)
    monkeypatch.setattr(planning_session, "_now", lambda: _NOW)


def _configured(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime | None = None,
) -> tuple[_MemoryStore, Profile, str, set[str]]:
    store = _MemoryStore()
    active_locks: set[str] = set()
    _install_store(monkeypatch, store, active_locks)
    profile = _profile()
    configuration = Configuration(profiles=[profile])
    save_configuration(root, configuration)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile, expires_at=expires_at),
        client_secret="m56-client-secret",
        expected_slot=slot,
    )
    store.reads.clear()
    store.writes.clear()
    return store, profile, digest(load_configuration(root)), active_locks


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
        "fieldMask": (
            "customer.id,customer.resource_name,customer.currency_code,"
            "customer.time_zone,customer.status"
        ),
        "queryResourceConsumption": "1",
    }


def _campaign_payload(*, status: str = "ENABLED") -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {
                    "id": _CAMPAIGN,
                    "resourceName": f"customers/{_CUSTOMER}/campaigns/{_CAMPAIGN}",
                    "status": status,
                }
            }
        ],
        "totalResultsCount": "1",
        "fieldMask": "campaign.id,campaign.resource_name,campaign.status",
        "queryResourceConsumption": "1",
    }


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    requests: list[httpx.Request],
    active_locks: set[str],
    *,
    campaign_status: str = "ENABLED",
    after_identity: Callable[[], None] | None = None,
) -> None:
    real_client = httpx.Client

    def client() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            # The current no-refresh snapshot and both protected locks,
            # including the outer configuration lock, must have been released
            # before the fixed resource POST reaches the transport.
            assert active_locks == set()
            requests.append(request)
            assert request.method == "POST"
            assert request.url.path == f"/v25/customers/{_CUSTOMER}/googleAds:search"
            assert request.headers["authorization"] == f"Bearer {_ACCESS}"
            assert "developer-token" not in request.headers
            assert request.headers["login-customer-id"] == _LOGIN
            body = json.loads(request.content)
            assert body["searchSettings"] == {"returnTotalResultsCount": True}
            assert set(body) == {"query", "searchSettings"}
            if "FROM customer" in body["query"]:
                if after_identity is not None:
                    after_identity()
                return httpx.Response(200, json=_identity_payload())
            assert body["query"].endswith(f"campaign.id = {_CAMPAIGN}")
            return httpx.Response(200, json=_campaign_payload(status=campaign_status))

        return real_client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(planning_session, "_google_ads_http_client", client)


def test_fresh_pair_exposes_only_detached_m55_facts_after_unlocked_fixed_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    monkeypatch.setattr(
        credentials,
        "credential",
        lambda *_args, **_kwargs: pytest.fail("generic credential() must not be used"),
    )

    manager = planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    )
    with manager as session:
        binding = session.execution_binding
        observed = session.observe_campaign(_CAMPAIGN)
        assert binding.profile_ref == profile.profile_id
        assert binding.client_application_ref == _CLIENT
        assert binding.required_scope == "https://www.googleapis.com/auth/adwords"
        assert binding.configuration_digest == expected
        assert binding.profile_configuration_digest == digest(profile)
        assert binding.login_customer_client_ref == _LOGIN
        assert binding.resolved_at == _NOW
        assert binding.expires_at == _NOW + timedelta(minutes=10)
        assert observed.account_binding.account_client_ref == _CUSTOMER
        assert observed.account_binding.account_status == "CLOSED"
        assert observed.observation_binding.campaign.campaign_client_ref == _CAMPAIGN
        assert observed.observation_binding.campaign.status == "ENABLED"
        assert observed.observation_binding.read_started_at == _NOW
        assert observed.observation_binding.read_finished_at == _NOW
        assert not hasattr(session, "identity")
        assert not hasattr(session, "read_campaign_state")
        for value in (_ACCESS, "m56-google-ads-refresh-token", "m56-client-secret"):
            assert value not in repr(session)
            assert value not in repr(observed)
        with pytest.raises(TypeError):
            pickle.dumps(session)
        with pytest.raises(TypeError):
            pickle.dumps(observed)

    assert len(requests) == 2
    assert active_locks == set()
    assert _ACCESS not in repr(getattr(manager, "gen", None))


@pytest.mark.parametrize(
    ("expected_digest", "profile_update", "code"),
    [
        ("0" * 64, None, "CONFIGURATION_CHANGED"),
        (None, {"purpose": "READ"}, "GOOGLE_ADS_EDIT_PROFILE_REQUIRED"),
    ],
)
def test_factory_refuses_stale_or_wrong_profile_before_slots_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_digest: str | None,
    profile_update: dict[str, object] | None,
    code: str,
) -> None:
    store, profile, _expected, active_locks = _configured(tmp_path, monkeypatch)
    if profile_update is not None:
        save_configuration(
            tmp_path,
            Configuration(profiles=[profile.model_copy(update=profile_update)]),
            replace=True,
        )
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    reads_before = len(store.reads)

    with pytest.raises(AdhelmError) as raised:
        with planning_session.configured_google_ads_pause_planning_session(
            tmp_path,
            profile.profile_id,
            expected_digest,
        ):
            pytest.fail("invalid composition must not yield")

    assert raised.value.code == code
    assert len(store.reads) == reads_before
    assert requests == []


def test_malformed_expected_digest_and_campaign_refuse_before_store_or_pair_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    reads_before = len(store.reads)
    with pytest.raises(AdhelmError) as malformed:
        with planning_session.configured_google_ads_pause_planning_session(
            tmp_path, profile.profile_id, "not-a-digest"
        ):
            pytest.fail("malformed digest must fail")
    assert malformed.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"
    assert len(store.reads) == reads_before

    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        with pytest.raises(AdhelmError) as invalid_campaign:
            session.observe_campaign("042")
    assert invalid_campaign.value.code == "INVALID_INPUT"
    assert requests == []


@pytest.mark.parametrize("drift", ["oauth", "binding-key", "configuration"])
def test_pre_identity_drift_fails_before_any_fixed_resource_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)

    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        if drift == "oauth":
            bundle = oauth_credentials._decode_bundle(
                store.values[(oauth_credentials._SERVICE, profile.credential_ref)]
            )
            oauth_credentials._write_bundle(
                store, profile.credential_ref, replace(bundle, access_token="m56-rotated-access")
            )
        elif drift == "binding-key":
            store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = "c" * 64
        else:
            changed = load_configuration(tmp_path).model_copy(
                update={"output": OutputPreferences(artifact_mode="OFF")}
            )
            save_configuration(tmp_path, changed, replace=True)
        with pytest.raises(AdhelmError) as raised:
            session.observe_campaign(_CAMPAIGN)

    assert raised.value.code in {"GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED", "CONFIGURATION_CHANGED"}
    assert requests == []


def test_drift_after_identity_is_mapped_and_blocks_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []

    def rotate_binding_key() -> None:
        store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = "c" * 64

    _install_http(monkeypatch, requests, active_locks, after_identity=rotate_binding_key)
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        with pytest.raises(AdhelmError) as raised:
            session.observe_campaign(_CAMPAIGN)

    assert raised.value.code == "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED"
    assert len(requests) == 1


def test_non_enabled_campaign_is_not_a_pause_planning_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks, campaign_status="PAUSED")
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        with pytest.raises(AdhelmError) as raised:
            session.observe_campaign(_CAMPAIGN)
    assert raised.value.code == "GOOGLE_ADS_PAUSE_OBSERVATION_INVALID"
    assert len(requests) == 2


def test_retention_holds_local_facts_without_callback_io_and_blocks_reads_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        with session.retention_recheck() as recheck:
            reads_before = len(store.reads)
            assert GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE in active_locks
            with pytest.raises(AdhelmError) as owner_binding:
                _ = session.execution_binding
            assert owner_binding.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_BUSY"
            with pytest.raises(AdhelmError) as owner_read:
                session.observe_campaign(_CAMPAIGN)
            assert owner_read.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_BUSY"

            done = Event()
            result: list[AdhelmError] = []

            def competing_read() -> None:
                try:
                    session.observe_campaign(_CAMPAIGN)
                except AdhelmError as error:
                    result.append(error)
                finally:
                    done.set()

            thread = Thread(target=competing_read)
            thread.start()
            assert done.wait(timeout=1)
            thread.join(timeout=1)
            assert result and result[0].code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_BUSY"
            recheck()
            # The held callback re-reads both protected slots through
            # already-acquired handles; it must not acquire new locks or send
            # a provider request.
            assert len(store.reads) > reads_before
            assert requests == []
        with pytest.raises(AdhelmError) as escaped:
            recheck()
        assert escaped.value.code == "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED"


def test_retention_requires_callback_and_constructed_values_have_no_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        with pytest.raises(AdhelmError) as missing:
            with session.retention_recheck():
                pass
        assert missing.value.code == "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED"
    forged_session = object.__new__(planning_session.GoogleAdsCampaignPausePlanningSession)
    with pytest.raises(AdhelmError) as invalid_session:
        _ = forged_session.execution_binding
    assert invalid_session.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"
    forged_observation = object.__new__(planning_session.GoogleAdsCampaignPausePlanningObservation)
    with pytest.raises(AdhelmError) as invalid_observation:
        _ = forged_observation.account_binding
    assert invalid_observation.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"


def test_retention_recheck_detects_manual_configuration_edit_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    config_path = tmp_path / "config.json"
    original = config_path.read_bytes()
    changed = load_configuration(tmp_path).model_copy(
        update={"output": OutputPreferences(artifact_mode="OFF")}
    )

    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        with session.retention_recheck() as recheck:
            # This bypasses AdHelm's cooperative transaction deliberately, as a
            # manual editor could.  The callback must re-read the held config
            # transaction and refuse before an append callback can proceed.
            config_path.write_bytes(canonical_bytes(changed.model_dump(mode="json")))
            with pytest.raises(AdhelmError) as changed_error:
                recheck()
            assert changed_error.value.code == "CONFIGURATION_CHANGED"
            config_path.write_bytes(original)
            recheck()

    assert requests == []


def test_detached_closed_models_reject_raw_float_aliases_before_canonicalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected
    ) as session:
        binding = session.execution_binding
        observed = session.observe_campaign(_CAMPAIGN)

    with pytest.raises(AdhelmError) as bad_execution:
        planning_session._checked_execution_binding(
            binding.model_copy(update={"schema_version": 1.0})
        )
    assert bad_execution.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"
    with pytest.raises(AdhelmError) as bad_account:
        planning_session._checked_account_binding(
            observed.account_binding.model_copy(update={"schema_version": 1.0})
        )
    assert bad_account.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"
    with pytest.raises(AdhelmError) as bad_observation:
        planning_session._checked_observation_binding(
            observed.observation_binding.model_copy(update={"schema_version": 1.0})
        )
    assert bad_observation.value.code == "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID"


def test_clock_rollback_between_fixed_requests_fails_before_second_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected_configuration_digest=expected
    ) as session:
        early = _NOW + timedelta(seconds=60)
        later = _NOW + timedelta(seconds=120)
        samples = iter([early, later, later, early, early, early])
        protected_samples = iter([later, later, early, early])
        monkeypatch.setattr(planning_session, "_now", lambda: next(samples))
        monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: next(protected_samples))
        with pytest.raises(AdhelmError) as raised:
            session.observe_campaign(_CAMPAIGN)
    assert raised.value.code == "CLOCK_INVALID"
    assert len(requests) == 1


def test_retention_checks_held_credential_timestamp_before_final_session_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected, active_locks = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests, active_locks)
    with planning_session.configured_google_ads_pause_planning_session(
        tmp_path, profile.profile_id, expected_configuration_digest=expected
    ) as session:
        with pytest.raises(AdhelmError) as raised:
            with session.retention_recheck() as recheck:
                monkeypatch.setattr(
                    oauth_credentials, "_utc_now", lambda: _NOW + timedelta(seconds=120)
                )
                monkeypatch.setattr(planning_session, "_now", lambda: _NOW + timedelta(seconds=60))
                recheck()
    assert raised.value.code == "CLOCK_INVALID"
    assert requests == []

"""Fake-only lifecycle tests for the sealed Google Ads EDIT observation session."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from adhelm import config as config_module
from adhelm import credentials
from adhelm import google_ads_edit_session as edit_session
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import digest
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

_NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
_CUSTOMER = "1234567890"
_LOGIN = "0987654321"
_CLIENT = "fixture-google-ads.apps.googleusercontent.com"
_SUBJECT = "fixture-google-subject"
_ACCESS = "fixture-google-ads-edit-access-token"
_DEVELOPER = "fixture-google-ads-developer-token"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads.append((service_name, username))
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile(**updates: object) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "EDIT")]
    data: dict[str, object] = {
        "profile_id": "google-ads-edit",
        "provider": "google_ads",
        "account_ref": _CUSTOMER,
        "principal_ref": derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        "client_ref": _CLIENT,
        "scopes": [scope],
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_OAUTH_GOOGLE",
        "credential_ref": "google-ads-edit-oauth",
        "secondary_credential_ref": "google-ads-edit-developer",
        "login_customer_ref": _LOGIN,
    }
    data.update(updates)
    return Profile.model_validate(data)


def _grant(profile: Profile, *, expires_at: datetime | None = None) -> GoogleOAuthInitialGrant:
    scope = profile.scopes[0]
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, scope),
        access_token=_ACCESS,
        expires_at=expires_at or _NOW + timedelta(minutes=5),
        refresh_token="fixture-google-ads-edit-refresh-token",
        refresh_token_expires_at=None,
        requested_scopes=("openid", scope),
        returned_scopes=(scope,),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=_SUBJECT,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _install_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _NOW)
    monkeypatch.setattr(edit_session, "_now", lambda: _NOW)


def _configured(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: Profile | None = None,
    expires_at: datetime | None = None,
) -> tuple[_MemoryStore, Profile, str]:
    store = _MemoryStore()
    _install_store(monkeypatch, store)
    selected = profile or _profile()
    configuration = Configuration(profiles=[selected])
    save_configuration(root, configuration)
    slot = oauth_credentials.prepare_google_oauth_slot(selected.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        selected.profile_id,
        _grant(selected, expires_at=expires_at),
        client_secret=None,
        expected_slot=slot,
    )
    assert selected.secondary_credential_ref is not None
    store.values[(credentials._SERVICE, selected.secondary_credential_ref)] = _DEVELOPER
    return store, selected, digest(load_configuration(root))


def _identity_payload(*, currency: str = "USD", timezone: str = "UTC") -> dict[str, object]:
    return {
        "results": [
            {
                "customer": {
                    "id": _CUSTOMER,
                    "resourceName": f"customers/{_CUSTOMER}",
                    "currencyCode": currency,
                    "timeZone": timezone,
                    "status": "CLOSED",
                }
            }
        ],
        "totalResultsCount": "1",
        "fieldMask": "customer.id,customer.currency_code,customer.time_zone,customer.status",
        "queryResourceConsumption": "1",
    }


def _campaign_payload(*, campaign: str = "42", status: str = "PAUSED") -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {
                    "id": campaign,
                    "resourceName": f"customers/{_CUSTOMER}/campaigns/{campaign}",
                    "status": status,
                }
            }
        ],
        "totalResultsCount": "1",
        "fieldMask": "campaign.id,campaign.resource_name,campaign.status",
        "queryResourceConsumption": "1",
    }


def _install_http(monkeypatch: pytest.MonkeyPatch, requests: list[httpx.Request]) -> None:
    real_client = httpx.Client

    def client() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.method == "POST"
            assert request.url.path == f"/v25/customers/{_CUSTOMER}/googleAds:search"
            assert request.headers["authorization"] == f"Bearer {_ACCESS}"
            assert request.headers["developer-token"] == _DEVELOPER
            assert request.headers["login-customer-id"] == _LOGIN
            body = json.loads(request.content)
            assert body["searchSettings"] == {"returnTotalResultsCount": True}
            assert set(body) == {"query", "searchSettings"}
            if "FROM customer" in body["query"]:
                return httpx.Response(200, json=_identity_payload())
            assert body["query"].endswith("campaign.id = 42")
            return httpx.Response(200, json=_campaign_payload())

        return real_client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(edit_session, "_google_ads_http_client", client)


def test_session_proves_fixed_identity_then_reads_one_campaign_without_generic_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)
    monkeypatch.setattr(
        credentials,
        "credential",
        lambda *_args, **_kwargs: pytest.fail("generic credential() must not be used"),
    )

    with edit_session.configured_google_ads_edit_session(
        tmp_path, profile.profile_id, expected
    ) as active:
        assert active.identity.status == "CLOSED"
        observed = active.read_campaign_state("42")
        assert observed.status == "PAUSED"
        assert _ACCESS not in repr(active)
        assert _DEVELOPER not in repr(active)
        assert "_bearer" not in active.__slots__
        assert "_developer" not in active.__slots__
        assert not hasattr(active, "mutate_campaign")

    assert len(requests) == 2


def test_stale_or_malformed_expected_configuration_fails_before_store_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, _expected = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)
    reads_before = len(store.reads)

    with pytest.raises(AdhelmError) as stale:
        with edit_session.configured_google_ads_edit_session(
            tmp_path, profile.profile_id, "0" * 64
        ):
            pytest.fail("stale configuration must fail before the session is yielded")
    assert stale.value.code == "CONFIGURATION_CHANGED"
    assert len(store.reads) == reads_before
    assert requests == []

    with pytest.raises(AdhelmError) as malformed:
        with edit_session.configured_google_ads_edit_session(
            tmp_path, profile.profile_id, "malformed-digest"
        ):
            pytest.fail("malformed digest must fail before the session is yielded")
    assert malformed.value.code == "GOOGLE_ADS_EDIT_SESSION_INVALID"
    assert len(store.reads) == reads_before
    assert requests == []


def test_developer_slot_drift_stops_before_subsequent_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, expected = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)
    assert profile.secondary_credential_ref is not None

    with edit_session.configured_google_ads_edit_session(
        tmp_path, profile.profile_id, expected
    ) as active:
        store.values[(credentials._SERVICE, profile.secondary_credential_ref)] = "rotated-developer"
        with pytest.raises(AdhelmError) as raised:
            active.read_campaign_state("42")

    assert raised.value.code == "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED"
    assert len(requests) == 1


def test_configuration_drift_stops_before_subsequent_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)

    with edit_session.configured_google_ads_edit_session(
        tmp_path, profile.profile_id, expected
    ) as active:
        save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
        # A complete identical configuration does not drift; use a safe local
        # output preference change to produce an exact new configuration digest.
        changed = load_configuration(tmp_path).model_copy(
            update={"output": OutputPreferences(artifact_mode="OFF")}
        )
        save_configuration(tmp_path, changed, replace=True)
        with pytest.raises(AdhelmError) as raised:
            active.read_campaign_state("42")

    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert len(requests) == 1


def test_expiry_and_clock_rollback_stop_before_subsequent_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected = _configured(
        tmp_path, monkeypatch, expires_at=_NOW + timedelta(minutes=5)
    )
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)

    with edit_session.configured_google_ads_edit_session(
        tmp_path, profile.profile_id, expected
    ) as active:
        monkeypatch.setattr(edit_session, "_now", lambda: _NOW + timedelta(minutes=6))
        with pytest.raises(AdhelmError) as expired:
            active.read_campaign_state("42")
    assert expired.value.code == "GOOGLE_ADS_EDIT_TOKEN_EXPIRED"
    assert len(requests) == 1

    monkeypatch.setattr(edit_session, "_now", lambda: _NOW)
    requests.clear()
    with edit_session.configured_google_ads_edit_session(
        tmp_path, profile.profile_id, expected
    ) as active:
        monkeypatch.setattr(edit_session, "_now", lambda: _NOW - timedelta(seconds=1))
        with pytest.raises(AdhelmError) as rollback:
            active.read_campaign_state("42")
    assert rollback.value.code == "CLOCK_INVALID"
    assert len(requests) == 1


def test_missing_developer_slot_and_read_profile_fail_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, expected = _configured(tmp_path, monkeypatch)
    assert profile.secondary_credential_ref is not None
    del store.values[(credentials._SERVICE, profile.secondary_credential_ref)]
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)

    with pytest.raises(AdhelmError) as missing:
        with edit_session.configured_google_ads_edit_session(
            tmp_path, profile.profile_id, expected
        ):
            pytest.fail("missing developer token must reject")
    assert missing.value.code == "GOOGLE_ADS_DEVELOPER_TOKEN_REQUIRED"
    assert requests == []

    read_profile = _profile(
        profile_id="google-ads-read",
        purpose="READ",
        scopes=[GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "READ")]],
    )
    save_configuration(tmp_path, Configuration(profiles=[read_profile]), replace=True)
    with pytest.raises(AdhelmError) as wrong_purpose:
        with edit_session.configured_google_ads_edit_session(tmp_path, read_profile.profile_id):
            pytest.fail("READ profile must reject before OAuth or HTTP")
    assert wrong_purpose.value.code == "GOOGLE_ADS_EDIT_PROFILE_REQUIRED"
    assert requests == []


def test_closed_or_constructed_session_has_no_escaped_read_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, expected = _configured(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    _install_http(monkeypatch, requests)

    with edit_session.configured_google_ads_edit_session(
        tmp_path, profile.profile_id, expected
    ) as active:
        closed = active
    with pytest.raises(AdhelmError) as raised:
        closed.read_campaign_state("42")
    assert raised.value.code == "GOOGLE_ADS_EDIT_SESSION_CLOSED"

    forged = object.__new__(edit_session.GoogleAdsEditSession)
    with pytest.raises(AdhelmError) as invalid:
        _ = forged.identity
    assert invalid.value.code == "GOOGLE_ADS_EDIT_SESSION_INVALID"

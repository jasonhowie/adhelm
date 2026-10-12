"""Independent fake-only review of Google Ads EDIT observation boundaries."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm import config, credentials
from adhelm import google_ads_edit_session as edit_session
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.google_oauth_setup import GoogleOAuthProfileDraft
from adhelm.models import Configuration, Profile
from adhelm.providers import google_ads_campaign_state as campaign_state
from adhelm.transport import SafeHttpClient

_ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"
_PRINCIPAL_REF = "google_oidc_principal_v1." + "a" * 64
_ACCESS_TOKEN = "fixture-google-ads-access-token"
_DEVELOPER_TOKEN = "fixture-google-ads-developer-token"


def _draft_data(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "profile_id": "google-ads-edit",
        "provider": "google_ads",
        "account_ref": "1234567890",
        "client_ref": "fixture.apps.googleusercontent.com",
        "scopes": [_ADWORDS_SCOPE],
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_ref": "google-ads-oauth-slot",
        "secondary_credential_ref": "google-ads-developer-slot",
    }
    value.update(updates)
    return value


def _profile_data(**updates: object) -> dict[str, object]:
    value = _draft_data(
        principal_ref=_PRINCIPAL_REF,
        credential_source="OS_OAUTH_GOOGLE",
    )
    value.update(updates)
    return value


@pytest.mark.parametrize("missing", ("currency", "timezone"))
@pytest.mark.parametrize("model", (GoogleOAuthProfileDraft, Profile))
def test_google_ads_edit_requires_explicit_expectations_before_default_normalization(
    missing: str, model: type[GoogleOAuthProfileDraft] | type[Profile]
) -> None:
    raw: dict[str, object] = _draft_data() if model is GoogleOAuthProfileDraft else _profile_data()
    raw.pop(missing)

    with pytest.raises(ValidationError):
        model.model_validate(raw)


@pytest.mark.parametrize("null_field", ("currency", "timezone"))
@pytest.mark.parametrize("model", (GoogleOAuthProfileDraft, Profile))
def test_google_ads_edit_rejects_null_expectations(
    null_field: str, model: type[GoogleOAuthProfileDraft] | type[Profile]
) -> None:
    raw: dict[str, object] = _draft_data() if model is GoogleOAuthProfileDraft else _profile_data()
    raw[null_field] = None

    with pytest.raises(ValidationError):
        model.model_validate(raw)


def test_google_ads_read_retains_existing_currency_and_timezone_defaults() -> None:
    draft_raw = _draft_data(purpose="READ", currency=None)
    draft_raw.pop("currency")
    draft_raw.pop("timezone")
    profile_raw = _profile_data(purpose="READ", currency=None)
    profile_raw.pop("currency")
    profile_raw.pop("timezone")

    draft = GoogleOAuthProfileDraft.model_validate(draft_raw)
    profile = Profile.model_validate(profile_raw)

    assert draft.currency is None
    assert draft.timezone == "UTC"
    assert profile.currency is None
    assert profile.timezone == "UTC"


def test_google_ads_edit_schema_and_runtime_share_the_same_scope_and_required_facts() -> None:
    assert GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE["google_ads", "READ"] == _ADWORDS_SCOPE
    assert GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE["google_ads", "EDIT"] == _ADWORDS_SCOPE

    draft_schema = GoogleOAuthProfileDraft.model_json_schema()
    alternatives = draft_schema["allOf"][0]["oneOf"]
    edit = next(
        choice
        for choice in alternatives
        if choice["properties"]["provider"] == {"const": "google_ads"}
        and choice["properties"]["purpose"] == {"const": "EDIT"}
    )
    assert set(edit["required"]) == {"provider", "purpose", "scopes", "currency", "timezone"}
    assert edit["properties"]["scopes"] == {"const": [_ADWORDS_SCOPE]}
    assert edit["properties"]["currency"] == {"type": "string"}

    profile_schema: dict[str, Any] = Profile.model_json_schema()
    conditional = profile_schema["allOf"][0]
    assert conditional["then"]["required"] == ["currency", "timezone"]
    assert conditional["then"]["properties"]["currency"] == {"type": "string"}


def _target() -> object:
    return campaign_state._GoogleAdsEditTarget(
        customer_client_ref="1234567890",
        login_customer_client_ref="0987654321",
        declared_currency="USD",
        declared_timezone="UTC",
    )


def _identity_payload(*, status: str = "CLOSED") -> dict[str, object]:
    return {
        "results": [
            {
                "customer": {
                    "id": "1234567890",
                    "resourceName": "customers/1234567890",
                    "currencyCode": "USD",
                    "timeZone": "UTC",
                    "status": status,
                }
            }
        ],
        "totalResultsCount": "1",
    }


def _campaign_payload(*, campaign_id: str = "42", status: str = "PAUSED") -> dict[str, object]:
    return {
        "results": [
            {
                "campaign": {
                    "id": campaign_id,
                    "resourceName": f"customers/1234567890/campaigns/{campaign_id}",
                    "status": status,
                }
            }
        ],
        "totalResultsCount": "1",
    }


def _reader(
    handler: httpx.MockTransport,
    getter: campaign_state.HeaderGetter | None = None,
    *,
    client: httpx.Client | None = None,
) -> campaign_state.GoogleAdsCampaignStateReader:
    http_client = client or httpx.Client(transport=handler, trust_env=False)
    transport = SafeHttpClient(
        http_client,
        {"googleads.googleapis.com"},
        maximum_response_bytes=65_536,
        read_attempts=1,
    )
    return campaign_state.GoogleAdsCampaignStateReader(
        transport,
        getter
        or (
            lambda: {
                "authorization": f"Bearer {_ACCESS_TOKEN}",
                "developer-token": _DEVELOPER_TOKEN,
                "login-customer-id": "0987654321",
            }
        ),
        _target(),  # type: ignore[arg-type]
    )


def test_fixed_google_ads_edit_reads_keep_closed_customer_status_non_gating() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == httpx.URL(
            "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
        )
        assert request.headers["authorization"] == f"Bearer {_ACCESS_TOKEN}"
        assert request.headers["developer-token"] == _DEVELOPER_TOKEN
        assert request.headers["login-customer-id"] == "0987654321"
        query = json.loads(request.content)["query"]
        if query == campaign_state._IDENTITY_QUERY:
            return httpx.Response(200, json=_identity_payload(status="CLOSED"))
        assert query == campaign_state._CAMPAIGN_QUERY_PREFIX + "42"
        return httpx.Response(200, json=_campaign_payload(campaign_id="42", status="PAUSED"))

    reader = _reader(httpx.MockTransport(handler))
    identity = reader.discover_identity()
    state = reader.read_campaign_state(identity, "42")

    assert identity.status == "CLOSED"
    assert state.status == "PAUSED"
    assert state.campaign_ref == "google_ads.campaign.1234567890.42"
    assert len(requests) == 2
    assert all("mutate" not in str(request.url) for request in requests)


@pytest.mark.parametrize("campaign_id", ("0", "01", "-1", "1.0", "9223372036854775808", 42, True))
def test_campaign_id_is_exact_positive_int64_before_credential_callback_or_post(
    campaign_id: object,
) -> None:
    callback_calls: list[object] = []
    requests: list[httpx.Request] = []

    def getter() -> dict[str, str]:
        callback_calls.append(object())
        return {
            "authorization": f"Bearer {_ACCESS_TOKEN}",
            "developer-token": _DEVELOPER_TOKEN,
            "login-customer-id": "0987654321",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    reader = _reader(httpx.MockTransport(handler), getter)
    identity = campaign_state.GoogleAdsEditIdentity(
        customer_ref="google_ads.customer.1234567890",
        customer_client_ref="1234567890",
        currency="USD",
        timezone="UTC",
        status="CLOSED",
    )

    with pytest.raises(AdhelmError) as raised:
        reader.read_campaign_state(identity, campaign_id)  # type: ignore[arg-type]

    assert raised.value.code == "INVALID_INPUT"
    assert raised.value.exit_code == ExitCode.INVALID_INPUT
    assert callback_calls == []
    assert requests == []


def test_header_callback_cannot_smuggle_ambient_cookie_state_into_fixed_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    def mutating_getter() -> dict[str, str]:
        client.cookies.set("private-cookie", "private-value")
        return {
            "authorization": f"Bearer {_ACCESS_TOKEN}",
            "developer-token": _DEVELOPER_TOKEN,
            "login-customer-id": "0987654321",
        }

    reader = _reader(
        httpx.MockTransport(lambda _request: httpx.Response(500)),
        mutating_getter,
        client=client,
    )

    with pytest.raises(AdhelmError) as raised:
        reader.discover_identity()

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert requests == []


def test_constructed_reader_and_mismatched_identity_fail_before_post() -> None:
    malformed = object.__new__(campaign_state.GoogleAdsCampaignStateReader)
    with pytest.raises(AdhelmError) as malformed_error:
        malformed.discover_identity()
    assert malformed_error.value.code == "TRANSPORT_CONFIG_INVALID"

    callback_calls: list[object] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    def getter() -> dict[str, str]:
        callback_calls.append(object())
        return {
            "authorization": f"Bearer {_ACCESS_TOKEN}",
            "developer-token": _DEVELOPER_TOKEN,
            "login-customer-id": "0987654321",
        }

    reader = _reader(httpx.MockTransport(handler), getter)
    mismatched = campaign_state.GoogleAdsEditIdentity(
        customer_ref="google_ads.customer.1234567890",
        customer_client_ref="1234567890",
        currency="USD",
        timezone="America/Chicago",
        status="CLOSED",
    )

    with pytest.raises(AdhelmError) as identity_error:
        reader.read_campaign_state(mismatched, "42")

    assert identity_error.value.code == "IDENTITY_BINDING_INVALID"
    assert callback_calls == []
    assert requests == []


@dataclass
class _SessionMemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads.append((service_name, username))
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[service_name, username] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


_SESSION_NOW = datetime(2029, 1, 1, tzinfo=UTC)


def _session_profile() -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE["google_ads", "EDIT"]
    return Profile(
        profile_id="google-ads-edit-observation",
        provider="google_ads",
        account_ref="1234567890",
        principal_ref=derive_google_oidc_principal_ref(
            GOOGLE_OIDC_CANONICAL_ISSUER, "independent-google-ads-edit-subject"
        ),
        client_ref="independent-google-ads.apps.googleusercontent.com",
        scopes=[scope],
        purpose="EDIT",
        currency="USD",
        timezone="UTC",
        credential_source="OS_OAUTH_GOOGLE",
        credential_ref="independent-google-ads-oauth",
        secondary_credential_ref="independent-google-ads-developer",
        login_customer_ref="0987654321",
    )


def _session_grant(
    profile: Profile, *, expires_at: datetime, access_token: str = _ACCESS_TOKEN
) -> GoogleOAuthInitialGrant:
    scope = profile.scopes[0]
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, scope),
        access_token=access_token,
        expires_at=expires_at,
        refresh_token="independent-google-ads-refresh-token",
        refresh_token_expires_at=None,
        requested_scopes=("openid", scope),
        returned_scopes=(scope,),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject="independent-google-ads-edit-subject",
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _install_session_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime = _SESSION_NOW + timedelta(minutes=10),
) -> tuple[_SessionMemoryStore, Profile, list[datetime]]:
    store = _SessionMemoryStore()
    session_clock = [_SESSION_NOW]
    monkeypatch.setattr(credentials, "_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config, "credential_mutation_lock", lambda _reference: nullcontext())
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: _SESSION_NOW)
    monkeypatch.setattr(edit_session, "_now", lambda: session_clock[0])

    profile = _session_profile()
    configuration = Configuration(profiles=[profile])
    save_configuration(tmp_path, configuration)
    slot = oauth_credentials.prepare_google_oauth_slot(profile.credential_ref, replace=False)
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _session_grant(profile, expires_at=expires_at),
        client_secret=None,
        expected_slot=slot,
    )
    assert profile.secondary_credential_ref is not None
    store.values[credentials._SERVICE, profile.secondary_credential_ref] = _DEVELOPER_TOKEN
    return store, profile, session_clock


def _session_handler(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == httpx.URL(
            "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
        )
        assert request.headers["authorization"] == f"Bearer {_ACCESS_TOKEN}"
        assert request.headers["developer-token"] == _DEVELOPER_TOKEN
        assert request.headers["login-customer-id"] == "0987654321"
        body = json.loads(request.content)
        assert set(body) == {"query", "searchSettings"}
        assert body["searchSettings"] == {"returnTotalResultsCount": True}
        if body["query"] == campaign_state._IDENTITY_QUERY:
            return httpx.Response(200, json=_identity_payload(status="CLOSED"))
        assert body["query"] == campaign_state._CAMPAIGN_QUERY_PREFIX + "42"
        return httpx.Response(200, json=_campaign_payload(campaign_id="42", status="PAUSED"))

    return httpx.MockTransport(handler)


def test_fixed_search_uses_nested_v25_search_settings_and_requires_resource_names() -> None:
    requests: list[httpx.Request] = []
    reader = _reader(_session_handler(requests))

    identity = reader.discover_identity()
    reader.read_campaign_state(identity, "42")

    assert len(requests) == 2
    for request in requests:
        assert json.loads(request.content)["searchSettings"] == {"returnTotalResultsCount": True}

    missing_customer = _identity_payload()
    missing_customer["results"][0]["customer"].pop("resourceName")  # type: ignore[index]
    with pytest.raises(AdhelmError) as customer_error:
        _reader(
            httpx.MockTransport(lambda _request: httpx.Response(200, json=missing_customer))
        ).discover_identity()
    assert customer_error.value.code == "PROVIDER_RESPONSE_INVALID"

    missing_campaign = _campaign_payload()
    missing_campaign["results"][0]["campaign"].pop("resourceName")  # type: ignore[index]
    with pytest.raises(AdhelmError) as campaign_error:
        _reader(
            httpx.MockTransport(lambda _request: httpx.Response(200, json=missing_campaign))
        ).read_campaign_state(
            campaign_state.GoogleAdsEditIdentity(
                customer_ref="google_ads.customer.1234567890",
                customer_client_ref="1234567890",
                currency="USD",
                timezone="UTC",
                status="CLOSED",
            ),
            "42",
        )
    assert campaign_error.value.code == "PROVIDER_RESPONSE_INVALID"


def test_session_rechecks_configuration_and_both_protected_slots_before_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, _session_clock = _install_session_profile(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        edit_session,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=_session_handler(requests), trust_env=False),
    )

    with edit_session.configured_google_ads_edit_session(tmp_path, profile.profile_id) as session:
        assert len(requests) == 1
        changed = profile.model_copy(update={"timezone": "America/Chicago"})
        save_configuration(tmp_path, Configuration(profiles=[changed]), replace=True)
        with pytest.raises(AdhelmError) as config_error:
            session.read_campaign_state("42")
        assert config_error.value.code == "CONFIGURATION_CHANGED"
        assert len(requests) == 1

    # A fresh session proves that a developer-token slot change is detected
    # in its fixed header callback before its campaign POST.
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    with edit_session.configured_google_ads_edit_session(tmp_path, profile.profile_id) as session:
        assert len(requests) == 2
        assert profile.secondary_credential_ref is not None
        store.values[credentials._SERVICE, profile.secondary_credential_ref] = "replacement-token"
        with pytest.raises(AdhelmError) as credential_error:
            session.read_campaign_state("42")
        assert credential_error.value.code == "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED"
        assert len(requests) == 2
        assert _DEVELOPER_TOKEN not in str(credential_error.value)
        assert "replacement-token" not in str(credential_error.value)


def test_session_rejects_primary_oauth_bundle_rotation_before_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, _session_clock = _install_session_profile(tmp_path, monkeypatch)
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        edit_session,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=_session_handler(requests), trust_env=False),
    )

    with edit_session.configured_google_ads_edit_session(tmp_path, profile.profile_id) as session:
        assert len(requests) == 1
        replacement = oauth_credentials.prepare_google_oauth_install(
            Configuration(profiles=[profile]), profile.profile_id, replace=True
        )
        oauth_credentials.install_google_oauth_grant(
            Configuration(profiles=[profile]),
            profile.profile_id,
            _session_grant(
                profile,
                expires_at=_SESSION_NOW + timedelta(minutes=10),
                access_token="rotated-google-ads-access-token",
            ),
            client_secret=None,
            expected_slot=replacement,
            replace=True,
        )
        with pytest.raises(AdhelmError) as credential_error:
            session.read_campaign_state("42")
        assert credential_error.value.code == "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED"
        assert len(requests) == 1
        assert _ACCESS_TOKEN not in str(credential_error.value)
        assert "rotated-google-ads-access-token" not in str(credential_error.value)


def test_session_checks_expiry_and_constructed_state_before_campaign_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expires_at = _SESSION_NOW + timedelta(minutes=2)
    _store, profile, session_clock = _install_session_profile(
        tmp_path, monkeypatch, expires_at=expires_at
    )
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        edit_session,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=_session_handler(requests), trust_env=False),
    )

    with edit_session.configured_google_ads_edit_session(tmp_path, profile.profile_id) as session:
        assert len(requests) == 1
        session_clock[0] = expires_at
        with pytest.raises(AdhelmError) as expired:
            session.read_campaign_state("42")
        assert expired.value.code == "GOOGLE_ADS_EDIT_TOKEN_EXPIRED"
        assert len(requests) == 1

    malformed = object.__new__(edit_session.GoogleAdsEditSession)
    with pytest.raises(AdhelmError) as malformed_error:
        _ = malformed.identity
    assert malformed_error.value.code == "GOOGLE_ADS_EDIT_SESSION_INVALID"

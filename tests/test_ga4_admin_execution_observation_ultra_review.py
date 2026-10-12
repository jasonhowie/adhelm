"""Independent fake-only review of OAuth-bound GA4 Admin EDIT observations."""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import adhelm.ga4_admin_execution_observation as observation
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_execution_binding import Ga4AdminEditOAuthExecutionBinding
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.models import Configuration, OutputPreferences, Profile

_KEY = b"m24-ultra-fingerprint-key-material-0001"
_OTHER_KEY = b"m24-ultra-fingerprint-key-material-0002"
_NOW = datetime(2031, 2, 3, tzinfo=UTC)
_CLIENT = "m24-ultra-ga4-admin.apps.googleusercontent.com"
_SUBJECT = "m24-ultra-private-oidc-subject"
_ACCESS = "m24-ultra-private-access-token"
_REFRESH = "m24-ultra-private-refresh-token"
_SCOPE = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", "EDIT")]
_HTTP_CLIENT = httpx.Client


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    reads: int = 0

    def get_password(self, service_name: str, username: str) -> str | None:
        self.reads += 1
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _profile(*, credential_source: str = "OS_OAUTH_GOOGLE", suffix: str = "main") -> Profile:
    return Profile(
        profile_id=f"m24-ultra-edit-{suffix}",
        provider="ga4_admin",
        account_ref="123",
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        client_ref=_CLIENT,
        scopes=[_SCOPE],
        purpose="EDIT",
        currency="USD",
        timezone="UTC",
        credential_source=credential_source,
        credential_ref=f"m24-ultra-oauth-{suffix}",
    )


def _grant(profile: Profile) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, _SCOPE),
        access_token=_ACCESS,
        expires_at=_NOW + timedelta(hours=1),
        refresh_token=_REFRESH,
        refresh_token_expires_at=None,
        requested_scopes=("openid", _SCOPE),
        returned_scopes=(_SCOPE,),
        identity=VerifiedGoogleOidcIdentity(
            canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
            raw_subject=_SUBJECT,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        ),
    )


def _install(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: Profile | None = None,
) -> tuple[_MemoryStore, Profile, Configuration, list[datetime]]:
    now = [_NOW]
    store = _MemoryStore()
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials,
        "credential_mutation_lock",
        lambda _reference: nullcontext(),
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: now[0])
    monkeypatch.setattr(observation, "_utc_now", lambda: now[0])
    selected = profile or _profile(suffix=directory.name)
    configuration = Configuration(
        profiles=[selected],
        output=OutputPreferences(max_output_bytes=16_384),
    )
    save_configuration(directory, configuration)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration,
        selected.profile_id,
        replace=False,
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        selected.profile_id,
        _grant(selected),
        client_secret=None,
        expected_slot=slot,
    )
    store.reads = 0
    return store, selected, configuration, now


def _install_paginated_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.content == b""
        assert request.url.host == "analyticsadmin.googleapis.com"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        if request.url.path == "/v1beta/properties/123":
            assert tuple(request.url.params.multi_items()) == (
                ("fields", "name,timeZone,currencyCode,serviceLevel,deleteTime"),
            )
            return httpx.Response(
                200,
                json={
                    "name": "properties/123",
                    "timeZone": "UTC",
                    "currencyCode": "USD",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                },
            )
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            assert tuple(request.url.params.multi_items()) == (("fields", "name,type"),)
            return httpx.Response(
                200,
                json={"name": "properties/123/dataStreams/456", "type": "WEB_DATA_STREAM"},
            )
        assert request.url.path == "/v1alpha/properties/123/keyEvents"
        page = request.url.params.get("pageToken")
        if page is None:
            assert tuple(request.url.params.multi_items()) == (
                ("pageSize", "200"),
                (
                    "fields",
                    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),nextPageToken",
                ),
            )
            return httpx.Response(
                200,
                json={
                    "keyEvents": [
                        {
                            "name": "properties/123/keyEvents/701",
                            "eventName": "ultra_private_event_one",
                            "custom": True,
                            "deletable": True,
                            "countingMethod": "ONCE_PER_EVENT",
                        }
                    ],
                    "nextPageToken": "m24-next",
                },
            )
        assert page == "m24-next"
        return httpx.Response(
            200,
            json={
                "keyEvents": [
                    {
                        "name": "properties/123/keyEvents/702",
                        "eventName": "ultra_private_event_two",
                        "custom": False,
                        "deletable": False,
                        "countingMethod": "ONCE_PER_SESSION",
                    }
                ]
            },
        )

    def factory(*, trust_env: bool, follow_redirects: bool) -> httpx.Client:
        assert trust_env is False
        assert follow_redirects is False
        return _HTTP_CLIENT(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(observation.httpx, "Client", factory)
    return requests


def _assert_redacted(error: AdhelmError) -> None:
    rendered = str(error)
    for private in (_ACCESS, _REFRESH, _SUBJECT, _KEY.hex(), "ultra_private_event"):
        assert private not in rendered


def test_paginated_fixed_gets_use_one_bound_bearer_and_preserve_caveats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, profile, configuration, _now = _install(tmp_path, monkeypatch)
    requests = _install_paginated_transport(monkeypatch)

    result = observation.observe_ga4_admin_edit_execution_inventory(
        tmp_path,
        profile.profile_id,
        stream_id="456",
        fingerprint_key=_KEY,
        expected_configuration_digest=digest(configuration),
    )

    assert store.reads == 1
    assert [request.url.path for request in requests] == [
        "/v1beta/properties/123",
        "/v1beta/properties/123/dataStreams/456",
        "/v1alpha/properties/123/keyEvents",
        "/v1alpha/properties/123/keyEvents",
    ]
    assert {request.headers["authorization"] for request in requests} == {f"Bearer {_ACCESS}"}
    assert result.inventory.capacity.complete_observed_key_event_total == 2
    assert result.oauth_binding.profile_ref == profile.profile_id
    assert result.oauth_binding.profile_configuration_digest == digest(profile)
    assert result.oauth_binding.principal_ref == profile.principal_ref
    assert result.oauth_binding.client_ref == profile.client_ref
    assert result.oauth_binding.provider_create_permission == "NOT_PREPROVABLE"
    assert result.inventory_content_integrity == "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    assert result.observation_content_integrity == "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    assert re.fullmatch(r"[a-f0-9]{64}", result.observation_content_marker)
    assert result.consistency == "SEQUENTIAL_NONATOMIC"
    assert result.granted_scopes == "NOT_EVALUATED"
    assert result.current_provider_state == "NOT_ESTABLISHED"
    assert result.reservation_status == "NOT_ESTABLISHED"
    assert result.execution_authority is False
    assert result.remote_mutations is False
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    for private in (_ACCESS, _REFRESH, _SUBJECT, _KEY.hex(), "ultra_private_event"):
        assert private not in rendered
    assert (
        observation.revalidate_ga4_admin_edit_execution_observation(
            result,
            fingerprint_key=_KEY,
        )
        == result
    )


@pytest.mark.parametrize("source", ["OS_STORE", "EXPLICIT_CI"])
def test_non_oauth_profile_is_rejected_before_key_or_lifecycle_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    profile = _profile(credential_source=source, suffix=f"{source.lower()}-{tmp_path.name}")
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    lifecycle_calls = 0

    def unexpected_binding(_profile: Profile) -> object:
        nonlocal lifecycle_calls
        lifecycle_calls += 1
        pytest.fail("rejected profile reached protected OAuth lifecycle")

    monkeypatch.setattr(observation, "_resolve_binding", unexpected_binding)
    monkeypatch.setattr(
        observation,
        "_require_fingerprint_key",
        lambda _key: pytest.fail("rejected profile reached fingerprint-key validation"),
    )
    with pytest.raises(AdhelmError) as raised:
        observation.observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED"
    assert lifecycle_calls == 0


@pytest.mark.parametrize("fingerprint_key", [b"short", b"x" * 4097])
def test_invalid_fingerprint_key_stops_before_oauth_lifecycle_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fingerprint_key: object,
) -> None:
    _store, profile, _configuration, _now = _install(tmp_path, monkeypatch)
    lifecycle_calls = 0

    def unexpected_binding(_profile: Profile) -> object:
        nonlocal lifecycle_calls
        lifecycle_calls += 1
        pytest.fail("invalid fingerprint key reached protected OAuth lifecycle")

    monkeypatch.setattr(observation, "_resolve_binding", unexpected_binding)
    with pytest.raises(AdhelmError) as raised:
        observation.observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=fingerprint_key,  # type: ignore[arg-type]
        )
    assert raised.value.code == "FINGERPRINT_KEY_REQUIRED"
    assert lifecycle_calls == 0


def test_constructed_wrong_binding_identity_cannot_reach_bearer_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, _configuration, _now = _install(tmp_path, monkeypatch)
    forged = object.__new__(Ga4AdminEditOAuthExecutionBinding)
    for name, value in {
        "profile_ref": "other-profile",
        "profile_digest": "a" * 64,
        "provider": "ga4_admin",
        "purpose": "EDIT",
        "credential_source": "OS_OAUTH_GOOGLE",
        "account_ref": "123",
        "principal_ref": profile.principal_ref,
        "client_ref": profile.client_ref,
        "api_scope": _SCOPE,
    }.items():
        object.__setattr__(forged, name, value)
    consumed = 0

    def unexpected_consume(_binding: object, _profile: Profile) -> str:
        nonlocal consumed
        consumed += 1
        pytest.fail("identity-mismatched binding reached bearer consumption")

    monkeypatch.setattr(observation, "_resolve_binding", lambda _profile: forged)
    monkeypatch.setattr(observation, "_consume_binding", unexpected_consume)
    monkeypatch.setattr(
        observation.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("identity-mismatched binding reached HTTP"),
    )
    with pytest.raises(AdhelmError) as raised:
        observation.observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_ADMIN_EDIT_EXECUTION_IDENTITY_MISMATCH"
    assert consumed == 0


def test_configuration_drift_after_binding_stops_before_bearer_and_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, configuration, _now = _install(tmp_path, monkeypatch)
    consumes = 0

    def drift_after_binding(_profile: Profile) -> object:
        changed = configuration.model_copy(
            update={"output": OutputPreferences(max_output_bytes=2048)}
        )
        save_configuration(tmp_path, changed, replace=True)
        return object()

    def unexpected_consume(_binding: object, _profile: Profile) -> str:
        nonlocal consumes
        consumes += 1
        pytest.fail("configuration drift reached bearer consumption")

    monkeypatch.setattr(observation, "_resolve_binding", drift_after_binding)
    monkeypatch.setattr(observation, "_consume_binding", unexpected_consume)
    monkeypatch.setattr(
        observation.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("configuration drift reached HTTP"),
    )
    with pytest.raises(AdhelmError) as raised:
        observation.observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert consumes == 0


def test_unrequested_provider_shape_fails_closed_without_raw_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, _configuration, _now = _install(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/properties/123"
        return httpx.Response(
            200,
            json={
                "name": "properties/123",
                "timeZone": "UTC",
                "currencyCode": "USD",
                "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                "private_unrequested_provider_value": "ultra_private_event",
            },
        )

    monkeypatch.setattr(
        observation.httpx,
        "Client",
        lambda *, trust_env, follow_redirects: _HTTP_CLIENT(
            transport=httpx.MockTransport(handler),
            trust_env=trust_env,
            follow_redirects=follow_redirects,
        ),
    )
    with pytest.raises(AdhelmError) as raised:
        observation.observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    _assert_redacted(raised.value)


def test_public_fact_and_snapshot_serializers_reject_constructed_or_copied_bypasses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, profile, _configuration, _now = _install(tmp_path, monkeypatch)
    _install_paginated_transport(monkeypatch)
    result = observation.observe_ga4_admin_edit_execution_inventory(
        tmp_path,
        profile.profile_id,
        stream_id="456",
        fingerprint_key=_KEY,
    )

    direct_facts = observation.Ga4AdminEditExecutionOAuthBindingFacts.model_construct(
        execution_authority=True,
    )
    with pytest.raises(ValueError, match="OAuth binding facts are invalid"):
        direct_facts.model_dump(mode="json")

    forged_facts = result.oauth_binding.model_copy(update={"execution_authority": True})
    forged_snapshot = result.model_copy(update={"oauth_binding": forged_facts})
    with pytest.raises(ValueError, match="execution observation is invalid"):
        forged_snapshot.model_dump(mode="json")

    reversed_interval = result.model_copy(
        update={"read_finished_at": result.read_started_at - timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="execution observation is invalid"):
        reversed_interval.model_dump(mode="json")

    with pytest.raises(AdhelmError) as wrong_key:
        observation.revalidate_ga4_admin_edit_execution_observation(
            result,
            fingerprint_key=_OTHER_KEY,
        )
    assert wrong_key.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"


@pytest.mark.parametrize(
    "updates",
    [
        {"configuration_digest": "0" * 64},
        {"observation_content_marker": "0" * 64},
        {
            "read_started_at": _NOW - timedelta(days=1),
            "read_finished_at": _NOW - timedelta(days=1) + timedelta(seconds=1),
        },
    ],
)
def test_copied_observation_cannot_rebind_or_tamper_with_sealed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, object],
) -> None:
    _store, profile, _configuration, _now = _install(tmp_path, monkeypatch)
    _install_paginated_transport(monkeypatch)
    result = observation.observe_ga4_admin_edit_execution_inventory(
        tmp_path,
        profile.profile_id,
        stream_id="456",
        fingerprint_key=_KEY,
    )

    forged = result.model_copy(update=updates)
    with pytest.raises(AdhelmError) as raised:
        observation.revalidate_ga4_admin_edit_execution_observation(
            forged,
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "INVALID_GA4_ADMIN_EDIT_EXECUTION_OBSERVATION"


@pytest.mark.parametrize("value", [True, 1.0, "private-not-a-digest"])
def test_invalid_public_request_stops_before_config_or_oauth_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    monkeypatch.setattr(
        observation,
        "validate_ga4_admin_edit_inventory",
        lambda *_args, **_kwargs: pytest.fail("invalid request reached configuration preflight"),
    )
    with pytest.raises(AdhelmError) as raised:
        observation.validate_ga4_admin_edit_execution_observation(
            tmp_path,
            "m24-ultra-profile",
            stream_id="456",
            expected_configuration_digest=value,  # type: ignore[arg-type]
        )
    assert raised.value.code == "INVALID_GA4_ADMIN_EDIT_EXECUTION_INPUT"
    assert "private-not-a-digest" not in str(raised.value)
    assert raised.value.exit_code is ExitCode.INVALID_INPUT

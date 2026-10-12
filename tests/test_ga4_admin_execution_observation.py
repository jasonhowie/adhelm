"""Hermetic lifecycle-bound GA4 Admin EDIT inventory observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from pydantic import ValidationError

from adhelm import ga4_admin_execution_observation as execution_observation
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ga4_admin_execution_observation import (
    Ga4AdminEditExecutionInventoryObservation,
    Ga4AdminEditExecutionOAuthBindingFacts,
    observe_ga4_admin_edit_execution_inventory,
    revalidate_ga4_admin_edit_execution_observation,
)
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
)
from adhelm.transport import SafeHttpClient

_KEY = b"m24-fingerprint-key-for-fake-observation!"
_OTHER_KEY = b"m24-other-fingerprint-key-for-tests!!"
_NOW = datetime(2031, 1, 1, tzinfo=UTC)
_CLIENT = "m24-ga4-admin.apps.googleusercontent.com"
_SUBJECT = "m24-private-oidc-subject"
_ACCESS = "m24-private-oauth-access"
_REFRESH = "m24-private-oauth-refresh"
_SCOPE = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", "EDIT")]


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


def _profile(
    *,
    credential_source: Literal["OS_STORE", "EXPLICIT_CI", "OS_OAUTH_GOOGLE"] = "OS_OAUTH_GOOGLE",
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"] = "EDIT",
    profile_id: str = "m24-edit-profile",
) -> Profile:
    scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", purpose)]
    return Profile(
        profile_id=profile_id,
        provider="ga4_admin",
        account_ref="123",
        principal_ref=derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
        client_ref=_CLIENT,
        scopes=[scope],
        purpose=purpose,
        currency="USD",
        timezone="UTC",
        credential_source=credential_source,
        credential_ref="m24-edit-oauth-primary",
    )


def _configuration(profile: Profile) -> Configuration:
    return Configuration(profiles=[profile], output=OutputPreferences(max_output_bytes=16_384))


def _grant(
    profile: Profile, *, expires_at: datetime = _NOW + timedelta(hours=1)
) -> GoogleOAuthInitialGrant:
    return GoogleOAuthInitialGrant(
        target=GoogleOAuthTarget(profile.client_ref, _SCOPE),
        access_token=_ACCESS,
        expires_at=expires_at,
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


def _install_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, profile: Profile | None = None
) -> tuple[_MemoryStore, Profile, Configuration, list[datetime]]:
    current = [_NOW]
    store = _MemoryStore()
    monkeypatch.setattr(oauth_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        oauth_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(oauth_credentials, "_utc_now", lambda: current[0])
    selected = profile or _profile()
    configuration = _configuration(selected)
    save_configuration(tmp_path, configuration)
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, selected.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        selected.profile_id,
        _grant(selected),
        client_secret=None,
        expected_slot=slot,
    )
    store.reads = 0
    return store, selected, configuration, current


def _install_fixed_inventory_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_list: Callable[[], None] | None = None,
    count: int = 2,
) -> list[httpx.Request]:
    client_type = httpx.Client
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET" and not request.content
        assert request.url.host == "analyticsadmin.googleapis.com"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        if request.url.path == "/v1beta/properties/123":
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
            return httpx.Response(
                200,
                json={"name": "properties/123/dataStreams/456", "type": "WEB_DATA_STREAM"},
            )
        assert request.url.path == "/v1alpha/properties/123/keyEvents"
        if on_list is not None:
            on_list()
        return httpx.Response(
            200,
            json={
                "keyEvents": [
                    {
                        "name": f"properties/123/keyEvents/{700 + index}",
                        "eventName": f"m24_private_event_{index}",
                        "custom": True,
                        "deletable": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                    for index in range(count)
                ]
            },
        )

    def factory(*, trust_env: bool, follow_redirects: bool) -> httpx.Client:
        assert trust_env is False and follow_redirects is False
        return client_type(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    monkeypatch.setattr("adhelm.ga4_admin_execution_observation.httpx.Client", factory)
    return calls


def _inventory(
    key: bytes, *, property_id: str = "123", stream_id: str = "456"
) -> Ga4AdminEditKeyEventInventory:
    target = Ga4AdminEditInventoryTarget(
        property_ref=f"ga4_admin.property.{property_id}",
        property_client_ref=property_id,
        web_stream_ref=f"ga4_admin.web_stream.{stream_id}",
        web_stream_client_ref=stream_id,
        expected_timezone="UTC",
        expected_currency="USD",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1beta/properties/{property_id}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{property_id}",
                    "timeZone": "UTC",
                    "currencyCode": "USD",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                },
            )
        if request.url.path == f"/v1beta/properties/{property_id}/dataStreams/{stream_id}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{property_id}/dataStreams/{stream_id}",
                    "type": "WEB_DATA_STREAM",
                },
            )
        return httpx.Response(200, json={"keyEvents": []})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: _ACCESS,
            fingerprint_key=key,
        ).read_inventory(target)


def test_observes_one_fixed_inventory_with_one_lifecycle_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, configuration, _current = _install_oauth(tmp_path, monkeypatch)
    calls = _install_fixed_inventory_http(monkeypatch)

    result = observe_ga4_admin_edit_execution_inventory(
        tmp_path,
        profile.profile_id,
        stream_id="456",
        fingerprint_key=_KEY,
        expected_configuration_digest=digest(configuration),
    )

    assert store.reads == 1
    assert len(calls) == 3
    assert result.configuration_digest == digest(configuration)
    assert result.oauth_binding.profile_ref == profile.profile_id
    assert result.oauth_binding.profile_configuration_digest == digest(profile)
    assert result.oauth_binding.property_client_ref == "123"
    assert result.oauth_binding.principal_ref == profile.principal_ref
    assert result.oauth_binding.client_ref == profile.client_ref
    assert result.target.web_stream_client_ref == "456"
    assert result.inventory_digest == digest(result.inventory)
    assert result.read_started_at.tzinfo is not None
    assert result.read_finished_at >= result.read_started_at
    assert result.fingerprint_key_continuity == "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"
    assert result.inventory_content_integrity == "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    assert result.observation_content_integrity == (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    )
    assert result.consistency == "SEQUENTIAL_NONATOMIC"
    assert result.provider_create_permission == "NOT_PREPROVABLE"
    assert result.current_provider_state == "NOT_ESTABLISHED"
    assert result.reservation_status == "NOT_ESTABLISHED"
    assert result.execution_authority is False and result.remote_mutations is False
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    for private in (_ACCESS, _REFRESH, _SUBJECT, _KEY.hex(), "m24_private_event"):
        assert private not in rendered
    assert revalidate_ga4_admin_edit_execution_observation(result, fingerprint_key=_KEY) == result


@pytest.mark.parametrize("source", ["OS_STORE", "EXPLICIT_CI"])
def test_non_oauth_edit_profiles_fail_before_key_or_lifecycle_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    profile = _profile(credential_source=cast(Literal["OS_STORE", "EXPLICIT_CI"], source))
    configuration = _configuration(profile)
    save_configuration(tmp_path, configuration)
    calls = 0

    def unexpected_resolve(_: Profile) -> object:
        nonlocal calls
        calls += 1
        pytest.fail("a non-OAuth profile reached protected lifecycle resolution")

    monkeypatch.setattr(
        execution_observation, "resolve_ga4_admin_edit_execution_binding", unexpected_resolve
    )
    monkeypatch.setattr(
        execution_observation,
        "_require_fingerprint_key",
        lambda _value: pytest.fail("a non-OAuth profile reached fingerprint-key validation"),
    )

    with pytest.raises(AdhelmError) as raised:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED"
    assert calls == 0


def test_preflight_digest_drift_stops_before_protected_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, _configuration_value, _current = _install_oauth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        execution_observation,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _: pytest.fail("drift must stop before lifecycle resolution"),
    )

    with pytest.raises(AdhelmError) as raised:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
            expected_configuration_digest="0" * 64,
        )
    assert raised.value.code == "CONFIGURATION_DRIFT"


def test_configuration_drift_after_binding_stops_before_bearer_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, configuration, _current = _install_oauth(tmp_path, monkeypatch)
    calls = 0

    def drift_after_binding(_: Profile) -> object:
        changed = configuration.model_copy(
            update={"output": OutputPreferences(max_output_bytes=2048)}
        )
        save_configuration(tmp_path, changed, replace=True)
        return object()

    def unexpected_bearer(_: object, __: Profile) -> str:
        nonlocal calls
        calls += 1
        pytest.fail("configuration drift reached bearer consumption")

    monkeypatch.setattr(execution_observation, "_resolve_binding", drift_after_binding)
    monkeypatch.setattr(execution_observation, "_consume_binding", unexpected_bearer)
    monkeypatch.setattr(
        "adhelm.ga4_admin_execution_observation.httpx.Client",
        lambda **_kwargs: pytest.fail("configuration drift reached HTTP"),
    )

    with pytest.raises(AdhelmError) as raised:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert calls == 0


def test_configuration_drift_after_bearer_stops_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, configuration, _current = _install_oauth(tmp_path, monkeypatch)
    original_consume = execution_observation._consume_binding

    def consume_then_drift(binding: object, selected: Profile) -> str:
        token = original_consume(binding, selected)
        save_configuration(
            tmp_path,
            configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=2048)}),
            replace=True,
        )
        return token

    monkeypatch.setattr(execution_observation, "_consume_binding", consume_then_drift)
    monkeypatch.setattr(
        "adhelm.ga4_admin_execution_observation.httpx.Client",
        lambda **_kwargs: pytest.fail("post-bearer configuration drift reached HTTP"),
    )

    with pytest.raises(AdhelmError) as raised:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "CONFIGURATION_DRIFT"


def test_configuration_drift_during_fixed_reads_rejects_returned_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, configuration, _current = _install_oauth(tmp_path, monkeypatch)

    def drift() -> None:
        save_configuration(
            tmp_path,
            configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=2048)}),
            replace=True,
        )

    calls = _install_fixed_inventory_http(monkeypatch, on_list=drift)
    with pytest.raises(AdhelmError) as raised:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert len(calls) == 3


def test_configuration_drift_after_current_key_revalidation_rejects_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, configuration, _current = _install_oauth(tmp_path, monkeypatch)
    _install_fixed_inventory_http(monkeypatch)
    original_revalidate = execution_observation.revalidate_ga4_admin_edit_execution_observation

    def revalidate_then_drift(
        value: object, *, fingerprint_key: bytes
    ) -> Ga4AdminEditExecutionInventoryObservation:
        checked = original_revalidate(value, fingerprint_key=fingerprint_key)
        save_configuration(
            tmp_path,
            configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=2048)}),
            replace=True,
        )
        return checked

    monkeypatch.setattr(
        execution_observation,
        "revalidate_ga4_admin_edit_execution_observation",
        revalidate_then_drift,
    )
    with pytest.raises(AdhelmError) as raised:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert raised.value.code == "CONFIGURATION_DRIFT"


def test_tampered_or_target_mismatched_returned_inventory_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, _configuration_value, _current = _install_oauth(tmp_path, monkeypatch)
    tampered = _inventory(_KEY).model_copy(update={"fingerprint_key_marker": "0" * 64})

    class TamperedReader:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_inventory(self, _target: object) -> Ga4AdminEditKeyEventInventory:
            return tampered

    monkeypatch.setattr(
        execution_observation, "Ga4AdminEditKeyEventInventoryClient", TamperedReader
    )
    with pytest.raises(AdhelmError) as marker_error:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert marker_error.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"

    mismatched = _inventory(_KEY, property_id="999")

    class MismatchedReader:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_inventory(self, _target: object) -> Ga4AdminEditKeyEventInventory:
            return mismatched

    monkeypatch.setattr(
        execution_observation, "Ga4AdminEditKeyEventInventoryClient", MismatchedReader
    )
    with pytest.raises(AdhelmError) as identity_error:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path,
            profile.profile_id,
            stream_id="456",
            fingerprint_key=_KEY,
        )
    assert identity_error.value.code == "GA4_ADMIN_EDIT_EXECUTION_IDENTITY_MISMATCH"


def test_result_revalidation_and_serializer_reject_constructed_or_copy_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, _configuration_value, _current = _install_oauth(tmp_path, monkeypatch)
    _install_fixed_inventory_http(monkeypatch)
    result = observe_ga4_admin_edit_execution_inventory(
        tmp_path, profile.profile_id, stream_id="456", fingerprint_key=_KEY
    )
    forged = result.model_copy(update={"execution_authority": True})
    with pytest.raises(ValueError, match="execution observation is invalid"):
        forged.model_dump(mode="json")
    with pytest.raises(AdhelmError) as forged_error:
        revalidate_ga4_admin_edit_execution_observation(forged, fingerprint_key=_KEY)
    assert forged_error.value.code == "INVALID_GA4_ADMIN_EDIT_EXECUTION_OBSERVATION"

    incomplete = Ga4AdminEditExecutionInventoryObservation.model_construct(
        configuration_digest="a" * 64
    )
    with pytest.raises(ValueError, match="execution observation is invalid"):
        incomplete.model_dump(mode="json")

    float_version = result.model_copy(update={"schema_version": 1.0})
    with pytest.raises(ValueError, match="execution observation is invalid"):
        float_version.model_dump(mode="json")

    with pytest.raises(AdhelmError) as wrong_key:
        revalidate_ga4_admin_edit_execution_observation(result, fingerprint_key=_OTHER_KEY)
    assert wrong_key.value.code == "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH"

    for altered in (
        result.model_copy(update={"configuration_digest": "0" * 64}),
        result.model_copy(
            update={
                "read_started_at": result.read_started_at - timedelta(days=1),
                "read_finished_at": result.read_finished_at - timedelta(days=1),
            }
        ),
        result.model_copy(update={"observation_content_marker": "f" * 64}),
    ):
        with pytest.raises(AdhelmError) as sealed:
            revalidate_ga4_admin_edit_execution_observation(altered, fingerprint_key=_KEY)
        assert sealed.value.code == "INVALID_GA4_ADMIN_EDIT_EXECUTION_OBSERVATION"

    forged_facts = Ga4AdminEditExecutionOAuthBindingFacts.model_construct(execution_authority=True)
    with pytest.raises(ValueError, match="OAuth binding facts are invalid"):
        forged_facts.model_dump(mode="json")


def test_read_clock_rollback_and_unexpected_lifecycle_failure_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, _configuration_value, _current = _install_oauth(tmp_path, monkeypatch)
    _install_fixed_inventory_http(monkeypatch)
    samples = iter((_NOW + timedelta(seconds=2), _NOW + timedelta(seconds=1)))
    monkeypatch.setattr(execution_observation, "_utc_now", lambda: next(samples))
    with pytest.raises(AdhelmError) as rollback:
        observe_ga4_admin_edit_execution_inventory(
            tmp_path, profile.profile_id, stream_id="456", fingerprint_key=_KEY
        )
    assert rollback.value.code == "GA4_ADMIN_EDIT_OBSERVATION_CLOCK_UNAVAILABLE"

    monkeypatch.setattr(
        execution_observation,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: (_ for _ in ()).throw(RuntimeError("m24-private-error")),
    )
    with pytest.raises(AdhelmError) as unavailable:
        execution_observation._resolve_binding(profile)
    assert unavailable.value.code == "GA4_ADMIN_EDIT_OAUTH_UNAVAILABLE"
    assert "m24-private-error" not in str(unavailable.value)


@pytest.mark.parametrize("alias", [True, 1.0])
def test_request_rejects_nonstring_digest_aliases_before_config_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: object
) -> None:
    monkeypatch.setattr(
        execution_observation,
        "validate_ga4_admin_edit_inventory",
        lambda *_args, **_kwargs: pytest.fail("invalid request reached config preflight"),
    )
    with pytest.raises(AdhelmError) as raised:
        execution_observation.validate_ga4_admin_edit_execution_observation(
            tmp_path,
            "profile",
            stream_id="456",
            expected_configuration_digest=alias,  # type: ignore[arg-type]
        )
    assert raised.value.code == "INVALID_GA4_ADMIN_EDIT_EXECUTION_INPUT"


def test_request_schema_rejects_raw_float_and_boolean_versions() -> None:
    raw = {
        "schema_version": 1.0,
        "profile_id": "profile",
        "stream_id": "456",
        "expected_configuration_digest": None,
    }
    with pytest.raises(ValidationError):
        execution_observation.Ga4AdminEditExecutionInventoryRequest.model_validate(raw)
    raw["schema_version"] = True
    with pytest.raises(ValidationError):
        execution_observation.Ga4AdminEditExecutionInventoryRequest.model_validate(raw)

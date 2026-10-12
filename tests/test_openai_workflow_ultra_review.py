"""Adversarial boundaries for the OpenAI conversion measurement composition."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.openai_measurement_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile
from adhelm.providers.openai_ads import OpenAIAdsClient, OpenAIAdsInventoryQuery
from adhelm.transport import SafeHttpClient


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "openai-ultra-read",
        "provider": "openai_ads",
        "account_ref": "account_fixture",
        "principal_ref": "principal.fixture",
        "client_ref": "client.fixture",
        "scopes": ["account-scoped-api-key"],
        "purpose": "READ",
        "currency": "USD",
        "timezone": "America/Chicago",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": "OPENAI_ADS_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _configure(root: Path, profile: Profile) -> None:
    save_configuration(root, Configuration(profiles=[profile]))


def _account(
    *,
    account_id: str = "account_fixture",
    timezone: str = "America/Chicago",
    name: str = "Fixture account name discarded before return",
) -> dict[str, object]:
    return {
        "id": account_id,
        "name": name,
        "url": "https://example.invalid/account",
        "preview_url": None,
        "status": "active",
        "timezone": timezone,
        "currency_code": "USD",
        "review": {"status": "approved"},
    }


def _setting(setting_id: str, *, account_id: str = "account_fixture") -> dict[str, object]:
    return {
        "id": setting_id,
        "name": "Fixture conversion name discarded before return",
        "event_type": "order_created",
        "custom_event_name": None,
        "attribution_window_days": 30,
        "ad_account_id": account_id,
        "source_ids": ["source_fixture"],
        "sources": [{"id": "source_fixture", "name": "Source name discarded"}],
        "campaigns": [],
        "archived": False,
        "version": 1,
    }


def _page(settings: list[dict[str, object]], *, has_more: bool = False) -> dict[str, object]:
    return {
        "object": "list",
        "data": settings,
        "first_id": settings[0]["id"] if settings else None,
        "last_id": settings[-1]["id"] if settings else None,
        "has_more": has_more,
    }


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[int]:
    real_client = httpx.Client
    constructions: list[int] = []

    def client_factory(*, trust_env: bool) -> httpx.Client:
        constructions.append(1)
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)
    return constructions


def test_public_openai_request_schema_is_versioned_and_preserves_defaults() -> None:
    request = workflow.OpenAIConversionsReadRequest(profile_id="openai-ultra-read")
    assert request.schema_version == 1
    assert request.maximum_records == request.page_size == 500
    assert request.ci_enabled is False
    assert (
        workflow.OpenAIConversionsReadRequest.model_json_schema()["properties"]["schema_version"][
            "const"
        ]
        == 1
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("account_ref", "account/not-an-opaque-id", "INVALID_OPENAI_ACCOUNT"),
        ("timezone", "not-a-real-timezone", "INVALID_PROFILE_TIMEZONE"),
    ],
)
def test_semantically_invalid_profile_identity_fails_before_credentials_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    _configure(tmp_path, _profile(**{field: value}))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        raise AssertionError("invalid local profile reached HTTP")

    constructions = _install_transport(monkeypatch, handler)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-ultra-read")
    assert raised.value.code == expected_code
    assert constructions == []
    assert requests == []


@pytest.mark.parametrize("field", ["principal_ref", "client_ref"])
def test_forged_principal_or_client_model_copy_fails_before_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    forged = _profile().model_copy(update={field: "invalid local reference"})
    configuration = Configuration(profiles=[_profile()]).model_copy(update={"profiles": [forged]})
    monkeypatch.setattr(workflow, "load_configuration", lambda _root: configuration)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        raise AssertionError("forged profile reached HTTP")

    constructions = _install_transport(monkeypatch, handler)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-ultra-read")
    assert raised.value.code == "CONFIGURATION_INVALID"
    assert constructions == []
    assert requests == []


def test_nonboolean_ci_opt_in_fails_before_configuration_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "load_configuration",
        lambda _root: (_ for _ in ()).throw(AssertionError("configuration was read")),
    )
    constructions = _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("invalid input reached HTTP")),
    )
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-ultra-read", ci_enabled="true")  # type: ignore[arg-type]
    assert raised.value.code == "INVALID_WORKFLOW_INPUT"
    assert constructions == []


def test_whole_session_uses_one_credential_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    requests: list[httpx.Request] = []
    credential_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer first-fixture-token"
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/conversions/event_settings"
        if request.url.params.get("after") is None:
            return httpx.Response(200, json=_page([_setting("setting_first")], has_more=True))
        assert request.url.params.get("after") == "setting_first"
        return httpx.Response(200, json=_page([_setting("setting_second")]))

    _install_transport(monkeypatch, handler)

    def snapshot_once(_profile: Profile, *, ci_enabled: bool) -> str:
        nonlocal credential_calls
        credential_calls += 1
        assert ci_enabled is True
        if credential_calls != 1:
            raise AssertionError("credential was read more than once")
        return "first-fixture-token"

    monkeypatch.setattr(workflow, "credential", snapshot_once)
    _identity, settings = workflow.read_openai_conversions(
        tmp_path,
        "openai-ultra-read",
        maximum_records=2,
        page_size=1,
        ci_enabled=True,
    )
    assert [item.conversion_setting_client_ref for item in settings] == [
        "setting_first",
        "setting_second",
    ]
    assert credential_calls == 1
    assert [request.url.path for request in requests] == [
        "/v1/ad_account",
        "/v1/conversions/event_settings",
        "/v1/conversions/event_settings",
    ]


@pytest.mark.parametrize("token", ["token space", "token\x7f", "token\u0100"])
def test_malformed_workflow_credential_fails_before_http_client_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    _configure(tmp_path, _profile())
    requests: list[str] = []
    constructions = _install_transport(
        monkeypatch,
        lambda request: requests.append(request.url.path) or httpx.Response(500),
    )
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: token)

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-ultra-read")
    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert constructions == []
    assert requests == []
    assert token not in raised.value.message


def test_mutated_cached_identity_is_not_current_and_cannot_start_child_egress() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        raise AssertionError("mutated identity reached child HTTP")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ads = OpenAIAdsClient(
            SafeHttpClient(client, {"api.ads.openai.com"}, read_attempts=1),
            lambda: "fixture-token-not-for-output",
        )
        identity = ads.discover_identity(expected_account_client_ref="account_fixture")
        object.__setattr__(identity, "timezone", "UTC")
        assert ads.is_current_identity(identity) is False
        with pytest.raises(AdhelmError) as raised:
            ads.list_campaigns(
                OpenAIAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert requests == ["/v1/ad_account"]


def test_invalid_provider_timezone_stops_identity_before_conversion_egress() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_account(timezone="not-a-real-timezone"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ads = OpenAIAdsClient(
            SafeHttpClient(client, {"api.ads.openai.com"}, read_attempts=1),
            lambda: "fixture-token-not-for-output",
        )
        with pytest.raises(AdhelmError) as raised:
            ads.discover_identity(expected_account_client_ref="account_fixture")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert requests == ["/v1/ad_account"]


@pytest.mark.parametrize("token", ["token space", "token\x7f", "token\u0100"])
def test_direct_openai_ads_client_rejects_malformed_bearer_before_http(token: str) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ads = OpenAIAdsClient(
            SafeHttpClient(client, {"api.ads.openai.com"}, read_attempts=1), lambda: token
        )
        with pytest.raises(AdhelmError) as raised:
            ads.discover_identity(expected_account_client_ref="account_fixture")
    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert token not in raised.value.message
    assert requests == []


def test_raw_account_display_name_is_discarded_before_workflow_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    marker = "raw account display name must not reach output"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account(name=marker))
        return httpx.Response(200, json=_page([_setting("setting_fixture")]))

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(
        workflow, "credential", lambda *_args, **_kwargs: "fixture-token-not-for-output"
    )
    identity, settings = workflow.read_openai_conversions(tmp_path, "openai-ultra-read")
    serialized = f"{identity.model_dump_json()} {[item.model_dump_json() for item in settings]}"
    assert marker not in serialized

"""End-to-end boundaries for OpenAI Ads conversion-setting measurement reads."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.openai_measurement_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile
from adhelm.providers.openai_ads import OpenAIAdsClient
from adhelm.transport import SafeHttpClient


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "openai-conversions-read",
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
    *, account_id: str = "account_fixture", timezone: str = "America/Chicago", currency: str = "USD"
) -> dict[str, object]:
    return {
        "id": account_id,
        "name": "Fixture account name discarded before return",
        "url": "https://example.invalid/account",
        "preview_url": None,
        "status": "active",
        "timezone": timezone,
        "currency_code": currency,
        "review": {"status": "approved"},
    }


def _setting(
    setting_id: str,
    *,
    account_id: str = "account_fixture",
) -> dict[str, object]:
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
) -> None:
    real_client = httpx.Client

    def client_factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr("adhelm.openai_measurement_workflow.httpx.Client", client_factory)


def _install_credential(monkeypatch: pytest.MonkeyPatch, calls: list[Profile]) -> None:
    def fixture_credential(profile: Profile, *, ci_enabled: bool) -> str:
        calls.append(profile)
        assert ci_enabled is True
        return "fixture-token-not-for-output"

    monkeypatch.setattr(workflow, "credential", fixture_credential)


def test_read_binds_observed_identity_to_same_session_conversion_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    requests: list[httpx.Request] = []
    credential_calls: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.ads.openai.com"
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/conversions/event_settings"
        assert request.url.params == httpx.QueryParams({"limit": "1", "order": "desc"})
        return httpx.Response(200, json=_page([_setting("setting_fixture")]))

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, credential_calls)
    identity, settings = workflow.read_openai_conversions(
        tmp_path,
        "openai-conversions-read",
        maximum_records=1,
        page_size=1,
        ci_enabled=True,
    )

    assert identity.account_ref == "openai_ads.account.account_fixture"
    assert identity.account_client_ref == "account_fixture"
    assert identity.timezone == "America/Chicago"
    assert identity.currency == "USD"
    assert [item.conversion_setting_client_ref for item in settings] == ["setting_fixture"]
    assert [request.url.path for request in requests] == [
        "/v1/ad_account",
        "/v1/conversions/event_settings",
    ]
    assert credential_calls and all(
        call.profile_id == "openai-conversions-read" for call in credential_calls
    )


@pytest.mark.parametrize(
    ("profile", "expected_code"),
    [
        (_profile(purpose="EDIT"), "READ_PROFILE_REQUIRED"),
        (_profile(provider="meta_ads"), "READ_PROFILE_REQUIRED"),
    ],
)
def test_profile_provider_and_purpose_fail_before_credential_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    expected_code: str,
) -> None:
    _configure(tmp_path, profile)
    requests: list[str] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        raise AssertionError("invalid profile reached HTTP")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-conversions-read")
    assert raised.value.code == expected_code
    assert requests == []


def test_model_copy_invalid_scope_fails_before_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged_profile = _profile().model_copy(update={"scopes": []})
    forged_configuration = Configuration(profiles=[_profile()]).model_copy(
        update={"profiles": [forged_profile]}
    )
    monkeypatch.setattr(workflow, "load_configuration", lambda _root: forged_configuration)
    calls: list[str] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("forged profile reached HTTP")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("forged profile read credential")
        ),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-conversions-read")
    assert raised.value.code == "CONFIGURATION_INVALID"
    assert calls == []


def test_expected_currency_is_required_before_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile(currency=None))
    calls: list[str] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("currency-less profile reached HTTP")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("currency-less profile read credential")
        ),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-conversions-read")
    assert raised.value.code == "EXPECTED_CURRENCY_REQUIRED"
    assert calls == []


@pytest.mark.parametrize(
    ("account_id", "timezone", "currency"),
    [
        ("other_account", "America/Chicago", "USD"),
        ("account_fixture", "UTC", "USD"),
        ("account_fixture", "America/Chicago", "CAD"),
    ],
)
def test_identity_account_timezone_and_currency_mismatch_stop_before_settings_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_id: str,
    timezone: str,
    currency: str,
) -> None:
    _configure(tmp_path, _profile())
    paths: list[str] = []
    credential_calls: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/v1/ad_account"
        return httpx.Response(
            200, json=_account(account_id=account_id, timezone=timezone, currency=currency)
        )

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, credential_calls)
    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-conversions-read", ci_enabled=True)
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert paths == ["/v1/ad_account"]
    assert credential_calls


def test_invalid_public_page_bound_stops_before_configuration_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        workflow,
        "load_configuration",
        lambda _root: (_ for _ in ()).throw(AssertionError("configuration was read")),
    )

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("invalid input reached HTTP")

    _install_transport(monkeypatch, unexpected_request)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid input read credential")
        ),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(tmp_path, "openai-conversions-read", maximum_records=True)
    assert raised.value.code == "INVALID_WORKFLOW_INPUT"
    assert calls == []


def test_late_page_failure_returns_no_partial_measurement_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    paths: list[str] = []
    credential_calls: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/conversions/event_settings"
        if request.url.params.get("after") is None:
            return httpx.Response(200, json=_page([_setting("setting_first")], has_more=True))
        assert request.url.params.get("after") == "setting_first"
        return httpx.Response(
            200, json=_page([_setting("setting_second", account_id="other_account")])
        )

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, credential_calls)
    with pytest.raises(AdhelmError) as raised:
        workflow.read_openai_conversions(
            tmp_path,
            "openai-conversions-read",
            maximum_records=2,
            page_size=1,
            ci_enabled=True,
        )
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert paths == [
        "/v1/ad_account",
        "/v1/conversions/event_settings",
        "/v1/conversions/event_settings",
    ]
    assert credential_calls
    assert not (tmp_path / "ledger").exists()


def test_current_identity_verifier_rejects_lookalike_and_stale_discovery_object() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/ad_account"
        return httpx.Response(200, json=_account())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ads = OpenAIAdsClient(
            SafeHttpClient(client, {"api.ads.openai.com"}, read_attempts=1),
            lambda: "fixture-token-not-for-output",
        )
        first = ads.discover_identity(expected_account_client_ref="account_fixture")
        lookalike = first.model_copy()
        assert ads.is_current_identity(first) is True
        assert ads.is_current_identity(lookalike) is False
        second = ads.discover_identity(expected_account_client_ref="account_fixture")
        assert ads.is_current_identity(first) is False
        assert ads.is_current_identity(second) is True
    assert calls == 2


def test_current_identity_verifier_invalidates_prior_object_after_failed_refresh() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/ad_account"
        if calls == 1:
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json={"id": "account_fixture"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ads = OpenAIAdsClient(
            SafeHttpClient(client, {"api.ads.openai.com"}, read_attempts=1),
            lambda: "fixture-token-not-for-output",
        )
        first = ads.discover_identity(expected_account_client_ref="account_fixture")
        with pytest.raises(AdhelmError) as raised:
            ads.discover_identity(expected_account_client_ref="account_fixture")
        assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
        assert ads.is_current_identity(first) is False
    assert calls == 2


@pytest.mark.parametrize("status", [403, 404])
def test_a_feature_gated_account_is_a_typed_fact_not_an_authorization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """The workflow reads the account-enabled surface through its typed outcome.

    A `403` or a `404` from this one fixed route says the ad account does not
    expose the feature; it must not reach the customer as
    `OPENAI_ADS_ACCESS_NOT_PERMITTED`, which is an authorization diagnostic
    about the stored key.
    """
    _configure(tmp_path, _profile())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        return httpx.Response(status, json={"error": "not enabled"})

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, [])
    identity, availability = workflow.read_openai_conversion_availability(
        tmp_path, "openai-conversions-read", ci_enabled=True
    )
    assert identity.account_client_ref == "account_fixture"
    assert availability.status == "NOT_ENABLED_FOR_ACCOUNT"
    assert availability.observed_http_status == status
    assert availability.settings == ()

    # The settings-only projection has nothing to return, so it refuses with the
    # same feature fact rather than reporting an account with zero settings.
    with pytest.raises(AdhelmError) as error:
        workflow.read_openai_conversions(tmp_path, "openai-conversions-read", ci_enabled=True)
    assert error.value.code == "OPENAI_ADS_CONVERSIONS_NOT_ENABLED_FOR_ACCOUNT"
    assert error.value.exit_code == 3
    assert "was not refused" in error.value.next_action


def test_an_unusable_key_on_the_conversion_route_stays_an_authorization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only 403 and 404 are a feature fact; a 401 is still about the key."""
    _configure(tmp_path, _profile())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        return httpx.Response(401, json={"error": "bad key"})

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, [])
    with pytest.raises(AdhelmError) as error:
        workflow.read_openai_conversion_availability(
            tmp_path, "openai-conversions-read", ci_enabled=True
        )
    assert error.value.code == "OPENAI_ADS_KEY_INVALID_OR_EXPIRED"
    assert error.value.exit_code == 4

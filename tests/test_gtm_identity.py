from __future__ import annotations

import json

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.gtm_identity import GtmIdentityClient
from adhelm.providers.gtm_read import GtmReadIdentity
from adhelm.transport import SafeHttpClient


def _account(**overrides: object) -> dict[str, object]:
    return {
        "path": "accounts/1",
        "accountId": "1",
        "name": "customer account",
        "shareData": False,
        "fingerprint": "provider-account-fingerprint",
        "tagManagerUrl": "https://tagmanager.google.com/customer-account",
        "features": {"supportUserPermissions": True, "supportMultipleContainers": False},
        **overrides,
    }


def _container(**overrides: object) -> dict[str, object]:
    return {
        "path": "accounts/1/containers/2",
        "accountId": "1",
        "containerId": "2",
        "name": "customer container",
        "domainName": ["customer.example"],
        "publicId": "GTM-SECRET",
        "tagIds": ["GTM-SECRET"],
        "features": {"supportWorkspaces": True, "supportTemplates": False},
        "notes": "customer-only notes",
        "usageContext": ["web"],
        "fingerprint": "provider-container-fingerprint",
        "tagManagerUrl": "https://tagmanager.google.com/customer-container",
        "taggingServerUrls": [],
        **overrides,
    }


def _client(
    handler: httpx.MockTransport, *, token_getter: object = lambda: "fixture-token"
) -> GtmIdentityClient:
    assert callable(token_getter)
    return GtmIdentityClient(
        SafeHttpClient(
            httpx.Client(transport=handler, trust_env=False),
            {"tagmanager.googleapis.com"},
            read_attempts=1,
        ),
        token_getter,
    )


@pytest.mark.parametrize("response_cap", [0, 8_388_609, True, "8192"])
def test_rejects_unbounded_or_invalid_transport_before_token_or_http(response_cap: object) -> None:
    token_calls = 0

    def getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return "fixture-token"

    transport = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: (_ for _ in ()).throw(AssertionError("no HTTP"))
            ),
            trust_env=False,
        ),
        {"tagmanager.googleapis.com"},
        maximum_response_bytes=response_cap,  # type: ignore[arg-type]
        read_attempts=1,
    )
    with pytest.raises(AdhelmError) as raised:
        GtmIdentityClient(transport, getter)
    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert token_calls == 0


def test_discovers_bound_web_identity_and_only_normalized_features() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        if request.url.path.endswith("/accounts/1/containers/2"):
            return httpx.Response(200, json=_container())
        raise AssertionError(request.url.path)

    client = _client(httpx.MockTransport(handler))
    discovery = client.discover_identity("1", "2")

    assert [request.url.path for request in requests] == [
        "/tagmanager/v2/accounts/1",
        "/tagmanager/v2/accounts/1/containers/2",
    ]
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["authorization"] == "Bearer fixture-token" for request in requests)
    assert discovery.identity.account_ref == "gtm.account.1"
    assert discovery.identity.container_ref == "gtm.container.2"
    assert discovery.account_features.support_user_permissions == "TRUE"
    assert discovery.account_features.support_multiple_containers == "FALSE"
    assert discovery.container_features.support_workspaces == "TRUE"
    assert discovery.container_features.support_templates == "FALSE"
    assert discovery.container_features.support_versions == "UNKNOWN"
    encoded = json.dumps(discovery.model_dump(mode="json"), sort_keys=True)
    for raw in (
        "customer.example",
        "customer-only notes",
        "provider-container-fingerprint",
        "GTM-SECRET",
    ):
        assert raw not in encoded
    assert client.is_current_identity(discovery.identity) is True


@pytest.mark.parametrize(
    ("account", "container", "expected_code"),
    [
        ("01x", "2", "INVALID_INPUT"),
        ("1", "2/../../3", "INVALID_INPUT"),
        ("１", "2", "INVALID_INPUT"),
    ],
)
def test_public_ids_fail_before_token_or_http(
    account: str, container: str, expected_code: str
) -> None:
    token_calls = 0

    def getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return "fixture-token"

    no_http = httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError("no HTTP")))
    with pytest.raises(AdhelmError) as raised:
        _client(no_http, token_getter=getter).discover_identity(account, container)
    assert raised.value.code == expected_code
    assert token_calls == 0


def test_response_identity_path_and_unknown_fields_fail_closed() -> None:
    responses = [
        _account(path="accounts/9"),
        _account(),
        _container(accountId="9"),
        _account(extra="unexpected"),
    ]
    for response in responses:
        calls = 0

        def handler(_: httpx.Request, response: dict[str, object] = response) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=response)

        with pytest.raises(AdhelmError) as raised:
            _client(httpx.MockTransport(handler)).discover_identity("1", "2")
        assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
        assert calls <= 2


@pytest.mark.parametrize(
    ("account_response", "container_response"),
    [
        (_account(), _container(path="accounts/1/containers/9")),
        (_account(), _container(containerId="9")),
        (_account(), _container(unexpectedField=True)),
    ],
)
def test_container_response_is_bound_to_exact_requested_parent(
    account_response: dict[str, object], container_response: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=account_response)
        if request.url.path.endswith("/accounts/1/containers/2"):
            return httpx.Response(200, json=container_response)
        raise AssertionError(request.url.path)

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity("1", "2")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("usage_context", [None, [], ["server"], ["android"], ["web", "server"]])
def test_only_declared_web_usage_context_is_in_scope(usage_context: object) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json=_container(usageContext=usage_context))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).discover_identity("1", "2")
    assert raised.value.code == "GTM_WEB_CONTAINER_REQUIRED"
    assert calls == 2


def test_missing_features_remain_unknown_and_invalid_feature_value_fails() -> None:
    def missing_features(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account(features=None))
        return httpx.Response(200, json=_container(features=None))

    discovery = _client(httpx.MockTransport(missing_features)).discover_identity("1", "2")
    assert discovery.account_features.support_user_permissions == "UNKNOWN"
    assert discovery.container_features.support_workspaces == "UNKNOWN"

    def invalid_feature(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json=_container(features={"supportWorkspaces": "yes"}))

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(invalid_feature)).discover_identity("1", "2")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("token", ["", "space token", "token\nvalue", "token🙂"])
def test_invalid_token_fails_before_http(token: str) -> None:
    no_http = httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError("no HTTP")))
    with pytest.raises(AdhelmError) as raised:
        _client(no_http, token_getter=lambda: token).discover_identity("1", "2")
    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"


def test_discovery_uses_one_token_snapshot_for_both_fixed_reads() -> None:
    token_calls = 0

    def getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return "fixture-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json=_container())

    _client(httpx.MockTransport(handler), token_getter=getter).discover_identity("1", "2")
    assert token_calls == 1


def test_exact_current_object_only_and_refresh_invalidates_old_proof() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        if calls <= 2:
            return httpx.Response(200, json=_container())
        return httpx.Response(200, json={"accountId": "1"})

    client = _client(httpx.MockTransport(handler))
    first = client.discover_identity("1", "2").identity
    assert client.is_current_identity(first) is True
    assert client.is_current_identity(first.model_copy()) is False
    assert (
        client.is_current_identity(
            GtmReadIdentity(
                account_ref="gtm.account.1",
                account_client_ref="1",
                container_ref="gtm.container.2",
                container_client_ref="2",
            )
        )
        is False
    )
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("1", "2")
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert client.is_current_identity(first) is False

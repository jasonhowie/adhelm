"""Adversarial regressions for the GTM identity-to-inventory handoff."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.gtm_measurement_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile
from adhelm.providers.gtm_identity import GtmIdentityClient
from adhelm.transport import SafeHttpClient

_HOST = "tagmanager.googleapis.com"
_READONLY_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
_FINGERPRINT_KEY = b"f" * 32
_PRIVATE_MARKER = "customer-token-and-id-must-not-escape"


def _account() -> dict[str, object]:
    return {
        "path": "accounts/1",
        "accountId": "1",
        "features": {"supportUserPermissions": True},
    }


def _container() -> dict[str, object]:
    return {
        "path": "accounts/1/containers/2",
        "accountId": "1",
        "containerId": "2",
        "usageContext": ["web"],
        "features": {"supportWorkspaces": True},
    }


def _header(version: str = "8") -> dict[str, object]:
    return {
        "path": f"accounts/1/containers/2/versions/{version}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": version,
    }


def _version(version: str) -> dict[str, object]:
    return {
        "path": f"accounts/1/containers/2/versions/{version}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": version,
        "container": {},
        "tag": [],
        "trigger": [],
        "variable": [],
        "folder": [],
        "builtInVariable": [],
        "zone": [],
        "customTemplate": [],
        "client": [],
        "gtagConfig": [],
        "transformation": [],
    }


def _workspace() -> dict[str, object]:
    return {
        "path": "accounts/1/containers/2/workspaces/9",
        "accountId": "1",
        "containerId": "2",
        "workspaceId": "9",
    }


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "gtm-read",
        "provider": "gtm",
        "account_ref": "1",
        "principal_ref": "principal.fixture",
        "client_ref": "client.fixture",
        "scopes": [_READONLY_SCOPE],
        "purpose": "READ",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": "GTM_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _identity_client(
    handler: Callable[[httpx.Request], httpx.Response],
    token_getter: Callable[[], str],
    **changes: object,
) -> tuple[GtmIdentityClient, SafeHttpClient]:
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {_HOST},
        read_attempts=1,
    )
    return GtmIdentityClient(transport, token_getter, **changes), transport


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("maximum_response_bytes", 8_388_609),
        ("read_attempts", 4),
        ("allowed_hosts", None),
    ],
)
def test_mutated_transport_cannot_expand_a_new_identity_read_or_preserve_stale_proof(
    attribute: str, value: object
) -> None:
    requests: list[str] = []
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json=_container())

    def token_getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return "fixture-token"

    client, transport = _identity_client(handler, token_getter)
    prior = client.discover_identity("1", "2").identity
    assert client.is_current_identity(prior) is True
    assert requests == ["/tagmanager/v2/accounts/1", "/tagmanager/v2/accounts/1/containers/2"]
    assert token_calls == 1

    setattr(transport, attribute, value)

    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("1", "2")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert client.is_current_identity(prior) is False
    assert requests == ["/tagmanager/v2/accounts/1", "/tagmanager/v2/accounts/1/containers/2"]
    assert token_calls == 1


def test_identity_reference_binding_fails_before_token_or_http() -> None:
    token_calls = 0
    request_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_calls
        request_calls += 1
        raise AssertionError("local reference binding reached HTTP")

    def token_getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return "fixture-token"

    def invalid_reference(_: str, __: str) -> str:
        return "invalid reference with whitespace"

    client, _ = _identity_client(
        handler,
        token_getter,
        reference_for=invalid_reference,
    )

    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("1", "2")

    assert raised.value.code == "IDENTITY_BINDING_INVALID"
    assert token_calls == 0
    assert request_calls == 0


def test_callback_failures_are_redacted_and_stop_before_http() -> None:
    request_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_calls
        request_calls += 1
        raise AssertionError("callback failure reached HTTP")

    def failing_token_getter() -> str:
        raise RuntimeError(_PRIVATE_MARKER)

    token_client, _ = _identity_client(handler, failing_token_getter)
    with pytest.raises(AdhelmError) as token_error:
        token_client.discover_identity("1", "2")
    assert token_error.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert _PRIVATE_MARKER not in str(token_error.value)
    assert token_error.value.__cause__ is None
    assert token_error.value.__suppress_context__ is True

    def failing_reference(_: str, __: str) -> str:
        raise RuntimeError(_PRIVATE_MARKER)

    reference_client, _ = _identity_client(
        handler,
        lambda: "fixture-token",
        reference_for=failing_reference,
    )
    with pytest.raises(AdhelmError) as reference_error:
        reference_client.discover_identity("1", "2")
    assert reference_error.value.code == "IDENTITY_BINDING_INVALID"
    assert _PRIVATE_MARKER not in str(reference_error.value)
    assert reference_error.value.__cause__ is None
    assert reference_error.value.__suppress_context__ is True
    assert request_calls == 0


def test_credential_failure_is_redacted_and_stops_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    client_calls = 0

    def client_factory(*, trust_env: bool) -> httpx.Client:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("credential failure reached HTTP construction")

    def failing_credential(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError(_PRIVATE_MARKER)

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)
    monkeypatch.setattr(workflow, "credential", failing_credential)

    with pytest.raises(AdhelmError) as raised:
        workflow.read_gtm(
            tmp_path,
            "gtm-read",
            container_id="2",
            fingerprint_key=_FINGERPRINT_KEY,
        )

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert _PRIVATE_MARKER not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert client_calls == 0


def test_invalid_public_refresh_input_clears_a_prior_identity_before_token_or_http() -> None:
    requests: list[str] = []
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/accounts/1"):
            return httpx.Response(200, json=_account())
        return httpx.Response(200, json=_container())

    def token_getter() -> str:
        nonlocal token_calls
        token_calls += 1
        return "fixture-token"

    client, _ = _identity_client(handler, token_getter)
    prior = client.discover_identity("1", "2").identity

    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("1/../../9", "2")

    assert raised.value.code == "INVALID_INPUT"
    assert client.is_current_identity(prior) is False
    assert requests == ["/tagmanager/v2/accounts/1", "/tagmanager/v2/accounts/1/containers/2"]
    assert token_calls == 1


def test_workflow_scopes_all_parent_scoped_gtm_references_and_exposes_request_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tagmanager/v2/accounts/1":
            return httpx.Response(200, json=_account())
        if request.url.path == "/tagmanager/v2/accounts/1/containers/2":
            return httpx.Response(200, json=_container())
        if request.url.path.endswith("/version_headers:latest"):
            return httpx.Response(200, json=_header())
        if request.url.path.endswith("/versions:live"):
            return httpx.Response(200, json=_version("7"))
        if request.url.path.endswith("/versions/8"):
            return httpx.Response(200, json=_version("8"))
        if request.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"workspace": [_workspace()]})
        raise AssertionError(request.url.path)

    def client_factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: "fixture-token-not-for-output",
    )

    request = workflow.GtmReadRequest(profile_id="gtm-read", container_id="2")
    discovery, inventory = workflow.read_gtm(
        tmp_path,
        request.profile_id,
        container_id=request.container_id,
        fingerprint_key=_FINGERPRINT_KEY,
    )

    assert request.schema_version == 1
    assert discovery.identity.account_ref == "gtm.account.1"
    assert discovery.identity.container_ref == "gtm.container.1.2"
    assert inventory.identity is discovery.identity
    assert inventory.live.version_ref == "gtm.container_version.1.2.7"
    assert inventory.latest.version_ref == "gtm.container_version.1.2.8"
    assert inventory.workspaces[0].workspace_ref == "gtm.workspace.1.2.9"


@pytest.mark.parametrize("field", ["principal_ref", "client_ref"])
def test_model_copy_forged_profile_identity_fails_before_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    forged_profile = _profile().model_copy(update={field: "invalid profile identity"})
    forged_configuration = Configuration(profiles=[_profile()]).model_copy(
        update={"profiles": [forged_profile]}
    )
    client_calls = 0

    def client_factory(*, trust_env: bool) -> httpx.Client:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("invalid profile reached HTTP construction")

    monkeypatch.setattr(workflow, "load_configuration", lambda _: forged_configuration)
    monkeypatch.setattr(workflow.httpx, "Client", client_factory)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_gtm(
            tmp_path,
            "gtm-read",
            container_id="2",
            fingerprint_key=_FINGERPRINT_KEY,
        )

    assert raised.value.code == "CONFIGURATION_INVALID"
    assert client_calls == 0

"""End-to-end boundaries for the bounded GTM Web measurement read workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.gtm_measurement_workflow as workflow
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile

_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
_KEY = b"f" * 32


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "gtm-read",
        "provider": "gtm",
        "account_ref": "1",
        "principal_ref": "principal.fixture",
        "client_ref": "client.fixture",
        "scopes": [_SCOPE],
        "purpose": "READ",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": "GTM_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _configure(root: Path, *profiles: Profile) -> None:
    save_configuration(root, Configuration(profiles=list(profiles)))


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> Callable[[], int]:
    real_client = httpx.Client
    calls = 0

    def client_factory(*, trust_env: bool) -> httpx.Client:
        nonlocal calls
        calls += 1
        assert trust_env is False
        return real_client(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr("adhelm.gtm_measurement_workflow.httpx.Client", client_factory)
    return lambda: calls


def _account() -> dict[str, object]:
    return {
        "path": "accounts/1",
        "accountId": "1",
        "name": "customer account metadata must stay transient",
        "features": {"supportUserPermissions": True},
    }


def _container(*, account_id: str = "1", container_id: str = "2") -> dict[str, object]:
    return {
        "path": f"accounts/{account_id}/containers/{container_id}",
        "accountId": account_id,
        "containerId": container_id,
        "name": "customer container metadata must stay transient",
        "domainName": ["customer.example"],
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


def _install_credential(monkeypatch: pytest.MonkeyPatch, calls: list[Profile]) -> None:
    def fixture_credential(profile: Profile, *, ci_enabled: bool) -> str:
        calls.append(profile)
        assert ci_enabled is True
        return "fixture-token-not-for-output"

    monkeypatch.setattr(workflow, "credential", fixture_credential)


def test_read_proves_identity_then_uses_one_fixed_session_for_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    requests: list[httpx.Request] = []
    credential_calls: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "tagmanager.googleapis.com"
        assert request.headers["authorization"] == "Bearer fixture-token-not-for-output"
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
            assert request.url.params == httpx.QueryParams()
            return httpx.Response(200, json={"workspace": []})
        raise AssertionError(request.url.path)

    client_calls = _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, credential_calls)
    discovery, inventory = workflow.read_gtm(
        tmp_path,
        "gtm-read",
        container_id="2",
        fingerprint_key=_KEY,
        ci_enabled=True,
    )

    assert [request.url.path for request in requests] == [
        "/tagmanager/v2/accounts/1",
        "/tagmanager/v2/accounts/1/containers/2",
        "/tagmanager/v2/accounts/1/containers/2/version_headers:latest",
        "/tagmanager/v2/accounts/1/containers/2/versions:live",
        "/tagmanager/v2/accounts/1/containers/2/versions/8",
        "/tagmanager/v2/accounts/1/containers/2/workspaces",
        "/tagmanager/v2/accounts/1/containers/2/version_headers:latest",
        "/tagmanager/v2/accounts/1/containers/2/versions:live",
    ]
    assert client_calls() == 1
    assert len(credential_calls) == 1
    assert discovery.identity.account_ref == "gtm.account.1"
    assert discovery.identity.container_ref == "gtm.container.1.2"
    assert discovery.account_features.support_user_permissions == "TRUE"
    assert discovery.container_features.support_workspaces == "TRUE"
    assert inventory.identity is discovery.identity
    assert inventory.live.version_ref == "gtm.container_version.1.2.7"
    assert inventory.latest.version_ref == "gtm.container_version.1.2.8"
    assert inventory.staging_eligibility == "NOT_EVALUATED"
    assert inventory.observation_status == "SEQUENTIALLY_REREAD_NONATOMIC"
    encoded = json.dumps(
        {
            "discovery": discovery.model_dump(mode="json"),
            "inventory": inventory.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    assert "customer.example" not in encoded
    assert "customer container metadata" not in encoded
    assert "fixture-token-not-for-output" not in encoded


@pytest.mark.parametrize(
    ("profile", "expected_code"),
    [
        (_profile(provider="ga4"), "READ_PROFILE_REQUIRED"),
        (_profile(purpose="EDIT"), "READ_PROFILE_REQUIRED"),
        (_profile(scopes=[_SCOPE, "scope.extra"]), "READ_SCOPE_REQUIRED"),
        (_profile(account_ref="account.not.numeric"), "INVALID_GTM_ACCOUNT"),
    ],
)
def test_profile_provider_purpose_scope_and_account_fail_before_credential_or_http(
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
        workflow.read_gtm(tmp_path, "gtm-read", container_id="2", fingerprint_key=_KEY)
    assert raised.value.code == expected_code
    assert requests == []


def test_public_input_and_fingerprint_key_fail_before_configuration_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow,
        "load_configuration",
        lambda _root: (_ for _ in ()).throw(AssertionError("configuration was read")),
    )
    _install_transport(
        monkeypatch,
        lambda _request: (_ for _ in ()).throw(AssertionError("HTTP was reached")),
    )
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as invalid_container:
        workflow.read_gtm(tmp_path, "gtm-read", container_id="2/../../9", fingerprint_key=_KEY)
    assert invalid_container.value.code == "INVALID_WORKFLOW_INPUT"
    with pytest.raises(AdhelmError) as invalid_key:
        workflow.read_gtm(tmp_path, "gtm-read", container_id="2", fingerprint_key=b"short")
    assert invalid_key.value.code == "FINGERPRINT_KEY_REQUIRED"


def test_missing_optional_gtm_profile_is_explicit_and_preserves_unrelated_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = _profile(
        profile_id="ga4-read",
        provider="ga4",
        account_ref="property.fixture",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    _configure(tmp_path, unrelated)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credential was read")),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.validate_gtm_read(tmp_path, "gtm-read", container_id="2")
    assert raised.value.code == "NOT_CONFIGURED_OPTIONAL"
    assert load_configuration(tmp_path).profiles == [unrelated]


def test_identity_mismatch_stops_before_any_inventory_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    paths: list[str] = []
    credential_calls: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/tagmanager/v2/accounts/1":
            return httpx.Response(200, json=_account())
        if request.url.path == "/tagmanager/v2/accounts/1/containers/2":
            return httpx.Response(200, json=_container(account_id="9"))
        raise AssertionError("inventory escaped identity failure")

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, credential_calls)
    with pytest.raises(AdhelmError) as raised:
        workflow.read_gtm(
            tmp_path,
            "gtm-read",
            container_id="2",
            fingerprint_key=_KEY,
            ci_enabled=True,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert paths == [
        "/tagmanager/v2/accounts/1",
        "/tagmanager/v2/accounts/1/containers/2",
    ]
    assert len(credential_calls) == 1


def test_late_workspace_page_failure_returns_no_partial_measurement_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, _profile())
    paths: list[str] = []
    credential_calls: list[Profile] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
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
        if request.url.path.endswith("/workspaces") and request.url.params.get("pageToken") is None:
            return httpx.Response(200, json={"workspace": [], "nextPageToken": "next"})
        if request.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"unsupported": "late page"})
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, credential_calls)
    with pytest.raises(AdhelmError) as raised:
        workflow.read_gtm(
            tmp_path,
            "gtm-read",
            container_id="2",
            fingerprint_key=_KEY,
            ci_enabled=True,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert paths[-2:] == [
        "/tagmanager/v2/accounts/1/containers/2/workspaces",
        "/tagmanager/v2/accounts/1/containers/2/workspaces",
    ]
    assert len(credential_calls) == 1
    assert not (tmp_path / "ledger").exists()

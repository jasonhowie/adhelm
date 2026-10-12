from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.gtm_read import GtmReadClient, GtmReadIdentity
from adhelm.transport import SafeHttpClient

_KEY = b"r" * 32
_RAW_MARKER = "customer-condition-should-never-escape"


def _identity() -> GtmReadIdentity:
    return GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.2",
        container_client_ref="2",
    )


def _header(identifier: str = "8", *, include_deleted: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "path": f"accounts/1/containers/2/versions/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": identifier,
        "name": "customer version",
        "numTags": "0",
        "numTriggers": "0",
        "numVariables": "0",
        "numZones": "0",
        "numCustomTemplates": "0",
        "numClients": "0",
        "numGtagConfigs": "0",
        "numTransformations": "0",
    }
    if include_deleted:
        value["deleted"] = False
    return value


def _version(
    identifier: str,
    *,
    include_deleted: bool = True,
    container: dict[str, object] | None = None,
    tags: list[object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "path": f"accounts/1/containers/2/versions/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": identifier,
        "name": "customer version",
        "description": "customer description",
        "container": {"name": "customer container"} if container is None else container,
        "tag": [] if tags is None else tags,
        "trigger": [],
        "variable": [],
        "folder": [],
        "builtInVariable": [],
        "fingerprint": "provider-fingerprint",
        "tagManagerUrl": "https://tagmanager.google.com/customer",
        "zone": [],
        "customTemplate": [],
        "client": [],
        "gtagConfig": [],
        "transformation": [],
    }
    if include_deleted:
        value["deleted"] = False
    return value


def _workspace(identifier: str) -> dict[str, str]:
    return {
        "path": f"accounts/1/containers/2/workspaces/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "workspaceId": identifier,
        "name": "customer workspace",
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token_getter: Callable[[], str] = lambda: "fixture-token",
    verifier: Callable[[GtmReadIdentity], bool] = lambda _: True,
    reference_for: Callable[[str, str], str] = lambda kind, value: f"gtm.{kind}.{value}",
) -> GtmReadClient:
    return GtmReadClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
            {"tagmanager.googleapis.com"},
            read_attempts=1,
        ),
        token_getter,
        verifier,
        fingerprint_key=_KEY,
        reference_for=reference_for,
    )


def _complete_handler(
    *,
    header: dict[str, object] | None = None,
    live: dict[str, object] | None = None,
    latest: dict[str, object] | None = None,
    workspaces: dict[str, object] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    header_value = _header() if header is None else header
    live_value = _version("7") if live is None else live
    latest_value = _version("8") if latest is None else latest
    workspaces_value = {"workspace": []} if workspaces is None else workspaces

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/version_headers:latest"):
            return httpx.Response(200, json=header_value)
        if path.endswith("/versions:live"):
            return httpx.Response(200, json=live_value)
        if path.endswith("/versions/8"):
            return httpx.Response(200, json=latest_value)
        if path.endswith("/workspaces"):
            return httpx.Response(200, json=workspaces_value)
        raise AssertionError(path)

    return handler


def test_deleted_false_defaults_are_accepted_and_live_latest_are_not_staging_proof() -> None:
    inventory = _client(
        _complete_handler(
            header=_header(include_deleted=False),
            live=_version("7", include_deleted=False),
            latest=_version("8", include_deleted=False),
        )
    ).inventory(identity=_identity())

    assert inventory.live.version_ref == "gtm.container_version.7"
    assert inventory.latest.version_ref == "gtm.container_version.8"
    assert inventory.live.version_ref != inventory.latest.version_ref
    assert inventory.staging_eligibility == "NOT_EVALUATED"
    assert inventory.observation_status == "SEQUENTIALLY_REREAD_NONATOMIC"


def test_forged_identity_native_ids_stop_before_verifier_token_or_http() -> None:
    calls: list[str] = []

    def fail_if_called(_: httpx.Request) -> httpx.Response:
        calls.append("http")
        raise AssertionError("identity must stop before HTTP")

    def token_getter() -> str:
        calls.append("token")
        return "fixture-token"

    def verifier(_: GtmReadIdentity) -> bool:
        calls.append("verifier")
        return True

    forged = _identity().model_copy(
        update={
            "account_client_ref": "1/../accounts/99",
            "account_ref": "gtm.account.1/../accounts/99",
            "container_client_ref": "2/../containers/88",
            "container_ref": "gtm.container.2/../containers/88",
        }
    )

    with pytest.raises(AdhelmError) as raised:
        _client(fail_if_called, token_getter=token_getter, verifier=verifier).inventory(
            identity=forged
        )

    assert raised.value.code == "IDENTITY_BINDING_INVALID"
    assert calls == []


def test_workspace_item_cap_stops_before_item_normalization_or_fingerprinting() -> None:
    reference_calls: list[str] = []

    def reference_for(kind: str, value: str) -> str:
        reference_calls.append(kind)
        return f"gtm.{kind}.{value}"

    workspaces = {"workspace": [_workspace(str(index)) for index in range(1, 1_002)]}
    with pytest.raises(AdhelmError) as raised:
        _client(_complete_handler(workspaces=workspaces), reference_for=reference_for).inventory(
            identity=_identity()
        )

    assert raised.value.code == "PAGINATION_LIMIT"
    assert "workspace" not in reference_calls


@pytest.mark.parametrize(
    ("container", "tags"),
    [
        ({"name": "customer container", "unexpected": _RAW_MARKER}, []),
        ({"usageContext": [{"raw": _RAW_MARKER}]}, []),
        (
            {"name": "customer container"},
            [
                {
                    "path": "accounts/99/containers/2/workspaces/3/tags/4",
                    "accountId": "99",
                    "containerId": "2",
                    "tagId": "4",
                    "type": "html",
                    "parameter": [{"value": _RAW_MARKER}],
                }
            ],
        ),
    ],
)
def test_malformed_nested_resources_fail_closed_without_raw_customer_content(
    container: dict[str, object], tags: list[object]
) -> None:
    with pytest.raises(AdhelmError) as raised:
        _client(_complete_handler(live=_version("7", container=container, tags=tags))).inventory(
            identity=_identity()
        )

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    rendered = str(raised.value)
    assert _RAW_MARKER not in rendered
    assert _RAW_MARKER not in json.dumps(
        {"code": raised.value.code, "message": raised.value.message}, sort_keys=True
    )

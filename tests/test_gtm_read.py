from __future__ import annotations

import json

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.gtm_read import GtmReadClient, GtmReadIdentity
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32


def _identity() -> GtmReadIdentity:
    return GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.2",
        container_client_ref="2",
    )


def _version(identifier: str, *, html: bool = False) -> dict[str, object]:
    tags: list[object] = []
    if html:
        tags.append({"tagId": "99", "type": "html", "parameter": [{"value": "secret code"}]})
    return {
        "path": f"accounts/1/containers/2/versions/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": identifier,
        "name": "customer version",
        "deleted": False,
        "description": "customer internal notes",
        "container": {"name": "customer container"},
        "tag": tags,
        "trigger": [],
        "variable": [],
        "folder": [],
        "builtInVariable": [],
        "fingerprint": "provider-value",
        "tagManagerUrl": "https://tagmanager.google.com/customer",
        "zone": [],
        "customTemplate": [{"templateData": "const email = 'person@example.test'"}],
        "client": [],
        "gtagConfig": [],
        "transformation": [],
    }


def _header(identifier: str = "8") -> dict[str, object]:
    return {
        "path": f"accounts/1/containers/2/versions/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": identifier,
        "name": "customer version",
        "numTags": "0",
        "numTriggers": "0",
        "deleted": False,
        "numVariables": "0",
        "numZones": "0",
        "numCustomTemplates": "1",
        "numClients": "0",
        "numGtagConfigs": "0",
        "numTransformations": "0",
    }


def _client(handler: httpx.MockTransport, *, verifier: object = lambda _: True) -> GtmReadClient:
    assert callable(verifier)
    return GtmReadClient(
        SafeHttpClient(
            httpx.Client(transport=handler, trust_env=False),
            {"tagmanager.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "fixture-token",
        verifier,
        fingerprint_key=_KEY,
    )


def test_fixed_inventory_reads_complete_versions_and_all_workspace_pages_without_raw_code() -> None:
    requests: list[httpx.Request] = []
    header_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal header_calls
        requests.append(request)
        path = request.url.path
        if path.endswith("/version_headers:latest"):
            header_calls += 1
            return httpx.Response(200, json=_header())
        if path.endswith("/versions:live"):
            return httpx.Response(200, json=_version("7", html=True))
        if path.endswith("/versions/8"):
            return httpx.Response(200, json=_version("8"))
        if path.endswith("/workspaces") and request.url.params.get("pageToken") is None:
            return httpx.Response(
                200, json={"workspace": [_workspace("3")], "nextPageToken": "next"}
            )
        if path.endswith("/workspaces"):
            return httpx.Response(200, json={"workspace": [_workspace("4")]})
        raise AssertionError(path)

    inventory = _client(httpx.MockTransport(handler)).inventory(identity=_identity())
    assert header_calls == 2
    assert [request.method for request in requests] == ["GET"] * 7
    assert inventory.live.version_ref == "gtm.container_version.7"
    assert inventory.latest.version_ref == "gtm.container_version.8"
    assert [workspace.workspace_ref for workspace in inventory.workspaces] == [
        "gtm.workspace.3",
        "gtm.workspace.4",
    ]
    assert inventory.live.resource_counts.tags == 1
    assert [item.kind for item in inventory.live.opaque_code] == [
        "CUSTOM_HTML_TAG",
        "CUSTOM_TEMPLATE",
    ]
    assert all(item.contents_status == "NOT_INSPECTED" for item in inventory.workspaces)
    encoded = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
    assert "person@example.test" not in encoded
    assert "secret code" not in encoded
    assert "customer internal notes" not in encoded
    assert inventory.observation_status == "SEQUENTIALLY_REREAD_NONATOMIC"


def _workspace(identifier: str) -> dict[str, str]:
    return {
        "path": f"accounts/1/containers/2/workspaces/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "workspaceId": identifier,
        "name": "customer workspace",
        "description": "internal work",
        "fingerprint": "provider-fingerprint",
        "tagManagerUrl": "https://tagmanager.google.com/customer",
    }


def test_unknown_fields_parent_mismatch_repeated_cursor_and_changing_views_fail_closed() -> None:
    def unknown(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version_headers:latest"):
            return httpx.Response(200, json={**_header(), "newField": True})
        raise AssertionError("must stop after unknown header field")

    with pytest.raises(AdhelmError, match="unsupported field"):
        _client(httpx.MockTransport(unknown)).inventory(identity=_identity())

    calls = 0

    def changing(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        path = request.url.path
        if path.endswith("/version_headers:latest"):
            calls += 1
            return httpx.Response(200, json=_header("8" if calls == 1 else "9"))
        if path.endswith("/versions:live"):
            return httpx.Response(200, json=_version("7"))
        if path.endswith("/versions/8"):
            return httpx.Response(200, json=_version("8"))
        if path.endswith("/workspaces"):
            return httpx.Response(200, json={"workspace": []})
        raise AssertionError(path)

    with pytest.raises(AdhelmError, match="changed during sequential"):
        _client(httpx.MockTransport(changing)).inventory(identity=_identity())

    def cursor(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/version_headers:latest"):
            return httpx.Response(200, json=_header())
        if path.endswith("/versions:live"):
            return httpx.Response(200, json=_version("7"))
        if path.endswith("/versions/8"):
            return httpx.Response(200, json=_version("8"))
        if path.endswith("/workspaces"):
            return httpx.Response(200, json={"workspace": [], "nextPageToken": "again"})
        raise AssertionError(path)

    with pytest.raises(AdhelmError, match="no safe progress"):
        _client(httpx.MockTransport(cursor)).inventory(identity=_identity())


def test_identity_and_transport_boundaries_stop_before_token_or_http() -> None:
    no_request = httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError("no HTTP")))
    client = _client(no_request, verifier=lambda _: False)
    with pytest.raises(AdhelmError, match="requires a verified"):
        client.inventory(identity=_identity())

    bad = _identity().model_copy(update={"container_ref": "gtm.container.9"})
    with pytest.raises(AdhelmError, match="do not bind"):
        _client(no_request).inventory(identity=bad)

    with pytest.raises(AdhelmError, match="fingerprint key"):
        GtmReadClient(
            SafeHttpClient(httpx.Client(transport=no_request), {"tagmanager.googleapis.com"}),
            lambda: "fixture-token",
            lambda _: True,
            fingerprint_key=b"bad",
        )


def test_custom_reference_binding_and_hmac_fingerprints_are_not_plain_hashes() -> None:
    def custom_reference(kind: str, value: str) -> str:
        return f"local.{kind}.{value}"

    identity = GtmReadIdentity(
        account_ref="local.account.1",
        account_client_ref="1",
        container_ref="local.container.2",
        container_client_ref="2",
    )
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/version_headers:latest"):
            return httpx.Response(200, json=_header())
        if request.url.path.endswith("/versions:live"):
            return httpx.Response(200, json=_version("7"))
        if request.url.path.endswith("/versions/8"):
            return httpx.Response(200, json=_version("8"))
        if request.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"workspace": []})
        raise AssertionError(request.url.path)

    reader = GtmReadClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            {"tagmanager.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "fixture-token",
        lambda _: True,
        fingerprint_key=_KEY,
        reference_for=custom_reference,
    )
    result = reader.inventory(identity=identity)
    assert result.latest.version_ref == "local.container_version.8"
    assert (
        result.latest.configuration_fingerprint
        != __import__("hashlib")
        .sha256(json.dumps(_version("8"), sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    assert paths

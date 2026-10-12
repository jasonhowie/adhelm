"""Fixture boundaries for the complete GTM Web fixed inventory (M01.1).

Every test uses `httpx.MockTransport`.  No credential, live container, or
provider request is involved, and no custom-code source may reach any output.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import pytest

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.gtm_inventory import GtmFullInventory, GtmInventoryClient
from adhelm.providers.gtm_read import GtmReadClient, GtmReadIdentity
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_REAL_HTTPX_CLIENT = httpx.Client
_SENTINEL = "ZZSENTINELCODEZZ"
_HTML_SOURCE = f"<script>window.x='{_SENTINEL}'</script>"
_JS_SOURCE = f"function(){{return '{_SENTINEL}';}}"
_TEMPLATE_SOURCE = f"___INFO___ {_SENTINEL} ___SANDBOXED_JS_FOR_WEB_TEMPLATE___"
_PARENT = "accounts/1/containers/2"
_WORKSPACE_COLLECTION_SEGMENTS = (
    "tags",
    "triggers",
    "variables",
    "folders",
    "built_in_variables",
    "templates",
    "zones",
    "clients",
    "transformations",
    "gtag_config",
)


def _identity() -> GtmReadIdentity:
    return GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.2",
        container_client_ref="2",
    )


def _reference_for(kind: str, value: str) -> str:
    return f"gtm.{kind}.{value}"


def _version(identifier: str) -> dict[str, Any]:
    return {
        "path": f"{_PARENT}/versions/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": identifier,
        "name": "customer version",
        "deleted": False,
        "container": {"name": "customer container"},
        "tag": [],
        "trigger": [],
        "variable": [],
        "folder": [],
        "builtInVariable": [],
        "fingerprint": "provider-value",
        "zone": [],
        "customTemplate": [],
        "client": [],
        "gtagConfig": [],
        "transformation": [],
    }


def _header(identifier: str = "8") -> dict[str, Any]:
    return {
        "path": f"{_PARENT}/versions/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "containerVersionId": identifier,
        "name": "customer version",
        "deleted": False,
    }


def _workspace(identifier: str) -> dict[str, Any]:
    return {
        "path": f"{_PARENT}/workspaces/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "workspaceId": identifier,
        "name": "customer workspace",
        "fingerprint": "provider-value",
    }


def _tag(identifier: str, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "path": f"{_PARENT}/workspaces/3/tags/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "workspaceId": "3",
        "tagId": identifier,
        "name": f"Tag {identifier}",
        "type": "gaawe",
        "fingerprint": "provider-value",
        "parameter": [],
    }
    value.update(changes)
    return value


def _resource(kind: str, segment: str, identifier: str, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "path": f"{_PARENT}/workspaces/3/{segment}/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "workspaceId": "3",
        f"{kind}Id": identifier,
        "name": f"{kind} {identifier}",
        "fingerprint": "provider-value",
    }
    value.update(changes)
    return value


def _default_workspace_items() -> dict[str, list[dict[str, Any]]]:
    """One representative resource per documented workspace collection."""

    return {
        "tags": [
            _tag(
                "10",
                type="html",
                parameter=[{"type": "template", "key": "html", "value": _HTML_SOURCE}],
            ),
            _tag(
                "11",
                firingTriggerId=["20"],
                blockingTriggerId=["21"],
                setupTag=[{"tagName": "Tag 10", "stopOnSetupFailure": True}],
                teardownTag=[{"tagName": "Missing Teardown", "stopTeardownOnFailure": False}],
                parentFolderId="30",
                paused=True,
                consentSettings={
                    "consentStatus": "needed",
                    "consentType": {
                        "type": "list",
                        "key": "consentType",
                        "list": [{"type": "template", "value": "ad_storage"}],
                    },
                },
                parameter=[
                    {"type": "template", "key": "eventName", "value": "purchase {{Page URL}}"},
                    {"type": "triggerReference", "key": "gtmOnSuccess", "value": "20"},
                    {"type": "tagReference", "key": "cleanup", "value": "Tag 10"},
                ],
            ),
        ],
        "triggers": [
            _resource("trigger", "triggers", "20", type="pageview"),
            _resource("trigger", "triggers", "21", type="click"),
        ],
        "variables": [
            _resource(
                "variable",
                "variables",
                "40",
                type="jsm",
                parameter=[{"type": "template", "key": "javascript", "value": _JS_SOURCE}],
            ),
            _resource("variable", "variables", "41", type="v", parentFolderId="30"),
        ],
        "folders": [_resource("folder", "folders", "30")],
        "built_in_variables": [
            {
                "path": f"{_PARENT}/workspaces/3/built_in_variables",
                "accountId": "1",
                "containerId": "2",
                "workspaceId": "3",
                "type": "pageUrl",
                "name": "Page URL",
            }
        ],
        "templates": [
            _resource("template", "templates", "50", templateData=_TEMPLATE_SOURCE),
        ],
        "zones": [
            _resource(
                "zone",
                "zones",
                "60",
                boundary={
                    "condition": [{"type": "equals"}],
                    "customEvaluationTriggerId": ["20"],
                },
                childContainer=[{"publicId": "GTM-CHILD", "nickname": "child"}],
                typeRestriction={"enable": True, "whitelistedTypeId": ["gaawe"]},
            )
        ],
        "clients": [_resource("client", "clients", "70", type="gaaw_client", priority=1)],
        "transformations": [_resource("transformation", "transformations", "80", type="redact")],
        "gtag_config": [
            {
                "path": f"{_PARENT}/workspaces/3/gtag_config/90",
                "accountId": "1",
                "containerId": "2",
                "workspaceId": "3",
                "gtagConfigId": "90",
                "type": "googtag",
                "fingerprint": "provider-value",
                "parameter": [],
            }
        ],
    }


class _Fixture:
    """A bounded fixture container with configurable collection responses."""

    def __init__(
        self,
        *,
        items: dict[str, list[dict[str, Any]]] | None = None,
        workspaces: list[str] | None = None,
        live_version: str = "8",
        latest_version: str = "8",
        destinations: list[dict[str, Any]] | None = None,
        environments: list[dict[str, Any]] | None = None,
        workspace_changes: list[dict[str, Any]] | None = None,
        paged: dict[str, list[list[dict[str, Any]]]] | None = None,
        endless: set[str] | None = None,
        destination_next_token: bool = False,
        status: dict[str, int] | None = None,
    ) -> None:
        self.items = _default_workspace_items() if items is None else items
        self.workspaces = ["3"] if workspaces is None else workspaces
        self.live_version = live_version
        self.latest_version = latest_version
        self.destinations = destinations if destinations is not None else []
        self.environments = environments if environments is not None else []
        self.workspace_changes = workspace_changes if workspace_changes is not None else []
        self.paged = paged or {}
        self.endless = endless or set()
        self.destination_next_token = destination_next_token
        self.status = status or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/tagmanager/v2/")
        for marker, code in self.status.items():
            if path.endswith(marker):
                return httpx.Response(code, json={"error": "provider detail must not leak"})
        if path.endswith("/version_headers:latest"):
            return httpx.Response(200, json=_header(self.latest_version))
        if path.endswith("/versions:live"):
            return httpx.Response(200, json=_version(self.live_version))
        if path == f"{_PARENT}/versions/{self.latest_version}":
            return httpx.Response(200, json=_version(self.latest_version))
        if path == f"{_PARENT}/workspaces":
            return httpx.Response(
                200, json={"workspace": [_workspace(item) for item in self.workspaces]}
            )
        if path == f"{_PARENT}/destinations":
            payload: dict[str, Any] = {"destination": self.destinations}
            if self.destination_next_token:
                payload["nextPageToken"] = "more"
            return httpx.Response(200, json=payload)
        if path == f"{_PARENT}/environments":
            return httpx.Response(200, json={"environment": self.environments})
        if path.endswith("/status"):
            return httpx.Response(200, json={"workspaceChange": self.workspace_changes})
        for segment in _WORKSPACE_COLLECTION_SEGMENTS:
            if not path.endswith(f"/{segment}"):
                continue
            field = _RESPONSE_FIELD[segment]
            if segment in self.endless:
                return httpx.Response(
                    200, json={field: [], "nextPageToken": f"page-{len(self.requests)}"}
                )
            if segment in self.paged:
                pages = self.paged[segment]
                token = request.url.params.get("pageToken")
                index = 0 if token is None else int(token)
                body: dict[str, Any] = {field: pages[index]}
                if index + 1 < len(pages):
                    body["nextPageToken"] = str(index + 1)
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={field: self.items.get(segment, [])})
        raise AssertionError(path)


_RESPONSE_FIELD = {
    "tags": "tag",
    "triggers": "trigger",
    "variables": "variable",
    "folders": "folder",
    "built_in_variables": "builtInVariable",
    "templates": "template",
    "zones": "zone",
    "clients": "client",
    "transformations": "transformation",
    "gtag_config": "gtagConfig",
}


def _read(fixture: _Fixture, *, now: Callable[[], datetime] | None = None) -> GtmFullInventory:
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(fixture.handler), trust_env=False),
        {"tagmanager.googleapis.com"},
        read_attempts=1,
    )
    reader = GtmReadClient(
        transport,
        lambda: "fixture-token",
        lambda _: True,
        fingerprint_key=_KEY,
        reference_for=_reference_for,  # type: ignore[arg-type]
    )
    client = GtmInventoryClient(
        transport,
        lambda: "fixture-token",
        reader,
        fingerprint_key=_KEY,
        reference_for=_reference_for,  # type: ignore[arg-type]
        now=now or (lambda: datetime(2026, 9, 9, tzinfo=UTC)),
    )
    return client.full_inventory(identity=_identity())


def _emitted(inventory: GtmFullInventory) -> str:
    return json.dumps(inventory.model_dump(mode="json"), sort_keys=True)


def _coverage(
    inventory: GtmFullInventory, scope_kind: str, collection: str
) -> list[tuple[str, int, int]]:
    return [
        (item.status, item.pages_read, item.resource_count)
        for item in inventory.coverage
        if item.scope_kind == scope_kind and item.collection == collection
    ]


def test_full_inventory_consumes_every_documented_collection_and_all_pages() -> None:
    fixture = _Fixture(
        paged={
            "tags": [
                [_tag("10", type="html", parameter=[])],
                [_tag("11")],
                [_tag("12")],
            ]
        }
    )
    inventory = _read(fixture)

    read_paths = [request.url.path.removeprefix("/tagmanager/v2/") for request in fixture.requests]
    for segment in _WORKSPACE_COLLECTION_SEGMENTS:
        assert f"{_PARENT}/workspaces/3/{segment}" in read_paths
    assert f"{_PARENT}/destinations" in read_paths
    assert [request.method for request in fixture.requests] == ["GET"] * len(fixture.requests)

    assert _coverage(inventory, "WORKSPACE", "tags") == [("COMPLETE", 3, 3)]
    tag_tokens = [
        request.url.params.get("pageToken")
        for request in fixture.requests
        if request.url.path.endswith("/workspaces/3/tags")
    ]
    assert tag_tokens == [None, "1", "2"]
    workspace_statuses = {
        item.status
        for item in inventory.coverage
        if item.scope_kind == "WORKSPACE"
        and item.collection not in ("destinations", "environments")
    }
    assert workspace_statuses == {"COMPLETE"}
    # Destinations and environments are documented only at container scope for
    # API v2; each is statused at the other scopes rather than left absent.
    assert _coverage(inventory, "WORKSPACE", "destinations") == [("NOT_DOCUMENTED", 0, 0)]
    assert _coverage(inventory, "WORKSPACE", "environments") == [("NOT_DOCUMENTED", 0, 0)]
    assert _coverage(inventory, "CONTAINER", "destinations") == [("COMPLETE", 1, 0)]
    assert _coverage(inventory, "CONTAINER", "environments") == [("COMPLETE", 1, 0)]
    # `workspaces.getStatus` is documented and read, so pending changes are a
    # recorded surface rather than an unstated omission.
    assert f"{_PARENT}/workspaces/3/status" in read_paths
    assert _coverage(inventory, "WORKSPACE", "workspace_changes") == [("COMPLETE", 1, 0)]
    assert _coverage(inventory, "CONTAINER", "workspace_changes") == [("NOT_DOCUMENTED", 0, 0)]
    assert inventory.counts.workspace_pending_changes == 0
    # The reference-edge bound is statused on every scope, never left implied.
    assert inventory.reference_coverage == "COMPLETE_WITHIN_DECLARED_BOUNDS"
    assert all(
        item.status == "COMPLETE"
        for item in inventory.coverage
        if item.collection == "reference_edges"
    )
    assert inventory.counts.workspaces_listed == 1
    assert inventory.counts.workspaces_inspected == 1
    assert inventory.counts.resources == len(inventory.resources)
    assert sum(item.collection == "tags" for item in inventory.resources) == 3
    # The six fixed base reads (two headers, two live, latest, workspaces) are
    # made by the existing reader; the inventory client counts only its own.
    assert inventory.counts.requests == len(fixture.requests) - 6


def test_every_documented_workspace_collection_produces_normalized_records() -> None:
    inventory = _read(_Fixture())
    kinds = sorted({resource.resource_kind for resource in inventory.resources})
    assert kinds == [
        "BUILT_IN_VARIABLE",
        "CLIENT",
        "FOLDER",
        "GTAG_CONFIG",
        "TAG",
        "TEMPLATE",
        "TRANSFORMATION",
        "TRIGGER",
        "VARIABLE",
        "ZONE",
    ]
    tag = next(item for item in inventory.resources if item.resource_client_ref == "11")
    assert tag.resource_ref == "gtm.workspace.3/tags/11"
    assert tag.scope_kind == "WORKSPACE"
    assert tag.native_path == f"{_PARENT}/workspaces/3/tags/11"
    assert tag.provider_type == "gaawe"
    assert tag.name == "Tag 11" and tag.name_status == "PRESENT"
    assert tag.parent_folder_ref == "gtm.workspace.3/folders/30"
    assert tag.paused is True
    assert tag.provider_fingerprint_digest is not None
    built_in = next(
        item for item in inventory.resources if item.resource_kind == "BUILT_IN_VARIABLE"
    )
    assert built_in.resource_client_ref == "pageUrl"
    assert [marker.classification for marker in built_in.code_markers] == ["BUILT_IN"]


def test_page_bound_exceeded_reports_incomplete_with_counts_and_never_complete() -> None:
    inventory = _read(_Fixture(endless={"triggers"}))
    assert _coverage(inventory, "WORKSPACE", "triggers") == [("INCOMPLETE_PAGE_BOUND", 20, 0)]
    assert inventory.completeness.status == "INCOMPLETE"
    assert inventory.completeness.incomplete_collections >= 1
    assert inventory.counts.incomplete_collections >= 1
    assert all(
        item.status != "COMPLETE"
        for item in inventory.coverage
        if item.collection == "triggers" and item.scope_kind == "WORKSPACE"
    )


def test_item_bound_exceeded_reports_incomplete_rather_than_a_truncated_complete() -> None:
    pages = [[_tag(str(4_000 + index)) for index in range(2_500)] for _ in range(2)]
    inventory = _read(_Fixture(paged={"tags": pages}))
    status, _, count = _coverage(inventory, "WORKSPACE", "tags")[0]
    assert status == "INCOMPLETE_ITEM_BOUND"
    assert count == 2_500
    assert inventory.completeness.status == "INCOMPLETE"


def test_container_destinations_are_bounded_without_a_documented_page_token() -> None:
    destination = {
        "path": f"{_PARENT}/destinations/1000",
        "accountId": "1",
        "containerId": "2",
        "destinationLinkId": "1000",
        "destinationId": "G-ABC",
        "name": "Destination 1000",
        "fingerprint": "provider-value",
    }
    inventory = _read(_Fixture(destinations=[destination], destination_next_token=True))
    assert _coverage(inventory, "CONTAINER", "destinations") == [
        ("INCOMPLETE_UNDOCUMENTED_PAGE_TOKEN", 1, 1)
    ]
    assert inventory.completeness.status == "INCOMPLETE"
    record = next(item for item in inventory.resources if item.resource_kind == "DESTINATION")
    assert record.scope_kind == "CONTAINER"
    assert record.resource_ref == "gtm.container.2/destinations/1000"
    destination_requests = [
        request for request in _Fixture().requests if request.url.path.endswith("/destinations")
    ]
    assert destination_requests == []


def test_custom_code_source_is_classified_and_hashed_but_never_emitted() -> None:
    inventory = _read(_Fixture())
    emitted = _emitted(inventory)
    assert _SENTINEL not in emitted
    assert _SENTINEL not in canonical_bytes(inventory).decode()
    for source in (_HTML_SOURCE, _JS_SOURCE, _TEMPLATE_SOURCE):
        assert source not in emitted
        assert "<script" not in emitted and "window.x" not in emitted
        assert source[:12] not in emitted
    for resource in inventory.resources:
        for marker in resource.code_markers:
            assert _SENTINEL not in json.dumps(marker.model_dump(mode="json"))

    html_tag = next(item for item in inventory.resources if item.resource_client_ref == "10")
    assert [marker.classification for marker in html_tag.code_markers] == ["CUSTOM_HTML"]
    assert html_tag.code_markers[0].parameter_key == "html"
    assert html_tag.code_markers[0].source_bytes == len(_HTML_SOURCE.encode())
    assert html_tag.code_markers[0].source_status == "DIGESTED_NEVER_PERSISTED"

    js_variable = next(item for item in inventory.resources if item.resource_client_ref == "40")
    assert [marker.classification for marker in js_variable.code_markers] == ["CUSTOM_JAVASCRIPT"]
    assert js_variable.code_markers[0].source_bytes == len(_JS_SOURCE.encode())

    template = next(item for item in inventory.resources if item.resource_kind == "TEMPLATE")
    assert [marker.classification for marker in template.code_markers] == ["CUSTOM_TEMPLATE"]
    assert template.code_markers[0].parameter_key == "templateData"
    assert inventory.custom_code_source == "CLASSIFIED_AND_HASHED_NEVER_PERSISTED"


def test_template_instance_and_script_bearing_parameters_are_classified() -> None:
    items = _default_workspace_items()
    items["tags"] = [
        _tag(
            "12",
            type="cvt_2_50",
            parameter=[{"type": "template", "key": "payload", "value": f"<script>{_SENTINEL}"}],
        )
    ]
    inventory = _read(_Fixture(items=items))
    assert _SENTINEL not in _emitted(inventory)
    instance = next(item for item in inventory.resources if item.resource_client_ref == "12")
    assert [marker.classification for marker in instance.code_markers] == [
        "TEMPLATE_INSTANCE",
        "CUSTOM_HTML",
    ]
    edge = next(item for item in inventory.edges if item.edge_kind == "TEMPLATE_INSTANCE")
    assert edge.target_ref == "gtm.workspace.3/templates/50"
    assert edge.resolution == "RESOLVED"


def test_documented_reference_edges_resolve_or_stay_explicitly_unresolved() -> None:
    inventory = _read(_Fixture())
    edges = {
        (edge.source_ref, edge.edge_kind, edge.selector): (edge.resolution, edge.target_ref)
        for edge in inventory.edges
        if edge.scope_kind == "WORKSPACE"
    }
    tag = "gtm.workspace.3/tags/11"
    assert edges[(tag, "FIRING_TRIGGER", "20")] == ("RESOLVED", "gtm.workspace.3/triggers/20")
    assert edges[(tag, "BLOCKING_TRIGGER", "21")] == ("RESOLVED", "gtm.workspace.3/triggers/21")
    assert edges[(tag, "PARENT_FOLDER", "30")] == ("RESOLVED", "gtm.workspace.3/folders/30")
    assert edges[(tag, "SETUP_TAG", "Tag 10")] == ("RESOLVED", "gtm.workspace.3/tags/10")
    # A name that matched no read resource is withheld: the only text an edge
    # publishes is a name this read actually saw.
    assert edges[(tag, "TEARDOWN_TAG", None)] == ("UNRESOLVED_NO_MATCHING_RESOURCE", None)
    teardown = next(item for item in inventory.edges if item.edge_kind == "TEARDOWN_TAG")
    assert teardown.selector_status == "WITHHELD_UNRESOLVED_REFERENCE"
    assert teardown.selector_digest is not None
    assert edges[(tag, "PARAMETER_TRIGGER_REFERENCE", "20")] == (
        "RESOLVED",
        "gtm.workspace.3/triggers/20",
    )
    assert edges[(tag, "PARAMETER_TAG_REFERENCE", "Tag 10")] == (
        "RESOLVED",
        "gtm.workspace.3/tags/10",
    )
    # A `{{ }}` name resolves against read variables including built-ins.
    assert edges[(tag, "VARIABLE_REFERENCE", "Page URL")] == (
        "RESOLVED",
        "gtm.workspace.3/built_in_variables/pageUrl",
    )
    assert inventory.dependency_graph == "RAW_EDGES_ONLY_NOT_A_GRAPH"
    assert inventory.completeness.dependency_proof == "NOT_COMPUTED_REQUIRES_DEPENDENCY_ENGINE"
    assert inventory.completeness.deletion_authority == "NEVER_ESTABLISHED_BY_INVENTORY"


def test_variable_reference_parsing_handles_nested_malformed_and_oversize_values() -> None:
    items = _default_workspace_items()
    items["tags"] = [
        _tag(
            "13",
            parameter=[
                {"type": "template", "key": "a", "value": "{{outer{{Page URL}}tail}}"},
                {"type": "template", "key": "b", "value": "{{unclosed"},
                {"type": "template", "key": "c", "value": "x" * 70_000 + "{{Page URL}}"},
                {"type": "template", "key": "d", "value": "{{Absent Variable}}"},
            ],
        )
    ]
    inventory = _read(_Fixture(items=items))
    source = "gtm.workspace.3/tags/13"
    edges = [edge for edge in inventory.edges if edge.source_ref == source]
    by_selector = {edge.selector: edge for edge in edges}
    # Only the brace-free innermost reference is treated as a reference.
    assert by_selector["Page URL"].resolution == "RESOLVED"
    assert "outer{{Page URL" not in by_selector
    # A reference that names no read resource keeps only its digest: it may be
    # a fragment of custom code rather than a variable name.
    absent = [
        edge
        for edge in edges
        if edge.resolution == "UNRESOLVED_NO_MATCHING_RESOURCE"
        and edge.selector_status == "WITHHELD_UNRESOLVED_REFERENCE"
    ]
    assert len(absent) == 1 and absent[0].selector is None
    assert "Absent Variable" not in by_selector
    malformed = [edge for edge in edges if edge.resolution == "UNRESOLVED_MALFORMED"]
    assert malformed, "an unbalanced reference must stay explicitly unresolved"
    assert any(edge.selector_status == "NOT_SCANNED_OVERSIZE" for edge in edges)
    assert all(edge.selector is None or len(edge.selector) <= 256 for edge in edges)
    assert inventory.counts.unresolved_edges == sum(
        edge.resolution.startswith("UNRESOLVED_") for edge in inventory.edges
    )


def test_ambiguous_names_never_resolve_to_a_single_target() -> None:
    items = _default_workspace_items()
    items["variables"] = [
        _resource("variable", "variables", "40", type="v", name="Shared"),
        _resource("variable", "variables", "41", type="v", name="Shared"),
    ]
    items["tags"] = [
        _tag("14", parameter=[{"type": "template", "key": "a", "value": "{{Shared}}"}])
    ]
    inventory = _read(_Fixture(items=items))
    edge = next(
        item
        for item in inventory.edges
        if item.source_ref == "gtm.workspace.3/tags/14" and item.edge_kind == "VARIABLE_REFERENCE"
    )
    assert edge.resolution == "UNRESOLVED_AMBIGUOUS_NAME"
    assert edge.target_ref is None
    # Ambiguous is not resolved, so the selector text is withheld as well.
    assert edge.selector is None
    assert edge.selector_status == "WITHHELD_UNRESOLVED_REFERENCE"
    assert edge.selector_digest is not None


def test_consent_settings_and_paused_state_are_captured_as_documented() -> None:
    inventory = _read(_Fixture())
    tag = next(item for item in inventory.resources if item.resource_client_ref == "11")
    assert tag.consent is not None
    assert tag.consent.consent_status == "needed"
    assert tag.consent.consent_types == ("ad_storage",)
    assert tag.consent.consent_type_status == "PRESENT"
    assert tag.paused is True
    untagged = next(item for item in inventory.resources if item.resource_client_ref == "10")
    assert untagged.consent is None and untagged.paused is None


def test_undocumented_consent_status_is_labelled_rather_than_normalized() -> None:
    items = _default_workspace_items()
    items["tags"] = [_tag("15", consentSettings={"consentStatus": "somethingNew"})]
    inventory = _read(_Fixture(items=items))
    tag = next(item for item in inventory.resources if item.resource_client_ref == "15")
    assert tag.consent is not None
    assert tag.consent.consent_status == "UNDOCUMENTED_VALUE"


def test_zone_boundary_and_zone_edges_are_recorded() -> None:
    inventory = _read(_Fixture())
    zone = next(item for item in inventory.resources if item.resource_kind == "ZONE")
    assert zone.zone_boundary is not None
    assert zone.zone_boundary.condition_count == 1
    assert zone.zone_boundary.custom_evaluation_trigger_count == 1
    assert zone.zone_boundary.child_container_count == 1
    assert zone.zone_boundary.type_restriction_enabled is True
    assert zone.zone_boundary.allowed_type_count == 1
    zone_edges = {
        (edge.edge_kind, edge.selector): edge.resolution
        for edge in inventory.edges
        if edge.source_ref == zone.resource_ref
    }
    assert zone_edges[("ZONE_TRIGGER", "20")] == "RESOLVED"
    # A child container is a separate container that this read never inspects.
    # That is scope, not a bounded-read gap, so it has its own status and never
    # counts as an unresolved edge: a zoned container must not be permanently
    # INCOMPLETE for a reason this inventory is not trying to cover.
    assert zone_edges[("ZONE_CHILD_CONTAINER", "GTM-CHILD")] == "OUT_OF_SCOPE_CHILD_CONTAINER"
    assert inventory.counts.out_of_scope_edges == 1
    assert all(
        edge.resolution != "OUT_OF_SCOPE_CHILD_CONTAINER"
        for edge in inventory.edges
        if edge.edge_kind != "ZONE_CHILD_CONTAINER"
    )


def test_live_and_latest_versions_report_their_equality_status() -> None:
    equal = _read(_Fixture(live_version="8", latest_version="8"))
    assert equal.live_latest.status == "LIVE_EQUALS_LATEST"
    assert equal.live_latest.live_version_ref == equal.live_latest.latest_version_ref
    assert equal.live_latest.staging_eligibility == "NOT_EVALUATED"

    differing = _read(_Fixture(live_version="7", latest_version="8"))
    assert differing.live_latest.status == "LIVE_DIFFERS_FROM_LATEST"
    assert differing.live_latest.live_version_ref == "gtm.container_version.7"
    assert differing.live_latest.latest_version_ref == "gtm.container_version.8"
    assert (
        differing.live_latest.live_configuration_fingerprint
        != differing.live_latest.latest_configuration_fingerprint
    )


def test_version_scopes_are_inventoried_alongside_workspaces() -> None:
    inventory = _read(_Fixture(live_version="7", latest_version="8"))
    scopes = {resource.scope_kind for resource in inventory.resources}
    assert scopes == {"WORKSPACE"}
    live_coverage = [item for item in inventory.coverage if item.scope_kind == "LIVE_VERSION"]
    latest_coverage = [item for item in inventory.coverage if item.scope_kind == "LATEST_VERSION"]
    # Ten embedded collections, the three collections documented only at another
    # scope (destinations, environments, workspace pending changes), and the
    # scope's reference-edge bound.
    assert len(live_coverage) == 14 and len(latest_coverage) == 14
    assert {item.status for item in live_coverage} == {"COMPLETE", "NOT_DOCUMENTED"}


def test_version_embedded_resources_are_normalized_from_the_version_body() -> None:
    fixture = _Fixture()
    original = fixture.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/versions:live"):
            body = json.loads(response.content)
            body["tag"] = [
                {
                    "path": f"{_PARENT}/versions/8/tags/10",
                    "accountId": "1",
                    "containerId": "2",
                    "tagId": "10",
                    "name": "Live Tag",
                    "type": "html",
                    "parameter": [{"type": "template", "key": "html", "value": _HTML_SOURCE}],
                }
            ]
            return httpx.Response(200, json=body)
        return response

    fixture.handler = handler  # type: ignore[method-assign]
    inventory = _read(fixture)
    live = [item for item in inventory.resources if item.scope_kind == "LIVE_VERSION"]
    assert [item.resource_ref for item in live] == ["gtm.container_version.8/tags/10"]
    assert [marker.classification for marker in live[0].code_markers] == ["CUSTOM_HTML"]
    assert _SENTINEL not in _emitted(inventory)


def test_uninspected_workspaces_are_reported_and_never_silently_dropped() -> None:
    inventory = _read(_Fixture(workspaces=[str(index) for index in range(100, 130)]))
    assert inventory.counts.workspaces_listed == 30
    assert inventory.counts.workspaces_inspected == 25
    assert inventory.completeness.uninspected_workspaces == 5
    assert inventory.completeness.status == "INCOMPLETE"
    bounded = [item for item in inventory.coverage if item.status == "INCOMPLETE_WORKSPACE_BOUND"]
    # The ten collections plus the workspace's pending changes, which the
    # inspection bound leaves unread just as explicitly.
    assert len(bounded) == 5 * 11
    assert {item.collection for item in bounded} >= {"workspace_changes"}


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "GTM_AUTHORIZATION_REJECTED"), (403, "GTM_ACCESS_NOT_PERMITTED")],
)
def test_denied_access_is_typed_as_authorization(status: int, code: str) -> None:
    fixture = _Fixture(status={"/tags": status})
    with pytest.raises(AdhelmError) as denied:
        _read(fixture)
    assert denied.value.code == code
    assert denied.value.exit_code == 4
    assert denied.value.status_code == status
    assert "provider detail" not in denied.value.message + denied.value.next_action
    # The read stops at the refused request; nothing is sent after it.
    assert fixture.requests[-1].url.path.endswith("/tags")


def test_throttling_and_outages_stay_precondition_failures() -> None:
    with pytest.raises(AdhelmError) as throttled:
        _read(_Fixture(status={"/tags": 429}))
    assert throttled.value.code == "PROVIDER_REQUEST_FAILED"
    assert throttled.value.exit_code == 3

    with pytest.raises(AdhelmError) as unavailable:
        _read(_Fixture(status={"/tags": 503}))
    assert unavailable.value.exit_code == 3


def test_oversized_provider_response_fails_closed_through_the_shared_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * 9_000_000 + b"}")

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"tagmanager.googleapis.com"},
        read_attempts=1,
        maximum_response_bytes=1_024,
    )
    reader = GtmReadClient(
        transport,
        lambda: "fixture-token",
        lambda _: True,
        fingerprint_key=_KEY,
        reference_for=_reference_for,  # type: ignore[arg-type]
    )
    client = GtmInventoryClient(
        transport,
        lambda: "fixture-token",
        reader,
        fingerprint_key=_KEY,
        reference_for=_reference_for,  # type: ignore[arg-type]
    )
    with pytest.raises(AdhelmError) as failure:
        client.full_inventory(identity=_identity())
    assert failure.value.code == "RESPONSE_TOO_LARGE"


def test_unsupported_collection_fields_and_repeated_tokens_fail_closed() -> None:
    def unknown_field(request: httpx.Request) -> httpx.Response:
        fixture = _Fixture()
        if request.url.path.endswith("/tags"):
            return httpx.Response(200, json={"tag": [], "unexpected": []})
        return fixture.handler(request)

    fixture = _Fixture()
    fixture.handler = unknown_field  # type: ignore[method-assign]
    with pytest.raises(AdhelmError) as failure:
        _read(fixture)
    assert failure.value.code == "PROVIDER_RESPONSE_INVALID"

    repeated = _Fixture()
    original = repeated.handler

    def repeat_token(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/triggers"):
            return httpx.Response(200, json={"trigger": [], "nextPageToken": "same"})
        return original(request)

    repeated.handler = repeat_token  # type: ignore[method-assign]
    with pytest.raises(AdhelmError) as loop_failure:
        _read(repeated)
    assert loop_failure.value.code == "PROVIDER_RESPONSE_INVALID"


def test_sensitive_or_oversize_names_are_withheld_and_only_digested() -> None:
    items = _default_workspace_items()
    items["tags"] = [
        _tag("16", name="contact person@customer.example"),
        _tag("17", name="n" * 400),
    ]
    inventory = _read(_Fixture(items=items))
    emitted = _emitted(inventory)
    assert "person@customer.example" not in emitted
    sensitive = next(item for item in inventory.resources if item.resource_client_ref == "16")
    assert sensitive.name is None and sensitive.name_status == "WITHHELD_SENSITIVE"
    assert sensitive.name_digest is not None
    oversize = next(item for item in inventory.resources if item.resource_client_ref == "17")
    assert oversize.name is None and oversize.name_status == "WITHHELD_OVERSIZE"


def test_every_persisted_record_passes_the_shared_privacy_validator() -> None:
    from adhelm.privacy import validate_data

    inventory = _read(_Fixture())
    validate_data(inventory.model_dump(mode="json"))
    for resource in inventory.resources:
        validate_data(resource.model_dump(mode="json"))
    for edge in inventory.edges:
        validate_data(edge.model_dump(mode="json"))
    for item in inventory.coverage:
        validate_data(item.model_dump(mode="json"))


def test_repeated_reads_of_one_fixture_are_deterministic() -> None:
    first = _read(_Fixture())
    second = _read(_Fixture())
    assert canonical_bytes(first) == canonical_bytes(second)


def test_counts_and_completeness_cannot_disagree_with_their_records() -> None:
    inventory = _read(_Fixture())
    payload = inventory.model_dump(mode="json")
    payload["counts"]["resources"] = inventory.counts.resources + 1
    with pytest.raises(ValueError):
        GtmFullInventory.model_validate(payload)

    complete = inventory.model_dump(mode="json")
    complete["completeness"]["status"] = "COMPLETE_WITHIN_DECLARED_BOUNDS"
    complete["completeness"]["unresolved_edges"] = 1
    with pytest.raises(ValueError):
        GtmFullInventory.model_validate(complete)


def test_inventory_never_claims_a_dependency_graph_or_ui_reconciliation() -> None:
    inventory = _read(_Fixture())
    assert inventory.observation_status == "SEQUENTIALLY_READ_NONATOMIC"
    assert inventory.ui_reconciliation == "NOT_PERFORMED"
    assert inventory.base.staging_eligibility == "NOT_EVALUATED"
    assert inventory.read_started_at <= inventory.read_finished_at
    assert inventory.read_started_at.tzinfo is not None


def test_scope_kind_literals_stay_closed() -> None:
    # Live and latest are one scope when they are one version; distinct versions
    # exercise every closed scope kind.
    values: set[Literal["LIVE_VERSION", "LATEST_VERSION", "WORKSPACE", "CONTAINER"]] = {
        item.scope_kind for item in _read(_Fixture(live_version="8", latest_version="9")).coverage
    }
    assert values == {"LIVE_VERSION", "LATEST_VERSION", "WORKSPACE", "CONTAINER"}
    same = {item.scope_kind for item in _read(_Fixture()).coverage}
    assert same == {"LIVE_VERSION", "WORKSPACE", "CONTAINER"}


def _workflow_profile() -> Any:
    from adhelm.models import Profile

    return Profile.model_validate(
        {
            "profile_id": "gtm-read",
            "provider": "gtm",
            "account_ref": "1",
            "principal_ref": "principal.fixture",
            "client_ref": "client.fixture",
            "scopes": ["https://www.googleapis.com/auth/tagmanager.readonly"],
            "purpose": "READ",
            "credential_source": "EXPLICIT_CI",
            "credential_ref": "GTM_FIXTURE_TOKEN",
        }
    )


def _install_workflow_fixture(
    monkeypatch: pytest.MonkeyPatch, root: Any, fixture: _Fixture
) -> None:
    import adhelm.gtm_measurement_workflow as workflow
    from adhelm.config import save_configuration
    from adhelm.models import Configuration, Profile

    save_configuration(root, Configuration(profiles=[_workflow_profile()]), replace=True)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/tagmanager/v2/accounts/1":
            fixture.requests.append(request)
            return httpx.Response(
                200, json={"path": "accounts/1", "accountId": "1", "name": "customer account"}
            )
        if path == "/tagmanager/v2/accounts/1/containers/2":
            fixture.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "path": _PARENT,
                    "accountId": "1",
                    "containerId": "2",
                    "name": "customer container",
                    "usageContext": ["web"],
                },
            )
        return fixture.handler(request)

    def client_factory(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), trust_env=trust_env)

    monkeypatch.setattr("adhelm.gtm_measurement_workflow.httpx.Client", client_factory)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda profile, *, ci_enabled: "fixture-token-not-for-output",
    )
    assert issubclass(Profile, object)


def test_existing_gtm_request_routes_byte_identically_to_the_fixed_read(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.gtm_measurement_workflow as workflow

    direct_fixture = _Fixture()
    _install_workflow_fixture(monkeypatch, tmp_path, direct_fixture)
    direct = workflow.read_gtm(
        tmp_path, "gtm-read", container_id="2", fingerprint_key=_KEY, ci_enabled=True
    )
    direct_paths = [request.url.path for request in direct_fixture.requests]

    routed_fixture = _Fixture()
    _install_workflow_fixture(monkeypatch, tmp_path, routed_fixture)
    discovery, inventory, full = workflow.read_gtm_selected(
        tmp_path, "gtm-read", container_id="2", fingerprint_key=_KEY, ci_enabled=True
    )
    routed_paths = [request.url.path for request in routed_fixture.requests]

    assert full is None
    assert routed_paths == direct_paths
    assert canonical_bytes(discovery) == canonical_bytes(direct[0])
    assert canonical_bytes(inventory) == canonical_bytes(direct[1])
    assert workflow.GtmReadRequest(profile_id="gtm-read", container_id="2").full_inventory is False


def test_full_inventory_request_field_selects_the_complete_read(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.gtm_measurement_workflow as workflow

    fixture = _Fixture()
    _install_workflow_fixture(monkeypatch, tmp_path, fixture)
    discovery, inventory, full = workflow.read_gtm_selected(
        tmp_path,
        "gtm-read",
        container_id="2",
        fingerprint_key=_KEY,
        full_inventory=True,
        ci_enabled=True,
    )
    assert full is not None
    assert inventory is full.base
    assert full.identity == discovery.identity
    assert full.counts.resources > 0
    assert _SENTINEL not in json.dumps(full.model_dump(mode="json"))
    paths = [request.url.path for request in fixture.requests]
    for segment in _WORKSPACE_COLLECTION_SEGMENTS:
        assert f"/tagmanager/v2/{_PARENT}/workspaces/3/{segment}" in paths
    assert f"/tagmanager/v2/{_PARENT}/destinations" in paths


def test_unconfigured_gtm_profile_stays_optional_for_the_full_read(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.gtm_measurement_workflow as workflow
    from adhelm.config import save_configuration
    from adhelm.models import Configuration

    save_configuration(tmp_path, Configuration(profiles=[]))
    with pytest.raises(AdhelmError) as failure:
        workflow.read_gtm_selected(
            tmp_path,
            "gtm-read",
            container_id="2",
            fingerprint_key=_KEY,
            full_inventory=True,
        )
    assert failure.value.code == "NOT_CONFIGURED_OPTIONAL"
    assert monkeypatch is not None


def test_custom_code_string_references_become_edges_without_leaking_surrounding_source() -> None:
    items = _default_workspace_items()
    embedded = f"<script>var u='{_SENTINEL}'+{{{{Page URL}}}}+'{_SENTINEL}';</script>"
    items["tags"] = [
        _tag(
            "18",
            type="html",
            parameter=[{"type": "template", "key": "html", "value": embedded}],
        )
    ]
    inventory = _read(_Fixture(items=items))
    emitted = _emitted(inventory)
    assert _SENTINEL not in emitted
    assert "var u=" not in emitted

    tag = next(item for item in inventory.resources if item.resource_client_ref == "18")
    assert [marker.classification for marker in tag.code_markers] == ["CUSTOM_HTML"]
    assert tag.code_markers[0].source_bytes == len(embedded.encode())
    reference = next(
        edge
        for edge in inventory.edges
        if edge.source_ref == tag.resource_ref and edge.edge_kind == "VARIABLE_REFERENCE"
    )
    assert reference.selector == "Page URL"
    assert reference.target_ref == "gtm.workspace.3/built_in_variables/pageUrl"
    assert reference.resolution == "RESOLVED"


def test_contradictory_live_and_latest_identity_is_undeterminable_not_guessed() -> None:
    fixture = _Fixture(live_version="8", latest_version="8")
    original = fixture.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions/8"):
            body = _version("8")
            body["description"] = "the latest body differs from the live body"
            return httpx.Response(200, json=body)
        return original(request)

    fixture.handler = handler  # type: ignore[method-assign]
    inventory = _read(fixture)
    assert inventory.live_latest.live_version_ref == inventory.live_latest.latest_version_ref
    assert (
        inventory.live_latest.live_configuration_fingerprint
        != inventory.live_latest.latest_configuration_fingerprint
    )
    assert inventory.live_latest.status == "UNDETERMINABLE"


def test_provider_page_order_does_not_change_the_normalized_observation() -> None:
    items = _default_workspace_items()
    forward = _Fixture(paged={"tags": [[items["tags"][0]], [items["tags"][1]]]})
    reversed_pages = _Fixture(paged={"tags": [[items["tags"][1]], [items["tags"][0]]]})
    first = _read(forward)
    second = _read(reversed_pages)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert [item.resource_ref for item in first.resources] == sorted(
        item.resource_ref for item in first.resources
    )
    assert [(item.scope_ref, item.collection) for item in first.coverage] == sorted(
        (item.scope_ref, item.collection) for item in first.coverage
    )


def _environment(identifier: str, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "path": f"{_PARENT}/environments/{identifier}",
        "accountId": "1",
        "containerId": "2",
        "environmentId": identifier,
        "name": f"Environment {identifier}",
        "type": "user",
        "fingerprint": "provider-value",
        "url": "https://customer.example/preview",
        "authorizationCode": "ZZENVIRONMENTSECRETZZ",
        "enableDebug": True,
        "containerVersionId": "8",
    }
    value.update(changes)
    return value


def test_container_environments_are_read_and_never_carry_their_authorization_code() -> None:
    inventory = _read(_Fixture(environments=[_environment("5"), _environment("6")]))

    assert _coverage(inventory, "CONTAINER", "environments") == [("COMPLETE", 1, 2)]
    records = [item for item in inventory.resources if item.resource_kind == "ENVIRONMENT"]
    assert [item.resource_ref for item in records] == [
        "gtm.container.2/environments/5",
        "gtm.container.2/environments/6",
    ]
    assert records[0].scope_kind == "CONTAINER"
    assert records[0].provider_type == "user"
    assert records[0].name == "Environment 5"
    assert records[0].native_path == f"{_PARENT}/environments/5"
    # The documented resource carries an authorization code and a preview URL.
    # Neither is a field this inventory keeps, in any form but the keyed
    # configuration fingerprint.
    emitted = canonical_bytes(inventory).decode()
    assert "ZZENVIRONMENTSECRETZZ" not in emitted
    assert "customer.example" not in emitted


def test_environment_pages_are_consumed_through_the_documented_page_token() -> None:
    fixture = _Fixture(environments=[_environment("5")])
    original = fixture.handler
    pages = [
        {"environment": [_environment("5")], "nextPageToken": "1"},
        {"environment": [_environment("6")]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/environments"):
            token = request.url.params.get("pageToken")
            return httpx.Response(200, json=pages[0 if token is None else int(token)])
        return original(request)

    fixture.handler = handler  # type: ignore[method-assign]
    inventory = _read(fixture)
    assert _coverage(inventory, "CONTAINER", "environments") == [("COMPLETE", 2, 2)]
    assert sum(item.resource_kind == "ENVIRONMENT" for item in inventory.resources) == 2


def test_workspace_pending_changes_are_counted_without_retaining_the_changed_bodies() -> None:
    changed = _tag(
        "10", type="html", parameter=[{"type": "template", "key": "html", "value": _HTML_SOURCE}]
    )
    inventory = _read(
        _Fixture(
            workspace_changes=[
                {"changeStatus": "updated", "tag": changed},
                {"changeStatus": "added", "trigger": _resource("trigger", "triggers", "21")},
            ]
        )
    )

    assert _coverage(inventory, "WORKSPACE", "workspace_changes") == [("COMPLETE", 1, 2)]
    assert inventory.counts.workspace_pending_changes == 2
    assert inventory.counts.workspace_merge_conflicts == 0
    # A change entry embeds a whole resource body. None of it is normalized, so
    # no changed resource, name, or custom-code source enters the observation.
    assert _SENTINEL not in canonical_bytes(inventory).decode()
    assert all(
        item.scope_kind != "CONTAINER" or item.resource_kind != "TAG"
        for item in inventory.resources
    )


def test_an_unsupported_workspace_status_field_fails_closed() -> None:
    fixture = _Fixture()
    original = fixture.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"workspaceChange": [], "undocumented": True})
        return original(request)

    fixture.handler = handler  # type: ignore[method-assign]
    with pytest.raises(AdhelmError) as failure:
        _read(fixture)
    assert failure.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_path_or_fingerprint_the_provider_omits_is_statused_not_merely_null() -> None:
    items = _default_workspace_items()
    items["tags"] = [
        _tag("19"),
        _tag("20", path=None, fingerprint=None),
    ]
    del items["tags"][1]["path"]
    del items["tags"][1]["fingerprint"]
    inventory = _read(_Fixture(items=items))

    present = next(item for item in inventory.resources if item.resource_client_ref == "19")
    assert present.native_path_status == "PRESENT"
    assert present.provider_fingerprint_digest_status == "PRESENT"
    absent = next(item for item in inventory.resources if item.resource_client_ref == "20")
    assert absent.native_path is None and absent.native_path_status == "ABSENT"
    assert absent.provider_fingerprint_digest is None
    assert absent.provider_fingerprint_digest_status == "ABSENT"


def test_a_fingerprint_of_an_unusable_type_is_withheld_rather_than_reported_absent() -> None:
    items = _default_workspace_items()
    items["tags"] = [_tag("21", fingerprint=17)]
    inventory = _read(_Fixture(items=items))
    record = next(item for item in inventory.resources if item.resource_client_ref == "21")
    assert record.provider_fingerprint_digest is None
    assert record.provider_fingerprint_digest_status == "WITHHELD_UNSUPPORTED"


def test_trigger_condition_values_in_every_documented_filter_field_are_classified() -> None:
    condition = [
        {
            "type": "equals",
            "parameter": [{"type": "template", "key": "arg1", "value": _HTML_SOURCE}],
        }
    ]
    items = _default_workspace_items()
    items["triggers"] = [
        _resource("trigger", "triggers", "23", type="customEvent", customEventFilter=condition),
        _resource("trigger", "triggers", "24", type="click", autoEventFilter=condition),
    ]
    inventory = _read(_Fixture(items=items))

    assert _SENTINEL not in canonical_bytes(inventory).decode()
    for client_ref in ("23", "24"):
        trigger = next(
            item for item in inventory.resources if item.resource_client_ref == client_ref
        )
        assert [marker.classification for marker in trigger.code_markers] == ["CUSTOM_HTML"]
        assert trigger.code_markers[0].parameter_key == "arg1"
        assert trigger.code_markers[0].source_status == "DIGESTED_NEVER_PERSISTED"
    assert inventory.counts.custom_code_resources >= 2


def test_the_declared_edge_bound_sets_the_flag_the_dependency_engine_must_consult() -> None:
    referenced = [str(1_000 + index) for index in range(1_000)]
    triggers = [_resource("trigger", "triggers", value, type="pageview") for value in referenced]
    tags = [_tag(str(500 + index), firingTriggerId=list(referenced)) for index in range(21)]
    items: dict[str, list[dict[str, Any]]] = {
        "tags": tags,
        "triggers": triggers,
        "variables": [],
        "folders": [],
        "built_in_variables": [],
        "templates": [],
        "zones": [],
        "clients": [],
        "transformations": [],
        "gtag_config": [],
    }
    inventory = _read(_Fixture(items=items))

    # 21,000 references cross the declared 20,000-edge bound. Nothing is dropped
    # at this size, but the reader stops claiming a complete reference set.
    assert inventory.counts.edges == 21_000
    assert inventory.counts.dropped_edges == 0
    assert inventory.reference_coverage == "BOUNDED_INCOMPLETE"
    assert inventory.completeness.status == "INCOMPLETE"
    bounded = [
        item for item in inventory.coverage if item.status == "INCOMPLETE_EDGE_BOUND_EXCEEDED"
    ]
    assert [item.collection for item in bounded] == ["reference_edges"]
    assert bounded[0].scope_kind == "WORKSPACE"
    assert bounded[0].resource_count == 21_000
    assert bounded[0].dropped_count == 0


def test_edges_past_the_hard_capacity_are_counted_rather_than_silently_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared bound reports; only the hard capacity actually truncates.

    The capacities are lowered here rather than building a 50,000-edge fixture:
    the behaviour under test is the accounting, not the constant.
    """

    import adhelm.gtm_inventory as module

    monkeypatch.setattr(module, "_MAX_EDGES", 2)
    monkeypatch.setattr(module, "_MAX_EMITTED_EDGES", 3)
    items = _default_workspace_items()
    items["tags"] = [_tag("15", firingTriggerId=["20", "21"], parentFolderId="30")]
    items["zones"] = []
    inventory = _read(_Fixture(items=items))

    workspace = next(
        item
        for item in inventory.coverage
        if item.collection == "reference_edges" and item.scope_kind == "WORKSPACE"
    )
    assert workspace.status == "INCOMPLETE_EDGE_BOUND_EXCEEDED"
    assert workspace.resource_count == 3
    assert workspace.dropped_count > 0
    assert inventory.counts.dropped_edges == workspace.dropped_count
    assert inventory.counts.edges == 3
    assert inventory.reference_coverage == "BOUNDED_INCOMPLETE"
    assert inventory.completeness.status == "INCOMPLETE"

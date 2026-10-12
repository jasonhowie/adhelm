"""Bounded GTM Web fixed inventory: normalized resources, coverage, and raw edges.

This module reads only documented Tag Manager API v2 `GET` collections for one
already verified account/container identity.  It normalizes every resource into
a closed record, records per-collection pagination completeness honestly, and
emits raw reference edges as the dependency-graph input for the separate
`PROVEN_UNREFERENCED` engine.  It deliberately computes no dependency proof.

Custom-code sources (custom HTML tags, custom JavaScript variables, custom
template source, and script-bearing parameter values, including the values
nested in a trigger's documented condition filters) are hashed and classified in
memory only.  No source byte is returned, logged, exported, or placed in any
envelope, in line with the repository custom-code rule.  The same rule governs
reference edges: a name-form selector survives as text only when it named a
resource this read actually saw, and never when the classification rules would
call it custom code.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.models import SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.providers.gtm_read import (
    GtmReadClient,
    GtmReadIdentity,
    GtmReadInventory,
    ReferenceFor,
    TokenGetter,
    request_gtm_read,
)
from adhelm.transport import SafeHttpClient

_HOST = "tagmanager.googleapis.com"
_API_ROOT = "tagmanager/v2"

# Bounds.  Every one of them is reported rather than silently applied: a read
# that hits a bound is INCOMPLETE, never COMPLETE.
_MAX_INSPECTED_WORKSPACES = 25
_MAX_COLLECTION_PAGES = 20
_MAX_COLLECTION_ITEMS = 4_000
_MAX_REQUESTS = 1_000
_MAX_RESOURCES = 10_000
# The declared reference-edge bound.  Crossing it does not truncate: it makes
# the observation INCOMPLETE and sets `reference_coverage = BOUNDED_INCOMPLETE`,
# because past this point the reader stops promising a complete reference set
# and M01.2 must not read a missing edge as evidence of non-reference.
_MAX_EDGES = 20_000
# The hard emission capacity.  Only drafts past this are actually dropped, and
# every dropped draft is counted in `counts.dropped_edges` and in the owning
# scope's `reference_edges` coverage entry.
_MAX_EMITTED_EDGES = 50_000
_MAX_SCANNED_STRING = 65_536
_MAX_WALK_NODES = 20_000
_MAX_WALK_DEPTH = 24
_MAX_RESOURCE_BYTES = 1_048_576
_MAX_NAME = 256
_MAX_SOURCE_BYTES = 4_194_304

_NATIVE_ID = re.compile(r"^[0-9]{1,20}$")
_CLIENT_REF = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_RESOURCE_TYPE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.~-]{1,120}$")
_CONSENT_TYPE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# GTM writes a custom-template instance type as `cvt_<container>_<template>`.
# That format is observed, not documented in the v2 reference; the doc records
# it UNVERIFIED and an unparsable value stays UNRESOLVED rather than guessed.
_TEMPLATE_INSTANCE_TYPE = re.compile(r"^cvt_([0-9]{1,20})_([0-9]{1,20})$")
# Only a brace-free innermost reference is a GTM variable reference.  An outer
# `{{a{{b}}c}}` therefore yields `b` and the outer text stays unparsed.
_VARIABLE_REFERENCE = re.compile(r"\{\{([^{}]{0,256})\}\}")
_SCRIPT_HTML = re.compile(r"(?i)<\s*script\b|<\s*/\s*script\s*>")
_SCRIPT_URI = re.compile(r"(?i)javascript\s*:")

GtmScopeKind = Literal["LIVE_VERSION", "LATEST_VERSION", "WORKSPACE", "CONTAINER"]
GtmCollection = Literal[
    "tags",
    "triggers",
    "variables",
    "folders",
    "built_in_variables",
    "templates",
    "zones",
    "clients",
    "transformations",
    "gtag_configs",
    "destinations",
    "environments",
    # Not a listable resource collection: `workspaces.getStatus` reports the
    # pending changes of one workspace, and `reference_edges` reports the
    # reference-edge bound for one scope.  Both are statused like a collection
    # so that a surface the inventory promises is never silently absent.
    "workspace_changes",
    "reference_edges",
]
GtmResourceKind = Literal[
    "TAG",
    "TRIGGER",
    "VARIABLE",
    "FOLDER",
    "BUILT_IN_VARIABLE",
    "TEMPLATE",
    "ZONE",
    "CLIENT",
    "TRANSFORMATION",
    "GTAG_CONFIG",
    "DESTINATION",
    "ENVIRONMENT",
]
GtmCollectionStatus = Literal[
    "COMPLETE",
    "INCOMPLETE_PAGE_BOUND",
    "INCOMPLETE_ITEM_BOUND",
    "INCOMPLETE_REQUEST_BOUND",
    "INCOMPLETE_WORKSPACE_BOUND",
    "INCOMPLETE_RESOURCE_BOUND",
    "INCOMPLETE_UNDOCUMENTED_PAGE_TOKEN",
    "INCOMPLETE_EDGE_BOUND_EXCEEDED",
    "NOT_DOCUMENTED",
]
GtmCodeClassification = Literal[
    "CUSTOM_HTML",
    "CUSTOM_JAVASCRIPT",
    "CUSTOM_TEMPLATE",
    "TEMPLATE_INSTANCE",
    "BUILT_IN",
]
GtmEdgeKind = Literal[
    "FIRING_TRIGGER",
    "BLOCKING_TRIGGER",
    "SETUP_TAG",
    "TEARDOWN_TAG",
    "ENABLING_TRIGGER",
    "DISABLING_TRIGGER",
    "PARENT_FOLDER",
    "PARAMETER_TRIGGER_REFERENCE",
    "PARAMETER_TAG_REFERENCE",
    "VARIABLE_REFERENCE",
    "ZONE_TRIGGER",
    "ZONE_CHILD_CONTAINER",
    "TEMPLATE_INSTANCE",
]
GtmEdgeTargetKind = Literal[
    "TAG", "TRIGGER", "VARIABLE", "FOLDER", "TEMPLATE", "CHILD_CONTAINER", "UNKNOWN"
]
GtmSelectorForm = Literal["NATIVE_ID", "NAME", "PUBLIC_ID", "TEMPLATE_TYPE", "NONE"]
GtmSelectorStatus = Literal[
    "PRESENT",
    "ABSENT",
    "WITHHELD_OVERSIZE",
    "WITHHELD_SENSITIVE",
    "WITHHELD_UNRESOLVED_REFERENCE",
    "NOT_SCANNED_OVERSIZE",
]
GtmEdgeResolution = Literal[
    "RESOLVED",
    "UNRESOLVED_NO_MATCHING_RESOURCE",
    "UNRESOLVED_AMBIGUOUS_NAME",
    "UNRESOLVED_MALFORMED",
    "UNRESOLVED_NOT_INSPECTED",
    # A zone child container is a different container. This read is scoped to
    # one container by design, so the edge is out of scope rather than a gap in
    # a bounded read; it is deliberately not counted as an unresolved edge.
    "OUT_OF_SCOPE_CHILD_CONTAINER",
]
GtmValueStatus = Literal["PRESENT", "ABSENT", "WITHHELD_OVERSIZE", "WITHHELD_SENSITIVE"]
# The presence status of a field the reader either keeps verbatim, never
# received, or received in a form it cannot use.
GtmFieldStatus = Literal["PRESENT", "ABSENT", "WITHHELD_UNSUPPORTED"]

# collection -> (path segment, response array field, resource kind, id field)
_COLLECTIONS: dict[GtmCollection, tuple[str, str, GtmResourceKind, str]] = {
    "tags": ("tags", "tag", "TAG", "tagId"),
    "triggers": ("triggers", "trigger", "TRIGGER", "triggerId"),
    "variables": ("variables", "variable", "VARIABLE", "variableId"),
    "folders": ("folders", "folder", "FOLDER", "folderId"),
    "built_in_variables": ("built_in_variables", "builtInVariable", "BUILT_IN_VARIABLE", "type"),
    "templates": ("templates", "template", "TEMPLATE", "templateId"),
    "zones": ("zones", "zone", "ZONE", "zoneId"),
    "clients": ("clients", "client", "CLIENT", "clientId"),
    "transformations": ("transformations", "transformation", "TRANSFORMATION", "transformationId"),
    "gtag_configs": ("gtag_config", "gtagConfig", "GTAG_CONFIG", "gtagConfigId"),
    "destinations": ("destinations", "destination", "DESTINATION", "destinationLinkId"),
    "environments": ("environments", "environment", "ENVIRONMENT", "environmentId"),
}
_WORKSPACE_COLLECTIONS: tuple[GtmCollection, ...] = (
    "tags",
    "triggers",
    "variables",
    "folders",
    "built_in_variables",
    "templates",
    "zones",
    "clients",
    "transformations",
    "gtag_configs",
)
# `accounts.containers.destinations.list` is documented at container scope and
# has no documented `pageToken` request parameter (verified 2026-09-09).
# `accounts.containers.environments.list` is documented at the same container
# scope and does document both `pageToken` and `nextPageToken` (fetched
# 2026-09-09); `docs/measurement-control.md` requires environments in the
# inventory, so it is read rather than statused NOT_READ_BY_DESIGN.
_CONTAINER_COLLECTIONS: tuple[GtmCollection, ...] = ("destinations", "environments")
_PAGEABLE_COLLECTIONS = frozenset(_WORKSPACE_COLLECTIONS) | {"environments"}
# Collections that exist in this read plan but are not documented for a given
# scope. They are recorded explicitly instead of being silently absent.
_SCOPE_ONLY_COLLECTIONS: tuple[GtmCollection, ...] = (
    "destinations",
    "environments",
    "workspace_changes",
)
# ContainerVersion embeds its resources; it documents no destination array.
_VERSION_COLLECTIONS: tuple[tuple[GtmCollection, str], ...] = (
    ("tags", "tag"),
    ("triggers", "trigger"),
    ("variables", "variable"),
    ("folders", "folder"),
    ("built_in_variables", "builtInVariable"),
    ("templates", "customTemplate"),
    ("zones", "zone"),
    ("clients", "client"),
    ("transformations", "transformation"),
    ("gtag_configs", "gtagConfig"),
)
# Which read resource kinds a documented edge target may match.  A built-in
# variable is a legitimate `{{name}}` target alongside a user-defined variable.
_RESOLVABLE_KINDS: dict[GtmEdgeTargetKind, tuple[GtmResourceKind, ...]] = {
    "TAG": ("TAG",),
    "TRIGGER": ("TRIGGER",),
    "VARIABLE": ("VARIABLE", "BUILT_IN_VARIABLE"),
    "TEMPLATE": ("TEMPLATE",),
    "FOLDER": ("FOLDER",),
}


class GtmCodeMarker(StrictModel):
    """A classified custom-code surface; the source itself is never persisted."""

    schema_version: Literal[1] = 1
    classification: GtmCodeClassification
    parameter_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    source_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_bytes: int | None = Field(default=None, ge=0, le=_MAX_SOURCE_BYTES)
    source_status: Literal["DIGESTED_NEVER_PERSISTED", "NO_SOURCE"]

    @model_validator(mode="after")
    def digest_matches_status(self) -> GtmCodeMarker:
        digested = self.source_status == "DIGESTED_NEVER_PERSISTED"
        if digested != (self.source_digest is not None) or digested != (
            self.source_bytes is not None
        ):
            raise ValueError("GTM code marker digest and byte length must match its source status")
        return self


class GtmConsentSetting(StrictModel):
    """Documented `Tag.consentSettings`, kept as declared consent semantics only."""

    schema_version: Literal[1] = 1
    consent_status: Literal["notSet", "notNeeded", "needed", "UNDOCUMENTED_VALUE"]
    consent_types: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    consent_type_status: Literal["PRESENT", "ABSENT", "WITHHELD_UNSUPPORTED"]


class GtmZoneBoundary(StrictModel):
    """Documented `Zone.boundary` and `Zone.typeRestriction` shape counters."""

    schema_version: Literal[1] = 1
    condition_count: int = Field(ge=0, le=1_000)
    custom_evaluation_trigger_count: int = Field(ge=0, le=1_000)
    child_container_count: int = Field(ge=0, le=1_000)
    type_restriction_enabled: bool | None = None
    allowed_type_count: int = Field(ge=0, le=1_000)


class GtmInventoryResource(StrictModel):
    """One normalized GTM resource, closed and privacy-validated."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    scope_ref: SafeRef
    collection: GtmCollection
    resource_kind: GtmResourceKind
    resource_ref: SafeRef
    resource_client_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    native_path: str | None = Field(default=None, max_length=512)
    native_path_status: GtmFieldStatus
    provider_type: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    provider_type_status: GtmValueStatus
    name: str | None = Field(default=None, max_length=_MAX_NAME)
    name_status: GtmValueStatus
    name_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provider_fingerprint_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provider_fingerprint_digest_status: GtmFieldStatus
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_folder_ref: SafeRef | None = None
    paused: bool | None = None
    consent: GtmConsentSetting | None = None
    zone_boundary: GtmZoneBoundary | None = None
    code_markers: tuple[GtmCodeMarker, ...] = Field(default_factory=tuple, max_length=256)
    source_persistence: Literal["NEVER_PERSISTED"] = "NEVER_PERSISTED"

    @model_validator(mode="after")
    def name_matches_status(self) -> GtmInventoryResource:
        if (self.name is not None) != (self.name_status == "PRESENT"):
            raise ValueError("GTM resource name must match its declared status")
        if self.name_status == "ABSENT" and self.name_digest is not None:
            raise ValueError("An absent GTM resource name cannot carry a digest")
        if self.name_status != "ABSENT" and self.name_digest is None:
            raise ValueError("A supplied GTM resource name requires its digest")
        if (self.provider_type is not None) != (self.provider_type_status == "PRESENT"):
            raise ValueError("GTM resource type must match its declared status")
        if (self.native_path is not None) != (self.native_path_status == "PRESENT"):
            raise ValueError("GTM resource path must match its declared status")
        if (self.provider_fingerprint_digest is not None) != (
            self.provider_fingerprint_digest_status == "PRESENT"
        ):
            raise ValueError("GTM provider fingerprint must match its declared status")
        if not self.resource_ref.startswith(self.scope_ref + "/"):
            raise ValueError("GTM resource reference must be scoped to its container surface")
        return self


class GtmReferenceEdge(StrictModel):
    """A raw, unresolved-or-resolved reference edge for the M01.2 dependency graph."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    scope_ref: SafeRef
    source_ref: SafeRef
    edge_kind: GtmEdgeKind
    target_kind: GtmEdgeTargetKind
    selector_form: GtmSelectorForm
    selector: str | None = Field(default=None, max_length=_MAX_NAME)
    selector_status: GtmSelectorStatus
    selector_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target_ref: SafeRef | None = None
    resolution: GtmEdgeResolution
    occurrence_count: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def resolution_matches_target(self) -> GtmReferenceEdge:
        if (self.target_ref is not None) != (self.resolution == "RESOLVED"):
            raise ValueError("A GTM edge target reference exists exactly when it resolves")
        if (self.selector is not None) != (self.selector_status == "PRESENT"):
            raise ValueError("A GTM edge selector must match its declared status")
        return self


class GtmCollectionCoverage(StrictModel):
    """Per-collection pagination completeness; a bound is reported, never hidden."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    scope_ref: SafeRef
    collection: GtmCollection
    status: GtmCollectionStatus
    pages_read: int = Field(ge=0, le=_MAX_COLLECTION_PAGES)
    # Listed items for a real collection; emitted edges for `reference_edges`;
    # pending changes for `workspace_changes`.  The bound is the widest of the
    # three so an edge count is reported exactly rather than clamped.
    resource_count: int = Field(ge=0, le=_MAX_EMITTED_EDGES)
    # Records this scope produced but could not emit because a hard capacity
    # was reached.  It is zero for every collection whose read completed.
    dropped_count: int = Field(default=0, ge=0, le=_MAX_EMITTED_EDGES)


class GtmLiveLatestStatus(StrictModel):
    """Live versus latest identity, taken from the container header and both reads."""

    schema_version: Literal[1] = 1
    status: Literal["LIVE_EQUALS_LATEST", "LIVE_DIFFERS_FROM_LATEST", "UNDETERMINABLE"]
    live_version_ref: SafeRef
    latest_version_ref: SafeRef
    live_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    latest_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    staging_eligibility: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"


class GtmInventoryCounts(StrictModel):
    schema_version: Literal[1] = 1
    workspaces_listed: int = Field(ge=0, le=1_000)
    workspaces_inspected: int = Field(ge=0, le=_MAX_INSPECTED_WORKSPACES)
    resources: int = Field(ge=0, le=_MAX_RESOURCES)
    edges: int = Field(ge=0, le=_MAX_EMITTED_EDGES)
    unresolved_edges: int = Field(ge=0, le=_MAX_EMITTED_EDGES)
    out_of_scope_edges: int = Field(default=0, ge=0, le=_MAX_EMITTED_EDGES)
    dropped_edges: int = Field(default=0, ge=0, le=_MAX_EMITTED_EDGES)
    custom_code_resources: int = Field(ge=0, le=_MAX_RESOURCES)
    incomplete_collections: int = Field(ge=0, le=100_000)
    workspace_pending_changes: int = Field(default=0, ge=0, le=_MAX_COLLECTION_ITEMS)
    workspace_merge_conflicts: int = Field(default=0, ge=0, le=_MAX_COLLECTION_ITEMS)
    requests: int = Field(ge=0, le=_MAX_REQUESTS)


class GtmInventoryCompleteness(StrictModel):
    """Explicit unreadable/unknown coverage so M01.2 can refuse a deletion proof."""

    schema_version: Literal[1] = 1
    status: Literal["COMPLETE_WITHIN_DECLARED_BOUNDS", "INCOMPLETE"]
    incomplete_collections: int = Field(ge=0, le=100_000)
    uninspected_workspaces: int = Field(ge=0, le=1_000)
    unresolved_edges: int = Field(ge=0, le=_MAX_EMITTED_EDGES)
    dependency_proof: Literal["NOT_COMPUTED_REQUIRES_DEPENDENCY_ENGINE"] = (
        "NOT_COMPUTED_REQUIRES_DEPENDENCY_ENGINE"
    )
    deletion_authority: Literal["NEVER_ESTABLISHED_BY_INVENTORY"] = "NEVER_ESTABLISHED_BY_INVENTORY"

    @model_validator(mode="after")
    def status_matches_gaps(self) -> GtmInventoryCompleteness:
        complete = (
            self.incomplete_collections == 0
            and self.uninspected_workspaces == 0
            and self.unresolved_edges == 0
        )
        if complete != (self.status == "COMPLETE_WITHIN_DECLARED_BOUNDS"):
            raise ValueError("GTM inventory completeness must match its recorded gaps")
        return self


class GtmFullInventoryHeader(StrictModel):
    """Every fact of a complete inventory except its three record collections.

    The collections are the only part of an observation that grows with the
    container.  Keeping them apart from this header lets an inventory too large
    for one ledger record be retained as a header plus bounded collection
    chunks, and reassembled into exactly the `GtmFullInventory` that was read.
    """

    schema_version: Literal[1] = 1
    capability_version: Literal["gtm-full-fixed-inventory-1"] = "gtm-full-fixed-inventory-1"
    identity: GtmReadIdentity
    base: GtmReadInventory
    read_started_at: datetime
    read_finished_at: datetime
    live_latest: GtmLiveLatestStatus
    counts: GtmInventoryCounts
    completeness: GtmInventoryCompleteness
    #: The one flag the M01.2 `PROVEN_UNREFERENCED` engine must consult before
    #: reading the absence of an edge as evidence.  `BOUNDED_INCOMPLETE` means
    #: the declared reference-edge bound was crossed, so an unreferenced-looking
    #: resource may simply have had its only referring edge bounded away.
    reference_coverage: Literal["COMPLETE_WITHIN_DECLARED_BOUNDS", "BOUNDED_INCOMPLETE"] = (
        "COMPLETE_WITHIN_DECLARED_BOUNDS"
    )
    observation_status: Literal["SEQUENTIALLY_READ_NONATOMIC"] = "SEQUENTIALLY_READ_NONATOMIC"
    dependency_graph: Literal["RAW_EDGES_ONLY_NOT_A_GRAPH"] = "RAW_EDGES_ONLY_NOT_A_GRAPH"
    ui_reconciliation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    custom_code_source: Literal["CLASSIFIED_AND_HASHED_NEVER_PERSISTED"] = (
        "CLASSIFIED_AND_HASHED_NEVER_PERSISTED"
    )

    @model_validator(mode="after")
    def bound_header(self) -> GtmFullInventoryHeader:
        if self.read_started_at.tzinfo is None or self.read_finished_at.tzinfo is None:
            raise ValueError("GTM inventory read times must be timezone-aware")
        if self.read_finished_at < self.read_started_at:
            raise ValueError("GTM inventory read interval cannot be reversed")
        if self.base.identity != self.identity:
            raise ValueError("GTM inventory must bind the exact observed identity")
        return self


class GtmFullInventory(GtmFullInventoryHeader):
    """The complete fixed GTM Web inventory observation for one container."""

    coverage: tuple[GtmCollectionCoverage, ...] = Field(max_length=100_000)
    resources: tuple[GtmInventoryResource, ...] = Field(max_length=_MAX_RESOURCES)
    edges: tuple[GtmReferenceEdge, ...] = Field(max_length=_MAX_EMITTED_EDGES)

    @model_validator(mode="after")
    def bound_observation(self) -> GtmFullInventory:
        if self.counts.resources != len(self.resources) or self.counts.edges != len(self.edges):
            raise ValueError("GTM inventory counts must match their normalized records")
        if self.counts.unresolved_edges != sum(
            edge.resolution.startswith("UNRESOLVED_") for edge in self.edges
        ):
            raise ValueError("GTM unresolved edge count must match its edges")
        if self.counts.out_of_scope_edges != sum(
            edge.resolution == "OUT_OF_SCOPE_CHILD_CONTAINER" for edge in self.edges
        ):
            raise ValueError("GTM out-of-scope edge count must match its edges")
        if self.counts.incomplete_collections != sum(
            item.status != "COMPLETE" and item.status != "NOT_DOCUMENTED" for item in self.coverage
        ):
            raise ValueError("GTM incomplete collection count must match its coverage")
        if self.completeness.incomplete_collections != self.counts.incomplete_collections:
            raise ValueError("GTM completeness must match the recorded coverage gaps")
        if self.counts.dropped_edges != sum(item.dropped_count for item in self.coverage):
            raise ValueError("GTM dropped edge count must match its coverage")
        bounded = any(item.status == "INCOMPLETE_EDGE_BOUND_EXCEEDED" for item in self.coverage)
        if bounded != (self.reference_coverage == "BOUNDED_INCOMPLETE"):
            raise ValueError("GTM reference coverage must match its recorded edge bound")
        seen = {resource.resource_ref for resource in self.resources}
        if len(seen) != len(self.resources):
            raise ValueError("GTM inventory resources must have unique references")
        return self


def _fingerprint(key: bytes, domain: str, value: Any) -> str:
    return hmac.new(
        key,
        b"adhelm.gtm-inventory.v1/" + domain.encode("ascii") + b"\0" + canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def _text_digest(key: bytes, domain: str, value: str) -> str:
    return hmac.new(
        key,
        b"adhelm.gtm-inventory.v1/" + domain.encode("ascii") + b"\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = parse_json(response.content, maximum_bytes=8_388_608)
    except (AdhelmError, UnicodeError, ValueError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GTM {context} response is invalid."
        ) from exc
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} response is invalid.")
    return cast(dict[str, Any], value)


def _page_token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or any(ord(character) < 33 for character in value)
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM pagination token is invalid.")
    return value


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(ord(character) < 33 for character in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED", "GTM authorization is unavailable.", exit_code=4
        )
    return token


def _walk(value: Any, *, budget: list[int], depth: int = 0) -> Iterator[tuple[str, Any]]:
    """Yield (key, value) pairs from a bounded nested structure without copying it."""

    if depth > _MAX_WALK_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(value, dict):
        for key, child in value.items():
            if budget[0] <= 0:
                return
            yield (key if isinstance(key, str) else ""), child
            yield from _walk(child, budget=budget, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            if budget[0] <= 0:
                return
            # A scalar member of an array is a value in its own right. Yielding
            # only the recursion would skip every `{{ }}` token that sits
            # directly inside a string array, and resource bodies are not
            # schema-closed, so such a field can appear at any time.
            yield "", child
            yield from _walk(child, budget=budget, depth=depth + 1)


def _bounded_name(value: Any, key: bytes) -> tuple[str | None, GtmValueStatus, str | None]:
    """Keep a short, privacy-clean display name; otherwise keep only its digest."""

    if value is None:
        return None, "ABSENT", None
    if not isinstance(value, str):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource name is invalid.")
    digest_value = _text_digest(key, "name", value)
    if len(value) > _MAX_NAME or any(ord(character) < 32 for character in value):
        return None, "WITHHELD_OVERSIZE", digest_value
    try:
        validate_data(value)
    except AdhelmError:
        return None, "WITHHELD_SENSITIVE", digest_value
    return value, "PRESENT", digest_value


def _bounded_type(value: Any) -> tuple[str | None, GtmValueStatus]:
    if value is None:
        return None, "ABSENT"
    if not isinstance(value, str):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource type is invalid.")
    if _RESOURCE_TYPE.fullmatch(value) is None:
        return None, "WITHHELD_OVERSIZE"
    return value, "PRESENT"


def _native_path(value: Any, prefix: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 512
        or not value.startswith(prefix)
        or any(ord(character) < 32 for character in value)
        or any(
            _PATH_SEGMENT.fullmatch(segment) is None
            for segment in value.removeprefix(prefix).split("/")
        )
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource path is invalid.")
    return value


def _client_ref(value: Any) -> str:
    if not isinstance(value, str) or _CLIENT_REF.fullmatch(value) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource identifier is invalid.")
    return value


def _string_list(value: Any, context: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 1_000:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} is invalid.")
    for item in value:
        if not isinstance(item, str) or len(item) > _MAX_NAME:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} is invalid.")
    return list(value)


class _EdgeDraft:
    """A pre-resolution edge; resolution happens once a whole scope is read."""

    __slots__ = (
        "edge_kind",
        "match_digest",
        "native_selector",
        "occurrences",
        "selector",
        "selector_digest",
        "selector_form",
        "selector_status",
        "source_ref",
        "target_kind",
        "unresolvable",
    )

    def __init__(
        self,
        *,
        source_ref: str,
        edge_kind: GtmEdgeKind,
        target_kind: GtmEdgeTargetKind,
        selector_form: GtmSelectorForm,
        selector: str | None,
        selector_status: GtmSelectorStatus,
        selector_digest: str | None,
        native_selector: str | None = None,
        match_digest: str | None = None,
        unresolvable: GtmEdgeResolution | None = None,
    ) -> None:
        self.source_ref = source_ref
        self.edge_kind = edge_kind
        self.target_kind = target_kind
        self.selector_form = selector_form
        self.selector = selector
        self.selector_status = selector_status
        self.selector_digest = selector_digest
        #: The exact provider text, kept in memory for identifier matching only.
        #: It is never copied into a `GtmReferenceEdge`.
        self.native_selector = native_selector
        #: The `name`-domain digest of that text.  Resource names are digested
        #: in the `name` domain, so a name-form selector must be digested in the
        #: same domain to be able to match one; the persisted `selector_digest`
        #: keeps its own `selector` domain.
        self.match_digest = match_digest
        self.unresolvable = unresolvable
        self.occurrences = 1

    @property
    def identity(self) -> tuple[str, str, str, str, str | None]:
        return (
            self.source_ref,
            self.edge_kind,
            self.target_kind,
            self.selector_form,
            self.selector_digest,
        )


class _Scope:
    """One readable surface: a container version or an accessible workspace."""

    def __init__(self, kind: GtmScopeKind, scope_ref: str) -> None:
        self.kind = kind
        self.scope_ref = scope_ref
        self.resources: list[GtmInventoryResource] = []
        self.edges: list[_EdgeDraft] = []
        self.coverage: list[GtmCollectionCoverage] = []
        self.by_id: dict[tuple[GtmResourceKind, str], str] = {}
        # Names are matched by their `name`-domain digest only.  Every read
        # name has one, whether it was kept verbatim or withheld, so this single
        # index resolves an oversize or sensitive name exactly like a plain one.
        self.by_name_digest: dict[tuple[GtmResourceKind, str], list[str]] = {}
        self.emitted_edges = 0
        self.dropped_edges = 0
        self.edge_bound_exceeded = False


class GtmInventoryClient:
    """Closed GTM full-inventory reader; it exposes no generic request or write."""

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        reader: GtmReadClient,
        *,
        fingerprint_key: bytes,
        reference_for: ReferenceFor,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if _HOST not in transport.allowed_hosts:
            raise AdhelmError("TRANSPORT_CONFIG_INVALID", "GTM inventory requires its fixed host.")
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) != 32:
            raise AdhelmError(
                "FINGERPRINT_KEY_REQUIRED", "GTM inventory requires a local fingerprint key."
            )
        self._transport = transport
        self._token_getter = token_getter
        self._reader = reader
        self._fingerprint_key = fingerprint_key
        self._reference_for = reference_for
        self._now = now
        self._requests = 0
        self._resource_budget = _MAX_RESOURCES
        self._edge_budget = _MAX_EDGES
        self._emission_budget = _MAX_EMITTED_EDGES
        self._pending_changes = 0
        self._merge_conflicts = 0
        self._path_prefix = ""

    # ---------------------------------------------------------------- reading

    def full_inventory(self, *, identity: GtmReadIdentity) -> GtmFullInventory:
        started = self._now()
        self._requests = 0
        self._resource_budget = _MAX_RESOURCES
        self._edge_budget = _MAX_EDGES
        self._emission_budget = _MAX_EMITTED_EDGES
        self._pending_changes = 0
        self._merge_conflicts = 0
        raw_base, live_raw, latest_raw = self._reader.inventory_with_sources(identity=identity)
        base = _ordered_base(raw_base)
        account = identity.account_client_ref
        container = identity.container_client_ref
        parent = f"accounts/{account}/containers/{container}"
        self._path_prefix = f"{parent}/"
        headers = {"authorization": f"Bearer {_safe_token(self._token_getter)}"}

        scopes: list[_Scope] = [
            self._version_scope("LIVE_VERSION", base.live.version_ref, live_raw),
        ]
        if base.latest.version_ref != base.live.version_ref:
            scopes.append(
                self._version_scope("LATEST_VERSION", base.latest.version_ref, latest_raw)
            )
        # When live and latest are one version, that version is inventoried once
        # under the LIVE_VERSION scope; the live/latest status records equality.
        container_scope = _Scope("CONTAINER", self._reference_for("container", container))
        self._read_container_collections(container_scope, parent, headers)
        scopes.append(container_scope)

        inspected = 0
        for workspace in base.workspaces:
            workspace_ref = workspace.workspace_ref
            native = workspace_ref.rsplit(".", 1)[-1]
            if _NATIVE_ID.fullmatch(native) is None:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GTM workspace reference is unusable."
                )
            scope = _Scope("WORKSPACE", workspace_ref)
            if inspected >= _MAX_INSPECTED_WORKSPACES:
                self._record_bound(scope, _WORKSPACE_COLLECTIONS, "INCOMPLETE_WORKSPACE_BOUND")
                scopes.append(scope)
                continue
            inspected += 1
            self._read_workspace_collections(scope, f"{parent}/workspaces/{native}", headers)
            scopes.append(scope)

        resources: list[GtmInventoryResource] = []
        edges: list[GtmReferenceEdge] = []
        coverage: list[GtmCollectionCoverage] = []
        for scope in scopes:
            resources.extend(scope.resources)
            edges.extend(self._resolve(scope))
            scope.coverage.append(
                GtmCollectionCoverage(
                    scope_kind=scope.kind,
                    scope_ref=scope.scope_ref,
                    collection="reference_edges",
                    status=(
                        "INCOMPLETE_EDGE_BOUND_EXCEEDED"
                        if scope.edge_bound_exceeded
                        else "COMPLETE"
                    ),
                    pages_read=0,
                    resource_count=scope.emitted_edges,
                    dropped_count=scope.dropped_edges,
                )
            )
            coverage.extend(scope.coverage)
        # Order is presentation-independent.  Sorting by exact local references
        # keeps one container's observation identical whatever order the
        # provider returned its pages in.
        coverage.sort(key=lambda item: (item.scope_ref, item.collection))
        resources.sort(key=lambda item: item.resource_ref)
        edges.sort(key=_edge_order)
        finished = self._now()

        incomplete = sum(
            item.status != "COMPLETE" and item.status != "NOT_DOCUMENTED" for item in coverage
        )
        unresolved = sum(edge.resolution.startswith("UNRESOLVED_") for edge in edges)
        out_of_scope = sum(edge.resolution == "OUT_OF_SCOPE_CHILD_CONTAINER" for edge in edges)
        dropped = sum(scope.dropped_edges for scope in scopes)
        bounded = any(scope.edge_bound_exceeded for scope in scopes)
        uninspected = max(len(base.workspaces) - inspected, 0)
        return GtmFullInventory(
            identity=identity,
            base=base,
            read_started_at=started,
            read_finished_at=finished,
            live_latest=self._live_latest(base),
            coverage=tuple(coverage),
            resources=tuple(resources),
            edges=tuple(edges),
            counts=GtmInventoryCounts(
                workspaces_listed=len(base.workspaces),
                workspaces_inspected=inspected,
                resources=len(resources),
                edges=len(edges),
                unresolved_edges=unresolved,
                out_of_scope_edges=out_of_scope,
                dropped_edges=dropped,
                custom_code_resources=sum(bool(item.code_markers) for item in resources),
                incomplete_collections=incomplete,
                workspace_pending_changes=self._pending_changes,
                workspace_merge_conflicts=self._merge_conflicts,
                requests=self._requests,
            ),
            reference_coverage=(
                "BOUNDED_INCOMPLETE" if bounded else "COMPLETE_WITHIN_DECLARED_BOUNDS"
            ),
            completeness=GtmInventoryCompleteness(
                status="COMPLETE_WITHIN_DECLARED_BOUNDS"
                if incomplete == 0 and uninspected == 0 and unresolved == 0
                else "INCOMPLETE",
                incomplete_collections=incomplete,
                uninspected_workspaces=uninspected,
                unresolved_edges=unresolved,
            ),
        )

    def _live_latest(self, base: GtmReadInventory) -> GtmLiveLatestStatus:
        live, latest = base.live, base.latest
        same_version = live.version_ref == latest.version_ref
        same_fingerprint = live.configuration_fingerprint == latest.configuration_fingerprint
        if same_version and same_fingerprint:
            status: Literal["LIVE_EQUALS_LATEST", "LIVE_DIFFERS_FROM_LATEST", "UNDETERMINABLE"] = (
                "LIVE_EQUALS_LATEST"
            )
        elif not same_version and not same_fingerprint:
            status = "LIVE_DIFFERS_FROM_LATEST"
        else:
            # One identity says equal and the other says different.  Never guess.
            status = "UNDETERMINABLE"
        return GtmLiveLatestStatus(
            status=status,
            live_version_ref=live.version_ref,
            latest_version_ref=latest.version_ref,
            live_configuration_fingerprint=live.configuration_fingerprint,
            latest_configuration_fingerprint=latest.configuration_fingerprint,
        )

    def _request(
        self, url: str, headers: dict[str, str], params: dict[str, str] | None
    ) -> httpx.Response:
        if self._requests >= _MAX_REQUESTS:
            raise AdhelmError("PAGINATION_LIMIT", "GTM inventory exceeded its fixed request bound.")
        self._requests += 1
        return request_gtm_read(self._transport, url, headers=headers, params=params)

    def _read_container_collections(
        self, scope: _Scope, parent: str, headers: dict[str, str]
    ) -> None:
        for collection in _CONTAINER_COLLECTIONS:
            self._read_collection(scope, parent, headers, collection)
        # Pending changes belong to a workspace, never to the container itself.
        self._record_not_documented(scope, ("workspace_changes",))

    def _read_workspace_collections(
        self, scope: _Scope, parent: str, headers: dict[str, str]
    ) -> None:
        for collection in _WORKSPACE_COLLECTIONS:
            self._read_collection(scope, parent, headers, collection)
        self._read_workspace_status(scope, parent, headers)
        # Destinations and environments are documented only at container scope.
        self._record_not_documented(scope, ("destinations", "environments"))

    def _read_workspace_status(self, scope: _Scope, parent: str, headers: dict[str, str]) -> None:
        """Record this workspace's pending changes from the documented status read.

        `accounts.containers.workspaces.getStatus` (`{workspace}/status`,
        fetched 2026-09-09) documents a closed response with `workspaceChange[]`
        and `mergeConflict[]` and no request parameters, so it is read rather
        than statused NOT_READ.  Only the counts survive: a `workspaceChange`
        entry embeds a whole changed tag, trigger or variable body, and none of
        that raw configuration is normalized, digested, or persisted here.
        """

        if self._requests >= _MAX_REQUESTS:
            self._record_status_coverage(scope, "INCOMPLETE_REQUEST_BOUND", 0, 0)
            return
        url = f"https://{_HOST}/{_API_ROOT}/{parent}/status"
        payload = _json(self._request(url, headers, None), "workspace status")
        if set(payload) - {"workspaceChange", "mergeConflict"}:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM workspace status has an unsupported field."
            )
        changes = payload.get("workspaceChange", [])
        conflicts = payload.get("mergeConflict", [])
        if not isinstance(changes, list) or not isinstance(conflicts, list):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM workspace status is invalid.")
        if len(changes) > _MAX_COLLECTION_ITEMS or len(conflicts) > _MAX_COLLECTION_ITEMS:
            self._record_status_coverage(scope, "INCOMPLETE_ITEM_BOUND", 1, 0)
            return
        self._pending_changes = min(self._pending_changes + len(changes), _MAX_COLLECTION_ITEMS)
        self._merge_conflicts = min(self._merge_conflicts + len(conflicts), _MAX_COLLECTION_ITEMS)
        self._record_status_coverage(scope, "COMPLETE", 1, len(changes))

    def _record_status_coverage(
        self, scope: _Scope, status: GtmCollectionStatus, pages: int, changes: int
    ) -> None:
        scope.coverage.append(
            GtmCollectionCoverage(
                scope_kind=scope.kind,
                scope_ref=scope.scope_ref,
                collection="workspace_changes",
                status=status,
                pages_read=pages,
                resource_count=changes,
            )
        )

    def _record_not_documented(self, scope: _Scope, collections: tuple[GtmCollection, ...]) -> None:
        for collection in collections:
            scope.coverage.append(
                GtmCollectionCoverage(
                    scope_kind=scope.kind,
                    scope_ref=scope.scope_ref,
                    collection=collection,
                    status="NOT_DOCUMENTED",
                    pages_read=0,
                    resource_count=0,
                )
            )

    def _record_bound(
        self,
        scope: _Scope,
        collections: tuple[GtmCollection, ...],
        status: GtmCollectionStatus,
    ) -> None:
        for collection in (*collections, "workspace_changes"):
            scope.coverage.append(
                GtmCollectionCoverage(
                    scope_kind=scope.kind,
                    scope_ref=scope.scope_ref,
                    collection=collection,
                    status=status,
                    pages_read=0,
                    resource_count=0,
                )
            )
        self._record_not_documented(scope, ("destinations", "environments"))

    def _read_collection(
        self,
        scope: _Scope,
        parent: str,
        headers: dict[str, str],
        collection: GtmCollection,
    ) -> None:
        segment, field, kind, id_field = _COLLECTIONS[collection]
        url = f"https://{_HOST}/{_API_ROOT}/{parent}/{segment}"
        pageable = collection in _PAGEABLE_COLLECTIONS
        status: GtmCollectionStatus = "COMPLETE"
        pages = 0
        items: list[dict[str, Any]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            if self._requests >= _MAX_REQUESTS:
                status = "INCOMPLETE_REQUEST_BOUND"
                break
            params = {"pageToken": token} if token is not None else None
            payload = _json(self._request(url, headers, params), f"{collection} list")
            pages += 1
            if set(payload) - {field, "nextPageToken"}:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", f"GTM {collection} list has an unsupported field."
                )
            page = payload.get(field, [])
            if not isinstance(page, list):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {collection} page is invalid.")
            if len(items) + len(page) > _MAX_COLLECTION_ITEMS:
                status = "INCOMPLETE_ITEM_BOUND"
                break
            for raw in page:
                if not isinstance(raw, dict):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", f"GTM {collection} item is invalid."
                    )
                items.append(raw)
            next_token = payload.get("nextPageToken")
            if next_token is None:
                break
            if not pageable:
                # The documented request surface offers no page token to return.
                status = "INCOMPLETE_UNDOCUMENTED_PAGE_TOKEN"
                break
            if pages >= _MAX_COLLECTION_PAGES:
                status = "INCOMPLETE_PAGE_BOUND"
                break
            token = _page_token(next_token)
            if token in seen_tokens:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", f"GTM {collection} pagination made no progress."
                )
            seen_tokens.add(token)
        for raw in items:
            if self._resource_budget <= 0:
                status = "INCOMPLETE_RESOURCE_BOUND"
                break
            self._resource_budget -= 1
            self._normalize(scope, raw, collection, kind, id_field)
        scope.coverage.append(
            GtmCollectionCoverage(
                scope_kind=scope.kind,
                scope_ref=scope.scope_ref,
                collection=collection,
                status=status,
                pages_read=pages,
                resource_count=len(items),
            )
        )

    def _version_scope(
        self,
        kind: GtmScopeKind,
        version_ref: str,
        raw: dict[str, Any],
    ) -> _Scope:
        scope = _Scope(kind, version_ref)
        for collection, field in _VERSION_COLLECTIONS:
            _, _, resource_kind, id_field = _COLLECTIONS[collection]
            resources = raw.get(field, [])
            if not isinstance(resources, list):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GTM version resource collection is invalid."
                )
            status: GtmCollectionStatus = "COMPLETE"
            for item in resources:
                if not isinstance(item, dict):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "GTM version resource is invalid."
                    )
                if self._resource_budget <= 0:
                    status = "INCOMPLETE_RESOURCE_BOUND"
                    break
                self._resource_budget -= 1
                self._normalize(scope, item, collection, resource_kind, id_field)
            scope.coverage.append(
                GtmCollectionCoverage(
                    scope_kind=kind,
                    scope_ref=version_ref,
                    collection=collection,
                    status=status,
                    pages_read=0,
                    resource_count=len(resources),
                )
            )
        # A container version embeds no destination, environment, or pending
        # workspace change; each is documented at a different scope and is
        # statused here rather than being silently absent.
        self._record_not_documented(scope, _SCOPE_ONLY_COLLECTIONS)
        return scope

    # ----------------------------------------------------------- normalizing

    def _normalize(
        self,
        scope: _Scope,
        raw: dict[str, Any],
        collection: GtmCollection,
        kind: GtmResourceKind,
        id_field: str,
    ) -> None:
        key = self._fingerprint_key
        if len(canonical_bytes(raw)) > _MAX_RESOURCE_BYTES:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource exceeds its byte bound.")
        client_ref = _client_ref(raw.get(id_field))
        resource_ref = f"{scope.scope_ref}/{collection}/{client_ref}"
        if len(resource_ref) > 160:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource reference is too long.")
        if (kind, client_ref) in scope.by_id:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM resource identifier is duplicated.")
        name, name_status, name_digest = _bounded_name(raw.get("name"), key)
        provider_type, type_status = _bounded_type(raw.get("type"))
        fingerprint = raw.get("fingerprint")
        fingerprint_digest = (
            _text_digest(key, "provider-fingerprint", fingerprint)
            if isinstance(fingerprint, str)
            else None
        )
        native_path = _native_path(raw.get("path"), self._path_prefix)
        parent_folder = raw.get("parentFolderId")
        paused = raw.get("paused")
        if paused is not None and not isinstance(paused, bool):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM paused state is invalid.")
        resource = GtmInventoryResource(
            scope_kind=scope.kind,
            scope_ref=scope.scope_ref,
            collection=collection,
            resource_kind=kind,
            resource_ref=resource_ref,
            resource_client_ref=client_ref,
            native_path=native_path,
            native_path_status=_field_status(raw.get("path"), native_path),
            provider_type=provider_type,
            provider_type_status=type_status,
            name=name,
            name_status=name_status,
            name_digest=name_digest,
            provider_fingerprint_digest=fingerprint_digest,
            provider_fingerprint_digest_status=_field_status(fingerprint, fingerprint_digest),
            configuration_fingerprint=_fingerprint(key, f"resource.{kind}", raw),
            parent_folder_ref=(
                f"{scope.scope_ref}/folders/{_client_ref(parent_folder)}"
                if parent_folder is not None
                else None
            ),
            paused=paused,
            consent=self._consent(raw.get("consentSettings")),
            zone_boundary=self._zone_boundary(raw) if kind == "ZONE" else None,
            code_markers=self._code_markers(raw, kind, provider_type),
        )
        scope.resources.append(resource)
        scope.by_id[(kind, client_ref)] = resource_ref
        if name_digest is not None:
            scope.by_name_digest.setdefault((kind, name_digest), []).append(resource_ref)
        self._edges(scope, raw, kind, resource_ref, provider_type)

    def _consent(self, value: Any) -> GtmConsentSetting | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM consent settings are invalid.")
        raw_status = value.get("consentStatus")
        status: Literal["notSet", "notNeeded", "needed", "UNDOCUMENTED_VALUE"]
        if raw_status in ("notSet", "notNeeded", "needed"):
            status = cast(Literal["notSet", "notNeeded", "needed"], raw_status)
        elif raw_status is None:
            status = "notSet"
        else:
            status = "UNDOCUMENTED_VALUE"
        consent_type = value.get("consentType")
        if consent_type is None:
            return GtmConsentSetting(consent_status=status, consent_type_status="ABSENT")
        if not isinstance(consent_type, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM consent type is invalid.")
        types: list[str] = []
        supported = True
        for parameter in _parameters(consent_type, budget=[_MAX_WALK_NODES]):
            child = parameter.get("value")
            if not isinstance(child, str):
                continue
            if _CONSENT_TYPE.fullmatch(child) is None:
                supported = False
                continue
            if len(types) >= 64:
                supported = False
                break
            if child not in types:
                types.append(child)
        return GtmConsentSetting(
            consent_status=status,
            consent_types=tuple(sorted(types)),
            consent_type_status="PRESENT" if supported and types else "WITHHELD_UNSUPPORTED",
        )

    def _zone_boundary(self, raw: dict[str, Any]) -> GtmZoneBoundary:
        boundary = raw.get("boundary")
        restriction = raw.get("typeRestriction")
        children = raw.get("childContainer")
        if (
            (boundary is not None and not isinstance(boundary, dict))
            or (restriction is not None and not isinstance(restriction, dict))
            or (children is not None and not isinstance(children, list))
        ):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM zone boundary is invalid.")
        conditions = (boundary or {}).get("condition")
        triggers = _string_list(
            (boundary or {}).get("customEvaluationTriggerId"), "zone evaluation triggers"
        )
        allowed = _string_list((restriction or {}).get("whitelistedTypeId"), "zone type allowlist")
        enabled = (restriction or {}).get("enable")
        if enabled is not None and not isinstance(enabled, bool):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM zone type restriction is invalid.")
        return GtmZoneBoundary(
            condition_count=len(conditions) if isinstance(conditions, list) else 0,
            custom_evaluation_trigger_count=len(triggers),
            child_container_count=len(children or []),
            type_restriction_enabled=enabled,
            allowed_type_count=len(allowed),
        )

    def _code_markers(
        self, raw: dict[str, Any], kind: GtmResourceKind, provider_type: str | None
    ) -> tuple[GtmCodeMarker, ...]:
        key = self._fingerprint_key
        markers: list[GtmCodeMarker] = []
        if kind == "BUILT_IN_VARIABLE":
            return (GtmCodeMarker(classification="BUILT_IN", source_status="NO_SOURCE"),)
        if kind == "TEMPLATE":
            source = raw.get("templateData")
            if isinstance(source, str):
                markers.append(self._source_marker("CUSTOM_TEMPLATE", "templateData", source, key))
            else:
                markers.append(
                    GtmCodeMarker(classification="CUSTOM_TEMPLATE", source_status="NO_SOURCE")
                )
        if provider_type is not None and _TEMPLATE_INSTANCE_TYPE.fullmatch(provider_type):
            markers.append(
                GtmCodeMarker(classification="TEMPLATE_INSTANCE", source_status="NO_SOURCE")
            )
        for parameter in _classifiable_parameters(raw, budget=[_MAX_WALK_NODES]):
            value = parameter.get("value")
            if not isinstance(value, str) or not value:
                continue
            raw_key = parameter.get("key")
            parameter_key = (
                raw_key
                if isinstance(raw_key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw_key)
                else None
            )
            classification = _classify_parameter(kind, provider_type, parameter_key, value)
            if classification is None:
                continue
            if len(markers) >= 256:
                break
            markers.append(self._source_marker(classification, parameter_key, value, key))
        return tuple(markers)

    @staticmethod
    def _source_marker(
        classification: GtmCodeClassification, parameter_key: str | None, source: str, key: bytes
    ) -> GtmCodeMarker:
        encoded = source.encode("utf-8")
        if len(encoded) > _MAX_SOURCE_BYTES:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM custom code exceeds its byte bound."
            )
        return GtmCodeMarker(
            classification=classification,
            parameter_key=parameter_key,
            source_digest=_text_digest(key, "custom-code", source),
            source_bytes=len(encoded),
            source_status="DIGESTED_NEVER_PERSISTED",
        )

    # ---------------------------------------------------------------- edges

    def _edges(
        self,
        scope: _Scope,
        raw: dict[str, Any],
        kind: GtmResourceKind,
        source_ref: str,
        provider_type: str | None,
    ) -> None:
        key = self._fingerprint_key
        drafts: list[_EdgeDraft] = []

        def add(
            edge_kind: GtmEdgeKind,
            target_kind: GtmEdgeTargetKind,
            selector_form: GtmSelectorForm,
            value: str,
            *,
            pattern: re.Pattern[str] | None = None,
        ) -> None:
            digest_value = _text_digest(key, "selector", value)
            if pattern is not None and pattern.fullmatch(value) is None:
                drafts.append(
                    _EdgeDraft(
                        source_ref=source_ref,
                        edge_kind=edge_kind,
                        target_kind=target_kind,
                        selector_form=selector_form,
                        selector=None,
                        selector_status="ABSENT",
                        selector_digest=digest_value,
                        native_selector=value,
                        match_digest=_text_digest(key, "name", value),
                        unresolvable="UNRESOLVED_MALFORMED",
                    )
                )
                return
            selector, status, _ = _bounded_name(value, key)
            if _custom_code_text(value) is not None:
                # The module classifies this exact string as custom code
                # elsewhere. It cannot also be published verbatim as edge text,
                # even if it later matches a read resource name: a resource name
                # that is itself code is still code (AGENTS rule 13).
                selector, status = None, "WITHHELD_SENSITIVE"
            drafts.append(
                _EdgeDraft(
                    source_ref=source_ref,
                    edge_kind=edge_kind,
                    target_kind=target_kind,
                    selector_form=selector_form,
                    selector=selector,
                    selector_status=cast(GtmSelectorStatus, status),
                    selector_digest=digest_value,
                    native_selector=value,
                    match_digest=_text_digest(key, "name", value),
                )
            )

        parent_folder = raw.get("parentFolderId")
        if isinstance(parent_folder, str):
            add("PARENT_FOLDER", "FOLDER", "NATIVE_ID", parent_folder, pattern=_NATIVE_ID)
        for field, edge_kind in (
            ("firingTriggerId", "FIRING_TRIGGER"),
            ("blockingTriggerId", "BLOCKING_TRIGGER"),
            ("enablingTriggerId", "ENABLING_TRIGGER"),
            ("disablingTriggerId", "DISABLING_TRIGGER"),
        ):
            for value in _string_list(raw.get(field), field):
                add(cast(GtmEdgeKind, edge_kind), "TRIGGER", "NATIVE_ID", value, pattern=_NATIVE_ID)
        for field, edge_kind in (("setupTag", "SETUP_TAG"), ("teardownTag", "TEARDOWN_TAG")):
            sequenced = raw.get(field)
            if sequenced is None:
                continue
            if not isinstance(sequenced, list) or len(sequenced) > 1_000:
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM tag sequencing is invalid.")
            for item in sequenced:
                if not isinstance(item, dict):
                    raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM tag sequencing is invalid.")
                tag_name = item.get("tagName")
                if isinstance(tag_name, str):
                    add(cast(GtmEdgeKind, edge_kind), "TAG", "NAME", tag_name)
        if kind == "ZONE":
            boundary = raw.get("boundary")
            if isinstance(boundary, dict):
                for value in _string_list(
                    boundary.get("customEvaluationTriggerId"), "zone evaluation triggers"
                ):
                    add("ZONE_TRIGGER", "TRIGGER", "NATIVE_ID", value, pattern=_NATIVE_ID)
            children = raw.get("childContainer")
            if isinstance(children, list):
                for item in children[:1_000]:
                    if isinstance(item, dict) and isinstance(item.get("publicId"), str):
                        add(
                            "ZONE_CHILD_CONTAINER",
                            "CHILD_CONTAINER",
                            "PUBLIC_ID",
                            item["publicId"],
                            pattern=_PUBLIC_ID,
                        )
        if provider_type is not None and _TEMPLATE_INSTANCE_TYPE.fullmatch(provider_type):
            template_id = cast(re.Match[str], _TEMPLATE_INSTANCE_TYPE.fullmatch(provider_type))[2]
            add("TEMPLATE_INSTANCE", "TEMPLATE", "TEMPLATE_TYPE", template_id, pattern=_NATIVE_ID)

        budget = [_MAX_WALK_NODES]
        parameters = raw.get("parameter")
        for parameter in _parameters(parameters, budget=budget):
            declared = parameter.get("type")
            parameter_value = parameter.get("value")
            if not isinstance(parameter_value, str):
                continue
            if declared == "triggerReference":
                add(
                    "PARAMETER_TRIGGER_REFERENCE",
                    "TRIGGER",
                    "NATIVE_ID",
                    parameter_value,
                    pattern=_NATIVE_ID,
                )
            elif declared == "tagReference":
                add("PARAMETER_TAG_REFERENCE", "TAG", "NAME", parameter_value)

        for _, scanned in _walk(raw, budget=[_MAX_WALK_NODES]):
            if not isinstance(scanned, str) or "{{" not in scanned:
                continue
            value = scanned
            if len(value) > _MAX_SCANNED_STRING:
                drafts.append(
                    _EdgeDraft(
                        source_ref=source_ref,
                        edge_kind="VARIABLE_REFERENCE",
                        target_kind="VARIABLE",
                        selector_form="NONE",
                        selector=None,
                        selector_status="NOT_SCANNED_OVERSIZE",
                        selector_digest=_text_digest(key, "selector", value[:_MAX_SCANNED_STRING]),
                        unresolvable="UNRESOLVED_MALFORMED",
                    )
                )
                continue
            matches = _VARIABLE_REFERENCE.findall(value)
            for name in matches:
                add("VARIABLE_REFERENCE", "VARIABLE", "NAME", name)
            if value.count("{{") != value.count("}}") or (not matches and "{{" in value):
                drafts.append(
                    _EdgeDraft(
                        source_ref=source_ref,
                        edge_kind="VARIABLE_REFERENCE",
                        target_kind="VARIABLE",
                        selector_form="NONE",
                        selector=None,
                        selector_status="ABSENT",
                        selector_digest=_text_digest(key, "selector", value),
                        unresolvable="UNRESOLVED_MALFORMED",
                    )
                )

        merged: dict[tuple[str, str, str, str, str | None], _EdgeDraft] = {}
        for draft in drafts:
            existing = merged.get(draft.identity)
            if existing is None:
                merged[draft.identity] = draft
            else:
                existing.occurrences = min(existing.occurrences + 1, 100_000)
        scope.edges.extend(merged.values())

    def _resolve(self, scope: _Scope) -> list[GtmReferenceEdge]:
        edges: list[GtmReferenceEdge] = []
        for draft in scope.edges:
            if self._emission_budget <= 0:
                # Past the hard capacity an edge really is lost. It is counted
                # here and reported by this scope's `reference_edges` entry.
                scope.dropped_edges += 1
                scope.edge_bound_exceeded = True
                continue
            self._emission_budget -= 1
            if self._edge_budget <= 0:
                # The declared bound was crossed. Nothing is dropped yet, but
                # the reference set is no longer complete within the bounds this
                # reader declares, so it says so instead of implying coverage.
                scope.edge_bound_exceeded = True
            else:
                self._edge_budget -= 1
            scope.emitted_edges += 1
            target_ref: str | None = None
            resolution: GtmEdgeResolution = draft.unresolvable or "UNRESOLVED_NO_MATCHING_RESOURCE"
            if draft.unresolvable is None:
                if draft.target_kind == "CHILD_CONTAINER":
                    resolution = "OUT_OF_SCOPE_CHILD_CONTAINER"
                elif draft.selector_form in ("NATIVE_ID", "TEMPLATE_TYPE"):
                    for candidate in _RESOLVABLE_KINDS.get(draft.target_kind, ()):
                        found = (
                            scope.by_id.get((candidate, draft.native_selector))
                            if draft.native_selector is not None
                            else None
                        )
                        if found is not None:
                            target_ref, resolution = found, "RESOLVED"
                            break
                elif draft.match_digest is not None:
                    # Name matching runs on the `name`-domain digest, never on
                    # the selector text, so a withheld oversize or sensitive
                    # name resolves exactly like a name kept verbatim.
                    matches: list[str] = []
                    for candidate in _RESOLVABLE_KINDS.get(draft.target_kind, ()):
                        matches.extend(
                            scope.by_name_digest.get((candidate, draft.match_digest), [])
                        )
                    unique = sorted(set(matches))
                    if len(unique) == 1:
                        target_ref, resolution = unique[0], "RESOLVED"
                    elif len(unique) > 1:
                        resolution = "UNRESOLVED_AMBIGUOUS_NAME"
            selector = draft.selector
            selector_status = draft.selector_status
            if draft.selector_form == "NAME" and resolution != "RESOLVED":
                # A name-form selector is the only free-text channel an edge
                # has. Unless it matched a resource this read actually saw, it
                # is arbitrary customer configuration -- often a fragment of
                # custom code -- and only its digest survives (AGENTS rule 13).
                selector = None
                selector_status = "WITHHELD_UNRESOLVED_REFERENCE"
            edges.append(
                GtmReferenceEdge(
                    scope_kind=scope.kind,
                    scope_ref=scope.scope_ref,
                    source_ref=draft.source_ref,
                    edge_kind=draft.edge_kind,
                    target_kind=draft.target_kind,
                    selector_form=draft.selector_form,
                    selector=selector,
                    selector_status=selector_status,
                    selector_digest=draft.selector_digest,
                    target_ref=target_ref,
                    resolution=resolution,
                    occurrence_count=draft.occurrences,
                )
            )
        return edges


def _edge_order(edge: GtmReferenceEdge) -> tuple[str, str, str, str, str, str]:
    return (
        edge.scope_ref,
        edge.source_ref,
        edge.edge_kind,
        edge.target_kind,
        edge.selector_form,
        edge.selector_digest or "",
    )


def _custom_code_text(value: str) -> GtmCodeClassification | None:
    """Classify one free-text value as script-bearing, by shape alone.

    This is the bounded heuristic half of `_classify_parameter`, factored out so
    that a value the module would call custom code can never be published as
    text anywhere else -- including as a reference edge's selector.  It is not a
    proof that anything it declines is script-free.
    """

    if len(value) > _MAX_SCANNED_STRING:
        # An oversize value cannot be proven script-free within the scan bound,
        # so it is classified conservatively and hashed rather than ignored.
        return "CUSTOM_HTML"
    if _SCRIPT_HTML.search(value):
        return "CUSTOM_HTML"
    if _SCRIPT_URI.search(value):
        return "CUSTOM_JAVASCRIPT"
    return None


def _field_status(raw: Any, kept: str | None) -> GtmFieldStatus:
    """Say whether a documented scalar was kept, never supplied, or unusable."""

    if kept is not None:
        return "PRESENT"
    return "ABSENT" if raw is None else "WITHHELD_UNSUPPORTED"


def _ordered_base(base: GtmReadInventory) -> GtmReadInventory:
    """Return the fixed read with every provider-ordered list sorted.

    `GtmReadInventory` keeps provider order, so one unchanged container read
    twice can produce two different canonical payloads and therefore two
    different retained `snapshot_digest` values.  The retained complete
    inventory sorts them the same way the default `measurement gtm` path sorts
    workspaces before retention, so the observation depends on the container
    rather than on page order.
    """

    return base.model_validate(
        {
            **base.model_dump(),
            "workspaces": tuple(sorted(base.workspaces, key=lambda item: item.workspace_ref)),
            "live": {
                **base.live.model_dump(),
                "opaque_code": tuple(
                    sorted(
                        base.live.opaque_code,
                        key=lambda item: (item.kind, item.configuration_fingerprint),
                    )
                ),
            },
            "latest": {
                **base.latest.model_dump(),
                "opaque_code": tuple(
                    sorted(
                        base.latest.opaque_code,
                        key=lambda item: (item.kind, item.configuration_fingerprint),
                    )
                ),
            },
        }
    )


def _classify_parameter(
    kind: GtmResourceKind,
    provider_type: str | None,
    parameter_key: str | None,
    value: str,
) -> GtmCodeClassification | None:
    """Classify one documented parameter value as custom code, or not at all.

    The two exact cases are documented GTM resource types: a `html` tag carries
    its markup in the `html` parameter and a `jsm` variable carries its function
    in the `javascript` parameter.  Everything else is a bounded, explicitly
    heuristic scan for embedded script; it is never a proof of safety.
    """

    if kind == "TAG" and provider_type == "html" and parameter_key == "html":
        return "CUSTOM_HTML"
    if kind == "VARIABLE" and provider_type == "jsm" and parameter_key == "javascript":
        return "CUSTOM_JAVASCRIPT"
    return _custom_code_text(value)


# A `Trigger` carries its values under `filter`, `customEventFilter` and
# `autoEventFilter` as arrays of `Condition`, each with its own `parameter`
# array (fetched 2026-09-09).  Those values are as free-form as a tag's own
# parameters, so they are classified and counted the same way.
_CONDITION_FIELDS: tuple[str, ...] = ("filter", "customEventFilter", "autoEventFilter")


def _classifiable_parameters(raw: dict[str, Any], *, budget: list[int]) -> Iterator[dict[str, Any]]:
    """Yield every documented `Parameter` whose value may carry custom code."""

    yield from _parameters(raw.get("parameter"), budget=budget)
    for field in _CONDITION_FIELDS:
        conditions = raw.get(field)
        if not isinstance(conditions, list):
            continue
        for condition in conditions:
            if budget[0] <= 0:
                return
            if isinstance(condition, dict):
                yield from _parameters(condition.get("parameter"), budget=budget)


def _parameters(value: Any, *, budget: list[int]) -> Iterator[dict[str, Any]]:
    """Yield documented `Parameter` objects, including nested list/map members."""

    if budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(value, list):
        for item in value:
            yield from _parameters(item, budget=budget)
    elif isinstance(value, dict):
        yield value
        for nested in ("list", "map"):
            yield from _parameters(value.get(nested), budget=budget)

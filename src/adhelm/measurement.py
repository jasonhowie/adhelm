"""Pure local validators for GTM, consent, and conversion-measurement contracts.

This module deliberately models evidence and inventory supplied by a real adapter;
it neither calls a provider nor manufactures a successful browser or receipt result.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field, field_validator, model_validator

from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import SafeRef, StrictModel

if TYPE_CHECKING:  # The retained-inventory adapter below is the only consumer.
    from adhelm.gtm_inventory import GtmFullInventory


class ResourceSurface(StrEnum):
    TAG = "TAG"
    TRIGGER = "TRIGGER"
    VARIABLE = "VARIABLE"
    BUILT_IN_VARIABLE = "BUILT_IN_VARIABLE"
    TEMPLATE = "TEMPLATE"
    FOLDER = "FOLDER"
    GOOGLE_TAG_CONFIGURATION = "GOOGLE_TAG_CONFIGURATION"
    ZONE = "ZONE"
    CLIENT = "CLIENT"
    TRANSFORMATION = "TRANSFORMATION"
    CUSTOM_HTML = "CUSTOM_HTML"
    CUSTOM_TEMPLATE = "CUSTOM_TEMPLATE"


class EdgeStatus(StrEnum):
    PROVEN = "PROVEN"
    DYNAMIC_OR_UNKNOWN = "DYNAMIC_OR_UNKNOWN"
    BROKEN = "BROKEN"


class ReferenceStatus(StrEnum):
    PROVEN_UNREFERENCED = "PROVEN_UNREFERENCED"
    REFERENCED = "REFERENCED"
    DYNAMIC_OR_UNKNOWN = "DYNAMIC_OR_UNKNOWN"
    BROKEN = "BROKEN"
    INVENTORY_INCOMPLETE = "INVENTORY_INCOMPLETE"


class ConsentState(StrEnum):
    DENIED = "DENIED"
    GRANTED = "GRANTED"


class EvidenceKind(StrEnum):
    CONFIGURATION = "CONFIGURATION"
    QUICK_PREVIEW = "QUICK_PREVIEW"
    BROWSER_NETWORK = "BROWSER_NETWORK"
    NATIVE_RECEIPT = "NATIVE_RECEIPT"
    PROCESSED_REPORT = "PROCESSED_REPORT"
    OPTIMIZATION_ASSOCIATION = "OPTIMIZATION_ASSOCIATION"


_REF_PATTERN = r"^[A-Za-z0-9_.:/-]{1,160}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_DOMAIN_PATTERN = r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
_PATH_PATTERN = r"^/[A-Za-z0-9_./-]{0,255}$"
_EVENT_PATTERN = r"^[a-z][a-z0-9_]{0,79}$"
_FIELD_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "email",
        "phone",
        "name",
        "user_id",
        "client_id",
        "gclid",
        "fbclid",
        "form_value",
        "form_values",
        "dom_text",
        "page_text",
        "html",
        "url",
        "query",
        "cookie",
        "token",
    }
)


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    return values


class ResourceNode(StrictModel):
    resource_ref: SafeRef
    surface: ResourceSurface
    fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    context_ref: SafeRef


class DependencyEdge(StrictModel):
    source_ref: SafeRef
    target_ref: SafeRef | None = None
    status: EdgeStatus
    context_ref: SafeRef

    @model_validator(mode="after")
    def target_matches_edge_kind(self) -> DependencyEdge:
        if self.status is EdgeStatus.DYNAMIC_OR_UNKNOWN and self.target_ref is not None:
            raise ValueError("dynamic references cannot claim a resolved target")
        if self.status in (EdgeStatus.PROVEN, EdgeStatus.BROKEN) and self.target_ref is None:
            raise ValueError("resolved and broken edges require their target reference")
        return self


class WorkspaceInventory(StrictModel):
    workspace_ref: SafeRef
    workspace_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    accessible: bool
    pending_change_count: int = Field(ge=0)


class InventoryCoverage(StrictModel):
    context_ref: SafeRef
    surface: ResourceSurface
    inventory_digest: str = Field(pattern=_DIGEST_PATTERN)


class ContainerInventory(StrictModel):
    account_ref: SafeRef
    container_ref: SafeRef
    live_version_ref: SafeRef
    latest_version_ref: SafeRef
    accessible_workspace_refs: tuple[SafeRef, ...]
    workspaces: tuple[WorkspaceInventory, ...]
    inventoried_context_refs: tuple[SafeRef, ...]
    inventory_coverage: tuple[InventoryCoverage, ...]
    applicable_surfaces: tuple[ResourceSurface, ...]
    inventoried_surfaces: tuple[ResourceSurface, ...]
    nodes: tuple[ResourceNode, ...]
    edges: tuple[DependencyEdge, ...]
    live_inventory_complete: bool
    latest_inventory_complete: bool
    workspace_inventory_complete: bool
    workspace_limit: int = Field(ge=1)

    @model_validator(mode="after")
    def complete_workspace_and_surface_declarations(self) -> ContainerInventory:
        _unique(tuple(self.accessible_workspace_refs), "accessible workspace references")
        workspace_refs = tuple(workspace.workspace_ref for workspace in self.workspaces)
        _unique(workspace_refs, "workspace references")
        actual_accessible = tuple(
            workspace.workspace_ref for workspace in self.workspaces if workspace.accessible
        )
        if set(actual_accessible) != set(self.accessible_workspace_refs):
            raise ValueError("accessible workspace inventory must exactly match discovery")
        if len(self.applicable_surfaces) != len(set(self.applicable_surfaces)):
            raise ValueError("applicable surfaces must be unique")
        if len(self.inventoried_surfaces) != len(set(self.inventoried_surfaces)):
            raise ValueError("inventoried surfaces must be unique")
        _unique(tuple(self.inventoried_context_refs), "inventoried contexts")
        expected_contexts = {"live", "latest", *self.accessible_workspace_refs}
        if set(self.inventoried_context_refs) != expected_contexts:
            raise ValueError(
                "inventory contexts must cover live, latest, and every accessible workspace"
            )
        coverage_pairs = tuple(
            (coverage.context_ref, coverage.surface) for coverage in self.inventory_coverage
        )
        if len(coverage_pairs) != len(set(coverage_pairs)):
            raise ValueError("each inventory context and surface may be covered once")
        node_refs = tuple(node.resource_ref for node in self.nodes)
        if len(node_refs) != len(set(node_refs)):
            raise ValueError("resource nodes must be unique")
        return self

    @property
    def full_dependency_inventory(self) -> bool:
        return (
            self.live_inventory_complete
            and self.latest_inventory_complete
            and self.workspace_inventory_complete
            and set(self.applicable_surfaces) == set(self.inventoried_surfaces)
            and set(self.inventoried_context_refs)
            == {"live", "latest", *self.accessible_workspace_refs}
            and {(coverage.context_ref, coverage.surface) for coverage in self.inventory_coverage}
            == {
                (context_ref, surface)
                for context_ref in ("live", "latest", *self.accessible_workspace_refs)
                for surface in self.applicable_surfaces
            }
            and len(self.workspaces) == len(self.accessible_workspace_refs)
        )

    @property
    def has_free_workspace_slot(self) -> bool:
        return len(self.workspaces) < self.workspace_limit

    def reference_status(self, resource_ref: str) -> ReferenceStatus:
        if not self.full_dependency_inventory:
            return ReferenceStatus.INVENTORY_INCOMPLETE
        if any(edge.status is EdgeStatus.DYNAMIC_OR_UNKNOWN for edge in self.edges):
            return ReferenceStatus.DYNAMIC_OR_UNKNOWN
        if any(edge.status is EdgeStatus.BROKEN for edge in self.edges):
            return ReferenceStatus.BROKEN
        if any(edge.target_ref == resource_ref for edge in self.edges):
            return ReferenceStatus.REFERENCED
        if resource_ref not in {node.resource_ref for node in self.nodes}:
            return ReferenceStatus.INVENTORY_INCOMPLETE
        return ReferenceStatus.PROVEN_UNREFERENCED


class StageGate(StrictModel):
    base_live_version_ref: SafeRef
    dedicated_workspace_required: bool = True
    existing_workspace_reuse_allowed: bool = False


def validate_stage_gate(inventory: ContainerInventory) -> StageGate:
    """Require a clean, fully inventoried container before allocating a new workspace."""

    if not inventory.full_dependency_inventory:
        raise AdhelmError(
            "INVENTORY_INCOMPLETE",
            "Complete live, latest, workspace, and applicable-surface inventory first.",
            exit_code=ExitCode.PRECONDITION,
        )
    if inventory.live_version_ref != inventory.latest_version_ref:
        raise AdhelmError(
            "WORKSPACE_CONFLICT",
            "Live and latest container versions differ; reconcile explicitly before staging.",
            exit_code=ExitCode.PRECONDITION,
        )
    if not inventory.has_free_workspace_slot:
        raise AdhelmError(
            "WORKSPACE_SLOT_REQUIRED",
            "No dedicated GTM workspace slot is available.",
            exit_code=ExitCode.PRECONDITION,
        )
    return StageGate(base_live_version_ref=inventory.live_version_ref)


class ConversionSemantics(StrictModel):
    semantics_ref: SafeRef
    customer_definition: str = Field(min_length=5, max_length=500)
    counting_rule: str = Field(min_length=3, max_length=160)


class EventField(StrictModel):
    name: str = Field(pattern=_FIELD_PATTERN)
    value_type: str = Field(pattern=r"^(BOOLEAN|INTEGER|LOW_CARDINALITY_STRING)$")
    required: bool = False

    @field_validator("name")
    @classmethod
    def declared_non_sensitive_field(cls, value: str) -> str:
        if value in _FORBIDDEN_EVENT_FIELDS:
            raise ValueError("event fields cannot carry sensitive, DOM, or form data")
        return value


class EventContract(StrictModel):
    event_contract_ref: SafeRef
    event_name: str = Field(pattern=_EVENT_PATTERN)
    semantics: ConversionSemantics
    fields: tuple[EventField, ...] = ()

    @model_validator(mode="after")
    def semantic_and_field_contract_are_explicit(self) -> EventContract:
        if (
            not self.semantics.customer_definition.strip()
            or not self.semantics.counting_rule.strip()
        ):
            raise ValueError("customer-defined conversion semantics are required")
        names = tuple(field.name for field in self.fields)
        _unique(names, "event field names")
        return self


class ConsentContract(StrictModel):
    consent_contract_ref: SafeRef
    allowed_public_hosts: tuple[str, ...] = Field(min_length=1)
    allowed_public_paths: tuple[str, ...] = Field(min_length=1)
    approved_outbound_domains: tuple[str, ...] = Field(min_length=1)
    denied_allowed_outbound_domains: tuple[str, ...] = ()
    tags_allowed_when_denied: tuple[SafeRef, ...] = ()
    tags_allowed_when_granted: tuple[SafeRef, ...] = Field(min_length=1)

    @field_validator(
        "allowed_public_hosts", "approved_outbound_domains", "denied_allowed_outbound_domains"
    )
    @classmethod
    def safe_domains(cls, domains: tuple[str, ...]) -> tuple[str, ...]:
        for domain in domains:
            if not re.fullmatch(_DOMAIN_PATTERN, domain):
                raise ValueError("endpoint domains must be lower-case hostnames")
        return _unique(domains, "endpoint domains")

    @field_validator("allowed_public_paths")
    @classmethod
    def safe_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_PATH_PATTERN, path) is None for path in paths):
            raise ValueError("public routes must be declared paths without query or fragment")
        return _unique(paths, "public routes")


class EvidenceItem(StrictModel):
    kind: EvidenceKind
    evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    event_contract_ref: SafeRef
    consent_contract_ref: SafeRef
    consent_state: ConsentState | None = None
    occurrence_count: int | None = Field(default=None, ge=0, le=1)
    outbound_domains: tuple[str, ...] = ()
    request_field_names: tuple[str, ...] = ()
    browser_host: str | None = None
    browser_path: str | None = None

    @field_validator("outbound_domains")
    @classmethod
    def safe_evidence_domains(cls, domains: tuple[str, ...]) -> tuple[str, ...]:
        for domain in domains:
            if not re.fullmatch(_DOMAIN_PATTERN, domain):
                raise ValueError("evidence domains must be lower-case hostnames")
        return _unique(domains, "evidence domains")

    @field_validator("request_field_names")
    @classmethod
    def safe_request_field_names(cls, fields: tuple[str, ...]) -> tuple[str, ...]:
        for field in fields:
            if not re.fullmatch(_FIELD_PATTERN, field) or field in _FORBIDDEN_EVENT_FIELDS:
                raise ValueError("evidence cannot contain sensitive or undeclared field names")
        return _unique(fields, "request field names")

    @field_validator("browser_host")
    @classmethod
    def safe_browser_host(cls, host: str | None) -> str | None:
        if host is not None and re.fullmatch(_DOMAIN_PATTERN, host) is None:
            raise ValueError("browser host must be a lower-case hostname")
        return host

    @field_validator("browser_path")
    @classmethod
    def safe_browser_path(cls, path: str | None) -> str | None:
        if path is not None and re.fullmatch(_PATH_PATTERN, path) is None:
            raise ValueError("browser path must be a declared path")
        return path


class PublishAuthorization(StrictModel):
    publish_scope_granted: bool
    immutable_version_digest: str = Field(pattern=_DIGEST_PATTERN)
    base_live_version_ref: SafeRef
    event_contract_ref: SafeRef
    consent_contract_ref: SafeRef
    browser_host: str = Field(pattern=_DOMAIN_PATTERN)
    browser_path: str = Field(pattern=_PATH_PATTERN)


class PublishState(StrictModel):
    live_version_ref: SafeRef
    latest_version_ref: SafeRef
    created_version_ref: SafeRef
    created_version_digest: str = Field(pattern=_DIGEST_PATTERN)
    browser_tested_digest: str = Field(pattern=_DIGEST_PATTERN)


def validate_publish_gate(
    *,
    authorization: PublishAuthorization,
    state: PublishState,
    event: EventContract,
    consent: ConsentContract,
    evidence: tuple[EvidenceItem, ...],
) -> None:
    """Validate local facts required before a separately authorized GTM publish."""

    if not authorization.publish_scope_granted:
        raise AdhelmError(
            "PUBLISH_SCOPE_REQUIRED", "Publishing requires separate publish authority."
        )
    if (
        authorization.event_contract_ref != event.event_contract_ref
        or authorization.consent_contract_ref != consent.consent_contract_ref
    ):
        raise AdhelmError(
            "PUBLISH_CONTRACT_MISMATCH",
            "Publish authority does not bind the declared event and consent contracts.",
        )
    if (
        state.created_version_digest != state.browser_tested_digest
        or state.created_version_digest != authorization.immutable_version_digest
    ):
        raise AdhelmError(
            "VERSION_DIGEST_MISMATCH", "The immutable version is not the browser-tested version."
        )
    if (
        state.live_version_ref != authorization.base_live_version_ref
        or state.latest_version_ref != state.created_version_ref
    ):
        raise AdhelmError(
            "REMOTE_DRIFT", "Live or latest GTM version differs from the approved publish state."
        )
    validate_measurement_evidence(event=event, consent=consent, evidence=evidence)
    if not any(
        item.kind is EvidenceKind.BROWSER_NETWORK
        and item.consent_state is ConsentState.GRANTED
        and item.browser_host == authorization.browser_host
        and item.browser_path == authorization.browser_path
        for item in evidence
    ):
        raise AdhelmError(
            "PUBLISH_BROWSER_BINDING_MISMATCH",
            "Publish authority is not bound to the approved browser test route.",
        )


def validate_measurement_evidence(
    *, event: EventContract, consent: ConsentContract, evidence: tuple[EvidenceItem, ...]
) -> None:
    """Require separate configuration, browser, native, processed, and optimization proof."""

    kinds = {item.kind for item in evidence}
    required = {
        EvidenceKind.CONFIGURATION,
        EvidenceKind.BROWSER_NETWORK,
        EvidenceKind.NATIVE_RECEIPT,
        EvidenceKind.PROCESSED_REPORT,
        EvidenceKind.OPTIMIZATION_ASSOCIATION,
    }
    missing = required - kinds
    if missing:
        raise AdhelmError(
            "MEASUREMENT_EVIDENCE_INCOMPLETE",
            "Required measurement verification evidence is missing.",
        )
    browser_states: set[ConsentState] = set()
    for item in evidence:
        if (
            item.event_contract_ref != event.event_contract_ref
            or item.consent_contract_ref != consent.consent_contract_ref
        ):
            raise AdhelmError(
                "EVIDENCE_CONTRACT_MISMATCH",
                "Evidence belongs to another event or consent contract.",
            )
        if item.kind is EvidenceKind.QUICK_PREVIEW:
            continue
        if item.kind is EvidenceKind.BROWSER_NETWORK:
            _validate_browser_evidence(item, event, consent)
            if item.consent_state is not None:
                browser_states.add(item.consent_state)
    if browser_states != {ConsentState.DENIED, ConsentState.GRANTED}:
        raise AdhelmError(
            "CONSENT_EVIDENCE_INCOMPLETE",
            "Measurement verification requires both denied and granted consent browser tests.",
        )


def _validate_browser_evidence(
    item: EvidenceItem, event: EventContract, consent: ConsentContract
) -> None:
    if item.consent_state is None or item.occurrence_count is None:
        raise AdhelmError(
            "BROWSER_EVIDENCE_INCOMPLETE",
            "Browser evidence needs consent state and event occurrence.",
        )
    if (
        item.browser_host not in consent.allowed_public_hosts
        or item.browser_path not in consent.allowed_public_paths
    ):
        raise AdhelmError(
            "UNAPPROVED_EGRESS",
            "Browser evidence used an undeclared public marketing route.",
        )
    declared_fields = {field.name for field in event.fields}
    if not set(item.request_field_names).issubset(declared_fields):
        raise AdhelmError(
            "UNDECLARED_EVENT_FIELD", "Browser evidence contains an undeclared event field."
        )
    allowed_domains = (
        set(consent.denied_allowed_outbound_domains)
        if item.consent_state is ConsentState.DENIED
        else set(consent.approved_outbound_domains)
    )
    if not set(item.outbound_domains).issubset(allowed_domains):
        raise AdhelmError(
            "CONSENT_DENIED_EGRESS",
            "Observed browser egress is not allowed by the consent contract.",
        )
    if item.consent_state is ConsentState.DENIED and item.occurrence_count != 0:
        raise AdhelmError(
            "CONSENT_DENIED_REQUEST", "A denied-consent browser test emitted a measurement event."
        )
    if item.consent_state is ConsentState.GRANTED and item.occurrence_count != 1:
        raise AdhelmError(
            "DUPLICATE_FIRING", "A granted-consent browser test must show exactly one event."
        )


# --------------------------------------------- retained GTM inventory adapter
#
# The validators above are pure and take a `ContainerInventory` the caller
# supplies.  The adapter below builds that structure from a *retained* GTM
# observation instead, so the stage gate's "live equals latest, every workspace
# inventoried, every applicable surface read" checks run on evidence this
# product actually read rather than on a hand-written declaration.  It is
# additive: it changes no existing validator's behaviour for any existing input.

#: Which GTM collection maps onto which stage-gate resource surface.  Only
#: collections that are a listable resource surface appear.  `destinations` and
#: `environments` are documented at container scope and have no `ResourceSurface`
#: member; `workspace_changes` and `reference_edges` are statuses, not
#: collections.  `CUSTOM_HTML` and `CUSTOM_TEMPLATE` are code surfaces inside a
#: tag or template rather than separate collections, so they are covered by
#: `TAG` and `TEMPLATE` and are deliberately not claimed as separate surfaces.
_GTM_SURFACES: dict[str, ResourceSurface] = {
    "tags": ResourceSurface.TAG,
    "triggers": ResourceSurface.TRIGGER,
    "variables": ResourceSurface.VARIABLE,
    "built_in_variables": ResourceSurface.BUILT_IN_VARIABLE,
    "templates": ResourceSurface.TEMPLATE,
    "folders": ResourceSurface.FOLDER,
    "gtag_configs": ResourceSurface.GOOGLE_TAG_CONFIGURATION,
    "zones": ResourceSurface.ZONE,
    "clients": ResourceSurface.CLIENT,
    "transformations": ResourceSurface.TRANSFORMATION,
}
#: The stage-gate context names `ContainerInventory` requires for the two
#: container versions.  Workspaces keep their own references.
_LIVE_CONTEXT = "live"
_LATEST_CONTEXT = "latest"


def container_inventory_from_gtm_inventory(
    inventory: GtmFullInventory, *, workspace_limit: int
) -> ContainerInventory:
    """Project one retained GTM full inventory onto the stage-gate structure.

    `workspace_limit` is the one value the Tag Manager API does not expose: a
    container's workspace allowance depends on the customer's Tag Manager
    service tier, and no documented `GET` returns it.  It is therefore a declared
    input rather than an invented one, and the caller must state it before the
    "is a dedicated workspace slot available" half of the gate can run at all.

    Every unresolved, ambiguous, malformed or out-of-scope reference edge becomes
    `DYNAMIC_OR_UNKNOWN`, never `BROKEN`: a `BROKEN` edge in this model must name
    the target it failed to reach, and an unresolved GTM reference by definition
    names none.  That is the fail-closed direction -- `reference_status` then
    reports `DYNAMIC_OR_UNKNOWN` and never `PROVEN_UNREFERENCED`.  The real
    classification lives in `adhelm.gtm_dependency_graph`; this projection exists
    for the stage gate.
    """

    from adhelm.canonical import digest
    from adhelm.gtm_inventory import GtmFullInventory as _GtmFullInventory

    if not isinstance(inventory, _GtmFullInventory):
        raise AdhelmError(
            "INVALID_STAGE_GATE_INPUT",
            "The stage gate requires a retained GTM full inventory observation.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if type(workspace_limit) is not int or not 1 <= workspace_limit <= 1_000:
        raise AdhelmError(
            "WORKSPACE_LIMIT_REQUIRED",
            "A declared container workspace limit between 1 and 1000 is required.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Declare the container's Tag Manager workspace allowance.",
        )

    live_ref = inventory.live_latest.live_version_ref
    latest_ref = inventory.live_latest.latest_version_ref
    workspace_refs = tuple(
        sorted(workspace.workspace_ref for workspace in inventory.base.workspaces)
    )
    # A container whose live version *is* its latest version reads both version
    # scopes under one reference.  The gate still needs both declared contexts,
    # so the scope is projected onto both, while each node and edge keeps a
    # single context: `ContainerInventory` requires unique node references, and
    # duplicating one resource under two context names would misreport the
    # container as holding it twice.
    scope_contexts: list[tuple[str, str]] = [
        (live_ref, _LIVE_CONTEXT),
        (latest_ref, _LATEST_CONTEXT),
        *((reference, reference) for reference in workspace_refs),
    ]
    context_of_scope: dict[str, str] = {}
    for scope_ref, context in scope_contexts:
        context_of_scope.setdefault(scope_ref, context)

    pending = {
        item.scope_ref: item.resource_count
        for item in inventory.coverage
        if item.collection == "workspace_changes" and item.status != "NOT_DOCUMENTED"
    }
    workspaces = tuple(
        WorkspaceInventory(
            workspace_ref=workspace.workspace_ref,
            workspace_fingerprint=workspace.configuration_fingerprint,
            accessible=True,
            pending_change_count=pending.get(workspace.workspace_ref, 0),
        )
        for workspace in sorted(inventory.base.workspaces, key=lambda item: item.workspace_ref)
    )

    coverage: list[InventoryCoverage] = []
    complete: dict[str, bool] = {context: True for _, context in scope_contexts}
    seen_pairs: set[tuple[str, ResourceSurface]] = set()
    for scope_ref, context in scope_contexts:
        for item in inventory.coverage:
            if item.scope_ref != scope_ref:
                continue
            surface = _GTM_SURFACES.get(item.collection)
            if surface is not None:
                if (context, surface) not in seen_pairs:
                    seen_pairs.add((context, surface))
                    coverage.append(
                        InventoryCoverage(
                            context_ref=context,
                            surface=surface,
                            inventory_digest=digest(item),
                        )
                    )
                if item.status != "COMPLETE":
                    complete[context] = False
            # A reference-edge or workspace-status gap is not a resource surface,
            # but it is still a gap in the same context, so it fails that context
            # closed rather than being dropped with the collection.
            elif item.collection in (
                "reference_edges",
                "workspace_changes",
            ) and item.status not in (
                "COMPLETE",
                "NOT_DOCUMENTED",
            ):
                complete[context] = False
    if inventory.reference_coverage != "COMPLETE_WITHIN_DECLARED_BOUNDS":
        complete = dict.fromkeys(complete, False)

    nodes = tuple(
        ResourceNode(
            resource_ref=resource.resource_ref,
            surface=_GTM_SURFACES[resource.collection],
            fingerprint=resource.configuration_fingerprint,
            context_ref=context_of_scope[resource.scope_ref],
        )
        for resource in sorted(inventory.resources, key=lambda item: item.resource_ref)
        if resource.collection in _GTM_SURFACES and resource.scope_ref in context_of_scope
    )
    edges = tuple(
        DependencyEdge(
            source_ref=edge.source_ref,
            target_ref=edge.target_ref if edge.resolution == "RESOLVED" else None,
            status=EdgeStatus.PROVEN
            if edge.resolution == "RESOLVED"
            else EdgeStatus.DYNAMIC_OR_UNKNOWN,
            context_ref=context_of_scope[edge.scope_ref],
        )
        for edge in inventory.edges
        if edge.scope_ref in context_of_scope
    )

    return ContainerInventory(
        account_ref=inventory.identity.account_ref,
        container_ref=inventory.identity.container_ref,
        live_version_ref=live_ref,
        latest_version_ref=latest_ref,
        accessible_workspace_refs=workspace_refs,
        workspaces=workspaces,
        inventoried_context_refs=(_LIVE_CONTEXT, _LATEST_CONTEXT, *workspace_refs),
        inventory_coverage=tuple(coverage),
        applicable_surfaces=tuple(_GTM_SURFACES.values()),
        inventoried_surfaces=tuple(_GTM_SURFACES.values()),
        nodes=nodes,
        edges=edges,
        live_inventory_complete=complete[_LIVE_CONTEXT],
        latest_inventory_complete=complete[_LATEST_CONTEXT],
        workspace_inventory_complete=(
            all(complete[reference] for reference in workspace_refs)
            and not inventory.completeness.uninspected_workspaces
        ),
        workspace_limit=workspace_limit,
    )


def validate_gtm_stage_gate(inventory: GtmFullInventory, *, workspace_limit: int) -> StageGate:
    """Run the stage gate over a retained GTM observation, failing closed on any gap.

    This is `validate_stage_gate` over `container_inventory_from_gtm_inventory`,
    with one extra precondition in front of it: the observation's own
    `completeness` must be `COMPLETE_WITHIN_DECLARED_BOUNDS`.  That status is
    `INCOMPLETE` whenever a collection was bounded, a listed workspace was left
    uninspected, *or* an emitted reference edge did not resolve.  An unresolved
    reference is not a coverage gap, but it does mean the container's reference
    set is not proven, and staging into a container whose references this
    product cannot follow is exactly the fail-open direction rule 9 forbids.
    """

    if inventory.live_latest.status != "LIVE_EQUALS_LATEST":
        # Rule 9 requires *proof* that live and latest match. A refusal to decide
        # (UNDETERMINABLE) is not a proof and fails closed exactly like a mismatch.
        raise AdhelmError(
            "WORKSPACE_CONFLICT",
            "The retained GTM observation does not prove that the live and latest "
            f"versions match ({inventory.live_latest.status}).",
            exit_code=ExitCode.PRECONDITION,
            next_action="Re-read the container after its live and latest versions agree.",
        )
    if inventory.counts.workspace_merge_conflicts:
        raise AdhelmError(
            "WORKSPACE_CONFLICT",
            "The retained GTM observation reports a workspace merge conflict; staging "
            "must not silently resolve customer work.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Resolve the conflict in GTM, then re-read the container.",
        )
    if inventory.completeness.status != "COMPLETE_WITHIN_DECLARED_BOUNDS":
        raise AdhelmError(
            "INVENTORY_INCOMPLETE",
            "The retained GTM observation records an incomplete or unresolved read.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Re-read the container until every collection, workspace and "
            "reference is complete before staging.",
        )
    return validate_stage_gate(
        container_inventory_from_gtm_inventory(inventory, workspace_limit=workspace_limit)
    )

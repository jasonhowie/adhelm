"""GTM dependency graph and `PROVEN_UNREFERENCED` classification over a retained inventory.

This module is pure.  It reads one already-retained `GtmFullInventory`
observation and computes a scoped dependency graph plus one reference
classification per resource per scope.  It makes no provider request, reads no
credential, appends no ledger record, and stages, publishes or deletes nothing.

The classification fails closed by construction.  `docs/measurement-control.md`
states that a resource is `PROVEN_UNREFERENCED` only when *every* applicable
resource surface, supported reference form, and accessible pending workspace has
been inventoried across live and latest versions and no edge reaches it.  So a
proof of absence is refused whenever the observation cannot support it:

* any collection coverage entry in the scope is incomplete;
* any collection coverage entry anywhere in the container is incomplete, or a
  listed workspace was never inspected (the deletion rule is container-wide, not
  scope-local: a resource must be unreferenced in live, latest *and* every
  accessible workspace before it may be removed);
* `GtmFullInventory.reference_coverage` is `BOUNDED_INCOMPLETE`, meaning the
  declared reference-edge bound was crossed and a referring edge may have been
  bounded away;
* any edge in the scope is `UNRESOLVED_*` -- an unresolved, ambiguous or
  malformed reference could name any resource, so it disables the proof for the
  whole scope rather than for its apparent target alone;
* the node is a built-in variable, or a custom template whose instances this
  read could not bind; or
* live-versus-latest identity is `UNDETERMINABLE`.

Age, name, paused state, folder, and apparent inactivity are deliberately not
inputs to the classification (AGENTS rule 11).  They are audit signals, never
deletion proof, and `tests/test_gtm_dependency_graph.py` pins that.

Nothing here recommends or authorizes a deletion.  Cleanup operations are
separate later work; this module only reports what a read can and cannot prove.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.gtm_inventory import (
    GtmCollection,
    GtmEdgeKind,
    GtmEdgeResolution,
    GtmEdgeTargetKind,
    GtmFullInventory,
    GtmInventoryResource,
    GtmReferenceEdge,
    GtmResourceKind,
    GtmScopeKind,
    GtmSelectorForm,
)
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.gtm_read import GtmReadIdentity

CAPABILITY_VERSION: Final = "gtm-dependency-graph-1"

# Bounds mirror the inventory reader's own emission bounds so that any
# observation it can produce can be turned into a graph without silent loss.
MAXIMUM_NODES: Final = 10_000
MAXIMUM_EDGES: Final = 50_000
MAXIMUM_SCOPES: Final = 1_100
# How many inbound sources one node names inline.  The complete count always
# stays on the node, and every inbound edge is still an individual edge row, so
# a node above the bound is reachable by paging `edges` and matching
# `target_ref`.  Nothing is dropped; the node says when it could not inline.
MAXIMUM_INLINE_INBOUND_SOURCES: Final = 16
# Depth guard for sequencing-cycle discovery.  A container that reaches it is
# reported rather than analyzed further.
MAXIMUM_CYCLE_NODES: Final = 10_000

#: How a raw inventory edge kind reads as a dependency relation.  Every
#: documented edge kind maps to exactly one relation; a new kind is a type
#: error here rather than an unclassified edge.
GtmEdgeRelation = Literal[
    "FIRING",
    "BLOCKING",
    "ENABLING",
    "DISABLING",
    "SEQUENCING_SETUP",
    "SEQUENCING_TEARDOWN",
    "FOLDER_MEMBERSHIP",
    "ZONE_BOUNDARY",
    "TEMPLATE_INSTANCE",
    "VARIABLE_REFERENCE",
    "PARAMETER_REFERENCE",
]

_RELATIONS: Final[dict[GtmEdgeKind, GtmEdgeRelation]] = {
    "FIRING_TRIGGER": "FIRING",
    "BLOCKING_TRIGGER": "BLOCKING",
    "ENABLING_TRIGGER": "ENABLING",
    "DISABLING_TRIGGER": "DISABLING",
    "SETUP_TAG": "SEQUENCING_SETUP",
    "TEARDOWN_TAG": "SEQUENCING_TEARDOWN",
    "PARENT_FOLDER": "FOLDER_MEMBERSHIP",
    "ZONE_TRIGGER": "ZONE_BOUNDARY",
    "ZONE_CHILD_CONTAINER": "ZONE_BOUNDARY",
    "TEMPLATE_INSTANCE": "TEMPLATE_INSTANCE",
    "VARIABLE_REFERENCE": "VARIABLE_REFERENCE",
    "PARAMETER_TRIGGER_REFERENCE": "PARAMETER_REFERENCE",
    "PARAMETER_TAG_REFERENCE": "PARAMETER_REFERENCE",
}
#: The relations that order tag execution.  A cycle among them is a real
#: configuration defect, not a reference-proof input.
_SEQUENCING_RELATIONS: Final = frozenset({"SEQUENCING_SETUP", "SEQUENCING_TEARDOWN"})

#: Exact, ordered reasons a `PROVEN_UNREFERENCED` claim is refused.  The order
#: is the reporting precedence: node-specific reasons first, then the scope, then
#: the container.  A node always carries *every* applicable reason; the
#: classification names the first.
GtmProofBlocker = Literal[
    "REFERENCED_IN_ANOTHER_SCOPE",
    "BUILT_IN_RESOURCE",
    "TEMPLATE_WITH_UNBOUND_INSTANCES",
    "SCOPE_COVERAGE_INCOMPLETE",
    "SCOPE_UNRESOLVED_EDGE_PRESENT",
    "CONTAINER_COVERAGE_INCOMPLETE",
    "CONTAINER_UNRESOLVED_EDGE_PRESENT",
    "UNINSPECTED_WORKSPACE_PRESENT",
    "REFERENCE_COVERAGE_BOUNDED_INCOMPLETE",
    "LIVE_LATEST_UNDETERMINABLE",
]
_BLOCKER_ORDER: Final[tuple[GtmProofBlocker, ...]] = (
    "REFERENCED_IN_ANOTHER_SCOPE",
    "BUILT_IN_RESOURCE",
    "TEMPLATE_WITH_UNBOUND_INSTANCES",
    "SCOPE_COVERAGE_INCOMPLETE",
    "SCOPE_UNRESOLVED_EDGE_PRESENT",
    "CONTAINER_COVERAGE_INCOMPLETE",
    "CONTAINER_UNRESOLVED_EDGE_PRESENT",
    "UNINSPECTED_WORKSPACE_PRESENT",
    "REFERENCE_COVERAGE_BOUNDED_INCOMPLETE",
    "LIVE_LATEST_UNDETERMINABLE",
)

GtmReferenceClassification = Literal[
    "PROVEN_UNREFERENCED",
    "REFERENCED",
    "UNPROVABLE_BUILT_IN_RESOURCE",
    "UNPROVABLE_TEMPLATE_WITH_UNBOUND_INSTANCES",
    "UNPROVABLE_SCOPE_COVERAGE_INCOMPLETE",
    "UNPROVABLE_SCOPE_UNRESOLVED_EDGE_PRESENT",
    "UNPROVABLE_CONTAINER_COVERAGE_INCOMPLETE",
    "UNPROVABLE_CONTAINER_UNRESOLVED_EDGE_PRESENT",
    "UNPROVABLE_UNINSPECTED_WORKSPACE_PRESENT",
    "UNPROVABLE_REFERENCE_COVERAGE_BOUNDED_INCOMPLETE",
    "UNPROVABLE_LIVE_LATEST_UNDETERMINABLE",
]

#: A coverage entry with either status is not a gap: `COMPLETE` was read whole,
#: and `NOT_DOCUMENTED` means the collection does not exist at that scope and was
#: deliberately never called.
_ACCEPTABLE_COVERAGE: Final = frozenset({"COMPLETE", "NOT_DOCUMENTED"})


class GtmGraphNode(StrictModel):
    """One retained resource inside one scope, with its inbound-reference index."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    scope_ref: SafeRef
    resource_ref: SafeRef
    resource_kind: GtmResourceKind
    resource_client_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    collection: GtmCollection
    provider_type: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_folder_ref: SafeRef | None = None
    paused: bool | None = None
    custom_code_marker_count: int = Field(ge=0, le=256)
    outbound_edge_count: int = Field(ge=0, le=MAXIMUM_EDGES)
    inbound_edge_count: int = Field(ge=0, le=MAXIMUM_EDGES)
    inbound_relations: tuple[GtmEdgeRelation, ...] = Field(default_factory=tuple, max_length=11)
    inbound_source_refs: tuple[SafeRef, ...] = Field(
        default_factory=tuple, max_length=MAXIMUM_INLINE_INBOUND_SOURCES
    )
    inbound_source_ref_status: Literal["COMPLETE", "BOUNDED_PAGE_EDGES_BY_TARGET_REF"] = "COMPLETE"
    reference_status: GtmReferenceClassification
    blocking_reasons: tuple[GtmProofBlocker, ...] = Field(default_factory=tuple, max_length=9)
    #: Restated on every node so that no reader can quote a classification
    #: without the statement that it authorizes nothing.
    deletion_authority: Literal["NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"] = (
        "NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"
    )

    @model_validator(mode="after")
    def classification_matches_its_reasons(self) -> GtmGraphNode:
        if len(set(self.blocking_reasons)) != len(self.blocking_reasons):
            raise ValueError("Each GTM proof blocker is named once")
        if tuple(sorted(self.blocking_reasons, key=_BLOCKER_ORDER.index)) != self.blocking_reasons:
            raise ValueError("GTM proof blockers must be reported in their fixed precedence")
        if self.reference_status == "REFERENCED":
            # `REFERENCED` is positive evidence: an edge reaches the node.  It
            # attempts no proof of absence, so it carries no blocking reason.
            if self.inbound_edge_count < 1 or self.blocking_reasons:
                raise ValueError("A referenced GTM node cites an inbound edge and no blocker")
        elif self.reference_status == "PROVEN_UNREFERENCED":
            if self.inbound_edge_count or self.blocking_reasons:
                raise ValueError("A proven-unreferenced GTM node has no inbound edge or blocker")
        else:
            if not self.blocking_reasons:
                raise ValueError("An unprovable GTM node names why the proof was refused")
            if self.reference_status != f"UNPROVABLE_{self.blocking_reasons[0]}":
                raise ValueError("An unprovable GTM node is named by its first blocking reason")
        if len(set(self.inbound_source_refs)) != len(self.inbound_source_refs):
            raise ValueError("A GTM node names each inbound source once")
        if len(self.inbound_source_refs) > self.inbound_edge_count:
            raise ValueError("A GTM node cannot name more inbound sources than inbound edges")
        if bool(self.inbound_edge_count) != bool(self.inbound_source_refs):
            raise ValueError("A GTM node with an inbound edge names at least one source")
        # A node that could not inline every distinct source inlined exactly its
        # bound and says so; nothing is dropped, because every inbound edge is
        # still an individual edge row reachable by `target_ref`.
        if self.inbound_source_ref_status != "COMPLETE" and (
            len(self.inbound_source_refs) != MAXIMUM_INLINE_INBOUND_SOURCES
        ):
            raise ValueError("A bounded GTM inbound index inlines exactly its bound")
        return self


class GtmGraphEdge(StrictModel):
    """One retained reference edge, with its resolution preserved exactly."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    scope_ref: SafeRef
    source_ref: SafeRef
    target_ref: SafeRef | None = None
    edge_kind: GtmEdgeKind
    relation: GtmEdgeRelation
    target_kind: GtmEdgeTargetKind
    selector_form: GtmSelectorForm
    resolution: GtmEdgeResolution
    occurrence_count: int = Field(ge=1, le=100_000)
    #: True when this edge alone disables every proof of absence in its scope.
    disables_scope_proof: bool

    @model_validator(mode="after")
    def resolution_matches_target_and_effect(self) -> GtmGraphEdge:
        if (self.target_ref is not None) != (self.resolution == "RESOLVED"):
            raise ValueError("A GTM graph edge target exists exactly when it resolves")
        if self.disables_scope_proof != self.resolution.startswith("UNRESOLVED_"):
            raise ValueError("Exactly the unresolved GTM edges disable a scope proof")
        return self


class GtmSequencingCycle(StrictModel):
    """One setup/teardown sequencing cycle inside one scope."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    scope_ref: SafeRef
    member_refs: tuple[SafeRef, ...] = Field(min_length=1, max_length=32)
    member_count: int = Field(ge=1)
    member_ref_status: Literal["COMPLETE", "BOUNDED_PAGE_EDGES_BY_SCOPE"] = "COMPLETE"

    @model_validator(mode="after")
    def members_are_unique_and_counted(self) -> GtmSequencingCycle:
        if len(set(self.member_refs)) != len(self.member_refs):
            raise ValueError("A GTM sequencing cycle names each member once")
        if self.member_count < len(self.member_refs):
            raise ValueError("A GTM sequencing cycle cannot inline more members than it has")
        if (self.member_ref_status == "COMPLETE") != (self.member_count == len(self.member_refs)):
            raise ValueError("A GTM sequencing cycle states whether it inlined every member")
        return self


class GtmScopeGraph(StrictModel):
    """One readable surface's graph: a container version, a workspace, or the container."""

    schema_version: Literal[1] = 1
    scope_kind: GtmScopeKind
    #: A container whose live version is also its latest version reads one
    #: surface under one reference.  The extra kinds recorded for that same
    #: reference are named here rather than dropped, so a reader can see that
    #: this one graph is both version scopes.
    also_covers_scope_kinds: tuple[GtmScopeKind, ...] = Field(default_factory=tuple, max_length=4)
    scope_ref: SafeRef
    node_count: int = Field(ge=0, le=MAXIMUM_NODES)
    edge_count: int = Field(ge=0, le=MAXIMUM_EDGES)
    resolved_edge_count: int = Field(ge=0, le=MAXIMUM_EDGES)
    unresolved_edge_count: int = Field(ge=0, le=MAXIMUM_EDGES)
    out_of_scope_edge_count: int = Field(ge=0, le=MAXIMUM_EDGES)
    incomplete_collections: tuple[GtmCollection, ...] = Field(default_factory=tuple, max_length=16)
    proof_status: Literal["PROOF_AVAILABLE", "PROOF_BLOCKED"]
    blocking_reasons: tuple[GtmProofBlocker, ...] = Field(default_factory=tuple, max_length=9)
    proven_unreferenced_count: int = Field(ge=0, le=MAXIMUM_NODES)
    referenced_count: int = Field(ge=0, le=MAXIMUM_NODES)
    unprovable_count: int = Field(ge=0, le=MAXIMUM_NODES)
    sequencing_cycles: tuple[GtmSequencingCycle, ...] = Field(default_factory=tuple, max_length=256)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> GtmScopeGraph:
        if (
            self.proven_unreferenced_count + self.referenced_count + self.unprovable_count
            != self.node_count
        ):
            raise ValueError("Every GTM node in a scope carries exactly one classification")
        if (
            self.resolved_edge_count + self.unresolved_edge_count + self.out_of_scope_edge_count
            != self.edge_count
        ):
            raise ValueError("Every GTM edge in a scope carries exactly one resolution class")
        if (self.proof_status == "PROOF_BLOCKED") != bool(self.blocking_reasons):
            raise ValueError("A blocked GTM scope proof names its reasons")
        if self.proof_status == "PROOF_BLOCKED" and self.proven_unreferenced_count:
            raise ValueError("A blocked GTM scope proves nothing unreferenced")
        if len(set(self.blocking_reasons)) != len(self.blocking_reasons):
            raise ValueError("Each GTM scope blocker is named once")
        if tuple(sorted(self.blocking_reasons, key=_BLOCKER_ORDER.index)) != self.blocking_reasons:
            raise ValueError("GTM scope blockers must be reported in their fixed precedence")
        if self.scope_kind in self.also_covers_scope_kinds or len(
            set(self.also_covers_scope_kinds)
        ) != len(self.also_covers_scope_kinds):
            raise ValueError("A GTM scope names each additional scope kind once")
        return self


class GtmUnionClassification(StrictModel):
    """One resource identity resolved across every scope it appears in.

    A workspace graph is never mixed with a version graph by default: GTM names
    and references resolve inside one workspace or version, so a union would
    invent references that do not exist.  The caller may opt in to this
    container-wide view, which is the one a deletion decision would need: a
    resource is unreferenced for the container only when it is
    `PROVEN_UNREFERENCED` in *every* scope that holds it.
    """

    schema_version: Literal[1] = 1
    collection: GtmCollection
    resource_client_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    resource_kind: GtmResourceKind
    scope_count: int = Field(ge=1, le=MAXIMUM_SCOPES)
    scope_refs: tuple[SafeRef, ...] = Field(min_length=1, max_length=64)
    scope_ref_status: Literal["COMPLETE", "BOUNDED_PAGE_NODES_BY_CLIENT_REF"] = "COMPLETE"
    reference_status: GtmReferenceClassification
    blocking_reasons: tuple[GtmProofBlocker, ...] = Field(default_factory=tuple, max_length=9)

    @model_validator(mode="after")
    def union_is_the_most_conservative_member(self) -> GtmUnionClassification:
        if len(set(self.scope_refs)) != len(self.scope_refs):
            raise ValueError("A GTM union entry names each scope once")
        if self.scope_count < len(self.scope_refs):
            raise ValueError("A GTM union entry cannot inline more scopes than it has")
        if (self.scope_ref_status == "COMPLETE") != (self.scope_count == len(self.scope_refs)):
            raise ValueError("A GTM union entry states whether it inlined every scope")
        if self.reference_status.startswith("UNPROVABLE_"):
            if not self.blocking_reasons:
                raise ValueError("An unprovable GTM union entry names why the proof was refused")
            if self.reference_status != f"UNPROVABLE_{self.blocking_reasons[0]}":
                raise ValueError("An unprovable GTM union entry is named by its first blocker")
        elif self.blocking_reasons:
            raise ValueError("A decided GTM union entry carries no blocking reason")
        return self


class GtmDependencyGraphCounts(StrictModel):
    schema_version: Literal[1] = 1
    scopes: int = Field(ge=0, le=MAXIMUM_SCOPES)
    nodes: int = Field(ge=0, le=MAXIMUM_NODES)
    edges: int = Field(ge=0, le=MAXIMUM_EDGES)
    resolved_edges: int = Field(ge=0, le=MAXIMUM_EDGES)
    unresolved_edges: int = Field(ge=0, le=MAXIMUM_EDGES)
    out_of_scope_edges: int = Field(ge=0, le=MAXIMUM_EDGES)
    proven_unreferenced: int = Field(ge=0, le=MAXIMUM_NODES)
    referenced: int = Field(ge=0, le=MAXIMUM_NODES)
    unprovable: int = Field(ge=0, le=MAXIMUM_NODES)
    sequencing_cycles: int = Field(ge=0, le=100_000)
    built_in_variables: int = Field(ge=0, le=MAXIMUM_NODES)
    custom_code_resources: int = Field(ge=0, le=MAXIMUM_NODES)


class GtmDependencyGraph(StrictModel):
    """The complete, scoped dependency graph for one retained GTM inventory."""

    schema_version: Literal[1] = 1
    capability_version: Literal["gtm-dependency-graph-1"] = CAPABILITY_VERSION
    identity: GtmReadIdentity
    #: The exact retained inventory this graph was derived from.  A graph is
    #: reproducible from that observation alone and is never independent evidence.
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    live_latest_status: Literal["LIVE_EQUALS_LATEST", "LIVE_DIFFERS_FROM_LATEST", "UNDETERMINABLE"]
    reference_coverage: Literal["COMPLETE_WITHIN_DECLARED_BOUNDS", "BOUNDED_INCOMPLETE"]
    inventory_completeness: Literal["COMPLETE_WITHIN_DECLARED_BOUNDS", "INCOMPLETE"]
    container_blocking_reasons: tuple[GtmProofBlocker, ...] = Field(
        default_factory=tuple, max_length=9
    )
    scopes: tuple[GtmScopeGraph, ...] = Field(default_factory=tuple, max_length=MAXIMUM_SCOPES)
    nodes: tuple[GtmGraphNode, ...] = Field(default_factory=tuple, max_length=MAXIMUM_NODES)
    edges: tuple[GtmGraphEdge, ...] = Field(default_factory=tuple, max_length=MAXIMUM_EDGES)
    union: tuple[GtmUnionClassification, ...] | None = Field(default=None, max_length=MAXIMUM_NODES)
    counts: GtmDependencyGraphCounts
    scope_composition: Literal["SCOPES_KEPT_DISTINCT"] = "SCOPES_KEPT_DISTINCT"
    live_provider_reads: Literal["NONE_RETAINED_OBSERVATION_ONLY"] = (
        "NONE_RETAINED_OBSERVATION_ONLY"
    )
    deletion_authority: Literal["NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"] = (
        "NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"
    )
    cleanup_operations: Literal["NOT_IMPLEMENTED_SEPARATE_AUTHORIZED_WORK"] = (
        "NOT_IMPLEMENTED_SEPARATE_AUTHORIZED_WORK"
    )
    ui_reconciliation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"

    @model_validator(mode="after")
    def counts_match_their_records(self) -> GtmDependencyGraph:
        if self.counts.scopes != len(self.scopes) or self.counts.nodes != len(self.nodes):
            raise ValueError("GTM graph counts must match their scopes and nodes")
        if self.counts.edges != len(self.edges):
            raise ValueError("GTM graph counts must match their edges")
        if len({node.resource_ref for node in self.nodes}) != len(self.nodes):
            raise ValueError("GTM graph nodes must be unique")
        if len({scope.scope_ref for scope in self.scopes}) != len(self.scopes):
            raise ValueError("GTM graph scopes must be unique")
        if len(set(self.container_blocking_reasons)) != len(self.container_blocking_reasons):
            raise ValueError("Each GTM container blocker is named once")
        if self.container_blocking_reasons and self.counts.proven_unreferenced:
            raise ValueError("A container-blocked GTM graph proves nothing unreferenced")
        return self

    def scope(self, scope_ref: str) -> GtmScopeGraph | None:
        for item in self.scopes:
            if item.scope_ref == scope_ref:
                return item
        return None

    def inbound_edges(self, resource_ref: str) -> tuple[GtmGraphEdge, ...]:
        """Every resolved edge that reaches one node.  This is the inbound index."""
        return tuple(edge for edge in self.edges if edge.target_ref == resource_ref)


def build_gtm_dependency_graph(
    inventory: GtmFullInventory, *, include_union: bool = False
) -> GtmDependencyGraph:
    """Build the scoped dependency graph and classification for one retained inventory.

    `include_union` adds the opt-in container-wide view described on
    `GtmUnionClassification`.  Without it, scopes stay strictly separate.
    """

    if not isinstance(inventory, GtmFullInventory):
        raise AdhelmError(
            "INVALID_GTM_GRAPH_INPUT",
            "A GTM dependency graph requires a retained full inventory observation.",
            exit_code=2,
        )
    if len(inventory.resources) > MAXIMUM_NODES or len(inventory.edges) > MAXIMUM_EDGES:
        raise AdhelmError(
            "GTM_GRAPH_BOUND_EXCEEDED",
            "The retained GTM inventory exceeds the dependency graph bounds.",
        )

    scope_refs = _scope_refs(inventory)
    if len(scope_refs) > MAXIMUM_SCOPES:
        raise AdhelmError(
            "GTM_GRAPH_BOUND_EXCEEDED", "The retained GTM inventory exceeds the scope bound."
        )

    container_blockers = _container_blockers(inventory)
    incomplete_by_scope = _incomplete_collections(inventory)
    unresolved_by_scope = {
        scope_ref: any(
            edge.resolution.startswith("UNRESOLVED_")
            for edge in inventory.edges
            if edge.scope_ref == scope_ref
        )
        for scope_ref in scope_refs
    }
    template_instances_by_scope = _unbound_template_instance_scopes(inventory)

    edges = tuple(
        sorted(
            (_graph_edge(edge) for edge in inventory.edges),
            key=lambda item: (
                item.scope_ref,
                item.source_ref,
                item.edge_kind,
                item.target_kind,
                item.target_ref or "",
            ),
        )
    )
    inbound: dict[str, list[GtmGraphEdge]] = {}
    outbound: dict[str, int] = {}
    for edge in edges:
        outbound[edge.source_ref] = outbound.get(edge.source_ref, 0) + 1
        if edge.target_ref is not None:
            inbound.setdefault(edge.target_ref, []).append(edge)

    nodes = tuple(
        _graph_node(
            resource,
            inbound=inbound.get(resource.resource_ref, []),
            outbound_edge_count=outbound.get(resource.resource_ref, 0),
            scope_blockers=_scope_blockers(
                incomplete=bool(incomplete_by_scope.get(resource.scope_ref)),
                unresolved=bool(unresolved_by_scope.get(resource.scope_ref)),
            ),
            container_blockers=container_blockers,
            template_instances_unbound=resource.scope_ref in template_instances_by_scope,
        )
        for resource in sorted(inventory.resources, key=lambda item: item.resource_ref)
    )

    nodes = _refuse_cross_scope_proofs(nodes)

    scopes = tuple(
        _scope_graph(
            inventory,
            scope_ref=scope_ref,
            nodes=nodes,
            edges=edges,
            incomplete=incomplete_by_scope.get(scope_ref, ()),
            unresolved=bool(unresolved_by_scope.get(scope_ref)),
            container_blockers=container_blockers,
        )
        for scope_ref in scope_refs
    )

    return GtmDependencyGraph(
        identity=inventory.identity,
        inventory_digest=digest(inventory),
        live_latest_status=inventory.live_latest.status,
        reference_coverage=inventory.reference_coverage,
        inventory_completeness=inventory.completeness.status,
        container_blocking_reasons=container_blockers,
        scopes=scopes,
        nodes=nodes,
        edges=edges,
        union=_union(nodes) if include_union else None,
        counts=GtmDependencyGraphCounts(
            scopes=len(scopes),
            nodes=len(nodes),
            edges=len(edges),
            resolved_edges=sum(edge.resolution == "RESOLVED" for edge in edges),
            unresolved_edges=sum(edge.disables_scope_proof for edge in edges),
            out_of_scope_edges=sum(
                edge.resolution == "OUT_OF_SCOPE_CHILD_CONTAINER" for edge in edges
            ),
            proven_unreferenced=sum(
                node.reference_status == "PROVEN_UNREFERENCED" for node in nodes
            ),
            referenced=sum(node.reference_status == "REFERENCED" for node in nodes),
            unprovable=sum(node.reference_status.startswith("UNPROVABLE_") for node in nodes),
            sequencing_cycles=sum(len(scope.sequencing_cycles) for scope in scopes),
            built_in_variables=sum(node.resource_kind == "BUILT_IN_VARIABLE" for node in nodes),
            custom_code_resources=sum(bool(node.custom_code_marker_count) for node in nodes),
        ),
    )


# --------------------------------------------------------------------- pieces


def _scope_refs(inventory: GtmFullInventory) -> tuple[str, ...]:
    """Every scope the observation recorded, in stable reference order."""
    return tuple(sorted({item.scope_ref for item in inventory.coverage}))


def _container_blockers(inventory: GtmFullInventory) -> tuple[GtmProofBlocker, ...]:
    """Reasons the whole container can prove nothing unreferenced.

    The deletion rule is container-wide.  A resource must be unreferenced in the
    live version, the latest version and every accessible workspace before it may
    be removed, so a gap anywhere refuses the proof everywhere.
    """
    blockers: set[GtmProofBlocker] = set()
    if any(item.status not in _ACCEPTABLE_COVERAGE for item in inventory.coverage):
        blockers.add("CONTAINER_COVERAGE_INCOMPLETE")
    if any(edge.resolution.startswith("UNRESOLVED_") for edge in inventory.edges):
        blockers.add("CONTAINER_UNRESOLVED_EDGE_PRESENT")
    if inventory.completeness.uninspected_workspaces:
        blockers.add("UNINSPECTED_WORKSPACE_PRESENT")
    if inventory.reference_coverage != "COMPLETE_WITHIN_DECLARED_BOUNDS":
        blockers.add("REFERENCE_COVERAGE_BOUNDED_INCOMPLETE")
    if inventory.live_latest.status == "UNDETERMINABLE":
        blockers.add("LIVE_LATEST_UNDETERMINABLE")
    return _ordered(blockers)


def _incomplete_collections(
    inventory: GtmFullInventory,
) -> dict[str, tuple[GtmCollection, ...]]:
    incomplete: dict[str, list[GtmCollection]] = {}
    for item in inventory.coverage:
        if item.status not in _ACCEPTABLE_COVERAGE:
            incomplete.setdefault(item.scope_ref, []).append(item.collection)
    return {ref: tuple(sorted(values)) for ref, values in incomplete.items()}


def _unbound_template_instance_scopes(inventory: GtmFullInventory) -> frozenset[str]:
    """Scopes holding a template instance this read could not bind to a template.

    A GTM template instance names its template through a `cvt_<container>_<id>`
    resource type.  When that reference does not resolve to a template this read
    saw, the instance may belong to any custom template in the scope, so no
    template there can be proven unreferenced.
    """
    return frozenset(
        edge.scope_ref
        for edge in inventory.edges
        if edge.edge_kind == "TEMPLATE_INSTANCE" and edge.resolution != "RESOLVED"
    )


def _scope_blockers(*, incomplete: bool, unresolved: bool) -> tuple[GtmProofBlocker, ...]:
    blockers: set[GtmProofBlocker] = set()
    if incomplete:
        blockers.add("SCOPE_COVERAGE_INCOMPLETE")
    if unresolved:
        blockers.add("SCOPE_UNRESOLVED_EDGE_PRESENT")
    return _ordered(blockers)


def _ordered(blockers: set[GtmProofBlocker]) -> tuple[GtmProofBlocker, ...]:
    return tuple(reason for reason in _BLOCKER_ORDER if reason in blockers)


def _graph_edge(edge: GtmReferenceEdge) -> GtmGraphEdge:
    return GtmGraphEdge(
        scope_kind=edge.scope_kind,
        scope_ref=edge.scope_ref,
        source_ref=edge.source_ref,
        target_ref=edge.target_ref,
        edge_kind=edge.edge_kind,
        relation=_RELATIONS[edge.edge_kind],
        target_kind=edge.target_kind,
        selector_form=edge.selector_form,
        resolution=edge.resolution,
        occurrence_count=edge.occurrence_count,
        disables_scope_proof=edge.resolution.startswith("UNRESOLVED_"),
    )


def _graph_node(
    resource: GtmInventoryResource,
    *,
    inbound: list[GtmGraphEdge],
    outbound_edge_count: int,
    scope_blockers: tuple[GtmProofBlocker, ...],
    container_blockers: tuple[GtmProofBlocker, ...],
    template_instances_unbound: bool,
) -> GtmGraphNode:
    node_blockers: set[GtmProofBlocker] = set()
    # A built-in variable is a container setting, not a deletable resource, and
    # a `{{name}}` reference to one is indistinguishable from a reference to a
    # user-defined variable of the same name.  It is never proven unreferenced.
    if resource.resource_kind == "BUILT_IN_VARIABLE":
        node_blockers.add("BUILT_IN_RESOURCE")
    if resource.resource_kind == "TEMPLATE" and template_instances_unbound:
        node_blockers.add("TEMPLATE_WITH_UNBOUND_INSTANCES")
    blockers = _ordered(node_blockers | set(scope_blockers) | set(container_blockers))

    sources = tuple(sorted({edge.source_ref for edge in inbound}))
    inlined = sources[:MAXIMUM_INLINE_INBOUND_SOURCES]
    status: GtmReferenceClassification
    if inbound:
        status = "REFERENCED"
        blockers = ()
    elif blockers:
        status = _unprovable(blockers[0])
    else:
        status = "PROVEN_UNREFERENCED"
    return GtmGraphNode(
        scope_kind=resource.scope_kind,
        scope_ref=resource.scope_ref,
        resource_ref=resource.resource_ref,
        resource_kind=resource.resource_kind,
        resource_client_ref=resource.resource_client_ref,
        collection=resource.collection,
        provider_type=resource.provider_type,
        configuration_fingerprint=resource.configuration_fingerprint,
        parent_folder_ref=resource.parent_folder_ref,
        paused=resource.paused,
        custom_code_marker_count=len(resource.code_markers),
        outbound_edge_count=outbound_edge_count,
        inbound_edge_count=len(inbound),
        inbound_relations=tuple(sorted({edge.relation for edge in inbound})),
        inbound_source_refs=inlined,
        inbound_source_ref_status=(
            "COMPLETE" if len(inlined) == len(sources) else "BOUNDED_PAGE_EDGES_BY_TARGET_REF"
        ),
        reference_status=status,
        blocking_reasons=blockers,
    )


def _unprovable(blocker: GtmProofBlocker) -> GtmReferenceClassification:
    value = f"UNPROVABLE_{blocker}"
    # Every blocker has one classification member; a missing one is a type
    # error rather than an unnamed refusal.
    if value not in _UNPROVABLE_VALUES:
        raise AdhelmError("GTM_GRAPH_CLASSIFICATION_INVALID", "Unknown GTM proof blocker.")
    return value  # type: ignore[return-value]


_UNPROVABLE_VALUES: Final = frozenset(f"UNPROVABLE_{reason}" for reason in _BLOCKER_ORDER)


def _scope_graph(
    inventory: GtmFullInventory,
    *,
    scope_ref: str,
    nodes: tuple[GtmGraphNode, ...],
    edges: tuple[GtmGraphEdge, ...],
    incomplete: tuple[GtmCollection, ...],
    unresolved: bool,
    container_blockers: tuple[GtmProofBlocker, ...],
) -> GtmScopeGraph:
    scope_nodes = [node for node in nodes if node.scope_ref == scope_ref]
    scope_edges = [edge for edge in edges if edge.scope_ref == scope_ref]
    kinds = tuple(
        sorted({item.scope_kind for item in inventory.coverage if item.scope_ref == scope_ref})
    )
    scope_kind: GtmScopeKind = kinds[0] if kinds else "CONTAINER"
    blockers = _ordered(
        set(_scope_blockers(incomplete=bool(incomplete), unresolved=unresolved))
        | set(container_blockers)
    )
    return GtmScopeGraph(
        scope_kind=scope_kind,
        scope_ref=scope_ref,
        node_count=len(scope_nodes),
        edge_count=len(scope_edges),
        resolved_edge_count=sum(edge.resolution == "RESOLVED" for edge in scope_edges),
        unresolved_edge_count=sum(edge.disables_scope_proof for edge in scope_edges),
        out_of_scope_edge_count=sum(
            edge.resolution == "OUT_OF_SCOPE_CHILD_CONTAINER" for edge in scope_edges
        ),
        incomplete_collections=incomplete,
        proof_status="PROOF_BLOCKED" if blockers else "PROOF_AVAILABLE",
        blocking_reasons=blockers,
        proven_unreferenced_count=sum(
            node.reference_status == "PROVEN_UNREFERENCED" for node in scope_nodes
        ),
        referenced_count=sum(node.reference_status == "REFERENCED" for node in scope_nodes),
        unprovable_count=sum(
            node.reference_status.startswith("UNPROVABLE_") for node in scope_nodes
        ),
        sequencing_cycles=sequencing_cycles(scope_kind, scope_ref, scope_edges),
        also_covers_scope_kinds=kinds[1:],
    )


def sequencing_cycles(
    scope_kind: GtmScopeKind, scope_ref: str, edges: list[GtmGraphEdge]
) -> tuple[GtmSequencingCycle, ...]:
    """Setup/teardown cycles inside one scope, found with an iterative Tarjan pass.

    Only resolved sequencing edges participate: an unresolved edge names no
    target, so it can neither create nor complete a cycle.  Ordering is by
    resource reference throughout, so the result is identical for any input
    order.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        if edge.relation not in _SEQUENCING_RELATIONS or edge.target_ref is None:
            continue
        adjacency.setdefault(edge.source_ref, []).append(edge.target_ref)
        adjacency.setdefault(edge.target_ref, [])
    if len(adjacency) > MAXIMUM_CYCLE_NODES:
        raise AdhelmError(
            "GTM_GRAPH_BOUND_EXCEEDED", "GTM sequencing analysis exceeds its declared bound."
        )
    for targets in adjacency.values():
        targets.sort()

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []

    for start in sorted(adjacency):
        if start in index:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, position = work[-1]
            if position == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            if position < len(adjacency[node]):
                work[-1] = (node, position + 1)
                child = adjacency[node][position]
                if child not in index:
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in adjacency[node]:
                    components.append(sorted(component))

    cycles = []
    for component in sorted(components):
        cycles.append(
            GtmSequencingCycle(
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                member_refs=tuple(component[:32]),
                member_count=len(component),
                member_ref_status=(
                    "COMPLETE" if len(component) <= 32 else "BOUNDED_PAGE_EDGES_BY_SCOPE"
                ),
            )
        )
    return tuple(cycles[:256])


def _refuse_cross_scope_proofs(nodes: tuple[GtmGraphNode, ...]) -> tuple[GtmGraphNode, ...]:
    """Never publish the container-wide term for a resource another scope references.

    `PROVEN_UNREFERENCED` is defined across live, latest and every workspace.
    A scope view that finds no inbound edge for a resource the same container
    references elsewhere (same collection and client reference) reports it as
    unprovable in that scope instead of proven.
    """

    referenced = {
        (node.collection, node.resource_client_ref)
        for node in nodes
        if node.reference_status == "REFERENCED"
    }
    adjusted = []
    for node in nodes:
        if (
            node.reference_status == "PROVEN_UNREFERENCED"
            and (node.collection, node.resource_client_ref) in referenced
        ):
            blockers = _ordered({"REFERENCED_IN_ANOTHER_SCOPE", *node.blocking_reasons})
            node = node.model_copy(
                update={
                    "reference_status": _unprovable(blockers[0]),
                    "blocking_reasons": blockers,
                }
            )
        adjusted.append(node)
    return tuple(adjusted)


def _union(nodes: tuple[GtmGraphNode, ...]) -> tuple[GtmUnionClassification, ...]:
    grouped: dict[tuple[str, str], list[GtmGraphNode]] = {}
    for node in nodes:
        grouped.setdefault((node.collection, node.resource_client_ref), []).append(node)
    entries = []
    for (collection, client_ref), members in sorted(grouped.items()):
        scope_refs = tuple(sorted(member.scope_ref for member in members))
        blockers = _ordered(
            {
                reason
                for member in members
                for reason in member.blocking_reasons
                # The union already spans every scope, so a cross-scope
                # reference is positive evidence here, not a refusal.
                if reason != "REFERENCED_IN_ANOTHER_SCOPE"
            }
        )
        status: GtmReferenceClassification
        if blockers:
            status = _unprovable(blockers[0])
        elif any(member.reference_status == "REFERENCED" for member in members):
            # Referenced in any scope is referenced for the container: a
            # deletion would break that scope.
            status = "REFERENCED"
        else:
            status = "PROVEN_UNREFERENCED"
        inlined = scope_refs[:64]
        entries.append(
            GtmUnionClassification(
                collection=collection,  # type: ignore[arg-type]
                resource_client_ref=client_ref,
                resource_kind=members[0].resource_kind,
                scope_count=len(scope_refs),
                scope_refs=inlined,
                scope_ref_status=(
                    "COMPLETE"
                    if len(inlined) == len(scope_refs)
                    else "BOUNDED_PAGE_NODES_BY_CLIENT_REF"
                ),
                reference_status=status,
                blocking_reasons=blockers,
            )
        )
    return tuple(entries)


__all__ = [
    "CAPABILITY_VERSION",
    "MAXIMUM_EDGES",
    "MAXIMUM_INLINE_INBOUND_SOURCES",
    "MAXIMUM_NODES",
    "MAXIMUM_SCOPES",
    "GtmDependencyGraph",
    "GtmDependencyGraphCounts",
    "GtmEdgeRelation",
    "GtmGraphEdge",
    "GtmGraphNode",
    "GtmProofBlocker",
    "GtmReferenceClassification",
    "GtmScopeGraph",
    "GtmSequencingCycle",
    "GtmUnionClassification",
    "build_gtm_dependency_graph",
    "sequencing_cycles",
]

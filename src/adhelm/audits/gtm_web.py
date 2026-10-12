# ruff: noqa: E501
"""Read-only GTM Web audit rule table over one retained inventory and its graph.

This module is the GTM half of the provider-audit seam documented in
`adhelm/audits/__init__.py`.  It honours the same three invariants -- no provider
request, no credential, no ledger append, no compiled plan; an explicit coverage
status for every audit dimension; and no recommendation naming an operation this
product cannot compile -- over a different input.

It deliberately does **not** register in `_RULE_MODULES`.  That map drives
`adhelm.audit.audit_provider`, whose entry point takes a `NormalizedReport`: an ad
performance report with a data window, spend and delivery rows.  GTM has no such
report, and manufacturing one to satisfy the dispatch signature would be exactly
the fabricated result AGENTS rule 20 forbids.  What the seam *does* share is its
vocabulary -- coverage statuses, the declined-recommendation record and its
inline bound -- which this module imports rather than redefines, and
`COMPILABLE_OPERATION_KINDS["gtm"]`, which is the empty set: no GTM mutation is
compilable in this build, so every candidate action here is a declined outcome.

Deletion and cleanup in particular are never recommended.  A resource the graph
classifies `PROVEN_UNREFERENCED` is still not a deletion candidate this product
may act on: staged cleanup inside a dedicated workspace is separate, separately
authorized work.  The unreferenced-trigger and unreferenced-variable rules below
are therefore reported only as reference status, exactly as
`docs/measurement-control.md` requires.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.audit import Confidence, Risk, Severity
from adhelm.audits import (
    MAXIMUM_INLINE_FINDING_IDS,
    BoundExceeded,
    NoRecommendation,
    NoRecommendationReason,
    ProviderCoverageStatus,
    RecommendedOperationKind,
)
from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.gtm_dependency_graph import GtmDependencyGraph
from adhelm.gtm_inventory import GtmFullInventory
from adhelm.models import SafeRef, StrictModel

RULE_TABLE_VERSION: Final = "gtm-web-audit-rules-1"
#: One finding per row, bounded so a whole container's audit stays reviewable.
MAXIMUM_FINDINGS: Final = 5_000

GtmAuditDimension = Literal[
    "REFERENCE_INTEGRITY",
    "NAMING",
    "TAG_FIRING",
    "REFERENCE_STATUS",
    "SEQUENCING",
    "ZONE_BOUNDARY",
    "TAG_STATE",
    "CONSENT",
    "CUSTOM_CODE",
    "VERSION_DIVERGENCE",
    "WORKSPACE_HYGIENE",
    "COVERAGE",
]
_DIMENSIONS: Final[tuple[GtmAuditDimension, ...]] = (
    "REFERENCE_INTEGRITY",
    "NAMING",
    "TAG_FIRING",
    "REFERENCE_STATUS",
    "SEQUENCING",
    "ZONE_BOUNDARY",
    "TAG_STATE",
    "CONSENT",
    "CUSTOM_CODE",
    "VERSION_DIVERGENCE",
    "WORKSPACE_HYGIENE",
    "COVERAGE",
)

GtmAuditRuleId = Literal[
    "GTM_BROKEN_REFERENCE",
    "GTM_AMBIGUOUS_NAME_REFERENCE",
    "GTM_DUPLICATE_RESOURCE_NAME",
    "GTM_TAG_WITHOUT_FIRING_TRIGGER",
    "GTM_TRIGGER_WITHOUT_REFERRER",
    "GTM_VARIABLE_WITHOUT_REFERRER",
    "GTM_SEQUENCING_CYCLE",
    "GTM_ZONE_BOUNDARY_UNRESOLVED",
    "GTM_ZONE_RESTRICTION_UNVERIFIABLE_OUTSIDE_CONTAINER",
    "GTM_PAUSED_TAG",
    "GTM_CONSENT_SETTING_ABSENT",
    "GTM_CONSENT_TYPE_NOT_REVIEWED",
    "GTM_CUSTOM_CODE_RESOURCE",
    "GTM_LIVE_LATEST_DIVERGENCE",
    "GTM_WORKSPACE_PENDING_CHANGES",
    "GTM_WORKSPACE_MERGE_CONFLICTS",
    "GTM_COVERAGE_GAP",
    "GTM_UNINSPECTED_WORKSPACE",
]

#: GTM tag types whose consent behaviour this product has reviewed and whose
#: `consentSettings` therefore carries a meaning it can report.  An unlisted type
#: is never reported healthy: it gets `GTM_CONSENT_TYPE_NOT_REVIEWED` instead, so
#: an absent consent declaration is always visible, with an honest statement of
#: how much this build knows about it.
_CONSENT_REVIEWED_TAG_TYPES: Final = frozenset(
    {
        "gaawe",  # GA4 event
        "gaawc",  # GA4 configuration
        "googtag",  # Google tag
        "awct",  # Google Ads conversion tracking
        "sp",  # Google Ads remarketing
        "flc",  # Floodlight counter
        "fls",  # Floodlight sales
    }
)
#: Tag types whose firing is governed by another resource rather than by their
#: own firing triggers, so "no firing trigger" is not a defect for them.
_NON_FIRING_TAG_TYPES: Final = frozenset({"googtag", "gaawc"})


class GtmAuditCoverage(StrictModel):
    """One audit dimension's coverage status, derived from the inventory's own coverage."""

    schema_version: Literal[1] = 1
    dimension: GtmAuditDimension
    status: ProviderCoverageStatus
    evidence_source: Literal["RETAINED_GTM_FULL_INVENTORY", "NONE"] = "RETAINED_GTM_FULL_INVENTORY"
    evidence_record_count: int = Field(default=0, ge=0)
    rationale: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def unread_dimensions_cite_nothing(self) -> GtmAuditCoverage:
        if self.status in {"NOT_READ", "NOT_APPLICABLE"} and (
            self.evidence_record_count or self.evidence_source != "NONE"
        ):
            raise ValueError("An unread or inapplicable GTM dimension cites no evidence")
        if self.status in {"COVERED", "PARTIAL"} and (
            self.evidence_source == "NONE" or self.evidence_record_count < 1
        ):
            raise ValueError("A covered GTM dimension names the evidence it was evaluated from")
        return self


class GtmAuditFinding(StrictModel):
    """One GTM audit finding, citing the retained records it was read from.

    This is not `adhelm.audit.AuditFinding`: that model requires a report data
    window -- window start and end, source timezone and attribution model -- which
    a configuration read does not have and must not invent.  The finding
    identifier scheme, severity, confidence and risk vocabularies are the shared
    ones, and `automatic_write_allowed` is the same hard `False`.
    """

    schema_version: Literal[1] = 1
    finding_id: str = Field(pattern=r"^finding:sha256:[0-9a-f]{64}$")
    rule_id: GtmAuditRuleId
    dimension: GtmAuditDimension
    coverage_status: ProviderCoverageStatus
    severity: Severity
    confidence: Confidence
    risk: Risk
    scope_kind: Literal["LIVE_VERSION", "LATEST_VERSION", "WORKSPACE", "CONTAINER", "OBSERVATION"]
    scope_ref: SafeRef
    resource_refs: tuple[SafeRef, ...] = Field(default_factory=tuple, max_length=4)
    #: Exact digests of the retained records this finding rests on: the ledger
    #: record digest of the observation, its snapshot digest, and where one
    #: resource is named, that resource's configuration fingerprint.
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=4)
    observation: str = Field(min_length=1, max_length=500)
    effect: str = Field(min_length=1, max_length=300)
    smallest_safe_remediation: str = Field(min_length=1, max_length=300)
    automatic_write_allowed: Literal[False] = False
    deletion_recommended: Literal[False] = False

    @field_validator("evidence_refs")
    @classmethod
    def record_digests_only(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("GTM audit evidence references must be record digests")
        return values


class GtmWebAuditSections(StrictModel):
    """Everything the GTM Web rule table returns for one retained observation."""

    schema_version: Literal[1] = 1
    provider: Literal["gtm"] = "gtm"
    rule_table_version: Literal["gtm-web-audit-rules-1"] = RULE_TABLE_VERSION
    source_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    live_provider_reads: Literal["NONE_RETAINED_RECORDS_ONLY"] = "NONE_RETAINED_RECORDS_ONLY"
    ledger_appends: Literal["NONE"] = "NONE"
    coverage: tuple[GtmAuditCoverage, ...] = Field(default_factory=tuple, max_length=32)
    findings: tuple[GtmAuditFinding, ...] = Field(
        default_factory=tuple, max_length=MAXIMUM_FINDINGS
    )
    bound_exceeded: tuple[BoundExceeded, ...] = Field(default_factory=tuple, max_length=32)
    finding_bound: Literal[5_000] = MAXIMUM_FINDINGS
    omitted_finding_count: int = Field(default=0, ge=0)
    finding_bound_status: Literal["COMPLETE", "TRUNCATED_AT_BOUND"] = "COMPLETE"
    #: There is no recommendation list.  `COMPILABLE_OPERATION_KINDS["gtm"]` is
    #: empty, so no GTM operation can be recommended without promising something
    #: this build cannot compile.  Every candidate action is a declined outcome.
    recommendation_status: Literal["NO_COMPILABLE_GTM_OPERATION_EVERY_CANDIDATE_DECLINED"] = (
        "NO_COMPILABLE_GTM_OPERATION_EVERY_CANDIDATE_DECLINED"
    )
    no_recommendations: tuple[NoRecommendation, ...] = Field(default_factory=tuple, max_length=32)
    finding_counts_by_rule: tuple[tuple[GtmAuditRuleId, int], ...] = Field(
        default_factory=tuple, max_length=32
    )

    @model_validator(mode="after")
    def every_dimension_has_exactly_one_coverage_status(self) -> GtmWebAuditSections:
        dimensions = [item.dimension for item in self.coverage]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("Each GTM audit dimension has exactly one coverage status")
        if set(dimensions) != set(_DIMENSIONS):
            raise ValueError("Every GTM audit dimension carries an explicit coverage status")
        by_dimension = {item.dimension: item.status for item in self.coverage}
        for finding in self.findings:
            if by_dimension.get(finding.dimension) != finding.coverage_status:
                raise ValueError("A GTM finding restates its dimension's coverage status")
        counted = dict(self.finding_counts_by_rule)
        if len(counted) != len(self.finding_counts_by_rule):
            raise ValueError("Each GTM rule is counted once")
        observed: dict[GtmAuditRuleId, int] = {}
        for finding in self.findings:
            observed[finding.rule_id] = observed.get(finding.rule_id, 0) + 1
        if self.finding_bound_status == "COMPLETE":
            if self.omitted_finding_count or observed != counted:
                raise ValueError("GTM rule counts must match the findings they summarize")
        else:
            # Counts stay complete while the inlined findings are bounded.
            if not self.omitted_finding_count or len(self.findings) != self.finding_bound:
                raise ValueError("A truncated GTM audit states its bound and its omissions")
            if sum(counted.values()) != len(self.findings) + self.omitted_finding_count:
                raise ValueError("GTM rule counts must cover every finding, inlined or omitted")
            if any(observed[rule] > counted.get(rule, 0) for rule in observed):
                raise ValueError("GTM rule counts must match the findings they summarize")
        return self


def evaluate_gtm_web_audit(
    *,
    inventory: GtmFullInventory,
    graph: GtmDependencyGraph,
    source_record_digest: str,
    snapshot_digest: str,
) -> GtmWebAuditSections:
    """Evaluate the GTM Web rule table over one retained inventory and its graph."""

    if graph.inventory_digest != digest(inventory):
        raise AdhelmError(
            "GTM_GRAPH_INVENTORY_MISMATCH",
            "The supplied GTM dependency graph was not built from this inventory.",
            exit_code=2,
        )
    coverage = _coverage(inventory)
    status = {item.dimension: item.status for item in coverage}
    evidence = (source_record_digest, snapshot_digest)
    findings: list[GtmAuditFinding] = []

    findings.extend(_reference_integrity(inventory, status, evidence))
    findings.extend(_naming(inventory, status, evidence))
    findings.extend(_tag_firing(graph, status, evidence))
    findings.extend(_reference_status(graph, status, evidence))
    findings.extend(_sequencing(graph, status, evidence))
    findings.extend(_zones(inventory, status, evidence))
    findings.extend(_tag_state(inventory, status, evidence))
    findings.extend(_consent(inventory, status, evidence))
    findings.extend(_custom_code(inventory, status, evidence))
    findings.extend(_version_divergence(inventory, status, evidence))
    findings.extend(_workspace_hygiene(inventory, status, evidence))
    findings.extend(_coverage_gaps(inventory, status, evidence))

    findings.sort(key=lambda item: (item.rule_id, item.scope_ref, item.finding_id))
    counts: dict[GtmAuditRuleId, int] = {}
    for finding in findings:
        counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    omitted_findings = max(0, len(findings) - MAXIMUM_FINDINGS)
    if omitted_findings:
        # The bound is reported, never fatal: every other bound in this module
        # is disclosed the same way. Counts by rule stay complete.
        findings = findings[:MAXIMUM_FINDINGS]
    declined, bound_exceeded = _declined(tuple(findings))
    return GtmWebAuditSections(
        source_record_digest=source_record_digest,
        snapshot_digest=snapshot_digest,
        graph_digest=digest(graph),
        coverage=coverage,
        findings=tuple(findings),
        no_recommendations=declined,
        bound_exceeded=bound_exceeded,
        finding_bound=MAXIMUM_FINDINGS,
        omitted_finding_count=omitted_findings,
        finding_bound_status=("TRUNCATED_AT_BOUND" if omitted_findings else "COMPLETE"),
        finding_counts_by_rule=tuple(sorted(counts.items())),
    )


# ------------------------------------------------------------------- coverage


def _collection_status(
    inventory: GtmFullInventory, collections: tuple[str, ...]
) -> tuple[ProviderCoverageStatus, int]:
    entries = [item for item in inventory.coverage if item.collection in collections]
    read = [item for item in entries if item.status != "NOT_DOCUMENTED"]
    if not read:
        return "NOT_READ", 0
    if all(item.status == "COMPLETE" for item in read):
        return "COVERED", len(read)
    return "PARTIAL", len(read)


def _coverage(inventory: GtmFullInventory) -> tuple[GtmAuditCoverage, ...]:
    def entry(
        dimension: GtmAuditDimension,
        status: ProviderCoverageStatus,
        count: int,
        rationale: str,
    ) -> GtmAuditCoverage:
        if status in {"NOT_READ", "NOT_APPLICABLE"}:
            return GtmAuditCoverage(
                dimension=dimension, status=status, evidence_source="NONE", rationale=rationale
            )
        return GtmAuditCoverage(
            dimension=dimension,
            status=status,
            evidence_record_count=count,
            rationale=rationale,
        )

    edge_status, edge_count = _collection_status(inventory, ("reference_edges",))
    if inventory.reference_coverage == "BOUNDED_INCOMPLETE" and edge_status == "COVERED":
        edge_status = "PARTIAL"
    tag_status, tag_count = _collection_status(inventory, ("tags",))
    name_status, name_count = _collection_status(inventory, ("tags", "triggers", "variables"))
    referrer_status, _ = _collection_status(
        inventory, ("tags", "triggers", "variables", "zones", "templates", "folders")
    )
    if referrer_status != "COVERED" and edge_status == "COVERED":
        # The edge entries count what the reader emitted; a bounded referrer
        # collection means resources that could emit an edge were never read.
        edge_status = "PARTIAL"
    code_status, code_count = _collection_status(inventory, ("tags", "variables", "templates"))
    zone_entries = [item for item in inventory.coverage if item.collection == "zones"]
    zones = [item for item in inventory.resources if item.resource_kind == "ZONE"]
    if not zones:
        zone_status: ProviderCoverageStatus = "NOT_APPLICABLE"
        zone_count = 0
        zone_reason = "The container has no zone, so zone boundaries are inapplicable."
    elif any(
        zone.zone_boundary is not None and zone.zone_boundary.child_container_count
        for zone in zones
    ):
        zone_status = "PARTIAL"
        zone_count = len(zone_entries)
        zone_reason = (
            "A zone governs a child container this single-container read never entered, "
            "so resources restricted outside this container are not observable here."
        )
    else:
        zone_status = (
            "COVERED" if all(item.status == "COMPLETE" for item in zone_entries) else "PARTIAL"
        )
        zone_count = len(zone_entries)
        zone_reason = "Zone boundaries were read from the zones collection of every scope."
    workspace_entries = [
        item for item in inventory.coverage if item.collection == "workspace_changes"
    ]
    read_workspaces = [item for item in workspace_entries if item.status != "NOT_DOCUMENTED"]
    if not read_workspaces:
        hygiene_status: ProviderCoverageStatus = "NOT_APPLICABLE"
        hygiene_count = 0
        hygiene_reason = "The container exposes no accessible workspace to check for pending work."
    elif all(item.status == "COMPLETE" for item in read_workspaces) and (
        not inventory.completeness.uninspected_workspaces
    ):
        hygiene_status = "COVERED"
        hygiene_count = len(read_workspaces)
        hygiene_reason = "Every accessible workspace reported its pending changes and conflicts."
    else:
        hygiene_status = "PARTIAL"
        hygiene_count = len(read_workspaces)
        hygiene_reason = "At least one workspace was left uninspected or reported a bound."

    return (
        entry(
            "REFERENCE_INTEGRITY",
            edge_status,
            edge_count,
            "Reference integrity is read from the per-scope reference-edge coverage entries.",
        ),
        entry(
            "NAMING",
            name_status,
            name_count,
            "Name ambiguity is read from the tag, trigger and variable collections.",
        ),
        entry(
            "TAG_FIRING",
            tag_status,
            tag_count,
            "Firing behaviour is read from the tags collection and its resolved trigger edges.",
        ),
        entry(
            "REFERENCE_STATUS",
            edge_status,
            edge_count,
            "Reference status restates the dependency graph, never a deletion decision.",
        ),
        entry(
            "SEQUENCING",
            tag_status,
            tag_count,
            "Sequencing is read from resolved setup and teardown tag edges inside each scope.",
        ),
        entry("ZONE_BOUNDARY", zone_status, zone_count, zone_reason),
        entry(
            "TAG_STATE",
            tag_status,
            tag_count,
            "Paused state is read verbatim from each tag; it is never a deletion signal.",
        ),
        entry(
            "CONSENT",
            tag_status,
            tag_count,
            "Consent settings are read from each tag; unreviewed types are stated, not assumed.",
        ),
        entry(
            "CUSTOM_CODE",
            code_status,
            code_count,
            "Custom code is counted and digested only; no source is read into this audit.",
        ),
        entry(
            "VERSION_DIVERGENCE",
            "PARTIAL" if inventory.live_latest.status == "UNDETERMINABLE" else "COVERED",
            2,
            "Live and latest identity come from the container header and both version reads.",
        ),
        entry("WORKSPACE_HYGIENE", hygiene_status, hygiene_count, hygiene_reason),
        entry(
            "COVERAGE",
            "COVERED",
            len(inventory.coverage),
            "Coverage gaps are read from the observation's own per-collection coverage entries.",
        ),
    )


# ---------------------------------------------------------------------- rules


def _finding(
    *,
    rule_id: GtmAuditRuleId,
    dimension: GtmAuditDimension,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    severity: Severity,
    confidence: Confidence,
    risk: Risk,
    scope_kind: str,
    scope_ref: str,
    resource_refs: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    observation: str,
    effect: str,
    remediation: str,
) -> GtmAuditFinding:
    identity = {
        "rule_id": rule_id,
        "scope_ref": scope_ref,
        "resource_refs": list(resource_refs),
        "observation": observation,
        "evidence_refs": list(evidence_refs),
    }
    return GtmAuditFinding(
        finding_id=f"finding:sha256:{digest(identity)}",
        rule_id=rule_id,
        dimension=dimension,
        coverage_status=status[dimension],
        severity=severity,
        confidence=confidence,
        risk=risk,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_ref=scope_ref,
        resource_refs=resource_refs,
        evidence_refs=evidence_refs,
        observation=observation,
        effect=effect,
        smallest_safe_remediation=remediation,
    )


def _reference_integrity(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for edge in inventory.edges:
        if edge.resolution not in ("UNRESOLVED_NO_MATCHING_RESOURCE", "UNRESOLVED_MALFORMED"):
            continue
        broken = edge.resolution == "UNRESOLVED_NO_MATCHING_RESOURCE"
        findings.append(
            _finding(
                rule_id="GTM_BROKEN_REFERENCE",
                dimension="REFERENCE_INTEGRITY",
                status=status,
                severity="HIGH" if broken else "MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                scope_kind=edge.scope_kind,
                scope_ref=edge.scope_ref,
                resource_refs=(edge.source_ref,),
                evidence_refs=evidence,
                observation=(
                    f"A {edge.edge_kind} reference by {edge.selector_form} form did not resolve "
                    f"inside this scope ({edge.resolution})."
                ),
                effect=(
                    "The referenced resource cannot be identified, so no proof of absence holds "
                    "anywhere in this scope."
                ),
                remediation=(
                    "Repair or remove the dangling reference in a dedicated workspace under a "
                    "separate authorization; this audit stages nothing."
                ),
            )
        )
    return findings


def _naming(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for edge in inventory.edges:
        if edge.resolution != "UNRESOLVED_AMBIGUOUS_NAME":
            continue
        findings.append(
            _finding(
                rule_id="GTM_AMBIGUOUS_NAME_REFERENCE",
                dimension="NAMING",
                status=status,
                severity="HIGH",
                confidence="HIGH",
                risk="MEDIUM",
                scope_kind=edge.scope_kind,
                scope_ref=edge.scope_ref,
                resource_refs=(edge.source_ref,),
                evidence_refs=evidence,
                observation=(
                    f"A {edge.edge_kind} name reference matched more than one resource of the "
                    "same kind in this scope."
                ),
                effect="The reference target is ambiguous, so it can never be proven.",
                remediation="Give the duplicated resource names distinct values before any change.",
            )
        )
    seen: dict[tuple[str, str, str], list[str]] = {}
    for resource in inventory.resources:
        if resource.name_digest is None:
            continue
        seen.setdefault(
            (resource.scope_ref, resource.resource_kind, resource.name_digest), []
        ).append(resource.resource_ref)
    for (scope_ref, kind, _), refs in sorted(seen.items()):
        if len(refs) < 2:
            continue
        scope_kind = next(
            (item.scope_kind for item in inventory.resources if item.scope_ref == scope_ref),
            "CONTAINER",
        )
        findings.append(
            _finding(
                rule_id="GTM_DUPLICATE_RESOURCE_NAME",
                dimension="NAMING",
                status=status,
                severity="MEDIUM",
                confidence="HIGH",
                risk="LOW",
                scope_kind=scope_kind,
                scope_ref=scope_ref,
                resource_refs=tuple(sorted(refs))[:4],
                evidence_refs=evidence,
                observation=(
                    f"{len(refs)} {kind} resources in this scope share one name, so any name-form "
                    "reference to it is ambiguous."
                ),
                effect="Name-form references to these resources cannot resolve to one target.",
                remediation="Rename the duplicates so each name identifies one resource.",
            )
        )
    return findings


def _tag_firing(
    graph: GtmDependencyGraph,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    firing = {
        edge.source_ref
        for edge in graph.edges
        if edge.relation == "FIRING" and edge.edge_kind == "FIRING_TRIGGER"
    }
    findings = []
    for node in graph.nodes:
        if node.resource_kind != "TAG" or node.resource_ref in firing:
            continue
        if node.provider_type is not None and node.provider_type in _NON_FIRING_TAG_TYPES:
            continue
        findings.append(
            _finding(
                rule_id="GTM_TAG_WITHOUT_FIRING_TRIGGER",
                dimension="TAG_FIRING",
                status=status,
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="LOW",
                scope_kind=node.scope_kind,
                scope_ref=node.scope_ref,
                resource_refs=(node.resource_ref,),
                evidence_refs=(*evidence, node.configuration_fingerprint),
                observation="This tag declares no firing trigger in this scope.",
                effect="The tag cannot fire from its own configuration; it may be inert.",
                remediation=(
                    "Confirm whether the tag is fired by sequencing from another tag, or attach "
                    "the intended trigger under a separate authorization."
                ),
            )
        )
    return findings


def _reference_status(
    graph: GtmDependencyGraph,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    rules: dict[str, GtmAuditRuleId] = {
        "TRIGGER": "GTM_TRIGGER_WITHOUT_REFERRER",
        "VARIABLE": "GTM_VARIABLE_WITHOUT_REFERRER",
    }
    for node in graph.nodes:
        rule = rules.get(node.resource_kind)
        if rule is None or node.inbound_edge_count:
            continue
        findings.append(
            _finding(
                rule_id=rule,
                dimension="REFERENCE_STATUS",
                status=status,
                severity="INFO",
                confidence="HIGH" if node.reference_status == "PROVEN_UNREFERENCED" else "LOW",
                risk="LOW",
                scope_kind=node.scope_kind,
                scope_ref=node.scope_ref,
                resource_refs=(node.resource_ref,),
                evidence_refs=(*evidence, node.configuration_fingerprint),
                observation=(
                    f"No edge in this scope reaches this resource; its classification is "
                    f"{node.reference_status}."
                ),
                effect=(
                    "This is reference status only. It is not a deletion decision and this build "
                    "compiles no GTM cleanup operation."
                ),
                remediation=(
                    "Record the reference status. Removal, if ever wanted, is separate staged work "
                    "in a dedicated workspace under its own authorization."
                ),
            )
        )
    return findings


def _sequencing(
    graph: GtmDependencyGraph,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for scope in graph.scopes:
        for cycle in scope.sequencing_cycles:
            findings.append(
                _finding(
                    rule_id="GTM_SEQUENCING_CYCLE",
                    dimension="SEQUENCING",
                    status=status,
                    severity="HIGH",
                    confidence="HIGH",
                    risk="HIGH",
                    scope_kind=cycle.scope_kind,
                    scope_ref=cycle.scope_ref,
                    resource_refs=cycle.member_refs[:4],
                    evidence_refs=evidence,
                    observation=(
                        f"{cycle.member_count} tags form a setup/teardown sequencing cycle in "
                        "this scope."
                    ),
                    effect="Sequenced firing order cannot be satisfied; tags may not fire.",
                    remediation="Break the cycle by removing one sequencing link.",
                )
            )
    return findings


def _zones(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for edge in inventory.edges:
        if edge.edge_kind == "ZONE_TRIGGER" and edge.resolution != "RESOLVED":
            findings.append(
                _finding(
                    rule_id="GTM_ZONE_BOUNDARY_UNRESOLVED",
                    dimension="ZONE_BOUNDARY",
                    status=status,
                    severity="MEDIUM",
                    confidence="HIGH",
                    risk="MEDIUM",
                    scope_kind=edge.scope_kind,
                    scope_ref=edge.scope_ref,
                    resource_refs=(edge.source_ref,),
                    evidence_refs=evidence,
                    observation=(
                        "A zone's custom evaluation trigger did not resolve inside this scope."
                    ),
                    effect="The boundary that decides where the zone applies cannot be evaluated.",
                    remediation="Repair the zone's evaluation trigger reference.",
                )
            )
    for resource in inventory.resources:
        boundary = resource.zone_boundary
        if boundary is None or not boundary.child_container_count:
            continue
        findings.append(
            _finding(
                rule_id="GTM_ZONE_RESTRICTION_UNVERIFIABLE_OUTSIDE_CONTAINER",
                dimension="ZONE_BOUNDARY",
                status=status,
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                scope_kind=resource.scope_kind,
                scope_ref=resource.scope_ref,
                resource_refs=(resource.resource_ref,),
                evidence_refs=(*evidence, resource.configuration_fingerprint),
                observation=(
                    f"This zone governs {boundary.child_container_count} child container(s) and "
                    f"declares {boundary.allowed_type_count} allowed type(s); the child container "
                    "is a separate identity this read never entered."
                ),
                effect=(
                    "Whether a resource restricted by this zone sits outside its boundary cannot "
                    "be determined from a single-container read."
                ),
                remediation=(
                    "Read the child container under its own authorization before judging the "
                    "zone's type restriction."
                ),
            )
        )
    return findings


def _tag_state(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    return [
        _finding(
            rule_id="GTM_PAUSED_TAG",
            dimension="TAG_STATE",
            status=status,
            severity="INFO",
            confidence="HIGH",
            risk="LOW",
            scope_kind=resource.scope_kind,
            scope_ref=resource.scope_ref,
            resource_refs=(resource.resource_ref,),
            evidence_refs=(*evidence, resource.configuration_fingerprint),
            observation="This tag is paused in this scope.",
            effect=(
                "A paused tag does not fire. Paused state is never evidence that the tag is "
                "unreferenced or safe to remove."
            ),
            remediation=(
                "Confirm with the customer whether the pause is intended; no state change is "
                "proposed here."
            ),
        )
        for resource in inventory.resources
        if resource.resource_kind == "TAG" and resource.paused is True
    ]


def _consent(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for resource in inventory.resources:
        if resource.resource_kind != "TAG":
            continue
        declared = resource.consent is not None and resource.consent.consent_status not in (
            "notSet",
            "UNDOCUMENTED_VALUE",
        )
        reviewed = (
            resource.provider_type is not None
            and resource.provider_type in _CONSENT_REVIEWED_TAG_TYPES
        )
        if declared and reviewed:
            continue
        # A declaration on a type this build has not reviewed is not evidence of
        # health: the meaning of that field for the type is unknown here.
        findings.append(
            _finding(
                rule_id=(
                    "GTM_CONSENT_SETTING_ABSENT" if reviewed else "GTM_CONSENT_TYPE_NOT_REVIEWED"
                ),
                dimension="CONSENT",
                status=status,
                severity="MEDIUM" if reviewed else "INFO",
                confidence="HIGH" if reviewed else "LOW",
                risk="MEDIUM" if reviewed else "LOW",
                scope_kind=resource.scope_kind,
                scope_ref=resource.scope_ref,
                resource_refs=(resource.resource_ref,),
                evidence_refs=(*evidence, resource.configuration_fingerprint),
                observation=(
                    "This tag declares no additional consent requirement."
                    if reviewed
                    else (
                        "This tag declares no additional consent requirement, and this build has "
                        "not reviewed its type's documented consent behaviour."
                    )
                ),
                effect=(
                    "The tag may send data before the customer's consent state allows it."
                    if reviewed
                    else "Consent behaviour for this tag type is unknown, not healthy."
                ),
                remediation=(
                    "Validate the tag against the customer's declared consent contract before any "
                    "change; this product never chooses consent policy."
                ),
            )
        )
    return findings


def _custom_code(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for resource in inventory.resources:
        if not resource.code_markers:
            continue
        kinds = sorted({marker.classification for marker in resource.code_markers})
        digests = tuple(
            sorted(
                {
                    marker.source_digest
                    for marker in resource.code_markers
                    if marker.source_digest is not None
                }
            )
        )
        findings.append(
            _finding(
                rule_id="GTM_CUSTOM_CODE_RESOURCE",
                dimension="CUSTOM_CODE",
                status=status,
                severity="INFO",
                confidence="HIGH",
                risk="MEDIUM",
                scope_kind=resource.scope_kind,
                scope_ref=resource.scope_ref,
                resource_refs=(resource.resource_ref,),
                evidence_refs=(*evidence, *digests)[:4],
                observation=(
                    f"{len(resource.code_markers)} custom-code surface(s) classified "
                    f"{', '.join(kinds)}; only counts and digests are recorded."
                ),
                effect=(
                    "Custom code is inspected locally and never placed in logs, evidence or "
                    "remote analysis."
                ),
                remediation=(
                    "Review the source in Tag Manager itself; this product neither exports nor "
                    "generates it."
                ),
            )
        )
    return findings


def _version_divergence(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    if inventory.live_latest.status == "LIVE_EQUALS_LATEST":
        return []
    undeterminable = inventory.live_latest.status == "UNDETERMINABLE"
    return [
        _finding(
            rule_id="GTM_LIVE_LATEST_DIVERGENCE",
            dimension="VERSION_DIVERGENCE",
            status=status,
            severity="HIGH",
            confidence="HIGH" if not undeterminable else "MEDIUM",
            risk="HIGH",
            scope_kind="OBSERVATION",
            scope_ref=inventory.identity.container_ref,
            resource_refs=(
                inventory.live_latest.live_version_ref,
                inventory.live_latest.latest_version_ref,
            ),
            evidence_refs=evidence,
            observation=(
                f"Live and latest container version identity is {inventory.live_latest.status}."
            ),
            effect=(
                "Staging fails closed until the customer reconciles explicitly; this product "
                "never calls set_latest or chooses a baseline."
            ),
            remediation=(
                "Return the divergence to the customer for an explicit reconciliation decision."
            ),
        )
    ]


def _workspace_hygiene(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for item in inventory.coverage:
        if item.collection != "workspace_changes" or item.status == "NOT_DOCUMENTED":
            continue
        if not item.resource_count:
            continue
        findings.append(
            _finding(
                rule_id="GTM_WORKSPACE_PENDING_CHANGES",
                dimension="WORKSPACE_HYGIENE",
                status=status,
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                scope_kind=item.scope_kind,
                scope_ref=item.scope_ref,
                resource_refs=(item.scope_ref,),
                evidence_refs=evidence,
                observation=f"This workspace holds {item.resource_count} pending change(s).",
                effect=(
                    "The workspace is dirty. Staging must use a new dedicated workspace and must "
                    "never resolve or sync away this customer work."
                ),
                remediation="Leave the customer workspace untouched and stage elsewhere.",
            )
        )
    if inventory.counts.workspace_merge_conflicts:
        findings.append(
            _finding(
                rule_id="GTM_WORKSPACE_MERGE_CONFLICTS",
                dimension="WORKSPACE_HYGIENE",
                status=status,
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                scope_kind="OBSERVATION",
                scope_ref=inventory.identity.container_ref,
                resource_refs=(inventory.identity.container_ref,),
                evidence_refs=evidence,
                observation=(
                    f"{inventory.counts.workspace_merge_conflicts} workspace merge conflict(s) "
                    "were reported across the inspected workspaces."
                ),
                effect="A conflicted workspace cannot be staged into or merged automatically.",
                remediation=(
                    "Return the conflict to the customer; this product never resolves one."
                ),
            )
        )
    return findings


def _coverage_gaps(
    inventory: GtmFullInventory,
    status: dict[GtmAuditDimension, ProviderCoverageStatus],
    evidence: tuple[str, str],
) -> list[GtmAuditFinding]:
    findings = []
    for item in inventory.coverage:
        if item.status in ("COMPLETE", "NOT_DOCUMENTED"):
            continue
        findings.append(
            _finding(
                rule_id="GTM_COVERAGE_GAP",
                dimension="COVERAGE",
                status=status,
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                scope_kind=item.scope_kind,
                scope_ref=item.scope_ref,
                resource_refs=(item.scope_ref,),
                evidence_refs=evidence,
                observation=(
                    f"The {item.collection} collection of this scope is {item.status} after "
                    f"{item.pages_read} page(s)."
                ),
                effect=(
                    "No resource in this container can be proven unreferenced while a surface is "
                    "unread."
                ),
                remediation="Re-read the container; do not act on an incomplete inventory.",
            )
        )
    if inventory.completeness.uninspected_workspaces:
        findings.append(
            _finding(
                rule_id="GTM_UNINSPECTED_WORKSPACE",
                dimension="COVERAGE",
                status=status,
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                scope_kind="OBSERVATION",
                scope_ref=inventory.identity.container_ref,
                resource_refs=(inventory.identity.container_ref,),
                evidence_refs=evidence,
                observation=(
                    f"{inventory.completeness.uninspected_workspaces} listed workspace(s) were "
                    "never inspected."
                ),
                effect=(
                    "An uninspected workspace can hold the only reference to a resource, so no "
                    "proof of absence holds."
                ),
                remediation="Inspect every accessible workspace before any dependency claim.",
            )
        )
    return findings


# ------------------------------------------------------- declined candidates


#: Every candidate action this rule table could imagine, the rules that would
#: motivate it, and the operation kind it would need.  Each one is declined:
#: `COMPILABLE_OPERATION_KINDS["gtm"]` is empty.
_CANDIDATES: Final[
    tuple[
        tuple[RecommendedOperationKind, NoRecommendationReason, tuple[GtmAuditRuleId, ...], str],
        ...,
    ]
] = (
    (
        "UPDATE",
        "OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        ("GTM_BROKEN_REFERENCE", "GTM_AMBIGUOUS_NAME_REFERENCE", "GTM_DUPLICATE_RESOURCE_NAME"),
        "Repairing a broken or ambiguous reference would be a staged GTM workspace change. This build compiles no GTM mutation, so no action is recommended.",
    ),
    (
        "UPDATE",
        "OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        ("GTM_TAG_WITHOUT_FIRING_TRIGGER", "GTM_ZONE_BOUNDARY_UNRESOLVED"),
        "Attaching or repairing a trigger would be a staged GTM workspace change. This build compiles no GTM mutation, so no action is recommended.",
    ),
    (
        "DELETE",
        "OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        ("GTM_TRIGGER_WITHOUT_REFERRER", "GTM_VARIABLE_WITHOUT_REFERRER"),
        "Reference status is not a deletion decision. Deletion requires a PROVEN_UNREFERENCED classification and separately authorized staged cleanup, neither of which this build compiles.",
    ),
    (
        "PAUSE",
        "OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        ("GTM_CONSENT_SETTING_ABSENT", "GTM_CUSTOM_CODE_RESOURCE"),
        "Pausing an unsafe or undeclared tag would be a staged GTM workspace change. This build compiles no GTM mutation, so no action is recommended.",
    ),
    (
        "UPDATE",
        "OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        ("GTM_SEQUENCING_CYCLE",),
        "Breaking a sequencing cycle would be a staged GTM workspace change. This build compiles no GTM mutation, so no action is recommended.",
    ),
    (
        "OBSERVE",
        "COVERAGE_NOT_READ",
        (
            "GTM_COVERAGE_GAP",
            "GTM_UNINSPECTED_WORKSPACE",
            "GTM_LIVE_LATEST_DIVERGENCE",
            "GTM_WORKSPACE_PENDING_CHANGES",
            "GTM_WORKSPACE_MERGE_CONFLICTS",
        ),
        "A re-read would refresh coverage, but this audit neither reads nor schedules one, and an incomplete observation must not be acted on.",
    ),
)


def _declined(
    findings: tuple[GtmAuditFinding, ...],
) -> tuple[tuple[NoRecommendation, ...], tuple[BoundExceeded, ...]]:
    declined = []
    exceeded = []
    for operation, reason, rules, detail in _CANDIDATES:
        matched = [finding for finding in findings if finding.rule_id in rules]
        if not matched:
            continue
        identifiers = tuple(sorted({finding.finding_id for finding in matched}))
        rule_ids = tuple(sorted({finding.rule_id for finding in matched}))
        inlined = identifiers[:MAXIMUM_INLINE_FINDING_IDS]
        declined.append(
            NoRecommendation(
                finding_ids=inlined,
                bound_finding_count=len(identifiers),
                bound_rule_ids=rule_ids,
                reason=reason,
                detail=detail,
                considered_operation_kind=operation,
            )
        )
        if len(inlined) < len(identifiers):
            exceeded.append(
                BoundExceeded(
                    scope="DECLINED_FINDING_IDS",
                    subject=f"{reason}:{operation}:{rule_ids[0]}",
                    bound=MAXIMUM_INLINE_FINDING_IDS,
                    total_finding_count=len(identifiers),
                    inlined_finding_count=len(inlined),
                    omitted_finding_count=len(identifiers) - len(inlined),
                    rule_ids=rule_ids,
                )
            )
    return tuple(declined), tuple(exceeded)


__all__ = [
    "MAXIMUM_FINDINGS",
    "RULE_TABLE_VERSION",
    "GtmAuditCoverage",
    "GtmAuditDimension",
    "GtmAuditFinding",
    "GtmAuditRuleId",
    "GtmWebAuditSections",
    "evaluate_gtm_web_audit",
]

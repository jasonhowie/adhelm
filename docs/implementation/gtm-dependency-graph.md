# GTM dependency graph, `PROVEN_UNREFERENCED`, and the read-only Web audit (M01.2)

This is the second half of the GTM inventory work. M01.1
([`gtm-inventory.md`](gtm-inventory.md)) reads one container and retains a
`gtm_web_inventory` observation: normalized resources, per-collection coverage,
and raw reference edges, with an explicit statement that it computes no
dependency proof. This ticket consumes that observation and computes the proof
— or, far more often, records exactly why the proof is refused.

Everything here is read-only and derived. `adhelm.gtm_dependency_graph` and
`adhelm.audits.gtm_web` are pure functions over a retained observation. They make
no provider request, read no credential, append no ledger record, compile no
plan, and stage, publish or delete nothing. The CLI command
`measurement gtm-graph` loads the selected record from verified local history and
emits the result; it is not retained (see "Retention" below).

## What the graph is

`build_gtm_dependency_graph(inventory, *, include_union=False)` returns one
`GtmDependencyGraph`:

- **Nodes** — one per retained resource, in its own scope: the live version, the
  latest version, the container (destinations and environments), and every
  inspected workspace. A node carries its collection, resource kind, provider
  type, configuration fingerprint, parent folder, paused state, custom-code
  marker count, outbound edge count, and its inbound-reference index.
- **Edges** — one per retained `GtmReferenceEdge`, with `resolution` preserved
  exactly and a `relation` classification added. Every documented edge kind maps
  to exactly one relation, so no reference is silently unclassified:

  | Relation | Edge kinds |
  | --- | --- |
  | `FIRING` / `BLOCKING` / `ENABLING` / `DISABLING` | `FIRING_TRIGGER`, `BLOCKING_TRIGGER`, `ENABLING_TRIGGER`, `DISABLING_TRIGGER` |
  | `SEQUENCING_SETUP` / `SEQUENCING_TEARDOWN` | `SETUP_TAG`, `TEARDOWN_TAG` |
  | `FOLDER_MEMBERSHIP` | `PARENT_FOLDER` |
  | `ZONE_BOUNDARY` | `ZONE_TRIGGER`, `ZONE_CHILD_CONTAINER` |
  | `TEMPLATE_INSTANCE` | `TEMPLATE_INSTANCE` |
  | `VARIABLE_REFERENCE` | `VARIABLE_REFERENCE` (including built-ins) |
  | `PARAMETER_REFERENCE` | `PARAMETER_TRIGGER_REFERENCE`, `PARAMETER_TAG_REFERENCE` |

- **Inbound index** — each node names the distinct sources that reach it, up to
  16 inline. Above that the node says `BOUNDED_PAGE_EDGES_BY_TARGET_REF` and the
  complete set stays reachable by paging `edges` and matching `target_ref`.
  Nothing is dropped and no count is clamped.
- **Scopes stay distinct.** GTM names and references resolve inside one workspace
  or one version, so mixing a workspace graph with a version graph would invent
  references that do not exist. `include_union=True` is the one opt-in that
  crosses scopes, and it is the view a deletion decision would need: see
  "Union view" below.
- **Sequencing cycles** — resolved setup/teardown edges inside one scope, found
  with an iterative Tarjan pass over references sorted by name, so the output is
  identical for any input order.

A container whose live version *is* its latest version reads one surface under
one reference. That scope records `scope_kind = "LATEST_VERSION"` with
`also_covers_scope_kinds = ("LIVE_VERSION",)` rather than being silently halved.

## Classification

Each node gets exactly one `reference_status`.

| Status | When |
| --- | --- |
| `REFERENCED` | At least one edge in the scope resolves to this node. This is positive evidence and it wins over every refusal reason, because a referenced resource is referenced whatever else the read could not cover. |
| `PROVEN_UNREFERENCED` | No inbound edge **and** no blocking reason applies. |
| `UNPROVABLE_<reason>` | No inbound edge, and at least one blocking reason applies. The status names the first reason in precedence order; `blocking_reasons` lists every one that applies. |

The blocking reasons, in reporting precedence:

| Reason | Meaning |
| --- | --- |
| `BUILT_IN_RESOURCE` | The node is a built-in variable. It is a container setting rather than a deletable resource, and a `{{name}}` reference to one is indistinguishable from a reference to a user-defined variable of the same name. |
| `TEMPLATE_WITH_UNBOUND_INSTANCES` | The node is a custom template and the scope holds a template instance (`cvt_<container>_<id>`) that did not resolve to a template this read saw. That instance may belong to any template in the scope. |
| `SCOPE_COVERAGE_INCOMPLETE` | A collection coverage entry in this scope is neither `COMPLETE` nor `NOT_DOCUMENTED`. |
| `SCOPE_UNRESOLVED_EDGE_PRESENT` | Some edge in this scope is `UNRESOLVED_*`. An unresolved, ambiguous or malformed reference could name any resource, so it disables the proof for the *whole* scope, not only for its apparent target. |
| `CONTAINER_COVERAGE_INCOMPLETE` | A coverage entry anywhere in the container is incomplete. |
| `CONTAINER_UNRESOLVED_EDGE_PRESENT` | Some edge anywhere in the container is `UNRESOLVED_*`. |
| `UNINSPECTED_WORKSPACE_PRESENT` | A listed workspace was never inspected. |
| `REFERENCE_COVERAGE_BOUNDED_INCOMPLETE` | `GtmFullInventory.reference_coverage` is `BOUNDED_INCOMPLETE`: the declared reference-edge bound was crossed, so a referring edge may have been bounded away. This is M01.1's single fail-open failure mode and the one flag this engine must consult. |
| `LIVE_LATEST_UNDETERMINABLE` | Live-versus-latest identity is `UNDETERMINABLE` — one identity says the versions are equal and the other says they differ. |

The last five are container-wide by design.
[`measurement-control.md`](../measurement-control.md) states that a resource is
`PROVEN_UNREFERENCED` only when *every* applicable resource surface, supported
reference form, and accessible pending workspace has been inventoried across
live and latest versions. The deletion rule is therefore about the container, not
about one scope: a gap anywhere refuses the proof everywhere.

### What is deliberately not an input

Age, name, paused state, folder membership, and apparent inactivity are **not**
classification inputs (AGENTS rule 11). A resource named `OLD legacy 2019
unused`, paused, and sitting in an "archive" folder gets exactly the
classification the same resource gets without any of that. They are audit
signals, never deletion proof.
`tests/test_gtm_dependency_graph.py::test_age_name_paused_and_folder_are_not_classification_inputs`
pins both directions: the decorated resource is not demoted, and a paused but
referenced tag is not promoted.

### Union view

`include_union=True` adds `GtmUnionClassification`, one entry per
`(collection, resource_client_ref)` across every scope that holds it. It is the
most conservative member: unprovable in any scope makes the union entry
unprovable; referenced in any scope makes it referenced; only unreferenced-and-
unblocked everywhere yields `PROVEN_UNREFERENCED`. This is deliberately opt-in
because it is the only view that crosses scope boundaries.

## Audit rules

`adhelm/audits/gtm_web.py` evaluates a fixed rule table (`gtm-web-audit-rules-1`)
over the inventory and the graph.

| Rule | Dimension | Fires when |
| --- | --- | --- |
| `GTM_BROKEN_REFERENCE` | `REFERENCE_INTEGRITY` | An edge is `UNRESOLVED_NO_MATCHING_RESOURCE` or `UNRESOLVED_MALFORMED`. |
| `GTM_AMBIGUOUS_NAME_REFERENCE` | `NAMING` | An edge is `UNRESOLVED_AMBIGUOUS_NAME`. |
| `GTM_DUPLICATE_RESOURCE_NAME` | `NAMING` | Two or more resources of one kind in one scope share a name digest. |
| `GTM_TAG_WITHOUT_FIRING_TRIGGER` | `TAG_FIRING` | A tag declares no firing trigger, excluding the Google-tag types whose firing is governed elsewhere. |
| `GTM_TRIGGER_WITHOUT_REFERRER` / `GTM_VARIABLE_WITHOUT_REFERRER` | `REFERENCE_STATUS` | No edge in the scope reaches the trigger or variable. **Severity `INFO`, reference status only.** Confidence is `HIGH` only when the graph proved it and `LOW` otherwise, and `deletion_recommended` is a hard `False`. |
| `GTM_SEQUENCING_CYCLE` | `SEQUENCING` | Resolved setup/teardown edges form a cycle in one scope. |
| `GTM_ZONE_BOUNDARY_UNRESOLVED` | `ZONE_BOUNDARY` | A zone's custom evaluation trigger did not resolve. |
| `GTM_ZONE_RESTRICTION_UNVERIFIABLE_OUTSIDE_CONTAINER` | `ZONE_BOUNDARY` | A zone governs a child container. See the note below. |
| `GTM_PAUSED_TAG` | `TAG_STATE` | A tag is paused. `INFO`, and the finding states in prose that pause is never removal evidence. |
| `GTM_CONSENT_SETTING_ABSENT` | `CONSENT` | A tag of a reviewed consent-documenting type declares `notSet` or no consent setting. |
| `GTM_CONSENT_TYPE_NOT_REVIEWED` | `CONSENT` | The same absence on a tag type this build has not reviewed. Reported as unknown, never as healthy. |
| `GTM_CUSTOM_CODE_RESOURCE` | `CUSTOM_CODE` | A resource carries custom-code markers. Counts, classifications and digests only; no source. |
| `GTM_LIVE_LATEST_DIVERGENCE` | `VERSION_DIVERGENCE` | Live and latest are not `LIVE_EQUALS_LATEST`. |
| `GTM_WORKSPACE_PENDING_CHANGES` / `GTM_WORKSPACE_MERGE_CONFLICTS` | `WORKSPACE_HYGIENE` | A workspace reports pending changes or merge conflicts. |
| `GTM_COVERAGE_GAP` / `GTM_UNINSPECTED_WORKSPACE` | `COVERAGE` | A coverage entry is incomplete, or a listed workspace was never inspected. |

The reviewed consent-documenting tag types are `gaawe`, `gaawc`, `googtag`,
`awct`, `sp`, `flc`, and `fls`. An unlisted type is never reported healthy: it
gets `GTM_CONSENT_TYPE_NOT_REVIEWED` instead, so the absence is always visible
alongside an honest statement of how much this build knows about it.

**Zone-restricted resources outside their zone cannot be evaluated from a
single-container read.** A zone's `typeRestriction` governs a *child container*,
which is a separate identity, a separate authorization, and a separate read.
`GtmZoneBoundary` retains the allowed-type *count*, not the type list. Rather
than invent a check, the `ZONE_BOUNDARY` dimension drops to `PARTIAL` whenever a
zone names a child container and the rule above states exactly what was not
observable. A container with no zones reports `NOT_APPLICABLE`.

Every dimension carries exactly one coverage status —
`COVERED` / `PARTIAL` / `NOT_READ` / `NOT_APPLICABLE` — derived from the
inventory's own coverage entries, and the model rejects sections that omit one.
Every finding cites record digests: the ledger record digest of the observation,
its snapshot digest, and, where one resource is named, that resource's
configuration fingerprint.

### No recommendation is compilable

`COMPILABLE_OPERATION_KINDS["gtm"]` is `frozenset()`. There is no recommendation
list on `GtmWebAuditSections` at all; there is
`recommendation_status = NO_COMPILABLE_GTM_OPERATION_EVERY_CANDIDATE_DECLINED`
and a `no_recommendations` list of typed `NoRecommendation` records. Repairing a
reference, attaching a trigger, pausing a tag, and breaking a cycle are all
declined with `OPERATION_NOT_COMPILABLE_FOR_PROVIDER`; a coverage gap is declined
with `COVERAGE_NOT_READ` against a considered `OBSERVE`.

Deletion is declined explicitly and separately. Cleanup — consolidating,
tightening, renaming, pausing, migrating and removing resources inside a
dedicated workspace — is later, separately authorized work, and reference status
is not a deletion decision even when the graph proves it.

### Why `gtm` is not in `_RULE_MODULES`

`adhelm/audits/__init__.py` documents a provider-rule seam whose entry point is
`adhelm.audit.audit_provider(report: NormalizedReport, ...)`: an ad performance
report with a data window, spend and delivery rows. GTM produces no such report.
Registering `gtm` in `_RULE_MODULES` would either require manufacturing a report
to satisfy the dispatch signature — the fabricated result AGENTS rule 20 forbids
— or dead-end the dispatch. So the GTM rule table keeps its own entry point,
`evaluate_gtm_web_audit(inventory=..., graph=..., ...)`, and imports the seam's
*vocabulary* (`ProviderCoverageStatus`, `NoRecommendation`,
`MAXIMUM_INLINE_FINDING_IDS`, `RecommendedOperationKind`) rather than redefining
it. The one edit to `audits/__init__.py` is the `"gtm": frozenset()` entry in
`COMPILABLE_OPERATION_KINDS`, which is enforced by the shared
`ProviderRecommendation` validator.

`GtmAuditFinding` is likewise not `adhelm.audit.AuditFinding`: that model requires
a report data window (window start and end, source timezone, attribution model)
that a configuration read does not have and must not invent. The finding
identifier scheme, severity, confidence and risk vocabularies are the shared
ones, and `automatic_write_allowed` is the same hard `False`.

## Stage gate on retained evidence

`measurement check-stage` validates a `ContainerInventory` the caller supplies.
`adhelm.measurement` now also builds that structure from a retained observation:

- `container_inventory_from_gtm_inventory(inventory, *, workspace_limit)` projects
  the observation onto `ContainerInventory`; and
- `validate_gtm_stage_gate(inventory, *, workspace_limit)` runs
  `validate_stage_gate` over it, with one extra precondition in front.

Both are additive. No existing validator's output changes for any existing input.

Mapping notes:

- The ten listable workspace collections map onto the ten `ResourceSurface`
  members. `destinations` and `environments` are documented only at container
  scope and have no surface member; `workspace_changes` and `reference_edges` are
  statuses rather than collections. `CUSTOM_HTML` and `CUSTOM_TEMPLATE` are code
  surfaces *inside* a tag or template, so they are covered by `TAG` and
  `TEMPLATE` and are deliberately not claimed as separate inventoried surfaces.
- Every unresolved, ambiguous, malformed or out-of-scope edge becomes
  `DYNAMIC_OR_UNKNOWN`, never `BROKEN`: a `BROKEN` edge in that model must name
  the target it failed to reach, and an unresolved GTM reference by definition
  names none. That is the fail-closed direction —
  `ContainerInventory.reference_status` then reports `DYNAMIC_OR_UNKNOWN` and can
  never report `PROVEN_UNREFERENCED`. The real classification lives in the graph
  module; the projection exists for the gate.
- A container whose live version is its latest version projects that one scope
  onto both required context names, while each node and edge keeps a single
  context: `ContainerInventory` requires unique node references.

The extra precondition in `validate_gtm_stage_gate` is that the observation's own
`completeness.status` must be `COMPLETE_WITHIN_DECLARED_BOUNDS`. That status is
`INCOMPLETE` whenever a collection was bounded, a listed workspace was left
uninspected, *or* an emitted reference edge did not resolve. An unresolved
reference is not a coverage gap, but it does mean the container's reference set
is not proven, and staging into a container whose references this product cannot
follow is the fail-open direction rule 9 forbids.

`workspace_limit` is the one value the Tag Manager API does not expose: the
allowance depends on the customer's Tag Manager edition and no documented `GET`
returns it. It is therefore a declared input, and `measurement gtm-graph` reports
`stage_gate.outcome = NOT_EVALUATED` with code `WORKSPACE_LIMIT_REQUIRED` when it
is absent, rather than guessing one.

## CLI

```text
adhelm measurement gtm-graph <record-digest>
    [--section NODES|EDGES|FINDINGS|COVERAGE]
    [--scope-ref <reference>] [--workspace-limit <n>] [--union]
    [--cursor <n>] [--limit <n>]
```

The envelope always carries the graph and audit digests, the source record,
snapshot and checkpoint digests, live/latest status, `reference_coverage`,
inventory completeness, container blocking reasons, counts, per-scope summaries,
the rule counts, the declined outcomes, the stage-gate result, and the
`deletion_authority` / `cleanup_operations` statements. Rows are paginated with
the same shrink loop `measurement show` uses, so a large graph never truncates
silently: the page shrinks and `next_cursor` reports the remainder.

`measurement show` now also surfaces `reference_coverage` for a
`gtm_web_inventory` observation. That is the M01.1 follow-up: the flag a
dependency proof must consult before reading a missing edge as evidence was
retained but never shown for review.

## Retention

The graph and audit are **not** retained as a measurement observation. They are a
deterministic function of an observation the ledger already holds: the graph
binds `inventory_digest`, the audit binds `graph_digest` and the source record
digest, and re-running the command over the same record reproduces both digests
exactly. Retaining them would add a fifth `MeasurementSnapshot` union member and a
second copy of evidence that adds no fact, so the command is emit-only and says
so in `retention.graph_retained = false` with
`reason = DERIVED_DETERMINISTICALLY_FROM_A_RETAINED_OBSERVATION`. If a later
ticket needs the graph in history — for example to compare two graphs over time —
that is a closed additive snapshot kind with its own golden-digest test.

## Bounds

| Bound | Value |
| --- | --- |
| Graph nodes | 10,000 (the inventory's own resource bound) |
| Graph edges | 50,000 (the inventory's hard emission capacity) |
| Scopes | 1,100 |
| Inline inbound sources per node | 16, then `BOUNDED_PAGE_EDGES_BY_TARGET_REF` |
| Cycle members inlined | 32, then `BOUNDED_PAGE_EDGES_BY_SCOPE` |
| Cycles per scope | 256 |
| Audit findings | 5,000, above which the command fails closed rather than truncating |
| Inline finding identifiers per declined outcome | `MAXIMUM_INLINE_FINDING_IDS` (32), shared with the provider-audit seam |

## Evidence and limits

This subset is `FIXTURE_VERIFIED`. Every test builds its container from the
M01.1 fixture helpers in `tests/test_gtm_inventory.py` through
`httpx.MockTransport`; no live GTM container, credential or network call is
involved, and `tests/conftest.py` blocks the network.

It proves **no** live GTM behaviour, no GTM UI reconciliation, no two-
organization coverage, and no cleanup, staging or publish capability. It does not
promote the `GTM Web API/browser` capability-matrix row, which stays
`UNRESEARCHED` for every operation cell. A `PROVEN_UNREFERENCED` classification
from this engine is a statement about one retained observation of one container;
it is never authorization to delete anything, and no cleanup operation exists to
act on it.

## Review repairs (2026-09-09)

An interrupted independent review left ten findings; the coordinator repaired
nine on this branch against the reviewer's tests (`tests/test_gtm_dependency_graph_review.py`):

- The stage gate fails closed with `WORKSPACE_CONFLICT` when the live/latest
  status is anything but `LIVE_EQUALS_LATEST` (an `UNDETERMINABLE` status is a
  refusal to decide, not a proof) and when a workspace reports a merge conflict.
- A scope view never publishes `PROVEN_UNREFERENCED` for a resource another
  scope of the same container references; such nodes carry the blocker
  `REFERENCED_IN_ANOTHER_SCOPE`. The union view treats that reference as
  positive evidence.
- A consent declaration on a tag type this build has not reviewed produces
  `GTM_CONSENT_TYPE_NOT_REVIEWED`; it is never reported healthy.
- `REFERENCE_INTEGRITY` and `REFERENCE_STATUS` coverage drops to `PARTIAL` when
  any referrer collection was bounded, not only when edge emission was bounded.
- A declined outcome that cannot inline every finding identifier records a
  `BoundExceeded` entry in `bound_exceeded`.
- The finding bound (5,000) is reported, not fatal: findings are truncated in
  their deterministic order, `omitted_finding_count` and
  `finding_bound_status = TRUNCATED_AT_BOUND` are set, and rule counts stay
  complete.
- `--union` selects a paged `UNION` section instead of embedding the whole
  container-wide list in the summary.
- The reader inventories a version once when live and latest are the same
  version (a real container's normal state), under the `LIVE_VERSION` scope.

The tenth finding, a full-inventory observation exceeding the 1 MiB ledger
record bound at roughly 1,150 resources (one ninth of the reader's own bound),
was open as a strict xfail tracked as `M01.1.retention`. It is repaired: such an
observation is now retained as bounded chunk records plus a commit record, and
the reviewer's test passes unchanged in intent. See
[Retention beyond one record](gtm-inventory.md#retention-beyond-one-record).

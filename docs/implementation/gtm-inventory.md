# GTM Web complete fixed inventory (M01.1)

Status: fixture-tested library composition. It is not live capability evidence,
not a dependency proof, and not a staging, publication, or deletion decision.

This record covers `src/adhelm/gtm_inventory.py`, the additive
`inventory_with_sources` entry point on `GtmReadClient`, and the
`read_gtm_full_inventory` / `read_gtm_selected` workflow functions in
`src/adhelm/gtm_measurement_workflow.py`. It completes the fixed *read* half of
[`definition-of-done.md`](../definition-of-done.md) section 3 inventory line and
produces the input the separate M01.2 dependency engine consumes. It computes no
`PROVEN_UNREFERENCED` classification.

## Read plan

Every read is a documented Tag Manager API v2 `GET` against
`https://tagmanager.googleapis.com/tagmanager/v2`, under the single declared
scope `https://www.googleapis.com/auth/tagmanager.readonly`. There is no
generic request method and no write route.

1. Account and container identity are re-verified by the existing
   `GtmIdentityClient` (`accounts/{account}` then
   `accounts/{account}/containers/{container}`, `usageContext` exactly
   `["web"]`). The inventory reader accepts only the exact identity object that
   discovery observed.
2. The existing fixed read runs unchanged: latest version header, live version,
   header-selected latest version, bounded workspace pages, then the latest
   header and live version rereads. A changed view still fails
   `INVENTORY_INCONSISTENT`.
3. The complete inventory then normalizes the live and latest
   `ContainerVersion` bodies that step 2 already retrieved. No extra version
   request is issued and no raw body leaves the call.
4. Per accessible workspace, the ten documented workspace-scoped collections are
   listed with full `pageToken` consumption.
5. `destinations` is listed once at container scope, and `environments` is
   listed at container scope with full `pageToken` consumption.
6. Per accessible workspace, `workspaces.getStatus` reports that workspace's
   pending changes and merge conflicts. Only the two counts are retained.

| Scope | Collection | Route | Response array |
| --- | --- | --- | --- |
| workspace | tags | `{workspace}/tags` | `tag` |
| workspace | triggers | `{workspace}/triggers` | `trigger` |
| workspace | variables | `{workspace}/variables` | `variable` |
| workspace | folders | `{workspace}/folders` | `folder` |
| workspace | built_in_variables | `{workspace}/built_in_variables` | `builtInVariable` |
| workspace | templates | `{workspace}/templates` | `template` |
| workspace | zones | `{workspace}/zones` | `zone` |
| workspace | clients | `{workspace}/clients` | `client` |
| workspace | transformations | `{workspace}/transformations` | `transformation` |
| workspace | gtag_configs | `{workspace}/gtag_config` | `gtagConfig` |
| container | destinations | `{container}/destinations` | `destination` |
| container | environments | `{container}/environments` | `environment` |
| workspace | workspace_changes | `{workspace}/status` | `workspaceChange` (counted only) |
| live/latest version | the ten collections above | embedded in `ContainerVersion` | `tag`, `trigger`, `variable`, `folder`, `builtInVariable`, `customTemplate`, `zone`, `client`, `transformation`, `gtagConfig` |

`destinations` and `environments` are documented only at container scope, so
workspace and version scopes record them `NOT_DOCUMENTED` and never call them.
`ContainerVersion` documents neither array, so the version scopes record the
same status. Pending changes belong to a workspace, so the container and
version scopes record `workspace_changes` as `NOT_DOCUMENTED`. Every scope also
records a `reference_edges` entry for its own reference-edge bound. The exact
routes, response fields, and page-token surfaces are recorded with their fetch
date in [`gtm-read-contract.md`](../research/gtm-read-contract.md).

`environments` is required by
[`measurement-control.md`](../measurement-control.md) ("GTM Web contract /
Inventory"). It is a documented, bounded, page-token-bearing container
collection, so it is read rather than statused `NOT_READ_BY_DESIGN`. An
`Environment` carries an `authorizationCode`, a preview `url`, and a
`tagManagerUrl`; none of those is normalized. Only the fields every other
resource keeps survive.

Workspace pending changes are also required there. `workspaces.getStatus` is
documented with a closed, stable response, so it is read; but a
`workspaceChange` entry embeds a whole changed tag, trigger, or variable body,
so only the number of changes and the number of merge conflicts is retained, in
`counts.workspace_pending_changes` and `counts.workspace_merge_conflicts`.
Normalizing those embedded bodies would copy customer configuration, including
custom code, into the observation through a second undocumented path.

## Coverage statuses

Pagination is proven complete or explicitly bounded. Every bound is reported:

| Status | Meaning |
| --- | --- |
| `COMPLETE` | Every documented page was consumed and the provider returned no further `nextPageToken`. |
| `INCOMPLETE_PAGE_BOUND` | The 20-page bound was reached while a `nextPageToken` remained. |
| `INCOMPLETE_ITEM_BOUND` | The 4,000-item per-collection bound would have been exceeded. |
| `INCOMPLETE_REQUEST_BOUND` | The 1,000-request per-inventory bound was reached. |
| `INCOMPLETE_WORKSPACE_BOUND` | The workspace was listed but not inspected; the 25-workspace inspection bound was reached. |
| `INCOMPLETE_RESOURCE_BOUND` | The 10,000-resource per-inventory bound was reached. |
| `INCOMPLETE_UNDOCUMENTED_PAGE_TOKEN` | The response advertised a `nextPageToken` for a collection whose documented request surface has no `pageToken` parameter. This is the `destinations` case. |
| `INCOMPLETE_EDGE_BOUND_EXCEEDED` | The scope's reference edges crossed the declared 20,000-edge bound. Recorded on the `reference_edges` entry with the edges emitted for that scope and the number, if any, dropped past the hard capacity. |
| `NOT_DOCUMENTED` | The collection is not documented for that scope and was never called. |

`GtmInventoryCompleteness.status` is `COMPLETE_WITHIN_DECLARED_BOUNDS` only when
no coverage entry is incomplete, no listed workspace was left uninspected, and
every emitted edge resolved. Otherwise it is `INCOMPLETE`. It always carries
`dependency_proof = NOT_COMPUTED_REQUIRES_DEPENDENCY_ENGINE` and
`deletion_authority = NEVER_ESTABLISHED_BY_INVENTORY`.

### The reference-edge bound, and what M01.2 must consult

The reference-edge bound is the one bound whose failure mode is fail-*open*: a
resource whose only referring edge was dropped looks unreferenced, which is
exactly the input the `PROVEN_UNREFERENCED` engine must never be handed
silently. It is therefore split in two.

| Bound | Value | Effect |
| --- | --- | --- |
| Declared reference-edge bound | 20,000 | Crossing it drops nothing. It sets the scope's `reference_edges` status to `INCOMPLETE_EDGE_BOUND_EXCEEDED`, makes the observation `INCOMPLETE`, and sets `reference_coverage = BOUNDED_INCOMPLETE`. Past this point the reader stops claiming a complete reference set even though this particular read still emitted every edge. |
| Hard emission capacity | 50,000 | Drafts past it are actually dropped. Each is counted in the owning scope's `dropped_count` and in `counts.dropped_edges`, and the same `INCOMPLETE_EDGE_BOUND_EXCEEDED` status applies. |

`GtmFullInventory.reference_coverage` is the single flag M01.2 must consult
before treating the absence of an edge as evidence. It is
`COMPLETE_WITHIN_DECLARED_BOUNDS` or `BOUNDED_INCOMPLETE`, and its value is
re-derived from the coverage entries by the model validator, so a payload
cannot claim complete reference coverage while carrying a bounded scope.

`counts.out_of_scope_edges` counts `OUT_OF_SCOPE_CHILD_CONTAINER` edges, which
are deliberately **not** unresolved edges (see "Reference edges" below).

Malformed responses are a different class and still fail closed rather than
degrade coverage: unsupported response fields, non-list collections, duplicate
native identifiers, a repeated page token, a path outside the configured
account/container parent, and an oversize single resource all raise
`PROVIDER_RESPONSE_INVALID`.

## Record shapes

`GtmFullInventory` carries the exact observed `GtmReadIdentity`, the unchanged
`GtmReadInventory` under `base`, `read_started_at` / `read_finished_at`,
`live_latest`, `coverage`, `resources`, `edges`, `counts`, and `completeness`.
It is a closed `StrictModel`, so the shared privacy validator runs over every
persisted field automatically. Coverage, resources, and edges are sorted by
their exact local references, so the observation does not depend on the order
in which the provider returned its pages.

`base` is ordering-normalized the same way before it is embedded or returned:
its `workspaces` are sorted by `workspace_ref`, exactly as the default
`measurement gtm` path sorts them before retention, and each version's
`opaque_code` is sorted by its kind and configuration fingerprint. Previously
`base` was the reader's result verbatim, so one unchanged container read twice
could produce two different canonical payloads and two different retained
`snapshot_digest` values — which defeats "identical input yields an identical
digest" and any reconciliation over these observations. The default path is
unchanged: `GtmReadClient` still returns provider order, and only the complete
inventory normalizes it.

`counts.requests` counts the collection requests this reader issues; the six
fixed reads the existing `GtmReadClient` makes are already described by `base`.
Alongside the existing counters, `counts` carries `out_of_scope_edges`,
`dropped_edges`, `workspace_pending_changes`, and `workspace_merge_conflicts`.

`GtmInventoryResource` holds the native path, scope and collection, canonical
`resource_ref`, native identifier, provider `type`, name, keyed digests of the
name and the provider `fingerprint`, a keyed configuration fingerprint over the
whole resource, `parentFolderId` as a local reference, `paused`,
`consentSettings`, zone boundary counters, and custom-code markers.

Every optional retained field carries its own `*_status` companion, so a null is
never ambiguous between "the provider supplied nothing" and "the reader would
not keep what it supplied": `name_status` and `provider_type_status`
(`PRESENT` / `ABSENT` / `WITHHELD_OVERSIZE` / `WITHHELD_SENSITIVE`), and
`native_path_status` and `provider_fingerprint_digest_status`
(`PRESENT` / `ABSENT` / `WITHHELD_UNSUPPORTED`, the last for a documented field
returned in a form the reader cannot use). A model validator rejects a record
whose value and status disagree.

A name is kept only when it is at most 256 characters, control-character free,
and passes `validate_data`. Otherwise it is withheld as `WITHHELD_OVERSIZE` or
`WITHHELD_SENSITIVE` and only its keyed digest survives. Everything else in a
parameter tree is either resolved into an edge or reduced to the keyed
configuration fingerprint; no other raw parameter value is persisted.

`GtmCodeMarker` classifies a custom-code surface as `CUSTOM_HTML`,
`CUSTOM_JAVASCRIPT`, `CUSTOM_TEMPLATE`, `TEMPLATE_INSTANCE`, or `BUILT_IN`, and
records the parameter key, a keyed digest of the source, and the source byte
length. `source_status` is `DIGESTED_NEVER_PERSISTED` or `NO_SOURCE`. The
source itself is never returned, logged, exported, or placed in any
envelope. Classification rules:

- a `html` tag's `html` parameter is `CUSTOM_HTML`;
- a `jsm` variable's `javascript` parameter is `CUSTOM_JAVASCRIPT`;
- a custom template's `templateData` is `CUSTOM_TEMPLATE`;
- a resource whose provider `type` matches `cvt_<container>_<template>` is
  additionally `TEMPLATE_INSTANCE`;
- a built-in variable is `BUILT_IN` with no source; and
- any other parameter value containing a `<script>` marker or a `javascript:`
  URI is classified and hashed as script-bearing. That last rule is an explicit
  bounded heuristic, not a proof that the remaining values are script-free. A
  value longer than the 64 KiB scan bound is classified conservatively rather
  than skipped.

Classification scans the resource's `parameter` tree and, for a trigger, the
`parameter` arrays nested in every documented `filter`, `customEventFilter`, and
`autoEventFilter` condition. A trigger condition value is as free-form as a
tag's own parameter, so scanning only the top-level tree left script-bearing
condition values neither digested nor counted in
`counts.custom_code_resources`.

The heuristic half of the rule is also enforced on the *other* side of the
module: any string a classification would call custom code is withheld wherever
else it would be published, in particular as a reference edge's selector. A
resource name that is itself script is still script.

## Reference edges (the M01.2 input)

`GtmReferenceEdge` records raw references only. Each edge names its source
resource, edge kind, target kind, selector form (`NATIVE_ID`, `NAME`,
`PUBLIC_ID`, `TEMPLATE_TYPE`, `NONE`), the selector when it is short and
privacy-clean, the selector's keyed digest, the resolved target reference when
one exists, a resolution status, and an occurrence count for de-duplicated
repeats.

Emitted edge kinds: `FIRING_TRIGGER`, `BLOCKING_TRIGGER`, `SETUP_TAG`,
`TEARDOWN_TAG`, `ENABLING_TRIGGER`, `DISABLING_TRIGGER`, `PARENT_FOLDER`,
`PARAMETER_TRIGGER_REFERENCE`, `PARAMETER_TAG_REFERENCE`, `VARIABLE_REFERENCE`,
`ZONE_TRIGGER`, `ZONE_CHILD_CONTAINER`, and `TEMPLATE_INSTANCE`.

Resolution happens only inside one scope, because GTM names are scoped to a
workspace or version. Statuses are `RESOLVED`,
`UNRESOLVED_NO_MATCHING_RESOURCE`, `UNRESOLVED_AMBIGUOUS_NAME`,
`UNRESOLVED_MALFORMED`, `UNRESOLVED_NOT_INSPECTED`, and
`OUT_OF_SCOPE_CHILD_CONTAINER`.

A zone child container is a different container, and this inventory is scoped to
one container by design. That is not a gap in a bounded read, so it has its own
status and is counted in `counts.out_of_scope_edges` rather than in
`counts.unresolved_edges`. Otherwise any container using zones would report
`INCOMPLETE` forever, for a reason the read was never trying to cover, and the
signal that a bounded read really did miss something would be lost in it.
Crossing into the child container remains out of scope: it is a separate
identity, a separate authorization, and a separate read.

Names are matched by digest, never by text. A resource's name is digested in the
`name` HMAC domain and indexed; a name-form selector is digested in the same
domain purely for that lookup, while the digest persisted on the edge keeps its
own `selector` domain. Matching on the name domain is what lets a reference to a
resource whose name was withheld as oversize or sensitive still resolve to that
resource: before this, the two domains differed, the digest lookup could never
hit, and such a reference was permanently `UNRESOLVED_NO_MATCHING_RESOURCE`.

`{{ }}` scanning covers scalar members of JSON arrays as well as object values.
Resource bodies are not schema-closed, so a `{{Page URL}}` token sitting
directly inside a string array would otherwise produce no edge at all, which is
the fail-open direction for M01.2.

`{{ }}` parsing runs over every bounded string in a resource, including trigger
conditions, zone boundaries, and custom-code sources. Custom HTML and custom
template source legitimately carry `{{name}}` references, and the definition of
done requires them; only the reference token inside the braces is extracted,
never the surrounding source. Parsing is deliberately over-inclusive on the
source side (a resource name containing braces also produces an edge), because
an extra unresolved edge makes the read INCOMPLETE rather than falsely clean. Only a brace-free innermost reference counts,
so `{{outer{{Page URL}}tail}}` yields `Page URL` and the surrounding text stays
unparsed. An unbalanced reference produces an explicit `UNRESOLVED_MALFORMED`
edge, and a string beyond the scan bound produces an edge with
`selector_status = NOT_SCANNED_OVERSIZE`. A `{{name}}` may resolve to a
user-defined or a built-in variable; two resources sharing one name resolve to
neither and become `UNRESOLVED_AMBIGUOUS_NAME`.

`Zone.typeRestriction.whitelistedTypeId` restricts resource *types*, not
resource identifiers, so no zone-to-tag edge is documented for v2 and none is
invented.

## Live versus latest

`GtmLiveLatestStatus` records both version references, both keyed configuration
fingerprints, and one of `LIVE_EQUALS_LATEST`, `LIVE_DIFFERS_FROM_LATEST`, or
`UNDETERMINABLE`. Equality requires both the header-selected latest version
reference and its configuration fingerprint to match the live read; a
contradiction between the two signals is `UNDETERMINABLE`, never a guess.
`staging_eligibility` stays `NOT_EVALUATED`: staging remains an M04 decision
that also requires an available workspace slot.

## Errors

`401` and `403` are retyped, for every GTM read, as `GTM_AUTHORIZATION_REJECTED`
and `GTM_ACCESS_NOT_PERMITTED` with exit code 4; see
[GTM auth lifecycle](gtm-auth-lifecycle.md).
`429` and `5xx` remain the shared transport's precondition failures at exit code
3 after its bounded read retries. Oversize responses fail
`RESPONSE_TOO_LARGE` inside `SafeHttpClient` before any body is parsed. No
provider response body, header, name, or credential appears in any error
message.

## Command contract

```text
adhelm measurement gtm PROFILE CONTAINER_ID [--full-inventory] [--ci-enabled]
```

`--full-inventory` is opt-in and defaults off. `GtmReadRequest` carries the same
discriminator as `full_inventory`, so an existing JSON input that omits it
routes byte-identically to the existing fixed read: same request sequence, same
`GtmMeasurementSnapshot`, same snapshot digest.

| Aspect | Without the flag | With `--full-inventory` |
| --- | --- | --- |
| Workflow | `read_gtm` | `read_gtm_full_inventory` |
| Retained snapshot | `GtmMeasurementSnapshot` | `GtmFullInventoryMeasurementSnapshot` |
| `capability_version` | `gtm-versions-workspace-metadata-1` | `gtm-full-fixed-inventory-1` |
| Ledger record kind | `MEASUREMENT_OBSERVATION` | `MEASUREMENT_OBSERVATION`, or chunk records plus `CHUNKED_MEASUREMENT_OBSERVATION` past one record |
| Ledger `provider` | `gtm` | `gtm_web_inventory` |
| `measurement show` rows | version summaries, opaque-code markers, workspace metadata | collection coverage, resources, reference edges |
| `measurement compare` | supported | refused `NOT_COMPARABLE_FULL_INVENTORY` (exit 2) |

The two snapshot kinds are separate provider-discriminated union members rather
than one model with an optional field, because a nullable field would change the
canonical bytes, and therefore the recorded `snapshot_digest`, of every GTM
observation already retained. A golden-digest test pins the GA4, OpenAI, and
existing GTM snapshot digests.

`MEASUREMENT_OBSERVATION` already binds `provider` and `account_ref` to the
snapshot, and the ledger validates its payload through the snapshot union, so a
complete inventory that fits one record needs no new record kind, stream, or
provenance rule. One that does not fit needs the two record kinds described
under [Retention beyond one record](#retention-beyond-one-record).

Outcomes and exit codes:

| Situation | Code | Exit |
| --- | --- | --- |
| No GTM profile configured | `NOT_CONFIGURED_OPTIONAL` | 3 |
| Google rejected the credential (401) | `GTM_AUTHORIZATION_REJECTED` | 4 |
| GTM refused the profile or its quota (403) | `GTM_ACCESS_NOT_PERMITTED` | 4 |
| Throttling, 5xx, oversize response | `PROVIDER_REQUEST_FAILED` / `PROVIDER_UNAVAILABLE` / `RESPONSE_TOO_LARGE` | 3 |
| Read succeeded and was retained | `CONFIGURATION_RETAINED` | 0 |
| Comparison over a complete inventory | `NOT_COMPARABLE_FULL_INVENTORY` | 2 |

A failed read retains nothing: the observation is appended only after the whole
inventory closes. (A failure partway through retaining a chunked observation is
described under [Retention beyond one record](#retention-beyond-one-record).)
`measurement compare` refuses the complete inventory explicitly because coverage
statuses, bounded resource records, and raw reference edges have no defined
difference semantics yet; an empty or partial comparison would be a false change
claim.

`adhelm discover` continues to report the `gtm` provider as
`FIXTURE_VERIFIED_READ_SUBSET` with `live_verified: false`. This change adds no
live evidence and does not alter that row.

## Retention beyond one record

A ledger record is bounded at 1 MiB (`MAX_RECORD_BYTES`), and an inventory
canonicalizes to roughly 890 bytes per resource, so a container crosses one
record at about 1,150 resources while the reader admits 10,000 resources and
50,000 edges. `M01.1.retention` removes that ceiling without raising the record
bound and without truncating anything:

- When the observation's canonical payload fits one record
  (`MAX_PAYLOAD_BYTES`, the record bound less a 4 KiB envelope reserve), it is
  retained exactly as before: one `MEASUREMENT_OBSERVATION`, byte-identical to
  every earlier retention.
- Otherwise `retain_measurement` appends ordered `MEASUREMENT_OBSERVATION_CHUNK`
  records, each holding a deterministic, size-bounded slice of the coverage,
  resource and edge collections, then one `CHUNKED_MEASUREMENT_OBSERVATION`
  commit. The commit carries the read interval, snapshot digest, identity
  discovery and every inventory header fact (`GtmFullInventoryHeader`), and
  names each chunk by its ledger record digest. "Chunk" is deliberate: a ledger
  file segment is the `SEGMENT_STARTED` rotation unit.

The observation exists exactly when its commit does. The snapshot digest is
computed over the whole snapshot, so it is identical whether the observation
was retained as one record or as chunks; the receipt's `record_digest` is the
commit's. `measurement catalog`, `show`, `gtm-graph` and `compare` select the
commit's digest; chunk records are never listed or selectable.

Reassembly rebuilds the exact `MeasurementObservation` and revalidates it. The
ledger does this on every verified read as well as on append, so
`history verify` rejects with a typed error (`MEASUREMENT_CHUNKS_INVALID`, or
the closed-schema error for a chunk named twice in one commit) a commit that
names a chunk that is absent, out of order, claimed by another commit, recorded
under another provider, account or observation time, or built from another
snapshot, and a run whose records are missing or altered, even if the hash chain
was re-linked. A chunk's envelope `observed_at` must equal its commit's, so a
commit cannot adopt the chunks an earlier, interrupted read left behind: the
ledger's observation interval and the chunk records never contradict.
Each chunk is validated on its own as well: same privacy screening, closed
schema, unknown fields rejected, and provenance pinned to source `UNKNOWN` with
the provider and account bound (`INVALID_MEASUREMENT_PROVENANCE` otherwise).

Failure behavior is honest rather than atomic. Appends are durable one at a
time, so a read interrupted after some chunks but before the commit leaves
unreferenced chunks. They are inert: not an observation, not listed, not
loadable (`MEASUREMENT_NOT_RETAINED`), and `history verify` still passes. The
command reports `MEASUREMENT_RETENTION_UNVERIFIED` and a repeat read retains a
complete new observation. History is append-only, so the orphans stay.

An inventory that cannot be chunked still fails closed with
`LEDGER_RECORD_TOO_LARGE`: a single record larger than a chunk, or a run longer
than 1,024 chunks. Other providers' observations keep the unchanged one-record
bound. `measurement compare` still refuses a chunked observation with
`NOT_COMPARABLE_FULL_INVENTORY`.

Cost grows with inventory size, as it does for any ledger record: every verified
read of the `changes` stream validates and reassembles each retained inventory.
Every append to that stream is itself a verified read: each chunk, the commit,
and any other change record. An append resolves only its own commit against the
history that scan has already verified, so earlier inventories are not
reassembled a second time within one append. Retaining a chunked inventory
therefore pays the reassembly of every earlier retained inventory at least once
per chunk plus once for the commit, and once more whenever an append starts a
new ledger segment; that cost keeps growing as inventories accumulate. On
the maximum fixture (9,900 resources and 3,300 edges, about 10 MB in 10
chunks) on a loaded shared Linux machine with no earlier inventory retained,
retention took about 40 s including the fixture read, a verified checkpoint
about 6 s, and `measurement show` or `gtm-graph` about 9 to 13 s. Typical
containers are far smaller.

## Evidence limits

- Fixture-only. `httpx.MockTransport` throughout; no live GTM container, no
  OAuth flow, and no credential is exercised.
- No GTM UI reconciliation and no two-organization proof. Both remain release
  gates for the GTM row in [`capability-matrix.md`](../capability-matrix.md).
- The sequential read is not atomic. The existing reader rereads the latest
  header and the live version; the workspace collections are read once each,
  so a workspace edited mid-read is observed at one arbitrary point.
- Matching header counters, complete coverage, and resolved edges together do
  not establish a dependency graph, deletion safety, compiler status, preview,
  browser firing, consent behavior, GA4 receipt, or publish readiness.
- The `cvt_<container>_<template>` custom-template instance type format is
  observed rather than documented in the v2 reference. It is marked
  `UNVERIFIED`; an unparsable type stays `UNRESOLVED_MALFORMED`.
- The script-bearing parameter heuristic detects declared markers only. It is
  not a claim to detect every embedded script.

## Validation

```text
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest -q -n 8 --dist=loadfile tests
uv run python scripts/check_repository.py
```

## Unresolved name-form selectors keep only a digest

`selector_form = NAME` is the only free-text channel a reference edge has; the
`NATIVE_ID`, `PUBLIC_ID`, and `TEMPLATE_TYPE` forms are pattern-bound and cannot
carry arbitrary text. A name-form selector is therefore retained verbatim only
when it resolved to a resource this read actually saw. Every other case — no
matching resource, an ambiguous name, a malformed value — is recorded with
`selector_status = WITHHELD_UNRESOLVED_REFERENCE` and its keyed digest only.

This is uniform across `VARIABLE_REFERENCE`, `PARAMETER_TAG_REFERENCE`
(`Parameter.type == "tagReference"`), `SETUP_TAG`, and `TEARDOWN_TAG`
(`SetupTag.tagName` / `TeardownTag.tagName`). The independent review found the
rule was gated on `VARIABLE_REFERENCE` alone, so the other three name-form
kinds kept up to 256 bytes of raw provider text — including text the same module
had already classified `CUSTOM_HTML` — and carried it into the retained ledger
record, `measurement show`, and the export bundle. Withholding is now decided by
selector form and resolution, not by edge kind.

Independently, and even when it resolves, a selector whose text the
classification rules would call custom code is withheld: a resource name that is
itself a `<script>` or a `javascript:` URI is still custom code and is not
persisted as text (AGENTS rule 13).

# Local history ledger and exports

Last updated: 2026-09-04

## Decision

Canonical operational history is schema-versioned, append-only JSONL. CSV is a
deterministic projection for analysis and spreadsheets. JSON snapshots may be
exported for convenience, but they are never the only durable history.

JSONL is append-friendly, streamable, diffable, and recoverable after a partial
write. It is not inherently schema-stable: stability comes from explicit schema
versions, canonical serialization, immutable past records, tested upcasters, and
deterministic projections.

The ledger is local to the customer's configured profile. It is not telemetry,
is not sent to a product service, and does not contain raw provider responses or
secrets.

SQLite is the default local operational read index for resource lookup,
historical joins, observation revisions, and creative deployments. Its tables
are rebuildable projections of verified JSONL, not a second canonical write
path. DuckDB is an optional local analytics engine over verified snapshots or
exports; it is not required for normal operation or mutation execution. These
are implementation requirements, not claims that either engine is implemented.

## Canonical streams

Use three related streams rather than mixing high-volume observations with rare
mutations:

```text
<profile-data>/ledger/changes/<segment>.jsonl
<profile-data>/ledger/performance/<segment>.jsonl
<profile-data>/ledger/creative/<segment>.jsonl
```

- `changes` records plans, approvals, attempted/applied/verified/recovered
  mutations, normalized state snapshots, and out-of-band drift.
- `performance` records immutable revisions of normalized aggregate report rows.
  Provider conversion backfill creates a new observation; it never rewrites a
  prior row.
- `creative` records creative families, asset and creative revisions, extracted
  features, and effective-dated deployments.

Files rotate by a deterministic size/time policy. Each new segment begins with
a `SEGMENT_STARTED` record referencing the previous closed segment's head digest.
Rotation never rewrites a closed segment.

## Common record envelope

Every physical JSONL line is one compact UTF-8 JSON object with escaped internal
newlines and this envelope:

```text
schema_uri
schema_version
canonicalization_version
record_kind
record_id
stream_id
stream_sequence
occurred_at
observed_at
recorded_at
profile_ref
provider
account_ref
run_id
source
payload
previous_record_digest
record_digest
```

Rules:

- `record_id` is globally unique and sortable; `stream_sequence` is strictly
  monotonic within the locked stream.
- `canonicalization_version` selects a published deterministic UTF-8 JSON
  profile. V1 uses RFC 8785 JSON Canonicalization Scheme semantics, rejects
  non-finite numbers, and represents money as integers and precision-sensitive
  derived metrics in schema-defined integer or decimal-string form.
- All timestamps are ISO 8601 instants in UTC. Provider/account timezones remain
  explicit inside report/state payloads.
- `occurred_at` may be null for an external change whose exact time is unknown.
  `observed_at` and the prior observation bound the interval; never invent an
  actor or timestamp.
- `account_ref` and resource references are stable local identifiers. Exports
  use keyed opaque references unless the user explicitly requests local IDs.
- `record_digest` is lowercase hexadecimal SHA-256 over the canonical envelope
  with `record_digest` omitted. The previous digest creates a tamper-evident
  chain; it does not claim WORM storage or defend against an administrator
  replacing the entire local history.
- One record has a bounded maximum size. Media, raw API bodies, and other binary
  artifacts live outside JSONL and are referenced only by content digest and an
  approved local path.
- A normalized observation that cannot fit one record may be retained as ordered
  chunk records followed by one commit record that names each chunk by record
  digest. The observation exists only when its commit does; chunks are validated
  individually, reassembled and revalidated against the observation's content
  digest on every verified read, and a chunk can be named by at most one commit.
  Chunks left by an interrupted append are inert and never an observation. This
  is a closed per-kind mechanism, not a general batch append. Today only the
  complete GTM inventory uses it
  ([GTM inventory](implementation/gtm-inventory.md#retention-beyond-one-record)).

## Resource snapshots

A `RESOURCE_SNAPSHOT` contains allowlisted normalized configuration state, not a
raw response:

```text
snapshot_id
provider_api_version
connector_version
resource_type
resource_ref
parent_resource_refs[]
remote_fingerprint
normalized_state
normalized_state_digest
effective_status
source_updated_at
data_quality_statuses[]
```

Supported `resource_type` values include `ACCOUNT`, `CAMPAIGN`, `AD_GROUP`,
`AD`, `CREATIVE`, `CREATIVE_BINDING`, `ASSET`, `KEYWORD`, `TARGET`, `PLACEMENT`,
`CONVERSION_ACTION`, `GA4_RESOURCE`, and `GTM_RESOURCE`, with namespaced provider
extensions. Unknown fields are rejected before ledger append.

Inventory/reconcile compares the new allowlisted snapshot with the most recent
known snapshot. Unchanged state may update a compact high-watermark without
emitting a false change. Changed state emits a `REMOTE_CHANGE_OBSERVED` event.

## Change events

A `CHANGE_EVENT` records the history of account, campaign, ad group, ad,
creative, measurement, and GTM changes uniformly:

```text
event_type
operation
origin
actor_ref
resource_type
resource_ref
parent_resource_refs[]
recommendation_ref
plan_ref
plan_digest
subplan_digest
approval_ref
idempotency_ref
before_snapshot_ref
before_digest
after_snapshot_ref
after_digest
field_deltas[]
effect_status
spend_impact_minor
currency
provider_api_version
connector_version
evidence_refs[]
superseded_record_ref
replacement_record_ref
supersession_reason
```

The three supersession fields are null for ordinary events and required for
`RECORD_SUPERSEDED`; they identify the prior record, the appended replacement,
and a reviewable reason. A correction never embeds or mutates the old line.

Required `event_type` values:

- `PLAN_CREATED`
- `APPROVAL_GRANTED`
- `APPLY_STARTED`
- `APPLY_RESULT`
- `VERIFICATION_RESULT`
- `REMOTE_CHANGE_OBSERVED`
- `RECONCILIATION_RESULT`
- `RECOVERY_STARTED`
- `RECOVERY_RESULT`
- `RECORD_SUPERSEDED`

Required `operation` values include `CREATE`, `UPDATE`, `ASSOCIATE`, `REPLACE`,
`ACTIVATE`, `PAUSE`, `ENABLE`, `ARCHIVE`, `DELETE`, `PUBLISH`, `RESTORE`, and
`OBSERVE`.

`origin` is `LOCAL_CLI`, `PROVIDER_UI`, `PROVIDER_AUTOMATION`, `IMPORT`, or
`UNKNOWN`. `actor_ref` is present only when proven. An external snapshot diff
normally has an observation interval and `UNKNOWN` actor; it does not pretend to
be a provider audit log.

Each `field_delta` has a typed field path plus before/after state:

```text
path
before_status
before_value
after_status
after_value
value_classification
```

Values are normalized and allowlisted. A value can be represented by digest or
`REDACTED` when the exact value is unsafe. Creative marketing copy is allowed in
the local creative stream after sensitive-data validation; it is not copied
into a broadly shared evidence bundle by default.

`effect_status` is one of:

- `NO_REMOTE_WRITE`
- `APPLIED_VERIFIED`
- `FAILED_NO_EFFECT`
- `PARTIAL_EFFECT`
- `AMBIGUOUS_REMOTE_STATE`
- `EXTERNAL_CHANGE_DETECTED`

## Mutation ordering and reconciliation

Per-stream append locks protect record integrity; they do not make authorization
consumption atomic or serialize conflicting account operations. Every local
writer must also acquire an OS-supported profile/account coordinator lock for
the affected scope, resolving profile aliases to stable remote account identity.
Acquire shared delegation-policy allowance locks as well: disjoint account
operations can still consume one cumulative budget. Multi-account/policy
operations acquire coordinator locks in a stable order or fail before any
write. Local enforcement belongs to one coordinating host/ledger; independent
copies do not share counters. Under that coordination, verify the
current authorization and its remaining scope, reject prior consumption, and
append the durable operation claim with `APPLY_STARTED` as one critical section.
The claim identifies the authorization, plan/subplan, operation, and invocation,
so consuming a bounded authorization cannot be mistaken for permission to repeat
the operation. The exact authorization policy is defined in
[`lifecycle-contract.md`](lifecycle-contract.md).

Before network egress, append, flush, and fsync `APPLY_STARTED` with the exact
approved plan/subplan and intended operation. Append `APPLY_RESULT` after the
transport outcome is classified, any response is schema-validated, and a post-
attempt remote re-read is attempted. If the re-read cannot prove state, record
`AMBIGUOUS_REMOTE_STATE`; do not infer failure or success. Append a separate
`VERIFICATION_RESULT` for the final proof.

Hold the coordinator through the remote attempt and durable outcome recording.
A crash releases the OS lock but does not release a recorded claim. Another
process must inspect canonical unresolved starts before it can proceed; neither
a stale index nor rebuilding SQLite can restore consumed authorization. If
claim durability cannot be established, fail closed before egress. Local locks
do not prevent provider UI changes: retain the final remote re-read, use
provider-supported conditional writes where available, and report the remaining
race where a provider cannot enforce the checked revision.

An `APPLY_STARTED` without a terminal event is unresolved. `doctor` and
`history verify` flag it and block another write without changing local or
remote state. `history reconcile` performs the separately authorized provider
read, appends a truthful reconciliation result, and never retries the mutation.

Every successful inspect/reconcile can detect changes made in a provider UI or
automation. Snapshot-and-diff is the universal fallback. Where a provider has a
native change-history surface, use it only as additional evidence and preserve
its coverage/retention limits.

Corrections never edit a prior line. Append `RECORD_SUPERSEDED` with the bad
record, corrected record, reason, and actor.

## Performance observations

`PERFORMANCE_OBSERVATION` wraps the exact `ReportEnvelope`, `PerformanceRow`, or
`ConversionBreakdownRow` from [`reporting-schema.md`](reporting-schema.md), plus:

```text
observation_key
observation_revision
supersedes_record_id
query_as_of
source_report_digest
```

Providers can backfill or restate conversions. The natural observation key
covers provider/account/resource/dimensions/window/query contract; a later value
increments the revision and references the prior record. Reports select the
latest revision as of an explicit ledger high-watermark. Historical analyses can
reproduce what was known at an earlier time.

## Append and recovery guarantees

- Use an OS-supported exclusive file lock per stream.
- Canonically serialize before hashing and append exactly one LF-terminated
  record. Segment creation also fsyncs the containing directory before the
  segment can receive a committed mutation event.
- Flush and fsync before reporting success for a write-history event.
- On startup, verify sequence, duplicate IDs, schema, digest chain, and final
  newline. A torn final line is quarantined and repaired only through an explicit
  recorded recovery; corruption in the middle fails closed.
- Concurrent writers serialize or fail with a bounded retryable lock error; they
  never interleave bytes.
- A schema change adds an upcaster and a new schema version. Old lines remain
  untouched. A breaking rewrite produces a new stream linked to the old head
  digest and preserves the old stream until explicit customer deletion.
- Compaction creates a verified checkpoint/new segment; it never silently drops
  source history.

## Local read indexes and analytics

The default SQLite index records its schema version and the exact verified
sequence/head digest for each source stream. Update index rows and those
watermarks in one SQLite transaction, after the corresponding JSONL records are
durable. A crash between ledger append and index update leaves an index that can
catch up; it never loses the canonical event. A failed or incompatible index is
discardable and rebuildable from retained verified history.

Reports use a checkpoint manifest containing the selected high-watermark of all
three streams. Validate cross-stream references before exposing the checkpoint;
do not assume that three independently sampled stream heads describe a
consistent report. Index queries bind to that checkpoint and return its
provenance. If an index is behind, catch it up or return an explicit diagnostic;
never silently answer from stale state. Mutation coordination and authorization
checks remain authoritative against the ledger regardless of index availability.

DuckDB may query a customer-selected verified snapshot/export bundle for larger
local analyses. Record the source manifest and checkpoint with each derived
result. It must not write operational history, consume authorization, query
provider APIs, or introduce a required service. Enabling it does not authorize
installation of customer-managed external tools. The same redaction and sharing
boundaries apply to SQLite tables, DuckDB files, and JSONL.

## CLI contract

At minimum:

```text
history status
history verify
history tail [--resource ...] [--since ...]
history reconcile [--provider ...]
history export --format jsonl|csv --output <dir> [--as-of ...]
report ... --format table|json|jsonl|csv
creative catalog|analyze|compare|report
```

`history verify` is read-only. `history reconcile` reads providers and appends
new observations/events; it never mutates remote state. Export never mutates the
canonical streams.

## Deterministic CSV projection bundle

CSV is generated from a verified JSONL high-watermark. It is never maintained by
a separate write path.

```text
manifest.json
events.csv
change-fields.csv
resource-snapshots.csv
performance.csv
conversions.csv
creative-versions.csv
creative-assets.csv
creative-renditions.csv
creative-components.csv
creative-colors.csv
creative-fingerprints.csv
creative-experiments.csv
creative-deployments.csv
creative-performance.csv
```

This is the minimum named bundle, not permission to discard other normalized
fields. The versioned projection schema generates deterministic child tables
for every exported nested object or array, including resource parents/state,
event references, quality statuses, provider extensions, palette entries, and
experiment dimensions. The manifest enumerates every generated table and its
parent/foreign-key contract so the allowlisted normalized export model can be
reconstructed without parsing ad hoc JSON cells.

`manifest.json` records schema/projection versions, filters, a deterministic
`projection_as_of` derived from the selected ledger checkpoint, source stream
IDs and head digests, high-watermark sequences, row counts, file digests,
currency/timezone semantics, and tool/connector versions. It does not record the
wall-clock export invocation time unless that time is supplied as an explicit
projection input; repeated exports of the same checkpoint remain byte-identical.

CSV rules:

- UTF-8, RFC 4180 quoting, CRLF row endings, fixed headers, and deterministic
  row ordering on every supported operating system;
- one scalar per cell; nested values become child tables, never ad hoc comma-
  joined strings;
- ISO 8601 UTC timestamps, integer minor-unit money, explicit null/unavailable
  status, and canonical booleans/enums;
- untrusted text cells whose first character is apostrophe, `=`, `+`, `-`, `@`,
  tab, or carriage return receive one leading ASCII apostrophe in CSV only.
  Including apostrophe in the source escape set makes the transform reversible:
  a schema-aware decoder removes exactly one escape apostrophe from every such
  encoded text cell. Typed numeric fields are serialized by schema and are not
  text-escaped. The manifest records the rule and marked columns; JSONL retains
  the validated canonical value;
- provider text and identifiers pass redaction/sensitive-data policy before
  export; and
- identical verified inputs, projection version, filters, and platform-independent
  newline setting produce byte-identical output.

## Projection version 3

0.1.1 adds `conversion_count_decimal`, `conversion_kind`, `campaign_ref`, and
`date` columns to `conversions.csv`. Consumers must select one conversion kind
before summing Google Ads totals; primary and all conversions overlap. The
exact decimal column is authoritative when present, while legacy records leave
it empty. Source rows remain reconstructible through the nested child tables.
Campaign labels are pseudonymized by default. No historical JSONL is migrated
or modified to generate these columns.

## Retention and deletion

The customer controls the local data directory and a versioned retention
configuration. Preserve canonical history and referenced assets by default;
rotation and index rebuilding do not authorize history deletion. Report disk
use and observed growth by stream, assets, exports, and cache. Recommend settings
with their estimated space savings, retained analysis window, reproducibility
loss, and recovery impact, and let the customer preview and save a policy.
Recommendations never become active merely because an agent generated them.

Configure `changes`, `performance`, and `creative` independently, and configure
asset files, generated exports/artifacts, and rebuildable caches separately.
Scope each rule to explicit profiles and local paths. A retention age is only an
eligibility threshold; it is not proof that a record or resource may be deleted.
Default to manual preview. Automatic local cleanup is allowed only under an
explicitly saved policy that names the scope, categories, thresholds, and
schedule; it never authorizes a provider mutation, remote upload, or cleanup
outside those paths.

Illustrative proposed configuration schema, not an implemented command or a
recommended retention period:

```yaml
local_storage:
  schema_version: 1
  read_index: sqlite
  analytics_engine: disabled # optional: duckdb over verified exports
  retention:
    schema_version: 1
    profiles: [example-profile]
    root: "<profile-data>"
    execution: manual_preview # automatic requires an explicitly saved policy
    schedule: null
    streams:
      changes: {keep_days: null}
      performance: {keep_days: null}
      creative: {keep_days: null}
    assets: {keep_days: null}
    exports: {keep_days: null}
    caches: {keep_days: null}
```

`null` preserves data without an age limit. Invalid versions, ambiguous paths,
unknown categories, and unsupported schedules fail closed. Policy activation
must preview candidate data and consequences; each later automatic run records
the saved policy digest and exact candidate manifest and reevaluates dependency
protection before deletion. Changes that expand automatic deletion require a
new saved policy. The CLI does not create a background service merely because a
retention section exists; execution uses the explicitly configured local
schedule.

Cleanup must prove candidates `PROVEN_UNREFERENCED` under the dependency engine.
For local retention, distinguish a dependency requiring the original data from
an integrity/provenance link to a previous head digest. A surviving verified
boundary checkpoint and receipt can satisfy the latter without retaining every
old segment. They cannot satisfy a dependency that needs original records for
recovery, replay prevention, an active deployment, or a promised reproducible
report. The dependency engine records that classification for each candidate.
Protect unresolved `APPLY_STARTED` records, authorization consumption needed to
prevent replay, active plan/recovery dependencies, verification evidence still
required by a supported workflow, active creative deployment dependencies, and
checkpoint manifests or retained exports that depend on the source. Unknown
dependencies block deletion. Cache deletion is permitted only when the required
source remains available and the data is actually rebuildable; a cache cannot
silently become the only copy of retained history.

For canonical streams, delete only eligible closed segments as an explicitly
declared retention operation; never rewrite past records to hide the removal.
Preserve a verified boundary checkpoint, segment head digests, and a surviving
receipt that records the removed sequence ranges and unavailable history.
Reports and index rebuilds must expose those gaps and reject an unavailable
historical checkpoint instead of presenting partial history as complete. A
checkpoint proves the retained boundary, not recoverability of deleted records.
`history verify` validates surviving segments and authorized retention receipts
and reports `VERIFIED_WITH_RETENTION_GAPS` for those declared boundaries. It
still fails closed on unexplained missing segments or middle corruption. It
must never report a pruned historical range as present or fully reproducible.

Manual deletion names exact segments/assets and previews loss and chain impact
before confirmation. Both manual and policy-driven cleanup append a durable
local intent before deletion, then record per-target outcomes, skipped protected
items, failures, and reclaimed space. A crash or ambiguous filesystem result
requires local reconciliation. Keep deletion receipts outside the candidate
set; if the intent or receipt cannot be safely persisted, do not delete. No
cleanup mode uploads data. Do not market the ledger as immutable; call it
append-only and tamper-evident with customer-controlled retention.

## Storage design references

- [SQLite as an application file format](https://www.sqlite.org/appfileformat.html)
- [DuckDB concurrency model](https://duckdb.org/docs/current/connect/concurrency)

These inform engine selection; neither database replaces the canonical history
or supplies application-level authorization and recovery guarantees by itself.

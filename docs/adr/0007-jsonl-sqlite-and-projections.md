# ADR 0007: JSONL ledger, SQLite index, and projections

- Status: Accepted
- Date: 2026-09-04

## Decision

Schema-versioned append-only JSONL is canonical. Separate `changes`,
`performance`, and `creative` streams use monotonic sequence numbers, previous
digests, deterministic rotation, locking, flush/fsync, and explicit recovery.
Every remote mutation durably appends `APPLY_STARTED` before network egress.

SQLite is the default disposable local read index. It records verified stream
high-watermarks and is always rebuildable from retained JSONL. Optional DuckDB
reads verified snapshots or exports and never writes operational history.

CSV and JSON are deterministic projections from a validated multi-stream
checkpoint. They have fixed schemas, ordering, newline/encoding rules,
formula-injection protection, row counts, and file digests. There is no dual
write path.

Retention preserves history by default. Deletion requires an explicit policy,
dependency proof, durable intent and receipt, protected replay/recovery state,
and truthful retained-coverage reporting.

## Consequences

- Index loss or staleness cannot erase authorization consumption.
- Crashes and ambiguous transport results force read-only reconciliation before
  another write.
- The detailed record and export contract remains
  [`local-ledger-and-exports.md`](../local-ledger-and-exports.md).

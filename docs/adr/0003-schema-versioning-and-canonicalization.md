# ADR 0003: Schema versioning and canonicalization

- Status: Accepted
- Date: 2026-09-04

## Decision

All persisted and agent-facing records use closed, versioned Pydantic schemas.
Unknown fields fail validation at trust boundaries. Provider extensions are
typed, namespaced, allowlisted, and independently versioned.

Digests use SHA-256 over UTF-8 RFC 8785 JSON Canonicalization Scheme semantics.
Schemas reject non-finite numbers. Money is integer minor units plus ISO 4217
currency; precision-sensitive derived values use schema-defined integers or
decimal strings. Instants are UTC ISO 8601 while source timezones and date
boundaries remain explicit.

Breaking changes add a schema version and tested upcaster. Historical JSONL is
never rewritten. Canonical digest domains explicitly exclude self-referential
digest fields and include every field that can change the authorized effect.
The creative digest domain follows
[`creative-intelligence.md`](../creative-intelligence.md).

## Consequences

- Golden vectors must prove identical canonical bytes and digests across
  supported operating systems.
- An upcast result records source and target versions and cannot alter the
  original line.
- Schema or canonicalization versions participate in plans, evidence, ledger
  checkpoints, and capability declarations.

# Retained strategy candidate history

`compile strategy --input <brief> --retain` recompiles the complete closed
strategy and appends exactly one `STRATEGY_CANDIDATE_COMPILED` observation to the
canonical `changes` JSONL stream. It never appends one child at a time. A late
provider compiler or normalized-review failure therefore leaves no partial
strategy history.

The observation contains the strategy brief, input, and internal aggregate
digests; its expiry; exact declared caps and recomputed maximum spends for each
currency; and the original ordered child candidate/specification digests. Its
one-through-six child values use the existing closed normalized
`CandidateCompilationRecord` variants. They preserve each provider's reviewed
native budget units, declared creative references, paused/draft/disabled state,
and every provider-specific unmet planning check. The strategy wrapper remains
`OFFLINE_CANDIDATE_NOT_EXECUTABLE`,
`FIXTURE_ONLY_NOT_LIVE_VALIDATED`, and
`ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET`.

The record deliberately excludes the source strategy brief, customer intent,
assumptions, provenance text, approval owner, native specifications and request
graphs, serving copy, destinations, native media or identity IDs, and every
other replayable payload. A child retains only the same normalized fields
already allowed for single-provider candidate history. Local account and role
references are pseudonymized before agent-visible output; their retention is a
declaration, not proof that a native resource exists.

The aggregate ledger envelope has no provider, account, run, occurrence, or
observation timestamp. `recorded_at` is the only durable local retention time.
It cannot claim a remote event, current account identity, shared semantic
result, authorization, provider eligibility, drift check, execution, recovery,
or verification. A retained observation is historical even before its expiry;
later planning requires a new compilation, all current provider readbacks and
checks, an exact plan, and direct or delegated authorization. Retention rejects
an expiry that passes before the durable append boundary.

The compact retention receipt is sized before the ledger append and requires no
export key, provider credential, or provider call. A local append/durability
failure reports `STRATEGY_RETENTION_UNVERIFIED` without claiming persistence or
retrying automatically. The caller must run history verification and catalog
inspection before another explicit retention attempt.

`strategy catalog` reads one verified cross-stream checkpoint and exposes
newest-first bounded digest, count, cap, status, expiry, and local-record-time
rows without needing the export key. `strategy show <record-digest>` reads one
selected record from a verified checkpoint, emits the complete sealed child
review after HMAC reference redaction, and includes its checkpoint digest and
an explicit as-of expiry status. It never labels an unexpired historical record
currently valid. If the complete required review cannot fit the configured
output bound, show fails explicitly before the sharing key is accessed; it does
not trim a child review or an unmet check. A cursor does not pin a multi-call
catalog snapshot, so callers restart a catalog walk when its checkpoint digest
changes.

Existing `CANDIDATE_COMPILED` records and their provider-specific bytes/digests
remain unchanged. The strategy event is a separate closed schema and does not
turn existing individual observations into a strategy or use independent JSON
persistence.

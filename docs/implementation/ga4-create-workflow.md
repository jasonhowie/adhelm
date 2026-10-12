# Configured GA4 key-event CREATE planning

This development workflow prepares and retains an immutable CREATE plan. It
performs fixed Google Admin GETs through the configured OAuth lifecycle and
local ledger writes. It sends no CREATE, obtains no authorization grant, and
does not establish live provider conformance or runtime measurement evidence.

The closed request selects a retained candidate, supplies its complete fresh
source input, names the GA4 Admin EDIT OAuth profile, and bounds lifetime to
60–900 seconds. Configuration and target validation precede private key or
credential access. Static and CI credentials cannot use this path. Unresolved
execution history blocks planning until reconciliation.
The CLI supplies the exact configuration digest used for its artifact and
output preflight; a changed saved policy fails before key access or provider
reads instead of adopting unchecked preferences.

Selection reads one verified cross-stream checkpoint. The selected candidate
must use the latest retained conversion definition, that definition must be
effective, and fresh recompilation must reproduce the exact retained candidate.
A future-effective superseding definition also blocks an older candidate; this
workflow does not infer a schedule or recover missing source prose from history.

The observation factory owns resolution and one-use consumption of the protected
OAuth snapshot. One captured bearer serves the fixed property, selected stream
and complete bounded key-event inventory reads. Configuration and key changes
at the checked boundaries fail closed. The observation seals its full normalized
content, configuration, profile, principal, client and interval under the current
local fingerprint key. This establishes local content integrity and protected
bundle association, without proving provider origin or Google write permission.
Reads remain sequential and non-atomic; an exact change and reversion between
sampling boundaries cannot be observed.

Retention stores the complete sealed execution observation in the separate EDIT
observation kind. Legacy records omit this optional field and keep their original
wire representation; explicit null or a partial proof is invalid. New CREATE
plans require the complete proof. The retainer authenticates it with the current
key, and ledger history validation binds the plan's execution principal, client,
profile, scope and property to that observation. Exports omit the nested proof
and report that exported historical data has not authenticated its local seal.

After observation retention, selection is repeated at the final checkpoint.
The pure plan compiler binds the workspace, candidate, all source records,
checkpoint, configuration, execution identity, observation interval, exact native
request digest, semantic before/after collection and expiry into the CompositePlan
digest. The full inventory digest separately detects configuration drift; the
predicted semantic collection does not invent a Google resource ID. The declared
destination label and execution profile remain separate facts because no typed
mapping between those labels has been evidenced.

The plan retainer repeats fresh compilation and keyed semantic reconstruction.
At append, the ledger acquires all stream locks in checkpoint order and compares
the complete current checkpoint before segment creation or writing. Those locks
remain held through fsync, excluding concurrent changes, creative or performance
appends. Historical predecessor checks require the selected candidate, latest
definition and latest EDIT observation. The final callback performs only bounded
configuration and clock checks; it must never read or acquire the ledger. No
provider HTTP occurs under ledger or configuration locks.

The retained plan is explicitly historical and non-authorizing. Full review
allowlists meaningful facts and pseudonymizes references; raw native requests,
source prose, OAuth identity internals and content markers do not enter agent
output. CREATE has zero advertising spend, immediate live GA4 configuration,
no draft or backfill, no documented conditional write or idempotency guarantee,
and reconcile/export/stop recovery without remote compensation. A failed local
append receipt reports `GA4_CREATE_RETENTION_UNVERIFIED`; inspect catalog/history
before retrying because fsync may already have succeeded.

Tests use synthetic OAuth stores, mocked fixed HTTP responses and temporary
ledgers. Independent ultra regressions exercise forged identity, provenance
digest changes, stale observations, concurrent stream appends and lock ordering.
They do not exercise a real credential store, account, browser, CREATE request,
native approval host, or released executable.

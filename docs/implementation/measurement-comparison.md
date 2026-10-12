# Retained measurement comparison

`compare_measurements(ledger, before_record_digest, after_record_digest)` is a
local, read-only comparison of exactly two retained `MEASUREMENT_OBSERVATION`
records. It validates both lowercase digest selectors before reading history,
takes one verified ledger checkpoint, and selects both records only from that
checkpoint. It does not read a provider, access a credential or sharing key,
append history, create an artifact, or grant authority.

The two observations must use the same local `profile_ref`, provider, account,
capability version, and stable provider identity: GA4 property and web stream,
OpenAI Ads account, or GTM account and container. The before read must finish
no later than the after read starts. The comparison interval is deliberately
the outer interval from `before.read_started_at` through
`after.read_finished_at`; it does not claim to identify an exact remote object
read time. Its actor is `UNKNOWN` and exact change time is null. Consistency is
explicitly `SEQUENTIAL_NONATOMIC`; runtime firing, processed receipt, native
association, and change attribution remain explicitly unevaluated.

Before projecting facts, the library rechecks the selected record envelope,
record digest, observation/snapshot digest, identity binding, and the raw JSON
types of version and control booleans. This prevents an otherwise rehashed
`true`/`1` or `1.0` literal from being normalized through a Pydantic model. A
source that fails this closed validation produces no comparison. GA4 default
numeric values must also retain their normalized JSON-string representation;
the comparator rejects a numeric JSON substitute before Decimal parsing.

Only the three normalized retained schemas are projected. GA4 exposes safe
key-event and event-rule metadata. OpenAI exposes account and standard
conversion-setting metadata. GTM exposes feature availability, version and
workspace metadata, header-count status, and HMAC-backed opaque-code marker
counts. It never compares arbitrary JSON, provider response bodies, rule text,
event names, custom HTML/template source, or user-entered text. Fact output
contains only allowlisted resource type/reference, status, changed field names,
opaque observed counts where applicable, and before/after fact digests.

Each fact is `CHANGED`, `ADDED`, or `OBSERVED_ABSENT_FROM_LIST`. The last form
means only that a resource was absent from the later complete retained list; it
does not mean deletion. A comparison contains at most 1,000 facts and each
changed fact names at most 32 allowlisted fields. Exceeding the fact bound fails
closed with `MEASUREMENT_COMPARISON_TOO_LARGE`; review the two original records
with `measurement show` because no complete comparison was produced.

GTM's `live` and `latest` observations compare as stable contextual slots, so a
version ID transition is a `CHANGED` slot with `version_ref` among its changed
fields. It is not represented as a misleading added/deleted version pair.

Every result includes its checkpoint, source record/snapshot/observation
digests, source provider/account/capability binding, source completeness records,
and canonical `comparison_digest`. It
also always exposes `implementation_evidence=FIXTURE_VERIFIED_LIVE_UNVERIFIED`
even when there are no facts. OpenAI keeps
`FIXTURE_CONTRACT_LIVE_UNVERIFIED`; GTM keeps its version/workspace metadata
coverage, `NOT_PROVEN` dependency status, sequential non-atomic inventory,
header status, and `NOT_EVALUATED` staging status. Those caveats remain present
when the normalized surfaces are equal.

New GA4 reads retain a [domain-separated key marker](ga4-fingerprint-continuity.md).
Two matching markers produce `MATCHING_RECORDED_KEY_MARKERS`; two different
markers fail with `MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH` before any
facts are generated. Key rotation cannot masquerade as configuration drift.
If either GA4 record lacks a marker, continuity remains `NOT_EVIDENCED`; the
CLI never reads today's key to guess historical continuity. GTM remains
`NOT_EVIDENCED`. OpenAI has no such fingerprint-key comparison, so its value
is `NOT_APPLICABLE`. Matching recorded markers do not authenticate the source,
prove current access, or satisfy a current duplicate check. All facts have
`fact_basis=NORMALIZED_OBSERVATION_DIFFERENCE`: in particular, a fingerprint
difference with continuity not evidenced does not establish that the remote
configuration changed.

This is fixture-verified local history comparison, not provider audit history,
live conformance, reconciliation proof, runtime receipt, deletion evidence,
or a remote mutation capability.

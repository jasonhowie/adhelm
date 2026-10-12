# Local report audit contract

`adhelm.audit` evaluates one already-normalized `NormalizedReport` against one
customer-declared `AuditPolicy`. It receives the immutable digest of the
verified `PERFORMANCE_OBSERVATION` record that supplied the report, plus an
injected timezone-aware `as_of` value. It makes no network request, reads no
credential, writes no ledger record, and never creates a plan.

## Input and output

The programmatic entry point is:

```python
audit_report(report, source_record_digest=record_digest, policy=policy, as_of=as_of)
```

The strict file-input entry point is `audit_from_json(raw)`, which accepts only
the `AuditInput` object: `report`, `source_record_digest`, `policy`, and
`as_of`. Unknown fields, raw URL/query values, invalid report models, malformed
JSON (including duplicate keys), inputs over 1 MiB, and naive timestamps fail
with `INVALID_AUDIT_INPUT`. This low-level function validates the supplied
object; it does not independently establish that its digest exists in history.

The CLI evidence boundary is stricter: `adhelm audit RECORD_DIGEST --input POLICY`
loads the selected immutable report from a verified local ledger checkpoint.
Missing or invalid record digests fail before audit evaluation. `report catalog`
lists the latest revisions; `report show RECORD_DIGEST` can review a retained
older revision explicitly. Both the source record and selected checkpoint remain
in the audit result. `--at` fixes an aware evaluation time for reproducible
pagination; future evaluation times are rejected. Saved row/byte preferences
apply, with an explicit `--limit` override and complete row counts.

`AuditResult` carries the input record digest, the report envelope's source
ledger checkpoints, a digest of the declared policy, its injected `as_of`,
coverage, findings, and recommendations. This typed result is available even
when no output artifact is configured.

`AuditPolicy` contains only customer-declared values:

- `maximum_observation_age_seconds`
- optional `maximum_cpc` as `Money`
- optional `minimum_impressions`
- `require_conversion_evidence`

There are no universal CPC, volume, freshness, performance, or causal
thresholds. CPC comparison stays within one normalized row and compares integer
minor units without a currency conversion. Google Ads comparisons use retained
exact native micros before the declared half-even display rounding. Rows are never summed across
currency, metric grain, report window, or attribution context.

## Evidence boundaries

Each finding and recommendation cites only the supplied immutable source record
digest. Row digests, raw provider responses, names, URLs, query values,
credentials, and creative text are not emitted. Finding and recommendation IDs
are RFC 8785 SHA-256 content identifiers over their fixed output semantics and
input identity.

Findings use the reporting-schema fields: `finding_id`, `finding_type`,
`severity`, `provider`, `resource_refs`, `observation`, `evidence_refs`,
`data_window`, `confidence`, `risk`, `suggested_action`, and
`automatic_write_allowed=false`. Recommendations use the matching prescribed
fields, always set `proposed_plan_ref=null`, and have only a next-read or
verification precondition. They do not authorize a remote mutation.

The implementation labels all required dimensions as `EVIDENCE_MISSING`,
`PARTIAL`, or `CHECKED`. A report can directly check declared delivery, spend,
bid, or placement fields. It cannot establish structure, targeting, creative
health, destination configuration, tracking, native conversion configuration,
browser measurement, or policy health. Aggregate conversion rows provide only
partial native-conversion coverage, and only provider-sourced rows count: a GA4
key event or a CRM outcome is never a provider-native conversion. Clicks,
sessions, key events, and traffic never imply a native conversion count.
Satisfying a native-reporting evidence policy requires a complete provider
conversion row with an explicit action, event contract, mapping, counting method,
proven join, and declared attribution. It still cannot prove browser firing or
processed receipt.

Unknown report freshness remains unavailable. A stale observation finding comes
only from the declared policy age and injected time. Data-quality caveats remain
visible as a partial report finding; they are never turned into healthy zeroes.

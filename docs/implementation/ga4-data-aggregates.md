# GA4 Data API fixed aggregate reporting (M00)

Implements the `M00` row of the internal finishing plan
and the GA4 aggregate rows of
[definition-of-done.md](../definition-of-done.md) section 2: pagination,
timezone, currency, attribution, freshness, thresholding, sampling, date
boundaries, and "missing, stale, duplicate, thresholded, sampled, and
processing-pending states never masquerade as zero".

Wire-level citations live in
[ga4-data-aggregate-contract.md](../research/ga4-data-aggregate-contract.md).
Deterministic provider-campaign joins are ticket **M00.2** and are deliberately
not implemented here.

## Scope

| Surface | Status |
| --- | --- |
| `Ga4DataClient.run_aggregate_report` | New. Three fixed reviewed shapes. |
| `Ga4DataClient.run_report` / `run_realtime_report` | Unchanged contract; existing `report ga4` inputs behave exactly as before. |
| `_validate_metadata` | Corrected to the documented `ResponseMetaData` field names. |
| Deterministic joins | Out of scope (M00.2). Rows carry the exact dimension values that work needs. |
| GA4 Admin writes | Out of scope (M02). |

## Fixed request shapes

No dimension or metric name is ever accepted from input. `Ga4AggregateQuery`
carries an `aggregate_shape` discriminator; the adapter selects the request from
a compiled table.

| `aggregate_shape` | Dimensions | Metrics | `dimensionFilter` |
| --- | --- | --- | --- |
| `CAMPAIGN_SOURCE_MEDIUM` | `sessionCampaignName`, `sessionSource`, `sessionMedium` | `sessions`, `totalUsers`, `keyEvents` | none |
| `CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT` | `sessionCampaignName`, `sessionSource`, `sessionMedium`, `eventName` | `keyEvents` | `eventName` in the declared key events |
| `KEY_EVENT_TOTALS` | `eventName` | `keyEvents` | `eventName` in the declared key events |

Every request also sends `keepEmptyRows: false`, an explicit `limit`/`offset`
pair, and a fixed `orderBys` list that sorts ascending `ALPHANUMERIC` on each
requested dimension so paging and the normalized projection are deterministic.

### Why no `keyEvents:<name>` metric

Verified negative result: GA4 documents no `keyEvents:<key event name>` metric.
The only documented per-key-event parameterized metrics are
`sessionKeyEventRate:event_name` and `userKeyEventRate:event_name`, which are
percentages rather than counts. Per-key-event **counts** therefore come from the
`eventName` dimension restricted to the property's configured key events.

`key_event_names` is supplied by the caller from the GA4 Admin key-event
inventory; the Data API cannot enumerate a property's key events. The adapter
refuses a key-event shape with an empty list, and refuses a response containing
an event name that was not declared (`SENSITIVE_DATA_RISK`).

### Semantic caveat on the campaign × key-event shape

`sessionCampaignName` / `sessionSource` / `sessionMedium` are session-scoped;
`eventName` and `keyEvents` are event-scoped. Google documents that "adding
dimensions to a query can alter the aggregated values for metrics in a report".
The campaign × key-event shape is therefore **not** arithmetically comparable to
the campaign-only shape and the two are never summed or differenced.

## Timezone, currency, and date boundaries

The property timezone and `currencyCode` are read by the existing
`discover_identity` Admin read and bound **before** any data query:

- a property with no `currencyCode` is refused with `EXPECTED_CURRENCY_REQUIRED`
  before a single `analyticsdata.googleapis.com` request;
- a query timezone or currency that differs from the verified identity is
  refused with `TIMEZONE_MISMATCH` / `CURRENCY_MISMATCH`;
- a window ending after the current property-local date is refused with
  `INVALID_REPORT_WINDOW`; and
- response `metadata.timeZone` / `metadata.currencyCode` that drift from the
  verified identity fail the read closed.

`window_start` and `window_end` are **property-local calendar dates**, and GA4
treats both `DateRange` ends as inclusive. Because "inclusive end date" is
ambiguous to a downstream consumer, the report also records the exact instants:

```text
window_boundary               = INCLUSIVE_START_INCLUSIVE_END_PROPERTY_LOCAL_DATES
window_start_instant          = local midnight of window_start, in UTC
window_end_exclusive_instant  = local midnight of (window_end + 1 day), in UTC
```

Known limitation: for a property timezone whose local midnight does not exist on
a spring-forward day, Python's `zoneinfo` resolves the nonexistent local time
using the pre-transition offset (PEP 495 `fold=0`). GA4's own boundary handling
for that day is `UNVERIFIED`.

## Pagination and completeness

`limit`/`offset` pages are consumed up to `page_bound` (default 10, maximum 50),
with `page_size * page_bound` capped at the 10,000-row normalized limit.

`completeness` is a first-class result, not an exception:

| Value | Meaning |
| --- | --- |
| `COMPLETE` | Every declared `rowCount` row was consumed. |
| `EMPTY` | The provider declared and returned zero rows, with no thresholding and no reason. |
| `EMPTY_THRESHOLDED` | Zero rows, but GA4 declared the read subject to thresholding, so rows may have been suppressed. |
| `WITHHELD` | Pagination reconciled, but thresholding withheld at least one configured key event. |
| `NOT_AVAILABLE_FROM_PROVIDER` | GA4 returned an `emptyReason`; it declined to report. |
| `INCOMPLETE_PAGE_BOUND_EXCEEDED` | The bound was exhausted before `rowCount` was reached. |
| `INCOMPLETE_ROW_COUNT_MISMATCH` | `rowCount` changed between pages, or consumed rows did not reconcile to it. |

`exhaustive()` requires `COMPLETE` or `EMPTY` **and** that GA4 declared no
thresholding, no `(other)` roll-up, and no `emptyReason`. The roll-up test is
`rolled_up()`, which is derived from the rows' own `*_OTHER_ROW` dimension
statuses as well as the report-level `other_row_aggregation` flag, so a report
rebuilt without that flag still cannot claim exhaustive coverage of a table
containing a roll-up bucket. Reconciled pagination
alone is not proof that the returned rows are every matching row. The model
rejects a report that claims `COMPLETE` or `EMPTY` while carrying a
`THRESHOLD_WITHHELD` measurement. An incomplete report
always reports `consumed_row_count`, `declared_row_count`, `pages_consumed` and
`page_bound`, and adds `PARTIAL_PAGE_FAILURE` to the envelope; it is never
presented as the full table. A page whose `rowCount` disagrees with the first
page is discarded rather than merged, because it belongs to a different provider
result. Rows exceeding the declared total, or a page that makes no progress,
fail closed with `PROVIDER_RESPONSE_INVALID`.

## Quality semantics

Every documented `ResponseMetaData` field is surfaced as typed state.

| Response field | Report field | Envelope quality status |
| --- | --- | --- |
| `subjectToThresholding` | `subject_to_thresholding` | `THRESHOLDED` |
| `samplingMetadatas[]` | `sampling[]` (`samplesReadCount`, `samplingSpaceSize`) plus `sampling_consistency` | `SAMPLED` |
| `dataLossFromOtherRow` | `other_row_aggregation` | `PROVIDER_REPORTED_UNKNOWN` |
| `schemaRestrictionResponse` | `restricted_metrics[]` | `PROVIDER_REPORTED_UNKNOWN` |
| `emptyReason` | `empty_reason` | `NOT_AVAILABLE_FROM_PROVIDER` |
| GA4 `(other)` dimension value | `dimension_statuses`, `other_row_aggregation` | `PROVIDER_REPORTED_UNKNOWN` |
| `timeZone` / `currencyCode` | verified against identity | refusal on drift |

`COMPLETE` appears on the envelope only when none of the above applies and
pagination reconciled.

The adapter sends exactly one `dateRange`, and GA4 documents one
`samplingMetadatas` entry per date range. A page carrying more than one window
does not answer the request that was sent and fails closed with
`PROVIDER_RESPONSE_INVALID`. Pages are never concatenated: the first non-empty
window set is recorded once, and disagreement between pages is recorded as
`sampling_consistency = SAMPLING_METADATA_INCONSISTENT` (with
`PROVIDER_REPORTED_UNKNOWN` on the envelope) instead of being merged or
truncated.

### A withheld cell is never the number zero

Each measurement is a `Ga4MetricCell` with an explicit status. Only `OBSERVED`
and `SAMPLED_ESTIMATE` may carry a number; every other status carries `null`.

| Cell status | Meaning |
| --- | --- |
| `OBSERVED` | GA4 returned this value. A returned `0` stays `0`. |
| `SAMPLED_ESTIMATE` | Returned, but the report is sampled. |
| `THRESHOLD_WITHHELD` | The report is subject to thresholding and this declared key event has no row. |
| `RESTRICTED` | The metric is named in `activeMetricRestrictions`. |
| `NOT_RETURNED` | The declared key event has no row and the report is not thresholded. |

For `KEY_EVENT_TOTALS` the declared key events form a known universe, so the
adapter emits an explicit row for each configured key event that GA4 did not
return, marked `KEY_EVENT_NOT_RETURNED` with `THRESHOLD_WITHHELD` or
`NOT_RETURNED` cells and a `THRESHOLDED` or `MISSING_EVENT` quality status. This
is what prevents "no row" from being read as "no conversions".

For the campaign shapes the universe of campaigns is unknown, so absence cannot
be enumerated. The report-level `subject_to_thresholding`,
`other_row_aggregation` and `completeness` facts carry that caveat instead, and
`exhaustive()` is the only sanctioned basis for treating the table as complete.

### The documented `(other)` roll-up is an aggregation fact, not an incident

Google states that high-cardinality dimension values are grouped into a row
labelled `(other)` and that "filters don't look inside the `(other)` row". The
`eventName` `inListFilter` therefore cannot exclude it, so `(other)` is an
expected response on a busy property. The adapter keeps the row with its value
unmodified and records a dimension status — `CAMPAIGN_OTHER_ROW`,
`SOURCE_OTHER_ROW`, `MEDIUM_OTHER_ROW`, or `EVENT_OTHER_ROW` — plus
`PROVIDER_REPORTED_UNKNOWN` on the row, and sets `other_row_aggregation`. An
`(other)` event bucket mints no conversion action reference, does not satisfy a
declared key event, and projects as `conversion_kind = KEY_EVENT_OTHER_ROW`.
`SENSITIVE_DATA_RISK` stays reserved for an actually undeclared event name.

## Freshness

GA4's Data API returns **no** per-row or per-report freshness field. The
adapter records only `observed_at` (the local, timezone-aware read instant) and
keeps `data_fresh_through = null`. `freshness_disclosure` is the constant
`NO_PROVIDER_FRESHNESS_FIELD_OBSERVED_AT_ONLY`. The model rejects any GA4
aggregate report that tries to set a freshness instant.

## Attribution

The Data API exposes no attribution model on `runReport` or its metadata.
Attribution settings exist only on the separate Admin API **v1alpha**
`properties.getAttributionSettings` surface, which this read-only reporting
profile does not call. The envelope therefore records:

```text
attribution_model      = GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED
attribution_window     = null
attribution_disclosure = GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED
```

The pilot `run_report` path previously wrote `attribution_model = "GA4_DATA_API"`
for the same surface. That label implied a known model, so both Data API report
paths now emit the single honest value. `run_realtime_report` keeps
`GA4_REALTIME`, which names the surface rather than an attribution claim.

### History note: the attribution label changed without a schema bump

`ReportEnvelope.schema_version` is still `1`. The pilot label therefore changed
from `GA4_DATA_API` to `GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED` in place
on this branch, and an append-only ledger that spans the change holds **both
values for the same GA4 Data API surface**. Records written before this branch
keep the old string; the value is a connector labelling correction, not a
property setting that moved.

Consumers must treat the two strings as one surface:

- reconciliation must not report the difference as an out-of-band change under
  AGENTS rule 24 — no remote GA4 state changed, and no actor is implied;
- a comparison across the boundary must not read it as an
  `ATTRIBUTION_MISMATCH` between two differently attributed sources; and
- `connector_version` on each envelope, not the label, distinguishes which
  connector produced a record.

A future envelope schema bump plus an upcaster is the clean way to normalize
old records. That belongs to the ledger lifecycle ticket (`H03`), not here.

### Follow-up recorded, not applied

The reviewer recommends renaming the realtime label `GA4_REALTIME` to
`GA4_REALTIME_NO_ATTRIBUTION_DOCUMENTED` for the same honesty reason. It is
deliberately **not** changed here: the realtime surface is outside this
ticket's scope, and a second in-place label change would widen the mixed-value
window described above. It should be done together with the envelope schema
bump.

No attribution model is guessed, and GA4 rows are never summed with an ad
provider's differently attributed conversions.

## Key events are not provider conversions

Per AGENTS rule 16 and
[measurement-control.md](../measurement-control.md), a GA4 key event is not
automatically a Google Ads conversion and is not a native signal for Meta,
LinkedIn, TikTok, Reddit, or OpenAI Ads. Every aggregate report carries
`key_event_disclosure = GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION`, and
projected conversion rows use `conversion_source = "GA4"` with
`conversion_kind = "KEY_EVENT"`.

## Joins are resolved by M00.2, not here

Every row read by this adapter carries `join_status = "UNMAPPED"`, the
`UNMAPPED` quality status, and
`join_disclosure = DETERMINISTIC_PROVIDER_JOIN_DEFERRED`, so GA4 rows stay
visible and excluded from authoritative joined totals. Ticket M00.2 resolves
them against a customer-declared contract and is documented in
[ga4-campaign-joins.md](ga4-campaign-joins.md); the optional `join_map=`
keyword on `run_aggregate_report` changes no request and only attaches that
already-declared result. Dimension values are
retained **exactly as GA4 returned them**, including `(not set)`, `(other)`,
`(direct)` and `(none)`; the adapter records a `dimension_statuses` marker
instead of rewriting the value. No campaign resource reference is minted, so
M00.2 can perform the deterministic join without re-reading GA4.

## Normalized projection and persistence

`Ga4AggregateReport.to_normalized_report()` projects onto the existing
`NormalizedReport` contract so the ordinary ledger, redaction, and artifact path
is reused unchanged:

- one `PerformanceRow` per aggregate row at `ACCOUNT` grain (no campaign
  resource is claimed), with `dimension_type = "GA4_<shape>"` and
  `dimension_value` set to the digest of the exact dimension tuple, which is
  bounded and stable;
- exact dimension values in the allowlisted extensions
  `ga4_sessionCampaignName`, `ga4_sessionSource`, `ga4_sessionMedium`,
  `ga4_eventName`, `ga4_totalUsers`;
- `sessions` / `key_events` left `null` for any non-measured cell; and
- `ConversionBreakdownRow` records only for `KEY_EVENT_TOTALS`, where the event
  identifies the row unambiguously. A campaign-scoped key-event conversion row
  needs the M00.2 join to be identifiable and is not emitted.

The append-only ledger is canonical history under AGENTS rule 22, so a stored
`null` must stay attributable. Every projected row therefore also persists its
per-metric cell status and the report-level completeness facts through
allowlisted extensions:

| Extension key | Carries |
| --- | --- |
| `ga4_cell_status_sessions` / `ga4_cell_status_totalUsers` / `ga4_cell_status_keyEvents` | the `Ga4MetricCell` status behind each stored number or `null` |
| `ga4_dimension_statuses` | the row's dimension statuses, comma-joined |
| `ga4_completeness` | the report completeness value |
| `ga4_declared_row_count` / `ga4_consumed_row_count` | `rowCount` reconciliation |
| `ga4_pages_consumed` / `ga4_page_bound` | pagination bound provenance |
| `ga4_subject_to_thresholding` / `ga4_other_row_aggregation` | provider withholding flags |
| `ga4_sampling_consistency` | sampling agreement across pages |
| `ga4_empty_reason` | present only when GA4 declined |
| `ga4_restricted_metrics` | present only when metrics were restricted |

`ConversionBreakdownRow` carries the reviewed subset
`ga4_cell_status_keyEvents`, `ga4_dimension_statuses`, `ga4_completeness`;
all other conversion extensions remain rejected.

`Ga4AggregateReport.quality_summary()` is agent-visible output only and
excludes rows; it is a projection of the same facts, not their only home.

## Wiring

`adhelm report ga4` selects the fixed aggregate contract when the input JSON
carries the required `aggregate_shape` discriminator. `aggregate_shape` is not a
field of `Ga4ReportQuery`, and that model forbids extra fields, so every input
that validated before still validates and routes to the unchanged pilot path.
No CLI command was added or changed.

Registering `Ga4AggregateQuery` in the published schema catalog is a one-line
`cli.py` change that this ticket does not own; it is submitted in the handoff.

## Evidence limits

- **Fixture-only.** Every test uses `httpx.MockTransport`. No socket is opened,
  no credential is read, and no GA4 property was queried.
- **No GA4 UI reconciliation.** The DoD requires an identical request to
  reconcile with the GA4 UI within documented tolerances. That is live work
  under `L01` and is not evidenced here.
- **No quota evidence.** `returnPropertyQuota` is never requested.
- **No 360 / sampling-level behaviour.** `properties.reportTasks.create` and its
  `samplingLevel` field are not used.
- **Sampled and thresholded fixtures are synthetic.** They prove the adapter's
  handling of the documented response shape, not that a real property produces
  those shapes.
- **`emptyReason` values are not enumerated** by Google; the adapter treats the
  field as a bounded opaque string.

## Tests

`tests/test_ga4_data_aggregates.py` covers: fixed request shape and the absent
`keyEvents:<name>` form; multi-page consumption; page-bound exceeded; `rowCount`
disagreement; rows beyond the declared total; thresholded, withheld and missing
key events; sampled rows and the corrected `SamplingMetadata` field names;
sampling agreement and disagreement across pages; restricted metrics; `(other)`
roll-up on campaign and on `eventName`; empty result with `emptyReason`;
currency and timezone binding refusals before any report request; metadata
drift; metric and dimension header mismatch; undeclared event names; duplicate
dimension combinations; `RESPONSE_TOO_LARGE` through `SafeHttpClient`; the
privacy validator and the report-level completeness facts on persisted rows;
byte-identical normalized output; and the workflow route with ledger
persistence.

`tests/test_ga4_data_aggregates_review.py` is the independent M00.1 review
suite. It pins the repaired behaviour for the `(other)` `eventName` roll-up,
the exhaustiveness of suppressed and declined results, `(other)` statuses on
source and medium, de-duplicated row quality statuses, per-date-range sampling
windows, and the persistence of cell statuses.

`tests/test_ga4_data_aggregates_rereview.py` pins that a report assembled
directly from roll-up rows — not through the adapter — is still never
exhaustive.

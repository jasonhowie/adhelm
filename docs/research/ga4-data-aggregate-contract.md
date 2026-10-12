# GA4 Data API aggregate request/response contract

Fetched and verified 2026-09-08 against the public Google Analytics Data API
v1beta reference. Every claim below is either cited to an official Google page
or explicitly marked `UNVERIFIED`. No live GA4 property was queried to produce
this document.

## Sources

| Ref | Page | Google "Last updated" |
| --- | --- | --- |
| S1 | [`properties.runReport`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport) | 2026-04-23 |
| S2 | [`RunReportResponse`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/RunReportResponse) | 2026-04-23 |
| S3 | [`ResponseMetaData`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/ResponseMetaData) | 2026-04-23 |
| S4 | [`DateRange`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/DateRange) | 2026-04-23 |
| S5 | [`OrderBy`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/OrderBy) | 2026-04-23 |
| S6 | [API dimensions and metrics](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema) | 2026-08-26 |
| S7 | [Reporting data expectations](https://developers.google.com/analytics/devguides/reporting/data/v1/reporting-data-expectations) | 2026-08-26 |
| S8 | [Create a report (basics)](https://developers.google.com/analytics/devguides/reporting/data/v1/basics) | 2026-08-26 |
| S9 | [Admin API v1alpha `properties.getAttributionSettings`](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties/getAttributionSettings) | fetched 2026-09-08 |

## Dimensions and metrics used by the fixed shapes

All are documented core-reporting fields in S6.

| API name | Kind | Documented description (S6) |
| --- | --- | --- |
| `sessionCampaignName` | dimension | "The marketing campaign name for a session. Includes Google Ads Campaigns, Manual Campaigns, & other Campaigns." |
| `sessionSource` | dimension | "The source that initiated a session on your website or app." |
| `sessionMedium` | dimension | "The medium that initiated a session on your website or app." |
| `eventName` | dimension | "The name of the event." |
| `sessions` | metric | "The number of sessions that began on your site or app (event triggered: session_start)." |
| `totalUsers` | metric | "The number of distinct users who have logged at least one event, regardless of whether the site or app was in use when that event was logged." |
| `keyEvents` | metric | "The count of key events. Marking an event as a key event affects reports from time of creation. It doesn't change historic data." |

### There is no documented `keyEvents:<key event name>` metric

**Verified negative result.** S6 lists exactly two parameterized per-key-event
metric forms, and both are rates, not counts:

- `sessionKeyEventRate:event_name` — "The percentage of sessions in which a
  specific key event event was triggered."
- `userKeyEventRate:event_name` — "The percentage of users who triggered a
  specific key event event."

S6 additionally warns: "If you try to request the key event rate for an event
that is not registered as a key event, the request will fail."

Consequence for this product: a per-key-event **count** must come from the
`eventName` dimension combined with the `keyEvents` metric, restricted by a
`dimensionFilter` `inListFilter` to the property's configured key events. The
adapter therefore never emits a `keyEvents:<name>` metric name.

`UNVERIFIED`: whether a future Data API release adds a per-key-event count
metric. Re-check S6 before assuming one exists.

## Request semantics

- `limit` (S1): "The number of rows to return. If unspecified, 10,000 rows are
  returned. The API returns a maximum of 250,000 rows per request, no matter how
  many you ask for. `limit` must be positive."
- `offset` (S1): "The row count of the start row. The first row is counted as
  row 0. When paging, the first request does not specify offset; or equivalently,
  sets offset to 0; the first request returns the first `limit` of rows. The
  second request sets offset to the `limit` of the first request."
- `keepEmptyRows` (S1): "If false or unspecified, each row with all metrics equal
  to 0 will not be returned." The adapter sends `false` explicitly, so an absent
  row means *no returned data*, never a proven zero.
- `dateRanges[].startDate` / `endDate` (S4): both are **inclusive**. "The
  inclusive start date for the query in the format `YYYY-MM-DD`." / "The
  inclusive end date …". Relative forms (`NdaysAgo`, `yesterday`, `today`) are
  "inferred based on the property's reporting time zone"; the adapter never uses
  them and always sends absolute property-local dates.
- `orderBys[].dimension` (S5): `{"dimensionName": …, "orderType": "ALPHANUMERIC"}`,
  where `ALPHANUMERIC` is "Alphanumeric sort by Unicode code point". A fixed
  total order is required for stable `limit`/`offset` paging.
- Authorization scopes (S1): `https://www.googleapis.com/auth/analytics.readonly`
  or `https://www.googleapis.com/auth/analytics`. The reporting profile uses the
  readonly scope only.

`UNVERIFIED`: S1/S8 do not state that `limit`/`offset` paging is snapshot
isolated. Pages may therefore be drawn from different underlying results; the
adapter reconciles `rowCount` across pages instead of assuming stability.

## Response semantics

- `rowCount` (S2): "The total number of rows in the query result. `rowCount` is
  independent of the number of rows returned in the response, the `limit` request
  parameter, and the `offset` request parameter."
- `ResponseMetaData` (S3) has exactly these fields: `dataLossFromOtherRow`,
  `samplingMetadatas[]`, `schemaRestrictionResponse`, `currencyCode`, `timeZone`,
  `emptyReason`, `subjectToThresholding`.
  - `dataLossFromOtherRow`: "If true, indicates some buckets of dimension
    combinations are rolled into `(other)` row."
  - `currencyCode`: "If `currencyCode` was specified in the request, this
    response parameter will echo the request parameter; otherwise, this response
    parameter is the property's current `currencyCode`."
  - `timeZone`: "The property's current timezone", IANA formatted.
  - `emptyReason`: "If empty reason is specified, the report is empty for this
    reason." The set of possible values is **not enumerated** in S3.
  - `subjectToThresholding`: "this report is subject to thresholding and only
    returns data that meets the minimum aggregation thresholds. It is possible
    for a request to be subject to thresholding … and no data is absent from the
    report."
- `SamplingMetadata` (S3) fields are **singular scalars**: `samplesReadCount`
  (int64 string) and `samplingSpaceSize` (int64 string). "To calculate the
  percentage of available data that was used in this report, compute
  `samplesReadCount/samplingSpaceSize`." "One `samplingMetadatas` is populated
  for each date range … However if the results are not sampled, this field will
  not be defined."
- `ActiveMetricRestriction` (S3) fields are `restrictedMetricTypes[]` (enum
  array: `RESTRICTED_METRIC_TYPE_UNSPECIFIED`, `COST_DATA`, `REVENUE_DATA`) and
  `metricName`.

### Correction to the prior pilot validator

The pilot adapter validated `samplesReadCounts` / `samplingSpaceSizes` (plural
lists) and `restrictedMetricType` (singular). Neither name appears in S3. A real
sampled or restricted response would have been rejected as
`PROVIDER_RESPONSE_INVALID`. The validator now matches S3 exactly.

## Freshness

**There is no per-row or per-report freshness field.** S3's field list contains
no timestamp of any kind, and S2's response body contains none. S7 states only
that "Google Analytics takes time to process and aggregate event data. When
working with very recent data, you might see minor differences between reports
if there is a time delay between data retrievals."

Consequence: `ReportEnvelope.data_fresh_through` stays `null` for GA4. Only the
local `observed_at` instant is provable. Any claim of a GA4 "data current
through" timestamp would be fabricated.

`UNVERIFIED`: the exact realtime/intraday/daily processing intervals are
described in Analytics Help, not in the Data API reference, and Google states
they are "not a guarantee, nor an SLA or an SLO". They are not encoded as a
product tolerance.

## Attribution

**The Data API exposes no attribution model.** No field of `RunReportRequest`
(S1), `RunReportResponse` (S2), or `ResponseMetaData` (S3) carries an
attribution model, lookback window, or conversion-credit setting. S7 documents a
related but distinct property setting — "Reporting identity … decides how users
are deduplicated in reports" — which is also not returned by `runReport`.

Attribution settings exist only on the **Admin API v1alpha** surface
`properties.getAttributionSettings` (S9); they are absent from Admin API v1beta.
The read-only reporting profile deliberately does not call that method, so the
normalized envelope records
`attribution_model = "GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED"` rather than
naming a model.

`UNVERIFIED`: the effective attribution model of any specific customer property.
Reading it would need a separate authorized Admin API v1alpha call and its own
ticket.

## Quality caveats Google documents for this surface (S7)

- Sampling: "the Google Analytics Data API `runReport` method can return sampled
  data, especially for large datasets or complex queries. While the Google
  Analytics UI also applies sampling, the specific thresholds and algorithms can
  differ, leading to minor differences in reported values."
- Thresholding: "If a report row contains a small number of users, that row may
  be excluded from the results."
- `(other)` row: "If a dimension has high cardinality, Google Analytics may group
  less-common values into a row labeled as `(other)`… filters don't look inside
  the `(other)` row."
- Unique-count approximation: "The Google Analytics Data API uses the
  HyperLogLog++ (HLL++) algorithm to estimate unique counts for metrics like
  Active Users and Sessions… the results are approximations rather than exact
  counts." This applies to `sessions` and `totalUsers` in the fixed shapes.
- Query specificity: adding dimensions "can result in fewer events used in
  calculations", so the campaign × key-event shape is not arithmetically
  comparable to the campaign-only shape.

## Not established by this document

- Any GA4 UI reconciliation result.
- Any live request/response from a customer property.
- Any quota behaviour (`returnPropertyQuota` is deliberately not requested).
- The exact enumeration of `emptyReason` values.

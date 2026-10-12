# Google Ads fixed read contract — 2026-09-04

## Evidence status

This repository implements a **fixture-tested only** Google Ads REST read
adapter.  It has made no live request, used no customer credential, and has no
mutation capability.  It must not be presented as provider or UI verified.

## Primary-source contract

Google documents the current REST surface as Google Ads API `v25` at
`https://googleads.googleapis.com/v25`.  Calls require an OAuth 2 access token
in `Authorization: Bearer …` and an independent `developer-token` header.  A
manager acting on a client account additionally sends `login-customer-id`, a
hyphen-free manager customer ID; without it manager access can be denied.
[Authorization and HTTP headers](https://developers.google.com/google-ads/api/rest/auth)
[API call structure](https://developers.google.com/google-ads/api/docs/concepts/call-structure)

The adapter uses only `POST
/v25/customers/{customerId}/googleAds:search`.  Search returns fixed pages of
up to 10,000 rows and continues with `nextPageToken` supplied as `pageToken`
in the same request body.  Google documents that `pageSize` is unsupported for
this endpoint. [Search reference](https://developers.google.com/google-ads/api/reference/rpc/v25/GoogleAdsService/Search)
[Search and SearchStream](https://developers.google.com/google-ads/api/rest/common/search)

The total-count option is nested as
`"searchSettings": {"returnTotalResultsCount": true}`. It is not a top-level
request field in v25. The 2026-09-06 source review corrected the earlier
fixture-tested request and now asserts the nested shape on identity,
inventory, reporting and continuation requests.
[Search request](https://developers.google.com/google-ads/api/reference/rpc/v25/SearchGoogleAdsRequest)
[Search settings](https://developers.google.com/google-ads/api/reference/rpc/v25/SearchSettings)

Every generic read query ends with
`PARAMETERS omit_unselected_resource_names = true` because these inventory and
report projections do not select resource names. Google otherwise returns
implicit resource names, which conflict with the deliberately closed row
schemas. The separate EDIT observer selects and validates resource names
explicitly and does not use this omission setting.
[GAQL query structure](https://developers.google.com/google-ads/api/docs/query/structure)

The v25 response may also contain `queryResourceConsumption` and
`metricAttributes`. The generic reader validates and discards this metadata;
it does not change normalized metrics or retain the metadata. The local
subset accepts a canonical nonnegative int64 decimal consumption string,
at most 32 metric entries with at most 16 attributes each, printable metric
names/keys of at most 160 characters, and printable values of at most 512
characters. Missing optional attribute lists and values use their empty
protobuf defaults; unknown keys, explicit nulls and malformed values fail.
These are local bounds, not published provider maxima or a live conformance
claim.
[Search REST response](https://developers.google.com/google-ads/api/reference/rpc/v25/GoogleAdsService/Search?transport=rest)
[Metric attributes](https://developers.google.com/google-ads/api/reference/rpc/v25/MetricAttributes)
[Attribute fields](https://developers.google.com/google-ads/api/reference/rpc/v25/MetricAttributes.Attribute)

The fixed GAQL projections use the v25 field references for
[customer](https://developers.google.com/google-ads/api/fields/v25/customer),
[campaign and campaign budget](https://developers.google.com/google-ads/api/fields/v25/campaign),
[ad group](https://developers.google.com/google-ads/api/fields/v25/ad_group),
[ad group ad](https://developers.google.com/google-ads/api/fields/v25/ad_group_ad),
[metrics](https://developers.google.com/google-ads/api/fields/v25/metrics), and
[segments](https://developers.google.com/google-ads/api/fields/v25/segments).

The target customer and optional login customer are local configuration values
of exactly ten decimal digits, without formatting hyphens.  The identity query
must return the exact configured `customer.id`, `currency_code`, and
`time_zone` before inventory or reporting can proceed.

## Implemented fixed queries

The client accepts no GAQL text, resource path, selected field, filter, or
sort input.  It compiles these bounded queries only:

1. Customer identity: `customer.id`, `customer.currency_code`,
   `customer.time_zone` from `customer`.
2. Campaign inventory: campaign identifier, status, channel type, and dates.
3. Ad-group inventory: campaign and ad-group identifiers, status, and type.
4. Ad inventory: campaign, ad-group, ad identifiers/types, and ad status.
5. Campaign-day delivery: campaign identifier, `segments.date`, impressions,
   clicks, and `metrics.cost_micros` over an inclusive date window of at most 367 days.
6. Native-ad-day delivery: campaign, ad-group, and native-ad identifiers plus
   `segments.date`, impressions, clicks, and `metrics.cost_micros` over that
   same 367-day maximum inclusive window.  It carries no creative binding and is explicitly
   labelled `CREATIVE_NOT_EXPOSED`.

Google’s protobuf JSON mapping represents 64-bit metric and monetary values as
decimal strings.  The shared `Money` contract currently has a reviewed two-decimal conversion
for this adapter, so delivery reads fail before report egress unless the
verified customer currency is USD. Google documents `metrics.cost_micros` as
an `INT64` sum of CPC and CPM costs for the period. It routinely has fractions
of a cent. The adapter therefore applies `HALF_EVEN_TO_MINOR` only to the
USD `Money.minor_units` display-compatible value, and preserves the exact
provider decimal string in `provider_extensions.google_ads_cost_micros` beside
the fixed `google_ads_rounding` marker. For example, 5,001 micros becomes one
cent; the 5,000-micro half-cent tie becomes zero cents. This does **not** make
the cents amount lossless: consumers that require exact per-row cost use the
stored micros field and must not recompute totals from rounded row cents. The
adapter performs no cross-row cost aggregation; a future aggregate projection
must sum the retained micros before applying a declared rounding policy.
Budget and bid metadata remain lossless-only and reject values that are not
whole USD cents. [v25 metrics field reference](https://developers.google.com/google-ads/api/fields/v25/metrics)
Attribution window/model and sampling are not inferred from these reads, and
`data_fresh_through` stays null.  Processing state is marked only as
`PROCESSING_PENDING` for the customer-local open day, as described under
"Freshness and repeated rows"; no other processing state, and no settlement
time for a closed day, is inferred.  The envelope sets the attribution model to
`NOT_AVAILABLE_FROM_PROVIDER` and marks that unavailable provider fact
explicitly rather than inventing a provider model.

## Additive audit-support reads — 2026-09-09

The fixed query set grew by one query and by fields on three existing ones, so
that `adhelm.audits.google_ads` has presence and enum facts to evaluate. Every
addition is read-only and none retains customer advertising content.

**Sources checked 2026-09-09** (fetched by this research pass; no live API call
was made):

- [`campaign` fields](https://developers.google.com/google-ads/api/fields/v25/campaign)
  for `network_settings.target_google_search`, `target_search_network`,
  `target_content_network`, `target_partner_search_network`, and
  `geo_target_type_setting.positive_geo_target_type` /
  `negative_geo_target_type`.
- [`campaign_budget` fields](https://developers.google.com/google-ads/api/fields/v25/campaign_budget)
  for `period`, `delivery_method`, and `explicitly_shared`.
- [`campaign_criterion` fields](https://developers.google.com/google-ads/api/fields/v25/campaign_criterion)
  for `criterion_id` and `type`, filtered to `AD_SCHEDULE`.
- [`ad_group_ad` fields](https://developers.google.com/google-ads/api/fields/v25/ad_group_ad)
  for `policy_summary.approval_status` and `policy_summary.review_status`, and
  for `ad.final_urls` and `ad.responsive_search_ad.headlines` /
  `descriptions`.
- [`customer` fields](https://developers.google.com/google-ads/api/fields/v25/customer)
  for `test_account`.
- [Responsive search ads](https://developers.google.com/google-ads/api/docs/ads/responsive-search-ads)
  for the documented minimum of three headlines and two descriptions.
- [Developer token](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)
  for the Test/Basic/Standard access levels and their operation allowances.
- [Rate limits and quotas](https://developers.google.com/google-ads/api/docs/best-practices/quotas)
  for `RESOURCE_EXHAUSTED`.

### The added fixed queries

7. Customer identity now also selects `customer.test_account`. An account that
   returns no flag is `None`: neither test nor proven production.
8. Campaign inventory now also selects the four `network_settings` booleans,
   both `geo_target_type_setting` enums, and `campaign_budget.period`,
   `delivery_method` and `explicitly_shared`. An omitted boolean is retained as
   `None`, never as a proven `false`, because a protobuf JSON default and an
   unreturned field are indistinguishable.
9. **New:** campaign ad-schedule presence, from `campaign_criterion` filtered
   to `campaign_criterion.type = 'AD_SCHEDULE'`, ordered by campaign and
   criterion identifier. Only the **set of campaign identifiers** that returned
   at least one such criterion is kept; no day, hour, or bid modifier is read
   or retained. A criterion naming a campaign the same read did not return
   fails the whole inventory as a mixed response.
10. Ad inventory now also selects `ad_group_ad.policy_summary.approval_status`
    and `review_status`, plus `ad_group_ad.ad.final_urls` and
    `ad_group_ad.ad.responsive_search_ad.headlines` / `descriptions`.

### Creative and destination redaction

The last three fields are selected so that their **counts** can be retained.
`_redact_ad_creative` replaces each of those collections with its length
immediately after the response is parsed, before the shared privacy screen and
before any other reader in the module. Members are checked for their JSON type
only; no member value is compared, normalized, screened, or placed in a
message. After that call there is no headline, description, or destination text
left in the page for a later parser, projection, log, export, or error to
reach. Two consequences are deliberate:

- No ad copy or landing page can be persisted, exported, or leaked through a
  diagnostic.
- A legitimate account whose headline contains a phone number or whose landing
  page sits under an `/account/` path does not fail the read on the shared
  sensitive-text screen, because that screen never sees the value.

The retained snapshot keeps presence and a documented-minimum bucket, not the
counts; `GoogleAdsAd` keeps the exact counts for a caller holding the live
read. `creative_capture_status` stays `CREATIVE_NOT_EXPOSED`: a count is not
creative capture.

### The retained normalized state shape

These reads also changed **what is retained**, not only what is requested. Two
fields were added to the local normalized snapshot state:

- `normalized_state.budget_type` on a Google Ads campaign, set to `DAILY` when
  `campaign_budget.period` is `DAILY` and left absent otherwise. Google's
  `CUSTOM_PERIOD` is neither `DAILY` nor `LIFETIME`, so it is **not** coerced
  into the shared closed vocabulary; the exact period is retained as a
  `GOOGLE_ADS_BUDGET_PERIOD_*` data-quality status instead.
- `normalized_state.objective` on a Google Ads ad, carrying the ad type.

`normalized_state_digest` is derived from that state, so the first read after
this change rewrites the digest of every retained campaign and every retained
ad **even though nothing at Google moved**. A naive snapshot diff would then
publish an out-of-band change for the whole account. Two things prevent that:

1. `GOOGLE_ADS_STATE_SHAPE_VERSION` is recorded beside every Google Ads
   snapshot as a `GOOGLE_ADS_STATE_SHAPE_<version>` data-quality status. It
   lives in `data_quality_statuses`, never in `normalized_state`, so recording
   it creates no drift of its own, and it must be bumped in the same change as
   any future edit to what `_google_snapshots` retains.
2. `adhelm.audits.google_ads` compares only the fields **both** retained reads
   returned. A field present on one side's key set and absent from the other is
   reported as `GOOGLE_ADS_LOCAL_RETAINED_STATE_SHAPE_DIFFERS`, never as an
   out-of-band change at Google. That absence is genuinely ambiguous — the
   snapshot writer drops a `None`, so a campaign losing its end date removes
   the key exactly as a connector release that stopped retaining it would — and
   claiming a remote change from it would be a fabricated observation.

`campaign_budget.delivery_method` is retained the same way, as a
`GOOGLE_ADS_BUDGET_DELIVERY_METHOD_*` status, and is read by one audit rule:
Google documents accelerated delivery as spending a budget as quickly as
possible rather than evenly across the period.

### Pagination completeness

Every fixed search already sends `searchSettings.returnTotalResultsCount` and
walks `nextPageToken` to exhaustion. The adapter now reports what that walk
proved rather than assuming it:

- `GoogleAdsInventory.pagination_completeness` is `PROVEN_COMPLETE` only when
  **every** search of that read declared a total and the retained rows equalled
  it; otherwise it is `INCOMPLETE_TOTAL_NOT_DECLARED`.
- A delivery report adds the `COMPLETE` quality status to its envelope only
  under the same condition. Without it the envelope stays silent, and the
  provider audit reports the row set as having no proven read boundary.
- A total that changes between pages still fails closed with
  `REPORT_CHANGED_DURING_PAGINATION`, and page-token repetition, an empty
  continuing page, and the ten-page ceiling still fail as before.

### Canonical wire spelling

Google serializes every int64 as a canonical decimal string. The reader accepts
that spelling only: ASCII digits, no sign, no leading zero. Entity identifiers
and budget, bid and `metrics.cost_micros` micros are accepted at most at the
int64 maximum. Impressions, clicks and total result counts are further bounded
at 2^53-1 (9,007,199,254,740,991), the largest integer a JSON number or a
float64 consumer holds exactly, so `9007199254740992` is refused. A looser spelling
(full-width digits, `007`, `005000`) converts to the same integer but would
name a different local reference, or retain a different exact-micros provenance
string, so it is refused instead of normalized. Dates are read only as
`YYYY-MM-DD`; `date.fromisoformat` would also accept `20260901` and ISO week
dates. The empty string remains an absent campaign start or end date.

### Freshness and repeated rows

`segments.date` is stamped in the customer's time zone, so the day still
accruing is the **customer-local** date on which the read began (taken from the
clock before the first request), never the UTC date and never a later clock
read: a walk that crosses customer-local midnight read that day's rows while it
was still accruing, so the day stays pending. `observed_at` remains the instant
the walk finished. A report whose window ends on that date adds
`PROCESSING_PENDING` to its envelope, between `COMPLETE` (when present) and
`NOT_AVAILABLE_FROM_PROVIDER`, and the rows of that day carry
`PROCESSING_PENDING` instead of `COMPLETE`.
`COMPLETE` on the envelope remains a statement about the read boundary (every
page walked and the declared total matched), not about the data being final, so
the two coexist. A window ending on an earlier day carries no such status. No
claim is made about how long Google takes to settle a closed day, and
`data_fresh_through` stays null. A window ending after the customer-local date
is still refused before egress.

A response in which one resource repeats a day, on one page or across pages, is
refused as a whole: the repeat would be summed twice and nothing identifies the
authoritative copy. A day with no row is absent from the report. It is never
zero-filled, and an empty result is not a zero row, because the response cannot
distinguish no activity from no data. A metric that is absent or `null` is
refused, never read as zero; a canonical `"0"` is retained as zero.

### Quota and access classification

`classify_google_ads_transport_error` turns one shared-transport refusal into an
exact diagnostic from the observed HTTP status and the bounded whole-second
`Retry-After` the transport attached. **No response body is read**, so the
`RESOURCE_EXHAUSTED` error code Google documents in the body is not what this
function classifies; it classifies the HTTP status that carries it, and says
so.

- `401`/`403` → `GOOGLE_ADS_ACCESS_NOT_PERMITTED`, exit code 4, pointing at
  `adhelm auth google-oauth status`, the separate developer token, and the
  manager `login-customer-id` route.
- `429` → `GOOGLE_ADS_QUOTA_EXHAUSTED`, exit code 3, disclosing the bounded
  retry hint when the provider sent one and saying so when it did not, and
  stating that the daily operation allowance depends on the developer token's
  access level, which no reviewed read discloses.
- Any other status is returned unchanged rather than relabelled into a quota or
  credential problem it was never shown to be.

### Developer-token access level

Google documents Test, Basic and Standard developer-token access levels, and
they constrain both the reachable environments and the daily operation
allowance. **No reviewed v25 read returns the level in use.**
`GoogleAdsIdentity.developer_token_access_level` is therefore always
`NOT_DOCUMENTED_LOCALLY`, and the retained account snapshot carries
`GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED`. Nothing in this build
infers a tier, a quota, or an environment from it. `UNVERIFIED`: that no
reviewed read returns the tier is a documentation reading, not a live
observation.

`GOOGLE_ADS_API_VERSION` is now the one place this build names `v25`; every
request path, model literal, and retained snapshot reads it from there.

## Read completeness review (O2.1)

Gaps found against this contract, the `DOD-01-01`/`DOD-01-03`/`DOD-01-04`
requirement-map rows, `reporting-schema.md` and the Wave 2 exit list, limited to
read-only behavior:

| Gap | Disposition |
| --- | --- |
| The open current day was not stated: a window ending today carried only `COMPLETE`, and every row of it claimed `COMPLETE` | `PROCESSING_PENDING` on the envelope and on that day's rows, judged by the customer-local date |
| A resource repeated for one day was accepted and would be summed twice | Refused as a whole |
| `_decimal` accepted full-width digits and leading zeros (impressions, clicks, totals); the micros reader accepted leading zeros and kept them in the exact-micros provenance string; entity identifiers accepted leading zeros and values above int64 | One canonical int64 grammar for all of them |
| Dates accepted `20260901` and ISO week dates | `YYYY-MM-DD` only |
| `RESULT_LIMIT_REQUIRED` and `PAGINATION_LIMIT` named no remedy | `next_action` names the settings (`--maximum-records` for an inventory, `maximum_rows` in the report query, at most 100,000), says to shorten a report window, and states that a result is never truncated |
| The identity read capped itself at one row, so a second customer surfaced as a caller-selectable result bound instead of an invalid identity | The cap is two so the exactly-one check sees a second customer; any other count, including a response beyond two customers that trips the bound, is translated to `PROVIDER_RESPONSE_INVALID`. The fixed identity read never reports a bound the caller cannot change |
| The open day came from a clock read taken after the page walk, so a walk crossing customer-local midnight closed the day it had read while still accruing | The open day is the customer-local date when the read began, taken before egress |
| Auth, refresh, re-auth, revocation and scopes (`DOD-01-01`) | **Reviewed, no change.** The read takes an in-memory access-token callback fed by the shared Google OAuth path (`adhelm auth google-oauth`), whose scope map (`adwords` for Google Ads READ), refresh, uncertain-refresh blocking and reconnect-required behavior are covered by `tests/test_google_oauth_protocol.py`, `tests/test_google_oauth_credentials.py` and `tests/test_google_oauth_status.py`. This adapter classifies an observed `401`/`403` as `GOOGLE_ADS_ACCESS_NOT_PERMITTED` and a `429` as `GOOGLE_ADS_QUOTA_EXHAUSTED` (`tests/test_google_ads_read_completeness.py`). Live refresh, revocation and re-auth behavior is `L01` |
| Attribution (`DOD-01-01`) | **Not read, no change.** The envelope declares `NOT_AVAILABLE_FROM_PROVIDER` and a null window |
| API version (`DOD-01-01`) | **No change.** `GOOGLE_ADS_API_VERSION` remains the single place `v25` is named |
| Thin fixture coverage of identity mismatch, malformed identity, repeated and empty page tokens, the ten-page ceiling, the result bound, round-half-even micros, non-USD refusals, customer-local date boundaries both sides of UTC, zero versus missing metrics, and omitted versus explicit-false settings | `tests/test_google_ads_read_integrity.py` (existing tests already covered the declared total, quota and access classification, creative redaction and the developer-token disclosure) |
| Non-USD customers are refused for reports, and for an inventory that returns any budget or bid | **Deferred.** `Money` has no per-currency exponent and `reporting.py`'s native-decimal validators are shared; pinned as an explicit `UNSUPPORTED_CURRENCY` refusal |
| `customer.status` is not read, so a suspended or canceled customer is indistinguishable from one with no activity | **Deferred.** Needs a retained-state field, a state-shape version bump and an audit rule, which is not read-completeness of the existing surface; the audit keeps reporting `GA_POLICY_ACCOUNT_STATUS_NOT_READ` |
| Developer-token access level, conversion actions and goals, native conversion metrics, targeting criterion values, placements, tracking templates, creative text and final-URL values | **Not read.** Reported `NOT_IN_REVIEWED_FIXED_READ` by the audit; each needs its own primary-source field review |
| Inventory duplicates and parent links | **No change.** The shared snapshot hierarchy check already refuses duplicate resource references and missing or mistyped parents before anything becomes history |
| Live UI reconciliation, a second independent customer, current quota values | `L01`, not offline-verifiable |

Unverified against any live account: whether a segmented query omits days with
no activity or returns zero rows for them, how Google settles a closed day,
whether `campaign.end_date` is returned empty or as a far-future sentinel for a
campaign with no end, the `Retry-After` behavior, and whether the added
projections are accepted for a real customer.

## Safety and remaining gaps

- Egress is limited to `googleads.googleapis.com` through `SafeHttpClient`.
- Access and developer tokens are supplied separately by in-memory callbacks;
  neither appears in models, errors, tests, exports, or research evidence.
- Search requests set `searchSettings.returnTotalResultsCount`; responses are parsed with
  duplicate-key rejection and closed nested response schemas.  Raw responses, campaign names, creative text, and GAQL
  results are never persisted.
- All reads are bounded to ten pages and a caller-selected complete-result
  limit. Delivery requests are model-rejected before egress when their
  inclusive historical window exceeds 367 days.  Repeated/missing page tokens, an empty continuing page, changed total
  counts, unsupported fields, and malformed values fail explicitly.
- The adapter reports campaign-day and actual native-ad-day grain only.  It does
  not synthesize creative bindings, renditions, components, conversions,
  attribution, or causal metrics.

Still required before a stronger claim: authorized test-account identity,
developer-token access tier, OAuth/refresh/revocation behavior, API quota and
rate-limit handling, actual field compatibility, page-boundary behavior, UI
reconciliation, live timezone/currency/attribution/freshness behavior, and all
write/activation/recovery workflows.

The 2026-09-09 additive reads do not change that. In particular they are
`UNVERIFIED` in these respects: no live response has confirmed that Google
accepts the added campaign, criterion, or ad projections for a real customer;
no `429` or `RESOURCE_EXHAUSTED` refusal has been observed, so the
classification above is a documentation reading applied to a fixture; and the
developer-token access tier remains unobservable through any reviewed read.

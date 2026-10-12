# Meta Ads fixed read contract

Reviewed on 2026-09-04 for the local v1 read adapter. The adapter uses the
Marketing API's Graph API `v26.0` prefix and only HTTPS `GET` requests to
`graph.facebook.com`. It never sends an access token in a query parameter and
has no mutation method.

## Sources and version evidence

- Meta's [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog)
  lists `v26.0` as introduced on 2026-07-29. Direct documentation retrieval
  returned HTTP 429 during this review, so the version date was corroborated
  against the changelog mirror returned by web search. The adapter pins the
  version instead of relying on Meta's automatic version upgrade.
- Meta's [Marketing API overview](https://developers.facebook.com/docs/marketing-api/overview)
  and [Insights API](https://developers.facebook.com/docs/marketing-api/insights)
  are the primary endpoint references. The same HTTP 429 prevented copying
  their text into this repository.
- The adapter requires a customer-configured local token with `ads_read`. It
  does not request `ads_management`.

### Re-fetched 2026-09-09 for the additive read surfaces

The 2026-09-04 review recorded an unresolved Graph version pin because the
official pages returned HTTP 429. They were reachable on **2026-09-09**, and
each field, enumeration, and error code named in the additive sections below
was read from the page listed here on that date. Nothing below is inferred from
a previous version, a mirror, or an SDK.

| Fetched 2026-09-09 | What it settled |
| --- | --- |
| [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog) | `v26.0` is the latest version, introduced 2026-07-29 (`v25.0`: 2026-02-18). The adapter's pin matches the current latest version. The pin is now **corroborated**; no request has been sent to it by this product. |
| [Campaign reference](https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group/) | `objective` (21 documented values), `buying_type` (`AUCTION`, `RESERVED`), `bid_strategy` (`LOWEST_COST_WITHOUT_CAP`, `LOWEST_COST_WITH_BID_CAP`, `COST_CAP`, `LOWEST_COST_WITH_MIN_ROAS`), `effective_status` (`ACTIVE`, `PAUSED`, `DELETED`, `ARCHIVED`, `IN_PROCESS`, `WITH_ISSUES`) |
| [Ad Set reference](https://developers.facebook.com/docs/marketing-api/reference/ad-campaign/) | `targeting` ("'countries' is required"), `effective_status` (adds `CAMPAIGN_PAUSED`), `lifetime_budget` ("If specified, you must also specify an `end_time`"), `billing_event`, `bid_amount`, `bid_strategy` |
| [Ad reference](https://developers.facebook.com/docs/marketing-api/reference/adgroup/) | `effective_status` (`ACTIVE`, `PAUSED`, `DELETED`, `PENDING_REVIEW`, `DISAPPROVED`, `PREAPPROVED`, `PENDING_BILLING_INFO`, `CAMPAIGN_PAUSED`, `ARCHIVED`, `ADSET_PAUSED`, `IN_PROCESS`, `WITH_ISSUES`), `creative` (an `AdCreative`) |
| [AdCreative reference](https://developers.facebook.com/docs/marketing-api/reference/ad-creative/) | `image_hash`, `link_url`, `object_url` exist and are requestable; `object_story_spec`, `title`, and `body` exist and are the copy-bearing fields this adapter deliberately never requests |
| [Advanced targeting](https://developers.facebook.com/docs/marketing-api/targeting/) | The targeting spec's documented top-level keys include `geo_locations`, `age_min`, `age_max`, `interests`, `flexible_spec`, and `genders` |
| [Graph API rate limiting](https://developers.facebook.com/docs/graph-api/overview/rate-limiting/) | Error codes `4` (app request limit), `17` (user request limit), `32` (Pages API limit), `613` (custom rate limit); `X-Business-Use-Case-Usage` carries `estimated_time_to_regain_access` in minutes |

**UNVERIFIED, still.** App review outcome, Standard versus Advanced Access tier,
Business Verification state, sandbox availability, and any live behaviour of
`v26.0` remain unproven by this product. `auth meta-token status` reports app
review and Business Verification as `NOT_DOCUMENTED_LOCALLY` rather than
assuming any of them, and no command reads them.

## Fixed requests

The account identifier is normalized to `act_<numeric-id>` and is checked
against `GET /v26.0/act_<id>?fields=id,account_status,currency,timezone_name`.
The returned ID, IANA timezone, and reviewed currency are required before any
inventory or report request.

Inventory is bounded to ten pages and uses these exact edges and fields:

| Edge | Fixed fields |
| --- | --- |
| `/act_<id>/campaigns` | `id,effective_status,objective,buying_type,bid_strategy,daily_budget,lifetime_budget,spend_cap,start_time,stop_time` |
| `/act_<id>/adsets` | `id,campaign_id,effective_status,billing_event,bid_amount,bid_strategy,daily_budget,lifetime_budget,start_time,end_time,targeting` |
| `/act_<id>/ads` | `id,campaign_id,adset_id,effective_status,creative{id,image_hash,link_url,object_url}` |

`META_ADS_API_HOST` and `META_ADS_API_VERSION` in
`src/adhelm/providers/meta_ads.py` are the **only** places the host and the Graph
version are written, so the product cannot send two versions at once.
`META_ADS_READ_SURFACES` in the same module is the single declaration of what
each of these reads returns; the provider audit derives its coverage ceiling
from that map rather than restating it.

### Presence-only additions (2026-09-09)

`targeting` and `creative{...}` are requested for **presence only**. Their
values are reduced to booleans inside the parser and never reach a model, a
serialization, or local history:

| Fact | How it is derived | What is never read |
| --- | --- | --- |
| Targeting declared | The returned `targeting` object is non-empty | Any key's value |
| Targeting geography | `geo_locations` present and non-empty | Country, region, city, radius |
| Targeting age | `age_min` or `age_max` present | Either bound's value |
| Targeting interest | `interests` or `flexible_spec` present and non-empty | Interest name, audience identifier |
| Creative associated | `creative.id` returned | The identifier itself is not retained |
| Creative image | `creative.image_hash` present | The hash value |
| Creative link | `creative.link_url` **or** `creative.object_url` present | The URL, its query, its tagging |

`object_story_spec`, `title`, `body`, and `asset_feed_spec` are never requested,
so no ad copy reaches this process at all. A creative whose destination lives
only inside `object_story_spec` therefore returns no link to this read, and the
audit says exactly that rather than reporting a missing destination as proven.
The targeting object is **not** key-checked against a closed set — Meta
documents dozens of targeting keys and grows them per version — but its size is
bounded and no value is inspected beyond emptiness. Any unknown key inside
`creative{...}` fails closed, because that object's requested field set is
fixed and small.

The campaign buying type, the ad-set budget mode (daily / lifetime / both /
none), schedule-end presence, and the ad review state derived from
`effective_status` are recorded as explicit data-quality statuses rather than
invented state keys: the shared closed `NormalizedResourceState` in
`src/adhelm/ledger.py` has no field for any of them. Campaign `bid_strategy` is
different -- that field **does** exist in the shared state model, and this
change began retaining it; see the retained-state-shape section below. Every
presence fact is stated in **both** directions, so an absent status is never
read as either "present" or "absent". `billing_event` is requested and parsed
but has no shared state field, so it does not reach local history; the audit
reports that seam explicitly instead of treating the value as read.

### The retained normalized state shape

This change also changed **what is retained**, not only what is requested. One
field was added to the local normalized snapshot state:

- `normalized_state.bid_strategy` on a Meta campaign, carrying the documented
  campaign bid strategy. `src/adhelm/ledger.py` already declares the field, so it
  is a real state key and not a data-quality status, and
  `adhelm.audits.meta_ads` reads it back from there.

`normalized_state_digest` is derived from that state, so the first read after
this change rewrites the digest of every retained campaign **even though
nothing at Meta moved**. A naive snapshot diff would then publish an
out-of-band change for the whole account, which AGENTS.md rule 24 forbids. Two
things prevent that:

1. `META_ADS_STATE_SHAPE_VERSION` is recorded beside every Meta snapshot as a
   `META_ADS_STATE_SHAPE_<version>` data-quality status. It lives in
   `data_quality_statuses`, never in `normalized_state`, so recording it
   creates no drift of its own, and it must be bumped in the same change as any
   future edit to what `_meta_snapshots` retains.
2. `adhelm.audits.meta_ads` compares only the fields **both** retained reads
   returned. A field present on one side's key set and absent from the other is
   reported as `META_LOCAL_RETAINED_STATE_SHAPE_DIFFERS`, never as an
   out-of-band change at Meta. That absence is genuinely ambiguous -- the
   snapshot writer drops a `None`, so a campaign losing its stop time removes
   the key exactly as a connector release that stopped retaining it would --
   and claiming a remote change from it would be a fabricated observation.

### The local privacy screen

`adhelm.privacy.validate_data` screens every parsed Graph response before a
record exists. The creative's three value-bearing fields -- `image_hash`,
`link_url`, `object_url` -- are exempt from that whole-read screen and are
screened **one resource at a time** instead. An advertiser landing page
legitimately carries a contact address in its query string, and voiding a whole
bounded ad read over one such URL would take an entire account's inventory
offline for ordinary content this product never retains anyway. The presence
fact still stands, the value is withheld exactly as it would have been, and the
ad records `META_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN` so a withheld value
is never mistaken for an absent one.

Everything else -- including the whole `targeting` object, where an uploaded
audience really could carry a person-level value -- is still screened across
the whole response and still fails the read closed, as
`META_ADS_RESPONSE_WITHHELD_BY_LOCAL_PRIVACY_SCREEN` with exit code 3. That
diagnostic names the read that stopped and says this is a local screening
decision rather than a Meta error; it never names, quotes, or hashes the value
that matched.

### Rate-limit classification

`classify_meta_ads_graph_error` reads **only** the integer `error.code` from a
`200`-status Graph error envelope. `message`, `error_user_title`,
`error_user_msg`, and `fbtrace_id` are provider text and are never read, logged,
or interpolated into a diagnostic. Codes `4`, `17`, `32`, and `613` classify to
`META_ADS_RATE_LIMITED` with exit code 3; any other code becomes a fixed
`PROVIDER_REQUEST_FAILED`, and a non-integer code becomes
`PROVIDER_RESPONSE_INVALID` rather than a guess.

`classify_meta_ads_transport_error` classifies from the typed HTTP status the
shared transport attaches: `429` to `META_ADS_RATE_LIMITED` (exit 3), `401` and
`403` to `PROVIDER_ACCESS_REQUIRED` (exit 4) with guidance that names
`adhelm auth meta-token inspect` and refuses to assert an app-review outcome. Any
other status is returned unchanged rather than relabelled. `MetaAdsClient._get`
wraps every fixed read in it, so the classification reaches the caller on the
identity, inventory, and insights paths alike.

The bounded hint is read from `X-Business-Use-Case-Usage` **only** when the
header value is present and entirely ASCII decimal, and it is clamped to 1,440
minutes; anything else -- absent, structured JSON, non-ASCII digits -- is
`NOT_DISCLOSED`. In this build the hint is *always* `NOT_DISCLOSED` in practice:
`SafeHttpClient.request` rebuilds every response with a fixed content type and
discards provider headers, and it raises before reading a body for any status at
or above 400. `tests/test_meta_read_completeness.py` pins that structural fact
rather than claiming a bound this product cannot observe.

Daily reporting uses `/act_<id>/insights` with `time_increment=1`, a closed
`time_range` of at most 367 inclusive calendar days (model-validated before
egress), explicitly supplied `action_attribution_windows`, and exactly
one `level`: `campaign` or `ad`. Campaign rows request
`campaign_id,date_start,date_stop,impressions,clicks,spend,reach`; ad rows add
`adset_id,ad_id`. The output preserves those actual grains. Ad rows carry
`CREATIVE_NOT_EXPOSED` and `COMBINATION_UNKNOWN`; the adapter has no creative
binding capability.

`spend` is parsed as an exact decimal using a reviewed per-currency exponent.
Unreviewed currencies and values with excess precision fail. Budget fields are
accepted only as integer minor units. The API response does not provide a
verified freshness timestamp, so report freshness is `null` and the envelope
records `NOT_AVAILABLE_FROM_PROVIDER`. Attribution metadata comes only from
the exact windows supplied to the request.

### Value grammar, daily boundaries and freshness (O2.2)

Every number and date is matched against an ASCII-only grammar before it is
converted, because `str.isdecimal`, `int`, `Decimal` and `date.fromisoformat`
each accept spellings Graph does not document:

| Field | Accepted | Refused (examples) |
| --- | --- | --- |
| Resource ID | ASCII digits, no leading zero | `٣`, full-width digits, `0123`, signs, spaces |
| Count (`impressions`, `clicks`, `reach`) and native budget | A JSON string of at most 16 ASCII digits, at most 9,007,199,254,740,991 | a longer string (refused before `int`, whose 4300-digit limit would otherwise raise unclassified), `"٣"`, `"1.0"`, `"1e3"`, `"1_0"`, a JSON number, a boolean |
| `spend` | `0`, or digits without a leading zero, with an optional `.` and digits; converted by exact string arithmetic, so a fraction longer than the currency exponent (after trailing zeros) is refused as having unsupported precision, never rounded; the result in minor units is at most 9,007,199,254,740,991 (`90071992547409.91` USD is accepted exactly) | `1e2`, `1_0`, `-0`, `+1`, `NaN`, `Infinity`, `1.`, `.5`, `01`, spaces, non-ASCII digits, a string longer than 80 characters (refused first, as invalid, before the grammar is applied); a value over the bound, such as `90071992547409.92` or the whole-USD `9007199254740991`, is refused as exceeding the numeric bound (a magnitude failure, not a precision failure). Precision is checked before magnitude, so a value that fails both, such as `9007199254740991.001` USD, is refused as having unsupported precision |
| `date_start`/`date_stop` | `YYYY-MM-DD`, equal to each other and inside the window | `20260901`, `2026-W36-2`, a multi-day row |

An account `timezone_name` that is not a loadable IANA key (`../x`, `a/`, a
tzdata directory such as `America` or `Etc`, an unknown zone) is refused as
`PROVIDER_RESPONSE_INVALID`; it used to escape as an unclassified `ValueError`
or `IsADirectoryError` whose message carried a local path.

A metric that is absent or `null` refuses the whole response; it is never read
as zero. A string `"0"` is retained as zero. A window with no returned rows
returns no rows, because the response cannot say whether the account did not
deliver or Meta omitted the rows, so no zero row is fabricated.

A resource repeated on one day (within a page or on a later page) refuses the
whole report, because every aggregate would sum it twice and nothing identifies
the authoritative copy.

The future-window check and the one freshness claim use the **account's**
calendar day (the connector clock converted to the account timezone, never the
connector's UTC day). A window whose last day is that day carries
`PROCESSING_PENDING` on the envelope and on that day's rows (in place of the
row's `COMPLETE`); earlier days do not. `COMPLETE` on the envelope keeps its
existing meaning, that every documented cursor page was consumed, and the
provider audit still reads it that way. No claim is made about how long Meta
takes to settle a closed day or to finalize attribution.

A report that reaches its row bound is refused with a `next_action` that names
narrowing the window.

## Read completeness review (O2.2)

Gaps found against this contract, the `DOD-01-01`/`DOD-01-03`/`DOD-01-04`
requirement-map rows, `reporting-schema.md` and the Wave 2 exit list, limited to
read-only behavior. Identity, cursor pagination, rate-limit classification,
minor-unit budgets and the presence-only retention were already pinned by
`tests/test_meta_read_completeness.py` and `tests/test_meta_independent_review.py`
and are not repeated here.

| Gap | Disposition |
| --- | --- |
| The current account day was reported `COMPLETE` with no freshness caveat, and the future-window check had no test at a UTC-offset boundary | `PROCESSING_PENDING` on the envelope and the open day's rows; boundary tested at UTC+14 (open day ahead of UTC) and at an instant where the account day is behind UTC (Los Angeles, 03:00 UTC) |
| A resource-day repeated on a later page was accepted and would be summed twice | Refused |
| `spend` went through `Decimal(...)`, which accepts `1e2`, `1_0`, whitespace, non-ASCII digits and `-0` | ASCII decimal grammar |
| Resource IDs and counts used `str.isdecimal`, which accepts non-ASCII digits (a lookalike ID reached a request path and reference) | ASCII only |
| `date.fromisoformat` accepted `20260901` and `2026-W36-2` for an insight day | `YYYY-MM-DD` only |
| A malformed account `timezone_name` such as `../x` raised an unclassified `ValueError`, and a tzdata directory name such as `America` raised `IsADirectoryError` carrying a local path | `PROVIDER_RESPONSE_INVALID` |
| The report row-bound refusal named no remedy | `next_action` names window narrowing |
| Thin fixture coverage of identity mismatch (returned account, expected profile account, foreign account on a report), timezone/currency/grain mismatch before egress, zero versus missing metrics, an empty window, per-day UTC boundaries, report cursor following with the fixed request reissued, and repeated-day refusal | `tests/test_meta_read_completeness.py` |
| A Graph rate-limit refusal that arrives with an HTTP `4xx` status cannot be typed: the shared transport raises before reading a body for any status at or above 400, so the integer `error.code` is only readable on a `200` envelope. Which status Graph pairs with codes `4`, `17`, `32` and `613` is `UNVERIFIED` here | **Deferred.** Needs a body-aware change to the shared transport and its egress tests; until then such a refusal surfaces as the generic `PROVIDER_REQUEST_FAILED` with its status, never relabelled |
| Currencies outside the 14 reviewed exponents (for example `BRL`, `INR`) are refused as `UNSUPPORTED_CURRENCY` | **Deferred.** Each needs a reviewed minor-unit exponent against Meta's documentation; pinned as an explicit refusal, not guessed |
| A campaign `daily_budget`/`lifetime_budget` of `"0"` is retained as a native zero, not as an absent budget | **No change.** Whether Meta returns `"0"` for a campaign whose budget lives on its ad sets is `UNVERIFIED`; no semantics are invented |
| Targeting values, placement, creative content and review feedback, native conversions, pixel health, destination URLs, report freshness | **Not read.** Reported `NOT_IN_REVIEWED_FIXED_READ` by `META_ADS_READ_SURFACES` and by the audit |
| Live UI reconciliation, App Review and Advanced Access tier, current quota values, how long Meta takes to settle a closed day, whether campaigns/adsets/ads edges list archived or deleted objects, whether a zero-delivery day is returned as a zero row or omitted | `L01`, not offline-verifiable |

## Pagination and exclusions

The adapter does not follow `paging.next`. It validates the returned cursor and
next URL's HTTPS host, versioned account-edge path, and matching `after`
cursor, then reissues the original fixed request with that cursor. Meta
documents cursor paging as `cursors{before,after}` plus `next` **and**
`previous`; every page after the first carries `previous`, which is accepted
and then ignored, because this adapter only ever walks forward from its own
fixed first request. Graph also documents that "it is possible that a page may
be empty but contain a `next` paging link", so an empty continuing page is
followed within the fixed page cap rather than refused. Repeated cursors,
malformed paging, unknown response fields, raw creative fields, unexpected
nested objects, and more than ten pages fail closed. Each request sets Graph `limit` to the
smaller of the configured page size and the remaining complete-result bound;
a continuing page at that bound fails with `RESULT_LIMIT_REQUIRED` instead of
requesting or discarding an extra page.

Creative HTML, ad creative payloads, targeting, audiences, leads, conversion
actions, generic field selection, arbitrary Graph paths, and remote writes are
outside this adapter's contract.

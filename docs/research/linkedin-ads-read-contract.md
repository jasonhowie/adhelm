# LinkedIn Ads fixed read contract — 2026-09-04

## Evidence status

This repository implements a **fixture-tested only** fixed read adapter. It
has made no live request, used no customer credential, and has no mutation
methods. It is not provider-verified, UI-verified, or a substitute for
LinkedIn Marketing API program approval.

## Primary-source contract

[LinkedIn Marketing API versioning](https://learn.microsoft.com/en-us/linkedin/marketing/versioning?view=li-lms-2026-07)
documents the Marketing REST base as `https://api.linkedin.com/rest/`, the
required `Linkedin-Version: YYYYMM` header, and `X-Restli-Protocol-Version:
2.0.0`. The current adapter pins its reviewed published version, `202608`.
The versioning page must be rechecked before updating that value; no version is
inferred from an unversioned endpoint.

The documented Advertising API read scopes are `r_ads` for account resources
and `r_ads_reporting` for reporting. Access also depends on the authenticated
member's account role and LinkedIn application/API-program approval. The
adapter receives a bearer token only through an in-memory callback and never
stores it. [Ad account access and schema](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-accounts?view=li-lms-2026-05)
[Reporting](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-01)

Identity is the fixed `GET /rest/adAccounts/{id}` read. The documented account
schema supplies its numeric ID, three-letter currency, status, and serving
statuses. It does **not** establish an IANA account timezone, so the normalized
identity leaves timezone `null`. It does not borrow a local profile timezone
and call it provider data.

## Implemented fixed reporting

The client accepts no raw URI, URN, field-selection, report-query, or endpoint
input. It sends only this Ad Analytics finder request, restricted to one
configured numeric account:

```text
GET /rest/adAnalytics
  q=analytics
  accounts=List(urn:li:sponsoredAccount:{configured-id})
  pivot=CAMPAIGN | CREATIVE (fixed internally from public CAMPAIGN | AD)
  timeGranularity=DAILY
  dateRange=(start:(year:Y,month:M,day:D),end:(year:Y,month:M,day:D))
  fields=clicks,impressions,costInLocalCurrency,pivotValues,dateRange
```

The Reporting guide requires requested metrics through `fields` and documents
that otherwise only impressions and clicks are returned. It documents account
facets, CAMPAIGN and CREATIVE reporting levels, and the Ad Analytics finder.
The [reporting schema](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting-schema?view=li-lms-2026-06)
states that report `dateRange` is specified in **UTC**, daily bounds are
inclusive, and `costInLocalCurrency` is a `BigDecimal`. Therefore this adapter
uses UTC for both source and report timezone without inventing an account
setting. Each report accepts at most 367 inclusive days and an explicit
one-response result maximum of 1,000 rows. The official Ad Analytics reference
says that this finder does **not support pagination**; its documented response
has `paging.start` and `paging.count`, not a completion total. The adapter
therefore sends no undocumented page offset or count, rejects malformed/nonzero
paging offsets, and does not label a response `COMPLETE`. A response reaching
the requested row bound fails rather than pretending that it is exhaustive.

CAMPAIGN output retains native campaign-day grain. Public `AD` report requests
compile only to LinkedIn's documented CREATIVE delivery pivot; the adapter
represents it as an AD-level `LINKEDIN_SPONSORED_CREATIVE` delivery row,
without claiming it is a local creative version, rendition, component, or
campaign binding. Those rows carry
`CREATIVE_NOT_EXPOSED` and `COMBINATION_UNKNOWN` until an independently
reviewed provider-to-local creative association exists.

The current monetary conversion is deliberately USD-only. It accepts only a
plain decimal string; JSON floating-point money values and other account
currencies fail closed. A fractional-cent `costInLocalCurrency` is retained
unchanged in the namespaced
`linkedin_ads_cost_in_local_currency` provider extension and projects to the
shared USD-cent field using `HALF_EVEN_TO_MINOR`, recorded in
`linkedin_ads_rounding`. Audits use the retained native decimal rather than a
per-row rounded cent projection. The source documentation labels the field
`BigDecimal`, but its live serialization and currency exponent behavior still
need authorized validation. Attribution model/window, sampling, processing
state, and a true freshness timestamp are not supplied by this fixed request,
so the envelope uses `NOT_AVAILABLE_FROM_PROVIDER` and
`data_fresh_through: null`.

The one freshness fact the adapter can state without the provider is that a
window whose last day is the current UTC day (judged by the connector clock
converted to UTC, never by a local offset) still has an open day. That report
adds `PROCESSING_PENDING` ahead of `NOT_AVAILABLE_FROM_PROVIDER`; an earlier
window does not. No claim is made about how long LinkedIn takes to settle a
closed day.

A response is refused as a whole, not repaired, when a metric is absent or
`null` (a missing count is never read as zero), when a count is not a bounded
non-negative JSON integer (a string count such as `"7"` is refused; only the
cost `BigDecimal` arrives as a string), when a row is not a single in-window
UTC day, when a pivot is not the requested resource kind, when a field is
undocumented, or when one resource repeats a day (it would be summed twice and
nothing identifies the authoritative copy). An integer count of `0` and a cost
string of `"0"` are retained as zero. A response with no elements returns no
rows: it is not a zero row, because the reviewed guidance is that empty
analytics can mean no activity or missing read access, and the response alone
cannot tell them apart.
The narrowing remedy for a response that reaches the bound is part of the
refusal's `next_action`.

## Quota and access refusals

The shared transport reports only the observed HTTP status and a whole-second
`Retry-After` (clamped to one hour; any other form is discarded). The adapter
classifies from those two facts alone and reads no response body:

| Observed | Result |
| --- | --- |
| `429` | `LINKEDIN_ADS_RATE_LIMITED`, exit `3`, the disclosed wait or its absence, and the instruction to wait and run the same bounded read once more. LinkedIn publishes application, member and resource quotas in its developer analytics rather than in any response, so the exhausted quota is never named |
| `401`/`403` on inventory or analytics | `PROVIDER_ACCESS_REQUIRED`, exit `4`, naming the permission that read needs (`r_ads`, or `r_ads_reporting` for analytics), the local `auth linkedin-token status` check and the ad-account role |
| `401`/`403` on identity | the code is unchanged: the shared `PROVIDER_ACCESS_REQUIRED`, or `LINKEDIN_ADS_AUTHORIZATION_REQUIRED` for a caller that records its granted permissions. A caller without recorded permissions (doctor, the report workflow) now receives the `r_ads`-naming `next_action` instead of the shared "Reauthorize locally with the required scope." |
| any other status | unchanged `PROVIDER_REQUEST_FAILED` with the status; never relabelled as a quota or credential fault |

The documented value of any quota, its reset boundary and whether LinkedIn
sends `Retry-After` at all are `UNVERIFIED`.

## Read completeness review (O2.3)

Gaps found against this contract, the `DOD-01-01`/`DOD-01-03`/`DOD-01-04`
requirement-map rows, `reporting-schema.md` and the Wave 2 exit list, limited to
read-only behavior:

| Gap | Disposition |
| --- | --- |
| No quota/rate-limit classification: `429` surfaced as the generic `PROVIDER_REQUEST_FAILED` | Fixed above |
| `401`/`403` on inventory and analytics carried only the generic re-authorize text | Fixed above |
| A repeated resource-day in one analytics response was accepted and would double count | Refused |
| The open current UTC day was not stated in the report's freshness facts | `PROCESSING_PENDING` |
| The row-bound refusal named no remedy | `next_action` names window narrowing |
| Non-ASCII decimal digits (for example full-width) passed the identifier grammar and reached a request path | Identifier grammar is ASCII only |
| The creative finder was scoped by a second, independent campaign read | `list_creatives` accepts the campaigns the inventory just read; inventory passes them |
| Thin fixture coverage of identity mismatch, zero versus missing metrics, UTC day boundaries, version/host pinning, and of cursor pagination beyond repeat rejection and the page cap (existing tests covered cursor following, repeated tokens, fractional-cost exactness, the nonzero analytics offset and the page-size bound) | `tests/test_linkedin_read_completeness.py` |
| Non-USD accounts are refused | **Deferred.** A cent projection needs a reviewed per-currency exponent and `reporting.py`'s native-decimal validators, which Google Ads and Reddit share; pinned as an explicit `UNSUPPORTED_CURRENCY` refusal |
| `servingStatuses`, `test` and `type` are accepted from the account response but not retained | **Deferred.** The closed `NormalizedResourceState` has no field for them and no audit rule consumes them |
| Budget, schedule, bid, targeting, placement, creative content and review state, destination, conversion tracking | **Not read.** Reported `NOT_READ` by the audit; each needs its own primary-source field review |
| The analytics finder has no documented pagination or completion total | **No change.** Reports stay bounded and non-exhaustive; no pagination or window splitting is invented |
| Live UI reconciliation, second independent organization, current quota values | `L01`, not offline-verifiable |

Unverified against any live account: whether the creatives finder lists
archived or removed creatives without a status criterion, whether a zero-delivery
day is returned as a zero row or omitted, how LinkedIn settles a closed day, the
`Retry-After` behavior, and the exact `BigDecimal` serialization.

## Implemented bounded hierarchy inventory

The current account-scoped REST paths themselves are the fixed account
constraint; this adapter does not use an unscoped global account facet. It
uses only:

```text
GET /rest/adAccounts/{configured-id}/adCampaignGroups?q=search
  &search=(status:(values:List(ACTIVE,ARCHIVED,CANCELED,DRAFT,PAUSED,PENDING_DELETION,REMOVED)))
  &sortOrder=ASCENDING&pageSize={bounded}&pageToken={returned-only}
GET /rest/adAccounts/{configured-id}/adCampaigns?q=search
  &search=(status:(values:List(ACTIVE,PAUSED,ARCHIVED,COMPLETED,CANCELED,DRAFT,PENDING_DELETION,REMOVED)))
  &sortOrder=ASCENDING&pageSize={bounded}&pageToken={returned-only}
GET /rest/adAccounts/{configured-id}/creatives?q=criteria
  &campaigns=List(urn:li:sponsoredCampaign:{returned-campaign-id})
  &sortOrder=ASCENDING&pageSize={bounded}&pageToken={returned-only}
```

The [campaign-group search documentation](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups?view=li-lms-2026-04)
and [campaign search documentation](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaigns?view=li-lms-2026-07)
make their status criterion mandatory and enumerate the status sets compiled
above. Both document cursor pagination with `pageSize`, `pageToken`, and
`metadata.nextPageToken`, with a maximum page size of 1,000. The
[Creatives API](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-creatives?view=li-lms-2026-06)
documents `q=criteria` and campaign-URN criteria. The adapter obtains those
URNs only from its fixed campaign search (the inventory passes the campaigns it
already read, so the creative scope is the retained campaign set and the finder
is not asked for them twice) and verifies every returned account
and parent campaign URN before forming local references. It reads ten pages at
most for each documented finder, rejects repeated/empty continuation tokens,
and fails instead of truncating once the caller's complete-result bound is
reached.

Inventory keeps only IDs, immutable account/campaign parents, status, and the
documented campaign objective. It neither stores names, targeting, raw content,
creative text, nor provider response payloads. Creative listing is scoped by
the exact locally verified campaign set and has no implied creative-version,
rendition, or component join.

Still required: approved app and least-privilege OAuth lifecycle evidence,
authorized test-account calls, current version/field serialization checks,
complete hierarchy inventory contract, non-USD exact exponent review,
provider data freshness/processing/sampling behavior, attribution semantics,
creative association evidence, reconciliation, and every paused creation,
activation, maintenance, recovery, and independent external-organization
requirement in the v1 definition of done.

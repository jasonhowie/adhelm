# Reddit Ads fixed read contract — 2026-09-04

The implementation is **fixture-tested only**. No customer credential or live
Reddit request was used, and the adapter exposes no mutation method.

Reddit’s current [Ads API v3](https://ads-api.reddit.com/docs/v3/) uses
`https://ads-api.reddit.com/api/v3`, OAuth 2 bearer authentication, and the
least-privilege `adsread` scope. V2 is deprecated. The documented read paths
implemented here are `GET /ad_accounts/{id}`, and list reads for
`campaigns`, `ad_groups`, and `ads` under that exact account. The report read
is `POST /ad_accounts/{id}/reports`. [List Campaigns](https://ads-api.reddit.com/docs/v3/api/list-campaigns)
[List Ad Groups](https://ads-api.reddit.com/docs/v3/api/list-ad-groups)
[Get A Report](https://ads-api.reddit.com/docs/v3/api/get-a-report)

List responses paginate with provider-returned `pagination.next_url`; Reddit
explicitly says to follow that URL rather than reconstruct pagination. The
adapter pins the origin and port, accepts the original resource path or the
documented `/api/v3` cursor endpoint, and preserves the original opaque query
bytes through a GET-only transport path. It rejects other origin/path changes and repeated/empty pages,
and stops at ten pages or the configured complete-result bound. It never accepts
an arbitrary user URL. Raw responses are parsed in memory and never persisted.
The root cursor form appears in Reddit's [adaptive time zone pagination example](https://ads-api.reddit.com/docs/v3/guides/programs/reporting/add-adaptive-time).
All identity-taking methods validate account IDs again before URL construction;
constructing a typed identity directly cannot widen the fixed operation path.

The fixed report body requests `campaign_id,date` or `ad_id,date` and only
`impressions`, `clicks`, and `spend`. The [official reporting examples](https://www.postman.com/reddit-ads-api/reddit-ads-api-v3/documentation/nee6egu/use-cases)
establish lowercase request fields and the nested `data.metrics` response.
Daily responses may also include a midnight `hour`; the parser requires it to
agree with the UTC date. Ad-level rows leave campaign/group associations
unmapped until independently proven. The provider's `metrics_updated_at` is
validated but never treated as data-fresh-through evidence.

Delivery retention is checked against an injected UTC observation time minus
24 calendar months, including leap days. This is a retention precondition, not
an invented fixed request-span limit. Freshness, attribution and report
completeness remain explicitly unavailable. USD spend retains its exact signed-
int64 native micros as a decimal string alongside a declared half-even minor-
unit projection. CPC thresholds compare the exact micros; summing rounded row
costs is not an exact aggregate.

The general API overview says reports paginate, while the detailed official
response example omits pagination. Its continuation method/shape has not been
established here. A recognized report continuation therefore fails with
`REPORT_PAGINATION_UNVERIFIED`; no speculative continuation request or complete
report claim is made, and the first response is not returned as if it were the
whole report. The diagnostic's next action tells the agent to narrow the window
or use the campaign grain so the provider returns one response. This is
deliberate: pagination the API does not document is not invented. Bounded
single-response reporting is a narrower development capability than the full
release requirement.

## Read surfaces and retention

`REDDIT_ADS_READ_SURFACES` in `src/adhelm/providers/reddit_ads.py` is the one
declaration of what the fixed read returns, and the provider audit derives its
coverage from it. Implemented: account identity and timezone, campaign, ad-group
and ad inventory with parents and declared status, the campaign objective, and
the campaign- and ad-grain daily report as a single response.
`REPORT_CONTINUATION` is `NOT_DOCUMENTED`. Account status, targeting, bids and
budgets, schedules, placement, ad content, ad review state, destinations and UTM
values, conversion tracking and native conversion metrics are
`NOT_IN_REVIEWED_FIXED_READ`: this build does not retain them, which is not a
claim that Reddit documents no such field.

The adapter accepts a closed set of response fields so that a response carrying a
name or business identifier does not fail, but retains only identity, parents,
the declared status (the effective status when returned, otherwise the
configured one, otherwise `NOT_AVAILABLE_FROM_PROVIDER`), the objective, and the
verified account currency. A name never reaches a snapshot. Any field outside the
accepted set still fails the read closed rather than being ignored, which means a
live response with an additional field will fail until that field is reviewed.

`ACTIVE` and `PAUSED` are the only status values this build reviewed. They were
not re-fetched when the audit was written; any other value is surfaced as
unreviewed, never interpreted.

## Quota, denial and repeats

The shared read transport retries `429`, `502`, `503` and `504` up to its bound,
honoring a bounded `Retry-After`, and then surfaces the final status and hint to
the caller unchanged. A `401` or `403` is `PROVIDER_ACCESS_REQUIRED` with the
status attached. The adapter replaces only the next action: a `401` names
`auth reddit-token refresh` because an access token lasts one hour, and a `403`
says a refresh will not change a permission refusal. Reddit's rate-limit
headers and quota model are not documented in the reviewed material and are not
read. A report that repeats a resource and day is refused, because one fixed
breakdown yields one row per resource and day and a repeat would double-count
delivery and spend.

## Authorization

The OAuth lifecycle that produces the bearer this adapter reads is described in
the [Reddit OAuth contract](reddit-oauth-contract.md) and the
[token lifecycle](../implementation/reddit-token-lifecycle.md). The audit rules are
in [Reddit audit recommendations](../implementation/reddit-audit-recommendations.md).

Still required: live OAuth and access evidence, verified live response schemas
and complete report continuation, non-USD exponents, delivery stabilization
behavior, and all v1 write, activation, recovery, and independent-organization
evidence.

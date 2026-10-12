# OpenAI Ads clean-room reference — 2026-09-04

## Scope and evidence status

This note corrects the earlier absence-of-public-documentation conclusion in
the provider-access research.  OpenAI now publishes Ads API documentation at
[`developers.openai.com/ads`](https://developers.openai.com/ads).  The API is
documented, but this repository has **not** run a live OpenAI Ads request and
has no live provider evidence.  The local adapter is fixture-tested only.

This record describes the initial campaign-only implementation. The subsequent
[hierarchy read contract](openai-hierarchy-read-contract.md) extends the local
adapter to scoped ad-group/ad inventory and native-ad daily reporting, with
separate fixtures. The original clean-room provenance and no-live-evidence
boundary still apply.

| Capability | Provider documentation | Clean-room reference | This repository | Live verification |
| --- | --- | --- | --- | --- |
| Bearer authentication and base URL | Documented | Observed generically | Implemented for fixed reads | No |
| Account identity | `GET /ad_account` documented | Observed generically | Fixture-tested | No |
| Campaign inventory | `GET /campaigns` documented | Observed generically | Fixture-tested | No |
| Daily campaign delivery report | `GET /ad_account/insights` documented | Observed generically | Fixture-tested | No |
| Conversion reporting | Route is referenced, schema not reviewed here | Observed generically | Not implemented | No |
| Campaign creation or any mutation | Provider docs exist for some resources | Not used as implementation authority here | Not exposed | No |

“Fixture-tested” means a local `httpx.MockTransport` checked request shape and
closed parsing using synthetic, non-customer data.  It is neither a provider
acceptance test nor a customer-account probe.

## Clean-room provenance boundary

On 2026-09-04, under the user’s explicit instruction, the author read the
following named files in the separate `apprentice-advertising` workspace after
reading its `AGENTS.md` and `CLAUDE.md`:

- `src/apprentice_ads/connectors/openai_ads.py`
- `tests/test_openai_ads.py`
- `docs/2026-09-03-openai-ads-api-review.md`
- `docs/2026-09-04-openai-ads-release-review.md`
- `docs/superpowers/specs/2026-07-01-openai-ads-connector-design.md`

This repository did not copy that code, tests, fixture payloads, account
identifiers, credentials, customer evidence, raw API payloads, or Git history.
The generic facts below were independently authored and checked against the
current OpenAI documentation through Context7.  Information that was merely
seen in the named materials is not local provider evidence.

## Primary-source access contract

OpenAI’s documented base URL is `https://api.ads.openai.com/v1`; requests use
an Ads API key in the `Authorization: Bearer …` header.  A key is account
scoped, so the fixed identity request is `GET /ad_account`; it returns an
account identifier, status, time zone, currency, and review status.  The local
profile must retain the token only in the operating-system credential store and
pass it via an in-memory getter.  It must never place the token in models,
ledger records, diagnostics, or exports. [OpenAI Ads authentication](https://developers.openai.com/ads/api-reference/authentication)
[OpenAI Ads partner setup](https://developers.openai.com/ads/api-partner-setup)

`GET /campaigns` lists the account’s campaigns.  It accepts `limit` from 1 to
500, `after`/`before` cursors, and `asc`/`desc` order; the response has `data`,
`last_id`, and `has_more`.  The adapter pages forward only, with a hard limit
of ten pages and a caller-provided complete-result bound. [OpenAI Ads campaign reference](https://developers.openai.com/ads/api-reference/campaigns)

`GET /ad_account/insights` supports daily aggregation at `campaign` grain,
the canonical `fields[]` projection, a JSON-encoded `date_range` in
`time_ranges[]`, a `limit` from 1 to 2,000, and `after`/`before` cursor
pagination.  `date_range.until` is inclusive.  The implemented fixed query
requests only readable date, campaign identifier, impressions, clicks, and
spend.  It does not accept user-selected fields, filters, segments, sort
expressions, ad-level reporting, or raw provider query fragments. [OpenAI Ads insights reference](https://developers.openai.com/ads/api-reference/insights)

The documented insights examples express USD spend in major units.  The
adapter converts only exact two-decimal-or-fewer USD values to local minor
units, rejecting unsupported precision rather than rounding money.  It rejects
non-USD reporting before egress until a reviewed ISO currency-exponent map is
added.  It retains the account’s documented time zone and currency in the
report envelope.  The provider documentation reviewed here does not give the
adapter enough basis to claim sampling state, freshness, attribution window, or
conversion value; those remain null or are omitted rather than invented.

## Implementation constraints

- Allow egress only to `api.ads.openai.com` over HTTPS, with redirects
  prohibited by `SafeHttpClient`.
- Fetch `GET /ad_account` before a read run and fail closed if the configured
  local account reference or time zone differs.
- Parse only closed account, campaign, and campaign-insight response shapes in
  memory; never persist raw responses or response names, URLs, descriptions,
  creative text, targeting, or arbitrary provider extensions.
- Treat daily campaign rows as campaign grain and use the provider-readable
  date solely as the report dimension.  Do not attach a creative version or
  fan campaign metrics into ads, creatives, renditions, or components.
- Keep account inventory and reporting bounded to ten pages.  Fail with an
  explicit result-limit or pagination diagnostic before emitting a partial
  report.
- Do not call `POST /conversions/insights` yet.  It is a read operation but
  needs a separately reviewed request/response schema and attribution contract
  before it can enter the normalized conversion ledger.
- Do not expose campaign creation, budget changes, pause/activation, audience
  upload, pixel/conversion setup, or any other write.  A future mutation needs
  the repository’s full plan, authorization digest, re-read, drift, apply,
  verify, and recovery contract.

## Unknowns that block a stronger claim

The following still require a bounded, authorized live feasibility run against
a non-customer test partner before an advertised live capability claim:

1. The exact scopes, app review, and account-approval conditions for the key
   profile.
2. Whether documented daily campaign insights preserve the selected account
   time zone consistently at pagination boundaries.
3. Delivery latency, backfill behavior, sampling/thresholding signals, and
   attribution semantics for the account’s reporting surface.
4. The exact conversion-insights schema, its required entity scope, and its
   conversion counting/value semantics.
5. The exact create/paused-draft format and the supported verification/recovery
   endpoints for a write-capable profile.

No status in this note claims that the adapter was live verified.

## Advertiser API overview review — fetched 2026-09-09

Source: [Advertiser API overview](https://developers.openai.com/ads/api-overview.md),
fetched 2026-09-09. Source: [Authentication reference](https://developers.openai.com/ads/api-reference/authentication),
fetched 2026-09-09. Source: [Ads documentation index](https://developers.openai.com/ads/llms.txt),
fetched 2026-09-09.

This addition records only what those pages state. It promotes no operation
group and adds no live evidence; this product has still never sent a live
OpenAI Ads request.

- **Quota.** The overview states that the Advertiser API enforces limits by
  both ad account and IP address, at 600 requests per minute per endpoint and
  1,200 requests per minute overall, and that bulk job creation has a separate
  limit of 10 requests per 10 seconds for each ad account. This build sends no
  bulk request. The two rate numbers are pinned as
  `OPENAI_ADS_ENDPOINT_REQUESTS_PER_MINUTE` and
  `OPENAI_ADS_ACCOUNT_REQUESTS_PER_MINUTE` and are disclosed in a quota
  diagnostic; the adapter does not meter or pace requests against them.
- **Key issuance.** The overview states that an API key is issued in the
  Settings tab of Ads Manager and passed as a bearer token on every request.
  That is the only documented key-management location.
- **Absent from every page reviewed on 2026-09-09**, and therefore recorded as
  `UNVERIFIED` wherever this repository relies on it:
  - any key expiry, rotation endpoint, or programmatic revocation route;
  - a documented revocation screen, or the effect of a revocation on in-flight
    requests;
  - the meaning of a `401`, `403`, or `404` response;
  - `429` semantics, a `Retry-After` header, or a documented backoff.

  The local classification of those statuses is ordinary HTTP behaviour, not
  documented OpenAI behaviour. It is implemented so that a refusal is never
  relabelled: a status this contract does not classify is re-raised unchanged.
- **Documentation index.** The current index lists no dedicated rate-limit,
  quota, or API-key page; the overview and the partner-setup page are the only
  getting-started resources. There is still no documented Ads sandbox or test
  account, so no non-production surface exists on which to prove any of this.

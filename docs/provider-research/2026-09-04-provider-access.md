# Provider access research

Accessed: 2026-09-04

## Evidence boundary

This is a credential-free review of first-party documentation. It is
`RESEARCH` evidence only: it does not prove an adapter, sanitized fixture,
account eligibility, live behavior, UI reconciliation, or support. Versions and
limits must be rechecked before implementation and every capability promotion.
The proposed formats are hypotheses in
[`provider-contracts.md`](../provider-contracts.md).

For every provider, a timeout after possible mutation egress is
`AMBIGUOUS_REMOTE_STATE` unless a provider idempotency mechanism or read-back
proves otherwise. Retries apply only to documented safe reads, explicit
idempotent requests, or a new plan after reconciliation.

## Advertising providers

### Google Ads

- Official sources: [release notes](https://developers.google.com/google-ads/api/docs/release-notes),
  [access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels),
  [OAuth](https://developers.google.com/google-ads/api/docs/oauth/internals),
  [quotas](https://developers.google.com/google-ads/api/docs/best-practices/quotas),
  [Search campaign setup](https://developers.google.com/google-ads/api/docs/campaigns/search-campaigns/getting-started),
  and [conversion setup](https://developers.google.com/google-ads/api/docs/conversions/getting-started).
- Version: pin API `v25` conservatively. The sunset material viewed listed
  `v25` released and `v25.1` as upcoming in August; no independent official
  release-note confirmation was recorded for `v25.1`.
- Auth/access: OAuth scope `https://www.googleapis.com/auth/adwords` plus a
  developer token from a manager account. Test/Explorer/Basic/Standard access
  determines test/production use and daily operations. Customer-owned OAuth
  clients and manager developer tokens are feasible; consent does not replace
  developer-token approval or account roles.
- Test access: non-serving Google Ads test accounts work before production
  approval. They do not prove production policy/review, delivery, or UI parity.
- Limits/reporting: access-level daily operations; mutate requests permit at
  most 10,000 operations; system/query resource throttles also apply.
  `Search` is paged and `SearchStream` streamed. `validate_only` checks many
  mutations without applying. Partial failure is endpoint-specific and every
  operation result must be classified.
- Narrow recommendation: budget -> paused Search campaign -> ad group ->
  keyword criteria -> paused RSA, then separate activation. Use complete
  resource read-back; do not assume mutation idempotency.
- Native conversion: `ConversionAction` creation and Google tag/GTM setup are
  feasible. V1 excludes click-ID/offline and enhanced-conversion uploads.
- Blockers: developer-token production access, authorized test accounts, exact
  v25 field/mutability fixtures, supported bid/goal mapping, and live/UI proof.

### Meta Ads

- Official sources: [versioning](https://developers.facebook.com/docs/marketing-api/overview/versioning/),
  [authentication](https://developers.facebook.com/docs/marketing-api/get-started/authentication/),
  [basic creation](https://developers.facebook.com/docs/marketing-api/get-started/basic-ad-creation/),
  [rate limiting](https://developers.facebook.com/docs/marketing-api/overview/rate-limiting/),
  [Insights limits](https://developers.facebook.com/docs/marketing-api/insights/best-practices),
  and [Ad Pixel](https://developers.facebook.com/docs/marketing-api/reference/ads-pixel/).
- Version: official pages observed both `v25.0` current/examples and a `v26.0`
  reference. Treat the latest deployable version as unresolved; start fixture
  work against `v25.0` only after checking the changelog and pinning a supported
  version. Marketing API versions have a short migration window.
- Auth/access: Business app and access token; `ads_read` for reports and
  `ads_management` for changes. Full production access can require app review,
  Business Verification, asset assignment, and user/system-user roles.
  Customer-owned apps are possible.
- Test access: sandbox mode exists but documentation notes important creative
  creation limitations. Development ad-account allowlists and sandbox results
  do not prove production.
- Limits/reporting: tier/account/business-use-case formulas rather than one
  fixed quota; inspect usage/throttle headers and back off. Insights supports
  cursor pagination and asynchronous report jobs; jobs can time out and polling
  consumes quota. No general campaign-mutation idempotency key was verified.
- Narrow recommendation: paused `OUTCOME_TRAFFIC` campaign -> paused ad set ->
  uploaded image/ad creative -> paused single-image ad; explicit placements,
  targeting, URL, CTA, and budget.
- Native conversion: Pixel and custom-conversion resource APIs exist. Regulated
  v1 may use only a reviewed browser standard event with automatic advanced
  matching disabled; server Conversions API/person matching is excluded.
- Blockers: definitive current version, app/business review, permitted customer
  app distribution, sandbox creative gaps, exact objective/optimization/placement
  combinations, automatic collection behavior, and live/UI proof.

### LinkedIn Ads

- Official sources: [August 2026 changes](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/recent-changes?view=li-lms-2026-08),
  [access tiers](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access?view=li-lms-2026-08),
  [OAuth testing](https://learn.microsoft.com/en-us/linkedin/marketing/authentication/lms-generate-an-access-token?view=li-lms-2026-02),
  [campaign objectives](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/campaign-objectives?view=li-lms-2026-08),
  [creatives](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-creatives?view=li-lms-2026-08),
  and [cursor pagination](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/cursor-based-pagination?view=li-lms-2026-06).
- Version: `Linkedin-Version: 202608` with
  `X-Restli-Protocol-Version: 2.0.0`.
- Auth/access: three-legged OAuth. `r_ads` reads; `rw_ads` changes; DSC image
  content also needs organization social scopes and qualifying page/ad-account
  roles. Advertising API product approval is separate from user consent.
- Test access: Development tier reads administered accounts, edits an allowlist
  of up to five accounts, and can create one test ad account; calls still use
  production infrastructure. Standard tier is separately approved.
- Limits/reporting: resource/member/app quotas are supplied in developer
  analytics rather than one portable number; 429 must honor delay. Search
  endpoints use `pageSize`/`pageToken` cursor pagination, typically max 1,000.
  Empty analytics can mean no activity or missing read access and must be
  disambiguated.
- Narrow recommendation: non-serving website-visits campaign with single-image
  DSC/inline post, explicit organization, uploaded image, CTA, and landing page;
  separate activation.
- Native conversion: conversion-rule and campaign-association APIs exist.
  Conversions API events require user identifiers and are excluded. Browser
  Insight Tag/image-pixel feasibility and typed API setup remain unproved.
- Blockers: app approval/tier, two independent organizations and page roles,
  exact non-serving DSC creation semantics, browser-only conversion resource
  flow, quotas, reporting/UI parity, and all live lifecycle evidence.

### TikTok Ads

- Official sources: [Marketing API](https://business-api.tiktok.com/portal/docs/marketing-api/v1.3),
  [sandbox accounts](https://business-api.tiktok.com/portal/docs/sandbox-accounts/v1.3),
  [rate limits](https://business-api.tiktok.com/portal/docs/rate-limits/v1.3),
  [manual ads](https://business-api.tiktok.com/portal/docs/create-ads/v1.3),
  and [web event setup](https://business-api.tiktok.com/portal/docs/setup-guide-for-web/v1.3).
- Version: the public portal paths reviewed use API `v1.3`; confirm endpoint
  lifecycle and current fields immediately before fixtures.
- Auth/access: the documented API product is **Marketing API**. Its documentation
  groups relevant endpoints under **Campaign Management**, **Creative
  Management**, **Reporting**, and **Ad Measurement**. App endpoint permissions
  are selected and approved in **My Apps -> App Detail -> Authorization**,
  followed by advertiser authorization and an access token. No stable public
  OAuth scope strings for these endpoint permissions were verified in the
  first-party pages reviewed, so implementation must discover and record the
  exact My Apps permission labels rather than inventing scope names.
  Customer-owned apps appear feasible subject to app and advertiser approval.
- Test access: one sandbox ad account per app; production and sandbox have
  distinct base URLs. Sandbox supports campaign/ad-group/ad create/read/update,
  media upload, and mock reporting. Sandbox limits are 1 QPS, 30 QPM, and 1,000
  QPD per endpoint.
- Limits/reporting: production uses app tiers and endpoint-specific QPS/QPM/QPD;
  Basic global is documented as 10/600/864,000 and `/ad/create/` as
  5/150/86,400. Reporting/pagination and asynchronous review must be
  fixture-verified. Request success does not prove serving or review.
- Narrow recommendation: disabled/paused `TRAFFIC` campaign -> ad group ->
  customer video upload/identity -> manual `SINGLE_VIDEO` ad.
- Native conversion: Pixel management endpoints and browser test tooling exist.
  Events API 2.0 is allowlist-only and includes person-level matching modes, so
  it is excluded; only a reviewed browser Pixel/event may qualify.
- Blockers: app permissions/production approval, current v1.3 contract, exact
  non-serving hierarchy, identity/placement constraints, safe Pixel template,
  processed receipt/reporting, and live/UI evidence.

### Reddit Ads

- Official sources: [Ads API overview](https://business.reddithelp.com/s/article/Reddit-Ads-API),
  [developer authentication](https://business.reddithelp.com/s/article/authenticate-your-developer-application),
  and Reddit's official [Ads API v3 Postman workspace](https://www.postman.com/reddit-ads-api/reddit-ads-api-v3/overview).
- Version: Ads API `v3`, base `https://ads-api.reddit.com/api/v3`.
- Auth/access: OAuth 2 authorization-code flow with refresh token; request
  `adsread` separately from `adsedit`. Reddit states the API is open to
  developers, while partner/support access and advertiser roles still apply.
  Customer-owned developer apps are feasible.
- Test access: no general ad-delivery sandbox was documented. Use a disposable
  authorized live account and paused-first minimal-spend plan.
- Limits/reporting: list/report endpoints are paginated. Quotas are pooled by
  endpoint group and returned in `RateLimit-Policy`/`RateLimit`; documented
  campaign-management read is 400 requests/60 seconds. 429 supports delayed
  retry for reads. 5xx or transport loss on a write requires read-back.
- Narrow recommendation: paused traffic campaign -> paused ad group ->
  customer-supplied link-post creative -> paused ad; avoid beta formats.
- Native conversion: Pixel/CAPI exists, but CAPI guidance favors match keys and
  person-level identifiers. V1 excludes that upload path; browser Pixel resource
  creation, tag behavior, receipt, and reporting require proof.
- Blockers: exact v3 create schemas and status transitions, no sandbox, app and
  account access, safe browser-only conversion setup, and live/UI proof.

### OpenAI Ads

- Official sources: [Advertiser API overview](https://developers.openai.com/ads/api-overview),
  [campaigns](https://developers.openai.com/ads/api-reference/campaigns),
  [ads](https://developers.openai.com/ads/api-reference/ads),
  [insights](https://developers.openai.com/ads/api-reference/insights),
  [Bulk API](https://developers.openai.com/ads/bulk-api), and
  [conversion setup](https://developers.openai.com/ads/api-reference/conversion-setup).
- Version: public Advertiser API `v1`, base
  `https://api.ads.openai.com/v1`.
- Auth/access: bearer API key issued in Ads Manager and scoped to exactly one ad
  account. Partner access and some account operations/features require OpenAI
  enablement. Store only the credential-store handle.
- Test access: no general sandbox documented; use an account/feature-enabled
  disposable live profile with paused-first, bounded activation.
- Limits/reporting: 600 requests/minute per endpoint and 1,200/minute overall,
  enforced by account and IP; bulk job creation is 10/10 seconds/account.
  Lists/insights use cursor pagination. Bulk supports up to 1,000 operations,
  `validate_only`, partial results, and documented idempotency keys. For direct
  writes, read back after uncertainty.
- Narrow recommendation: paused standard image/text (`chat_card`) campaign ->
  paused ad group -> file upload -> paused ad, with lifetime spend cap; separate
  activate/pause and irreversible archive.
- Native conversion: account-enabled web Pixel and event-setting APIs exist;
  pixel management and conversion bidding can return feature-gated 403/404.
  Use browser Pixel only; server event keys/events are excluded.
- Blockers: Ads account and API access, programmatic brand/conversion feature
  enablement, no sandbox, safe Pixel template, processed receipt/reporting, and
  full live/UI evidence.

## Analytics, tag management, and CRM

### GA4 Data API

- Sources: [`runReport` v1beta](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport),
  [quota](https://developers.google.com/analytics/devguides/reporting/data/v1/quotas),
  and [reporting expectations](https://developers.google.com/analytics/devguides/reporting/data/v1/reporting-data-expectations).
- Version/auth: REST `v1beta`; use OAuth
  `https://www.googleapis.com/auth/analytics.readonly` for the reporting
  profile. Property access is independently required.
- Test/limits: no equivalent provider sandbox; use a disposable GA4 property.
  Standard-property core quota is 200,000 tokens/day, 40,000/hour, 14,000 per
  project/property/hour, and 10 concurrent requests. `runReport` uses
  offset/limit, default 10,000 and max 250,000 rows/request.
- Quality/errors: return quota metadata; preserve timezone/currency, sampling,
  `subjectToThresholding`, empty reason, stale and processing states. UI/API
  filters and dates must match for reconciliation.
- Recommendation/blockers: allowlisted campaign aggregate `runReport` only.
  Blockers are OAuth/property access, fixture schemas, Realtime separation,
  threshold/pending cases, processed timing, and UI/live proof.

### GA4 Admin API

- Sources: [key events v1beta](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents),
  [v1alpha RPC surface](https://developers.google.com/analytics/devguides/config/admin/v1/rpc/google.analytics.admin.v1alpha),
  and [Admin quotas](https://developers.google.com/analytics/devguides/config/admin/v1/quotas).
- Version/auth: key events use `v1beta`; event-create/edit rules remain
  `v1alpha` and are separately capability-gated. Writes require
  `https://www.googleapis.com/auth/analytics.edit`.
- Test/limits: disposable property/stream. Quotas are 1,200 requests/minute,
  600/user/minute, 600 writes/minute, and 180 writes/user/minute.
- Semantics: Admin changes are live, not staged. Key-event name/forward-looking
  behavior, counting/default value, compensation, browser, Realtime, processed
  report, and UI are separate proof.
- Blockers: edit access, alpha rule stability, property/stream identity,
  reversible test design, and all live evidence.

### GTM Web

- Sources: [API v2](https://developers.google.com/tag-platform/tag-manager/api/v2),
  [authorization](https://developers.google.com/tag-platform/tag-manager/api/v2/authorization),
  [limits](https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas),
  [`quick_preview`](https://developers.google.com/tag-platform/tag-manager/api/v2/reference/accounts/containers/workspaces/quick_preview),
  and [publish](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions/publish).
- Version/auth: API `v2`, OAuth. Separate `tagmanager.readonly`,
  `tagmanager.edit.containers`, `tagmanager.edit.containerversions`, and
  `tagmanager.publish` profiles; do not request delete/manage-user scopes.
- Test/limits: no general sandbox; use disposable Web container/site/property.
  10,000 requests/project/day and 0.25 QPS/project; list methods use page tokens.
- Semantics: `quick_preview` creates a fake version and proves sync/compiler
  status only. Version creation can sync state; publish/restore timeouts are
  ambiguous until versions are re-read.
- Recommendation/blockers: typed allowlist only, new dedicated workspace after
  full inventory and live/latest equality. Blockers include two organizations,
  free workspace slots, complete dependency surface, browser/consent harness,
  immutable digest equality, publish/restore access, and processed GA4 proof.

### HubSpot

- Sources: [campaign guide](https://developers.hubspot.com/docs/api-reference/latest/marketing/campaigns/guide),
  [stable campaign metrics](https://developers.hubspot.com/docs/api-reference/latest/marketing/campaigns/reports/get-metrics),
  [2026-09 beta revenue report](https://developers.hubspot.com/docs/api-reference/2026-09-beta/marketing/campaigns/reports/get-revenue-report),
  and [attribution behavior](https://knowledge.hubspot.com/reports/understand-attribution-reporting).
- Version/auth: stable `2026-03` campaign metrics require
  `marketing.campaigns.read`. The `2026-09-beta` revenue endpoint is research
  only and documents alternative accepted scopes
  `marketing.campaigns.revenue.read`,
  `marketing.campaigns.revenue.lite.read`, or
  `marketing.campaigns.revenue.full.read`. Customer-owned OAuth or private-app
  access remains subject to portal permissions and product edition. None of
  these scopes adds a qualified-stage filter or source-side minimum-cell
  enforcement; exact edition/scope behavior still requires an authorized test.
- Test/limits: developer test accounts can exercise available products, but
  reporting/attribution features vary by subscription. Endpoint rate limits and
  response paging must be read from the selected app/tier and response headers.
- Feasibility: stable metrics provide sessions, influenced contacts, and new
  contacts only. Beta revenue provides aggregate deal/revenue amounts by
  campaign/model/window, but no qualified-stage filter and no source-side
  minimum-cell threshold.
- Exact blocker: the required qualified outcome plus pipeline/revenue aggregate
  contract cannot be met safely. CRM deal/contact search or local aggregation
  is prohibited. Status remains `BLOCKED` pending provider capability or an
  explicit product-owner scope decision.

### Salesforce

- Sources: [REST OAuth applications](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_oauth_and_connected_apps.htm),
  [REST query](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_query.htm),
  [`GROUP BY` considerations](https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_group_by_considerations.htm),
  and [`HAVING`](https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_having.htm).
- Version/auth: pin the current supported REST version during implementation.
  Use customer-owned OAuth External Client App; new Connected App creation is
  restricted from Spring '26. Request `api` and refresh/offline access, not
  `full`, under a read-only permission set and expected org identity.
- Test/limits: Salesforce sandboxes are available by customer edition. REST
  limits are org-specific and must be read from limit headers. Synchronous query
  batches are at most 2,000 rows; `GROUP BY` results cannot use a query locator.
- Recommendation: direct REST only, fixed reviewed aggregate SOQL with declared
  fields/model/stages/window/currency/join, `GROUP BY`, and `HAVING` minimum-cell
  suppression. Compile to fewer than 2,000 groups; bounded splits may not enable
  differencing. No generic SOQL, describe, metadata discovery, queryAll, or Bulk
  record export.
- Blockers: customer-declared schema and influence model, external client app
  and read-only permission set, proof that fixed joins/grouping are valid,
  group bound/suppression, org limits, live aggregate/UI reconciliation, and
  revocation tests.

## Scope guard

All six ad providers, GA4, GTM, HubSpot, Salesforce, and native conversion
targets remain v1 scope at their truthful evidence status. Post-v1 website
crawling, CRM metadata discovery, customer/vendor-hosted MCP roadmaps,
person-level uploads, arbitrary JavaScript, and Apple advertising receive no
implementation or completion requirement from this research.

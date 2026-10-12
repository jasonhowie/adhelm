# Phase 0 provider contracts

Last updated: 2026-09-04

## Status and interpretation

These narrow mappings are implementation hypotheses for the common lifecycle in
[`lifecycle-contract.md`](lifecycle-contract.md). They lock initial engineering
scope without narrowing the six-provider release contract. None is implemented,
fixture-verified, live-verified, or publicly supported. Fixture and live proof
may require a documented revision; unsupported extras remain explicit.

Every hierarchy is created non-serving, with complete child resources, before a
separate spend-explicit activation plan. All writes use exact authorization,
state re-read, drift rejection, pre-egress `APPLY_STARTED`, read-back,
verification, and reconciliation. Destructive archive is never the default.

## Advertising campaign formats

| Provider | Narrow v1 format and hierarchy | Initial objective/bid boundary | Create and maintain boundary |
| --- | --- | --- | --- |
| Google Ads | Search campaign: dedicated budget -> Search campaign -> ad group -> keyword criteria -> responsive search ad (RSA) | website traffic/lead-intent Search; manual CPC or one fixture/live-proved non-automated strategy; no Display/PMax/Shopping/Video | create campaign and ad paused; exact RSA components/pinning and final URL; maintain status, budget, bid, schedule, keyword/negative criteria, destination, and creative replacement where API mutability permits |
| Meta Ads | traffic campaign -> ad set -> uploaded single image/ad creative -> ad | `OUTCOME_TRAFFIC`, link click or landing-page-view optimization proved for selected placements; no Advantage+ or dynamic creative | campaign/ad set/ad paused; explicit placements and targeting, image hash, copy, CTA, destination; maintain supported status/budget/bid/schedule/targeting/destination and replace immutable creative |
| LinkedIn Ads | website-visits `SPONSORED_UPDATES` single-image Direct Sponsored Content (DSC): campaign group where required -> campaign -> uploaded image -> inline post/creative | `WEBSITE_VISIT`, `MAX_CLICK` or documented manual CPC; no lead-gen form or message ad | create with non-serving campaign/creative intent; bind organization/page roles, image, copy, CTA, landing page; maintain supported status/budget/bid/schedule/targeting and replace content when immutable |
| TikTok Ads | traffic campaign -> ad group -> uploaded customer video/identity -> manual single-video ad | `TRAFFIC`, website promotion, one supported click/landing-page optimization and bid mode; no catalog, Smart+, Spark, lead form, or automated creative | campaign/ad group/ad disabled or paused before serving; explicit placement, identity, video, text, CTA, URL; maintain supported status/budget/bid/schedule/targeting and replace creative as required |
| Reddit Ads | traffic campaign -> ad group -> customer-supplied link-post creative -> ad | traffic/link-click boundary; no catalog, free-form audience upload, or beta format | create every serving object paused/draft; explicit communities/interests/geo/device only when allowed; maintain status/budget/bid/schedule/targeting/destination and safe creative replacement |
| OpenAI Ads | standard image/text ad, mapped to provider `chat_card`: campaign -> ad group -> uploaded image file -> ad | impression or click bidding first; conversion bidding separately capability-gated | campaign/ad group/ad created `paused`; title, body, image file, target URL, schedule/location/context and lifetime spend cap; maintain supported fields, pause/activate, and treat archive as irreversible |

The compiler rejects requests outside these formats before egress. It does not
silently coerce objectives, placements, bid strategies, audiences, media, or
conversion semantics.

## Read, report, and creative boundary

Each adapter inventories every object needed to explain the chosen hierarchy
and paginates to completion. Reports preserve provider timezone, currency,
attribution, freshness, API version, quota, review, and asynchronous status.
Only allowlisted aggregates are normalized.

Creative capture records exact provider copy, CTA, destination, component
order/pinning, customer asset/rendition digests, local palette/text/visual
features, and effective-dated deployment. RSA or dynamic/component performance
is reported only at provider-exposed grain; ad-level metrics are never fanned
out to components or renditions.

## Measurement surfaces

| Surface | Locked v1 boundary | Separate capability gates |
| --- | --- | --- |
| GA4 Data API | `v1beta` `runReport`, read-only `analytics.readonly`, allowlisted campaign/source/medium/date and configured key-event aggregates | Realtime is separately queried; thresholded/sampled/pending/missing states and UI reconciliation remain required |
| GA4 Admin key events | `v1beta` key-event list/get/create/patch/delete with separate `analytics.edit` profile | live write, counting/default value, prospective behavior, browser, Realtime, processed report, compensation |
| GA4 Admin event rules | `v1alpha` event-create and event-edit rule operations only | alpha maturity is explicit; each operation can remain unavailable even when v1beta key events work |
| GTM Web | API v2 typed allowlist for inventory, graph, Google tag, allowlisted GA4/native tags, low-cardinality variables, and custom-event/page/click/form-state triggers | read, container edit, version, and publish scopes; dedicated workspace; live/latest equality; `quick_preview`; browser proof; immutable digest; publish and restore |

The GTM allowlist excludes arbitrary custom HTML/JavaScript creation. Existing
custom code is local-inspection-only. A missing safe signal produces
`NEEDS_SITE_INSTRUMENTATION`; no crawling, DOM text, or entered value fallback.

## Native conversion boundary

Each advertising provider must prove one safe mapping across resource inventory
and setup, campaign/goal association, reviewed browser tag/event, native
receipt, and processed aggregate reporting. A GA4 key event is not that proof.

- Google Ads: website `ConversionAction`, campaign-goal relationship where
  applicable, and reviewed Google tag/GTM template; enhanced/offline uploads are
  excluded.
- Meta: Pixel plus allowlisted standard browser event/custom conversion with
  automatic advanced matching disabled; Conversions API/person matching is
  excluded.
- LinkedIn: Insight Tag/image-pixel browser conversion rule only if its exact
  API/template and data behavior pass regulated review; Conversions API events
  require person identifiers and are excluded.
- TikTok: Pixel and reviewed browser event only; Events API/offline/CRM events
  and automatic advanced matching are excluded.
- Reddit: existing or safely configured Reddit Pixel and browser event/tag only;
  Conversions API match-key uploads are excluded.
- OpenAI Ads: account-enabled Pixel and standard event setting, reviewed
  JavaScript Pixel/tag, and optional campaign association; server Conversions
  API is excluded.

If resource creation, browser-only receipt, association, or processed reporting
is unavailable, that dimension remains blocked; a person-level fallback is
prohibited.

## CRM aggregate boundary

Salesforce uses direct version-pinned REST with fixed reviewed aggregate SOQL
`GROUP BY` and `HAVING`. The customer declares object/field mappings,
qualification/won stages, influence model, window, currency, campaign join, and
minimum-cell threshold. There is no describe/metadata discovery or generic
SOQL. Because grouped REST results cannot use a query locator, compilation must
prove fewer than 2,000 groups and split by predeclared bounded windows/dimensions
without differencing around suppressed cells.

HubSpot is `BLOCKED` for the required v1 baseline. Stable `2026-03` campaign
metrics expose sessions, influenced contacts, and new contacts, not the required
qualified outcomes plus pipeline/revenue. `2026-09-beta` adds a campaign revenue
aggregate but exposes neither a qualified-stage filter nor a source-side
minimum-cell threshold. Record-level deal/contact retrieval is prohibited.
Resolution requires a provider capability that satisfies the fixed aggregate
privacy contract or an explicit product-owner contract decision; traffic/contact
metrics do not substitute.

## Explicit exclusions

Website crawling, CRM metadata discovery, person-level uploads, enhanced
identifiers, server-side GTM, automatic CMP installation, arbitrary JavaScript,
hosted/customer-cloud MCP implementation, and Apple advertising remain outside
v1. No external CLI or MCP server is installed, authenticated, or invoked as a
fallback.

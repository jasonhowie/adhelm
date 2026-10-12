# OpenAI Ads hierarchy and AD-grain read contract

Research date: 2026-09-04. This contract describes fixture-tested, read-only
local parsing. It does not establish production credentials, live API
capability, write capability, or delivery verification.

## Primary sources

- [Ad groups API](https://developers.openai.com/ads/api-reference/ad-groups)
  documents `GET /ad_groups`, with required `campaign_id`, `limit` from 1 to
  500, `after`/`before` cursors, and `asc`/`desc` order. Its list response
  documents group ID, status, and bidding configuration including billing event
  type and maximum bid micros.
- [Ads API](https://developers.openai.com/ads/api-reference/ads) documents
  `GET /ads`, with required `ad_group_id` and the same bounded pagination
  parameters. Its list response documents ad ID, status, review status, and a
  creative object.
- [Insights API](https://developers.openai.com/ads/api-reference/insights)
  documents account insights with `aggregation_level=ad`, daily
  `time_granularity`, IANA time zones in a date range, and a maximum `limit` of
  2,000. It documents `ad.id`, `campaign.id`, and the generalized AD
  `impressions`, `clicks`, and `spend` metrics, and shows daily flattened
  `ad_id`, `campaign_id`, and `readable_time` response values.

## Implemented local contract

`OpenAIAdsClient.list_ad_groups(query, identity, campaign)` accepts an exact
already-discovered campaign carrying the discovered account reference. It
requests only `GET /v1/ad_groups` with the provider campaign client ID and
returns a typed group with account and campaign references, group reference,
status, billing event type, and maximum bid micros.

`OpenAIAdsClient.list_ads(query, identity, ad_group)` accepts an exact
already-discovered group and requests only `GET /v1/ads` with its client ID. It
returns typed account, campaign, group, and ad references plus ad status, review
status, and creative type. It deliberately discards ad name, copy, file ID,
and image and destination URLs. `list_hierarchy` reads the whole discovered
campaign-to-group-to-ad hierarchy under one caller-selected global resource
limit.

Native campaign lifetime-budget and ad-group maximum-bid micros are retained
as positive decimal strings, including values above JSON's exact binary-float
integer range. A signed 64-bit maximum is a local parser bound, not a claimed
provider limit. Ledger snapshots retain these provider-namespaced amounts;
shared minor-unit budget/bid fields are populated only for USD values exactly
divisible by 10,000. A subminor value is never rounded down. Missing amounts,
unverified non-USD conversion, and subminor amounts have distinct quality
statuses. Append and retained-ledger validation reject inconsistent minor-unit
projections and use of these OpenAI fields by another provider.

Account and parent client IDs must reproduce their canonical local references
before a child request. A caller-supplied parent with a forged reference or a
different account fails before child HTTP. Canonical inventory persistence also
validates closure of each campaign/group/ad relationship.

All lists use provider cursor pagination, a caller record cap, a maximum page
size of 500, and a hard ten-page local cap per request. A result that would
exceed either cap fails before returning a partial inventory. Unknown response
fields and unreviewed creative shapes fail closed. Raw response bodies stay in
process memory only.

`OpenAIAdsClient.run_ad_report(query, identity)` requests the documented daily
AD aggregation from `GET /v1/ad_account/insights`. It returns only date,
campaign and ad references, impressions, clicks, and USD spend converted
exactly to minor units. The report requires the discovered account identity and
exact IANA timezone. USD is the only currently supported spend currency;
another discovered currency fails before insights egress. Decimal JSON values
are parsed as `Decimal` before the exact USD conversion.

The documented AD aggregate response proves campaign and ad IDs, but does not
prove an ad-group parent or a content-addressed creative version for a row.
The AD rows therefore leave `parent_resource_ref` and `creative_version_id`
unset and carry `CREATIVE_NOT_EXPOSED` plus `NOT_AVAILABLE_FROM_PROVIDER`.
They do not infer either relationship from separately read inventory.
No attribution window, freshness timestamp, or provider join is fabricated;
those normalized fields remain explicitly unavailable.

## Boundaries and follow-up evidence

The adapter has fixture-only tests. It does not send live requests in tests,
retain raw payloads, upload assets, create entities, change spend, pause,
activate, or publish. Production promotion needs separately authorized
provider-account feasibility evidence and a review of response variants beyond
the closed fields above. Product-template or other creative response variants
with unreviewed fields fail closed rather than being silently reduced.

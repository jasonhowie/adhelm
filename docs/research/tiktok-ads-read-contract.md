# TikTok Ads fixed read contract

Reviewed on 2026-09-04. The adapter pins TikTok for Business Marketing API
`v1.3` and sends only HTTPS `GET` requests to the official documented host
`business-api.tiktok.com`. TikTok's interactive reference currently requires
JavaScript, so this review records the official search-indexed API reference
and limits implementation to the fields shown there.

## Primary evidence

- [Get ad account details](https://ads.tiktok.com/gateway/docs/index?doc_id=1739593083610113&language=ENGLISH)
  documents `GET /open_api/v1.3/advertiser/info/`, the `Access-Token` header,
  string advertiser IDs, and the account `advertiser_id`, `currency`,
  `timezone`, and `status` fields.
- [Marketing API v1.3 endpoint list](https://ads.tiktok.com/gateway/docs/index?doc_id=1735713875563521&language=ENGLISH)
  identifies v1.3 as current and documents synchronous
  `GET /open_api/v1.3/report/integrated/get/` for aggregate reporting.
- [Synchronous reporting migration example](https://ads.tiktok.com/gateway/docs/index?doc_id=1738864778664961&language=ENGLISH)
  shows `service_type=AUCTION`, `report_type=BASIC`, `data_level=AUCTION_AD`,
  JSON-array dimensions/metrics, and the `{code,message,request_id,data}`
  response envelope with `data.list` and `page_info`.
- [Synchronous reporting](https://business-api.tiktok.com/portal/docs/run-a-synchronous-report/v1.3)
  establishes the 30-day daily-query limit.
- [Official synchronous-report request](https://www.postman.com/tiktok/tiktok-api-for-business/request/7d5ufux/run-a-synchronous-report)
  and [report filters](https://business-api.tiktok.com/gateway/docs/index?doc_id=1751625296192514)
  establish the fixed `IN` filter object and serialized value list.
- [Ad-group fields](https://business-api.tiktok.com/gateway/docs/index?doc_id=1739499616346114)
  document numeric budget/bid fields. JSON decimal lexemes are parsed directly
  into `Decimal`, avoiding a binary floating-point intermediate.

## Fixed reads

Identity requests exactly `advertiser_id`, `currency`, `timezone`, and
`status`; the returned advertiser ID must equal the configured one. Inventory
uses exactly these paginated v1.3 reads:

| Endpoint | Fields retained |
| --- | --- |
| `/campaign/get/` | campaign ID, operation status, objective, budget mode, budget |
| `/adgroup/get/` | ad group/campaign IDs, operation status, budget mode/budget, bid type/price |
| `/ad/get/` | ad/ad group/campaign IDs and operation status |

The client retains the provider budget mode alongside exact decimal money and
the verified account currency. Parent references are returned by the typed
inventory models. It does not request names, targeting, audiences, creative
payloads, lead data, destination URLs, or arbitrary fields.

Daily reporting is the fixed synchronous endpoint, with `AUCTION_CAMPAIGN` or
`AUCTION_AD` data levels, daily date plus matching resource IDs as dimensions,
and only impressions, clicks, and spend. Daily reports use one date dimension
and one matching resource ID; ad reports do not infer campaign/ad-group joins.
The maximum daily date range is 30 inclusive calendar days,
rows and pages are bounded, and a future end date fails before egress. TikTok's
reviewed response does not prove a freshness time or an attribution setting;
the report envelope records both as unavailable. Ad-grain results carry
`CREATIVE_NOT_EXPOSED` and `COMBINATION_UNKNOWN`: they must never be treated
as rendition or component performance.

The report always selects `STATUS_ALL` through the documented fixed campaign
or ad status filter, so default exclusion of deleted entities is not silently
presented as a complete historical read. The response date parser currently
accepts only `YYYY-MM-DD`. The reviewed official material does not establish
the exact response token format; this remains a fail-closed assumption requiring
live evidence, not a verified provider guarantee.

## Fail-closed behavior

The adapter accepts only the reviewed response shape and fields, checks every
page counter, requires a stable total and fixed page size across numbered pages,
rejects duplicate resource/day rows, accepts no redirect or host change, and uses no next-URL from the
provider. Unknown nested fields, duplicate/incomplete pages, unreviewed
currency, non-integral minor-unit precision,
identity mismatch, and result bounds all fail before persistence. Raw API
responses are never persisted.

There is no write, creative retrieval, conversion, attribution configuration,
or generic query capability in this adapter. Access still requires a
customer-configured local read token and the provider's applicable permissions.

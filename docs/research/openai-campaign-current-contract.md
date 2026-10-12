# OpenAI Ads current-campaign read contract

Reviewed: 2026-09-05. This is fixed read-only preparation evidence. It does
not promote a pause, recovery, or execution capability.

The official [OpenAI Ads Campaigns reference](https://developers.openai.com/ads/api-reference/campaigns)
documents account campaign listing and single-campaign retrieval at
`GET /v1/campaigns/{campaign_id}`. `OpenAIAdsClient.get_campaign` uses that
fixed route only after the same client session has discovered the account and
completed a bounded campaign listing containing the requested native ID.

The response is parsed through the same closed campaign normalizer as the
bounded list. The retained allowlist is campaign ID, optional returned account
ID when supplied, status, bidding type, native lifetime-spend-limit micros, and
optional start/end schedule. It rejects unknown fields, malformed IDs, unknown
bidding types, malformed native budget values, an inconsistent optional account
ID, and a resource ID differing from the requested discovered campaign. An
absent optional budget, bidding type, start, or end remains absent; it does not
mean zero, a default bid, or an unbounded schedule.

All public read queries are rebuilt through their strict model boundary before
token access. A copied or constructed query cannot enlarge a page, record, or
report bound, and an invalid expected account or campaign ID fails before its
corresponding request. The native micros and Unix timestamp fields accept JSON
integers only: booleans, strings, and integral-looking decimal values are not
silently coerced.

Starting a campaign inventory refresh clears that account's old campaign and
dependent ad-group cache before the first request. Starting a current campaign
GET clears that campaign and its dependent groups before transport. Therefore a
transport, schema, or identity failure cannot leave an older parent eligible
for later child reads. A successful current GET stores only its newly parsed,
account-bound parent.

The route is a bounded GET, so the shared transport may use its existing
bounded retry behavior for retryable read failures. The read produces no
revision, ETag, conditional-write token, idempotency assurance, actor
attribution, or provider-state settlement conclusion. It performs no mutation,
does not use a pause route, and does not establish current identity/profile
evidence for a safety-kernel execution adapter. Raw response values remain
transient; prohibited response data fails with a sanitized provider-response
error and is not copied into the returned inventory model.

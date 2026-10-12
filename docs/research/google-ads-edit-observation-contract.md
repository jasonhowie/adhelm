# Google Ads EDIT observation contract

Checked on 2026-09-06. This note records the primary-source facts used by the
private M54 observation adapter. It does not claim a live conformance test,
provider permission, campaign-mutation support, or execution authority.

## Confirmed v25 read wire contract

The [v25 `GoogleAdsService.Search` REST reference](https://developers.google.com/google-ads/api/reference/rpc/v25/GoogleAdsService/Search?authuser=5&transport=rest)
defines `POST https://googleads.googleapis.com/v25/customers/{customerId}/googleAds:search`.
The request accepts `query` and `searchSettings`; requesting a result count is
the nested JSON value
`"searchSettings":{"returnTotalResultsCount":true}`, not a top-level
`returnTotalResultsCount` field. The response may include documented service
metadata such as `fieldMask`, `nextPageToken`, `totalResultsCount`,
`queryResourceConsumption`, `summaryRow`, and `metricAttributes` in addition
to `results`.

M54 sends exactly two fixed GAQL queries through that route:

1. `customer.id`, `customer.resource_name`, `customer.currency_code`,
   `customer.time_zone`, and `customer.status`.
2. One selected `campaign.id`, `campaign.resource_name`, and `campaign.status`.

The adapter requests the count and requires exactly one result, count `"1"`,
no pagination token, no summary row, and no metric attributes. It allows only
the documented envelope metadata above, and it retains none of it. It requires
the selected resource names because [Customer](https://developers.google.com/google-ads/api/fields/v25/customer?authuser=2)
documents `customers/{customer_id}` and [Campaign](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign?authuser=2)
documents `customers/{customer_id}/campaigns/{campaign_id}`.

The customer status field documents `CANCELED`, `CLOSED`, `ENABLED`,
`SUSPENDED`, `UNKNOWN`, and `UNSPECIFIED`. M54 keeps the first four as closed
observations and rejects the latter two. `CLOSED` is not a local eligibility
failure: the [customer status reference](https://developers.google.com/google-ads/api/reference/rpc/v25/CustomerStatusEnum.CustomerStatus)
explicitly includes test accounts under `CLOSED`. This slice neither evaluates
access eligibility nor provider mutation permission.
The campaign subset keeps only `ENABLED`, `PAUSED`, and `REMOVED`; it rejects
`UNKNOWN`, `UNSPECIFIED`, and any other value. A campaign ID is an ASCII,
positive signed-64-bit decimal scalar, and its returned resource name must bind
the configured customer and requested ID exactly.

## Authentication and local association

[Google's authorization/header documentation](https://developers.google.com/google-ads/api/rest/auth?hl=en)
required the `https://www.googleapis.com/auth/adwords` OAuth scope and, until Google
retired developer tokens on 2026-09-09, a `developer-token` header on every API call;
the header is now ignored and AdHelm never sends it. A manager-to-client call also uses
`login-customer-id`; the same source says it is omitted for direct customer
access. M54 therefore binds the configured target customer, optional manager
login customer, installed client, derived OIDC principal, the adwords scope,
OAuth bearer, and finite bearer expiry into a private
per-session HMAC marker.

Google Ads uses the same adwords scope for local READ and EDIT profiles. The
purpose separation in this repository is a local contract boundary; it does
not assert provider-enforced OAuth least privilege. The protected OAuth bundle
is read under configuration-then-credential lock ordering. The session exposes neither raw
value nor a generic credential resolver.

## Deliberate limits and uncertainty

This slice makes read-only REST calls with a clean `httpx.Client`, fixed host,
bounded 64 KiB response, disabled environment trust, no ambient auth/cookies/
hooks/redirects, and `read_attempts=1`. `Search` is a POST read, so the adapter
passes `mutation=False` but still forces one attempt; it never calls a
`campaigns:mutate` route.

The fixed observation proves only that the selected response shape bound to the
declared local target at that instant. It does not establish current account
authorization, campaign edit permission, a provider revision or CAS value,
mutation acknowledgement semantics, idempotency, effective delivery, spend,
or a live release claim. Those questions require a separately reviewed guarded
pause contract and live feasibility evidence. No customer API, OAuth endpoint,
OS credential store, or provider request was used to write this research note.

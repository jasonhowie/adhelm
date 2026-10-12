# Meta pause acknowledgement follow-up

Checked on 2026-09-06 without provider credentials or Graph API calls.

Direct retrieval of the official [Campaign reference](https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group/)
returned HTTP 429. Context7's indexed official Marketing API collection
(`/websites/developers_facebook_marketing-api`) returned an update summary with
`id` and `success`, but identified no original response snippet or version for
that example. A more specific follow-up returned examples for other operations.
Those generated summaries do not establish a closed campaign-update response
schema. The existing [tagged SDK review](meta-pause-wire-followup.md) still
supports the fixed node POST and form encoding; its generic success parser
does not establish an exact acknowledgement contract.

The confirmed-write response branch remains unevidenced. Future implementation
must retain post-egress uncertainty when an acknowledgement cannot be validated,
including when a later GET observes configured `PAUSED`. This gap does not block
local planning, historical review or independent approval-contract engineering.

A later primary-source search found Meta's public Postman
[campaign update example](https://www.postman.com/meta/facebook-marketing-api/request/mjscwl2/updating-campaign-details-l3).
It shows a campaign-node POST, Bearer authentication and `status=PAUSED` among
query parameters. Its generated example selects v20.0 and has no saved response
body. It therefore adds historical request evidence but closes neither the v26
wire contract nor the acknowledgement gap. The official Campaign reference
again returned HTTP 429; no Graph request was made.

## 2026-09-06 addendum: local subset, not provider proof

[The local acknowledgement subset](meta-pause-local-acknowledgement-subset.md)
uses the tagged SDK only to choose a fixed v26 campaign-node POST with a
form-encoded `status=PAUSED` value. A closed exact-200 / sole-real-Boolean
`success` rule plus fresh configured-status readback can be implemented and
fixture-tested without treating it as documented Graph behavior. All other
post-egress outcomes remain ambiguous and non-retryable. A versioned official
v26 response reference or approved live conformance remains necessary before a
live support claim.

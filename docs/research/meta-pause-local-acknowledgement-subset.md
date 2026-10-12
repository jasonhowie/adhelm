# Meta campaign PAUSE local acknowledgement subset

Checked 2026-09-06 without Graph credentials, a provider request, or a live
campaign. This note narrows an offline implementation rule. It does not claim a
documented Graph v26 response schema, customer eligibility, provider-side
idempotency, or live support.

## Reviewed primary-source facts

The tagged official Meta Business SDK release
[26.0.1](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.1)
sets its API version to v26.0 in
[`apiconfig.py`](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/apiconfig.py#L6-L9).
Its generated
[`Campaign.api_update`](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/adobjects/campaign.py#L523-L589)
constructs a campaign-node `POST` with endpoint `/` and accepts `status` from
the Campaign status enum, which includes `PAUSED`. The tagged
[request implementation](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/api.py#L275-L315)
passes non-GET parameters as `data`, rather than JSON or request query
parameters. The tagged default
[session implementation](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/session.py#L29-L53)
places its token in default query parameters.

Those sources establish the generated client's method, node shape and scalar
parameter pattern. They do not establish the exact Graph v26 media type,
authentication placement, request bytes, response body, or acknowledgement
semantics. The SDK's own
[generic success classifier](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/api.py#L76-L97)
is deliberately too broad for a guarded write: it can accept forms that this
product must not treat as evidence. Meta's public
[campaign-update Postman example](https://www.postman.com/meta/facebook-marketing-api/request/mjscwl2/updating-campaign-details-l3)
shows Bearer authentication and `status=PAUSED`, but it is v20.0 and has no
saved response body. It is supporting request evidence, not v26 confirmation
evidence.

## Fixed local request choice

The smallest private adapter may use the following fixed semantic request:

```text
POST https://graph.facebook.com/v26.0/{native-campaign-id}/
Authorization: Bearer <guarded subject token>
application/x-www-form-urlencoded body: status=PAUSED
```

The trailing campaign-node slash and form parameter follow the reviewed v26.0
generated SDK path and `data` pattern. The Authorization header deliberately
does **not** copy the SDK's default access-token query parameter: the existing
closed Meta campaign reader already confines the guarded subject token to that
header, and URL-token placement would weaken the product's redaction boundary.
The v20 official Postman example supports Bearer as a historical request
example, but this implementation does not claim v26 wire equivalence.

The shared HTTP utility may add only bounded form-data validation: scalar
strings, no nested values, and mutual exclusion with JSON, query parameters and
cursor bytes. It is still a general internal transport utility and accepts its
URL and headers from its internal callers. HTTPX documents that `data=` sends
form-encoded content ([HTTPX quickstart](https://www.python-httpx.org/quickstart/#sending-form-encoded-data)); that is library serialization evidence only.

The Meta adapter itself must hard-code the sole mapping `{"status": "PAUSED"}`,
`POST`, the v26 host/path, one mutation attempt, and a bounded response. It
must not accept a token, client, route, body, query, retry setting, headers, or
acknowledgement rule from a caller, and it must not become a generic Meta write
client.

## Local acknowledgement rule

After the kernel has appended `APPLY_STARTED` and the adapter has consumed its
one guarded permit, the adapter may classify a response as a *local
acknowledgement candidate* only when all of these are true:

1. The exact HTTP status is `200`.
2. Bounded JSON has unique keys and no non-finite values.
3. Its top-level value is exactly one object member, `{"success": true}`.
4. `success` is the real Boolean `true`, never an integer, string, truthy value,
   extra field, array, empty body, Campaign object, or SDK convenience result.
5. A new guarded account-bound campaign GET then proves the exact planned
   campaign has configured status `PAUSED`.

The post-write GET must retain its actual `effective_status` as a separate
allowlisted local observation alongside configured `PAUSED`. It cannot require
or predict `effective_status=PAUSED`, and it does not prove that spend or
delivery already stopped. A digest-only evidence reference is not recoverable
status history. It needs a dedicated bounded readback-observation record rather
than a `RESOURCE_SNAPSHOT`: the latter shares an inventory baseline key and its
partial configured-status state would create false inventory diffs. The record
must keep raw acknowledgement bodies and credential material out of history.

This is a deliberately stricter product-local subset than the generic SDK
classifier. It is enough to implement a closed, fixture-tested branch that can
return `CONFIRMED` to the local kernel after the independent configured-status
readback. It does not turn the response shape into a documented provider
guarantee or promote the capability beyond development/fixture evidence.

Every other result after permit consumption — a non-200 status, connection
loss, timeout, malformed or oversized body, duplicate/extra JSON member,
`success` alias, false Boolean, readback failure, drift, or local continuity
failure — remains `AMBIGUOUS_REMOTE_STATE`. A matching later `PAUSED` GET cannot
attribute the change to that POST and cannot make replay safe. No provider error
body, request body, token, or raw response may enter output, history, evidence,
or recovery text.

## What remains open

The lack of a retrieved versioned Graph v26 response reference is **not** a
reason to block the closed offline adapter or its synthetic conformance tests:
the implementation can fail every form outside the local subset. It still blocks
a live capability claim, a claim of Graph-v26 wire equivalence, and live
promotion of the local acknowledgement rule. Those require either a versioned
official v26 request/response reference or an approved, bounded live
conformance exercise with redacted evidence.

The following also remain intentionally unresolved: account/app eligibility,
provider conditional-update or idempotency semantics, effective-delivery timing,
and a complete error taxonomy. None may be inferred from the subset. The
adapter therefore retains one POST attempt, current local continuity and exact
baseline checks immediately before egress, durable pre-egress history, and
reconciliation instead of retry after ambiguity.

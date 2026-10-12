# Meta campaign-pause wire follow-up — 2026-09-06

This is a read-only follow-up to
[the initial pause-contract note](meta-ads-pause-contract.md). It records
what the reviewed **generated** Meta Business SDK v26.0.1 implementation
does, and separates that from Graph API v26 behavior that still needs an
official reference or an approved live conformance exercise. No Graph request,
credential read, SDK dependency, product code, or mutation capability was
added.

## Evidence and scope

| Source | Evidence obtained | Limit of that evidence |
| --- | --- | --- |
| Tagged [SDK configuration](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/apiconfig.py) | SDK `v26.0.1` selects Graph API `v26.0`. | Pins the generated-client review target; it does not attest a live customer integration. |
| Tagged [Campaign object](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/adobjects/campaign.py) | `api_update` is a campaign-node `POST`; its `status` parameter uses the Campaign status enum, which includes `PAUSED`. The generated configured and effective status sets are also present. | Generated code is useful interface evidence, not a versioned public HTTP specification or proof that a customer may perform the update. |
| Tagged [API transport and response code](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/api.py) | The SDK turns non-GET/DELETE parameter mappings into the `data=` argument of its session request and has a generic success classifier. | Shows this SDK's client behavior only. It does not establish Graph's required media type, exact bytes, or closed success contract. |
| Tagged [SDK session code](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/session.py) | The default session uses `https://graph.facebook.com` and installs the access token as a Requests session default parameter. | Records one generated SDK's authentication plumbing, not the Graph API's required authentication placement. |
| Tagged [SDK error parser](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/exceptions.py) | The SDK recognizes a top-level `error` object and extracts several optional fields. | It is an SDK convenience parser, not an authoritative enumeration of Graph v26 error bodies or retry safety. |
| Tagged [AdAccount object](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/adobjects/adaccount.py) | `account_status` is generated as an unsigned integer; `user_tasks` is present and the generated task labels include `ADVERTISE` and `MANAGE`. | The tag supplies neither an account-status enum nor a status-to-campaign-update eligibility rule. |

Direct retrieval of Meta's manage-campaigns and Graph-reference pages was not
available in this research environment. Context7's official-SDK material
confirms the general `api_update` pattern and SDK error surface, but did not
provide a versioned campaign-pause response contract. That retrieval gap is
not evidence that any Graph capability is absent or unsupported.

## What the tagged SDK actually constructs

For `Campaign.api_update`, the tagged generated source creates a request with
the campaign's own ID, method `POST`, and endpoint `/`. Its parameter checker
allows a scalar `status` from `Campaign.Status`; `PAUSED` is one of the listed
values. The general request builder turns that node request into a path of the
form:

```
https://graph.facebook.com/v26.0/{campaign-id}/
```

The tagged API implementation processes a non-GET/DELETE operation-specific
parameter mapping with `data=params`; it does not pass that mapping through
its `params=` or JSON-request arguments. For the narrow generated operation,
the candidate parameter is the scalar mapping `status: "PAUSED"`. Nested
values would be JSON-encoded by the SDK before it hands the mapping to the
session, but the scalar status value is not transformed by that step. The
Requests API documents a dictionary passed as `data` as form-encoded request
body data, so that is the reviewed SDK dependency's serialization behavior
([Requests request API](https://requests.readthedocs.io/en/stable/api/)).

The tagged default session sets `GRAPH` to `https://graph.facebook.com` and
adds the access token to its session-default `params` mapping. Requests
documents session `params` as query-string data attached to each request.
This explains the default SDK's likely split between an authentication query
parameter and the POST body parameter mapping; it does not prescribe a Graph
v26 authentication requirement and must not be copied into product logging or
credential handling.

This establishes a **generated-client transport pattern**: the default SDK
forms a POST to the campaign node with a form-encoded body parameter mapping
and session-level query parameters. It does not establish a Graph v26 wire
requirement such as a particular `Content-Type`, field ordering, trailing-slash
rule, authentication placement, or byte-level encoding. Those details remain
below the SDK's session implementation or outside the tagged source. A future
fixed client should therefore not claim that a reimplementation is
wire-equivalent merely because it sends `status=PAUSED`.

The same generated method lists `execution_options`, including a
`validate_only` enum value. The source does not document how Graph applies that
value to this operation. It is not evidence of a safe dry-run, conditional
update, or idempotency facility.

## Acknowledgement and error semantics

The SDK does **not** supply a sufficiently closed acknowledgement contract for
the product:

- Its generic response classifier rejects a JSON mapping containing `error`.
  For a truthy JSON value with a `success` member it uses that member; other
  truthy bodies can be accepted, and an otherwise empty HTTP 200 can also be
  accepted.
- The generated campaign operation requests an object parser, so the SDK
  façade may yield a Campaign-shaped object even though the generic transport
  classifier handled the underlying response.
- Its error class attempts to read `message`, `code`, `is_transient`,
  `error_subcode`, `type`, and nested blame data from an `error` mapping. It
  stores the raw request context and response body as part of its diagnostic
  object.

These are SDK parsing choices, not verified Graph v26 acknowledgement or error
schemas. In particular, the generic classifier is too broad for a guarded
remote write and its diagnostic object is unsuitable for product output because
it can retain request and provider-response details.

Until an official versioned reference establishes an exact acknowledgement,
the follow-on adapter must not turn a broad HTTP/SDK success classification
into `CONFIRMED`. A timeout, connection loss after dispatch, malformed body,
unrecognized response, or provider error after egress remains
`AMBIGUOUS_REMOTE_STATE`; it cannot be retried blindly. A fixed compact error
mapping must use reviewed fields only and must never surface raw response or
token-bearing request data.

## Account prerequisites: evidence and boundary

The tagged `AdAccount` class supports reading an `account_status` unsigned
integer and a `user_tasks` string list. Its generated task labels include
`ADVERTISE` and `MANAGE`. It does **not** define an `AccountStatus` enum or
express a predicate connecting any account-status value, task label, or account
field to `Campaign.api_update` eligibility.

Consequently, no source-backed rule such as “status N may pause a campaign” or
“MANAGE is sufficient for every campaign update” is available for this slice.
The future EDIT boundary may retain an exact returned account-status value as
an observation and prove that the campaign's parent account equals the
already-discovered account. It must leave authorization and any account
restriction to the fixed provider call and its later evidence; it must not
invent a local eligibility rule from an unsigned integer or task string.

The prior research's least-privilege target remains a separately inspected USER
token with `ads_management`. That scope observation, a declared customer
account, and a successful identity read still do not establish mutation
authority for a specific campaign.

## Configured pause versus effective delivery

The generated Campaign source distinguishes these finite status sets:

| Field | Generated values relevant here |
| --- | --- |
| `configured_status` / mutable `status` | `ACTIVE`, `ARCHIVED`, `DELETED`, `PAUSED` |
| `effective_status` | `ACTIVE`, `ARCHIVED`, `DELETED`, `IN_PROCESS`, `PAUSED`, `WITH_ISSUES` |

The source establishes the distinction and the vocabulary. It does not state
the transition timing or guarantee that `effective_status` becomes `PAUSED`
immediately after a configured pause. Nor does it show that a post-update
effective status proves delivery or spend has stopped. The only bounded
post-write observation to evaluate is a fresh, account-bound configured
`PAUSED` state; after an ambiguous egress it cannot attribute that state to
this attempt. The effective status remains separate evidence that must be
retained as observed, including `IN_PROCESS` or `WITH_ISSUES` when returned.

## Conditional and idempotency gap

The generated `api_update` parameter list has no visible revision, ETag,
If-Match, idempotency-key, or operation-token parameter. The generic SDK
response wrapper can expose an ETag header, but the generated campaign update
does not bind that header into a conditional update. This absence in generated
code is **not proof** that Graph v26 offers no documented conditional or
idempotency mechanism.

No such mechanism is used or inferred here. Until a reviewed official Graph
reference supplies exact semantics, the future adapter must pre-read its
closed baseline for local drift detection, append durable `APPLY_STARTED`
before egress, make at most one POST attempt, and recover by fresh read rather
than claim provider-side compare-and-swap or retry an ambiguous update.

## Evidence needed for a conservative implementation

A fixed request/confirmation path needs a reviewed source package or separately
authorized live conformance exercise for only these implementation-critical
facts:

1. The exact v26 campaign-node request contract: path form, authentication
   placement, accepted media type, and the closed `status=PAUSED` parameter
   representation.
2. The exact successful acknowledgement body and HTTP conditions that may be
   treated as a completed provider write.

A closed implementation can fail all other response forms conservatively and
record an ambiguous egress without first obtaining a complete provider-error
taxonomy. It does not need to infer a locally sufficient account-status rule;
the fixed provider call can reject an ineligible account or campaign.

## Explicit limitations that may remain UNKNOWN

The following are required to remain visible in the plan, result, and release
evidence if still unconfirmed. They are not invented approval gates for a
conservative one-shot pause implementation:

1. A documented account-status or task prerequisite for the selected account.
   Until proven, these remain observations and provider-side eligibility is
   allowed to fail closed.
2. A conditional-update or idempotency mechanism, including its scope and
   retry semantics. Until proven, the operation remains one-shot with local
   baseline drift detection and ambiguity recovery.
3. The timing and interpretation of `effective_status` after configured
   `PAUSED`. Until proven, it remains an observed delivery fact and never a
   claim that spend or delivery has stopped.
4. A detailed versioned Graph error schema. Until proven, only a closed local
   diagnostic category may be emitted; it cannot justify a retry after egress.

The future work remains an EDIT-only, fixed-operation promotion. This note
does not create a generic Graph client, provider mutation path, native approval
kind, credential route, or authorization surface.

## 2026-09-06 addendum: closed offline acknowledgement subset

[The local acknowledgement subset](meta-pause-local-acknowledgement-subset.md)
records a deliberately stricter implementation rule: exact HTTP 200, exactly
`{"success": true}` with a real Boolean, and a fresh account-bound configured
`PAUSED` readback. It corrects an inferred blocker in the wording above: the
missing official v26 response reference does not prevent a fixture-only closed
adapter that treats every other post-egress form as ambiguous and never retries.
It still prevents a Graph-v26 wire-equivalence or live-capability claim.

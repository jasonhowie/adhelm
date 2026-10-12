# OpenAI Ads paused chat-card write contract

Last reviewed: 2026-09-04

## Status and boundary

This is an **offline compilation contract**, not a claim that this repository
can create, update, activate, upload, or verify an OpenAI Ads resource. The
implementation in `adhelm.providers.openai_compiler` makes no HTTP request and
does not expose an executable write path. It compiles one deliberately narrow
native hierarchy from typed local input:

```text
paused Campaign
  -> one or more paused Ad Groups
      -> one or more paused chat-card Ads
```

The parent links in the compiled result are local logical handles. They are not
guessed `campaign_id`, `ad_group_id`, or `ad_id` values. Every result is marked
`OFFLINE_CANDIDATE_NOT_EXECUTABLE`. A later planner must resolve the local
creative and file evidence against the ledger, verify account identity, create
parents in order, bind each returned ID, re-read it, reject drift, and run
through the existing durable authorization and recovery workflow before any
network egress.

The source repository has fixture-tested reads only. There is no OpenAI Ads
write fixture, live request, account eligibility proof, UI reconciliation, or
live lifecycle evidence. Therefore no release support claim follows from this
compiler.

## Reviewed official API surface

OpenAI documents the hierarchy as Ads inside Ad Groups inside Campaigns.
Campaigns define budget and targeting; ads contain the title, description, and
image serving inputs. [OpenAI Ads quickstart](https://developers.openai.com/ads/api-quickstart)

The compiler supports only the following documented fields.

| Resource | Documented create route | Compiled fields | Narrow contract |
| --- | --- | --- | --- |
| Campaign | `POST /campaigns` | `name`, `start_time`, `end_time`, `status`, `budget.lifetime_spend_limit_micros`, `bidding_type`, `targeting.locations.include` | `status: "paused"`; finite schedule; exact lifetime cap; named numeric location IDs |
| Ad Group | `POST /ad_groups` | parent `campaign_id`, `name`, `status`, `bidding_config.billing_event_type`, `bidding_config.max_bid_micros` | local parent handle pending future result binding; `status: "paused"`; billing event derived from campaign bid type |
| Ad | `POST /ads` | parent `ad_group_id`, `name`, `status`, `creative.type`, `creative.title`, `creative.body`, `creative.target_url`, `creative.file_id` | local parent handle pending future result binding; `status: "paused"`; `creative.type: "chat_card"` only |

Campaign names must be 3–1000 characters, the lifetime budget has a documented
minimum of 1,000,000 micros, campaign state accepts `active` or `paused`, and
the documented bidding types are `impressions`, `clicks`, and `conversions`.
[Campaign API reference](https://developers.openai.com/ads/api-reference/campaigns)

Ad-group `billing_event_type` is `impression` for impression campaigns and
`click` for click or conversion campaigns; `max_bid_micros` has minimum 1.
[Ad Groups API reference](https://developers.openai.com/ads/api-reference/ad-groups)

For a `chat_card`, title is 3–50 characters, body is at most 100 characters,
and both target URL and a file ID returned by upload are required. The compiler
requires a customer-supplied opaque file reference, local evidence reference,
and matching local asset digest. It does not call that customer evidence native
file verification: ledger resolution and an account-bound readback remain
preconditions for plan promotion. [Ads API reference](https://developers.openai.com/ads/api-reference/ads)
[Files API reference](https://developers.openai.com/ads/api-reference/files)

Location targeting is explicit because omission can target every available
location. The compiler accepts only customer-declared, sorted numeric location
IDs under `targeting.locations.include`; it does not perform geo lookup.
[Campaign targeting](https://developers.openai.com/ads/campaign-targeting)

## Explicit scope exclusions and diagnostics

The compiler fails before any egress for `bidding_type: "conversions"` with
`UNSUPPORTED_CONVERSION_OPTIMIZATION`. OpenAI documents conversion bidding as
requiring an enabled account and exactly one active standard conversion event;
the fixed conversion-setting reads do not yet establish the live verification,
association, or measurement proof needed to meet those preconditions.
[Conversion-optimized campaigns](https://developers.openai.com/ads/conversion-optimized-campaigns)

Custom audiences, exclusions, audience bid multipliers, free-form context
hints, product feeds/product sets, product-ad templates, `price`, descriptions,
and all update/activation/archive paths are intentionally absent from the
strict input schema. Unknown fields fail validation. This prevents a user or
agent from treating missing support as a lossy translation.

The compiler requires declared HTTPS destination hosts and paths and uses the
shared controlled-UTM validator. Its supported chat-card format requires a
canonical `CreativeVersion`, source asset, and rendition chain. Exact title,
body, rendition, asset digest, and a deterministic redacted destination
reference must agree with the native serving input, so a destination or creative
change cannot reuse the prior creative version. Sensitive routes, credentials,
identifiers, unapproved query parameters, and person-level audience material
fail before a compiled spec exists.

## Spend, activation, content, and output rules

Every compilation is paused at all three native hierarchy levels. The campaign
lifetime input is `lifetime_budget: {minor_units, currency}`. The compiler derives
native micros exactly from that object; legacy bare micros input is rejected.
The documented minimum is 100 USD minor units, and the largest locally
representable cap is 900,719,925,474 minor units. That ceiling preserves the
canonical JSON integer domain; it is not a claim about an account's permitted
budget. The campaign lifetime micros cap remains exact in the result. This build has a reviewed
minor-unit map only for USD, with exponent 2; a mismatched caller exponent or
any other currency fails closed. That exposes the maximum lifetime spend without
rounding, implicit foreign exchange, or a caller-controlled unit conversion.

Activation is not compiled. The result records that a distinct `ACTIVATE` plan
needs a new digest and authorization and must bind the exact future account and
resources. The paused-creation digest does not authorize activation.

The complete typed input and compiled native requests are RFC-8785 digested.
Each chat-card result carries the canonical creative family/version/rendition,
asset digest, and local file-evidence reference. Its content digest covers exact
title, body, destination, opaque file ID, and the local creative chain. Default
review output exposes only content/version digests, counts, handles, paused
state, offline-candidate status, and spend exposure; it excludes exact copy,
destination URL, and provider file ID.

## Remaining proof gap

The official public references establish the local fields and hierarchy. They
do not establish this product's account access, ledger-backed file evidence,
file verification/read-back, idempotency behavior, mutation response schemas,
pending-review behavior, drift semantics, recovery route, conversion receipt,
or safe activation verification in a real customer or test-partner account.
Those gaps block a write-capable or live-supported claim. A guarded executor
must not be added until each operation has its own reviewed response contract
and fixture/live evidence.

This note was independently written from current official OpenAI documentation.
The clean-room reference remains provenance context only; no code, tests,
payloads, customer data, credentials, or history were copied from the project it
records.

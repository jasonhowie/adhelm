# OpenAI Ads pause and recovery contract research

Reviewed: 2026-09-05. Design input for a guarded adapter, not an installed
mutation capability or live evidence. No provider API request, credential or
customer data was used for this review.

The official [Campaigns reference](https://developers.openai.com/ads/api-reference/campaigns)
documents a dedicated `POST /campaigns/{campaign_id}/pause` action and says state
actions return the changed campaign. It also documents account-scoped listing
and single-resource retrieval. This supports a fixed pause operation followed by
an independent read. The public campaign example includes status, schedule,
budget and bidding fields; it does not establish a concurrency precondition or
an idempotency-key contract. A successful response alone cannot prove that all
planned state still agrees.

The [Ad Groups reference](https://developers.openai.com/ads/api-reference/ad-groups)
provides separate pause and retrieval routes. Its update guidance warns that an
immediate update response can omit product-set information, reinforcing the need
for an independent read. Create examples do not establish a complete, normative
success/error response schema for every supported hierarchy operation.

The [Ads reference](https://developers.openai.com/ads/api-reference/ads) documents
pause and retrieval, returned review status, and expiring preview creation.
Preview is therefore a separate provider action. Archive is described as
irreversible; it cannot be labeled a reversible recovery operation.

No idempotency or automatic write-retry contract was found in those three pages
or the reviewed [API overview](https://developers.openai.com/ads/api-overview).
That is an evidence gap, not proof that no provider mechanism exists. A lost
response remains ambiguous and cannot justify another write.

## Implementation constraints

The initial adapter should select one existing campaign through complete,
account-bound inventory, plan only its status transition to paused, and preserve
all other allowlisted state. The plan must bind the native resource and current
account/profile identity, before/after fingerprints, API/capability version,
expiry, exact action and readback/recovery conditions. Account-scoped API keys
must not be described as provider-enforced read/edit scopes.

The safety kernel must validate and authorize the exact composite, re-read and
reject drift, and fsync its durable start before egress. A transport or schema
failure after egress remains unresolved until a bounded read proves a settled
state. The adapter must not infer a remote actor or claim that observing a paused
state proves who changed it. Current account/resource access, authorization-host
integration, live conformance, and provider-side settlement guarantees remain
required. No generic POST, arbitrary URL, or executable candidate import follows
from this research.

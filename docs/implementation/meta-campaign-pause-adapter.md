# Fixed Meta campaign pause adapter

`MetaCampaignPauseGuardedAdapter` is a private, factory-scoped adapter for one
reviewed Meta campaign transition from configured `ACTIVE` to configured
`PAUSED`. It is constructed only by
`_configured_meta_campaign_pause_adapter(directory, action, ledger)`, which
revalidates the closed M40 action, exact local ledger root/profile, current
EDIT profile, nonsecret token binding facts, account identity, and finite
token windows. It does not add a CLI command, installed default route, generic
credential resolver, or provider capability claim.

The only mutation shape is the SDK-derived offline route
`POST https://graph.facebook.com/v26.0/{campaign}/`, with an `Authorization:
Bearer` subject header and the exact form body `status=PAUSED`. There is no
token query parameter, caller-selected URL, body, method, HTTP client, or
retry. A factory-issued private capability binds its EDIT core, native campaign
ID, plan digest, and subplan digest, then consumes the current kernel
`EgressPermit` at that sole handoff. A failed permit check remains a local
precondition with no POST. Once the permit is consumed, every dispatch,
transport, response, parser, continuity, readback, or retention failure is
`AMBIGUOUS_REMOTE_STATE`; the route never retries an uncertain write.

The shared one-attempt Graph transport is revalidated after the current
subject-token snapshot immediately before the POST. A response-induced cookie,
ambient client parameter, hook, authentication setting, or other changed
request state therefore blocks the POST rather than changing its fixed wire
shape. Because that final check runs after permit consumption, its failure is
conservatively ambiguous even when no bytes were sent.

Before the permit is consumed, the adapter obtains a fresh account identity and
campaign baseline, compares all stable EDIT association facts and declared
account facts to the approved action, checks action/token expiry, and requires
the exact approved `ACTIVE` configured/effective baseline. After the local ACK
subset, it repeats the account and campaign observations. The only subset that
permits `CONFIRMED` is HTTP 200 with duplicate-key-safe, bounded JSON exactly
equal to `{"success": true}` using a real boolean, followed by a fresh
account-bound `PAUSED` configured-status readback. This is an implementation
subset derived from tagged SDK evidence; it is not a claim that it is a live
Graph acknowledgement, conditional-write, idempotency, delivery, authority,
or zero-spend guarantee. Effective status is retained only as an observation.

After that fresh readback, the adapter appends one dedicated
`META_CAMPAIGN_PAUSE_READBACK_OBSERVATION` under the EDIT session’s
configuration-then-credential retention recheck. The append happens after all
network activity; its callback performs no network or ledger I/O. The record
keeps the exact action/plan/subplan/attempt binding, configured `PAUSED`,
observed effective status, and factual read interval without becoming an
inventory snapshot or an external-change claim. Append failure is ambiguous.

The returned evidence references are SHA-256 digests, not HMACs: one binds the
local acknowledgement subset and one binds the exact retained readback record.
Neither retains a raw response, token, receipt, key, or unknown provider data.

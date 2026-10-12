# Meta campaign PAUSE native approval subject

`META_CAMPAIGN_PAUSE_PLAN` is a closed, Core-only receipt subject for one
already-retained M40 Meta campaign PAUSE action. It accepts the complete
canonical action, reconstructs its action identifier, exact single-subplan
plan, and remote-state fingerprints, and binds the result into the existing
v1 approval challenge, review, and receipt formats. It is not a generic Meta
plan parser, Graph client, provider-currentness check, installed-host
attestation, or execution authority.

## Closed action and timestamp contract

The native decoder admits only the fixed M40 action shape and literals:

- development-fixture-only status, zero incremental spend, fixed Graph v26
  metadata, one Meta Ads `PAUSE` subplan, and finite expiry;
- an `ACTIVE` configured baseline and a configuration-only `PAUSED`
  postcondition with effective delivery `NOT_PREDICTED`;
- exact workspace, account, local association, observation, postcondition,
  plan, action-ID, plan-ID, subplan-ID, and state-fingerprint cross-bindings;
  and
- strict scalar, native-ID, currency, timezone, status, digest, and canonical
  JSON validation.

M40 action timestamps use their own lexical rule: they are whole-second UTC or
six fractional UTC digits with a nonzero fractional part. The local Meta parser rejects redundant
`.000000Z` spellings before it reconstructs an action or plan hash. This keeps
Swift aligned with the Python M40 canonicalizer without changing the broader
receipt timestamp grammar. It applies to all three execution timestamps, both
observation timestamps, the plan expiry, and the subplan expiry. Existing
receipt timestamps retain their v1 grammar.

The deterministic review exposes only safe, bounded facts: local
workspace/profile/application association, account and campaign references,
baseline configured/effective statuses, observation interval, the `ACTIVE` to
configuration-only `PAUSED` transition, plan identifiers and expiry, zero
incremental spend, fixed egress, recovery disposition, and explicit
limitations. It does not expose credential-binding references, derived
principal, configuration/profile digests, tokens, provider request or response
bodies, or a success/authority claim. The receipt binds the grantor reference,
action digest, plan digest, and exact subplan digest. It does not claim a
provider revision, conditional write protection, idempotency, effective
campaign delivery, zero residual spend, or current provider state.

## Raw XPC and bridge boundary

M42 deliberately does not promote Meta PAUSE into an installed bridge or
execution route. M32 bridge request construction still admits only OpenAI
PAUSE and GA4 key-event CREATE. The raw XPC client host-binding predicate and
the direct-plan state-machine predicate explicitly reject the Meta kind.

The raw XPC service also rejects a canonical Meta kind header before acquiring
an interaction lease, starting review, authentication, signing, or a native
handoff. The header-only predicate is intentionally narrow: it is not a
challenge validator or authority check. It preserves the established
interaction-gate-first error precedence for malformed and non-Meta raw
requests; this compatibility rule was repaired after the initial full-suite
regression exposed an over-broad raw-XPC preflight.

This slice opens no XPC connection and does not access a Keychain, local
protected store, Security, LocalAuthentication, AppKit, configured host,
helper, provider credential, provider API, or customer data. It does not
install or sign an app, consume a receipt, write a ledger record, execute a
plan, or send a Graph mutation.

## Synthetic interoperability vectors and checks

The generated fixtures live in
[`Fixtures/MetaPause`](../../native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause):

- `meta-campaign-pause-approval-v1.json` uses whole-second M40 action times.
- `meta-campaign-pause-approval-fractional-v1.json` uses `.123456Z` at all
  seven M40 action timestamps. Its receipt issue time remains a valid distinct
  v1 three-digit timestamp.

They contain synthetic canonical challenge, review, unsigned receipt, and
hash data only. They contain no signing key, signature, OS authentication
result, installed-host result, provider credential, provider result, or
execution authority.

`MetaCampaignPauseApprovalTests` consumes both vectors, compares the exact
canonical action/challenge/review/receipt digests, rejects rebuilt
`.000000Z` action variants, and verifies the existing receipt shape with an
ephemeral P-256 key. `MetaCampaignPauseXPCTests` uses injected fake transport,
engine, interaction gate, reviewer, authenticator, and signer seams to prove
that both raw-client and raw-service Meta admission fail before protected or
interactive work. The focused and full Swift test runs use an isolated SwiftPM
scratch path with sandboxing enabled; they do not invoke a configured host or
external service.

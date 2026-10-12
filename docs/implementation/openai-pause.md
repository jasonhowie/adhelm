# OpenAI campaign pause preparation

`openai_pause` is a development-only, fixture-tested fixed-operation adapter.
It has no CLI command, trusted approval host, live conformance evidence, or
claim of a live pause capability.

Planning requires a current account GET, complete bounded campaign inventory,
and a fixed current campaign GET. It accepts only an active account and active
campaign whose native bid, lifetime budget, and start schedule are all present.
The public campaign ID, inventory bound (1 through 5,000), and timezone-aware
future expiry are validated against an injected local clock before any provider
read. Invalid or expired values return a structured local diagnostic without
accessing the configured profile.
The resulting closed baseline contains account native ID/reference, timezone,
currency, status, profile reference, campaign native ID/reference, status,
native budget, bidding type, and complete available schedule. It deterministically
rebuilds the complete `CompositePlan` from that baseline: provider/API/capability
versions, PAUSE action, before/after state, fixed egress, zero incremental-spend
limit, expiry, recovery action, and every field change. Altering a rehashed
account, campaign, profile, credential binding, version, egress, budget, or
state fails validation; hashes are not accepted as a claim about missing source
values.

Plan and subplan IDs are deterministic digests rather than string interpolation
of native campaign IDs. Field-change paths are the fixed structural values
`campaign/identity` and `campaign/status`; native identifiers remain bound in
local reference fields and state digests, where normal redaction applies, and
cannot leak through paths, ledger resource references, or exports.

The credential binding is a domain-separated HMAC over the current in-memory
secret and profile reference with an adapter-private key of at least 32 bytes.
It is computed inside the adapter and is never supplied as a separately trusted
claim. The secret and private HMAC key have redacted representations and never
enter plans, errors, evidence, logs, or test output. Before the fixed POST the
adapter captures a credential snapshot for the immediate current read and POST,
then refetches and compares it immediately before egress to fail closed on
profile or key rotation.

Execution has only an actual-`SafetyKernel` wrapper around
`SafetyKernel.execute`; a lookalike executor and direct `apply` both fail.
Guarded apply requires the active, exact one-use permit after the kernel's
fsynced `APPLY_STARTED`. Permit consumption rechecks plan and authorization
expiry while holding its one-use lock immediately before dispatch; the adapter
also checks its plan clock immediately before consumption. Before each
preflight and write it repeats current identity, inventory, and campaign reads;
a profile, account, key-binding, resource, account metadata, or state mismatch
rejects before POST.
The adapter sends one fixed `POST /v1/campaigns/{campaign_id}/pause`; its local
correlation key is never sent to OpenAI. It only returns a verified proof when
both the conservatively parsed changed-campaign response and a separately
performed current GET exactly match the planned paused state. A transport loss,
5xx, malformed success response, or changed independent read produces no proof
and no retry. OpenAI documents no conditional-write, provider idempotency, or
settlement guarantee: a pure pause authorizes zero *incremental* spend, while
residual delivery or billing can still occur, and an unchanged read cannot prove
no effect. Reconciliation therefore remains explicitly unresolved for an
ambiguous attempt.

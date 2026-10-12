# Meta PAUSE private acceptance V2

`_installed_meta_campaign_pause_approval_bridge_v2.py` is an offline, private
handoff for one already-retained `META_CAMPAIGN_PAUSE_PLAN`. It prepares a
one-use Meta-only V2 request and later consumes an already-returned opaque V2
candidate through the existing exact receipt-claim path. It has no configured
resolver, helper launcher, pipe, host, operating-system secret access,
credential access, provider client, or egress path.

The caller first supplies an `ApprovalHostConfiguration` and a 64-character
metadata digest to
`_make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2`. The module
detaches and revalidates the seven configuration fields, then recomputes the
protocol configuration digest as part of the V2 request. The supplied full
metadata digest is only an expected caller-held fact in this layer; it does not
attest an installed host, a ledger origin, or authority to perform a provider
operation.

`_prepare_installed_meta_campaign_pause_approval_bridge_v2(kernel, ledger,
source_plan_record_digest=..., reviewed_metadata=...)` requires the exact
coordinating `SafetyKernel` and `Ledger`, selects one verified retained Meta
plan, and captures all three ledger stream prefixes plus the current checkpoint
digest. It accepts no caller-selected display grantor. Its challenge derives
that value deterministically from the selected record digest:

```
logical:adhelm-meta-campaign-pause-approval-bridge-v2:<record-digest>
```

The request uses the V2 six-field source-context preimage and the existing M42
Meta subject. Fresh nonce and request ID bytes are generated inside the module;
the optional byte parameters are private deterministic-test seams only.

`_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(...)` first
checks a module-issued pending value, its exact lock type, immutable source,
challenge, request, metadata, and one-use state. Malformed pending facts fail
before candidate inspection. A reused pending value returns an explicit
non-retryable consumed result. Once those pre-candidate checks pass, coordinator
mismatch, source-prefix reload failure, clock failure, candidate failure,
receipt consumption failure, or grant-recording failure becomes a single
ambiguous, no-retry result. A valid append-only ledger suffix is permitted, but
stream replacement, truncation, changed prefix head, or changed retained action
is not.

The V2-only wrapper in `approval_acceptance.py` revalidates the opaque candidate
at every local current-time sample. It extracts canonical receipt and active
key bytes only inside the private stack, compares the candidate challenge to the
held challenge, and calls the shared exact receipt acceptance core. That core
reloads the retained Meta plan before it records a receipt claim and direct
grant. Neither this module nor the wrapper returns raw receipt/key material.

This is local receipt and history handling only. A direct grant is not a claim
that the customer host is installed, that Meta has accepted a PAUSE update, or
that configuration-only `PAUSED` proves effective delivery or zero residual
spend. Existing V1 bridge inputs and raw receipt-XPC Meta refusal remain
unchanged. The shared V2 wire fixtures live under
`native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridgeV2MetaPause/`;
they cover codec interoperability, not this private acceptance path.

The fake-only tests cover detached caller facts, fixed grantor derivation, a
real CPython lock and forged-lock rejection, one-use consumption, all-stream
source-prefix drift, coordinator and clock ambiguity after a candidate, pending
corruption before candidate handling, and V1 Meta-route refusal. They do not
open a pipe, use a configured host, access an OS store, or contact a provider.

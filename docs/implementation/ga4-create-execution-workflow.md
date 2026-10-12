# Internal configured GA4 CREATE execution

This library connects an exact retained GA4 CREATE plan and native receipt to
the guarded adapter through `SafetyKernel`. It has no installed mutation CLI,
host trust resolver, or live-provider conformance evidence. Synthetic stores,
ephemeral approval keys and fake fixed HTTP exercise the internal path. The
action retains `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.

The closed execution request contains a retained plan record digest and complete
fresh candidate input. The future installed integration separately owns the
expected challenge it constructed for review and its enrolled pinned host trust.
An incoming receipt cannot select either. The workflow accepts no raw bearer,
inventory, execution identity, provider request, caller-built grant or adapter.

Before receipt acceptance, the workflow loads the exact verified retained plan,
checks the configured OAuth EDIT target and configuration/profile digests, rejects
unresolved execution history, and obtains the current local fingerprint key.
Source validation uses one verified checkpoint: it requires exactly one matching
retained plan, recompiles the complete fresh input against the exact selected
candidate and latest version of its conversion definition, and authenticates the
selected EDIT observation and semantic state with the current key. A later EDIT
observation for that profile invalidates the source selection. Parallel candidates
have no supersession relation, and unrelated later history is allowed; the original
full planning checkpoint is historical and is not required to equal current history.

The closed receipt acceptance path independently reparses the expected challenge,
verifies the pinned signature and time, compares the exact retained source action,
durably claims the receipt/nonce, and records its exact direct grant. Receipt
acceptance binds structural state; the configured source validator separately
authenticates local HMAC evidence. Neither supplies Google permission proof.
Failures after receipt claiming cannot reuse that receipt.

Only after acceptance does the configured adapter factory resolve and consume a
fresh protected OAuth snapshot. It owns the private bearer and fixed-host HTTP
transport for one lifetime. Current source, configuration, target, key and time
checks repeat before reads and immediately before permit consumption. No ledger
or configuration lock is held across provider HTTP. Sampling detects observed
changes; it cannot exclude an exact change and reversion between checks or make
a sequential remote inventory atomic.

The safety kernel requires a guarded adapter for every GA4 CREATE. Before the
durable `APPLY_STARTED` append, the ledger checks the exact retained plan and
latest relevant source records under its changes-stream lock. It validates the
provider, property, run, subplan, before/after state, zero spend, capability and
attempt digest. Duplicate plan provenance and repeated starts for that plan are
rejected. Physical ledger scans repeat these checks at each start's original
prefix. The check uses no credential, key, nested ledger read or provider call.

The kernel issues its one-use permit only after the start append has fsynced.
The adapter repeats current baseline checks, consumes the matching permit and
sends exactly one fixed CREATE request. Confirmation requires the response, an
independent GET of the returned resource, and complete collection readback to
match the predicted semantic state and exact runtime collection. The local
attempt digest is never sent as a Google idempotency header or parameter.

For GA4 CREATE, the kernel does not upgrade a non-confirmed response using a
matching later state or an adapter's retained verification proof. Ambiguity stays
`AMBIGUOUS_REMOTE_STATE`, with recovery required. A later matching collection
cannot attribute a lost CREATE to this invocation. Recovery is read-only
reconciliation/export/stop, without automatic delete, pause or replay. Config
verification does not establish browser firing, Realtime/processed receipt,
native conversion linkage, or legal/provider-policy approval.

Focused integration tests cover the actual receipt-to-kernel path using fake
HTTP, invalid signatures before OAuth, changed keys/configuration, superseded
conversion definitions, and lost responses without replay. Independent kernel
tests cover mandatory permits, source races and provenance duplication. Native
wire parity and installed-host trust remain separate evidence requirements.

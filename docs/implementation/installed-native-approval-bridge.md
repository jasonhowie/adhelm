# Private installed native approval coordinator

`adhelm._installed_native_approval_bridge` prepares and consumes one private
approval bridge request for an already retained `OPENAI_PAUSE_PLAN` or
`GA4_KEY_EVENT_CREATE_PLAN`. It is not a command, public API, trust resolver,
or provider executor.

The checked-in resolver always returns
`NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED`. A later reviewed installed Python
product must provide the compiled binding in code. It supplies only the closed
approval-host configuration and the full installed-metadata digest. It must
not load either value from a manifest, environment variable, path, receipt, or
user input. The binding contains no public-key pin, dynamic helper path, or
generic XPC selector.

The private issuer and one-use slots are in-process misuse guards only. They
do not prove an installed identity, establish a trust pin, or authorize a
provider action.

## Retained source and request

Before a private exchange, the coordinator selects exactly one record from a
verified `changes` checkpoint and reparses its closed action. It accepts only
the two direct retained-plan record kinds, requires the action profile to equal
the local ledger profile, and retains the source record digest, canonical
action bytes, approval kind, checkpoint digest, and all three stream prefixes.

A prefix consists of the stream ID, high watermark, and head digest. After a
candidate is returned, the coordinator takes a fresh verified checkpoint. Each
stream must retain the original ID and original head at its original sequence,
and may only add a suffix. This permits unrelated append-only history growth
without accepting truncation or replacement. It then reloads the selected
changes record and requires the same record kind, workspace profile, and
canonical action before any receipt claim.

The coordinator generates independent 32-byte challenge-nonce and request-ID
values itself. It derives the source-context digest from the retained profile,
record digest, and checkpoint digest. The challenge expiration is the earlier
of the retained plan expiration and the compiled protocol lifetime; a caller
cannot extend it. The pending request is private, nonserializable, and marked
attempted before its transport exchange. It is never retried or cached.

## Candidate and acceptance boundary

The coordinator calls the private M33 one-frame pipe transport only after the
compiled-binding gate and retained-source preparation. The transport remains
unwired from a public runtime in this checkout. Once it returns a candidate,
any reload mismatch, local-clock rollback, candidate failure, claim failure,
or grant-recording uncertainty returns the fixed ambiguous
`NATIVE_APPROVAL_BRIDGE_ACCEPTANCE_UNVERIFIED` result. The native nonce may
already be consumed; the next action is to inspect history and start a fresh
review, never to retry that exchange.

The coordinator hands the opaque candidate to a private shared acceptance
core. That core reparses it before every local receipt verification and keeps
its receipt/key material inside the exact call stack. It does not construct
`PinnedApprovalTrust`, expose a reusable pin, return raw receipt or key bytes,
or accept a caller-created candidate. The retained source reload occurs before
the shared core. Its final ledger append callback only revalidates already-held
candidate, request, configuration, and clock facts; it performs no ledger
read, transport call, or other I/O while the changes-stream lock is held.

The existing `SafetyKernel` remains the only owner of authorization use,
`APPLY_STARTED`, mutation permits, and provider egress. This coordinator
records no provider effect and does not make the unsigned/development native
artifact an installed, signed, enrolled, or executable approval capability.

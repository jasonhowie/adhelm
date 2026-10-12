# Native receipt-redemption state

This increment adds an offline, package-internal state contract for a future
native approval-receipt acceptance bridge. It does not add a CLI or XPC method,
wire the production runtime, access Keychain or Security, authenticate a user,
look up a key, execute a Python operation, append JSONL, contact a provider, or
establish installed authority. ADR0001 continues to make Python the only
provider-execution engine.

## Closed state and source binding

`ReceiptRedemptionSourceBindingV1` records only non-secret, exact source facts:

- the M29 seven-field protocol-configuration digest;
- challenge digest and SHA-256 nonce digest;
- canonical receipt SHA-256 digest, receipt identifier, and receipt kind; and
- the declared receipt expiry.

Construction from receipt bytes reconstructs the complete unsigned receipt from
the expected challenge, deterministic review, and trusted configuration. An
added or altered unsigned field therefore cannot enter the source binding just
because a selected digest field looks valid.

`ReceiptRedemptionStateV1` is the nonce-local record. `PENDING` contains the
source binding and `reserved_at`; `VERIFIED` adds `verified_at`. Both have
closed field sets, schema version 1, lowercase SHA-256 digests, bounded
canonical bytes, and strict type validation. A decoded nonce record must bind
exactly to the requested nonce and all source facts before it can be treated as
pending or replayed.

`ReceiptRedemptionGlobalStateV1` is a separate, mandatory fixed-slot record:

- `READY` has `schema_version`, `state`, positive safe-integer `generation`,
  and a canonical six-fraction UTC `observed_at`.
- `INTENT_PENDING` has those fields plus the exact seven-field source binding.

A normal redemption transitions `READY` generation *n* to `INTENT_PENDING`
generation *n* + 1, then to `READY` generation *n* + 2. The transition helpers
reject the wrong source state, changed binding, timestamp rollback, expiry, and
insufficient headroom for both increments before any durable mutation starts.
The global record is local protected-store integrity and ordering evidence. It
is not receipt signature verification, host attestation, installation proof, or
execution authority.

## Reservation and promotion

All coordinator collaborators and construction are internal to the macOS
module. A future adapter selects its one nonce item and one global item from
reviewed build metadata; callers cannot select a service, account, key, query,
or arbitrary persistence location.

The coordinator snapshots bounded receipt bytes, performs canonical preflight
before acquiring its one lease, then repeats preflight under that lease with a
fresh time sample. It requires an existing, decodable global record before any
current-key verification or state write. Missing global state returns
`globalStateRequired`; malformed global state returns recovery-required. Neither
is auto-initialized by this slice.

Under the lease the coordinator requires a `READY` global state, checks the
monotonic clock, examines any exact nonce record, and verifies the receipt
against the current ACTIVE key. An invalid signature, revoked key, mismatched
verified receipt, clock rollback, expiry, or cancellation before reservation
writes neither nonce nor global state.

After that verification, the coordinator writes and exactly rereads
`INTENT_PENDING` before it starts the independent nonce `PENDING` write. The
intent transition is the reservation ambiguity boundary. It then writes and
exactly rereads `PENDING`, verifies the current receipt again, checks
cancellation and time before promotion, replaces `PENDING` with `VERIFIED`, and
finally compares/replaces the exact intent with a new `READY`. Every write has
an exact canonical readback check.

Once global intent replacement is attempted, all subsequent failure modes map
to `postReservationUnverified`: a write might have taken effect even if its
call throws or readback is absent. This includes nonce readback loss, second
verification failure, cancellation, expiry, clock rollback, and final global
clear uncertainty. An `INTENT_PENDING`, a nonce `PENDING` paired with `READY`,
or any malformed/mismatched fixed state blocks before another key verification
or write. There is no automatic recovery or retry command.

The global and nonce records are deliberately separate sequential operations.
The collaborator's global compare-and-replace is a contract while the existing
lease is held; M31 does not implement a Keychain CAS adapter or claim a
cross-record atomic transaction. It also does not defend against arbitrary
restoration of an earlier valid `READY` record together with loss of a nonce
record. A future production adapter needs a rollback-resistant protected-store
anchor before claiming that stronger property.

## Permit and later integration

`ReceiptRedemptionPermit` is one-use, process-local, source-bound, and
non-Codable. It is not an XPC value, file value, stdout value, Python trust
value, provider credential, or execution authorization. `VERIFIED` proves only
that the native coordinator performed its two current-key checks and consumed
the local nonce under this offline collaborator contract.

A later installed acceptance integration must recheck current native state and
freshness at its own handoff point, then coordinate with the existing Python
`APPROVAL_RECEIPT_CLAIMED` and `APPLY_STARTED` lifecycle records before Python
provider egress. That later work must preserve Python's established exact grant
expiry and consumption semantics. M31 neither creates an atomic
protected-store-plus-JSONL transaction nor moves provider execution into the
native host.

## Offline checks

`ReceiptRedemptionStateTests` covers canonical nonce/global decoding, source
binding, strict timestamps and scalar types, transition direction, binding,
timestamp, and safe-generation boundaries, plus regenerated cross-language
vectors. `ReceiptRedemptionCoordinatorTests` uses only ephemeral P-256 keys and
in-memory lease/session/clock fakes. It covers invalid or revoked current
receipts before reservation, intent and nonce readback ambiguity across restart,
global loss/malformed state, nonce/global mismatch, rollback and expiry,
cancellation during and after verification, exact replay, and one-use permits.

Focused validation uses the package test runner with isolated SwiftPM scratch
paths and disabled Keychain/network resolution. It performs no real Keychain,
Security, LocalAuthentication, AppKit, XPC, provider, or filesystem protected
store operation.

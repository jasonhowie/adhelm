# Native approval bridge transport ultra review

## Scope

This independent, fake-only review covers the M33 path from the M31 fixed-slot
receipt state machine through the private M32 candidate producer and Python's
one-shot pipe transport:

- `ReceiptRedemptionCoordinator.redeemBridgeCandidate`
- `InitialEnrollmentReceiptVerificationLease` and
  `ReceiptRedemptionFixedSlotAdapter`
- `ReceiptRedemptionFixedSlotStore` transition/readback boundary
- `adhelm._native_approval_bridge_transport`

It did not start a helper, create an `LAContext`, touch Keychain/Security,
construct XPC, access a ledger, invoke a provider, or make a network request.

## Reviewed properties

The native producer accepts only exact static metadata and a direct M32
challenge, checks its own clock before protected work, and produces a candidate
only after the global READY-to-INTENT transition, nonce reservation, second
ACTIVE-key verification, nonce VERIFIED transition, global READY transition,
and exact readbacks. A later error is `postReservationUnverified`; it does not
return a candidate or make a retry safe.

The concrete adapter binds one configuration and object-identical enrollment /
redemption session. Its callback lease is invalidated on exit, rejects a
different thread before a collaborator call, and holds its operation lock
through each protected read, replace, and readback. Global replacement accepts
only the exact READY-to-INTENT_PENDING and INTENT_PENDING-to-READY state
transitions. Errors already classified by M31 remain in that domain instead of
being reduced to an unavailable precondition.

The transport has one source-fixed executable, argv, working directory and
environment. It uses only owned stdin/stdout/stderr pipes, one bounded
big-endian request frame, one bounded candidate frame plus EOF, bounded
discarded stderr, nonblocking FIFO descriptors, a fixed 300-second monotonic
budget, and terminal close/terminate/kill/reap handling after launch. It
revalidates the request at exchange start, checks cancellation immediately
after the adapter returns and before evidence return, and samples Python time
again after candidate validation. Every post-launch failure is the fixed
ambiguous result; raw frames and stderr do not enter its diagnostics.

## Repaired findings verified here

- The concrete adapter initially accepted only INTENT_PENDING-to-READY, even
  though M31 begins reservation with READY-to-INTENT_PENDING. Both exact
  transitions are now accepted and all others fail closed.
- A callback-domain `postReservationUnverified` could be converted to an
  unavailable error by the adapter catch-all. It now preserves M31's ambiguous
  result.
- A retained or cross-thread callback lease could previously race invalidation.
  Operations now hold the lease lock through the collaborator call and
  readback; invalidation waits on that lock and the original-thread check runs
  before the operation.
- The M31 outer clock could move behind a later current-key verifier sample.
  The observed verifier timestamp is now retained and becomes the next outer
  clock lower bound.
- The pipe writer originally accepted a write count larger than its submitted
  chunk, allowing unsubmitted request bytes to be skipped. It now bounds the
  count by the exact submitted chunk.
- Post-response cancellation and final local-clock rollback/expiry now fail as
  ambiguous after native consumption instead of returning candidate evidence.

## Independent regressions

`ReceiptRedemptionFixedSlotAdapterUltraReviewTests.swift` has four fake-only
tests:

1. An escaped callback lease cannot read the global state after callback exit.
2. A different thread cannot use a live callback lease before a store read.
3. Outer time `T=10`, verifier observation `T=20`, then outer time `T=15`
   returns `clockRollback` before INTENT or nonce writes.
4. A failed nonce write after the actual adapter's READY-to-INTENT transition
   remains `postReservationUnverified` with the INTENT record retained.

`test_native_approval_bridge_transport_ultra_review.py` has four fake-only
tests:

1. Cancellation immediately after adapter return never returns a candidate.
2. The final post-validation Python sample cannot roll back.
3. The final post-validation Python sample cannot reach receipt expiry.
4. An overreported partial write cannot advance beyond submitted bytes.

The final focused native command passed 4/4 independent adapter tests. The
final focused Python command passed 4/4 independent transport tests. Combined
M32/M33 Python base and ultra coverage passed 65 tests at the reviewed source
revision. Native source and production-path checks were not used to claim live
Keychain, helper, host, XPC, or authorization behavior.

## Limits

`SecurityReceiptRedemptionFixedSlotStore` remains unwired. Its local
read/compare/update/readback sequence is not a cross-process transaction, and
this review does not claim rollback-resistant protected-store durability. The
bridge candidate remains private, non-authorizing evidence; a later installed
bridge must recheck its own retained Python source context before any claim or
provider operation.

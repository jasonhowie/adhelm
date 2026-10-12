# Native approval bridge candidate producer

The internal `ReceiptRedemptionCoordinator.redeemBridgeCandidate` connects the
M31 consumption state machine to the M32 private candidate format. It requires
the same complete trusted configuration as the supplied static metadata before
acquiring a protected lease. The request must bind that metadata and a valid
direct-plan challenge at the retained exchange start.

Each current receipt verification now returns the exact observed ACTIVE X9.63
key and a fresh verification observation time with its signed receipt through
a module-internal, redacted value. The key
is bounded and deeply copied. The coordinator checks that the returned receipt
equals the original receipt and verifies its signature with those exact key
bytes before writing global intent. Verification after nonce reservation must
return the same key. This prevents an inconsistent collaborator result from
reserving a nonce or becoming a valid bridge candidate.
The next coordinator clock sample must also be at least that verification
observation. A protected read that advances the clock cannot hide a subsequent
rollback between the coordinator's own earlier samples.

After the existing global-intent and nonce transitions, the coordinator keeps
the final observation sampled after both exact readbacks in its private
one-use permit. Only its candidate-consumption method can combine these facts
with the retained request. That method reparses all M32 bindings and checks
the independent handoff clock. Cancellation, expiry, rollback, or any failure
after reservation returns `postReservationUnverified`. Native consumption is
not undone, and no retry is performed. A final clock and cancellation check
also follows candidate validation before its private return.

The candidate status describes native current verification and native nonce
consumption only. It does not record a Python claim, issue a grant, or authorize
provider execution. This producer has no installed XPC/runtime caller and
accepts no public CLI input. Deterministic review, explicit user approval,
fresh OS authentication, protected signing, the fixed helper, and Python source
reloading must still be connected through the reviewed installed bridge.

Base tests use ephemeral P-256 keys and fake stores. They cover both existing
direct kinds, exact response bytes, transition order, replay, wrong fixed
configuration, an unrelated returned key, failed final readback, and mutable
key-buffer isolation. The first run exposed a missing GA4 fixture in the macOS
test bundle; the existing synthetic Core fixture is now copied there, and the
Python regeneration check requires both copies to equal their canonical source.
These tests never access an OS credential store or launch an approval host.

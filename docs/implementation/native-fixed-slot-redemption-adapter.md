# Native fixed-slot redemption adapter

M33 adds a package-internal adapter between the M31 receipt-redemption state
machine and the existing `InitialEnrollmentCoordinator` fixed-slot lock. It is
an offline implementation boundary only. No runtime, CLI, XPC method, Python
handoff, provider operation, Keychain initialization, migration, deletion, or
recovery path constructs the adapter in this increment.

`ReceiptRedemptionFixedSlotAdapter` accepts one exact reviewed
`TrustedHostConfiguration`, one exact `InitialEnrollmentCoordinator`, one
fixed-slot store, and one object-identical enrollment/redemption session. It
holds the existing coordinator lease for the complete M31 operation. The
already-leased verifier returns the signed receipt, the exact current ACTIVE
X9.63 public-key bytes, and a fresh coordinator timestamp; it does not expose
a general key lookup. The adapter rechecks the receipt binding and preserves
the M31 coordinator's post-reservation errors unchanged.

The callback capabilities are confined to their creating thread and callback:
each operation holds its local guard through the protected-store call, and
invalidation waits for an in-flight operation. A retained lease, a different
session, cross-thread use, cancellation, or recursive same-coordinator lease
fails closed. This is process-local serialization. It does not claim a
Keychain compare-and-swap primitive, cross-process exclusion, or a transaction
across the global and nonce records.

The adapter derives its only selectors from a canonical domain-separated
preimage of reviewed host/key, requirement, enrollment, storage, access-group,
Mach-service, and protocol-configuration facts. The global record has a fixed
selector; nonce selectors append only a validated lowercase SHA-256 nonce
digest. There is no request-selected Keychain service, account, path, query,
or key. A missing or malformed global record remains a closed recovery state;
the adapter never initializes, migrates, deletes, or clears it.

Store writes are bounded, canonical, state-specific, compare-before-replace,
and exact-readback checked under the held lease. The only accepted global
transitions are `READY → INTENT_PENDING` and the exact matching
`INTENT_PENDING → READY`. An error after the first intent attempt is preserved
as M31's inspect-before-repeat result. A successful `VERIFIED` nonce is only
native current-verification and consumption evidence. It is not an execution
grant, provider permission, Python authority, or egress authorization.

`SecurityReceiptRedemptionFixedSlotStore` documents the eventual fixed
Keychain mapping and compiles only as a reviewable concrete collaborator. M33
does not construct or invoke it. Its sequential read/compare/update/readback
logic must not be read as an atomic Keychain or rollback-resistant storage
guarantee.

Focused fake-only validation:

```sh
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m33-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m33-module-cache \
swift test --package-path native/macos-approval-host \
  --enable-xctest --disable-swift-testing \
  --scratch-path /private/tmp/adhelm-m33-build \
  --filter ReceiptRedemptionFixedSlotAdapterTests
```

This runs five tests for successful exact transitions, post-INTENT ambiguity
and restart quarantine, replay/current-signature classification, exact-session
and callback confinement, and deterministic reviewed selector derivation. It
uses in-memory stores and ephemeral software P-256 keys only.

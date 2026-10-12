# Native redemption-global initialization ultra review

## Scope

This independent M36 review covers schema-v2 enrollment provenance and the
explicit in-memory fixed-slot global initialization path. It uses only pure
Core values and fake provenance, key, protected-session, and fixed-slot-store
collaborators. It does not invoke Security, Keychain, LocalAuthentication, XPC,
a helper, a host, a provider, or Python execution.

## Boundaries reviewed

- Schema-v1 `ACTIVE` stays byte-stable and cannot acquire a setup marker by
  adding fields or changing its schema number.
- Schema-v2 setup records reject scalar aliases and control-suffixed genesis
  timestamps, and `COMPLETE` requires the exact prior intent binding, selector,
  and freshly observed active-key digest.
- Only a schema-v2 `COMPLETE` marker can admit a concrete redemption lease.
  Legacy and interrupted markers fail before fixed-global access.
- The explicit setup action retains `INTENT_PENDING` after a post-intent
  failure. It neither repeats global creation nor converts a recovery state
  into a usable lease.
- A changed generation-one genesis record, a missing global after completion,
  or a current-key swap is recovery-only. None is adopted, reset, or
  reinitialized.

## Result

The reviewed implementation kept the v1/v2 boundary closed and made the setup
sequence one-way: `NOT_STARTED` to `INTENT_PENDING` to `COMPLETE`, with exact
readbacks between durable transitions. I did not reproduce a remaining source
defect after the owner fixes. The process-local locking and fake stores do not
claim atomic provenance-plus-Keychain transactions, cross-process exclusion,
or rollback-resistant storage; interrupted or inconsistent durable evidence
remains recovery-only.

## Validation

```text
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m36-ultra-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m36-ultra-module-cache \
swift test --package-path native/macos-approval-host \
  --scratch-path /private/tmp/adhelm-m36-ultra-build \
  --filter 'InitialEnrollmentRedemptionGlobalSetupTests|ReceiptRedemptionGlobalInitializationTests|InitialEnrollmentCoordinatorTests|ReceiptRedemptionFixedSlotAdapterTests|InitialEnrollmentRedemptionGlobalSetupUltraReviewTests|ReceiptRedemptionGlobalInitializationUltraReviewTests|ReceiptRedemptionFixedSlotAdapterUltraReviewTests'
```

The combined fake-only command passed 40 cases: 29 owner cases and 11
independent regressions. Four pre-existing M31 adapter tests initially used a
schema-v1 `ACTIVE` fixture, so M36 correctly denied their leases. Their fixture
now creates the explicit v2 `COMPLETE` genesis lineage; it preserves the
original escaped-lease, wrong-thread, clock-order, and post-reservation
ambiguity assertions. The other independent cases cover v1 field/schema
rewrites, strict nested decoding, binding/key mismatch, legacy and intent lease
denial, post-intent interruption, changed or missing genesis state, and
current-key replacement before global access.

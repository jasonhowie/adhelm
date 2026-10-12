# Native redemption-global initialization (M36)

M36 adds local preparation for the fixed protected receipt-redemption global
record. It does not add a settings action, CLI command, XPC method, helper
operation, runtime invocation, Keychain migration, recovery command, provider
operation, Python authorization, or installed authority claim. The prepared
Security adapter is not exercised by this increment; all tests use in-memory
stores and keys.

## Versioned enrollment provenance

Schema-v1 `ACTIVE` provenance remains unchanged. Its canonical bytes, fields,
and signing/current-receipt behavior remain readable as legacy facts. A v1
record is never inferred to mean that a redemption global was initialized, and
it cannot enter the native bridge/redemption fixed-slot path.

New enrollment writes schema-v2 `ACTIVE` provenance containing a closed
`redemption_global_setup` record:

- `NOT_STARTED` has only `schema_version: 1` and the state.
- `INTENT_PENDING` and `COMPLETE` additionally bind the fixed selector digest,
  genesis global canonical SHA-256 digest, generation `1`, exact six-fraction
  UTC genesis observation, and the active X9.63 public-key SHA-256 digest.

The active identity is kept in a separate `InitialEnrollmentActiveV2` value;
serializing it never rewrites a v1 `ACTIVE` as v2. The decoder has exact field
sets and scalar types, rejects aliases and whole-second genesis timestamps,
and does not accept a setup binding for `NOT_STARTED`.

## Explicit one-time sequence

`InitialEnrollmentCoordinator.initializeReceiptRedemptionGlobalState` is an
internal future-enrollment seam. It takes the existing authenticated session,
holds the existing coordinator lock for the complete sequence, validates the
reviewed host identity, reloads schema-v2 provenance, and re-observes the
currently selected key before the first setup mutation. A retained active-key
fingerprint alone is not enough to create durable setup evidence.

For a `NOT_STARTED` marker only, and only when the fixed global item is absent,
the coordinator performs these sequential exact-readback steps:

1. Write and reread matching provenance with `INTENT_PENDING`.
2. Add and reread exactly one fixed-selector `READY` generation-1 global
   record.
3. Write and reread matching provenance with `COMPLETE`.

The selector digest is the existing M33 domain-separated SHA-256 over the
reviewed host/key, Mach service, code requirements, key application-tag digest,
access group, storage, provenance selectors, and protocol-configuration digest.
The initializer accepts no caller-selected selector, path, query, or state.

Once the `INTENT_PENDING` write is attempted, every failure is
`recoveryRequired`: failed add, lost/changed readback, cancellation, or failed
complete-marker persistence stays inspect-before-repeat. It never retries,
clears, overwrites, adopts, deletes, or initializes a different record.
`INTENT_PENDING`, legacy v1 `ACTIVE`, an existing global paired with
`NOT_STARTED`, and `COMPLETE` paired with a missing or malformed global all
fail closed. A later valid `READY` global generation is accepted only when it
remains at or after the stored genesis observation; generation 1 must still
equal the recorded genesis digest.

The normal M31 fixed-slot adapter admits a lease only after it revalidates a
schema-v2 `COMPLETE` marker with the fresh current key. It checks the stored
genesis relation when it first observes a `READY` global state. M31 retains its
own exact `INTENT_PENDING` binding and recovery behavior during an in-flight
redemption. No bridge candidate, permit, receipt, key material, or execution
authority is emitted by M36.

The reads and writes are sequential under a process-local coordinator lease.
The future Keychain collaborator has bounded add/readback and compare/readback
operations; M36 does not claim Keychain compare-and-swap, cross-process
exclusion, rollback-resistant storage, or an atomic provenance-plus-global
transaction. Arbitrary restoration of old protected items remains outside this
increment and requires a later reviewed recovery design.

## Validation

`InitialEnrollmentRedemptionGlobalSetupTests` covers v1 byte stability, v2
closed decoding, setup binding scalar/timestamp checks, and one-way state
transitions. `ReceiptRedemptionGlobalInitializationTests` uses only fakes to
cover the exact setup sequence, later valid generations, legacy rejection,
existing-global refusal, interrupted intent, failed post-add readback,
missing/changed complete globals, and active-key replacement before mutation.

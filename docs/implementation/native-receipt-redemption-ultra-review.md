# Native receipt-redemption ultra review

## Scope

This independent M31 review covers the offline, module-internal native receipt
redemption state machine: bounded receipt source binding, global `READY` and
`INTENT_PENDING` records, nonce `PENDING` and `VERIFIED` records, current-key
verification sequencing, exact readback, one-use permits, and the generated
Python/native state vectors. It uses only in-memory lease, session, clock, and
ephemeral P-256 fakes.

It does not call Security, Keychain, LocalAuthentication, AppKit, XPC, a
provider, Python execution, a protected-store implementation, or a production
receipt-acceptance path.

## Required boundaries

- A bounded canonical receipt must be structurally and challenge-bound
  preflighted before a protected-slot lease or protected-state read, then
  preflighted again under the one lease with a fresh clock sample.
- Current ACTIVE-key verification must finish before the first durable global
  `INTENT_PENDING` transition. A malformed, rebound, invalid, expired, or
  revoked receipt must leave both global and nonce state untouched.
- An existing global record is mandatory. Missing, malformed, or
  `INTENT_PENDING` global state blocks before current-key verification. A
  global intent is the durable ambiguity boundary before the independent nonce
  `PENDING` write.
- Every state readback must match the exact canonical bytes and full seven-field
  source binding. `PENDING`, mismatched records, or write/readback ambiguity
  require recovery; `VERIFIED` is replay evidence only and never returns a new
  permit.
- Clock observations must not roll back and must remain before expiry.
  Cancellation observed before promotion retains quarantine. Cancellation
  during final readback returns no permit; an already verified nonce remains
  consumed.
- The permit must remain process-local, non-Codable, one-use, source-bound, and
  non-authorizing. It must not cross stdout, a file, XPC, Python, or provider
  egress.

## Findings repaired before freeze

The review exercised and validated these repairs:

- Receipt parsing and binding now occur before protected-slot acquisition and
  are repeated under the lease, so a malformed or challenge-rebound receipt
  cannot select a nonce slot. Cryptographic rejection may follow state reads,
  but still precedes every state write.
- A preinitialized global protected record now carries both a clock watermark
  and exact reservation intent. Loss, malformed data, an outstanding intent,
  or a nonce/global mismatch fails closed rather than treating a different
  nonce as fresh after restart.
- Global transitions now require `READY -> INTENT_PENDING -> READY`, exact
  binding, nondecreasing canonical timestamps, and two safe generation slots.
  A transition cannot overwrite an intent or clear a ready state directly.
- Global generated timestamps now require exact six-fraction UTC text. Raw
  whole seconds, null, Boolean, and floating-point aliases reject at the native
  and Python vector boundaries.
- `ClosedTimestamp` previously used `NSRegularExpression.firstMatch` without
  requiring the match to consume the full input. On this Foundation runtime it
  accepted LF, CRLF, and U+2028 terminal suffixes. The parser now requires the
  full range. The receipt identifier received an exact-byte helper as a related
  hardening measure; the ordinary `String.range` identifier pattern did not
  reproduce the terminal-suffix acceptance on this toolchain.

## Independent coverage and result

`ReceiptRedemptionUltraReviewTests` adds 12 fake-only adversarial cases:

- invalid current-key signature cannot write global or nonce state;
- a differently signed receipt at the same nonce requires recovery before a
  current-key read;
- expiry during the second verification leaves a durable quarantine and blocks
  retry;
- a lost global-intent readback or erased nonce after intent reservation remains
  recovery-required after coordinator reconstruction;
- cancellation after verification two cannot promote the nonce or clear the
  global intent;
- state transitions cannot overwrite an existing intent or clear `READY`
  directly;
- `PENDING` paired with `READY`, and an intent for another binding, block before
  verification or mutation;
- an exact permit rejects a different binding without consuming its one valid
  claim;
- deleting global state after one redemption cannot reset rollback protection
  for a different nonce;
- receipt identifiers and canonical timestamp parsing reject LF, CRLF, and
  U+2028 suffixes; and
- permit serialization remains unavailable.

The focused native group passed 29 tests: eight Core state tests, nine base
coordinator tests, and 12 independent tests.

```sh
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m31-ultra-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m31-ultra-module-cache \
swift test --package-path native/macos-approval-host \
  --scratch-path /private/tmp/adhelm-m31-ultra-build \
  --filter 'ReceiptRedemption(State|Coordinator|UltraReview)Tests'
```

The generated Python/native vector suite passed six tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache \
uv run pytest -q tests/test_receipt_redemption_vectors.py
```

`swift-format lint --strict` passed for the independent Swift test and
`git diff --check` passed.

The root then validated exact source
`9691d266b9fe246c0f7de54ead728e6e86f7df66`: all 162 native tests and ten
unconfigured-launch checks passed, as did 2,634 Python tests and 1,041 Python
ultra regressions. Two native builds of that source matched byte for byte.
The first full Python run caught the independent redemption fixture placed in
the older safety generator's directory. The fixture and generator now have a
dedicated `Fixtures/ReceiptRedemption` directory; the existing ownership
assertion remains intact. The development validation record
retains that failed attempt and the passing exact-source evidence.

## Limits retained by the contract

M31 supplies no protected-store adapter, initializer, migration, durable CAS
implementation, native-to-Python handoff, XPC method, CLI, or execution path.
Its collaborator contract does not claim an atomic transaction across global
and nonce records or detection of arbitrary restoration of an earlier valid
global `READY` record together with loss of a nonce record. A production
adapter needs a rollback-resistant protected-store anchor before it can claim
that stronger property. `VERIFIED` and the permit remain point-in-time local
evidence, never current trust, remote authority, or a provider-execution grant.

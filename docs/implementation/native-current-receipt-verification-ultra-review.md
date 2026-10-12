# Native current-receipt verification ultra review

This independent review covers the internal fixed-slot current-receipt path in
`PinnedReceiptVerifier` and `SecurityProtectedKey`. It does not treat the
foundation as an installed approval host, durable grant, replay-consumption
mechanism, provider authorization path, or cross-process revocation barrier.

## Resolved findings

| Severity | Reproduced condition | Final repair |
| --- | --- | --- |
| P1 | A caller-provided `now` value could remain valid while the coordinator waited for its lease or a protected read, allowing a receipt to be returned after the real deadline. | The verifier entry points no longer accept a caller timestamp. The coordinator owns an injected trusted clock, samples it before preflight, after acquiring the lease, before protected reads, before CryptoKit verification, and before return. Every sample must be finite, nondecreasing, and strictly before expiry. |
| P1 | A continuation callback could delay until expiry after an earlier clock sample, then allow provenance or current-key reads. | Each continuation gate now precedes an immediate fresh clock sample at the relevant handoff. Expiry stops before the next protected read. |
| P1 | The post-key sample compared against the provenance-read sample rather than the newer pre-key sample, allowing a backward clock step to escape the nondecreasing check. | The post-key sample now requires monotonicity from `beforeKeyReadNow`. |

The final source performs pure canonical/challenge/signature-encoding preflight
before code identity, provenance, or key access. It copies the bounded receipt
into one local value snapshot before preflight, then uses that same snapshot for
the final cryptographic check. The current X9.63 bytes are read through an
internal fixed-slot adapter, matched to `ACTIVE` provenance, and used
immediately as the CryptoKit verification key under the same local lease as
signing and revocation.

## Independent regressions

`CurrentReceiptVerificationUltraReviewTests.swift` adds seven fake-only XCTest
cases:

- an exactly typed but nonce-rebound challenge fails before code identity,
  provenance, or key access;
- nonfinite trusted-clock values fail before code identity or any protected
  read;
- cancellation after code identity prevents every protected read;
- advancing the trusted clock from a continuation callback stops before the
  provenance read and, separately, before the current-key read;
- a backward clock step after the key read cannot produce a receipt;
- malformed X9.63 bytes and a private store error become the closed public
  error, then a later request proves the lease was released; and
- an invalid-signature verification holds the in-process lease until it fails,
  a concurrent revoke follows it, and a later verification sees `REVOKED`.

All fakes are in-memory. The suite creates only ephemeral software CryptoKit
P-256 keys. It does not instantiate `LAContext`, access Keychain or Security,
display AppKit, start XPC, contact a provider, or read a credential.

## Boundary assessment

The concrete `SecurityProtectedKeyStore` seam accepts only
`LocalAuthenticationApprovalSession` and invalidates it with `defer` on both
success and failure. The coordinator's opaque session parameter exists solely
for in-memory test injection; it does not claim to prove OS-session freshness.
The review did not execute Local Authentication or Keychain code.

The coordinator lock serializes this host process only. A returned
`SignedApprovalReceipt` is a point-in-time verification result, never a
reusable current-trust token. A later durable acceptance path must independently
recheck current trust, replay consumption, policy state, and authorization in
its own transaction. The implementation makes no cross-process atomicity,
currentness-after-return, protected-ACL introspection, or hostile-same-user
claim.

No further actionable issue was found in the reviewed final source.

## Validation

The independent suite passed seven tests with the normal SwiftPM sandbox enabled:

~~~sh
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-current-receipt-ultra-review/clang \
swift test --package-path native/macos-approval-host \
  --disable-keychain --disable-netrc --skip-update \
  --disable-automatic-resolution --enable-xctest --disable-swift-testing \
  --filter CurrentReceiptVerificationUltraReviewTests \
  --scratch-path /private/tmp/adhelm-current-receipt-ultra-review/build \
  --cache-path /private/tmp/adhelm-current-receipt-ultra-review/cache \
  --config-path /private/tmp/adhelm-current-receipt-ultra-review/config \
  --security-path /private/tmp/adhelm-current-receipt-ultra-review/security \
  --manifest-cache local \
  -Xswiftc -module-cache-path \
  -Xswiftc /private/tmp/adhelm-current-receipt-ultra-review/modules
~~~

`xcrun swift-format lint --strict` and `git diff --check` pass for the
independent test file. The desktop outer sandbox required the normal approved
SwiftPM invocation; SwiftPM's own sandbox stayed enabled.

The adjacent current-receipt and core receipt group also passed 27 tests:
`CurrentReceiptVerificationTests`, this independent suite,
`ReceiptVerificationTests`, and `ReceiptVerifierUltraReviewTests`.

# Native current-receipt verification

This increment adds an internal macOS-host path that verifies one already
typed approval receipt against the **currently observed** public key of the
one fixed enrollment slot. It is a point-in-time local check. It is not an
installed trust root, a durable approval grant, a nonce-consumption protocol,
or a provider authorization path.

The implementation is confined to:

- `native/macos-approval-host/Sources/ApprovalHostCore/PinnedReceiptVerifier.swift`
- `native/macos-approval-host/Sources/ApprovalHostMacOS/SecurityProtectedKey.swift`
- `native/macos-approval-host/Tests/ApprovalHostMacOSTests/CurrentReceiptVerificationTests.swift`

## Closed internal interfaces

`PinnedReceiptVerifier.preflight` has package visibility and returns `Void`.
It checks the receipt's 16 KiB canonical JSON bound, exact expected-challenge
binding, fixed host/key configuration binding, receipt time, and canonical
signature encoding. It performs no public-key lookup or signature verification
and cannot produce authority.

`InitialEnrollmentCoordinator.verifyCurrentReceipt(receiptData:expectedChallenge:session:shouldContinue:)`
uses that pure preflight before it touches code identity, provenance, or a key.
It first copies the bounded receipt bytes into a local value snapshot, so the
later signature check uses the same bytes that preflight examined. The supplied
expected challenge is paired with an internal fakeable coordinator clock, never
a caller-provided timestamp. The coordinator samples that clock before
preflight, before protected provenance/key reads, before CryptoKit
verification, and before return. Samples must be finite, nondecreasing, and
strictly before the challenge expiry. This still makes the returned result a
point-in-time local observation, not a result that remains current after it
returns.
While the fixed-slot coordinator lease is held, it then:

1. validates the configured host code-signing requirement;
2. requires the exact fixed enrollment intent and an `ACTIVE` provenance
   record, rejecting missing, `PENDING`, `REVOKED`, malformed, and mismatched
   states;
3. observes the current private slot's P-256 public X9.63 bytes through the
   narrow internal key adapter and matches their SHA-256 fingerprint to
   `ACTIVE` provenance; and
4. constructs `PinnedReceiptVerifier` from those observed bytes and verifies
   the receipt's real CryptoKit P-256 signature against the supplied typed
   challenge at the final trusted clock sample.

The `SecurityProtectedKeyStore` forwarding seam accepts only a concrete
`LocalAuthenticationApprovalSession`; it invalidates that session with `defer`
on both success and failure. It does not create or authenticate an LAContext.
An installed controller must provide the session immediately after its own
fresh-authentication transition. The injection-friendly coordinator method
accepts an opaque session only for fake-only tests and does not independently
prove OS-session freshness.

Cancellation is checked before preflight, before protected reads, before the
current-key read, and before a verified receipt is returned. Parser, signature,
session/key-read, cancellation, and configuration failures are normalized to a
closed local protected-store failure. No raw Security, parser, or CryptoKit
detail leaves this boundary.

## Lease and revocation scope

Verification and the existing fixed-slot `revoke` operation share one
coordinator lease within one host process. A revoke therefore cannot interleave
between the provenance/key checks and cryptographic verification. The lease is
released on every outcome.

This is not cross-process atomicity. Another entitled process can still alter a
protected item outside this process after an observation. A returned
`SignedApprovalReceipt` is not a reusable current-trust token and must not be
cached across a later revocation. A future durable acceptance/grant path must
perform its own current-trust, replay-consumption, policy-revocation, and
authorization checks in its own transaction.
Local clock samples do not turn this in-process lease into an atomic
cross-process currentness claim.

The checked-in executable remains unconfigured. This increment does not add a
client-pin installation mechanism, a Python callback, a new XPC operation, a
generic public-key lookup, arbitrary verification/signing, an enrollment
ceremony, or any claim about installed host identity or cross-process OS trust.

## Offline validation

The focused suite uses only in-memory provenance, code-identity, session, and
key adapters plus ephemeral software P-256 keys. It validates active-key
success, malformed/expired input before protected reads, `PENDING`/`REVOKED`/
replacement rejection, code-identity and session-read failure normalization,
cancellation, cryptographic wrong-key rejection, and in-process
verify-versus-revoke serialization. It does not create an LAContext, access
Keychain/Security, display AppKit, connect XPC, invoke a provider, or use a
credential.

~~~sh
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-current-receipt-verification/clang \
swift test --package-path native/macos-approval-host \
  --disable-keychain --disable-netrc --skip-update \
  --disable-automatic-resolution --enable-xctest --disable-swift-testing \
  --filter CurrentReceiptVerificationTests \
  --scratch-path /private/tmp/adhelm-current-receipt-verification/build \
  --cache-path /private/tmp/adhelm-current-receipt-verification/cache \
  --config-path /private/tmp/adhelm-current-receipt-verification/config \
  --security-path /private/tmp/adhelm-current-receipt-verification/security \
  --manifest-cache local \
  -Xswiftc -module-cache-path \
  -Xswiftc /private/tmp/adhelm-current-receipt-verification/modules
~~~

SwiftPM's normal sandbox remains enabled. On this desktop environment, the
outer agent sandbox may require separately approved execution for that manifest
sandbox. The command uses isolated temporary build state and does not invoke
real OS protected operations.

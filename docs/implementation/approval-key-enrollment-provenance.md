# Fixed-slot enrollment provenance implementation

This increment adds a closed initial-enrollment record and a native macOS
coordinator for the one configured approval-host signing-key slot. It is not an
installed approval host, an enrollment ceremony, a client-pin installation, or
an authorization grant.

The implementation lives in:

- native/macos-approval-host/Sources/ApprovalHostCore/InitialEnrollmentProvenance.swift
- native/macos-approval-host/Sources/ApprovalHostCore/TrustedConfiguration.swift
- native/macos-approval-host/Sources/ApprovalHostMacOS/SecurityProtectedKey.swift
- native/macos-approval-host/Sources/ApprovalHostMacOS/AppKitReview.swift

The default executable remains unconfigured and refuses launch. Tests inject
in-memory code-identity, provenance, key, timestamp, and identifier adapters.
They do not invoke Security, Keychain, LocalAuthentication, AppKit, XPC, a
provider, or a credential.

## Implemented boundary

The core codec accepts only canonical, bounded JSON state forms:

| State | Required facts | Signing result |
| --- | --- | --- |
| PENDING | transaction and fixed host/key/storage intent | denied; recovery required |
| ACTIVE | PENDING facts plus observed public-key fingerprint and creation time | available only if the configured observable key matches |
| REVOKED | ACTIVE facts plus revocation time | denied |

The macOS coordinator registry shares one lease for one exact fixed-slot
configuration, serializing sign, enroll, and revoke within that host process.
An incompatible configuration claiming the same slot is refused before a
protected operation rather than receiving another lease.
It validates the configured host code identity before local enrollment/revocation
UI and again before protected operations. It records PENDING before key creation,
does not retry/delete/adopt after ambiguity, and records REVOKED without deleting
the key.

For typed receipt signing, permit consumption occurs while the coordinator lease
is held immediately before the first protected provenance/key read. The
coordinator then rechecks ACTIVE state, fixed host/key/storage values, and the
observed X9.63 public-key SHA-256 fingerprint; the Security adapter reloads the
same key and fingerprint immediately before signature creation.

This is not a cross-process transaction or revocation barrier. A different
process with the same entitlement can alter a protected item after the local
coordinator has read it; this increment does not claim atomic observation of
that external change.

The real Security adapter uses fixed selectors and supported APIs for code
identity, provenance records, P-256 key generation, key attributes, public-key
X9.63 export, and receipt signature creation. It exposes no public SecKey getter
or arbitrary sign-bytes endpoint. Enrollment-controller construction now belongs
to the package-scoped preflighted runtime's `makeEnrollmentController()` method;
the former public factory accepting a caller-selected configuration is removed.
The runtime shares its fixed protected store with the explicit AppKit
enrollment/revocation actions, rejects an absent provenance selector or inactive
runtime before controller creation, and exposes no store or direct key operation
to product callers. See [installed bootstrap](native-installed-bootstrap.md)
for startup and runtime-lifetime checks. The checked-in executable still has no
trusted deployment constants and refuses launch.

## Important limits

Public macOS Security APIs do not expose a way to inspect the full policy of an
existing opaque SecAccessControl. ACTIVE provenance therefore proves only that
this implementation requested the audited creation parameters at enrollment and
that the same public key is later observed. It does not prove a current ACL,
hardware attestation, or hostile-same-user protection.

SecKeyCreateRandomKey documents its key attributes but no LAContext argument.
The coordinator requires the one fresh authentication session for the
transaction, writes PENDING with that context before generation, and passes the
same context to subsequent Keychain record/key reads and updates with UI
disabled. The implementation does not claim that the generation call itself
provides an independent authentication attestation.

Rotation, replacement, orphan recovery, deletion, client public-key pinning,
receipt-v2 enrollment binding, durable grant recording, and cross-process
coordination are explicitly unsupported. Any existing PENDING, ACTIVE, REVOKED,
or unproven key stops a new enrollment before key creation.

## Offline reproduction

Run the focused fake-only suite with SwiftPM state outside the repository:

~~~sh
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-enrollment-provenance/clang \
swift test --package-path native/macos-approval-host \
  --disable-keychain --disable-netrc --skip-update \
  --disable-automatic-resolution --enable-xctest --disable-swift-testing \
  --filter 'InitialEnrollmentProvenanceTests|InitialEnrollmentCoordinatorTests|SecurityProtectedKeyTests' \
  --scratch-path /private/tmp/adhelm-enrollment-provenance/build \
  --cache-path /private/tmp/adhelm-enrollment-provenance/cache \
  --config-path /private/tmp/adhelm-enrollment-provenance/config \
  --security-path /private/tmp/adhelm-enrollment-provenance/security \
  --manifest-cache local \
  -Xswiftc -module-cache-path \
  -Xswiftc /private/tmp/adhelm-enrollment-provenance/modules
~~~

SwiftPM's own sandbox remains enabled. On this desktop environment the outer
agent sandbox may require approved execution for SwiftPM's manifest sandbox;
that is separate from the package's Security, UI, authentication, XPC, and
provider operations, none of which this command invokes.

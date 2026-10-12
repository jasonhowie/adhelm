# Native bootstrap ultra review

## Scope

This independent review covers the M29 installed-native bootstrap boundary:
the static `ApprovalHostBuildMetadataV1`, its seven-field protocol projection,
strict protocol limits, requirement preflight, current-host identity check, and
the retained listener runtime. It uses only injected fake requirement,
identity, store, engine, listener, and lifecycle seams. It does not invoke
Security, Keychain, LocalAuthentication, AppKit, XPC, installation, signing,
or a provider.

## Required review boundaries

- Build metadata must be static product data, never decoded from a request,
  argument, environment variable, local file, or generic runtime constructor.
- The protocol projection must retain exactly the Python-compatible host/key
  references and five exact bounded integer limits. Its digest is evidence for
  that projection only; it does not attest Mach service names, code
  requirements, Keychain selectors, storage choice, provenance selectors, or
  a public-key pin.
- Requirement compilation for both the client and host, followed by current
  host identity validation, must succeed before a store, engine, listener,
  UI, or key operation can be constructed.
- Fixed selectors and requirements must reject placeholders, wildcards,
  swapped domains, unsafe control characters, and limit aliases or values
  outside the v1 Python range.
- A process can retain and activate one runtime once. Failure must release the
  local lifecycle state, while this in-process lifecycle does not claim a
  cross-process listener, revocation, or Keychain transaction barrier.
- The checked-in foundation executable must remain unconfigured and refuse
  launch.

## Finding fixed before review freeze

The first runtime-gate draft checked active state before delegating to the
underlying protected store, but did not hold that check through the delegated
operation. An old operation could therefore be blocked after its last gate
check while shutdown released the process lease and a replacement runtime
constructed a second store. The final implementation makes a lease terminal
after any activated runtime shuts down. It invalidates the gate and listener
first, then refuses every replacement bootstrap until process restart. A
module-internal XCTest reset accepts only idle or terminal state, so it cannot
clear a live product claim.

This is deliberately conservative. It prevents an in-flight old store call
from overlapping a new runtime in the same process. It does not claim to
cancel an operation already inside an underlying OS store, provide a
cross-process mutex, or establish durable revocation.

## Independent coverage and result

`NativeBootstrapUltraReviewTests` adds five fake-only adversarial cases:

- exact Python default canonical bytes and digest, including the intentional
  seven-field-only digest scope;
- wildcard, placeholder, requirement-grammar injection, role-confusion, and
  fractional/non-finite limit rejection before preflight;
- normalization of an arbitrary preflight error to the fixed public
  `hostNotInstalled` result before any factory construction;
- shutdown while a fake underlying store call is deliberately blocked, proving
  that neither an immediate nor later replacement bootstrap constructs a
  second store, engine, or listener; and
- two concurrent fake bootstrap attempts, proving that exactly one runtime and
  one construction chain win the process lease.

The final focused command passed 20 tests: four Core metadata/vector tests,
11 bootstrap tests, and five independent review tests.

```sh
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m29-ultra-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m29-ultra-module-cache \
swift test --package-path native/macos-approval-host \
  --enable-xctest --disable-swift-testing \
  --scratch-path /private/tmp/adhelm-m29-ultra-build \
  --filter 'NativeBootstrapUltraReviewTests|InstalledApprovalHostBootstrapTests|ApprovalHostBuildMetadataTests'
```

SwiftPM's normal sandbox remained enabled. The desktop outer sandbox cannot
start SwiftPM's manifest sandbox, so this command used the approved outer test
boundary. The selected tests exercised only in-memory fakes and did not call
Security, Keychain, LocalAuthentication, AppKit UI, XPC, installation,
signing, or a provider.

`swift-format lint --strict` passed for the independent Swift file, and
`git diff --check` passed. No remaining concrete issue was found in the frozen
M29 core/bootstrap boundary.

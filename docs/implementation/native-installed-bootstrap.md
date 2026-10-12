# Native installed-bootstrap boundary

This increment adds a source-only, package-scoped bootstrap for a future signed
macOS approval-host product. It is not an installed service, a signed product,
a Mach-service registration, a Keychain enrollment, an OS-authentication flow,
or a provider-execution path. The checked-in `ApprovalHostApp` continues to
call `ApprovalHostInstallation.refuseUnconfiguredLaunch()` and exits without
loading configuration from an argument, environment variable, file, request,
or XPC message.

## Static build metadata

`ApprovalHostBuildMetadataV1` and its code-identity selectors have `package`
visibility. A future product target in this package must supply reviewed static
Swift constants; neither type is `Codable` or has a runtime loader. Metadata
contains the fixed host/key references, Mach service, fixed signing-key and
provenance selectors, storage choice, bounded protocol limits, and distinct
role-tagged host/client code-identity selectors.

Metadata accepts no raw code-requirement expression. A selector has an exact
role, reverse-DNS bundle identifier, and ten-character team identifier. It
generates only:

```text
anchor apple generic and identifier "<bundle-id>" and certificate leaf[subject.OU] = "<team-id>"
```

Role swaps, duplicate host/client bundle selectors, unsafe characters,
wildcards, placeholders, and arbitrary requirement grammar fail before the
bootstrap can reach Security APIs. `makeTrustedHostConfiguration()` also
revalidates the fixed selectors and produces the narrow seven-field
Python-compatible protocol projection: host/key references and the five exact
integer limits. The projection digest does not attest a Mach service, code
identity, key selector, enrollment selector, OS installation, public-key pin,
or authorization.

The strict v1 limits are:

| Field | Accepted range |
| --- | --- |
| Challenge bytes | integer 1–65,536 |
| Challenge lifetime | integral finite seconds 1–300 |
| Issue-time skew | integral finite seconds 0–300 |
| Subplans | integer 1–6 |
| Policy items | integer 1–64 |

## Bootstrap ordering and runtime ownership

`ApprovalHostInstallation.bootstrapInstalledHost(metadata:)` is package scoped.
Its product entry takes only the static metadata object and does this in order:

1. rebuilds the strict trusted configuration;
2. compiles the generated host requirement;
3. compiles the generated client requirement;
4. validates the current host `SecCode` against the compiled host requirement;
5. claims one process-local bootstrap lease;
6. constructs the fixed protected store, engine, and authenticated listener;
7. activates that listener once; and
8. returns one `InstalledApprovalHostRuntime` that retains the configuration,
   guarded store, engine, and listener.

The preflight happens before construction of the protected store, review
adapter, engine, or listener. The injected preflight/factory overload is
module-internal for `@testable` XCTest only, not an installed-product API.
`AuthenticatedApprovalXPCListener` construction and lifecycle are
module-internal, so even another target in this package cannot bypass bootstrap
with a caller-selected configuration or engine.

The process-local lease refuses a second bootstrap while the first is starting
or active. A construction failure releases only its own lease after the partial
component scope exits. After an activated runtime shuts down, its process-local
lease becomes terminal: `shutdown()` invalidates the runtime operation gate,
then the listener, then prevents replacement bootstrap until process restart.
This conservative rule avoids a new runtime overlapping an old protected
operation that passed its final continuation callback. Shutdown is idempotent
and does not delete a protected key, alter enrollment provenance, infer
recovery, or reactivate a listener. The lease is not a cross-process mutex,
installation claim, Keychain transaction, or revocation barrier.

The runtime's existing-store wrapper stops controllers and approval flows from
an older runtime generation from starting a new protected operation after
shutdown. It delegates to the existing fixed-slot coordinator and combines the
existing cancellation callback with the runtime gate; it does not introduce a
second key API or claim protection from hostile code in the same process.

An explicit AppKit enrollment controller can be made only by an active,
preflighted runtime and shares its guarded fixed store. Constructing that
controller does not perform an OS authentication or Keychain operation. A
controller retained from a shut-down runtime fails closed at the guarded store.
The process cannot bootstrap a replacement runtime after shutdown.

## Limits and remaining prerequisites

This is an in-process source boundary only. It does not establish a trusted
installed Python-to-XPC client bridge, a durable current-revocation resolver,
cross-process atomicity, a public-key pin handoff, a signed build, an app bundle,
an entitlement, a registered Mach service, notarization, an enrollment
ceremony, a Keychain result, user presence, receipt acceptance, or remote
execution authority. Native approval remains
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED` until those separate gates have
evidence.

## Focused validation

The focused XCTest suite uses injected requirement, host-identity, store,
engine, and listener fakes. It checks strict ordering; independently failed
host/client compilation and host validation; host-versus-client role binding;
single-runtime leasing; construction-failure release; listener-invalidating
shutdown ordering; stale-generation store rejection; enrollment-controller
gating; and continued unconfigured foundation launch. It makes no Security,
Keychain, LocalAuthentication, AppKit UI, XPC, provider, credential, signing,
or installation call.

```sh
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m29-bootstrap-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m29-bootstrap-module-cache \
swift test --package-path native/macos-approval-host \
  --enable-xctest --disable-swift-testing \
  --scratch-path /private/tmp/adhelm-m29-bootstrap-build \
  --filter InstalledApprovalHostBootstrapTests
```

SwiftPM's own sandbox remains enabled. On this desktop host, Codex's outer
sandbox cannot launch SwiftPM's manifest sandbox, so the identical focused
command required an approved outer execution boundary. That did not disable
SwiftPM sandboxing or invoke the real OS interfaces above.

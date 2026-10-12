# ADR 0003: fixed native host metadata and unsigned development artifacts

Accepted 2026-09-05 for the macOS implementation. Release remains IMPLEMENTING.

The native approval host is a separate app target with compiled host/client
identity selectors and protocol limits. A closed build document generates Swift
constants only inside an archived source tree. There is no runtime configuration
loader. The checked-in metadata throws `APPROVAL_HOST_NOT_INSTALLED`, so normal
source builds refuse before accessing the desktop, Security, Keychain or XPC.

The build inspector links only ApprovalHostCore. It accepts no arguments and
prints the canonical descriptor derived from its compiled constants. The builder
compares those exact bytes with the independently calculated Python descriptor.
It never runs the configured host. Neither descriptor establishes installation,
signing identity, key enrollment, current protected state or approval authority.

The host app contains its executable and one fixed LaunchAgent plist under
`Contents/Library/LaunchAgents`. `BundleProgram` is relative to the containing
app. This follows Apple's documented bundle layout for
[SMAppService helpers](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos).
Registration is a separate operation: Apple's
[register API](https://developer.apple.com/documentation/servicemanagement/smappservice/register())
can immediately bootstrap a LaunchAgent. The artifact builder therefore does
not register, install or start a service.

When separately signed and registered, the configured app requires the current
user's interactive desktop, compiles both generated code requirements, and
validates its own fixed identity before constructing the runtime or AppKit UI.
Its status menu exposes explicit key setup and revocation. An incoming approval
request has no key-management endpoint. A shared runtime admission slot excludes
simultaneous remote approval and local key-management prompts. Shutdown closes
admission, cancels accepted connections and terminates the process runtime;
restarting that runtime in the same process is forbidden.

Request response delivery and interaction cleanup are distinct. A synchronous
review/authentication/signing adapter can complete before returning its
cancellation handle. The engine retains its admission until these handoffs
return and terminal cancellation/session cleanup has been initiated. Final gate
release follows queued modal cleanup on the main queue. This is an in-process
ordering guarantee, not an OS attestation or a cross-process Keychain transaction.

Build-time entitlement output contains only an explicitly declared Keychain
access group. No App Sandbox, app-group, JIT, dynamic-loader or network exception
is inferred. Release signing still needs a reviewed hardened-runtime profile,
as described by Apple's
[hardened runtime documentation](https://developer.apple.com/documentation/xcode/configuring-the-hardened-runtime/).
The development artifact is not a signed release or a notarization submission.

The existing Python caller and native receipt verifier still need a protected
installed trust handoff and current-revocation proof. This increment does not
generate a client stub, accept a plaintext trust pin, add a generic XPC method,
or install an execution command that treats a fixture receipt as authorization.
Real identity, credential-store, fresh-authentication, XPC, release distribution
and live-provider evidence remain separate required work.

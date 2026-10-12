# Native approval bridge session handoff

The dedicated native bridge route is a package-internal, synchronous handoff
from a reviewed approval request to one M33 receipt-redemption operation. It
exists only for the direct `OPENAI_PAUSE_PLAN` and `GA4_KEY_EVENT_CREATE_PLAN`
kinds. It does not add a provider executor, a Python authorization grant, a
generic receipt consumer, or a public XPC method for protected redemption.

## Admission and output

`ApprovalHostXPCService.requestNativeApprovalBridge` first decodes the closed
M32 request, binds it to compiled build metadata and the exchange-start time,
and builds the exact post-signing consumer. These checks happen before the
interaction gate, review UI, fresh authentication, signing, protected-store
access, or native nonce reservation. The established raw
`requestApproval` endpoint always calls the ordinary receipt path and never
receives that consumer.

The package-only `AuthenticatedNativeApprovalBridgeClient` takes a typed M32
request and returns `NativeApprovalBridgeXPCResponse`: either one fully
revalidated candidate or a fixed public failure. It has no raw-receipt,
session, public-key, permit, transport-selector, host-requirement, or generic
XPC constructor. A candidate is checked again against the exact request,
compiled metadata, exchange-start time, and local clock before delivery. It is
evidence syntax, not provider authority or a reusable execution token.

## Session lifetime

After fresh review, authentication, and receipt signing succeed, the engine
creates an `ApprovalPostSigningSessionLease`. The lease exposes the exact
authenticated session only inside one synchronous callback,
`withAuthenticatedSession`. It is one-use, owner-thread-bound, non-Codable,
and has no session getter. Cancellation, deadline expiry, or another terminal
failure closes `shouldContinue()` immediately.

The engine deliberately waits to invalidate the underlying fresh session and
to invoke `interactionFinished` until that callback returns. This avoids
racing an in-progress protected M33 operation with session invalidation. A
candidate held in the deferred handoff window is discarded if cancellation or
expiry wins before delivery. The engine performs a final continuation and
expiry check after the consumer returns. The continuation callback is sampled
outside the lease lock, so terminal cleanup cannot invert its request and lease
lock order; protected work checks the lease again at each boundary.

The engine also supports a signer that invokes its completion synchronously.
The concrete protected signer must release any protected-store operation lock
before invoking that completion; the handoff itself does not broaden the
signer collaborator contract or make arbitrary synchronous signer behavior a
supported protected-store nesting model.

`NativeApprovalBridgePostSigningConsumer` passes that same session to the
session-bound M33 coordinator. It validates the exact expected challenge,
metadata, and request before using the lease; it does not recreate,
serialize, cache, or independently authenticate a session. The concrete
installed factory derives only fixed selectors from reviewed metadata. Missing
global redemption state remains a fail-closed M33 result: this route never
initializes, migrates, deletes, or recovers protected state.

## Runtime binding and limits

The configured installed-host factory creates the bridge consumer factory
from the already bootstrapped `SecurityProtectedKeyStore`, the exact reviewed
metadata, and the active runtime operation gate. Construction derives fixed
Keychain selectors only. It does not create an `LAContext`, query Keychain,
show UI, verify a receipt, consume a nonce, launch a helper, or perform an XPC
request.

The route remains unwired from any public CLI capability. Tests use in-memory
review, authentication, signing, M33 slots, transport, and clock fakes; they
do not call Keychain, LocalAuthentication, XPC, a helper process, a provider,
or a signed/installed host. The resulting unsigned development artifacts do
not establish native installation, current host identity, protected-store
availability, user approval, provider permission, ledger authority, or
provider execution. Python's existing safety and execution path remains the
only provider-execution owner.

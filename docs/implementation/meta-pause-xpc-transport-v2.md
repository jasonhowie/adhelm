# Private Meta campaign PAUSE XPC transport V2

This M47 slice adds a package-only XPC selector for the closed Meta campaign
PAUSE V2 request and candidate codecs. It is prepared transport code only. It
does not install or start a host, create a helper executable, access
LocalAuthentication or a protected store, contact Meta, or grant execution
authority.

`ApprovalHostXPCProtocol` now has three fixed operations:

- `requestApproval` remains the raw-receipt route and rejects a Meta subject
  before an interaction lease, review, authentication, signing, or a
  post-signing consumer.
- `requestNativeApprovalBridge` remains the M32/M33 OpenAI/GA4 route. Its
  canonical request, candidate bytes, errors, and admission order are not
  changed.
- `requestNativeMetaCampaignPauseApprovalBridge` accepts only a canonical
  `NativeMetaCampaignPauseApprovalBridgeRequestV2` and returns only a bounded
  `NativeMetaCampaignPauseApprovalBridgeCandidateV2` or a compact existing
  public failure.

The V2 client, wire response, transport, post-signing factory protocol, and
server completion path have separate Meta-specific types. The client rebuilds
the typed request before it creates a transport, then validates a reply against
the exact request, held metadata, exchange time, and current time. It permits
one outstanding request, one reply, and one candidate. Timeout or local
invalidation invalidates the fixed transport and ignores later replies.

The installed listener already pins the fixed Mach service, peer UID, and
client code-signing requirement. For the new selector it supplies the held
engine protocol-configuration digest to each service. Before acquiring the
interaction gate, the service requires an optional V2 factory, decodes the V2
request, binds it to the factory's held metadata, and requires the request
protocol digest to equal the listener-held engine digest. The engine repeats
its own V2 reconstruction and protocol check before review. The M46 consumer
retains the authenticated session only through synchronous durable receipt and
nonce consumption; cancellation or expiry after that handoff returns no
candidate and does not retry.

The V2 factory is optional and defaults to absent. A service constructed
without both the factory and listener-held protocol digest fails closed before
the interaction gate. This is a testable construction boundary, not evidence
that an injected factory is an installed host, OS provenance, or a protected
store.

The future fixed helper path is reserved as
`/Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-meta-campaign-pause-approval-client`.
This slice does not create, package, stage, execute, or bind that helper. A
later reviewed slice must add it as a separately fixed product and keep the
existing V1 helper path and selector untouched.

`NativeMetaCampaignPauseApprovalBridgeXPCTests` uses only in-memory request,
candidate, consumer, reviewer, authentication, signer, gate, scheduler, and
transport fakes. It covers exact candidate binding, V1/V2 cross-route refusal,
raw-route preservation, missing factory or held digest refusal, factory/engine
protocol mismatch before the gate, timeout, repeated replies, cancellation,
and session-bound candidate completion. It makes no XPC, helper, Keychain,
LocalAuthentication, or provider call.

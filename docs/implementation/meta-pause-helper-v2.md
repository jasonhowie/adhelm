# Private Meta campaign PAUSE helper V2

M48 adds the separate native executable target
`adhelm-macos-meta-campaign-pause-approval-client` and its package-only helper
runner, `NativeMetaCampaignPauseApprovalBridgeHelperRunner`. This is fixed V2
transport preparation. It does not wire an installed runtime factory, create a
host or helper installation, access a provider, or grant Meta execution
authority. A future configured request can activate the fixed Mach service;
M48 makes no claim that the helper itself prevents that service activation.

The existing `adhelm-macos-approval-client` product and its V1 selector remain
unchanged. The V2 runner accepts only
`NativeMetaCampaignPauseApprovalBridgeRequestV2`, uses only
`AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient`, and emits only a
`NativeMetaCampaignPauseApprovalBridgeCandidateV2`. It never sends V1 request
bytes to the V2 selector, accepts a raw receipt, or offers a request role,
path, endpoint, identity, environment, or command selection input.

## Fixed entrypoint

The new executable accepts no arguments. Any argument writes exactly:

```text
APPROVAL_META_CAMPAIGN_PAUSE_CLIENT_ARGUMENTS_UNSUPPORTED: This client accepts no launch arguments.
```

and exits with status 64. With no arguments, it calls the default
`GeneratedApprovalHostBuildMetadata.make()` before it constructs a runner,
checks pipe descriptors, validates code identity, or creates an XPC client.
The checked-in default metadata source remains unconfigured, so that path
returns the existing `APPROVAL_HOST_NOT_INSTALLED` diagnostic at status 78.
It cannot reach pipe, XPC, OS-authentication, protected-store, or provider
work.

A future reviewed artifact/runtime slice may place this fixed product at:

```text
/Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-meta-campaign-pause-approval-client
```

M48 does not stage, package, install, execute, or bind that path. The product
uses the existing reviewed full-18 metadata and client code-identity fields;
the distinct pathname does not introduce a caller-selected or second runtime
identity.

## One-shot exchange

The V2 runner shares only the fixed low-level descriptor, identity, clock, and
peer-liveness mechanics with the V1 helper. Request/candidate typing, client
creation, response slots, and framing roles remain separate. It requires:

- exactly `LANG=C`, `LC_ALL=C`, and `TZ=UTC` with no other environment;
- stdin, stdout, and stderr to be pipes, nonblocking stdout, and the existing
  configured client code-signing requirement;
- one 4-byte big-endian V2 request frame of at most 98,304 bytes followed by
  clean stdin EOF;
- a structurally decoded and metadata/time-bound Meta campaign PAUSE challenge
  before V2 client creation;
- one typed V2 candidate, revalidated against the exact request, held
  metadata, exchange time, and current UTC time;
- one V2 candidate frame of at most 131,072 bytes, partial writes of at most
  16 KiB, then process exit and stdout EOF.

The monotonic exchange deadline is 300 seconds. UTC time cannot move backward,
and both candidate validation and every write require the challenge to remain
unexpired. The runner checks the output peer while reading, immediately before
client construction and request, while waiting, and before every write. Once a
client exists, `defer` invalidates it on every terminal path. A lost parent,
malformed or trailing input, V1 input, identity failure, invalid candidate,
deadline, clock rollback, write inconsistency, or expiry produces no accepted
helper result and no retry.

The runner itself does not implement or expose authentication, protected-store
access, signing, nonce consumption, provider contact, or a generic pipe API. A
future configured client may delegate its one typed request to the separate
M47 XPC route; that route owns its own session, cancellation, and durable
consumption checks. Candidate bytes remain private reusable approval material,
not a raw-receipt/key retrieval API or a Meta execution permit.

## Fake-only validation

`NativeMetaCampaignPauseApprovalBridgeHelperRunnerTests` uses only synthetic
M45 V2 requests/candidates and in-memory pipe, clock, peer, identity, and
typed-client fakes. It covers exact frame/EOF output, argument/environment and
identity refusal, non-pipe/trailing/V1 input refusal before client creation,
deadline and parent-loss invalidation, rollback/expiry suppression, and
partial/overreported write failure. It does not create a process, XPC
connection, LocalAuthentication context, protected-store item, signer, or
provider request.

The M48 working-source validator also ran the hash-guarded unconfigured
products and recorded 16 refusal cases, including four for this target, in
`/private/tmp/adhelm-m48-working-native-smokes/unconfigured-artifact-targets.json`
(SHA-256
`94ea882f3d796268385cf76e066095a683ed49102d3f2ca97de0f45f6e658281`).
That is a working-tree refusal check, not exact-commit release validation. It
uses the unchanged default metadata source and confirms only that the new
entrypoint stops before runner, XPC, identity/authentication, protected-store,
or provider work; it is not configured-helper execution evidence.

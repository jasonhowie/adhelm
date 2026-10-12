# Meta campaign PAUSE native consumption V2

M46 adds one package-only native post-signing path for the existing closed
`META_CAMPAIGN_PAUSE_PLAN` V2 bridge request. It is a fake-testable
composition boundary. It does not add an XPC selector, client, helper protocol,
runtime/bootstrap binding, generated binding, CLI command, provider adapter, or
Meta Graph operation.

## Closed path

`ApprovalHostEngine.requestNativeMetaCampaignPauseApprovalBridge` accepts an
already typed `NativeMetaCampaignPauseApprovalBridgeRequestV2` and an explicit
`NativeMetaCampaignPauseApprovalBridgePostSigningConsumer`. It reconstructs
the request's canonical bytes and requires its protocol-configuration digest to
equal the engine's held `TrustedHostConfiguration` projection before it can
open review or fresh authentication. It then admits only
`META_CAMPAIGN_PAUSE_PLAN`.

This engine check proves only that seven protocol configuration facts agree
with the engine's already-held configuration. It does not select or validate
the full 18-field metadata, establish installed-host provenance, or make an
arbitrary injected consumer an OS-attested collaborator. The private
`NativeMetaCampaignPauseApprovalBridgePostSigningConsumerFactory` binds the
request to its separately held complete metadata before producing the consumer.
A future installed route must still construct that factory from reviewed static
configuration.

After exact review, fresh authentication, and signing, the engine retains the
opaque authenticated session only for the consumer's synchronous
`ApprovalPostSigningSessionLease.withAuthenticatedSession` callback. It
invalidates the session after that callback returns. Terminal cancellation and
expiry close the lease gate immediately; they do not invalidate a session while
a protected operation is in progress.

The V1 post-signing route, V1 codec bytes, helper, generated binding, raw
receipt XPC route, and their direct-kind allowlist remain unchanged. They
continue to reject Meta PAUSE. This V2 path has no XPC or helper exposure.

## Durable consumption before evidence

The V2 consumer creates a `ReceiptRedemptionCoordinator` only inside that
lease and reuses its generic `redeemUsingSession` path. Before a candidate can
be constructed, that path:

1. verifies the signed receipt against the current fixed-slot ACTIVE key;
2. writes and exactly rereads the global intent and nonce PENDING records;
3. verifies again with the same exact current key;
4. promotes the nonce to VERIFIED and the global record to READY with exact
   readback.

Only then does the one-use private permit create and reparse the 19-field V2
candidate. The permit checks the exact V2 request, complete metadata,
challenge, receipt, nonce, and returned X9.63 key again. It is not a grant,
trust object, reusable token, or provider authority.

A key mismatch before reservation leaves the global and nonce records
unchanged. Once a durable transition is attempted, cancellation, expiry,
rollback, candidate validation failure, or final callback failure returns no
candidate and remains `postReservationUnverified`. The durable state is not
undone and this component has no retry path. A later boundary must inspect and
recover before another attempt.

## Validation

The focused fake-only producer and handoff group exercises V2-only admission,
protocol-digest rejection before UI, fixed-key verification, durable
global/nonce ordering, cancellation after durable consumption, final engine
cancellation suppression, and continued V1 Meta refusal:

```text
env SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m45-module-cache \
  CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m45-clang-module-cache \
  swift test --package-path native/macos-approval-host --enable-xctest \
  --disable-swift-testing --scratch-path /private/tmp/adhelm-m45-swift-build \
  --filter 'NativeMetaCampaignPauseApprovalBridgeProducerTests|NativeMetaCampaignPauseApprovalBridgeHandoffTests'
```

The tests use synthetic P-256 keys, in-memory fixed-slot records, and the
checked-in synthetic M42 challenge. They do not access Keychain,
LocalAuthentication, XPC, a helper, a configured host, or a provider.

The focused group passed 8/0 at
`/private/tmp/adhelm-m46-focused-swift-tests.txt`; the full sandboxed native
package passed 263/0 at `/private/tmp/adhelm-m46-full-swift-tests.txt`.

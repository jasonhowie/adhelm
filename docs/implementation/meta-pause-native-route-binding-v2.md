# Meta campaign PAUSE native route binding V2

M49 adds the package-internal route-selection boundary that can later compose
the M46–M48 Meta campaign PAUSE V2 components into an installed Approval Host.
It does not add an artifact profile, configured generated source, runtime
switch, bootstrap API, CLI command, provider operation, or installation claim.

## Default source remains absent

The checked-in Core source exposes exactly one generated seam:

```swift
package enum GeneratedMetaCampaignPauseApprovalRouteBinding {
  package static func resolve(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration
  ) throws -> NativeMetaCampaignPauseApprovalRouteBindingV2?
}
```

Its ordinary-source implementation returns `nil`. It has no environment,
file, bundle, argument, manifest, or caller-selected toggle. The existing V1
build profile therefore constructs only its existing V1 post-signing factory;
the M47 V2 XPC selector still has no V2 consumer factory and fails closed.

A future separately reviewed artifact profile may replace this one source with
a static call to `NativeMetaCampaignPauseApprovalRouteBindingV2` using fixed
metadata and protocol-configuration digest literals. `nil` is the only
disabled result. A resolver error, malformed generated value, or non-`nil`
binding mismatch is `APPROVAL_HOST_NOT_INSTALLED` before either bridge factory
or protected collaborator is constructed; it never falls back to V1.

## Closed binding check

`NativeMetaCampaignPauseApprovalRouteBindingV2` accepts the held
`ApprovalHostBuildMetadataV1`, held `TrustedHostConfiguration`, and two
lowercase SHA-256 literals. At construction and again immediately before
security-factory use, it:

1. rebuilds the complete native artifact descriptor from all 18 metadata
   fields and checks its metadata digest;
2. rebuilds the seven-field protocol configuration and checks its digest;
3. requires the descriptor's protocol projection to equal the supplied
   configuration; and
4. requires the metadata-reconstructed configuration to equal the same
   supplied configuration exactly.

The binding is static build evidence only. It does not prove that the generated
profile was installed, attest code identity, open a protected store, establish
a key, authenticate a user, or authorize a Meta operation. M47 peer identity
and held-digest admission, and M46 current-key and durable nonce consumption,
remain independent checks.

## Paired security construction

`SecurityInstalledApprovalHostFactory.makeNativeBridgeListener` resolves the
fixed generated seam before asking `SecurityProtectedKeyStore` for bridge
factories. The store then rechecks the full metadata-derived configuration
against both the bootstrap configuration and its own fixed configuration before
it derives a coordinator or fixed-slot selector.

The existing
`makeNativeApprovalBridgePostSigningConsumerFactory(metadata:operationGate:)`
API remains available and delegates to the paired implementation with no V2
binding. When the generated binding is present and valid, the paired method
constructs one `SecurityNativeApprovalBridgeRedemptionFactory` over one
coordinator, one fixed-slot store, and one
`InstalledApprovalHostOperationGate`. That one object conforms to both the V1
and Meta V2 redemption-factory protocols. The V1 factory and the optional V2
factory therefore share the same shutdown boundary and cannot create
independent protected-store transaction paths.

Factory construction only derives fixed selectors. It does not query Keychain,
verify a receipt, sign, authenticate, consume a nonce, initialize redemption
state, connect to XPC, or launch a helper. Those operations remain in their
existing later V1 or V2 consumption paths.

## Preserved route boundaries

This slice does not alter V1 codec bytes, the V1 direct-kind allowlist, the raw
receipt selector, the V1 XPC selector, the helper runner, or application and
bootstrap APIs. Meta PAUSE remains rejected on every V1 and raw route. A future
explicit artifact profile must pair reviewed generated Python and Swift sources
and stage the distinct M48 helper; this package seam alone does not enable that
profile.

## Validation

The fake-only Core and MacOS tests cover default absence, exact full metadata
and protocol binding, non-protocol metadata mutation, malformed or mismatched
generated binding refusal before protected collaborators, V1-only paired
construction, and shared operation-gate invalidation across both protocol
views:

```text
env SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m45-module-cache \
  CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m45-clang-module-cache \
  swift test --package-path native/macos-approval-host \
  --scratch-path /private/tmp/adhelm-m45-swift-build \
  --filter 'NativeMetaCampaignPauseApprovalRouteBindingV2Tests|NativeMetaCampaignPauseApprovalRouteBindingRuntimeTests'
```

The focused run passed 8/0. Its fake collaborators make no Keychain query,
code-identity check, XPC connection, helper launch, LocalAuthentication
evaluation, or provider request.

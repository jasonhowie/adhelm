# Native build metadata and protocol configuration

This increment adds a closed native-core representation of reviewed approval
host build constants. It does not install an approval host, validate a macOS
signature, access Keychain, create an authentication session, start XPC, or
authorize any provider operation.

## Python-compatible protocol configuration

`TrustedHostConfiguration.approvalProtocolConfigurationV1` emits exactly the
seven non-secret fields used by Python's `ApprovalHostConfiguration.to_data()`:

- `host_ref`
- `signing_key_ref`
- `maximum_challenge_bytes`
- `maximum_challenge_lifetime_seconds`
- `maximum_issued_at_skew_seconds`
- `maximum_subplans`
- `maximum_policy_items`

The projection stores canonical RFC 8785 JSON and its SHA-256 digest. Native
tests consume vectors regenerated from the actual Python configuration model,
including defaults, both limit edges, and escaped Unicode references.

The limits reject non-finite or fractional `TimeInterval` values before a
projection can be made. Their exact v1 ranges are 1...65,536 challenge bytes,
1...300 lifetime seconds, 0...300 issue-skew seconds, 1...6 subplans, and
1...64 policy items.

The digest is local configuration evidence only. V1 challenge and receipt wire
formats continue to bind their existing host/key fields; this increment does
not add a configuration digest or limits to a signed receipt, and it does not
attest a service, code identity, key selector, provenance selector, storage
choice, or public-key pin.

## Static build metadata

`ApprovalHostBuildMetadataV1` and
`ApprovalHostCodeIdentitySelectorV1` are package-scoped immutable values. They
are unavailable to external clients and have no `Codable` conformance or
runtime loader. An installed product must provide reviewed Swift constants in
package code; the checked-in foundation supplies no metadata instance and its
executable continues to refuse unconfigured startup.

Metadata contains the opaque protocol host/key references, fixed Mach service,
fixed key and provenance selectors, storage choice, exact limits, and distinct
client and host code-identity selectors. A selector consists of a role, a
strict reverse-DNS bundle identifier, and an exact ten-character uppercase
alphanumeric Team ID. The core produces the only supported requirement form:

```text
anchor apple generic and identifier "<bundle-id>" and certificate leaf[subject.OU] = "<team-id>"
```

This generated grammar prevents metadata from accepting an arbitrary `true`,
`or`, wildcard, placeholder, or role-swapped requirement. Metadata construction
validates selector syntax and turns the constants into a
`TrustedHostConfiguration`; it does not compile a `SecRequirement` or inspect a
`SecCode`. The separately owned bootstrap must compile both generated
requirements and validate the current host identity before it creates a
protected store, engine, listener, UI, or key operation.

Protocol host/key references deliberately retain Python's bounded safe-text
grammar. Their namespaces are opaque: the projection binds their exact text but
does not prove a semantic `host:` or `key:` domain.

## Offline validation

The focused Core XCTest group is `ApprovalHostBuildMetadataTests`. It uses
only synthetic selectors, Python-generated canonical vectors, and pure
configuration construction. It checks all five limit families, the Python
canonical bytes/digests, Unicode escapes, generated role-bound requirements,
and rejection of role swaps or malformed static selectors. It performs no real
Security, Keychain, LocalAuthentication, AppKit, XPC, installation, signing,
or provider operation.

The adjacent focused Core suite was run with
`swift test --package-path native/macos-approval-host --filter ApprovalHostCoreTests`
using an isolated SwiftPM scratch/cache location: 55 tests passed with no
failures. It includes the four metadata/vector tests and existing challenge,
receipt, enrollment, and GA4 approval regressions affected by trusted limits.

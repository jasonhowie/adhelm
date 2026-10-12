# Native approval-host artifact contract

This increment provides a build-only, closed selector document for a
configured macOS approval-host development artifact. It does not sign,
notarize, install, register, launch, authenticate, access Security or
Keychain, create XPC, contact a provider, or establish approval authority.

## Closed build selector

`scripts/native_approval_artifact_contract.py` accepts exactly one JSON object
at schema version 1. Its 18 fields are:

- `schema_version`
- `host_ref`, `signing_key_ref`, and `mach_service_name`
- role-specific `client_bundle_identifier`, `client_team_identifier`,
  `host_bundle_identifier`, and `host_team_identifier`
- `signing_key_application_tag_hex`, `signing_key_access_group`, and
  `key_storage`
- `provenance_service` and `provenance_account`
- the five exact approval-host limits.

The parser accepts only a UTF-8 document up to 16 KiB, rejects duplicate,
missing, and unknown keys, and preserves original scalar types. Integer fields
reject booleans and floating-point aliases. The application tag is lowercase,
even-length hexadecimal decoded to one through 128 bytes. Access groups are
either `null` or exactly the host Team ID followed by a valid bundle ID. Bundle
identifiers, Mach service names, and Team IDs use the same narrow generated
selector grammar as native M29 metadata. Static provenance selectors reject
controls, wildcards, interpolation markers, and placeholder text.

Host/key references retain the bounded Python protocol safe-text grammar. They
are opaque selector text: this contract records exact values but does not prove
a semantic namespace, current host identity, a key, or any installation fact.

`parse_native_approval_artifact_contract_json` is the byte parser;
`parse_native_approval_artifact_contract` rebuilds an already JSON-shaped
mapping. Both return `NativeApprovalHostArtifactContractV1`. Its public
serialization path revalidates every field, so forged or incomplete dataclass
instances return the fixed contract error rather than exposing Python internals.

## Deterministic descriptor and generated source

`native_approval_artifact_descriptor` returns
`NativeApprovalHostArtifactDescriptorV1`. Its canonical JSON contains:

- the complete 18-field metadata and its SHA-256 digest;
- Python-compatible seven-field protocol configuration and its SHA-256 digest;
- exact generated client and host signing-requirement strings;
- `scope: UNSIGNED_BUILD_METADATA_ONLY_NOT_OS_ATTESTATION`; and
- `artifact_status: UNSIGNED_UNVERIFIED`.

The seven-field projection exactly matches Python
`ApprovalHostConfiguration.to_data()`: host/key reference, challenge-byte
limit, lifetime seconds, issued-at skew seconds, subplan limit, and policy-item
limit. It is local protocol-configuration evidence. It does not add a digest to
the v1 challenge or receipt wire, prove signing identity, or attest the native
host configuration.

`render_generated_approval_host_build_metadata` writes only fixed Swift syntax
for an isolated replacement of
`GeneratedApprovalHostBuildMetadata.swift`. Every input string is emitted as a
sequence of Swift Unicode scalar escapes, the tag is emitted as fixed hex byte
tokens, and storage is selected from two fixed enum cases. The renderer never
places a caller string in Swift syntax, accepts arbitrary Swift source, or
generates a loader for arguments, environment variables, files, Bundle metadata,
or network input.

## Native descriptor

`NativeApprovalHostArtifactDescriptorV1(metadata:)` lives in
`ApprovalHostCore`. It rebuilds M29 `ApprovalHostBuildMetadataV1` and
`TrustedHostConfiguration` before encoding the same descriptor shape as the
Python contract. `canonicalBytes` has no newline and is the only value the
Core-only inspector writes to standard output. The inspector accepts no
arguments and reads no filesystem, environment, or runtime configuration.

The checked-in generated metadata remains deliberately unconfigured and throws
`APPROVAL_HOST_NOT_INSTALLED`. The separate artifact builder may replace it
only inside an isolated archived source tree with source rendered from this
closed contract. A generated-source SHA is build-manifest evidence; it is not a
value supplied to the inspector and does not attest a signing identity.

No app-group, sandbox, hardened-runtime exception, client stub, launch-agent
registration, signing, or install behavior is introduced here. Those are
separate architecture and release decisions.

## Validation

Focused validation used synthetic selectors only:

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_native_approval_artifact_contract.py \
  tests/test_native_approval_artifact_contract_ultra_review.py
# 48 passed

swift test --filter NativeApprovalHostArtifactDescriptorTests
# 3 passed
```

The Python tests cover strict raw types, duplicate keys, selector role/access
binding, protocol parity, descriptor reconstruction, generated-source escaping,
and forged serializer objects. The Core tests cover the canonical metadata and
protocol projection, role-bound requirements, nullable access groups, and
separation between metadata and the seven-field protocol digest. No real OS or
provider operation occurs in either group.

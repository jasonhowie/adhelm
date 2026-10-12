# Native artifact ultra review

## Scope

This independent M30 review covers the build-only native approval-host artifact
contract, generated fixed Swift metadata, pure native descriptor, configured
host entrypoint, and deterministic unsigned archive staging. It uses synthetic
selector JSON, temporary filesystem fixtures, pure metadata inspection, and
fake-only native collaborators. It does not start an approval host, contact a
provider, access Security or Keychain, authenticate a user, create an XPC
listener, install/register a service, or sign an artifact.

## Review boundaries

- The closed build selector must preserve original JSON scalar types, reject
  duplicate or unknown fields, and never admit text that changes generated
  Swift syntax or requirement semantics.
- Generated source and the native descriptor must bind one exact selector;
  their digests are evidence for their documented surfaces only, never a
  signing, installation, current-host, key, or authority attestation.
- A configured entrypoint must derive only fixed generated metadata and finish
  both requirement preflights and current-host validation before it constructs
  a store, UI, engine, listener, or protected-key operation. The foundation
  target must continue to refuse an unconfigured launch.
- Archive staging must reject unsafe components, symlinks, hard links,
  traversal, replacement/overwrite, input mutation, and unexpected tool
  commands. Archive bytes and manifests must be deterministic and unsigned.
- No artifact-builder path may execute the approval host or accept a generic
  client/runtime configuration source.

## Results

The review reproduced and the owners repaired these boundaries:

- A synchronous review or authentication callback could complete before its
  cancellation handle returned. The Core engine now drains explicit adapter
  handoffs before it releases the shared interaction admission slot. The XPC
  service releases its lease from that terminal hook rather than response
  delivery.
- A retained XPC service could otherwise accept its first request after the
  owning connection had closed. Closing the service is now terminal and fails
  later requests before a gate or engine call.
- The local settings UI and XPC approval flow initially had separate admission
  paths. They now share a generation-bound interaction lease. A stale lease or
  controller fails before AppKit, authentication, or protected-store work.
- Ordinary path hashing could follow a final-path replacement after an earlier
  lstat. Artifact component, generated-source, source-archive, and result
  hashes now use the descriptor-safe regular-file reader.
- The first Mach-O reader accepted ARM64E while reporting exact ARM64. It now
  rejects a non-ARM64_ALL CPU subtype before recording build-target evidence.
- The builder and contract generator run from the checkout, while the artifact
  claims an archived commit. It now compares both scripts with their archived
  counterparts before and after the build, and fails closed on a mismatch.

The first real two-build reproduction attempt found different host-binary bytes:
the first binary retained 48 temporary build-path occurrences. The fixed
file-prefix map removed that broad difference, but the next pair still differed
in 24 `N_OSO` debug-STABS entries: each carried a four-to-five second timestamp
difference and a private temporary object path in the string table. The fixed
linker `-S` strips those debug STABS while retaining the ordinary loader UUID;
its inspector probe observed one `LC_UUID` and zero `N_OSO` entries. That probe
alone was not reproducibility evidence.

The independent builder test now requires one fixed
`-file-prefix-map <workspace>=/adhelm-native-build` and the one fixed linker
`-S` option, and proves the command allowlist rejects a changed map destination
or linker option. It also prohibits `-no_uuid`.
The diagnostic build showed that removing `LC_UUID` lets the normal SwiftPM
build complete but makes the Core-only inspector abort in dyld with a missing
`LC_UUID` load command. Normal SwiftPM sandboxing works; no sandbox bypass is
needed or allowed.

The root's subsequent two actual builds of
`074e3d62386435777c2592b5e1e7c9939e635e8e` produced byte-identical host
binaries, manifests, plist/entitlement templates and tar archives. The archive
SHA-256 is `6be3423f3e0606404aa2f114233f6766d108bb970c7241b4f56bb412882c5af3`.
The native reproducibility validation record
binds the exact source, synthetic configuration, toolchain, component hashes and
earlier failed attempts. Both builds executed only the Core-only inspector.
This establishes local unsigned development reproducibility for that fixed
profile, without an installed-host, signing, OS authentication or release claim.

Current independent, fake-only checks:

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_native_approval_artifact_contract.py \
  tests/test_native_approval_artifact_contract_ultra_review.py \
  tests/test_build_native_approval_artifact.py \
  tests/test_native_approval_artifact_builder_ultra_review.py
# 71 passed

SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m30-ultra-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m30-ultra-module-cache \
swift test --package-path native/macos-approval-host \
  --scratch-path /private/tmp/adhelm-m30-ultra-build \
  --filter NativeArtifactApplicationUltraReviewTests
# 11 passed

SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m30-ultra-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m30-ultra-module-cache \
swift test --package-path native/macos-approval-host \
  --scratch-path /private/tmp/adhelm-m30-ultra-build \
  --filter NativeApprovalHostArtifactDescriptorTests
# 3 passed
```

The finalized builder group includes the fixed SDK profile, Mach-O target
reader, source-provenance comparison, safe result hashing, archive staging, and
manifest checks. These tests do not run a configured host or invoke Security,
Keychain, LocalAuthentication, AppKit UI, XPC listener/connection, signing, installation,
registration, or a provider. The descriptor remains build metadata evidence;
it does not attest host behavior, code signing, installation, current host
identity, key state, or approval authority.

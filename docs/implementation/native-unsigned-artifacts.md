# Native unsigned approval-host artifacts

The development builder at
[`scripts/build_native_approval_artifact.py`](../../scripts/build_native_approval_artifact.py)
prepares one local macOS approval-host bundle from one exact Git commit and one
closed build-metadata document. It is a source build and staging tool. It does
not install, register, launch, sign, notarize, authenticate, enroll, open a
Keychain item, display approval UI, or contact a provider.

## Closed inputs and source identity

The builder accepts `--repo`, `--revision`, `--input`, `--output`, and an
optional `--source-date-epoch`. The metadata input is a bounded regular JSON
file parsed by the closed contract implementation in
[`scripts/native_approval_artifact_contract.py`](../../scripts/native_approval_artifact_contract.py).
It has no arbitrary Swift, command, toolchain, signing, path, or environment
field.

`revision` is resolved once to a full Git commit. The builder creates a Git tar
archive of that commit and extracts only bounded regular files and directories
into a private temporary tree. It rejects links, devices, FIFOs, duplicate or
non-canonical names, traversal, oversized input, and source/archive changes
during a checked read.

This development profile deliberately refuses a historical commit whose
archived `scripts/build_native_approval_artifact.py` or
`scripts/native_approval_artifact_contract.py` differs from the running files.
That prevents the running builder or generator from being silently attributed
to an older source snapshot. The selected generated Swift source is written
only in that private extracted tree. Its selected/opened/final file identity
and SHA-256 are checked before the host build and again after each build,
bin-path lookup, and inspector run.

Those checks are local source provenance evidence. They do not independently
attest that the separately compiled host binary has no additional behavior or
prove that it consumed a particular generated source file at link time.

## Fixed development tool profile

Only these executables can run:

- `/usr/bin/git` for exact commit resolution and archive creation;
- `/usr/bin/xcrun` for exactly three read-only macOS SDK probes;
- `/usr/bin/swift` for its pinned version and three exact package build forms;
- the exact Core-only inspector built in the private build directory, with no
  arguments.

The builder clears inherited compiler, SwiftPM, loader, SDK, toolchain, Git,
home, temporary-directory, and compiler-flag environment settings. It uses a
private home, cache, scratch, configuration, security, module-cache, and
temporary directory. It does not resolve dependencies automatically, access the
Keychain, or use a caller-selected Swift executable or package path.

The development profile requires the observed Xcode SDK identity:

| Field | Required value |
| --- | --- |
| SDK version | `26.2` |
| SDK build | `25C57` |
| declared probe path | `.../SDKs/MacOSX26.2.sdk` |
| canonical build path | `.../SDKs/MacOSX.sdk` |

The declared path is accepted only when it is the root-owned, exact
`MacOSX26.2.sdk -> MacOSX.sdk` alias and its resolved root-owned directory is
the fixed canonical path. The Swift invocation passes that canonical SDK path
through fixed compiler options. It also passes a fixed
`-file-prefix-map <private-workspace>=/adhelm-native-build`, which remaps private
source/build paths. The builder admits no caller-supplied compiler or linker
flags. It preserves the ordinary Mach-O UUID: the Core-only inspector requires
that load command to run under dyld. Its fixed linker `-S` strips debug STABS,
including the `N_OSO` object-path/timestamp entries that would otherwise vary
between private workspaces. This is a development-profile debug-information
choice; it does not state a release-symbol policy. The manifest separately
records the compiler's default target (`arm64-apple-macosx26.0`), the package
deployment target (`macos-13.0`), the SDK identity, and a pure parse of the
produced host's ARM64 `LC_BUILD_VERSION` header. The parser requires the
ARM64-all subtype, macOS platform, minimum OS `13.0.0`, and SDK `26.2.0`; it
rejects ARM64E and malformed or unbounded load-command data.

This is a fixed ARM64 development profile, not a portable or hermetic release
claim. The fixed remap addresses an observed temporary-path difference. The
fixed `-S` strips the observed debug STABS while retaining the ordinary UUID.
All host-binary variability remains subject to separate recorded two-build
evidence.

## Inspection and staged bundle

The builder compiles the installed host product and the Core-only metadata
inspector. It never executes the host. The inspector has no arguments and no
filesystem or environment input. Its stdout must exactly equal the canonical
native descriptor reconstructed from the selected metadata; a mismatch fails
before output staging.

After that check, the builder creates a new private output directory and stages
only:

```text
AdHelmApprovalHost.app/
  Contents/Info.plist
  Contents/MacOS/adhelm-macos-installed-approval-host
  Contents/Library/LaunchAgents/<mach-service>.plist
manifest.json
AdHelmApprovalHost.entitlements       # only a template when declared
adhelm-macos-installed-approval-host-unsigned.tar
```

The LaunchAgent plist is an unregistered template. Its `Label` and sole
`MachServices` key equal the declared service, and `BundleProgram` is the
relative `Contents/MacOS/adhelm-macos-installed-approval-host`. It has no
`RunAtLoad` or `KeepAlive` setting. The bundle contains no client or installer.

The manifest binds the source commit/archive, generated source, closed metadata,
native descriptor, toolchain/SDK/target evidence, and every staged component
with SHA-256. A deterministic tar uses sorted names, fixed modes, fixed owner,
and the selected source epoch. New-output, single-link regular-file, no-follow,
and fsync checks reject overwrites, final symlinks, hard-link aliases, and
changed files.

`developer_identity_signed: false` means this builder has no Developer ID or
release-signing evidence. `compiler_signature_status: NOT_INSPECTED` explicitly
does **not** claim that the produced Mach-O lacks an `LC_CODE_SIGNATURE`; a
toolchain may add an ad-hoc signature. The builder does not invoke `codesign`.
`registered`, `identity_authorized`, `notarized`, and `host_started` remain
false or `NOT_VERIFIED`. The resulting archive is not an installed approval
host and grants no approval or execution authority.

## Validation boundary

The focused builder tests use fake Git, SDK probes, Swift builds, and inspector
output. They cover exact tool forms, source/archive path rejection, generated
source stability, descriptor mismatch before staging, deterministic tar output,
safe final-file publication and hashing, closed result serialization, SDK
identity, and Mach-O target parsing. They do not build the configured host,
access macOS security services, or provide signing, installation, notarization,
or runtime evidence.

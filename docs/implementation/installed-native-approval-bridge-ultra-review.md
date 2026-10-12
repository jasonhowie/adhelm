# Installed native approval bridge ultra review

## Scope

This independent M34 review covers the private retained-plan coordinator, the
shared exact-receipt handoff, the package-only post-signing native path, fixed
candidate framing, and the one-shot helper runner. It uses ledger, clock,
cryptography, session, transport, pipe, and protected-store fakes only.

It does not launch a helper, create an XPC connection, use Security,
Keychain, LocalAuthentication, AppKit, a provider, or a real ledger-backed
execution operation.

## Required boundaries

- Only a retained direct OpenAI pause or GA4 CREATE plan may create a private
  request. All `changes`, `creative`, and `performance` checkpoint prefixes
  must still extend at acceptance, and the selected retained action and
  profile must still match exactly.
- A private pending request is one-use and must be structurally coherent
  before transport resolution. Corruption cannot begin a native review or
  consume a nonce.
- The native candidate is opaque evidence. It may reach the existing exact
  receipt-claim core only after exact request, challenge, metadata,
  configuration, source, signature, key, and clock revalidation. It does not
  construct caller-selected `PinnedApprovalTrust`.
- The post-signing consumer receives the exact fresh session only during its
  synchronous handoff. Cancellation or expiry wins over an undelivered
  candidate, while session invalidation waits until that handoff returns.
- The helper accepts one bounded request frame followed by EOF, validates the
  fixed client identity before native work, checks parent liveness immediately
  before client creation and request dispatch, and returns one bounded
  candidate frame without retrying a failed exchange.

## Findings repaired before freeze

- A low-level mutation of a real pending wrapper's retained source initially
  reached the pipe adapter and could therefore consume native state before
  failing as an ambiguous local acceptance. The coordinator now reconstructs
  and cross-checks binding, source, all stream prefixes, direct challenge,
  request, and source-context digest before it resolves the adapter. The
  malformed value returns `NATIVE_APPROVAL_BRIDGE_PENDING_INVALID` with no
  exchange.
- A parent could close the helper stdout pipe after request EOF but before
  client creation. The runner now checks peer liveness both before making the
  client and before dispatching its request, so it cannot start native review
  for a parent that cannot receive a candidate.
- The post-signing lease previously held its state lock while consulting the
  request continuation, while terminal completion acquired the locks in the
  opposite order. The continuation call now occurs outside the state lock; a
  terminal close can finish while that callback is blocked.
- The engine now suppresses a deferred candidate if cancellation or expiry
  wins during a blocking post-signing consumer, and rechecks both immediately
  after the consumer returns.
- Adjacent unsigned-artifact review found that the fixed one-shot client was
  initially archived without its executable mode, and a failed archive could
  leave an owned partial tar file. The archive now preserves both fixed
  executable modes and removes only its verified owned partial output on
  failure. The staged fixture includes the fixed client component.

## Independent coverage and result

`tests/test_installed_native_approval_bridge_ultra_review.py` adds four
fake-only cases:

- a corrupted pending retained source rejects before any adapter call;
- replacing the creative-stream prefix after a candidate blocks claim even
  though the selected changes record is unchanged;
- the pending wrapper stays redacted and cannot be pickled; and
- the bridge handoff does not enter the legacy pinned-trust acceptance path.

The coordinator base plus independent group passed 13 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache \
uv run pytest -q tests/test_installed_native_approval_bridge.py \
  tests/test_installed_native_approval_bridge_ultra_review.py
```

`InstalledNativeApprovalBridgeUltraReviewTests` adds four fake-only native
cases: cancellation during a blocking consumer, expiry after consumer return,
frame-decoder poisoning after trailing input, and terminal close while the
request-continuation callback is blocked. The Core group passed 4 tests.

`InstalledNativeApprovalBridgeHelperUltraReviewTests` adds the EOF-close case
and passed 1 test. The combined native review command passed all 5 tests:

```sh
SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m34-ultra-module-cache \
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m34-ultra-module-cache \
swift test --package-path native/macos-approval-host \
  --enable-xctest --disable-swift-testing \
  --scratch-path /private/tmp/adhelm-m34-ultra-build \
  --filter 'InstalledNativeApprovalBridge.*UltraReviewTests'
```

The adjacent fixed-client archive review passed 8 fake-only cases:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache \
uv run pytest -q tests/test_native_approval_artifact_builder_ultra_review.py
```

Ruff check and format passed for the independent Python test, and
`git diff --check` passed for the owned review files.

## Limits retained by the contract

M34 does not make the native candidate a Python authority token, generic
receipt API, raw key export, public trust pin, provider permit, or execution
result. The coordinator remains private and unwired until a reviewed compiled
binding is present. Prefix comparison detects ledger replacement or rollback
at the recorded checkpoint boundary; it does not claim atomicity with an
external protected store, helper process, native revocation, or provider.

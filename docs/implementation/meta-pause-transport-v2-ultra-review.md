# Meta campaign PAUSE transport V2 ultra review

This independent M47 review covered the private Python pipe exchange and the
separate package-only macOS XPC route. It used synthetic M45 V2 fixtures and
fake POSIX, process, selector, transport, scheduler, reviewer, authentication,
signer, and interaction-gate collaborators. It did not start a helper, connect
to XPC, access an operating-system credential store, authenticate a user, or
contact a provider.

## Findings repaired before freeze

### V2 could have used the V1 helper role

The first V2 exchange shape accepted the shared generic POSIX adapter. Its
default role is V1, so a typed V2 request could have reached the V1 fixed
helper path. The repair adds the module-issued
`_MetaCampaignPauseApprovalBridgePipeAdapterV2` and makes
`_make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(...)` the only
adapter factory accepted by the V2 exchange. It wraps an exact shared adapter
whose role is `META_CAMPAIGN_PAUSE_V2`; a default V1 adapter fails before
`operations.launch`.

The shared role table still preserves V1's zero-argument path and derives the
separate V2 path internally. Neither exchange accepts a caller-provided path,
command, environment, selector, or helper role. Both use the fixed anonymous
pipe launch spec: `cwd=/`, `LANG=C`, `LC_ALL=C`, `TZ=UTC`, no shell, and
`close_fds=True`.

### A failure after `Popen` was incorrectly classified as pre-launch

`_SystemNativeApprovalBridgePosixOperations.launch()` can successfully create
a child and then fail while wrapping or validating its streams. The original
generic launch exception boundary converted that case into a precondition
failure even though the child had existed. The repair preserves
`_PostLaunchUnverified` through the shared adapter, terminates and reaps the
child, and maps it to the V2 ambiguous, no-retry result. A genuine `Popen`
`OSError` remains a pre-launch unavailable result.

My fake regression exercises the system-wrapper path itself, then confirms the
separate genuine-launch control. A `KeyboardInterrupt` after launch also
closes each stream, closes the selector, terminates, kills if necessary, reaps,
and returns no candidate.

### Service admission needed listener-held configuration evidence

The first native V2 selector bound a request against the injected factory's
metadata before proving that its protocol configuration matched the listener's
engine configuration. The repair makes the listener supply its held protocol
configuration digest to the service. Before an interaction lease, the V2
selector now requires both that held digest and a V2 factory, validates the
request against factory-held metadata, compares its protocol-configuration
digest to the listener-held digest, and then constructs the consumer. The engine repeats its
own V2 reconstruction and protocol check.

This is a closed configuration-consistency check. An injected factory remains
a test seam, not evidence of installed provenance or OS protection.

## Boundaries verified

- Python request validation, V1-to-V2 type confusion, exchange-start skew,
  malformed fixed adapter state, and cancellation before launch fail before a
  fake child can start.
- After a possible helper launch, framing, EOF, deadline,
  cancellation, candidate, final-clock, cleanup, and `BaseException` paths map
  to `NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED`.
  They do not return a partial candidate or invite retry.
- The pipe state machine retains the fixed one-frame big-endian protocol,
  request and candidate caps, FIFO checks, nonblocking bounded chunks,
  stderr discard cap, EOF requirement, zero-exit requirement, and a 300-second
  monotonic deadline. Child stderr is discarded. The returned candidate keeps
  receipt and public-key bytes private to acceptance; diagnostics and logs do
  not serialize those bytes or raw frames.
- The macOS client uses the reviewed fixed Mach service and code-signing
  requirement from `TrustedHostConfiguration`. It has a distinct Meta V2
  selector and typed request/candidate response. V1 candidates are rejected by
  the V2 client; V2 candidates are rejected by the V1 client; raw receipt and
  V1 selectors retain their existing Meta refusal routes.
- The native service rejects an absent V2 factory or listener-held digest before
  review or interaction-gate acquisition. It also rejects factory/listener
  protocol mismatch before the gate. Its one-shot reply, timeout,
  cancellation, gate-concurrency, invalidation, and same-session handoff tests
  operate solely on fakes.

## Composition limit

M47 deliberately does not connect this transport to M46 pending acceptance.
M46 currently consumes a pending value when a candidate is supplied. A future
installed orchestrator must atomically validate and consume or claim the
pending value before launching a helper, retain only sealed facts through the
exchange, and use a post-candidate continuation without consuming it again.
The sequence `prepare -> inspect request -> exchange -> current accept` is not
safe after a lost reply and is not supplied here. This review therefore makes
no claim that a helper is installed, that an end-to-end installed route is
available, or that a lost exchange is retry-safe.

## Validation

The independent Python tests are in
`tests/test_meta_campaign_pause_approval_bridge_transport_v2_ultra_review.py`.
They add three fake-only regressions: cross-version and time preflight refusal,
post-`Popen` ambiguity versus true pre-launch failure, and interrupt cleanup.

```text
UV_CACHE_DIR=/private/tmp/adhelm-m47-uv-cache uv run pytest -q \
  tests/test_native_approval_bridge_transport.py \
  tests/test_meta_campaign_pause_approval_bridge_transport_v2.py \
  tests/test_meta_campaign_pause_approval_bridge_transport_v2_ultra_review.py \
  tests/test_meta_campaign_pause_approval_bridge_protocol_v2.py \
  tests/test_installed_meta_campaign_pause_approval_bridge_v2.py \
  tests/test_meta_pause_private_handoff_v2_integration.py
```

The combined fake-only Python surface passed **77 tests**. Strict mypy passed
for both transport modules and the independent test; Ruff, formatting, and
`git diff --check` passed.

The native owner ran its focused fake-only
`NativeMetaCampaignPauseApprovalBridgeXPCTests` suite: **14/0**, recorded in
`/private/tmp/adhelm-m47-xpc-focused-swift-tests.txt`. That suite does not create
an XPC connection or invoke a helper. Its final sandboxed full native package
run also passed **277/0**, recorded in
`/private/tmp/adhelm-m47-full-swift-tests.txt`; Swift formatting and the native
diff check passed.

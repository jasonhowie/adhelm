# Meta campaign PAUSE pipe transport V2

`adhelm._meta_campaign_pause_approval_bridge_transport_v2` is a private,
fake-tested one-shot transport for an already-issued
`NativeMetaCampaignPauseApprovalBridgeRequestV2`. It validates a typed request
against the trusted exchange-start clock, exchanges one length-prefixed frame,
then validates the typed candidate against the same request, the fixed metadata
digest, cancellation, and two trusted post-exchange clock samples.

It does not resolve an installed binding, create a pending request, accept a
receipt, write a claim or grant, access an operating-system store, read
configuration or credentials, contact a provider, or make provider egress
possible. It has no default runtime wiring. The fake-only tests do not invoke
a system helper, host, or provider.

## Closed helper role

The shared POSIX state machine has exactly two module-private launch roles. The
existing V1 role remains byte-for-byte compatible with its existing selector:

```text
/Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-approval-client
```

The V2 factory
`_make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(operations)` issues
the only adapter accepted by
`exchange_meta_campaign_pause_approval_bridge_once_v2(...)`. It binds the
shared state machine to this fixed future selector:

```text
/Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-meta-campaign-pause-approval-client
```

Both roles use no arguments, `cwd=/`, `LANG=C`, `LC_ALL=C`, `TZ=UTC`, no shell,
and only inherited anonymous stdin/stdout/stderr pipes. The V2 entry rejects a
default V1 adapter before `operations.launch`; callers cannot pass a path,
environment, command, selector, or role. The V2 executable is only a fixed
future path in this slice. It is not created, staged, or installed, is not
invoked by tests or a default runtime, and is not claimed to have an identity
here.

The shared pipe mechanics retain the existing 4-byte big-endian one-frame
format, 98,304-byte request cap, 131,072-byte candidate cap, 4,096-byte
discarded stderr cap, FIFO checks, nonblocking 16,384-byte chunks, clean EOF,
zero exit, cancellation checks, and 300-second monotonic deadline. They never
return stderr or child diagnostics.

## Outcome boundary

Malformed typed request facts, a bad V2 adapter capability, cancellation before
launch, and a definitely pre-launch failure return fixed V2 precondition
diagnostics. A true pre-`Popen` failure is definitely pre-launch. If `Popen`
succeeds but wrapping its streams fails, the shared adapter terminates and
reaps the child, preserves `_PostLaunchUnverified`, and classifies the result
as ambiguous. Once the V2 adapter begins its exchange, malformed/trailing
frames, early EOF, deadline, cancellation, cleanup failure, invalid candidate,
or final clock rollback/expiry return only
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED`. That is an
ambiguous, no-retry outcome because a future native nonce consumption may have
occurred. The transport does not serialize raw frames, receipt bytes, public
key bytes, or child output into its logs, errors, or a separate raw-receipt/key
API; the validated private candidate remains available only to a future private
composition layer.

## Future M46 composition

This transport is intentionally not wired to M46 private acceptance. M46's
current `_accept_pending_installed_meta_campaign_pause_approval_bridge_v2`
consumes its pending value when a candidate is supplied. A future orchestrator
must instead atomically validate and consume/claim that pending value **before**
launch, retain only its sealed facts through the V2 exchange, and call the
post-candidate acceptance continuation without consuming it again. A
`prepare -> peek request -> exchange -> current accept` sequence would permit
unsafe retry reasoning after a lost reply and is not an allowed composition.

## Validation

The base transport test uses only fake POSIX operations and synthetic M45 V2
fixtures:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m47-uv-cache uv run pytest -q \
  tests/test_meta_campaign_pause_approval_bridge_transport_v2.py \
  tests/test_native_approval_bridge_transport.py
```

It covers exact V2 role selection, V1 selector preservation, strict role
rejection before launch, pre-launch validation, framed EOF/trailing/stderr
failure cleanup, cancellation, candidate failure, and final-clock ambiguity.

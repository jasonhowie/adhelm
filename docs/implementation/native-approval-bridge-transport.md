# Native approval bridge transport

`adhelm._native_approval_bridge_transport` implements private, fake-tested POSIX
pipe mechanics for the future macOS approval bridge. It is not wired to a CLI,
configured product runtime, helper installation, native trust resolver, ledger
claim, or provider execution path. No test launches a helper or uses OS
authentication.

The only concrete launch description is fixed in source:

```text
executable: /Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-approval-client
argv:       [that exact executable path]
cwd:        /
environment: LANG=C, LC_ALL=C, TZ=UTC
shell:      false
close_fds:  true
stdin/stdout/stderr: inherited anonymous pipes only
```

There is no caller-selected path, argument, working directory, environment,
service, key, public-key pin, receipt, or runtime metadata input. The module
contains a private concrete `subprocess.Popen`/POSIX implementation behind an
injected operations seam, but it has no default production caller. The later
compiled-binding work must decide when it is legal to construct that adapter;
this increment does not do so.

## One exchange

The adapter sends exactly one request frame: a four-byte unsigned big-endian
length followed by at most 98,304 canonical M32 request bytes. It closes stdin
after the complete frame. It accepts exactly one stdout frame with the same
header format and at most 131,072 candidate bytes, then requires stdout EOF,
stderr EOF, and exit status zero. A second frame, trailing byte, early EOF,
bad length, non-pipe descriptor, nonblocking I/O anomaly, or stderr beyond the
fixed 4,096-byte discard cap fails closed.

The concrete adapter verifies all three child streams are FIFO descriptors,
sets them nonblocking, and uses a selector with fixed 16,384-byte read/write
chunks. It never calls `communicate()` or accumulates arbitrary stderr. Stderr
is discarded and never appears in an error, result, CLI surface, file, or
artifact.

The monotonic exchange budget is fixed at 300 seconds, matching the maximum
approval-challenge lifetime. Cancellation and deadline
checks run before launch and throughout I/O/reaping. Once a child has launched,
every failure, cancellation, deadline, malformed response, nonzero exit,
`BaseException`, or post-response validation failure closes pipes, terminates,
kills if needed, reaps, and returns
`NATIVE_APPROVAL_BRIDGE_EXCHANGE_UNVERIFIED` (exit 6). The diagnostic directs
the caller to inspect native approval state and start a fresh review; it never
automatically retries. A cancellation or unavailable condition before launch
has a separate precondition diagnostic because no child was started.

## M32 binding and time checks

Before launch, the transport rebuilds the opaque M32 request with
`revalidate_native_approval_bridge_request_v1_at_exchange`. That check validates
the direct challenge at the trusted exchange-start time, so expired or
future-skewed challenges fail before pipe creation.

After the single response frame and clean child exit, the transport samples the
trusted Python UTC clock, invokes M32 candidate revalidation, then samples the
clock again. The final sample must not roll back and must remain before the
candidate expiry. Cancellation is checked immediately after adapter return and
again before candidate return. A response is only private non-authorizing wire
evidence: this transport cannot create `PinnedApprovalTrust`, accept a receipt,
write a grant, or make provider egress possible.

The fixed helper remains a future reviewed component. This code does not prove
that the absolute path exists, is signed, is installed, is the intended client,
or can perform a user review. Those claims require separately promoted compiled
binding, signed-client, XPC, enrollment, and installation evidence.

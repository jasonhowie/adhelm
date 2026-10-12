# macOS approval-host foundation

`native/macos-approval-host` is a compilable, offline Swift foundation for a
future customer-installed approval host. It is not an installed host, an
authority grant, a provider executor, or evidence that an approval came from a
particular human.

The checked-in executable deliberately has no trusted host, Mach service,
code-signing requirement, access group, or key tag. It exits with
`APPROVAL_HOST_NOT_INSTALLED` rather than reading those values from a request,
file, environment variable, command argument, URL, or XPC message. A separate
reviewed and signed product target would need to provide those fixed values.
This repository does not install that product, register a Mach service,
enroll/read a Keychain key, show an AppKit alert, evaluate OS authentication,
or call a provider.

## Closed protocol

The core accepts canonical JSON only: bounded size and nesting, no duplicate
keys, no decimals or exponents, RFC 8785 key ordering, and only interoperable
integers in `[-9007199254740991, 9007199254740991]`. A trusted configuration
cannot raise the challenge byte limit above 64 KiB, and both the challenge
lifetime and issue-time skew must be finite.

The parser supports only three typed challenge kinds:

- `OPENAI_PAUSE_PLAN`, rebuilt from a complete OpenAI pause baseline and its
  exact expected plan;
- `DELEGATION_POLICY_INSTALL`; and
- `DELEGATION_POLICY_REVOKE`.

Generic `DIRECT_PLAN` and opaque state-fingerprint plans fail before review,
authentication, signing, or XPC transport. The OpenAI path rebuilds the plan,
account binding, state fingerprints, egress target, zero incremental spend,
campaign status transition, schedule, native budget, and recovery operation.
It accepts only the review contract’s local and native identifier alphabets and
the bids `impressions`, `clicks`, and `conversions`.

Foundation timezone lookup is not treated as Python `ZoneInfo` equivalence.
Until an independently maintained cross-runtime IANA allowlist is promoted,
this foundation accepts only `UTC` and `America/Chicago`; another otherwise
valid customer timezone fails closed before UI. The fixture generator verifies
both the retained action revalidation and `preview_openai_pause`, so fixtures
cannot merely satisfy the looser action shape while failing the installed
review boundary.

`DeterministicApprovalReview` renders labeled OpenAI pause facts: account and
campaign bindings, `Active → Paused`, retained bid/budget/schedule, zero
incremental cap, residual-billing uncertainty, egress, and both expiries. A
scrollable AppKit view includes canonical technical facts bound by the review
digest. Policy reviews render grantor, agent/host, accounts, operations,
currency caps, hosts, and effective window. Neither review accepts caller
prose, applies a provider operation, installs a policy, or grants authority.

## State machine and receipt boundary

The core order is review, fresh OS authentication, then protected signing. It
allows one process-wide flow at a time, binds each active request to a
generation as well as its nonce, and retains a process-lifetime consumed-nonce
set through challenge expiry. It rechecks expiry before active registration,
before startup/review, after authentication, and again under the engine lock
when the signer claims its permit. A late authenticated session is invalidated
even if timeout removed the request and the caller dropped its handle. The
replay set is only an in-process guard; an installed integration still needs
durable receipt and nonce consumption before it records a grant or begins
egress.

The signer receives a request-bound, nonserializable `ReceiptSigningPermit`.
The concrete signer claims it exactly once immediately before its first
protected-key operation. Cancellation before that claim invalidates the fresh
session and leaves the nonce usable. A claim consumes the nonce, so cancellation
or a Keychain/hardware failure after the handoff is fail-closed and
nonretryable. A signer success without a permit claim cannot produce a receipt.
This is an in-process misuse boundary; it does not protect against hostile code
running as the same macOS user or with equivalent process/control privileges.

Receipts use a typed signing payload:

```text
adhelm.macos-approval-host.receipt.v1\0<KIND>\0<CANONICAL_UNSIGNED_RECEIPT>
```

They bind the challenge digest and nonce, review digest, receipt kind, host and
key references, expiry, typed subject digest(s), and P-256 DER signature. The
separate pure pinned verifier checks canonical receipt bytes, all unsigned
fields against an expected challenge/configuration, time, nonce/kind binding,
and a caller-supplied trusted P-256 public key. It does not enroll a key, make
a durable grant, or authorize a provider operation.

## macOS adapters

The compiled adapters use the local macOS SDK declarations for
`NSXPCConnection.setCodeSigningRequirement`,
`NSXPCListener.setConnectionCodeSigningRequirement`, `LAContext`, and the
Security framework. An installed product must set the fixed code-signing
requirements before activating a connection or listener. The listener also
checks the effective UID. These checks identify a configured peer binary; they
do not establish that a user intended the operation or comprehended the
review.

The AppKit adapter presents an explicit named Continue button followed by fresh
`deviceOwnerAuthentication` with authentication reuse disabled. Cancellation
aborts the modal session as well as closing its window. The enrollment path is
separate from XPC and requires a local alert followed by fresh authentication.
If an authenticated callback arrives after cancellation, its session is
invalidated.

The Security adapter has code for explicit fixed-label P-256 enrollment with
`.privateKeyUsage` and `.userPresence`, including an optional Secure Enclave
storage choice. Existing-key lookup pins and checks private-key class, P-256
type, 256-bit size, signing capability, and configured storage token. Public
macOS Security declarations do not expose a way to inspect the constraints of
an existing opaque `SecAccessControl` object. Therefore existing-key signing
currently fails with `accessControlUnverifiable` instead of treating the
presence of an ACL object as proof of `.privateKeyUsage` and `.userPresence`.
This is intentional: the compiled foundation cannot produce a live protected
receipt until a separately reviewed installed-key verification mechanism is
available.

The XPC client has one operation. It decodes a challenge locally before
transport, permits one pending request, has an independent expiry deadline,
invalidates its request-bound connection on terminal paths, and returns only a
bounded 16 KiB canonical-object receipt candidate or an exact known compact
failure. Unknown/mismatched remote failure code or message pairs become
`APPROVAL_TRANSPORT_UNAVAILABLE`; raw peer diagnostics do not escape. A receipt
candidate is not a verified authorization until the pinned verifier runs.

## Offline reproduction

Generate the synthetic vectors from the actual Python models without writing
Python bytecode in the repository:

```sh
PYTHONDONTWRITEBYTECODE=1 \
UV_CACHE_DIR=/private/tmp/adhelm-approval-host-uv-cache \
uv run python native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/generate_safety_vectors.py
```

Run XCTest with SwiftPM state isolated from user credential/config paths:

```sh
CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-approval-host-test/clang-module-cache \
swift test --package-path native/macos-approval-host \
  --disable-keychain --disable-netrc --skip-update \
  --disable-automatic-resolution \
  --enable-xctest --disable-swift-testing \
  --scratch-path /private/tmp/adhelm-approval-host-test/scratch \
  --cache-path /private/tmp/adhelm-approval-host-test/cache \
  --config-path /private/tmp/adhelm-approval-host-test/config \
  --security-path /private/tmp/adhelm-approval-host-test/security \
  --manifest-cache local \
  -Xswiftc -module-cache-path \
  -Xswiftc /private/tmp/adhelm-approval-host-test/module-cache
```

The local package run exercised 41 XCTest cases with fake review,
authentication, signing, XPC transport, and deadline adapters. It also covers
Python-generated whole-second and six-fraction timestamp vectors, invalid
calendar/nonce/canonical JSON cases, Foundation-only offset rejection,
one-use signing permits, expiry at registration/start/consume boundaries,
late-authentication cleanup after timeout, active-flow boundedness, stale
generation callbacks, and XPC output/failure normalization. That is development
evidence only; root’s exact-source native validator and independent review are
still required. No actual XPC service, GUI, OS authentication, Keychain
operation, credential, provider call, or remote mutation was exercised.

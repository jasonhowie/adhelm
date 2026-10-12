# macOS approval-host implementation boundary

This is implementation preparation for the existing v1 authorization contract.
No native host, key enrollment, trusted receipt verifier or approval capability
has been implemented by this note. No OS authorization prompt or Keychain
operation was performed during the API research.

The subsequent [native foundation](macos-approval-host-foundation.md) and
[pinned receipt verifier](native-receipt-verification.md) implement part of
this design. Their offline evidence does not establish installed authority.

## Proposed local components

A signed, user-level AppKit application on macOS 13 or later exposes a narrow
NSXPC service. It accepts a bounded, closed approval challenge containing the
complete canonical plan facts or exact delegation policy. The host independently
validates the proposal, computes the composite and ordered subplan digests, and
renders its own deterministic review. It never accepts a file/link/model claim
as approval and never exposes a generic signing operation.

An explicit approval action is followed by a fresh LocalAuthentication context.
Use the selected device-owner policy; Touch ID is not universally required.
Keep authentication reuse disabled, evaluate the key's access control, sign one
domain-separated receipt using the access-controlled key, invalidate the context
and release the key reference. Secure Enclave P-256 can be a separately declared
stronger capability. Its presence, availability or attestation must not be
inferred from a public key or software-only fixture.

The receipt binds protocol version, operation kind, fixed host/key identities,
host-generated receipt/authorization identity, challenge nonce, grantor and host
references, exact plan digest and ordered subplan digests (or exact policy
digest/version/revocation operation), issue/expiry times, deterministic review
digest, declared authentication mode, and signature. Different approval kinds
must not share a generic policy-configuration receipt. A bounded receipt digest
can identify the user interaction without exposing authentication data.

The installed kernel path must resolve a fixed enrolled host verifier instead
of accepting a request-controlled `UserApprovalHost` object. Verify the exact
receipt against the enrolled/pinned public key, all bound fields, kind, expiry,
replay/revocation state and durable history before appending approval. The
existing injectable boolean protocol remains a development testing interface;
it is insufficient as the installed user-origin boundary.

Use mutual fixed code-signing requirements on XPC before connection activation.
UID/audit-session checks can add constraints but do not identify a human action.
The signed CLI may itself be invoked by an agent, so matching code identity does
not replace the review and authentication steps. No privileged helper or
Authorization Services elevation is needed for this workflow.

## Evidence limits and implementation checks

A click proves an event reached the application. LocalAuthentication reports
that the selected device-owner policy succeeded; a protected-key signature
binds receipt bytes. None proves that a person read or understood the plan,
provides generic hardware attestation, or defeats hostile code with equivalent
same-user OS privileges. The supported claim is a host-mediated review action
bound to exact displayed facts, followed by the declared OS authentication and
protected-key operation.

Offline checks must cover canonical challenge/receipt vectors, signature and
field coverage, closed decoder bounds, UI/auth/signing cancellation and timeout,
unavailable GUI/auth/key paths, mismatch/replay/expiry and distinct grant/policy/
revocation kinds. Every failed path must leave no durable approval. Real evidence
still needs signed host/client identities, a witnessed exact-plan review and
fresh OS authentication, receipt/signature/durable record checks, and actual XPC
and Keychain ACL behavior. A compile or fixture does not supply that evidence.

## Primary local API references

The Terra ultra research inspected the macOS 26.2 SDK shipped with Xcode:

- `LocalAuthentication.framework/Headers/LAContext.h`: policies, asynchronous
  evaluation, access-control evaluation, reuse and context invalidation.
- `Security.framework/Headers/SecAccessControl.h`, `SecItem.h`, `SecKey.h`:
  access-control flags, authentication contexts, key constraints and signatures.
- `Foundation.framework/Headers/NSXPCConnection.h` and
  `usr/include/xpc/connection.h`: peer identity and code-signing requirements.
- `Security.framework/Headers/SecCode.h`: audit-token-backed code identity.
- `AppKit.framework/Headers/NSApplication.h` and
  `CoreGraphics.framework/Headers/CGEvent.h`: synthetic events and their limits
  as evidence of user intent.

Their root was
`/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX26.2.sdk/System/Library/Frameworks`,
except the explicitly named `usr/include` path under that SDK. Availability and
exact supported access-control combinations must be checked by the implementation
and real platform tests; this note does not assert successful key operations.

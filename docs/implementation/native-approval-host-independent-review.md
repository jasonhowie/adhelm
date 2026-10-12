# Native approval-host independent state-machine review

This review exercised the frozen `ApprovalHostCore` state machine with only
in-memory review, authentication, signing, scheduler, identifier, and clock
adapters. It made no XPC request, AppKit presentation, LocalAuthentication,
Security or Keychain operation, provider request, credential lookup, or remote
mutation.

The independent regression suite covers five handoff and callback boundaries:

- A challenge whose clock reaches expiry at the decode-to-registration handoff
  fails with `challengeExpired` before review, authentication, or signing. The
  engine captures and checks the current time while holding its active-request
  lock.
- A challenge whose clock reaches expiry at the registration-to-start handoff
  also fails before review. The request checks its clock before changing from
  its created phase; it does not depend on an asynchronously scheduled timeout
  to close that handoff.
- A request whose clock reaches expiry at the concrete signer permit-claim
  handoff cannot consume its nonce or permit protected signing. The engine
  captures and rechecks time under the consume lock.
- An authenticated callback arriving after a timeout and after the caller has
  dropped its request handle invalidates the arriving session, does not reach
  the signer, and leaves the unclaimed nonce usable for a fresh flow.
- A stale review completion from a cancelled request cannot advance a newer
  request with the same nonce. The active flow remains bound to its engine
  generation and current request phase.

The first four checks found and reproduced concrete lifecycle gaps during
review. The source now has a lock-scoped pre-registration expiry check, a
request-start expiry check, a lock-scoped consume expiry check, and a fallback
invalidator for a late authenticated callback whose request has deallocated.
The fifth confirms the generation and phase protections after those fixes.

Validation used the isolated SwiftPM command documented by the native
foundation and ran `IndependentApprovalHostReviewTests`: 5 XCTest cases passed.
The complete native XCTest package then passed 41 cases with no failures.

This is development evidence only. The test fakes do not establish that an
installed host has fixed trusted configuration, an authenticated person,
protected-key enforcement, durable nonce consumption, XPC peer identity, or
provider authority. In particular, the in-process permit cannot make OS
cancellation atomic with a protected-key operation, and durable replay and
execution checks remain an installed-kernel responsibility.

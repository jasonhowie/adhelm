# Native one-time approval setup: independent review

This review covers M37's local status-menu wiring for the explicit M36
receipt-redemption global initializer. It examined
`ReceiptRedemptionGlobalSetup.swift`, the runtime construction boundary, and
the setup controller in `AppKitReview.swift`.

The controller is only constructed from an active runtime with the current
interaction lease, initial-enrollment configuration, and a runtime-bound store
that implements the private setup capability. It accepts no caller-provided
selector, configuration, key, receipt, challenge, recovery action, XPC input,
or helper input. The action is separate from signing and provider execution.

The independent test class adds four fake-only cases:

- cancellation before protected work leaves the fixed store untouched;
- an unexpected protected-store error becomes the fixed public
  `APPROVAL_SIGNING_UNAVAILABLE` result and is not retried;
- a retained recovery result becomes
  `APPROVAL_ENROLLMENT_STATE_UNVERIFIED`, requiring inspection; and
- invalidating the runtime and interaction gates after the protected store
  returns a nominal success still produces
  `APPROVAL_ENROLLMENT_STATE_UNVERIFIED`.

The last case covers the important terminal boundary: UI code cannot claim a
safe setup result after a shutdown races the synchronous protected handoff.
The normal controller keeps the interaction lease through its terminal result;
the M36 initializer itself retains `INTENT_PENDING` for any durable-write
ambiguity and does not reset or retry it.

These tests use in-memory stores, sessions, and gates only. They do not create
AppKit windows, Local Authentication contexts, Security/Keychain items, XPC
objects, a helper, an installed host, or a provider request. They do not prove
the durability of a real protected store or an actual user interface.

The focused fake-only M37/M36 controller group passed 25 tests: three Core
provenance tests, five fixed-slot adapter tests, seven global-initialization
tests, six owner controller tests, and these four independent cases. Strict
Swift formatting and `git diff --check` also passed.

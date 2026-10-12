# Native one-time approval setup (M37)

M37 wires the M36 fixed receipt-redemption global initializer to one explicit
local status-menu action: **Set Up One-Time Approvals…**. The user-facing
review explains that the action records local protection against reusing an
approval; it does not approve or execute a provider change.

## Admission and scope

The installed runtime constructs the setup controller only when all of these
local facts hold:

- the runtime is active and has its fixed interaction lease;
- its reviewed configuration includes initial-enrollment support; and
- the runtime-bound protected store implements the internal,
  `ReceiptRedemptionGlobalSetupStore` capability.

The controller receives no caller-selected configuration, key, fixed-slot
selector, recovery action, or store. The concrete Security store accepts only
a fresh `LocalAuthenticationApprovalSession`, even though the internal
orchestration protocol uses an erased session type to support in-memory tests.
It rechecks the configured host identity before protected work. A normal
`ProtectedKeyStore` cannot obtain the setup capability merely by runtime
wrapping.

The action is absent from XPC, the helper, CLI, Python, challenge handling,
and automatic enrollment follow-on. It does not initialize missing state at
startup, create a key, sign a receipt, issue a bridge candidate, make a
provider request, or reset/recover/delete any retained state.

## Local flow and cancellation

The status-menu operation shares the app's single interaction lease and menu
busy state with key enrollment and revocation. It performs the following
sequence:

1. Require the current runtime interaction lease and a single active menu
   operation.
2. Preflight the fixed host identity, show the local confirmation, and request
   fresh Local Authentication.
3. Begin protected work, then call the M36 initializer through the fixed
   runtime-bound store with the live runtime and interaction checks supplied
   as `shouldContinue`.
4. Invalidate the Local Authentication session after the synchronous setup
   handoff returns, and release the interaction lease after the terminal menu
   result.

Declining the dialog or cancelling before protected work produces the usual
review-cancelled result. Once protected work starts, cancellation, a stale
interaction lease, a stale runtime, a failed readback, a retained setup
intent, a missing/replaced global record, or any other setup ambiguity becomes
`APPROVAL_ENROLLMENT_STATE_UNVERIFIED`. The menu is then disabled for the
process and asks for inspection; it never offers a blind retry or reset. A
successful M36 result is shown as either newly set up or already set up.

## Limits

This is UI wiring over the existing local M36 state machine. Its tests use
only fakes; they do not create AppKit windows, Local Authentication contexts,
Security/Keychain items, XPC objects, a helper, an installed runtime, or a
provider request. The action does not demonstrate a configured host or make
any claim about protected-store durability beyond the M36 sequential
write/readback contract.

## Validation

`ReceiptRedemptionGlobalSetupControllerTests` covers fixed-store admission,
runtime-generation gating, pre-handoff and post-handoff cancellation behavior,
and stale interaction rejection before identity or authentication work.

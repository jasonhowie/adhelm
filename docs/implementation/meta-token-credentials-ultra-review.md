# Meta token credential lifecycle: independent review

This review exercised the Meta token profile, protected local bundle lifecycle,
and configured inspection handoff with synthetic token text, an in-memory
store, tracked local-lock seams, and an in-memory HTTP transport. It did not
open an OS credential store, prompt a terminal, contact Meta, inspect an
account, or mutate a provider.

## Reviewed boundary

`tests/test_meta_token_credentials_ultra_review.py` adds eight fake-only
regressions. The final focused profile, lifecycle, configured-handoff, fixed
inspection, and generic-credential group passed 95 tests.

The review verifies that:

- hidden subject and inspector token suppliers run only after the configuration
  and protected-bundle locks have been released;
- a changed canonical bundle or changed declared profile metadata after an
  enrollment/rotation snapshot blocks replacement before another bundle write;
- a failed rotation readback is an ambiguous local outcome, leaves retained
  bytes alone, and never triggers an automatic retry or cleanup write;
- malformed opaque install state fails before token collection or store access;
- generic credential lookup, status, store, rotation, and removal reject
  `OS_META_TOKEN` before a backend, prompt, or generic slot lock is reached;
- a model-copied invalid Meta profile is revalidated before generic credential
  handling;
- the configured fixed inspection GET occurs outside both local locks, then
  returns only the declared app, a pseudonymous user reference, and observed
  scopes; and
- a valid but replaced local bundle during that GET blocks the observation at
  the second private credential read rather than returning an observation
  assembled from two bundle generations.

During review, raw malformed bundle values, including a lone surrogate, were
normalized to diagnostic `UNVERIFIED` status. A credential-store outage still
returns the fixed operational error instead of being misrepresented as a
verified local state.

## Scope limits

The declared account ID, currency, timezone, inspector kind, and requested
purpose are customer configuration. They are not evidence of account access,
provider authentication, app review, current account metadata, provider
mutation authority, or an execution grant. The fixed token inspection remains
the only reviewed provider read in this slice, and its observation does not
expose account access or execution authority.

The private bundle is not a generic bearer credential. This review provides no
CLI route, account inventory, token refresh or exchange, provider plan,
approval, provider mutation, real Keychain access, or live Meta evidence.

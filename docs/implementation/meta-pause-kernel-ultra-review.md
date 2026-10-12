# Meta PAUSE kernel independent review

Status: `REVIEWED`.

This review covers only the M44 `SafetyKernel` containment for the fixed
`meta_ads` / `PAUSE` subplan. It uses actual M40 planned actions, temporary
ledgers, synthetic adapters, and locally generated M42 signed receipts. It
does not construct a Meta client, issue a provider request, access an operating
system credential store, start a native component, or expose a CLI path.

## Boundaries verified

`SafetyKernel._preflight_one()` requires `GuardedAdapter` for exactly the Meta
PAUSE pair before calling adapter capabilities, identity, state, or apply
methods. The existing all-subplan preflight prevents an unguarded later Meta
subplan from allowing an earlier provider mutation.

A Meta response other than `CONFIRMED` remains
`AMBIGUOUS_REMOTE_STATE`, even when the post-attempt fake read equals the
planned `PAUSED` fingerprint and the response claims no effect. The kernel
does not call `verify()` in that path, so a matching state cannot attribute the
attempt or make a retry safe.

The exact-history classifier binds provider, account, run, operation,
resource, plan, subplan, approval, derived idempotency reference, and the exact
planned before/after fingerprints. A row identifying that attempt but carrying
contradictory fingerprints is unsettled, rather than ignored by selection. It
accepts a Meta attempt as verified only when the history has one ordered
`APPLY_STARTED`, one `APPLIED_VERIFIED` `APPLY_RESULT`, and one matching
`APPLIED_VERIFIED` `VERIFICATION_RESULT` with evidence. Duplicate, reordered,
malformed, sparse, or missing terminal evidence stays unsettled. A
`KeyboardInterrupt` after guarded egress leaves no terminal record and remains
unresolved after a later target-state reconciliation.

The independent composite regression starts with a durable verified Meta
result, leaves a separate synthetic follower unsettled, and then returns a
contradictory Meta baseline / `FAILED_NO_EFFECT` reconciliation proof. The
kernel preserves the original verified result, returns Meta ambiguity, and
does not append a contradictory settled reconciliation result. This is a
kernel-only synthetic direct-grant harness approved for the multi-provider
case: one exact M42 Meta receipt cannot truthfully bind an unrelated follower.
The root integration test separately covers the exact M40 plan through the M42
signed-receipt path.

## Findings repaired during review

- A durable `APPLY_STARTED` with no `APPLY_RESULT` could be treated as an
  ordinary later reconciliation result. The Meta-specific classifier now keeps
  that crash boundary unsettled regardless of a later baseline or `PAUSED`
  read.
- A confirmed Meta result in an otherwise unfinished composite could be
  rewritten as `FAILED_NO_EFFECT` if a later current read returned baseline.
  Reconciliation now preserves the durable attempt conclusion and leaves the
  contradictory current read unresolved rather than making a false no-effect
  claim.
- The first ordered-history implementation accepted a verified-status record
  without durable verification evidence. It now requires the exact ordered
verification event and nonempty evidence, matching
  `Ledger.unresolved_starts()`.
- The matcher originally treated rows with a correct attempt identity but a
  mismatched retained `before_digest` or `after_digest` as unrelated. It now
  recognizes the attempt identity first and makes both a changed start and an
  extra malformed terminal row unsettled, rather than silently reporting no
  attempt or a verified sequence.

No open M44 kernel finding remains in this review scope.

## Limits

The containment rule does not prove Meta's live acknowledgement, provider-side
idempotency, current campaign access, or a configured `PAUSED` result. It does
not authorize a native bridge route, a raw XPC receipt flow, a CLI command, a
provider POST, rollback, automatic retry, or any remote effect. A later
matching or baseline observation remains insufficient to attribute an uncertain
attempt.

## Validation

The frozen focused group passed 24 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m44-uv-cache uv run pytest -q \
  tests/test_meta_pause_kernel.py \
  tests/test_meta_pause_kernel_integration.py \
  tests/test_meta_pause_kernel_ultra_review.py
```

The independent test file also passed Ruff and format checks.

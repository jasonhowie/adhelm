# Meta campaign PAUSE kernel containment

Status: implementing, fixture-tested kernel containment only.

This slice adds no Meta client, HTTP request, credential or configuration
read, CLI command, native approval bridge path, or provider capability claim.
It only makes the existing generic mutation kernel fail closed if a later
Meta PAUSE adapter is promoted without the required guarded boundary.

## Fixed pre-egress boundary

`SafetyKernel._preflight_one()` requires `GuardedAdapter` for exactly:

```text
provider = meta_ads
operation = PAUSE
```

The type check happens before the kernel asks the adapter for capabilities,
identity, or state. `SafetyKernel._preflight_all()` retains its existing
all-provider ordering, so an unguarded later Meta subplan prevents every first
provider write. The existing OpenAI account-key and GA4 CREATE guarded-adapter
requirements are unchanged.

The kernel still fsyncs its normal `APPLY_STARTED` event before it issues the
one-use permit. The synthetic guarded fixture verifies that its permit is
consumed only with the exact parent plan digest, subplan digest, and attempt
reference. It is not a Meta adapter and does not perform a provider operation.

## Attribution containment

For Meta PAUSE, a response other than `CONFIRMED` produces
`AMBIGUOUS_REMOTE_STATE` even when a later local adapter read reports the
planned configuration-only `PAUSED` fingerprint. The kernel does not call
`verify()` in that path. A confirmed synthetic response with the exact planned
state and bounded evidence can reach the existing `APPLIED_VERIFIED` path.

Read-only reconciliation scans only durable events for the exact Meta attempt:
provider, account, run, operation, resource, plan, subplan, approval, and
idempotency reference must all match. It keeps the attempt unresolved when:

- an exact `APPLY_STARTED` exists without an `APPLY_RESULT`; or
- an exact result is ambiguous, partial, malformed, duplicated, or otherwise
  not the single durable conclusion below.

The only current Meta conclusion that does not trigger this containment is one
exact `APPLIED_VERIFIED` `APPLY_RESULT` followed by one matching
`APPLIED_VERIFIED` `VERIFICATION_RESULT` carrying nonempty evidence references.
The event order and cardinality are part of the check: duplicate starts,
duplicate terminal events, a sparse verification record, and a result written
before its start or without a start are unresolved. Every matched event must also carry the exact
planned `before_digest` and `after_digest`; valid-shaped fingerprints from a
different subplan are not enough. The Meta rule deliberately turns every
non-confirmed response into ambiguity, including a failed response that might
otherwise have carried generic no-effect evidence. It therefore does not claim
a current Meta-specific authoritative no-egress terminal state. Adding one
would require a reviewed, durable distinction beyond the existing ledger ABI.

An already verified result is also not rewritten by a later baseline read when
its composite run remained open because another provider stopped it. A later
reconciliation can repeat only the exact verified after-state conclusion;
contradictory baseline/no-effect proof is left ambiguous. This distinction
matters for a crash after the durable start: the start record itself says no
more than that the process reached its pre-egress durability boundary. It
cannot establish whether egress happened.

When containment applies, a later adapter reconciliation is projected back to
an unsettled `AMBIGUOUS_REMOTE_STATE`; the kernel writes neither
`RECONCILIATION_RESULT` nor `PROVIDER_RECONCILED`. Repeated reconciliation is
therefore read-only and preserves the unresolved run. It does not retry,
rollback, infer that the campaign was untouched, or attribute a later baseline
or PAUSED state to the earlier attempt.

This is deliberately conservative under the current ledger ABI. It does not
claim to distinguish a process loss immediately before egress from one
immediately after egress. A reviewed fixed provider acknowledgement contract
would be required before any narrower conclusion is safe.

## Fixture coverage

`tests/test_meta_pause_kernel.py` uses an actual M40 Meta action, a synthetic
signed M42 direct grant, a temporary ledger, and fake adapters. It covers:

- unguarded Meta refusal before any adapter interaction;
- full multi-provider preflight, where a later unguarded Meta subplan prevents
  an earlier provider from applying;
- a confirmed guarded fixture with exact permit consumption and verification;
- a non-confirmed response followed by matching `PAUSED` reads, including
  repeated reconciliation and blocked retry; and
- simulated process loss after durable `APPLY_STARTED`, with both a later
  baseline and a later PAUSED state, neither of which settles the run.
- a process loss after a durable verified Meta result but before composite-run
  closure, where a contradictory later baseline proof cannot rewrite the
  retained verified conclusion.
- valid-shaped but mismatched before/after fingerprints in terminal history,
  which cannot establish a verified attempt.

No test opens an OS credential store, launches a native host or helper, makes a
network request, or constructs a concrete Meta mutation adapter.

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_meta_pause_kernel.py
```

The focused group passed seventeen tests when this note was written.

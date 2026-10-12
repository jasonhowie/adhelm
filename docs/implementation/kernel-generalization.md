# Authorization kernel generalization

Last updated: 2026-09-08 (revised after three independent review rounds)

## Scope and non-scope

This describes an internal generalization of `src/adhelm/safety.py`,
`src/adhelm/egress_permit.py`, and the additive child-egress record kinds in
`src/adhelm/ledger.py`.

It creates **no new provider capability, no new mutation command, no new
credential or scope, and no live provider evidence**. Every provider adapter
that existed before this change still executes exactly the operations it did
before, against the same hosts, under the same authorizations. Nothing here
promotes a capability's declared status, and nothing here is a claim that a
multi-resource provider hierarchy is implemented: only the kernel seam that a
future one would use now exists, exercised by in-memory fakes.

It also adds no approval challenge kind. Approval material and the installed
approval bridge are unchanged.

## What changed

### 1. One uniform write rule

The kernel previously named two mutations explicitly:

```python
def _requires_attributable_confirmation(subplan):
    return _is_meta_campaign_pause(subplan) or (
        subplan.provider == "ga4_admin" and subplan.operation is OperationKind.CREATE
    )
```

Only those two, plus any subplan carrying OpenAI account-key evidence, had to
reach their provider through a `GuardedAdapter` with a kernel-issued one-use
`EgressPermit`, and only those two refused to upgrade a non-confirmed
acknowledgement using a matching later read. Every future CREATE, ACTIVATE, or
MAINTAIN operation on any of the six providers would have needed its own branch
in that predicate.

The predicate is now a property of the operation, not of the provider:

```python
READ_ONLY_OPERATIONS: frozenset[OperationKind] = frozenset()


def _is_mutating(subplan):
    return subplan.operation not in READ_ONLY_OPERATIONS
```

Every `OperationKind` in the current contract writes, so `READ_ONLY_OPERATIONS`
is empty and the rule covers every provider and every operation:

- **Guarded egress.** `_preflight_one` refuses any mutating subplan whose
  adapter does not implement `GuardedAdapter`, with `ADAPTER_PERMIT_REQUIRED`,
  before any capability read, identity read, state read, or ledger append. Because
  `GuardedAdapter` extends `Adapter`, a provider that can write must also be able
  to `reconcile`; an adapter that cannot prove what happened cannot mutate.
- **Attributable confirmation.** A non-`CONFIRMED` `ApplyResponse` is always
  `AMBIGUOUS_REMOTE_STATE`, for every provider and operation, with no exception.
  No read this caller takes can attribute such an attempt in either direction: a
  readback at the target state cannot show that this attempt produced it, and a
  readback still at the approved baseline cannot show that the request never
  reached the provider, because an eventually consistent list, a lagging
  replica, and a lost acknowledgement of a write that did land all look exactly
  like that. `SafetyKernel.reconcile_run` already refuses to treat an unchanged
  read as proof of no effect; the apply path now refuses the same pair.
  A `CONFIRMED` acknowledgement whose readback never left the baseline is also
  ambiguous, because it contradicts itself.

  `ApplyResponse.no_effect_proven` therefore no longer changes any kernel
  status. It is retained as an adapter's statement that it refused *before*
  egress -- before consuming its permit -- and is surfaced only as the
  `NO_EFFECT_CLAIM_NOT_INDEPENDENTLY_PROVEN` diagnostic when an adapter makes
  the claim anyway, so the claim is neither acted on nor silently dropped.
  `FAILED_NO_EFFECT` at subplan level now comes only from `reconcile_run`, where
  the adapter must supply a provider-aware settled `Reconciliation`.

`_kernel_permit` moved from `_apply_ordered` into a new `_dispatch`, so the
permit scope now closes as soon as the adapter call returns or raises, before
the post-apply readback, rather than spanning the whole subplan.

### 2. Journaled multi-step child egress

`SubPlan` gains `expected_child_egress_count: int = 1` (validated `>= 1`, bounded
by `adhelm.models.MAX_CHILD_EGRESS_COUNT`). A subplan whose count is `1` serializes
exactly as before: the model serializer drops the field whenever it is `1`, so no
retained subplan digest, approval, or ledger record changes, and two semantically
identical single-request subplans cannot acquire different digests by setting the
default explicitly.

A subplan with a count above `1` must be executed by a `ChildEgressAdapter`,
which the kernel calls with a `KernelExecution` instead of a single permit:

```python
def apply_children(self, plan, *, idempotency_key, execution: KernelExecution) -> ApplyResponse:
    with execution.begin_child(1, "create-campaign") as permit:
        permit.consume(
            plan_digest=..., subplan_digest=..., attempt_ref=idempotency_key, child_index=1
        )
        ...
        execution.report_child(1, ApplyResponse(outcome=..., evidence_refs=(...,)))
```

Invariants:

- The composite-level `APPLY_STARTED` still precedes the first child. Nothing
  about the composite barrier, one-use authorization claim, or policy reservation
  changed.
- `begin_child` rechecks the plan expiry and authorization window (the
  release-policy gate has already refused at `SafetyKernel.execute`; there is no
  edition), then appends and fsyncs `CHILD_APPLY_STARTED` **before** the child's
  permit exists.
- Children are strictly sequential. `index` must equal the next expected index
  starting at `1` (`CHILD_EGRESS_OUT_OF_ORDER`), may not exceed
  `expected_child_egress_count` (`CHILD_EGRESS_COUNT_EXCEEDED`), and only one
  child scope may be open at a time (`CHILD_EGRESS_ALREADY_ACTIVE`).
- `EgressPermit.consume` now binds `(plan_digest, subplan_digest, attempt_ref,
  child_index)`. A permit issued for child *n* cannot be claimed as child *n+1*,
  and it is still single-shot and invalid outside its own scope. Existing
  single-request adapters call `consume` without `child_index`, which binds
  `None`; their behavior is unchanged.
- `CHILD_APPLY_RESULT` is appended when the scope closes, and its only statuses
  are `APPLIED_VERIFIED` and `AMBIGUOUS_REMOTE_STATE`. Only an evidenced
  `APPLIED_VERIFIED` resolves the matching start. A child whose permit was
  issued may already have reached the provider, so -- exactly as at subplan
  level -- an adapter's own no-effect claim cannot clear its own pre-egress
  marker. A missing report, a confirmation with no evidence, an ambiguous
  transport, and a claimed no effect all record `AMBIGUOUS_REMOTE_STATE`.
- `begin_child` returns an explicit closable scope rather than a generator
  context manager, and `_apply_children` closes it in a `finally`. An adapter
  that enters a child scope and never exits it therefore cannot leave an active
  permit alive: the kernel revokes the permit and journals that child's
  `CHILD_APPLY_RESULT` (ambiguous), in order, before it calls `read_state` or
  `verify`. This mirrors the `_invalidate_permit` guarantee on the
  single-request path. Entering a scope publishes its permit stack and rechecks
  the closed flag under one lock, so a force-close that lands between the claim
  and the permit's creation refuses the scope with `CHILD_EGRESS_CLOSED` and
  revokes the permit, rather than handing back one the kernel can no longer
  reach.
- A subplan whose approved children did not all prove they applied yields
  `AMBIGUOUS_REMOTE_STATE`, whatever the adapter's overall return value was.
- A crash between two children leaves a `CHILD_APPLY_STARTED` with no result at
  all. `Ledger.unresolved_child_starts()` reports it, and
  `SafetyKernel._reject_unresolved_starts` refuses every later write with
  `UNRESOLVED_CHILD_APPLY` -- reported before the subplan-level
  `UNRESOLVED_APPLY`, because the child is the more specific unresolved state.
  A subplan-level `RECONCILIATION_RESULT` for the same attempt, with evidence,
  resolves that attempt's children; that is the only way out, exactly as with an
  unresolved `APPLY_STARTED`.

### 3. Why one subplan, not several

`CompositePlan.valid_order_and_expiry` still allows only one subplan per
provider, and this change does not relax it.

A provider hierarchy such as campaign -> ad set -> ad is three dependent POSTs
that a marketer authorized as one outcome. Splitting it into three subplans would
mean three separate digests inside one composite, three entries in the one-use
authorization, and three independent `APPLY_STARTED` records for a sequence that
has no meaningful intermediate approved state: nobody authorized "create the
campaign but not the ad set". It would also invite a partially applied hierarchy
to be presented as two successes and one failure, which is not what the customer
decided about.

Keeping it as one subplan preserves the properties the lifecycle contract needs:
one approved before/after transition, one authorization claim, one recovery
disposition, one reconciliation target. The journaled children add the ordering
and crash-resolution facts that were previously missing, without multiplying the
authorization surface. Relaxing the one-subplan-per-provider rule remains a
separate decision that would need its own ordering, locking, and authorization
analysis.

### 4. One provider-neutral unattributable-attempt containment

`_meta_pause_attempt_history_state` became `_unattributable_attempt_state` and
lost its `meta_ads`/`PAUSE` gate: it now classifies the retained attempt of every
mutating subplan. It still binds provider, account, run, operation, resource,
plan, subplan, approval, derived idempotency reference, and the exact planned
before/after fingerprints, and it still accepts an attempt as `VERIFIED` only
when the history holds one ordered `APPLY_STARTED`, one `APPLIED_VERIFIED`
`APPLY_RESULT`, and one matching evidenced `APPLIED_VERIFIED`
`VERIFICATION_RESULT`.

Consequence, for every provider rather than only Meta: an attempt this kernel
durably started and did not verify is `UNSETTLED`, so `reconcile_run` reports
`AMBIGUOUS_REMOTE_STATE` and a later matching provider read alone never settles
it. A provider that verified still reconciles normally, and a selected provider
that was never started is still `NO_ATTEMPT` and reconciles on its own evidence,
so a partial composite still clears its barrier. What no longer happens is a
non-Meta started-but-unverified attempt being settled -- and its one-use
authorization unblocked -- by a read that cannot attribute it.

### 5. One bounded exit from the write barrier

Generalizing the containment removed the only way out of
`_reject_unresolved_starts`, which is global: one lost acknowledgement on one
provider would have refused every later plan on every provider and account,
permanently. That is a liveness defect, not a safety property, and it is fixed
without weakening attribution.

A mutating attempt that recorded `APPLY_STARTED` and never reached an evidenced
`APPLIED_VERIFIED` result is released by an additive
`ATTEMPT_RECOVERY_OBSERVED` record. The record's schema fixes `actor_ref` to
`UNKNOWN` and `attempt_effect_status` to `AMBIGUOUS_REMOTE_STATE`, so it can
never claim who caused the observed state or upgrade the attempt. It releases
the barrier and settles nothing. Two paths write it:

- **Provider-evidenced.** `reconcile_run` writes it when the attempt is
  *locally terminated* -- this kernel journaled one ordered `APPLY_STARTED`,
  `APPLY_RESULT` and `VERIFICATION_RESULT` for it -- and the adapter returned a
  settled `Reconciliation` with evidence. The kernel finished its own protocol
  there; only attribution is missing, and no read can ever supply it. The
  reported status stays `AMBIGUOUS_REMOTE_STATE` and no `RECONCILIATION_RESULT`
  is written.

  The record describes an observation, not a claim: before writing, this path
  re-checks identity and calls `read_state` under the coordinator lock, and
  records *that* fingerprint, relation and disposition -- the same standard
  `record_attempt_recovery` meets. It never copies `Reconciliation.state`
  through. When the adapter's settled proof disagrees with the adapter's own
  current read, the kernel records what it read and reports the disagreement as
  the `RECONCILIATION_PROOF_CONTRADICTS_READ` diagnostic; the released record
  can then never contain a fingerprint the kernel did not observe.
- **Operator-attested.** `SafetyKernel.record_attempt_recovery` handles the
  other case: an attempt with no journaled terminal pair, where the process died
  inside the write path and the kernel does not know whether the adapter
  dispatched at all. `reconcile_run` never releases that one. The operator
  supplies a separate grant whose subject is
  `attempt_recovery_subject(plan, subplan, started_record_digest=...)` -- built
  with `new_attempt_recovery_grant` -- so a grant issued for one lost
  acknowledgement can never release another. The grant is one-use, must be
  unexpired, must be proven by the configured `UserApprovalHost`, and a
  delegated grant must run under an installed, currently effective policy that
  names `OperationKind.RECOVER` explicitly. The kernel then performs a fresh
  identity check and `read_state` under the coordinator lock and records what it
  saw.

`OperationKind.RECOVER` is new. It is a grant and policy subject only:
`SubPlan` refuses it as an operation and as a recovery action, because it sends
no provider request.

Releasing the barrier does not make a retry safe. The original plan stays
one-use (`PLAN_ALREADY_STARTED` / `AUTHORIZATION_ALREADY_CONSUMED`), and any
further mutation needs a new plan compiled from a fresh read, whose drift check
runs against the state the recovery recorded.

`reconcile_run` is also idempotent now: repeating it after the barrier has been
released returns the same provider-aware answer and appends nothing, instead of
failing with `RECONCILIATION_RUN_MISMATCH`.

**Barrier scope.** The barrier remains global across providers and accounts.
Scoping it per stable account -- so an unresolved Meta attempt would not block
an unrelated Google Ads plan -- is a real narrowing of a safety property and is
left as a product-owner decision rather than taken here.

**CLI exposure.** `record_attempt_recovery` was a kernel API only when this was
written. S4 added its command surface -- `recover attempt` and `recover catalog`
-- in [`generic-execution-route.md`](generic-execution-route.md). That surface
is registered and fails closed: the installed approval bridge still refuses the
`ATTEMPT_RECOVERY` kind, and no execution adapter is registered, so an operator
recovery still requires a reviewed signed host that renders the recovery subject
and a configured provider adapter.

### 6. A documented settlement hook, implemented by nobody

`AttributableSettlementProof` is an optional adapter protocol returning an
`AttributableSettlement` that binds a plan digest, subplan digest, and attempt
reference to a settled status with evidence. When an adapter supplies one for
the exact attempt, `reconcile_run` treats that attempt as attributable and
reconciles it normally.

**No installed adapter implements it, and none may until its provider documents
a durable operation or idempotency resource that binds a request to its
outcome.** That is not left to a docstring. `AttributableSettlementProof` is a
`runtime_checkable` Protocol, so any class defining a method of that name would
otherwise qualify; the kernel therefore gates the hook on
`ATTRIBUTABLE_SETTLEMENT_PROVIDERS`, a frozen per-provider allowlist that is
**empty today**. Both the subplan's provider and the adapter's provider must
appear in it. Adding one is a deliberate, reviewed change that requires the
provider's documented attributing resource and its own evidence; it is not
something an adapter can grant itself. A proof that names a different attempt,
or that an adapter raises on, is ignored regardless.

### 7. Shared semantic-hierarchy projection

`src/adhelm/providers/_semantic_state.py` is a provider-neutral extraction of the
keyed projection that `ga4_admin_create_state.py` used privately: a
`SemanticHierarchySpec` (HMAC domain-separation context, state kind, projection
version, purpose labels, bounds) plus `target_binding_fingerprint`,
`member_fingerprint`, `collection_state`, and the bounded JSON/scalar validators.

The projection covers only request-controllable names and configuration.
Provider-assigned resource identifiers, create times, and similar metadata must
stay out: a provider assigns them at create time, so including them would make a
predicted `after` state impossible to compare against a later read. Which fields
are request-controllable is the caller's decision -- the shared module cannot
know a provider's schema -- but it is not a silent trust boundary:
`member_fingerprint` refuses identifier-shaped keys (`id`, `name`,
`resource_id`, `resource_name`, `resource_ref`, `*_id`, `*_ids`) and
provider resource-path values (`properties/123/keyEvents/456`,
`customers/1234567890/campaigns/77`) anywhere in a member configuration. Duplicate member fingerprints are retained rather than deduplicated,
because a set would lose the distinction between one matching resource and two
resources with the same request-controllable semantics.

GA4 now delegates to it. The construction is byte-identical:
`tests/test_ga4_admin_create_state.py::test_generic_semantic_projection_extraction_is_byte_identical`
pins the exact target-binding, collection-state, inventory-digest,
create-after, and candidate digests captured from the module before the
extraction.

## Record shapes

Both kinds are new entries in the existing `changes` stream. No existing record
kind, stream, projection, or digest chain semantics changed.

`CHILD_APPLY_STARTED`:

```json
{
  "schema_version": 1,
  "operation": "CREATE",
  "resource_ref": "<subplan_id>",
  "plan_ref": "<plan_id>",
  "plan_digest": "<64 hex>",
  "subplan_digest": "<64 hex>",
  "approval_ref": "<authorization id>",
  "idempotency_ref": "<64 hex subplan attempt ref>",
  "child_index": 1,
  "expected_child_egress_count": 3,
  "child_label": "create-campaign",
  "effect_status": "NO_REMOTE_WRITE",
  "intent": {
    "egress_hosts": ["graph.meta.test"],
    "egress_allowlist_digest": "<64 hex>",
    "intent_digest": "<64 hex>"
  }
}
```

`ATTEMPT_RECOVERY_OBSERVED`:

```json
{
  "schema_version": 1,
  "operation": "PAUSE",
  "resource_ref": "<subplan_id>",
  "plan_ref": "<plan_id>",
  "plan_digest": "<64 hex>",
  "subplan_digest": "<64 hex>",
  "approval_ref": "<the attempt's authorization id>",
  "idempotency_ref": "<64 hex attempt ref>",
  "started_record_digest": "<64 hex APPLY_STARTED record digest>",
  "recovery_source": "PROVIDER_SETTLEMENT_PROOF | OPERATOR_ATTESTED_OBSERVATION",
  "recovery_approval_ref": "<recovery grant id, operator path only>",
  "recovery_authorization_digest": "<64 hex, operator path only>",
  "recovery_subject_digest": "<64 hex, operator path only>",
  "observed_state_fingerprint": "<allowlisted state fingerprint>",
  "observed_state_revision": "<revision or null>",
  "observed_relation": "AT_BASELINE | AT_TARGET | OTHER",
  "disposition": "RECOVERED_AT_BASELINE | RECOVERED_AT_TARGET | RECOVERED_DIVERGED",
  "actor_ref": "UNKNOWN",
  "attempt_effect_status": "AMBIGUOUS_REMOTE_STATE",
  "observation_interval_start": "<the APPLY_STARTED recorded_at>",
  "evidence_refs": ["evidence:sha256:…"]
}
```

`actor_ref` and `attempt_effect_status` are schema constants, and `disposition`
must restate `observed_relation`, so the record cannot attribute the state or
upgrade the attempt. The three `recovery_*` fields are present exactly when the
source is `OPERATOR_ATTESTED_OBSERVATION`. The observation interval opens at the
durable start's `recorded_at` and closes at this record's own ledger
`recorded_at`: taking the closing instant from the ledger keeps both ends on one
clock, which a caller-supplied end could not guarantee.

`CHILD_APPLY_RESULT` has the same identity fields, an `effect_status` of
`APPLIED_VERIFIED` or `AMBIGUOUS_REMOTE_STATE`, and bounded `evidence_refs`;
`APPLIED_VERIFIED` requires evidence.

The redacted intent restates only facts the approved subplan already contains.
`egress_hosts` is the subplan's declared allowlist, sorted and deduplicated;
`egress_allowlist_digest` digests the full declared `{host, purpose}` allowlist;
`intent_digest` digests the child's position (`plan_digest`, `subplan_digest`,
`attempt_ref`, `child_index`, `child_label`). The kernel deliberately does not
record which allowlisted target a child will actually use, because it does not
know and must not invent it. `child_label` is the one adapter-supplied value in a child
record, and it is constrained to a step name rather than a resource name: at
most 64 characters matching `^[a-z][a-z0-9]*(?:-[a-z0-9]*[a-z][a-z0-9]*)*$`,
which requires a letter in every hyphen-separated word. `create-ad-set` is
accepted; `123456789`, `campaign-88213`, `create-123`, and anything containing
`/` are refused with `CHILD_EGRESS_LABEL_INVALID` before any record is written.
No request body, provider response, or provider-assigned identifier is
recorded.
Every child payload passes `adhelm.privacy.validate_data` before it is appended,
and `tests/test_kernel_child_egress.py` asserts that on the written records.

## Evidence and limits

- All checks are hermetic and fake-only. No provider transport, credential,
  network call, or live account was involved, and none of this proves a provider
  accepts a multi-step hierarchy.
- `KernelExecution` serializes child scopes with an in-process lock, and permits
  live in a `ContextVar` that a new thread does not inherit. That contains
  accidental concurrent use inside one process. As `egress_permit.py` already
  documents, it is not a boundary against a hostile process with access to the
  same internals. Cross-process serialization remains the coordinator locks and
  the ledger.
- A `CHILD_APPLY_RESULT` append that itself fails leaves the child unresolved,
  which is the safe state; the kernel does not swallow that failure.
- Both release paths must actually read the current state, so a provider whose
  read is unavailable has no exit until read access is restored. That is
  deliberate: a record that claims to describe the current state cannot be
  written from an adapter's assertion alone. `reconcile_run` reports
  `RECONCILIATION_UNPROVEN` and records nothing in that case.

## Behaviour changes for existing adapters

No adapter's code changed. The kernel classifies two situations differently:

- A non-`CONFIRMED` acknowledgement from any provider is now
  `AMBIGUOUS_REMOTE_STATE` (CLI exit 6) where a non-Meta, non-GA4 adapter could
  previously reach `PARTIAL_EFFECT` (exit 5) or `FAILED_NO_EFFECT` (exit 3). The
  OpenAI cross-resource pause case is the one covered by an existing review
  document; see the dated note in
  `docs/implementation/openai-pause-ultra-review.md`.
- `reconcile_run` no longer settles a started-but-unverified attempt for any
  provider on a later matching read alone. Its `SubplanResult` is
  `AMBIGUOUS_REMOTE_STATE` and no `RECONCILIATION_RESULT` is written; instead an
  `ATTEMPT_RECOVERY_OBSERVED` record releases the barrier. Two suites recorded
  the previous provider-specific behaviour and were updated: the Meta PAUSE
  kernel tests now assert that reconciliation releases the barrier without
  attributing (a same-plan retry is refused as
  `AUTHORIZATION_ALREADY_CONSUMED` rather than `UNRESOLVED_APPLY`), and the
  auditor-mode reconciliation test now expects `AMBIGUOUS_REMOTE_STATE` instead
  of `APPLIED_VERIFIED` for an attempt whose post-apply readback failed.
- The attempt classifier previously matched only `operation == "PAUSE"`, a
  leftover from its Meta origin, so generalizing it in the first repair left the
  containment silently inactive for every other operation kind. It now matches
  the subplan's own operation.

## Known follow-ups

- No provider adapter uses `ChildEgressAdapter` yet. The first real multi-step
  adapter will need its own compiler, contract, approval material, and evidence.
- Permit consumption is enforced (landed after the S2 reviews): a guarded
  adapter that returns `CONFIRMED` without ever claiming its permit yields
  `AMBIGUOUS_REMOTE_STATE` with the `GUARDED_EGRESS_NOT_CLAIMED` diagnostic,
  and a journaled child whose permit was never claimed cannot resolve its
  `CHILD_APPLY_STARTED` however it reports. An unclaimed permit means the
  kernel has no evidence the write went through the guarded seam.
- Barrier scope is global; per-account scoping is a product-owner decision (see
  section 5).
- `record_attempt_recovery` gained its CLI surface in S4
  ([`generic-execution-route.md`](generic-execution-route.md)); the native-host
  approval subject that would make it usable is still S1's, and no installed
  host renders it.

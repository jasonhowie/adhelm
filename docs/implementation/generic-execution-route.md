# One provider-agnostic execution route, and the attempt-recovery commands

Last updated: 2026-09-09 (revised after independent review)

This is S4. It adds one generic execution leaf, two attempt-recovery leaves, and
the adapter registry they resolve through. It adds **no provider capability, no
credential, no scope, no installed host, no compiler, and no live evidence**.
`CONFIGURED_EXECUTION_ADAPTERS` is empty, the installed approval bridge refuses
both provider-neutral approval kinds, and the ordinary checked-in binding is not
installed at all, so every one of these commands fails closed before any
credential, native helper, or provider request.

It closes two deferrals recorded elsewhere:

- [`kernel-generalization.md`](kernel-generalization.md) §5 left
  `SafetyKernel.record_attempt_recovery` with no command at all — "S4/S1 own
  that" — so the only exit from the global write barrier needed a private
  library integration.
- [`execution-plan-records.md`](execution-plan-records.md) and
  [`composite-execution-plan-approval.md`](composite-execution-plan-approval.md)
  produced a provider-neutral retained plan (S3) and a provider-neutral approval
  subject (S1) with nothing that executes them.

## The shape

```text
adhelm execute plan <record-digest>
adhelm recover attempt <apply-started-record-digest>
adhelm recover catalog [--cursor N] [--limit N]
```

None of the three takes an input file. One positional record digest is the
entire input: no receipt, challenge, trust pin, key, grant, candidate override,
adapter, transport, credential, provider request, artifact option, or provider
and operation selector. Which provider and operation run is decided by the
retained plan and by which adapter a provider ticket registered — never by the
command line. `discover` states this as
`execution_selection: POSITIONAL_RECORD_DIGEST_ONLY_NO_INPUT_FILE`.

`discover` also renders the ordered refusal gates and the code the first
unsatisfied one produces, both derived rather than restated, so the catalogue
cannot name a refusal the caller never reaches:

```json
"execution_refusal_gates": [
  {"gate": "RELEASE_POLICY_WRITES_RELEASED",             "satisfied": false, "refusal_code": "WRITES_NOT_RELEASED"},
  {"gate": "COMPILED_APPROVAL_BRIDGE_INSTALLED",        "satisfied": null,  "refusal_code": "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"},
  {"gate": "APPROVAL_KIND_RENDERED_BY_INSTALLED_HOST",  "satisfied": false, "refusal_code": "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED"},
  {"gate": "CONFIGURED_EXECUTION_ADAPTER",              "satisfied": false, "refusal_code": "EXECUTION_ADAPTER_NOT_CONFIGURED"}
],
"execution_first_refusal_code": "WRITES_NOT_RELEASED"
```

The release-policy gate is first and is never satisfied in a 0.1.0 build. The
compiled-binding gate is `null` deliberately: deciding it means resolving the
compiled binding, and `discover` and `schema` are static reads that must not. A
gate a static read cannot decide is treated as one that may refuse, because
skipping past it would name a later code the caller never gets to. In an
installed build that renders neither provider-neutral kind, the earliest bridge
refusal is named while the bridge might produce its sibling code; both appear in
the gate list, both are bridge refusals, and both precede any credential.

The two request models are registered as installed schemas
(`execution-plan-installed-approval-execution-request`,
`attempt-recovery-installed-approval-request`) because they are the typed
library requests the workflows accept, exactly as
`ga4-key-event-create-plan-record` is a registered record shape. Neither is a
file any command reads.

### The three fixed leaves are unchanged

`execute openai-pause`, `execute ga4-key-event-create`, and
`execute meta-campaign-pause` keep their `--input REQUEST.json` selection, their
request schemas, their readiness boundaries, their result projection, and their
exit mapping byte for byte. Their contracts remain
[`installed-approval-execution-cli.md`](installed-approval-execution-cli.md) and
[`meta-pause-execution-cli.md`](meta-pause-execution-cli.md).

The mapping onto the generic leaf, for when a provider ticket supplies the
compiler and adapter that make it real:

| Fixed leaf | Retained record | Approval kind | Generic equivalent |
| --- | --- | --- | --- |
| `execute openai-pause` | `EXECUTION_PLAN` (OpenAI pause action) | `OPENAI_PAUSE_PLAN` | `execute plan` over an `openai_ads` `PAUSE` `EXECUTION_PLAN_RETAINED` record |
| `execute ga4-key-event-create` | `GA4_KEY_EVENT_CREATE_PLAN` | `GA4_KEY_EVENT_CREATE_PLAN` | `execute plan` over a `ga4_admin` `CREATE` record |
| `execute meta-campaign-pause` | `META_CAMPAIGN_PAUSE_PLAN` | Meta V2 bridge | `execute plan` over a `meta_ads` `PAUSE` record |

**Aliasing the fixed leaves onto the generic one is deliberately not done here.**
Each of the three carries provider-specific work the generic route does not
have: OpenAI's configuration/credential-binding transaction, GA4's typed fresh
candidate source, and Meta's separate V2 readiness boundary. Redirecting a
command that has live-evidence obligations to a different code path without live
evidence would be exactly the silent substitution AGENTS.md rule 20 forbids.
That waits for P02 live evidence.

## The adapter registry

```python
CONFIGURED_EXECUTION_ADAPTERS: Mapping[tuple[str, OperationKind], ConfiguredExecutionAdapterFactory]
register_execution_adapter(provider: str, operation: OperationKind, factory) -> None
configured_execution_adapter(provider, operation) -> ConfiguredExecutionAdapterFactory | None
executable_execution_operations() -> tuple[dict[str, str], ...]
```

It is the sibling of `CONFIGURED_EXECUTION_PLANNERS` in
`execution_planning_session.py` and follows it exactly: a private `dict`, a
`MappingProxyType` public view so the emptiness cannot be revoked by an
incidental mutation from an unrelated import, and a registration function that
**refuses a duplicate pair** rather than replacing it — two factories for one
operation would make which credentials and transport executed an approved plan
depend on import order. `OperationKind.RECOVER` is not registrable: a recovery
reads the state of an attempt some other operation started and reuses that
subplan's adapter.

`executable_execution_operations()` is rendered by `discover` at call time, so
the catalogue can never advertise an operation no adapter executes or omit one a
provider ticket registered. It is `[]` today.

The seam a provider execution ticket implements:

```python
def declared_capabilities(self) -> ExecutionAdapterCapabilities: ...  # child_egress: bool
def open_configured_adapter(
    self, directory: Path, record: ExecutionPlanRecord, ledger: Ledger, *, clock
) -> AbstractContextManager[GuardedAdapter]: ...
```

`declared_capabilities` is answered from the factory's own compiled contract and
must not read configuration, open a credential store, or contact a provider: the
route asks for it while deciding whether the plan is executable at all, before
the bridge builds or consumes a receipt. The opened adapter is checked against
the declaration again before the kernel runs, so a factory that declares a
capability it does not implement is refused rather than believed.

The factory owns exactly what the generic route cannot: that provider's
credential and configuration locking, its fresh authentication handoff, its HTTP
lifetime, and its `GuardedAdapter` (or `ChildEgressAdapter`) implementation. The
returned context manager owns the adapter's whole lifetime; the kernel executes
inside it and it is closed before the route returns.

## The execute route's order

`execute_retained_execution_plan_with_installed_approval(directory, request, *,
loader, adapter_factory, expected_kind)` exists for its order:

0. the release-policy gate, which belongs to the command rather than this
   function (`execute plan` and `recover attempt` are declared remote-mutation
   leaves) and refuses with `WRITES_NOT_RELEASED` before anything below runs in
   a 0.1.0 build -- see [`release-policy-gate.md`](release-policy-gate.md);
1. the request shape, then the retained
   `EXECUTION_PLAN_RETAINED` record from a **verified checkpoint**
   (`load_execution_plan_record`), then the plan's own currency — workspace
   binding, expiry, **supersession**, and the unresolved-attempt barrier. All
   local reads. A plan this workspace's own history records as replaced is
   refused with `EXECUTION_PLAN_SUPERSEDED` (exit 3) naming the successor's
   record digest: executing the predecessor is not an authority bypass — the
   approval binds its own digests and the kernel still rejects drift — but it
   applies a decision local history says was superseded, from a strictly older
   bounded read. `plan execution catalog` rows now carry `superseded_by`, the
   reverse of the `supersedes_record_digest` they already carried, so a reader
   can tell a current plan from a replaced one without walking the chain.
2. the adapter for `(provider, operation)`, refused with
   `EXECUTION_ADAPTER_NOT_CONFIGURED` (exit 3) **before the bridge**, so a
   one-use receipt is never consumed for a plan nothing can execute; then the
   registered factory's own capability declaration against this plan, refused
   with `EXECUTION_ADAPTER_INCOMPATIBLE` (exit 3). A subplan whose
   `expected_child_egress_count` exceeds one is executable only by a
   `ChildEgressAdapter`; the kernel refuses anything else in `_preflight_one`,
   but by then the receipt is spent and a retry would need a fresh human review
   of a plan that was never executable. The refusal message names the kernel
   code (`ADAPTER_CHILD_EGRESS_REQUIRED`) it forestalls.
3. the installed bridge, which builds the `COMPOSITE_EXECUTION_PLAN` challenge,
   runs the native exchange, consumes the receipt, and records both the claim
   and a `DirectGrant` bound to the plan digest and every subplan digest. This
   route never sees a receipt, nonce, challenge, or trust pin.
4. a reload of the same record, compared byte for byte, because approval took
   time this process does not own; then the currency checks again.
5. the grant itself: wrong plan is `COMPOSITE_EXECUTION_APPROVAL_REQUIRED`, and
   an already-expired grant is `AUTHORIZATION_EXPIRED` — both **before** the
   adapter opens, because the factory is the first step that may reach a
   credential store or a provider. The kernel refuses the same grant a moment
   later; refusing here only keeps the order this function exists for.
6. `SafetyKernel.execute`, which owns every remaining check, the durable
   `APPLY_STARTED`, the one-use permit, the readback, and the recovery facts.

The bridge seam is private (`_resolve_composite_approval_bridge`), so an
installed product — or a test — replaces exactly one function instead of
widening a public signature. The adapter seam is private the same way:
`_adapter_registry` replaces the whole registry rather than injecting one
factory, so every resolution — production and test — is a lookup by the exact
`(provider, operation)` key the retained plan names, and a factory can never be
supplied for a pair it is not registered under. Below the release-policy gate,
the only bridge this checkout ships refuses both provider-neutral kinds in the
installed bridge's own order: `NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` first,
because this checkout has no reviewed compiled binding at all, then
`NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED`, because even a build that installs
one still has no signed host that renders a provider-neutral subject. Neither is
a claim that the refused kind is unsafe.

A retained record holds exactly one subplan (`execution-plan-records.md`), so
the route executes one provider per plan. Multi-provider composites remain the
separate decision `kernel-generalization.md` §3 records.

## The recovery route

`recover attempt` is the command `kernel-generalization.md` §5 deferred. It
sends no provider mutation, attributes nothing, settles nothing, and never
upgrades the attempt's effect status. Its order mirrors the execute route:

1. the exact unresolved durable `APPLY_STARTED` selected from
   `Ledger.unresolved_starts()` by record digest, refused with
   `ATTEMPT_RECOVERY_START_NOT_FOUND` when it is absent, already recovered, or
   ambiguous. A digest that names an unresolved `CHILD_APPLY_STARTED` instead
   is the wrong *level*, not an unknown record: the kernel records one
   observation for the whole approved operation and that release resolves every
   child of it, so this is refused with
   `ATTEMPT_RECOVERY_CHILD_START_NOT_RECOVERABLE` naming the parent attempt's
   own `APPLY_STARTED` digest;
2. the provider-neutral retained plan for that attempt, rebuilt from
   `EXECUTION_PLAN_RETAINED` history. An attempt started from one of the
   provider-specific retained families has no such record and is refused with
   `ATTEMPT_RECOVERY_PLAN_NOT_RETAINED` rather than reconstructed from the
   attempt's own digests — that would be a plan nobody compiled;
3. the attempt's **original** authorization, rebuilt field by field from its
   durable `APPROVAL_GRANTED` event and `APPROVAL_RECEIPT_CLAIMED` record and
   accepted only when the rebuild reproduces the `authorization_digest` the
   kernel appended. Nothing is invented; a delegated attempt has no such grant
   and is refused with `ATTEMPT_RECOVERY_AUTHORIZATION_UNVERIFIED`. This is the
   authorization the attempt *started under*, used to find that exact
   `APPLY_STARTED` — not the authorization that permits the recovery;
4. the adapter, again before the bridge;
5. the bridge, for the separate one-use `ATTEMPT_RECOVERY` grant whose subject is
   `attempt_recovery_subject(plan, subplan, started_record_digest=...)`, so a
   grant issued for one lost acknowledgement can never release another;
6. `SafetyKernel.record_attempt_recovery`, which takes a fresh identity check and
   `read_state` under the coordinator lock and appends
   `ATTEMPT_RECOVERY_OBSERVED`;
7. a reread of that exact record. The emitted envelope carries its record
   digest, `observed_relation`, `disposition`, `actor_ref` (`UNKNOWN`),
   `attempt_effect_status` (`AMBIGUOUS_REMOTE_STATE`), and the fixed statements
   `attribution: NO_ACTOR_ATTRIBUTED`, `provider_change_applied: false`,
   `attempt_settled: false`, `retry_authorized: false`.

`recover catalog` lists both levels the kernel refuses on — an unresolved
subplan `APPLY_STARTED` (`UNRESOLVED_APPLY`) and an unresolved journaled
`CHILD_APPLY_STARTED` (`UNRESOLVED_CHILD_APPLY`) — from hash-chain-verified
local history, with `blocking_effect: BLOCKS_EVERY_LATER_GUARDED_WRITE`. Its
`recoverable` flag states only whether `recover attempt` can act on that row
here; it never implies that recovering it makes a retry safe.

## Output policy and exits

`execute plan` reuses the fixed `EXECUTION_RESULT` projection: plan digest,
`authorization_consumed`, `recovery_required`, one provider status and subplan
digest, bounded opaque resource and evidence references, a fixed recovery
instruction, and `history status`. It omits authorization identifiers, receipt
or key material, grants, approval-bridge details, credential facts, provider
payloads, and raw adapter diagnostics. Its status-to-exit mapping is the shared
one: `APPLIED_VERIFIED` → 0, `PARTIAL_EFFECT` → 5, `AMBIGUOUS_REMOTE_STATE` → 6,
`FAILED_NO_EFFECT`/`NOT_STARTED` → 3. Every non-success outcome still emits the
bounded result first, and no ambiguous write is retried.

Two deliberate differences from the three fixed leaves:

- The result model's `opaque_resource_refs` bound is `MAX_CHILD_EGRESS_COUNT`
  rather than 1, because one approved generic subplan may issue several
  journaled child requests and therefore name several created resources. The
  bound is the kernel's own, not a new number. Each fixed leaf still applies its
  own maximum of 1 before building the surface, so it cannot present a second
  resource reference. `execute plan` reserves the longest installed provider
  name and the full child bound before any workflow work, so it requires a
  larger saved `max_output_bytes` than the fixed leaves and refuses to exceed it
  silently.
- `expected_provider` is `None` for the generic leaf: which installed provider
  executed is decided by the retained plan, and the workflow already bound the
  result to that plan's own subplan before it returned.

`recover attempt` is **not** an execution result and deliberately does not carry
that exit mapping. Recording an observation is the command's whole purpose, so
it exits 0 with outcome `ATTEMPT_RECOVERY_OBSERVED_NO_ATTRIBUTION`; the closed
fields, not the exit code, are where the envelope says the attempt itself stays
unattributed, unsettled, and unsafe to retry. Its envelope is reserved before
the bridge for the same reason the plan's is: a recovery grant is one-use, and
burning one on an unpresentable result would leave the barrier in place with no
second approval available. A post-append failure to present the observation is
`ATTEMPT_RECOVERY_RESULT_UNAVAILABLE` (exit 6) with "do not repeat the
recovery", never a retry.

`recover catalog` is a local history read: the release-policy gate does not
apply to it, and it never resolves the compiled binding.

## Validation

`tests/test_generic_execution_route.py` drives the workflows with a fake
*installed* layer and a real everything-else: a real signed receipt from an
ephemeral test key, the real `accept_composite_execution_plan_receipt` /
`accept_attempt_recovery_receipt`, a real `SafetyKernel`, and a real local
ledger. It covers the retained plan → receipt → digest-bound grant → kernel
execute → readback → envelope path; a receipt for another plan; a replayed
receipt; a grant for another plan; an expired grant; the unconfigured bridge
refusal with a recording credential store proving no store was touched;
`EXECUTION_ADAPTER_NOT_CONFIGURED` before the bridge with no claim record; the
unresolved-attempt barrier; the recovery happy path with `actor_ref UNKNOWN`;
and the catalog's subplan row, journaled child row, pagination, and refusals.
It also covers the review repairs: the successor stays executable and carries
`superseded_by`; a declaring factory that opens a non-declaring adapter is
refused before the kernel; an invalid declaration is refused before the bridge;
the registry seam resolves only by the plan's own pair; the advertised first
refusal code is the one this build produces; and a real three-child plan whose
second child dies proves the child-level recovery refusal names its parent.

`tests/test_generic_execution_route_review.py` is the independent reviewer's
suite: a superseded predecessor is refused before the bridge with its successor
named, and a plan no installed adapter can execute never consumes a receipt.

`tests/test_generic_execution_cli.py` covers the command boundary with synthetic
workflow returns: discovery and schema as static reads, the registry-derived
`executable_operations`, malformed selectors, output reservation, the redacted
envelopes, the exit mapping, the wider generic resource bound against the fixed
leaves' narrower one, and the catalog's empty, listed, shrinking, and refused
pages.

`scripts/validate_development.py` adds `generic_execution_refusal_smoke`: in the
installed wheel, `execute plan <64 zeros>` and `recover attempt <64 zeros>`
refuse with `WRITES_NOT_RELEASED` and exit 3 before any credential, and
`recover catalog` returns an empty bounded envelope.

## Evidence and limits

- Every check is hermetic. No provider transport, credential store, native
  helper, approval host, operating-system authentication, or network call was
  involved, and none of this is evidence that a provider accepts anything.
- The registry is empty, so no retained plan is executable in this build. The
  execute and recovery leaves are registered command surfaces, not a capability.
- A registered adapter is a separate reviewed change with its own contract,
  credentials, transport, and live evidence. Registering one makes
  `execute plan` reach that provider;
  `test_no_configured_execution_adapter_ships` pins the emptiness so it cannot
  change silently.
- `unresolved_attempt_catalog` reads the `changes` stream more than once (the
  retained-plan set, then each unresolved level). Each read is hash-chain
  verified, but the three are not one cross-stream checkpoint, so the catalog is
  a bounded local snapshot rather than a `checkpoint_digest`-bearing vector; it
  reports `history_verification: HASH_CHAIN_VERIFIED_LOCAL_SCAN` rather than
  claiming otherwise.
- The write barrier stays global across providers and accounts. Per-account
  scoping remains the product-owner decision `kernel-generalization.md` §5
  records.
- Releasing the barrier does not make a retry safe. The original plan stays
  one-use, and any further mutation needs a new plan compiled from a fresh read.

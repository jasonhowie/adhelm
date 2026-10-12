# Provider-neutral retained execution plans

Last updated: 2026-09-09

This is the generic retained-plan record, its verified-history selection, its
provider-agnostic planning session, and the generic `plan execution` command
family. It exists so the six providers' CREATE, ACTIVATE, and MAINTAIN tickets
do not each re-implement the three module families the OpenAI, Meta, Google
Ads, and GA4 pause/create paths currently carry.

## Scope and non-scope

It adds **no provider capability, no credential, no scope, no approval host, no
kernel route, and no live provider evidence**. `plan execution create` fails
closed today for every `(provider, operation)` pair, because
`CONFIGURED_EXECUTION_PLANNERS` is empty: no provider compiles a plan through
this seam yet. Nothing here promotes a capability's declared status, and a
retained record grants no execution authority — every kernel check still runs at
execution time against a fresh read.

The four existing retained-plan records, their commands, their artifact
renderers, and their tests are unchanged. This ticket deliberately does not
alias them onto the new kind; S4 owns that.

## The record

`EXECUTION_PLAN_RETAINED` is an additive kind in the existing `changes` stream.
`src/adhelm/execution_plan_records.py:ExecutionPlanRecord` holds:

| Field | Meaning |
| --- | --- |
| `provider`, `operation` | The subplan's own provider and `OperationKind`. `RECOVER` is refused: it authorizes recording an observation and sends no provider request, so `SubPlan` already refuses it and the record refuses a restatement that disagrees. |
| `plan` | The exact `adhelm.safety.CompositePlan` the kernel executes — a single `SubPlan` wrapped as a one-provider composite. Retaining the plan object, not only its digest, is what lets a later approval acceptance reload it from a verified checkpoint instead of taking a caller-supplied object (see `composite-execution-plan-approval.md`, "Known follow-ups"). |
| `plan_digest`, `subplan_digests` | The canonical-bytes digests, restated and revalidated against the embedded plan. |
| `expected_child_egress_count` | The subplan's declared number of provider requests, `1..MAX_CHILD_EGRESS_COUNT`. |
| `review_lines`, `review_source` | The bounded redacted review, derived by S1 and validated by `adhelm.approval_protocol.validate_review_lines`. See below. |
| `spend_bounds_by_currency` | Per-currency maxima using the S1 `NOT_SPEND_CAPABLE` rule. An empty list *is* `NOT_SPEND_CAPABLE`. |
| `expires_at` | The compiled plan's own expiry, restated. |
| `source_continuity` | The identity fingerprint and observed `before` state the plan was compiled against, plus `read_started_at`, `read_finished_at`, `read_consistency`, and `connector_version`. |
| `supersedes_record_digest` | The exact earlier retained record this plan replaces, or `null`. |
| `provenance` | Workspace, account, auth profile, provider API and capability versions, `compiled_by: LOCAL_CLI`, `capability_status`, `live_validated: false`. |
| `historical_evidence_status`, `execution_authority`, `requires_current_kernel_checks` | The same three fixed controls the four existing plan records carry. |

Every field is either a closed enum, a bounded scalar, a digest, or a restated
fact recomputed from the embedded plan. `StrictModel` runs
`adhelm.privacy.validate_data` over the whole record on construction and on every
revalidation, and the ledger runs it again before the payload is appended.
There is no raw provider payload, request body, response, secret, or free text
in the record; the only prose is `review_lines`, which passes the closed S1
line validator.

### Source continuity

`identity_fingerprint` is
`sha256(canonical({domain: "adhelm.execution-plan.source-identity.v1", provider,
account}))` over the subplan's own account binding, so it is recomputable at
revalidation and can never become an unverifiable claim. `observed_state` must
equal the subplan's `before`. The read interval and connector version are the
only facts the plan itself does not already hold.

`build_execution_plan_record` refuses, with
`EXECUTION_PLAN_SOURCE_CONTINUITY_MISMATCH`, a plan whose `before` is not the
supplied read's observed state — before any append. That is the retention-time
half; the record validator is the durable half, so a payload edited after the
fact cannot claim a read it does not describe.

### Review lines reuse the S1 derivation

The retained review is exactly what an approval host will render for a
`COMPOSITE_EXECUTION_PLAN` challenge, so a reviewer never sees a sentence the
retained record does not contain:

- the derived spend prefix — one
  `Maximum incremental spend <n> minor units <CUR>` line per currency bound, or
  the single `Maximum incremental spend NOT_SPEND_CAPABLE` line — leads the
  list, and is recomputed from `spend_bounds_by_currency` on every
  revalidation;
- the remaining lines come from
  `adhelm.approval_protocol.composite_subplan_review_lines(subplan)` when the
  candidate has no compiler review (`review_source:
  COMPOSITE_SUBPLAN_INTENT`), or from
  `adhelm.approval_protocol.compiler_review_lines(provider, redacted_review)`
  when it does (`COMPILER_CANDIDATE_REVIEW`);
- the whole list then passes `validate_review_lines(..., derived=...)`, so a
  supplied line still cannot make a spend or authorization claim.

`tests/test_execution_plan_records.py::test_review_lines_equal_the_s1_derivation_byte_for_byte`
rebuilds the S1 subject with `expected_composite_approval_subject` and requires
byte equality of both `review_lines` and `spend_bounds_by_currency`. The two
small local derivations (`derived_execution_review_lines`,
`execution_plan_spend_bounds`) restate S1's private helpers; that test is what
stops them drifting.

### One provider per record

`ExecutionPlanRecord` binds exactly one subplan. The ledger envelope carries a
single `provider` and a single `account_ref`, and the envelope validator
requires both to equal the record's own. Retaining a genuinely multi-provider
composite means deciding what those envelope fields mean, which is its own
ordering and account-binding analysis — see `kernel-generalization.md` §3 for
the analogous reasoning about subplans. The `spend_bounds_by_currency` list
shape is kept anyway so the derivation generalizes without a schema change.

## Ledger bindings

`src/adhelm/ledger.py` gains only additive entries. Nothing existing is removed
or reordered.

- `_KINDS["changes"]["EXECUTION_PLAN_RETAINED"] = ExecutionPlanRecord`.
- `_validate_payload` routes the kind through
  `revalidate_execution_plan_record`, joining the existing GA4/Meta/Google
  revalidation chain.
- `_make_record` lists the kind as a `local_compilation`, so `occurred_at` and
  `observed_at` are null: a compiled plan is not a provider occurrence.
- `validate_execution_plan_envelope` runs in `append` and in `_verify_record`,
  binding workspace, provider, and account and refusing a run identifier or an
  occurrence/observation timestamp.
- `validate_execution_plan_history` runs under the `changes` append lock with
  the prospective record, and on every physical scan.

## Predecessor linkage

A plan that supersedes another names its exact record digest. The history
validator requires the named predecessor to be retained earlier in the same
stream, to be an `EXECUTION_PLAN_RETAINED`, to describe the same workspace,
provider, operation, and account, to have a different plan digest, and not to
have been superseded already. A chain is therefore linear: two plans cannot
fork from one predecessor, and a supersession cannot be claimed backwards.
Failures are `EXECUTION_PLAN_PREDECESSOR_CONFLICT`.

## Verified-history selection

`src/adhelm/saved_execution_plans.py` mirrors the `saved_*` pattern:

- `retain_execution_plan_record(ledger, record, *, before_durable_append=None)`
  — the optional callback runs under the ledger stream lock immediately before
  the durable append, using only already-held state, exactly as the Google F01
  retainer documents. It never retries a partial local write.
- `load_execution_plan_record` / `load_execution_plan` — select from
  `Ledger.checkpoint()` only. The checkpoint scans and revalidates every record
  under ordered stream locks, so an unverified or truncated tail raises before
  any plan is returned. There is no path here that reads the rebuildable index.
- `execution_plan_catalog(..., provider=, operation=, account_ref=)` — newest
  first, bounded pagination, filters applied before pagination so
  `total_count` and `next_cursor` describe the filtered selection.

## The planning session

`src/adhelm/execution_planning_session.py` performs **no egress of its own**.

```python
plan_execution(
    ledger: Ledger,
    candidate: CompiledExecutionCandidate,
    *,
    read: BoundedExecutionRead,
    plan_id: str,
    profile_ref: str,
    expires_at: datetime,
    now: datetime,
    supersedes_record_digest: str | None = None,
    before_durable_append: Callable[[], None] | None = None,
) -> RetainedExecutionPlan
```

This is the seam P04/P05 call after their own configured read session returns.
It compiles the `SubPlan` and its one-provider `CompositePlan` with `before`
taken from the injected read and `after` from the candidate, derives the review,
builds the record, and retains it. Three lower-level functions are public for
callers that need them separately: `compile_execution_plan`,
`execution_plan_review`, `build_execution_plan_record`, and
`retain_execution_plan`.

`BoundedExecutionRead` carries `observed_state`, `read_started_at`,
`read_finished_at`, `connector_version`, and a fixed
`read_consistency: SEQUENTIAL_NONATOMIC`. The session never produces one: a
caller that cannot supply a completed read cannot retain a plan, which is the
point.

`CompiledExecutionCandidate` carries the provider, operation, subplan
identifier, account binding, API and capability versions, predicted `after`
state, field changes, spend cap, egress allowlist, recovery actions or
disposition, child count, optional compiler `redacted_review`, and capability
status.

Retention discipline, matching Google F01:

1. refuse an already expired plan (`EXECUTION_PLAN_EXPIRED`) before any append;
2. refuse a read the retention instant does not admit (below), also before any
   append;
3. append durably;
4. reload from a fresh verified checkpoint and compare the reloaded record's
   canonical bytes and provenance digests with the appended one;
5. report `EXECUTION_PLAN_RECEIPT_UNVERIFIED` (exit 6, ambiguous) on any
   failure after step 3, because a completed append cannot be distinguished
   from a failed one from there. Callers inspect local history; they do not
   retry.

### The retention clock window

`retain_execution_plan` bounds the retained observation interval against the
retention instant `now`, before any append:

- `read_finished_at > now` is `CLOCK_INVALID` (exit 2). A record claiming a
  read that has not happened would describe an observation that does not
  exist. The GA4 CREATE family this session generalizes already refuses exactly
  this (`ga4_create_workflow.py`, `CLOCK_INVALID`), and the generic seam is not
  allowed to be weaker than the family it replaces.
- `now - read_finished_at > expires_at - now` is `EXECUTION_PLAN_READ_TOO_STALE`
  (exit 3). The plan declares how long its picture of the provider is worth
  authorizing; an observation already staler than that whole remaining window
  was never worth retaining under it.

A stale-but-in-window read is deliberately **allowed** and retained honestly.
Local retention cannot prove the provider still matches, and it must not
pretend to: such a plan is refused later by the kernel's drift check against a
fresh read at execution time, which is where that comparison belongs.

### The configured-planner registry

`CONFIGURED_EXECUTION_PLANNERS` maps `(provider, OperationKind)` to a
`ConfiguredExecutionPlanner`:

```python
def plan_configured_execution(
    self, directory: Path, request: ExecutionPlanRequest, *, expected_configuration_digest: str
) -> RetainedExecutionPlan: ...
```

A configured planner owns what the generic command cannot: that provider's
credential and configuration locking and its own bounded read session.

The registry is empty, and that emptiness is structural rather than incidental:
the public name is a `MappingProxyType` over a private dict, so it cannot be
mutated by an unrelated import. A provider ticket calls
`register_execution_planner(provider, operation, planner)`, which validates the
provider and operation against the installed allowlists, refuses `RECOVER`, and
refuses a duplicate pair rather than replacing it — two planners for one
operation would make which compiler produced a retained plan depend on import
order. `compilable_execution_operations()` renders the registered pairs for
`discover`.

## The command family

`plan execution create --provider <p> --operation <op> --input <request.json>`,
`plan execution catalog`, `plan execution show <digest>`, registered from
`src/adhelm/plan_cli.py:register_commands`. The existing `plan openai-pause`,
`plan catalog`, `plan show`, `plan ga4-key-event`, `plan meta-campaign-pause`,
and `plan google-ads-campaign-pause` commands are untouched.

`create` order, matching the Google F01 command:

1. validate `--provider` and `--operation` against the closed allowlists
   (`RECOVER` refused, exit 2);
2. strict regular-file parse of `ExecutionPlanRequest`, before configuration,
   output policy, credentials, or any planner;
3. require the request file to name the same provider and operation;
4. load configuration and resolve the F02 artifact policy through
   `adhelm.artifacts.resolve_artifact_policy` with
   `ArtifactContext(workflow="plan", stage="before_approval",
   purpose="plan_review", provider=..., profile_id=...)` — the same resolution
   the Meta and Google Ads pause commands use. An incomplete matching rule is
   `ARTIFACT_CONFIGURATION_REQUIRED` (exit 3) and names `artifacts plan`;
5. reserve a complete receipt against `max_output_bytes` before any planner
   runs (`OUTPUT_LIMIT_EXCEEDED`, exit 3);
6. look up the registered planner. **There is none**, so the command fails
   closed with `EXECUTION_PLAN_NOT_COMPILABLE` (exit 3) and a message naming
   the exact provider and operation. There is no stub success, no partial
   receipt, and no retained record.

The receipt's `artifact_policy` notice carries `generation_supported: false`
and names `artifacts plan`: the generic family has no renderer of its own, and
saying so is more useful than implying a file this command wrote.

`catalog` and `show` return bounded redacted envelopes with the same exit codes
and output preflight as F01. `show` sizes the complete review with a synthetic
zero key before the private sharing key is touched, and an expired plan is
still reviewable, marked `EXPIRED_AT_REVIEW`.

`discover` advertises the three commands in `_COMMANDS`, the request schema as
`execution-plan-request`, and an `execution_plan` block whose
`compilable_operations` is rendered from
`compilable_execution_operations()` at call time rather than hard-coded, so the
catalogue can never advertise an operation no planner compiles or omit one a
provider ticket registered. It is `[]` today. AGENTS.md rule 27 makes that
catalogue the agent's only way to find the command.

`ExecutionPlanRequest` is closed: `schema_version`, `profile_id`, `provider`,
`operation`, `account_ref`, optional `resource_ref`, optional
`supersedes_record_digest`. A provider whose planner needs more extends it as a
discriminated union member in its own ticket. Today the command refuses an
unknown field rather than handing an uninterpreted body to a planner that does
not exist.

## The review surface

`src/adhelm/execution_plan_views.py` builds an explicit allowlisted preimage
rather than redacting the whole record, so a future record field cannot reach
an agent, an artifact, or a support bundle without a deliberate change there.
`projections._redact_payload` then HMACs every reference with the private local
sharing key. The review shows the provider, operation, digests, child count,
opaque workspace/account/auth references, API and capability versions, the
predicted state fingerprint, the declared egress hosts, recovery actions, the
review lines, the spend summary, the source interval and connector version, the
predecessor digest, expiry and expiry status, and the fixed non-authority
controls.

`history export` currently applies the generic recursive redaction to the raw
record rather than this narrower preimage. That is safe — the record holds no
raw payload and every `*_ref`, `*_id`, and `*_handle` is pseudonymized — but a
narrower export branch in `projections.py` belongs to that module's owner and is
listed as a follow-up.

## Mapping of the existing families

Nothing is aliased in this ticket, and S4 deliberately did not alias them
either; see [`generic-execution-route.md`](generic-execution-route.md) for why
that waits for P02 live evidence. This is the correspondence:

| Existing | Generic equivalent |
| --- | --- |
| `plan openai-pause` / `EXECUTION_PLAN` / `plan_records.py`, `saved_plans.py`, `plan_views.py` | `plan execution create --provider openai_ads --operation PAUSE`; `EXECUTION_PLAN_RETAINED`; `execution_plan_records.py`, `saved_execution_plans.py`, `execution_plan_views.py` |
| `plan meta-campaign-pause create` / `META_CAMPAIGN_PAUSE_PLAN` | `--provider meta_ads --operation PAUSE` |
| `plan google-ads-campaign-pause create` / `GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN` | `--provider google_ads --operation PAUSE` |
| `plan ga4-key-event create` / `GA4_KEY_EVENT_CREATE_PLAN` | `--provider ga4_admin --operation CREATE` |
| each family's `catalog` / `show` | `plan execution catalog --provider <p> --operation <op>` / `plan execution show <digest>` |

The four provider-specific records each wrap a provider contract's
`PlannedAction` with contract-specific bindings the generic record does not
carry, so aliasing is a migration with its own compatibility and history
questions, not a rename.

## Evidence and limits

- Everything here is hermetic and fixture-only. No provider transport,
  credential, network socket, live account, approval host, or Keychain was
  involved; `tests/conftest.py` blocks the network for these tests.
- **No plan can be created through the installed command.** The one retention
  path exercised end to end uses an in-memory fake planner that injects a
  fixture read. That proves the command, session, ledger, and review wiring; it
  proves nothing about any provider.
- The pinned digests in `tests/test_execution_plan_records.py` describe the
  fixed in-memory fixture only. They are canonical-byte regressions, not
  provider evidence.
- Retention grants no execution authority and refreshes no remote state. A
  retained plan whose `before` no longer matches the provider will be refused
  by the kernel's drift check at execution time, which is where that check
  belongs.
- The record's `capability_status` is
  `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED` and `live_validated` is fixed
  `false`.
- `tests/test_execution_plan_records.py` (40 tests),
  `tests/test_execution_plan_cli.py` (15 tests), and the independent reviewer
  suite `tests/test_execution_plan_records_review.py` (3 tests) cover record
  round-trip and pinned digests, the byte-for-byte S1 review derivation, spend-summary
  tampering, `RECOVER` refusal, the privacy validator, source-continuity
  mismatch at retention and at revalidation, expiry, the predecessor chain
  including a forked and an unretained predecessor, unverified-tail refusal,
  catalog filters and pagination bounds, `history verify` / `checkpoint` /
  `export` / index rebuild with the kind present, the bounded review and its
  output bound, the `NOT_COMPILABLE` failure, the output preflight and artifact
  policy ordering, the retention clock window in all three directions, the
  read-only registry and its duplicate and invalid registration refusals,
  `discover` parity and truthfulness, and that the existing pause commands
  still resolve.
- `scripts/validate_development.py::execution_plan_smoke` runs the same refusal
  against the installed wheel with sockets, the credential backend, and
  subprocess launching all patched to raise, and asserts that a refused create
  retained no local history.
- `tests/test_command_input_files.py` covers `plan execution create` in the
  shared symlink / FIFO / oversized-input hardening battery.

## Known follow-ups

- S4 added the generic `execute plan` route and the `record_attempt_recovery`
  command surface ([`generic-execution-route.md`](generic-execution-route.md)).
  Aliasing the four existing families onto this kind is still open and waits for
  P02 live evidence.
- Approval acceptance still takes a caller-supplied `CompositePlan`. Now that a
  compiled composite is retained, `accept_composite_execution_plan_receipt` can
  take a `source_plan_record_digest` and reload it from a verified checkpoint;
  that change is S1's.
- A narrower `EXECUTION_PLAN_RETAINED` branch in
  `projections._redact_payload`, if that module's owner wants the export to use
  the same preimage the review uses.
- A multi-provider composite record, if the ledger envelope's single provider
  and account binding is revisited.
- No artifact renderer exists for the generic family.

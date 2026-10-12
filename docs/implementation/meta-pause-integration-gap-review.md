# Meta campaign PAUSE integration gap review

This is a source-only design review of the future mapping in
[`docs/research/meta-ads-pause-contract.md`](../research/meta-ads-pause-contract.md),
against the paused-plan, native-approval, and kernel code present after M39.
It does not add a provider request, a credential read, a plan, a native
approval kind, a mutation adapter, or a command.

The current M39 EDIT session and campaign reader are useful prerequisites, but
they establish an observed account and campaign baseline only. They do not
prove a writable campaign, a provider-side conditional write, a current native
approval installation, or that a pause has stopped delivery or billing. Meta
`PAUSE` therefore remains unavailable until the closed work below is reviewed
and evidenced.

## Current seams and gaps

| Area | Existing closed seam | Required Meta-specific promotion |
| --- | --- | --- |
| EDIT observation | `configured_meta_ads_token_edit_session()` in `adhelm.meta_ads_token_edit_session`, `MetaAdsTokenEditSession.read_campaign_state()`, and `MetaCampaignStateReader.read_campaign_state()` | Reuse only for fixed identity and campaign GETs. Keep its hard-coded `EDIT`/`ads_management` purpose, guarded subject-token callback, configuration/bundle/expiry check before every GET, and nonsecret `MetaAdsTokenEditBinding`. Do not expose its client, subject token, inspector token, or a generic request method. |
| Pure plan | `adhelm.providers.openai_pause_contract` and `adhelm.providers.ga4_create_contract` show the existing deterministic-action pattern | Add a separate pure Meta pause contract and record type. Do not overload the OpenAI `EXECUTION_PLAN` record, the GA4 CREATE contract, or `MetaAdsClient`, which is intentionally read-only. |
| Retention | `Ledger.append(..., _before_durable_append=...)`, `saved_plans.py`, `saved_ga4_create_plans.py`, and M39 `retention_recheck()` | Add one exact Meta retained-plan loader/retainer and a provenance/history validator. A recheck context must acquire configuration then credential state before the ledger append and its callback must not read the ledger, call the network, or acquire locks again. |
| Direct approval | `ApprovalChallengeKind`, `ApprovalChallenge`, `VerifiedApprovalReceipt`, `ApprovalReceiptClaimRecord`, and `_accept_exact_receipt()` | Add a dedicated Meta pause subject, challenge builder/parser/review projection, receipt kind, exact retained-source comparison, and claim-history binding. No generic “plan approval” branch. |
| Installed bridge | `_installed_native_approval_bridge._select_retained_direct_source()`, `_prepare_installed_native_approval_request()`, `_reload_and_compare_retained_source()`, and `_accept_native_approval_bridge_candidate()` | Current bridge protocol v1 permits only OpenAI pause and GA4 CREATE. Meta requires a separately versioned and cross-language reviewed promotion; it must retain the existing metadata/configuration binding, one-use request, checkpoint-prefix comparison, and post-consumption ambiguity behavior. |
| Provider egress | `SafetyKernel`, `GuardedAdapter`, and the fixed GET-only Meta reader | Add a distinct guarded Meta pause adapter with one fixed POST only after a durable kernel start and one-use permit. Do not add a generic Meta POST to `MetaAdsClient` or a generic HTTP/provider passthrough. |

Two current kernel behaviors need explicit repair for this operation:

1. `SafetyKernel._preflight_one()` currently requires `GuardedAdapter` only
   for OpenAI account-key actions and GA4 CREATE. It must also require a
   guarded adapter for the fixed `(provider="meta_ads", operation=PAUSE)`
   pair before a Meta pause can execute.
2. `SafetyKernel._apply_one()` has a GA4-specific rule that a non-confirmed
   CREATE response cannot be upgraded by a later matching read. The documented
   Meta contract has the same attribution rule. A timeout, malformed
   acknowledgement, or other post-egress uncertainty must remain
   `AMBIGUOUS_REMOTE_STATE` even if a later campaign GET reports `PAUSED`.
   The future change should add the exact Meta PAUSE case, not weaken this rule
   into a generic best-effort result policy.

## Recommended closed action representation

The smallest complete representation is a new pure module such as
`adhelm.providers.meta_campaign_pause_contract`, with a distinct retained record
literal such as `META_CAMPAIGN_PAUSE_PLAN`. The names are proposed interfaces,
not current public APIs.

### 1. Local execution binding

`MetaCampaignPauseExecutionBinding` should be a strict, serializable projection
of the detached M39 `MetaAdsTokenEditBinding`, not that session object itself.
It should have exact fields equivalent to:

```text
schema_version: 1
binding_kind: "META_ADS_EDIT_LOCAL_TOKEN_ASSOCIATION"
provider: "meta_ads"
purpose: "EDIT"
credential_source: "OS_META_TOKEN"
profile_ref
configuration_digest
profile_configuration_digest
credential_binding_ref
client_application_ref
principal_ref
scopes: ("ads_management",)
local_association_status: "LOCAL_SEALED_TOKEN_ASSOCIATION_ONLY"
provider_scope_status: "INSPECTED_ADS_MANAGEMENT_NOT_ACCOUNT_ACCESS"
provider_pause_permission: "NOT_PREPROVABLE"
```

All references and digests need the existing strict scalar validation and the
scope tuple must be exactly the one shown. The binding contains no token,
inspector credential, local continuity key, raw user identifier, or provider
write claim. It represents the exact local configuration/profile/credential
association that a later fixed session must re-establish; it does not use a
`StateRef` as a surrogate credential version.

This follows the GA4 CREATE pattern: its deterministic action contains a
separate execution binding for local OAuth/profile facts. The existing generic
`AccountIdentity` already has the right narrow provider-identity fields
(`account_ref`, stable ID, profile, principal, application, scopes). It should
remain unchanged. The Meta action should construct that existing identity for
the `SubPlan`, while the full configuration/profile/credential association stays
in `MetaCampaignPauseExecutionBinding` and is checked separately by the fixed
EDIT session.

### 2. Baseline and intended configuration target

`MetaCampaignPauseBaseline` should bind the complete normalized pre-write fact:

```text
schema_version: 1
account_ref                  # canonical meta_ads.account.act_<native-id>
account_client_ref           # native decimal account ID
account_currency
account_timezone
provider_api_version: "v26.0"
campaign_ref                 # canonical meta_ads.campaign.<native-id>
campaign_client_ref          # native decimal campaign ID
configured_status: "ACTIVE"
effective_status             # one observed M39 effective-status literal
```

The exact account/campaign pairing must be rebuilt from the fixed M39 identity
and `MetaCampaignState`, so a caller cannot pair a campaign with another
account. The source contract permits only `configured_status == "ACTIVE"`.
`effective_status` is retained as a baseline observation, never as a promised
postcondition.

Use a separate `MetaCampaignPauseConfigurationTarget` for the only verified
aftercondition:

```text
schema_version: 1
account_ref
account_client_ref
campaign_ref
campaign_client_ref
configured_status: "PAUSED"
```

The deterministic `SubPlan.before` fingerprint should cover the complete
baseline. Its `revision` must be `None`, because no reviewed provider revision,
ETag, or compare-and-swap token exists. The `SubPlan.after` fingerprint should
cover this configuration target only. It must not include an invented
`effective_status == "ACTIVE"`, or any other predicted effective status.

After a confirmed POST, a new `MetaCampaignPauseVerificationObservation` may
record the fresh observed `effective_status` alongside the proved configured
`PAUSED` account/campaign state. That observation is evidence about delivery at
its observation instant; it neither changes the plan's after fingerprint nor
proves zero spend, zero billing, or an instantaneous delivery stop.

### 3. Fixed egress binding

Only after the missing primary-source review establishes the exact Graph v26
write syntax should the action gain a closed request binding, for example
`MetaCampaignPauseRequestBinding`:

```text
schema_version: 1
host: "graph.facebook.com"
method: "POST"
api_version: "v26.0"
campaign_client_ref
configured_status: "PAUSED"
request_shape_digest
```

The current research supports the proposed node POST and status literal, but
does not independently establish the exact body encoding, acknowledgement
shape, error schema, or conditional/idempotency behavior. Those values must
remain absent from production code until evidence closes them. A baseline
digest is local drift detection only and must never be described as provider
CAS or idempotency.

`MetaCampaignPausePlannedAction` should contain the workspace binding, the
three bindings above, a deterministic `CompositePlan`, a false
`execution_authority` control, and explicit literals such as
`current_state_status="RECHECK_REQUIRED_BEFORE_EGRESS"` and
`residual_delivery_or_billing="NOT_PROVEN_STOPPED"`. Its validator must
rebuild every plan field, account identity, before/after fingerprint, egress
target, operation, and expiry from the closed action data. A copied or
constructed model must fail serialization/revalidation rather than carry a
changed credential/configuration digest or state target.

## Retention and approval promotion

The future record should mirror `Ga4KeyEventCreatePlanRecord` rather than the
OpenAI-only `EXECUTION_PLAN` envelope:

1. Add `MetaCampaignPausePlanRecord` and
   `revalidate_meta_campaign_pause_plan_record()` in a dedicated plan-record
   module. Its envelope must bind the action digest, plan digest, expiry,
   provider `meta_ads`, maximum incremental spend, false authority, and
   current-kernel-check requirement exactly.
2. Add `retain_meta_campaign_pause_plan()` and
   `load_meta_campaign_pause_plan()` with an exact verified-checkpoint scan.
   The loader must return the canonical action and only safe provenance needed
   by the private coordinator. It must not return a token, a session, a
   transport, or a reusable configuration source.
3. Add the record to `Ledger._KINDS`, its exact provenance/envelope
   validation, and any changes-stream history validation. Retention must use
   the M39 `retention_recheck()` context around one append, with the callback
   passed as `Ledger.append(..., _before_durable_append=recheck)`. This gives
   the required lock order of configuration, credential, then changes stream;
   the callback itself must only revalidate already-held local facts.

The direct-approval family then needs a closed third branch:

- `ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN`;
- `MetaCampaignPauseApprovalSubject` and
  `build_meta_campaign_pause_challenge()` in `approval_protocol.py`;
- exact parser, receipt, and deterministic review projection branches in
  `decode_approval_challenge()`, `approval_review_facts()`, and receipt shape
  validation;
- `"META_CAMPAIGN_PAUSE_PLAN"` in `ApprovalKind` and exact source/action
  consistency in `ApprovalReceiptClaimRecord.exact_receipt_consistency()`;
- a matching `_accept_exact_receipt()` branch that loads only this retained
  record type, compares canonical action bytes, then creates a `DirectGrant`
  with exactly its plan and subplan digests; and
- a matching `Ledger._validate_approval_receipt_history()` branch, so the
  changes stream atomically rejects receipt, nonce, or receipt-ID replay and
  refuses a claim whose earlier Meta action differs byte-for-byte.

The native review projection should contain bounded safe references/digests,
the approved `ACTIVE` to `PAUSED` configured transition, the baseline effective
status labeled as observed, and the fixed residual-delivery caveat. It should
omit subject/inspector tokens, local binding keys, raw receipt material, raw
provider response text, and any claim that `ads_management` proves account
write access.

## Installed-bridge consequence

The M32/M33 bridge contract intentionally accepts only
`OPENAI_PAUSE_PLAN` and `GA4_KEY_EVENT_CREATE_PLAN`. The new Meta kind must not
be slipped into the existing v1 wire without corresponding Swift/native review
and vector changes. A separate protocol-versioning decision is required. The
safe default is to preserve the current v1 parser and fixtures unchanged and
introduce a new reviewed closed version for Meta only after the provider action
contract is fixed.

Whichever versioned promotion is approved must extend all of these exact seams:

- `_installed_native_approval_bridge._selected_source_record()`,
  `_retained_source_from_record()`, `_revalidate_pending_source()`,
  `_revalidate_pending_challenge()`, and
  `_prepare_installed_native_approval_request()` for the one named Meta record
  and action type;
- `NativeApprovalBridgeRequestV1`'s successor and native equivalent to bind the
  exact canonical challenge, metadata digest, seven-field protocol digest,
  request ID, and source-context digest;
- the native review/receipt path to consume the nonce before producing the only
  success candidate; and
- `_reload_and_compare_retained_source()`'s existing all-stream append-only
  prefix proof before the receipt claim.

The coordinator must preserve its current division of work: source reload and
any provider/configuration check occur outside the final changes-stream lock;
the final `_before_durable_append` receipt callback only revalidates held
candidate, request, configuration, and clock facts. It must never call the
ledger, a Meta session, the network, or the pipe transport while that lock is
held.

The current `_exchange_and_record_pending_installed_native_approval()` combines
candidate return and receipt claim. That does not itself violate the documented
order: receipt consumption may precede a later kernel drift refusal, and a
durable direct grant alone permits no provider egress. The required Meta
boundary is a fresh configuration, credential, account, and campaign check
after authorization but before `APPLY_STARTED` and POST egress. It must reject
drift before either boundary, without holding its local locks across native
review or HTTP.

A later implementation may choose a narrow internal post-candidate/pre-claim
readiness hook to reject drift even earlier. That is an optional design
alternative, not a required bridge change. If used, it must run outside the
final ledger lock and must not become a caller-supplied generic callback or an
I/O callback under `_before_durable_append`.

## Guarded adapter and kernel integration

Create a distinct private adapter such as
`MetaCampaignPauseGuardedAdapter`. It should implement `GuardedAdapter`, use the
existing M39 EDIT session only internally, and expose no generic Meta writer.
Its fixed behavior should be:

1. Rebuild and validate the exact Meta pause action and current EDIT session
   binding before every identity/state read. `read_identity()` must produce the
   existing `RemoteIdentity` only after the M39 session proves the action's
   account/profile/principal/application/scope facts.
2. `read_state()` must issue the one allowlisted campaign GET and derive either
   the complete `before` fingerprint for the approved `ACTIVE` baseline or the
   configuration-only `after` fingerprint for the exact `PAUSED` target. It
   must reject another account/campaign or any unplanned configured state.
3. `apply_guarded()` must re-read the current baseline immediately before the
   fixed POST, reject drift, recheck the local subject-token continuity and
   expiry, validate the kernel permit, then consume that one permit immediately
   before egress. It must accept only the reviewed success acknowledgement once
   its schema is evidenced.
4. Any timeout, malformed acknowledgement, transport failure, or uncertainty
   after attempted POST must return `ApplyOutcome.AMBIGUOUS`, never
   `no_effect_proven=True`. Only a conclusively pre-egress local refusal may
   yield `FAILED_NO_EFFECT` later, with bounded local evidence.
5. `verify()` must make a fresh fixed campaign GET after a confirmed response.
   It proves only account/campaign plus configured `PAUSED`; it emits an opaque
   evidence reference that records the observed effective status separately.
   It does not predict or require a particular effective status.
6. `reconcile()` must never upgrade an ambiguous Meta POST merely because a
   later GET reports `PAUSED`. Without attributed provider idempotency or CAS
   proof, that result stays `AMBIGUOUS_REMOTE_STATE` and requires a new plan and
   approval for any further mutation.

`SafetyKernel._apply_ordered()` already fsyncs `APPLY_STARTED` before it creates
the permit and calls `apply_guarded()`. The Meta adapter must rely on that
ordering, not create a second mutable authorization mechanism. The future
kernel change must additionally provide a narrow private *per-append recheck
context* for this adapter/workflow: it enters M39 configuration then credential
locks before the individual durable append, passes the session's zero-I/O
callback to `Ledger.append`, and exits before any HTTP. It must not wrap the
whole `SafetyKernel.execute()` call, because that would hold local credential
locks across native review or provider egress.

The first required use is the Meta `APPLY_STARTED` append. Result-history
append behavior after egress needs an explicit design before coding: failure to
recheck local credentials after an attempted POST cannot silently suppress the
durable result record. The implementation must retain an honest ambiguous
result/recovery path rather than turning a post-egress local recheck failure
into a retryable no-write result.

## Required test matrix

The following tests are required in addition to existing M39 fake-only GET and
session coverage. They should use in-memory stores and mocked HTTP/native
boundaries only until the provider evidence below is closed.

| Layer | Required assertions |
| --- | --- |
| Pure action | Exact scalar types reject bool/float aliases; all nested binding digests, account/campaign refs, app/principal/scope, status literals, expiry, egress host, and plan digest are deterministically rebuilt; constructed/model-copied actions cannot serialize changed values. `ACTIVE` is the sole plannable configured baseline. |
| Effective-status semantics | Baseline includes its observed effective status; the `after` fingerprint does not. A fresh post-write observation with `ACTIVE`, `IN_PROCESS`, `PAUSED`, or another allowed effective status can still prove configured `PAUSED` without a zero-spend claim. An unexpected configured status fails. |
| M39 composition | EDIT-only profile and `ads_management` are required before inspection; every GET rechecks full configuration, bundle marker, and both expiries; a changed profile/configuration/bundle/clock fails before its GET. No inspector token or subject token reaches a plan, record, adapter result, exception, or output. |
| Retention | Plan retention uses configuration then credential then ledger locking; its callback has no network/ledger/lock reentry; a changed configuration/bundle before the append prevents a plan record. Loader accepts exactly one verified Meta record and rejects stale/tampered canonical action data. |
| Approval/receipt | Challenge, review facts, receipt, claim record, and direct grant bind the exact Meta action/plan/subplans. Wrong record kind, altered action, nonce/receipt/ID replay, wrong full metadata digest, wrong protocol digest, or a receipt for a different action fails before a grant. The old bridge v1 rejects the Meta kind until its separately reviewed successor exists. |
| Bridge lifecycle | Retained source selection captures all stream prefixes; unrelated suffixes are permitted, but any historical head replacement, selected-record change, or action change fails after a candidate as ambiguous/no-retry. No raw candidate, receipt, key, pin, or transport becomes public. |
| Post-authorization drift | A fresh Meta configuration/credential/account/campaign check occurs after the direct grant and before `APPLY_STARTED`/POST. A mismatch prevents egress even though the receipt may already be durably consumed. |
| Kernel/adapter | Meta PAUSE requires `GuardedAdapter`; `APPLY_STARTED` is durable before POST; a permit is required and one-use; pre-POST drift is rejected; no config/credential lock spans HTTP. A non-confirmed/uncertain POST followed by matching `PAUSED` remains ambiguous. Only an exact confirmed acknowledgement plus fresh configured-PAUSED read and redacted evidence can become `APPLIED_VERIFIED`. |
| Workflow | The private entry accepts only a retained record digest and closed execution request. It has no receipt, key, pin, grant, adapter, transport, token, URL, or method input. The ordinary unconfigured compiled binding fails before session/provider work; no blind retry occurs after bridge consumption, claim uncertainty, or ambiguous provider state. |

## Evidence still required before implementation can claim capability

The documented research deliberately leaves four provider facts open:

1. The exact Graph v26 campaign-node POST path, request encoding, bounded
   success acknowledgement, and error schema.
2. The exact customer-owned app-review, account-role, and campaign access
   requirements beyond the inspected `ads_management` label.
3. Whether the operation has an official conditional-update, revision, ETag,
   or idempotency mechanism. Until then the implementation must preserve the
   residual race and ambiguity behavior above.
4. The timing and meaning of `effective_status` following a configured pause.

Source-only/fake tests also cannot establish an installed signed native helper,
protected-store nonce consumption, real user review/authentication, a writable
customer account, or a verified production pause. Those remain separate
installation and live-evidence gates. This review recommends no change to the
current capability status, no automatic command exposure, and no expansion of
the existing native bridge v1 wire.

# Google Ads Search maintenance and replacement (P05)

**Status: `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.** No live Google Ads
request has been made from this route, **nothing has ever been archived against
a real account, and no spend has ever been incurred.** Every claim below is
either a fixture result from `tests/test_google_ads_maintain_execution.py` or a
wire fact whose per-claim provenance is recorded in [wire-fact
provenance](#wire-fact-provenance).

**In this build an archive request is refused as
`EXECUTION_PLAN_NOT_COMPILABLE`.** An archive sets `CampaignStatus.REMOVED`, so
it is delete-class and AGENTS rule 11 requires a `PROVEN_UNREFERENCED`
dependency proof before it can be planned (see
[an archive is delete-class](#an-archive-is-delete-class-rule-11-needs-a-dependency-proof)).
No read this build can cite reaches any campaign reference surface, so no proof
can be produced. **The `("google_ads", ARCHIVE)` pair is therefore not
registered in a production build**: `discover` does not list it, and a request
for it is refused by the registry as `EXECUTION_PLAN_NOT_COMPILABLE` (exit 3)
after the local configuration and output-bound check but before any planner,
credential or provider request, with nothing retained and neither the campaign
nor the customer named. An archive intent sent under `--operation UPDATE` is
refused as `INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION`. The archive
operation is not an available capability here.
`GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN` is the planner's own rule 11 gate: it
fires only once the pair is registered, which today happens only in the
fake-read tests. The archive wire body, review, execution and readback below are
exercised only with a fake stand-in for the future read, and they are the part a
later ticket makes reachable. The budget, bid and schedule kinds are
unaffected.

P05 adds the guarded maintenance and replacement operations
[`definition-of-done.md`](../definition-of-done.md) §1 L81 requires beside
pause/archive. One family covers four operations on an existing Search
hierarchy, **each compiled as its own single-request executable plan** on the
generic route:

| `maintenance_kind` | operation | `expected_child_egress_count` | spend bound |
| --- | --- | --- | --- |
| `CAMPAIGN_BUDGET_AMOUNT` | `UPDATE` | 1 | the **new** campaign-lifetime total |
| `AD_GROUP_CPC_BID` | `UPDATE` | 1 | the campaign budget total |
| `AD_SCHEDULE_REPLACEMENT` | `UPDATE` | 1 | the campaign budget total |
| `CAMPAIGN_ARCHIVE` | `ARCHIVE` | 1 | `NOT_SPEND_CAPABLE` (explicit zero cap) |

The planner and adapter factory can serve **two** pairs — `("google_ads",
UPDATE)` and `("google_ads", ARCHIVE)` — because the four operations compile to
those two operation kinds, but a production build **registers only
`("google_ads", UPDATE)`** on the S3/S4 registries introduced by
[`execution-plan-records.md`](execution-plan-records.md) and
[`generic-execution-route.md`](generic-execution-route.md). `discover` renders
those registries, and its `compilable_operations` list must never advertise an
operation no request can compile; no archive can be planned in this build, so
the `ARCHIVE` pair is registered only by the fake-read tests (through
`monkeypatch`) and by the ticket that adds a cited reference read. It adds no
CLI leaf: `plan execution create --provider google_ads --operation UPDATE` and
`execute plan <record-digest>` are the whole surface. The operation kind is
derived from the request's own intent and must match the operation the command
names, so a caller cannot reach the archive path through an `UPDATE` request.

## Why a bid change is not `NOT_SPEND_CAPABLE`

The S1 rule in `execution_plan_records.NON_SPEND_OPERATIONS` treats `PAUSE`,
`ARCHIVE`, and `RECOVER` with an explicit zero cap as `NOT_SPEND_CAPABLE`.
`UPDATE` is not in that set, and this family does not try to put it there.

Raising a max CPC changes what the account can spend per click. Replacing an ad
schedule changes when the same budget is allowed to be spent. Neither is
spend-neutral, so both must state a bound. The honest,
**provider-enforceable** one is the campaign budget that caps the whole flight:
`authorization-policy.md` requires bounds from "verified provider-native
controls" with their semantics disclosed, and there is no smaller enforceable
ceiling on a bid change than the budget itself. A budget change states the new
amount, because that *is* the new ceiling.

An archive is the only operation here that removes delivery capability rather
than changing it, so it is the only one that summarizes as `NOT_SPEND_CAPABLE`
— and the only one whose review lines say the word irreversible.

## The request envelope

```json
{
  "schema_version": 1,
  "profile_id": "google-ads-edit",
  "provider": "google_ads",
  "operation": "UPDATE",
  "account_ref": "google_ads.customer.1234567890",
  "resource_ref": "google_ads.campaign_name.<sha256 of the campaign name>",
  "maintain_selection": {
    "selection_kind": "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN",
    "campaign_name": "Customer Search campaign",
    "intent": {
      "maintenance_kind": "CAMPAIGN_BUDGET_AMOUNT",
      "new_total_amount_micros": 500000000
    },
    "expires_in_seconds": 300
  }
}
```

`resource_ref` is a **keyless digest** of the campaign name under a domain
separator of its own, not the name itself. The generic envelope's `resource_ref`
is a `SafeRef`, and a customer campaign name is neither bounded to that
character set nor something this build puts in an uninterpreted reference slot.
The exact name travels in the selection, and the planner refuses unless
`resource_ref == campaign_name_ref(selection.campaign_name)`.

`ExecutionPlanRequest` keeps `extra=forbid` and additionally refuses a request
that names more than one provider selection: one plan is one operation.

### The archive intent has to say so

```json
{
  "maintenance_kind": "CAMPAIGN_ARCHIVE",
  "disposition": "IRREVERSIBLE_ARCHIVE",
  "acknowledged_irreversible": true
}
```

Both fields are exact literals. This build refuses to compile a `REMOVED`
transition unless the request itself names the outcome as irreversible, so an
archive can never be reached by a caller that thought it was pausing.
`test_an_archive_intent_requires_an_explicit_irreversibility_acknowledgement`
pins both refusals. The acknowledgement is necessary and is **not** the rule 11
gate: see the next section.

### An archive is delete-class: rule 11 needs a dependency proof

`CampaignStatus.REMOVED` is Google's removal state, and the build treats it as
exactly that: the intent demands `IRREVERSIBLE_ARCHIVE`, the review line says it
cannot be undone, and the plan's `after` is the empty member multiset. AGENTS
rule 11 governs the effect, whatever the wire shape: "A resource may be deleted
only when the dependency engine proves it is `PROVEN_UNREFERENCED`. Age, naming,
**paused status**, or apparent inactivity is never enough." Paused-first,
`IRREVERSIBLE_ARCHIVE` and the acknowledgement stay required and none of them is
the proof.

The proof is `GoogleAdsCampaignDependencyProof`, carried in
`GoogleAdsMaintainPreconditions.dependency_proof`. It is `None` for the other
three kinds, and an archive whose preconditions carry `None` is not a plan. The
model can hold nothing but a proof:

* the list `CAMPAIGN_REFERENCE_SURFACES` (experiments and drafts, shared set
  links, labels, conversion goal links, portfolio bidding strategy references)
  appears exactly once and in order, so a surface on the list cannot be silently
  omitted from an existing proof. **The enumeration itself is provisional and
  uncited**: no page in `docs/research/` cites a v25 resource for any of these
  five (wire fact 17), and others, such as campaign assets and asset sets,
  customizers and extension settings, are not on it with no argument that they
  are absent. Passing the list is not evidence for the `measurement-control.md`
  "every applicable resource surface" requirement;
* every surface must be `READ_EMPTY`. A `READ_NONEMPTY` surface is a live
  reference and a `NOT_READ` surface is an unproven one;
  [`measurement-control.md`](../measurement-control.md) requires an unreadable
  surface to block the proof, not to read as empty, and a `NOT_READ` reading may
  not carry a row count at all;
* it restates nothing its container already holds. The campaign name,
  hierarchy projection, local key marker and read interval belong to the
  `GoogleAdsMaintainObservationBinding` that carries the proof and are digested
  with it; the proof records **no read interval of its own**, because this build
  performs no reference read to measure one and copying the hierarchy read's
  would fabricate it. A later cited read adds the fields it measures;
* nothing a request carries reaches it. The only producer is
  `campaign_dependency_proof` in `google_ads_maintain_planning_session`.

The campaign's own ad groups, ads, keywords and campaign criteria are
deliberately **not** reference surfaces. The plan's `before` projection
enumerates them and its `after` is empty, so they are the reviewed scope of the
removal; counting them as references would make a proof impossible beside every
hierarchy this route can observe.

**What this build can prove: nothing.** `docs/research/` cites no v25 resource
for any of the five surfaces, and naming a GAQL resource here without a source
would be invention. `campaign_reference_surfaces` therefore reports each surface
`NOT_READ` with basis `NO_CITABLE_V25_GAQL_RESOURCE_IN_THIS_BUILD`. Once the
`ARCHIVE` pair is registered, the planner refuses an archive with
`GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN` naming every unproven surface, before
a plan or any record is retained; in this build the pair is unregistered, so that
gate is never reached by a real request (the request is refused earlier as
`EXECUTION_PLAN_NOT_COMPILABLE`) and fires only in the fake-read tests. That
function is the one seam a later, cited read replaces.

The proof is re-derived, not trusted, at execution, but the gate sits on the
archive's *precondition* and never on *observing* the campaign. The adapter's
`read_state` is also the kernel's observation read for attempt recovery and
reconciliation, which must report state rather than enforce a precondition
(AGENTS rules 6 and 22). So `read_state` re-checks the reference surfaces on
every fresh read and, when any is no longer `READ_EMPTY`, **returns a distinct
UNPROVEN state** (the semantic fingerprint re-keyed with an explicit marker,
under its own revision) instead of raising. The kernel's pre-start comparison
then refuses it as `REMOTE_DRIFT` before the durable start and before any egress,
the guarded adapter's context manager adds the named surfaces to that refusal's
`next_action`, and the adapter exposes them as `unproven_reference_surfaces`. A
recovery read of an interrupted archive attempt records the same honest
observation, so a surface that stops reading empty can never keep the write
barrier closed. The review states
`dependency proof: PROVEN_UNREFERENCED` for an archive and
`NOT_APPLICABLE_NOT_DELETE_CLASS` for the other kinds, and the review model
refuses the opposite.

## Semantic before/after

`before` and `after` reuse the P05 live-hierarchy projection described in
[the activation contract](google-ads-activate-execution.md#semantic-beforeafter-without-native-identifiers),
under the same keyed namespace (`GOOGLE_ADS_SEARCH_CAMPAIGN_LIVE_HIERARCHY_V1`)
and the same `(customer id, HMAC of the campaign name)` target binding. That is
deliberate reuse rather than a second hasher: two hand-written projections of
one hierarchy would be two chances to disagree about what "the same hierarchy"
means.

What each kind changes in the projection:

* `CAMPAIGN_BUDGET_AMOUNT` — the budget member's `total_amount_micros`;
* `AD_GROUP_CPC_BID` — the ad-group member's `cpc_bid_micros`. `cpc_bid_micros`
  is `optional int64` and Google omits it when unset, so an absent bid projects
  as the explicit string `NOT_SET` rather than a guessed zero;
* `AD_SCHEDULE_REPLACEMENT` — every `CAMPAIGN_CRITERION_AD_SCHEDULE` member is
  dropped and the replacements added, each carrying the **campaign's own**
  status (so a paused campaign gets paused criteria and an enabled one gets
  enabled criteria);
* `CAMPAIGN_ARCHIVE` — `after` is the **empty member multiset** under the same
  target binding: the local statement "no live hierarchy with this name in this
  customer". There is no `predicted_hierarchy_digest` and no `member_count`.

A plan whose `after` equals its `before` is refused: a no-operation is not a
plan, and the planner additionally refuses each kind's own no-op with
`GOOGLE_ADS_MAINTAIN_NO_CHANGE` before the plan is built.

The projection is keyed with the workspace's `export_key()`; the adapter refuses
with `GOOGLE_ADS_MAINTAIN_FINGERPRINT_KEY_CHANGED` before its first provider
read if the current key no longer reproduces the retained states.

## The plan

| Plan fact | Value |
| --- | --- |
| `provider` | `google_ads` |
| `operation` | `UPDATE` for the first three kinds, `ARCHIVE` for the archive |
| `expected_child_egress_count` | `1` — one atomic mutate per plan, so the factory declares `child_egress=False` |
| `max_spend` | per the table at the top of this document |
| `review_lines` | derived from this contract's own closed review model via `approval_protocol.compiler_review_lines`, so `review_source` is `COMPILER_CANDIDATE_REVIEW` |
| `recovery_actions` | `(UPDATE,)`, or `(ARCHIVE,)` for the archive |
| `recovery_semantics` | `RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT` |
| `field_changes` | one change on `campaign_budget/total_amount_micros`, `ad_group/cpc_bid_micros`, `campaign_criterion/ad_schedule`, or `campaign/status` |
| `capability_status` | `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`, `live_validated=false` |

`SubPlan` requires at least one recovery operation and refuses `RECOVER`, so an
archive names `ARCHIVE` — the operation kind whose attempt a recovery would
reconcile — rather than implying a compensating write that does not exist.

An archive's rendered review, with the derived `NOT_SPEND_CAPABLE` line leading:

```text
Maximum incremental spend NOT_SPEND_CAPABLE
Provider google_ads compiled candidate review
atomicity: ONE_MUTATE_PARTIAL_FAILURE_DISABLED
campaign status after: REMOVED
campaign status before: PAUSED
dependency proof: PROVEN_UNREFERENCED
live validated: false
maintenance effect: CAMPAIGN_STATUS_SET_TO_REMOVED_IRREVERSIBLE
maintenance kind: CAMPAIGN_ARCHIVE
new value: REMOVED
previous value: PAUSED
recovery: RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT
reversibility: IRREVERSIBLE_ARCHIVE
```

No plan can be retained to render this review until a cited read of the
reference surfaces exists.

## The four mutation bodies

Each is one `POST customers/{cid}/googleAds:mutate` with
`partialFailure=false`, `validateOnly=false`, and
`responseContentType=RESOURCE_NAME_ONLY`. Each is a pure function of the
observed hierarchy, the intent, and the customer ID, pinned as golden JSON:

| Kind | Operations | Fixture |
| --- | --- | --- |
| budget | one `campaignBudgetOperation.update`, `updateMask=totalAmountMicros` | [`...maintain_budget_mutate_body.json`](../../tests/fixtures/google_ads_search_campaign_maintain_budget_mutate_body.json) |
| bid | one `adGroupOperation.update`, `updateMask=cpcBidMicros` | [`...maintain_bid_mutate_body.json`](../../tests/fixtures/google_ads_search_campaign_maintain_bid_mutate_body.json) |
| schedule | one `campaignCriterionOperation.remove` per existing schedule criterion, then one `campaignCriterionOperation.create` per replacement | [`...maintain_schedule_mutate_body.json`](../../tests/fixtures/google_ads_search_campaign_maintain_schedule_mutate_body.json) |
| archive | one `campaignOperation.update`, `updateMask=status` to `REMOVED` | [`...maintain_archive_mutate_body.json`](../../tests/fixtures/google_ads_search_campaign_maintain_archive_mutate_body.json) |

Native resource names come from the **fresh pre-egress read**, never from the
retained plan; the plan carries no provider identifier at all. The two are tied
together by the `before` fingerprint.

### Ad schedules are immutable, so a replacement is remove-then-create

`CampaignCriterion.ad_schedule` is `Immutable` in v25, and so are `campaign` and
`negative`; `criterion_id` is `Output only` and "ignored during mutate". A
schedule is therefore never edited. The route removes the exact existing
schedule criteria and creates the replacements **in the same atomic mutate**, so
the campaign is never observably left with no schedule at all
(`partialFailure=false` makes the request a single atomic failure).

**No temporary resource ID is used, and none is needed.** Google's temporary
negative IDs exist so a later operation can reference a resource an earlier
operation created; here no created criterion is referenced by any later
operation in the same request. `test_the_schedule_body_removes_then_creates_in_one_atomic_mutate`
asserts that no `-1` appears anywhere in the body. That is a deliberate
divergence from the ticket's expectation of temporary IDs, and it is the
narrower request: adding an unused temporary ID would be a wire fact this build
cannot justify.

Location and language criteria are **not** touched. Only
`CAMPAIGN_CRITERION_AD_SCHEDULE` members are removed, and the golden body names
each removal by its exact resource path.

### An archive is a status update, not a `remove` operation

`CampaignOperation` has both an `update` and a `remove` member. This route sends
`update` with `updateMask=status` and `status=REMOVED`, which is the transition
`CampaignStatus.REMOVED` documents ("Campaign has been removed"). Using the one
shape for all four kinds keeps the acknowledgement parser and the evidence
digest identical across the family. It does **not** take the route out of
rule 11: `update` with `status=REMOVED` and `remove` both produce
`CampaignStatus.REMOVED`, and the v25 enum comment promises no reversibility
either way. Rule 11 is about the effect, so the archive is gated by the
[dependency proof](#an-archive-is-delete-class-rule-11-needs-a-dependency-proof)
whichever operation carries it.

### The closed acknowledgement subset

The only accepted local acknowledgement is an exact HTTP 200 JSON object whose
**sole** member is `mutateOperationResponses`, holding exactly one result per
planned operation, in order, each carrying exactly the expected result field
(`campaignBudgetResult`, `adGroupResult`, `campaignCriterionResult`, or
`campaignResult`) and a `resourceName` under this customer's path. Everything
else — a partial-failure envelope, an extra member, a missing or extra result,
an out-of-order result field, a foreign resource name, a non-200 status — is
refused, and because the permit is already consumed it is reported as
`AMBIGUOUS_REMOTE_STATE` and must not be retried.

## Verification, reconciliation, and recovery

Verification compares fingerprints, per kind:

* budget, bid, schedule — a fresh whole-hierarchy read is projected and must
  equal the approved `after` exactly;
* archive — the campaign's own status is read. `REMOVED` **or absent** is
  accepted, because GAQL's implicit filtering of removed resources is
  undocumented on any page this build could reach; anything else fails
  verification. `read_state` reports the archived `after` state in that case, so
  the kernel's post-apply check sees the postcondition rather than a hierarchy
  read that no longer describes a live campaign.

**Recovery of an interrupted archive.** Attempt recovery re-reads the campaign
through the adapter's `read_state` and records the same honest observation as for
the other kinds. The outcome depends on what that read finds:

* campaign `REMOVED` or absent: `read_state` returns the archived `after` state
  without reading any reference surface, so recovery records `AT_TARGET`
  (disposition `RECOVERED_AT_TARGET`);
* campaign unchanged and every reference surface `READ_EMPTY`: the observation
  is the proof-bearing baseline, so recovery records `AT_BASELINE` (disposition
  `RECOVERED_AT_BASELINE`);
* anything else: recovery records `observed_relation` `OTHER` and disposition
  `RECOVERED_DIVERGED`.

`OTHER` covers two different situations that the record does not tell apart. In
one, the campaign itself is unchanged but some surface is no longer
`READ_EMPTY`, so the proven-unreferenced precondition, which is part of the
baseline, no longer holds. In the other, the campaign or its hierarchy drifted
from the approved baseline (for example the campaign was re-enabled or its
budget was edited, or an ad group or other member changed). `RECOVERED_DIVERGED`
alone therefore does not show that the campaign was modified, and it does not
show that it was not. The recovery record deliberately does not name the
surfaces; they can be seen through the adapter's `unproven_reference_surfaces`
(which the `REMOTE_DRIFT` wrapper uses for its `next_action`) or by a fresh
archive plan attempt once the `ARCHIVE` pair is registered, which refuses with
`GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN` naming them. Pinned by
`test_recovery_records_an_honest_observation_when_a_surface_stops_reading_empty`.

`reconcile` returns `EffectStatus.AMBIGUOUS_REMOTE_STATE` with `settled=False`,
**always**. A later matching read cannot attribute a missing or uncertain
acknowledgement to this request, and there is no reviewed conditional-write or
idempotency proof for this offline SDK-derived route.

**Recovery is the recorded previous value.** Nothing here reverts
automatically. The readback retains `previous_value` and `new_value`, and a
correction is a *new* plan with a *new* authorization that names the previous
value. For an archive there is no correction: `reversibility` is
`IRREVERSIBLE_ARCHIVE` and the record says so.

## The readback record

`GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_READBACK_OBSERVATION` retains one bounded
post-acknowledgement observation carrying the maintenance kind, the operation,
the observation interval, `previous_value` and `new_value`, what the readback
observed (`LIVE_HIERARCHY_MATCHES_PREDICTED_SEMANTICS` or
`CAMPAIGN_STATUS_REMOVED`), the observed member count for a non-archive, the
observed customer status, the authorized per-currency ceiling restated from the
plan, the reversibility, `automatic_revert: false`, and the ordered
acknowledgement result kinds.

`previous_value == new_value` is refused: a maintenance operation must change
the value it addresses. `campaign_resource_ref` is a keyless SHA-256 reference
over the exact resource path under a domain separator of its own. No request
body, response body, bearer, or provider diagnostic is
retained.

`validate_google_ads_maintain_readback_history` runs on append **and on scan**,
and requires every readback to name one retained maintain action, restate that
action's plan and subplan digests, maintenance kind, operation, member count,
reversibility, authorized bound and customer status, and follow exactly one
durable `APPLY_STARTED` for the same attempt.

## Preconditions refused before egress

| Check | Refusal |
| --- | --- |
| The request's operation is not the one its intent compiles to | `INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION` |
| `resource_ref` is not the selection's own campaign-name reference | `INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION` |
| Configuration changed since the receipt was reserved | `CONFIGURATION_CHANGED` |
| Unresolved `APPLY_STARTED` or run in local history | `RECOVERY_REQUIRED` |
| Either additive ledger record kind is not installed | `GOOGLE_ADS_MAINTAIN_RECORD_KIND_NOT_INSTALLED` |
| Customer is not `ENABLED` | `GOOGLE_ADS_ACCOUNT_NOT_ENABLED` |
| Currency or timezone differs from the configured EDIT profile | `GOOGLE_ADS_MAINTAIN_FACTS_INVALID` |
| Any member is `REMOVED` | `GOOGLE_ADS_MAINTAIN_MEMBER_REMOVED` |
| A member status is neither `PAUSED` nor `ENABLED` | `GOOGLE_ADS_MAINTAIN_HIERARCHY_STATUS_UNSUPPORTED` |
| The campaign is not a Search campaign | `GOOGLE_ADS_MAINTAIN_CAMPAIGN_TYPE_UNSUPPORTED` |
| **Archive against an `ENABLED` campaign** | `GOOGLE_ADS_ARCHIVE_REQUIRES_PAUSED_CAMPAIGN` |
| **Archive request in a production build** (the `ARCHIVE` pair is unregistered) | `EXECUTION_PLAN_NOT_COMPILABLE` (exit 3), after the local configuration and output-bound check but before any planner, credential or provider request (so a configuration error or `OUTPUT_LIMIT_EXCEEDED` can precede it); nothing retained, campaign and customer not named. Pinned by `test_a_production_archive_request_is_refused_as_not_compilable_before_any_credential` and the `google_ads_maintain_execution_refusal_smoke` installed-CLI check |
| **Archive without a `PROVEN_UNREFERENCED` proof**, once the `ARCHIVE` pair is registered (today only in the fake-read tests) | `GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN`, the planner's rule 11 gate, naming each surface that is `NOT_READ` or `READ_NONEMPTY` |
| The requested budget, bid, or schedule set is the one already configured | `GOOGLE_ADS_MAINTAIN_NO_CHANGE` |
| A schedule replacement with no existing schedule criterion to replace | `GOOGLE_ADS_MAINTAIN_NO_AD_SCHEDULE_TO_REPLACE` |
| At execution: the current EDIT association differs from the plan's | `GOOGLE_ADS_MAINTAIN_EXECUTION_BINDING_DRIFT` |
| At execution: the local fingerprint key no longer reproduces the states | `GOOGLE_ADS_MAINTAIN_FINGERPRINT_KEY_CHANGED` |
| At execution: any hierarchy difference from the approved `before` | `REMOTE_DRIFT` |
| At execution: an archive whose campaign is now `ENABLED` | `REMOTE_DRIFT`, from the kernel's pre-start read (campaign status is part of the semantic fingerprint). The `apply_guarded` `GOOGLE_ADS_ARCHIVE_REQUIRES_PAUSED_CAMPAIGN` check is defense in depth behind it |
| At execution: an archive whose reference surfaces are no longer proven empty | `REMOTE_DRIFT` from the kernel's pre-start comparison of the UNPROVEN state, before the durable start; the refusal's `next_action` names the surfaces. Recovery of an interrupted archive still records its observation, but as `OTHER` / `RECOVERED_DIVERGED` rather than `AT_BASELINE` / `RECOVERED_AT_BASELINE`, and does not name the surfaces (see [recovery](#verification-reconciliation-and-recovery)) |
| At execution: the readback record kind is not installed | `GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND_NOT_INSTALLED` |
| At execution: the plan expired | `PLAN_EXPIRED` |

The paused-first rule and the dependency proof for an archive are each checked
**twice**: once at planning against the bounded read, and once at execution
against the fresh pre-egress read (the second as drift, as above). Stopping
delivery is its own operation with its own authorization, and it is never folded
into the same unreviewed step as removing the campaign.

## Wire-fact provenance

Google's own reference pages render navigation-only or `404` through this
fetcher, so where a claim could be read from the official v25 protobuf
definitions in `googleapis/googleapis` it is marked **CITED**; where it could
not, the exact unreachable source is named and the claim is `UNVERIFIED`.

| # | Wire fact this route relies on | Status | Source |
| --- | --- | --- | --- |
| 1 | The request/response messages and the `MutateOperation` / `MutateOperationResponse` oneof members used here | **CITED** | [v25 `google_ads_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/google_ads_service.proto), fetched 2026-09-09 |
| 2 | `CampaignOperation` has `google.protobuf.FieldMask update_mask = 4` and a `create/update/remove` oneof; `update_mask` "determines which resource fields are modified in an update" | **CITED** | [v25 `campaign_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/campaign_service.proto), fetched 2026-09-09 |
| 3 | `CampaignCriterionOperation` has the same shape, and its `remove` takes `customers/{customer_id}/campaignCriteria/{campaign_id}~{criterion_id}` | **CITED** | [v25 `campaign_criterion_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/campaign_criterion_service.proto), fetched 2026-09-09 |
| 4 | `CampaignCriterion.ad_schedule` is **Immutable** ("Immutable. Ad Schedule."), as are `campaign` and `negative`; `criterion_id` is "Output only ... ignored during mutate" | **CITED** | [v25 `campaign_criterion.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_criterion.proto), fetched 2026-09-09. This is the whole reason a schedule change is remove-then-create |
| 5 | `AdGroup.cpc_bid_micros` is "The maximum CPC (cost-per-click) bid. This field is used when the ad group's effective bidding strategy is Manual CPC", and is `optional int64` | **CITED** | [v25 `ad_group.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/ad_group.proto), fetched 2026-09-09. The "used when Manual CPC" scope is why the bid is projected but never claimed to be effective |
| 6 | `CampaignBudget.total_amount_micros` is used **only** when `period` is `CUSTOM_PERIOD`; `amount_micros` is the `DAILY` field; `period` is `Immutable`; `status` is `Output only` | **CITED** | [v25 `campaign_budget.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_budget.proto), fetched 2026-09-09. The budget change therefore writes `total_amount_micros` and never `period` |
| 7 | `CampaignStatus` includes `REMOVED` = "Campaign has been removed" | **CITED** | [v25 `campaign_status.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/enums/campaign_status.proto), fetched 2026-09-09 |
| 8 | With partial failure not enabled, the request is a single atomic failure | **CITED** | [Partial failures](https://developers.google.com/google-ads/api/docs/best-practices/partial-failures), cited by [P04](google-ads-create-execution.md#wire-fact-provenance) 2026-09-09 |
| 9 | Temporary resource names must be unique negative numbers and must be defined before they are referenced | **CITED** | [Mutate best practices](https://developers.google.com/google-ads/api/docs/mutating/best-practices), cited by [P04](google-ads-create-execution.md#wire-fact-provenance) 2026-09-09. This route references no created resource, so it allocates none |
| 10 | Proto `snake_case` maps to JSON `lowerCamelCase` | **CITED** | [REST design: JSON mappings](https://developers.google.com/google-ads/api/rest/design/json-mappings), fetched 2026-09-09: "Identifiers are transformed from *snake_case* (in protocol buffers) to *lowerCamelCase* in JSON" |
| 11 | A JSON `FieldMask` is a comma-separated string whose path segments are converted from `snake_case` to `lowerCamelCase`, so `total_amount_micros` is sent as `totalAmountMicros` and `cpc_bid_micros` as `cpcBidMicros` | **CITED for proto3; `UNVERIFIED` for the Google Ads REST surface specifically** | [ProtoJSON format](https://protobuf.dev/programming-guides/json/), fetched 2026-09-09. The Google Ads JSON-mappings page states the general identifier rule but **does not discuss `FieldMask` at all**, and its reference pages 404 through this fetcher, so the multi-word masks are the one wire fact here that a live readback must confirm. The archive's `status` mask is identical in both conventions and is unaffected |
| 12 | The write route is `POST https://googleads.googleapis.com/v25/customers/{customer_id}/googleAds:mutate` | `UNVERIFIED (REST reference 404 on 2026-09-09)` | inherited from [P04](google-ads-create-execution.md#wire-fact-provenance) |
| 13 | Whether a v25 `campaign` GAQL read returns a `REMOVED` campaign | `UNVERIFIED (not documented on any reachable page, 2026-09-09)` | the archive readback accepts **both** "one row, `REMOVED`" and "no rows", and refuses every other status, so the route does not depend on which behaviour is real |
| 14 | `partial_failure` and `response_content_type` defaults | `UNVERIFIED` | not relied on: both are always sent explicitly |
| 15 | Whether `campaign_budget.*` is selectable `FROM campaign` through the implicit join, and whether `ad_group_criterion.type = KEYWORD` is a valid predicate | `UNVERIFIED (field reference 404 on 2026-09-09)` | inherited from [P04](google-ads-create-execution.md#wire-fact-provenance); a response that omits a selected member is refused by the parser rather than defaulted |
| 16 | Whether Google recomputes an ad group's effective bid, or a campaign's pacing, immediately after these writes | `UNVERIFIED` | not claimed anywhere: `effective_delivery` is `NOT_PREDICTED` in both the postcondition and the readback |
| 17 | Which v25 GAQL resources read a campaign's experiments and drafts, shared set links, labels, conversion goal links, and portfolio bidding strategy references | `UNVERIFIED (no cited source in docs/research/)` | none is queried. Each surface is `NOT_READ`, which is why no archive can be planned and the pair is unregistered. **The five-surface enumeration is itself uncited and provisional**: the ticket that adds the cited read must first re-derive and cite the complete surface list |

### What the fixture suite does and does not prove

`tests/test_google_ads_maintain_execution.py` and
`tests/test_google_ads_maintain_dependency_proof.py` use `httpx.MockTransport`, a fake
credential store, and a fake installed approval bridge. Its fake customer
actually applies the mutation, so the four happy paths exercise a real
before/after fingerprint comparison rather than a stubbed one. Real in that
suite: the P05 maintenance contract, the three-slot protected credential
lifecycle, the S3 record, the S4 route, a real signed receipt from an ephemeral
test key, a real `SafetyKernel`, and a real local ledger. It proves the local
state machine, the exact four bodies this route would send, the spend
derivations, and every refusal. It proves **nothing** about Google Ads accepting
those bodies, about the `updateMask` encoding, about serving, or about spend.

The archive's proven path is the exception to "real" above: the suite replaces
`campaign_reference_surfaces` with a fake that reports every surface `READ_EMPTY`
(or a named one `READ_NONEMPTY`), because no cited read exists. That fake is the
only reason a plan, an execution and a readback of an archive can be exercised at
all, and it is evidence of the local state machine only, never of a campaign
being unreferenced. `test_an_archive_is_refused_without_a_dependency_proof` pins the planner's rule 11
gate with the pair test-registered; it does not show production behaviour. The
production refusal (an unregistered pair, so
`EXECUTION_PLAN_NOT_COMPILABLE` before any credential, with no plan retained) is
pinned by `test_a_production_archive_request_is_refused_as_not_compilable_before_any_credential`,
by the registry assertions in
`test_only_the_update_pair_is_registered_in_a_production_build`, and by the
installed-CLI `google_ads_maintain_execution_refusal_smoke` in
`scripts/validate_development.py`.

## Build dependency: two additive ledger record kinds

`GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN` and
`GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_READBACK_OBSERVATION` are registered in
`ledger.py`. A build whose ledger does not register both still refuses:
`plan execution create` with `GOOGLE_ADS_MAINTAIN_RECORD_KIND_NOT_INSTALLED`
before reading configuration or any credential, and the adapter factory with
`GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND_NOT_INSTALLED` at factory admission.
Neither is a fallback.

## Open work

* **Reading the campaign reference surfaces.** Until a cited v25 read reaches
  experiments and drafts, shared set links, labels, conversion goal links and
  portfolio bidding strategy references, no archive can be planned. The seam is
  `campaign_reference_surfaces`; the proof model, the proof carried inside the
  observation binding, the re-proof at execution and the review line already
  exist and are tested against a fake.
  **That five-member list is itself uncited and provisional.** The ticket that
  adds the cited read must first re-derive and cite the *complete* surface list,
  extending `CAMPAIGN_REFERENCE_SURFACES` (with a `schema_version` bump on the
  proof) where needed, before any surface can be `READ_EMPTY`. It must also
  measure and record the reference read's own interval, register the
  `("google_ads", ARCHIVE)` pair, and update `_REGISTERED_EXECUTION_PAIRS` in
  `scripts/validate_development.py`; nothing may declare `PROVEN_UNREFERENCED`
  from reads of these five alone.

* **Targeting, destination, tracking, and creative-association maintenance.**
  `definition-of-done.md` §1 L81 names those beside status, spend controls, bid,
  and schedule. This ticket ships the four operations it was scoped to; the rest
  need their own contracts, bodies, and evidence.
* A shared-budget or `DAILY`-budget campaign. The observation binding refuses
  any budget whose `period` is not `CUSTOM_PERIOD`.
* More than one ad group per campaign. The read requires exactly one, which is
  what the P04 compiler produces.
* The `updateMask` lowerCamelCase encoding for multi-word field paths, which
  needs a live readback (wire fact 11).
* Live validation. Nothing in this route has been executed against a real
  Google Ads account.

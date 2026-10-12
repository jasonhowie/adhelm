# Google Ads Search hierarchy ACTIVATE (P05)

**Status: `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.** No live Google Ads
request has been made from this route, **no activation has ever been executed
against a real account, and no spend has ever been incurred.** Every claim below
is either a fixture result from `tests/test_google_ads_activate_execution.py` or
a wire fact whose per-claim provenance is recorded in [wire-fact
provenance](#wire-fact-provenance).

P05 adds the operation rule 8 and
[`authorization-policy.md`](../authorization-policy.md) both name: **activation
is a separate, spend-explicit, digest-bound authorization.** P04 creates a
Search hierarchy in which every entity is `PAUSED`; nothing in that plan can
start delivery. This plan is the one that can, and it says so in the review a
person signs.

It registers the pair `("google_ads", ACTIVATE)` on the two provider-neutral
registries S3 and S4 introduced by
[`execution-plan-records.md`](execution-plan-records.md) and
[`generic-execution-route.md`](generic-execution-route.md). It adds no CLI leaf:
`plan execution create --provider google_ads --operation ACTIVATE` and
`execute plan <record-digest>` are the whole surface.

## What the route is

```text
plan execution create --provider google_ads --operation CREATE   (P04)
execute plan <digest>                                            (P04)
  -> a paused Search hierarchy exists
  -> plan execution create --provider google_ads --operation ACTIVATE
       resolve the retained GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN action
       one bounded read: customer identity + the whole live hierarchy
       refuse on: any ENABLED or REMOVED member, budget/date drift vs the
                  create baseline, a past end date-time, a missing final URL,
                  a DISAPPROVED ad, a non-Search campaign
       precompute the recovery pause plan and record its digest
       retain GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN
       retain EXECUTION_PLAN_RETAINED
  -> execute plan <record-digest>
       installed approval bridge -> COMPOSITE_EXECUTION_PLAN receipt
       kernel preflight -> durable APPLY_STARTED -> one egress permit
       one POST customers/{cid}/googleAds:mutate
       closed acknowledgement subset
       GAQL readback of the activated hierarchy
       retain GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION
```

### Two records, one join key

| Record kind | Written by | Read by |
| --- | --- | --- |
| `GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN` | `plan execution create` only | the P05 activate adapter factory |
| `EXECUTION_PLAN_RETAINED` | `plan execution create` only | `plan execution catalog/show`, `execute plan`, `recover attempt`, `recover catalog` |

Both describe the byte-identical `CompositePlan`, so `plan_digest` is the join
key. `_retained_action_for_plan` resolves the closed action from a verified
checkpoint and refuses unless the retained action's own plan compares byte for
byte with the approved one.

### The request envelope

`plan execution create` carries one additive, provider-discriminated envelope
member beside P04's:

```json
{
  "schema_version": 1,
  "profile_id": "google-ads-edit",
  "provider": "google_ads",
  "operation": "ACTIVATE",
  "account_ref": "google_ads.customer.1234567890",
  "resource_ref": "google_ads.create_plan.<retained-record-digest>",
  "activate_selection": {
    "selection_kind": "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE",
    "create_plan_record_digest": "<retained-record-digest>",
    "campaign_name": "Customer Search campaign",
    "expires_in_seconds": 300
  }
}
```

`ExecutionPlanRequest` keeps `extra=forbid` and additionally refuses a request
that names more than one provider selection: one plan is one operation.

### Only one activation source is installed

The plan promotes exactly one retained P04 CREATE plan. There is deliberately
**no** "activate whatever campaign has this name" path. A hierarchy this
workspace did not plan and create has no retained budget/date baseline to
compare against, so the drift check in
[preconditions](#preconditions-refused-before-egress) would have nothing to
compare and inventing a baseline from the same read it is supposed to check
would be a claim rather than a check. `plan execution create` refuses any other
`resource_ref` shape with `INVALID_GOOGLE_ADS_ACTIVATE_EXECUTION_SELECTION`
before any configuration, credential, or provider work.

Promoting a campaign discovered through a completed inventory read is therefore
**open work**, not shipped behaviour; see [open work](#open-work).

## Semantic before/after, without native identifiers

`before` and `after` are both **populated** semantic hierarchy projections
through [`providers/_semantic_state.py`](../../src/adhelm/providers/_semantic_state.py),
under a keyed namespace of their own
(`GOOGLE_ADS_SEARCH_CAMPAIGN_LIVE_HIERARCHY_V1`) that P04's create projection
cannot be replayed into:

* the **target binding** is `(customer id, HMAC of the campaign name)`;
* `before` is the observed member multiset, every non-budget member `PAUSED`;
* `after` is the same multiset with every non-budget member `ENABLED` and
  nothing else changed — no name, criterion, keyword, budget amount, date, bid,
  or creative surface.

A readback that differs anywhere else fails verification, which is the intended
outcome: an activation that also moved a budget is not the plan that was signed.

The **campaign budget member carries no status at all.** `CampaignBudget.status`
is output-only in v25 and a non-shared budget's status tracks its campaign, so
the projection would otherwise predict a field this route neither sends nor
controls. The same reasoning is why the mutate body addresses no budget.

The projection is keyed with the workspace's `export_key()`. The plan records its
`fingerprint_key_marker`, and the adapter refuses with
`GOOGLE_ADS_ACTIVATE_FINGERPRINT_KEY_CHANGED` **before its first provider read**
if the current key no longer reproduces the retained states.

## The plan

| Plan fact | Value |
| --- | --- |
| `provider` / `operation` | `google_ads` / `ACTIVATE` |
| `expected_child_egress_count` | `1` — one atomic mutate enables the whole hierarchy, so the factory declares `child_egress=False` |
| `max_spend` | the observed `CampaignBudget.total_amount_micros` in account minor units (`micros / 10_000`) |
| `review_lines` | derived from this contract's own closed review model via `approval_protocol.compiler_review_lines`, so `review_source` is `COMPILER_CANDIDATE_REVIEW` |
| `recovery_actions` | `(PAUSE,)` |
| `recovery_semantics` | `RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL` |
| `field_changes` | one `campaign/hierarchy/status` change from the paused projection to the enabled one |
| `capability_status` | `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`, `live_validated=false` |

### The spend bound is the point

`ACTIVATE` is **not** one of `execution_plan_records.NON_SPEND_OPERATIONS`
(`PAUSE`, `ARCHIVE`, `RECOVER`), so the subplan's `max_spend` always reaches the
`COMPOSITE_EXECUTION_PLAN` subject's per-currency bound and can never summarize
as `NOT_SPEND_CAPABLE`. The reviewer's first line is therefore the derived
spend statement, and
`test_the_spend_bound_is_the_budget_total_through_the_s1_derivation` pins it
against `approval_protocol.expected_composite_approval_subject` rather than
against a second hand-written comparison:

```text
Maximum incremental spend 25000 minor units USD
Provider google_ads compiled candidate review
activation effect: ACTIVATION_BEGINS_DELIVERY_AND_SPEND
activation scope: ONE_SEARCH_CAMPAIGN_AND_EVERY_CHILD_ENTITY
atomicity: ONE_MUTATE_UPDATE_MASK_STATUS_PARTIAL_FAILURE_DISABLED
bidding strategy: TARGET_SPEND_NON_CONVERSION_OBJECTIVE
budget period: CUSTOM_PERIOD_CAMPAIGN_LIFETIME_TOTAL
campaign end date time: 2030-03-31 23:59:59
campaign start date time: 2030-03-01 00:00:00
daily overdelivery: NOT_APPLICABLE_NO_AVERAGE_DAILY_BUDGET
entities enabled: 7
live validated: false
policy approval status: APPROVED
recovery: RETAIN_REFS_PAUSE_PROPOSAL_NEEDS_ITS_OWN_AUTHORIZATION
```

### Why the bound is a lifetime total and not a daily budget

The v25 budget this hierarchy was created with is `CUSTOM_PERIOD` with
`total_amount_micros` only; `amount_micros` is the `DAILY` field and the two are
mutually exclusive. The declared bound is therefore a **campaign-lifetime cap**,
and Google's up-to-2x average-daily overdelivery behaviour has nothing to apply
to — the plan says exactly that rather than staying silent. A future `DAILY`
candidate must declare `budget_period` and a doubled bound in its own review
lines before it can be activated through this route; the observation binding
refuses any budget whose `period` is not `CUSTOM_PERIOD`.

### Conversion-dependent activation is out of scope, explicitly

The finishing plan's row P05 (a build record that the public tree omits) makes
conversion-dependent activation depend on C02. The compiled campaign uses the
standard `target_spend` click strategy, which is a **non-conversion objective**,
so this route is the allowed case. The plan records that as
`conversion_dependency: NOT_APPLICABLE_TARGET_SPEND_NON_CONVERSION_OBJECTIVE`
rather than leaving a reader to infer it, **and it is a check rather than a
restatement**: the planner refuses with
`GOOGLE_ADS_ACTIVATE_BIDDING_STRATEGY_UNSUPPORTED` unless the observed
`Campaign.bidding_strategy_type` is `TARGET_SPEND`. A campaign whose strategy
had been changed out of band to a conversion one would make the plan's own
disclosure false, so it is refused rather than described.

## Recovery: a precomputed pause proposal, not a revert

The plan's `recovery_disposition` is
`RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL`. At planning time the planner reuses
the M55 PAUSE contract verbatim — its campaign baseline is `ENABLED`, which is
precisely the state this activation produces — builds the exact plan that would
return the hierarchy to `PAUSED`, and records **only its `plan_digest`**.

Three things that digest is not:

* it is not an authorization. Pausing is its own operation kind with its own
  exact grant, and the record says `NOT_AUTHORIZED_PROPOSAL_ONLY`;
* it is not retained as a plan. Nothing appends a
  `GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN` record here, and
  `test_the_recovery_disposition_records_a_precomputed_pause_plan_digest`
  asserts that;
* it is not automatic. `automatic_revert` is exactly `false`. A post-egress
  failure is ambiguous and requires reconciliation before any new plan.

What it *is* is the answer to "if this activation was a mistake, what is the
exact plan that stops it" — computed while the facts were fresh, so the answer
does not have to be recompiled under pressure.

## The single atomic mutation

One `POST customers/{cid}/googleAds:mutate`, `partialFailure=false`,
`validateOnly=false`, `responseContentType=RESOURCE_NAME_ONLY`, and one
operation per entity in this order:

1. `campaignOperation`
2. one `campaignCriterionOperation` per campaign criterion, sorted by resource name
3. `adGroupOperation`
4. one `adGroupCriterionOperation` per keyword, sorted by resource name
5. `adGroupAdOperation`

Every operation is `{"update": {"resourceName": ..., "status": "ENABLED"},
"updateMask": "status"}`. **No temporary resource ID is used or needed**:
activation addresses resources Google has already assigned identifiers to, and
no operation references one defined by a later operation. The body is a pure
function of the observed hierarchy and the customer ID, pinned as
[`tests/fixtures/google_ads_search_campaign_activate_mutate_body.json`](../../tests/fixtures/google_ads_search_campaign_activate_mutate_body.json).

The native resource names come from the **fresh pre-egress read**, never from
the retained plan. That is deliberate: the plan binds semantics under a local
key and carries no provider identifier at all
(`test_the_plan_before_is_the_paused_hierarchy_and_after_is_enabled` asserts
that no `customers/` path, `resourceName`, or native ID appears in it), while
the mutation needs paths that only a current read can supply. The two are tied
together by the `before` fingerprint: a read that does not reproduce it is
refused as drift before the permit is consumed.

### The closed acknowledgement subset

The only accepted local acknowledgement is an exact HTTP 200 JSON object whose
**sole** member is `mutateOperationResponses`, holding exactly one result per
planned operation, in order, each carrying exactly the expected result field
(`campaignResult`, `campaignCriterionResult`, `adGroupResult`,
`adGroupCriterionResult`, `adGroupAdResult`) and a `resourceName` under this
customer's path. A partial-failure envelope, an extra member, a missing or extra
result, an out-of-order result field, a resource name outside this customer, or
a non-200 status is refused.

The permit is already consumed at that point, so every one of those outcomes is
reported as `AMBIGUOUS_REMOTE_STATE` and must not be retried. For an activation
the possible unobserved effect is **delivery and spend**, which is why the
diagnostic says so.

## Verification, reconciliation, and recovery

`verify` reports the proof this adapter actually established: the acknowledgement
subset digest and the retained readback record digest. It is `verified=False`
until both exist.

`reconcile` reads the current state and returns
`EffectStatus.AMBIGUOUS_REMOTE_STATE` with `settled=False`, **always**. A later
matching read cannot attribute a missing or uncertain acknowledgement to this
request: there is no reviewed conditional-write or idempotency proof for this
offline SDK-derived route, and an activation that may already be delivering is
never settled locally. Rule 24's observation-interval and `UNKNOWN`-actor
discipline is what applies instead.

## The readback record

`GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION` retains one bounded
post-acknowledgement observation:

* the observation interval, the observed customer status, and the observed
  member count;
* the two semantic states this workspace observed on either side of the one
  mutation, as closed literals;
* the **authorized** per-currency ceiling, restated from the plan, beside
  `observed_spend: NOT_OBSERVABLE_FROM_THIS_ROUTE` — nothing local can observe
  provider delivery, and the record refuses to imply otherwise;
* the policy approval status the plan disclosed, verbatim;
* the recovery pause proposal digest and its `NOT_AUTHORIZED_PROPOSAL_ONLY`
  marker;
* the ordered acknowledgement result kinds.

`campaign_resource_ref` and `ad_group_resource_ref` are **keyless SHA-256
references** over the exact resource paths under a domain separator of their
own, so two records can be compared for identity without the record publishing a
Google Ads campaign or ad-group ID. No request body, response body, bearer, or
provider diagnostic is retained.

`validate_google_ads_activate_readback_history` runs on append **and on scan**.
It requires every readback to name one retained activate action, restate that
action's plan and subplan digests, member count, authorized bound, recovery
digest and policy status, and follow exactly one durable `APPLY_STARTED` for the
same attempt. A second readback for one attempt is refused: the fixed route
performs one mutation and one readback.

## Preconditions refused before egress

| Check | Refusal |
| --- | --- |
| Configuration changed since the receipt was reserved | `CONFIGURATION_CHANGED` |
| Unresolved `APPLY_STARTED` or run in local history | `RECOVERY_REQUIRED` |
| No retained CREATE plan with this record digest | `GOOGLE_ADS_ACTIVATE_CREATE_PLAN_NOT_RETAINED` |
| The retained CREATE plan is another customer's or another campaign's | `INVALID_GOOGLE_ADS_ACTIVATE_EXECUTION_SELECTION` |
| Either additive ledger record kind is not installed | `GOOGLE_ADS_ACTIVATE_RECORD_KIND_NOT_INSTALLED` |
| Customer is not `ENABLED` | `GOOGLE_ADS_ACCOUNT_NOT_ENABLED` |
| Currency or timezone differs from the configured EDIT profile | `GOOGLE_ADS_ACTIVATE_FACTS_INVALID` |
| Any member is already `ENABLED` (a no-operation is not a plan) | `GOOGLE_ADS_ACTIVATE_ALREADY_ENABLED` |
| Any member is `REMOVED` | `GOOGLE_ADS_ACTIVATE_MEMBER_REMOVED` |
| The hierarchy is not entirely paused | `GOOGLE_ADS_ACTIVATE_HIERARCHY_NOT_PAUSED` |
| The campaign is not a Search campaign | `GOOGLE_ADS_ACTIVATE_CAMPAIGN_TYPE_UNSUPPORTED` |
| The observed bidding strategy is not `TARGET_SPEND` | `GOOGLE_ADS_ACTIVATE_BIDDING_STRATEGY_UNSUPPORTED` |
| Budget total or either campaign date differs from the create baseline | `GOOGLE_ADS_ACTIVATE_BASELINE_DRIFT` |
| `Campaign.end_date_time` is already in the past (in the customer's zone) | `GOOGLE_ADS_ACTIVATE_CAMPAIGN_ENDED` |
| An ad has no `final_urls` | `PROVIDER_RESPONSE_INVALID` (the read refuses an ad with no destination) |
| The ad is `DISAPPROVED` | `GOOGLE_ADS_ACTIVATE_AD_DISAPPROVED` |
| The policy approval status is one this build does not interpret | `GOOGLE_ADS_ACTIVATE_POLICY_STATUS_UNSUPPORTED` |
| At execution: the current EDIT association differs from the plan's | `GOOGLE_ADS_ACTIVATE_EXECUTION_BINDING_DRIFT` |
| At execution: the local fingerprint key no longer reproduces the states | `GOOGLE_ADS_ACTIVATE_FINGERPRINT_KEY_CHANGED` |
| At execution: any hierarchy difference from the approved `before` | `REMOTE_DRIFT` |
| At execution: the readback record kind is not installed | `GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND_NOT_INSTALLED` |
| At execution: the plan expired | `PLAN_EXPIRED` |

**Policy approval status is read, not assumed.** `AdGroupAd.policy_summary` is
output-only, and v25 may omit it. When it is present the plan requires one of
`APPROVED`, `APPROVED_LIMITED`, or `AREA_OF_INTEREST_ONLY`; `DISAPPROVED` is
refused; `UNSPECIFIED`/`UNKNOWN` or anything else is refused as uninterpretable.
When it is absent the plan records the explicit disclosure
`NOT_READ_NO_POLICY_SUMMARY_RETURNED` and that string appears verbatim in the
review lines, so a reviewer authorizing spend can see that policy state was not
established. `test_an_unread_policy_summary_is_disclosed_rather_than_assumed`
pins that.

## Wire-fact provenance

Google's own reference pages render navigation-only or `404` through this
fetcher, so where a claim could be read from the official v25 protobuf
definitions in `googleapis/googleapis` it is marked **CITED**; where it could
not, the exact unreachable source is named and the claim is `UNVERIFIED`. Every
fact below is a *local* engineering decision until an authorized live readback
exists.

| # | Wire fact this route relies on | Status | Source |
| --- | --- | --- | --- |
| 1 | The request message is `MutateGoogleAdsRequest` with `customer_id`, `mutate_operations`, `partial_failure`, `validate_only`, `response_content_type` | **CITED** | [v25 `google_ads_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/google_ads_service.proto), fetched 2026-09-09 |
| 2 | `MutateOperation` carries `campaign_operation`, `campaign_criterion_operation`, `ad_group_operation`, `ad_group_criterion_operation`, `ad_group_ad_operation`, and `MutateOperationResponse` carries the matching `*_result` members | **CITED** | same file, fetched 2026-09-09 |
| 3 | `CampaignOperation` has `google.protobuf.FieldMask update_mask = 4` and an `oneof operation { create, update, remove }`, where `update` "is expected to have a valid resource name" | **CITED** | [v25 `campaign_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/campaign_service.proto), fetched 2026-09-09 |
| 4 | `CampaignCriterionOperation` has the same `update_mask` + `create/update/remove` shape | **CITED** | [v25 `campaign_criterion_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/campaign_criterion_service.proto), fetched 2026-09-09 |
| 5 | `CampaignStatus` is `UNSPECIFIED, UNKNOWN, ENABLED, PAUSED, REMOVED`, with `ENABLED` = "Campaign is active and can show ads" | **CITED** | [v25 `campaign_status.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/enums/campaign_status.proto), fetched 2026-09-09. This is the wire basis for "activation begins delivery" |
| 6 | `CampaignCriterion.status` is mutable, while `campaign`, `negative`, and the `ad_schedule`/`location`/`language` oneof members are `Immutable` and `criterion_id` is `Output only` | **CITED** | [v25 `campaign_criterion.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_criterion.proto), fetched 2026-09-09. Criterion *status* is therefore the only field an activation touches |
| 7 | `AdGroup.status` is mutable and `AdGroup.type` is `Immutable` | **CITED** | [v25 `ad_group.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/ad_group.proto), fetched 2026-09-09 |
| 8 | `AdGroupAd.status` is mutable; `resource_name` and `ad` are `Immutable`; `policy_summary` is `Output only` of type `AdGroupAdPolicySummary` | **CITED** | [v25 `ad_group_ad.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/ad_group_ad.proto), fetched 2026-09-09 |
| 9 | `PolicySummary.approval_status` exists and `PolicyApprovalStatus` is `UNSPECIFIED, UNKNOWN, DISAPPROVED ("Will not serve"), APPROVED_LIMITED ("Serves with restrictions"), APPROVED ("Serves without restrictions"), AREA_OF_INTEREST_ONLY` | **CITED** | [v25 `policy_summary.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/common/policy_summary.proto) and [v25 `policy_approval_status.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/enums/policy_approval_status.proto), both fetched 2026-09-09. The three statuses this build accepts are exactly the ones documented as serving |
| 10 | `CampaignBudget.total_amount_micros` is used **only** when `period` is `CUSTOM_PERIOD`; `amount_micros` is the `DAILY` field; `period` is `Immutable`; `status` is `Output only`/read-only | **CITED** | [v25 `campaign_budget.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_budget.proto), fetched 2026-09-09. This is why the projection carries no budget status and the mutate body addresses no budget |
| 11 | A non-shared budget's "name and status will stay in sync with the campaign's name and status" | **CITED** | same file, fetched 2026-09-09 |
| 12 | `Campaign.start_date_time` / `end_date_time` use `"yyyy-MM-dd HH:mm:ss"` **in the customer's time zone** | **CITED** | [v25 `campaign.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto), fetched 2026-09-09. This is why the past-end-date check localizes the value rather than reading it as UTC |
| 13 | Proto `snake_case` maps to JSON `lowerCamelCase`, so the members above are `mutateOperations`, `campaignOperation`, `updateMask`, ... | **CITED** | [REST design: JSON mappings](https://developers.google.com/google-ads/api/rest/design/json-mappings), fetched 2026-09-09: "Identifiers are transformed from *snake_case* (in protocol buffers) to *lowerCamelCase* in JSON" |
| 14 | A JSON `FieldMask` is a comma-separated string whose path segments are `lowerCamelCase` | **CITED** | [ProtoJSON format](https://protobuf.dev/programming-guides/json/), fetched 2026-09-09. This route's only mask is the single-word `status`, which is identical in both conventions, so nothing here depends on the conversion |
| 15 | `partial_failure` defaults to false | `UNVERIFIED (default not stated on any reachable page, 2026-09-09)` | the route never relies on the default: it always sends `"partialFailure": false` explicitly |
| 16 | The write route is `POST https://googleads.googleapis.com/v25/customers/{customer_id}/googleAds:mutate` | `UNVERIFIED (REST reference 404 on 2026-09-09)` | inherited from [P04](google-ads-create-execution.md#wire-fact-provenance); the host and `/v25/` segment are the read route's |
| 17 | Resource-name formats `customers/{cid}/campaigns/{id}`, `customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}`, `customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}` | **CITED** | the `campaign_criterion.proto` and `ad_group_ad.proto` comments above, fetched 2026-09-09 |
| 18 | `campaign_budget.*` and `ad_group_ad.policy_summary.approval_status` are selectable through the implicit GAQL joins this readback uses | `UNVERIFIED (field reference 404 on 2026-09-09)` | `.../fields/v25/campaign` and `.../fields/v25/ad_group_ad` both returned HTTP 404. A response that omits a selected member is refused by the parser rather than defaulted |
| 19 | `RESOURCE_NAME_ONLY` is the v25 default for `response_content_type` | `UNVERIFIED` | not relied on: the route sends the value explicitly so the acknowledgement shape is fixed by the request |
| 20 | Whether a v25 `campaign` GAQL read returns a `REMOVED` campaign | `UNVERIFIED (not documented on any reachable page, 2026-09-09)` | the activation route does not depend on it — it refuses any `REMOVED` member however it is observed. The [maintenance route](google-ads-maintain-execution.md) accepts both outcomes explicitly for its archive readback |

### What the fixture suite does and does not prove

`tests/test_google_ads_activate_execution.py` uses `httpx.MockTransport`, a fake
credential store, and a fake installed approval bridge. Real in that suite: the
P05 contract, the P04 CREATE plan it promotes, the three-slot protected
credential lifecycle, the S3 record, the S4 route, a real signed receipt from an
ephemeral test key, a real `SafetyKernel`, and a real local ledger. It proves the
local state machine, the exact body this route would send, the spend derivation,
and every refusal. It proves **nothing** about Google Ads accepting that body,
about provider approval, about serving, about pacing, or about spend.

## Build dependency: two additive ledger record kinds

`GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN` and
`GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION` are registered in
`ledger.py`. A build whose ledger does not register both still refuses:

* `plan execution create --provider google_ads --operation ACTIVATE` refuses with
  `GOOGLE_ADS_ACTIVATE_RECORD_KIND_NOT_INSTALLED` before reading configuration
  or any credential, and
* the adapter factory refuses with
  `GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND_NOT_INSTALLED` at factory admission,
  before any credential, header, or provider request.

Neither is a fallback. There is no path that activates a hierarchy and then
fails to retain the observation that proves what it activated.

## Open work

* Promoting a campaign named by a completed inventory read, rather than by a
  retained CREATE plan. It needs its own budget/date baseline story before it
  can be a plan rather than a claim.
* A `DAILY`-budget candidate, with the up-to-2x average-daily overdelivery
  disclosed in its own review lines and a bound that reflects it.
* Conversion-dependent activation, which depends on C02.
* Live validation. Nothing in this route has been executed against a real
  Google Ads account, and the capability status stays
  `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED` until it is.

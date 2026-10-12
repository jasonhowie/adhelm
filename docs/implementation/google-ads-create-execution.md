# Google Ads paused Search hierarchy CREATE (P04)

**Status: `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.** No live Google Ads
request has been made from this route. Every claim below is either a fixture
result from `tests/test_google_ads_create_execution.py` or a wire fact whose
per-claim provenance is recorded in [wire-fact
provenance](#wire-fact-provenance). Nothing here activates advertising: every
entity this route creates is `PAUSED`, and activation stays a separate,
spend-explicit, digest-bound authorization (rule 8).

P04 promotes the offline Search/RSA compiler candidate
(`providers/google_ads_compiler.py`, `OFFLINE_CANDIDATE_NOT_EXECUTABLE`) to a
retained `EXECUTION_PLAN_RETAINED` plan and adds the guarded CREATE adapter for
the pair `("google_ads", CREATE)` on the two provider-neutral registries S3 and
S4 introduced by [`execution-plan-records.md`](execution-plan-records.md) and
[`generic-execution-route.md`](generic-execution-route.md). It adds no CLI
leaf: `plan execution create --provider google_ads --operation CREATE` and
`execute plan <record-digest>` are the whole surface.

## What the route is

```text
compile google-ads (retained CANDIDATE_COMPILED)
  -> plan execution create --provider google_ads --operation CREATE
       recompile the supplied brief, bind it to the retained candidate digest
       one bounded read: customer identity + exact campaign-name collision
       retain GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN
       retain EXECUTION_PLAN_RETAINED
  -> execute plan <record-digest>
       installed approval bridge -> COMPOSITE_EXECUTION_PLAN receipt
       kernel preflight -> durable APPLY_STARTED -> one egress permit
       one POST customers/{cid}/googleAds:mutate
       closed acknowledgement subset
       GAQL readback of the created hierarchy by name
       retain GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION
```

### Two records, one join key

| Record kind | Written by | Read by |
| --- | --- | --- |
| `GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN` | `plan execution create` only | the P04 adapter factory |
| `EXECUTION_PLAN_RETAINED` | `plan execution create` only | `plan execution catalog/show`, `execute plan`, `recover attempt`, `recover catalog` |

Both describe the byte-identical `CompositePlan`, so `plan_digest` is the join
key. `_retained_action_for_plan` resolves the closed action from a verified
checkpoint and refuses unless the retained action's own plan compares byte for
byte with the approved one.

The provider-specific record is not duplication for its own sake. The
provider-neutral record models no native request at all, and the adapter cannot
build a `googleAds:mutate` body without the compiled candidate. A retained
`CANDIDATE_COMPILED` record cannot supply it either: that record deliberately
keeps only the redacted review and declares
`requires_new_compilation_for_planning`. The planner therefore **recompiles the
supplied brief** and refuses with `GOOGLE_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH`
unless the recompiled candidate is byte-identical to the retained one.

That is why `plan execution create` carries one additive, provider-discriminated
envelope member, exactly as `ExecutionPlanRequest`'s own docstring anticipates:

```json
{
  "schema_version": 1,
  "profile_id": "google-ads-edit",
  "provider": "google_ads",
  "operation": "CREATE",
  "account_ref": "google_ads.customer.1234567890",
  "resource_ref": "google_ads.candidate.<retained-record-digest>",
  "selection": {
    "selection_kind": "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE",
    "candidate_record_digest": "<retained-record-digest>",
    "fresh": { "...": "the exact GoogleAdsSearchCampaignBrief" },
    "expires_in_seconds": 300
  }
}
```

## Semantic before/after, without native identifiers

Google assigns every identifier at creation time, so a state fingerprint built
from resource names would be unusable as a prediction. `before` and `after` are
therefore **semantic** hierarchy projections through
[`providers/_semantic_state.py`](../../src/adhelm/providers/_semantic_state.py):
locally keyed HMACs over the request-controllable names and configuration only.

* The **target binding** is `(customer id, HMAC of the campaign name)`.
* `before` is the *empty* member multiset under that binding. That is the local
  statement "no campaign with this name in this customer".
* `after` is the exact predicted member multiset: one campaign budget, one
  campaign, every location/language/ad-schedule campaign criterion, one ad
  group, every keyword ad-group criterion, and one ad-group ad.

Every member fingerprint is `HMAC(identity components) + configuration`, where
the configuration carries no key ending in `_id`, no `name`/`resourceName`, and
no provider resource path — `_semantic_state.member_fingerprint` rejects those
shapes outright. Native constants such as `geoTargetConstants/2840` are hashed
into the *identity* slot rather than the configuration, so the projection stays
comparable and still binds the exact target.

The ad member binds `rsa_text_digest(headlines, descriptions, final_urls)`
rather than the compiler's `creative_content_digest`, because the latter also
commits the declared CTA headline and the local creative-version identity,
neither of which Google returns. The projection must bind the surface both
sides can observe, in the order they observe it. An extra or reordered headline
in the readback therefore fails verification, which is the intended outcome
when campaign-level asset automation expands the text.

The projection is keyed with the workspace's `export_key()`. The plan records
its `fingerprint_key_marker`, and the adapter refuses with
`GOOGLE_ADS_CREATE_FINGERPRINT_KEY_CHANGED` **before its first provider read**
if the current key no longer reproduces the retained states.

## The plan

| Plan fact | Value |
| --- | --- |
| `provider` / `operation` | `google_ads` / `CREATE` |
| `expected_child_egress_count` | `1` — Google creates the whole hierarchy in one atomic mutate, so the factory declares `child_egress=False` |
| `max_spend` | the candidate's own campaign-lifetime cap (`CampaignBudget.total_amount_micros / 10_000` USD minor units) |
| `review_lines` | derived from the compiler's `redacted_review` via `approval_protocol.compiler_review_lines`, so `review_source` is `COMPILER_CANDIDATE_REVIEW` |
| `recovery_actions` | `(PAUSE,)` |
| `recovery_semantics` | `RETAIN_REFS_LEAVE_PAUSED_NO_DELETE` |
| `field_changes` | one `campaign/hierarchy` change from the empty projection to the predicted one |
| `capability_status` | `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`, `live_validated=false` |

### Why there is no daily-overdelivery review line

The v25 budget this candidate compiles is `CUSTOM_PERIOD` with
`total_amount_micros` only; `amount_micros` is not representable and the two are
mutually exclusive (see [the compiler
contract](../research/google-ads-compiler-contract.md)). The declared bound is
therefore a campaign-lifetime cap, not an average daily budget, and Google's
up-to-2x daily overdelivery behaviour has nothing to apply to. A future
`DAILY`-budget candidate must declare that overdelivery in its review lines
before it can be promoted; this route refuses any candidate that is not the
compiled `CUSTOM_PERIOD` shape, because the compiler cannot produce another.

### Paused status is enforced at the plan level

`GoogleAdsSearchHierarchyProjection` refuses any member whose configured
`status` is not `PAUSED`. The compiler's own types already force this at every
level, and the projection restates it so a constructed or altered action cannot
reach a mutate body. The postcondition carries
`every_entity_configured_status: PAUSED` and
`activation: SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED`.

## The bounded promotion checks

Two of the compiler contract's required promotion checks have **no readable
account-scoped surface in v25**, and the plan says so rather than implying a
check it did not make:

| Precondition | Retained value |
| --- | --- |
| campaign-name collision | `NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED`, with `observed_campaign_name_matches: 0` |
| campaign-name near match | `NO_NEAR_MATCH_CAMPAIGN_NAME_OBSERVED`, with `observed_near_match_names: 0` and the exact `probed_near_match_names` count |
| name comparison rule | `BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE` |
| provider name-comparison semantics | `UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER` |
| campaign budget quota | `NOT_READABLE_NO_PROVIDER_SURFACE_IN_THIS_BUILD` |
| `geo_target_type_setting` | `NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE` |
| account asset automation | `NOT_READABLE_WEB_INTERFACE_ONLY_IN_V25` |
| campaign asset automation | `NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE` |
| asset-automation effect on the compiled text | `NOT_PREDICTED` |

The two `READ_BACK_AFTER_CREATE` values are honoured: the post-acknowledgement
readback records the provider-chosen
`campaign.geo_target_type_setting.positive_geo_target_type` /
`negative_geo_target_type` and every
`campaign.asset_automation_settings[]` pair, marked
`OBSERVED_PROVIDER_DEFAULT_NOT_AUTHORIZED`. They are facts this workspace
observed, never something this plan authorized.

### The campaign-name collision read, and what it refuses

GAQL's published grammar defines a string literal as `(' Char* ')` or
`(" Char* ")` and **does not define an escape rule**, so this build refuses to
construct a quoted literal for a name containing `'`, `"`, `\`, or a backtick.
That is a typed pre-egress refusal (`GOOGLE_ADS_CREATE_NAME_NOT_QUERYABLE`),
not a fallback: no credential is read and no request is made.

**One normalization rule governs both sides of every name comparison: none.**
The `before` projection hashes the campaign name byte for byte, and the GAQL
probe asks for byte-exact string literals. Neither side casefolds, trims, or
normalizes, so a plan can never be executed against a differently normalized
name and the retained `before` fingerprint always names exactly the string the
candidate compiled.

On top of that byte-exact rule the plan applies a **second, additional
refusal**. GAQL has no case-insensitive operator and does not document whether
`campaign.name =` compares case-sensitively (facts 17a and 17b), while some
Google Ads surfaces do treat campaign names case-insensitively. Creating a
campaign whose name a person cannot distinguish from an existing one is a real
hazard, so the planner refuses it:

* `near_match_campaign_names` builds a **closed, deterministic** variant set --
  the cross product of two seeds (the name and its stripped form), the four
  Unicode normalization forms, five case foldings, and four single-space
  paddings -- bounded at 64 strings and typically 15 for an ASCII name.
* One bounded GAQL `IN` read asks for the exact name and every variant at once,
  and the returned names are classified locally by byte-exact comparison:
  matches equal to the planned name are collisions, the rest are near matches.
* Either count above zero refuses: `GOOGLE_ADS_CREATE_NAME_COLLISION` or
  `GOOGLE_ADS_CREATE_NAME_COLLISION_NEAR_MATCH`. The adapter re-checks both
  before egress and raises `REMOTE_DRIFT` if one appears after planning.

The limits are stated rather than implied away. This catches a name differing
only by surrounding whitespace, by normalization form, or by a *uniform* case
change. It does **not** catch an arbitrary mixed-case name such as
`cUsToMeR search`, padding wider than one space, or a name that is merely
NFKC-equal to the planned one without the planned one being non-ASCII. No
bounded local enumeration can, and an unbounded account-wide campaign scan is
not a read this route is willing to make. The retained preconditions say so
directly: `name_comparison` is
`BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE` and
`provider_name_comparison_semantics` is
`UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER`, beside the exact
`probed_near_match_names` count.

## The single atomic mutation

One `POST https://googleads.googleapis.com/v25/customers/{customer_id}/googleAds:mutate`
with ordered `mutateOperations` and temporary resource IDs:

| # | Operation | Temporary resource name |
| --- | --- | --- |
| 1 | `campaignBudgetOperation.create` | `customers/{cid}/campaignBudgets/-1` |
| 2 | `campaignOperation.create` (names `-1` as `campaignBudget`) | `customers/{cid}/campaigns/-2` |
| 3..n | `campaignCriterionOperation.create` (each names `-2`) | — |
| n+1 | `adGroupOperation.create` (names `-2`) | `customers/{cid}/adGroups/-3` |
| ... | `adGroupCriterionOperation.create` (each names `-3`) | — |
| last | `adGroupAdOperation.create` (names `-3`) | — |

`partialFailure` is `false`, `validateOnly` is `false`, and
`responseContentType` is `RESOURCE_NAME_ONLY`. `MUTABLE_RESOURCE` was
considered and rejected: this route already re-reads the whole hierarchy through
GAQL for verification, so a larger response would add payload without adding
proof, and `RESOURCE_NAME_ONLY` keeps the acknowledgement shape the parser
accepts minimal and fixed by the request rather than by a server default.

The exact body is a pure function of the compiled candidate and the customer ID
(`google_ads_search_campaign_create_mutate_body`) and is pinned as golden JSON
at
[`tests/fixtures/google_ads_search_campaign_create_mutate_body.json`](../../tests/fixtures/google_ads_search_campaign_create_mutate_body.json).

**The permit is consumed immediately before the POST**, after the header
callback, the configuration recheck, and the transport revalidation have all
completed, so a drift, expiry, or clock failure stays a pre-egress refusal.

### The closed acknowledgement subset

`_required_acknowledgement` accepts exactly one shape and nothing else:

* HTTP `200`,
* a JSON object whose **sole** member is `mutateOperationResponses`,
* a list with **exactly one result per planned operation, in order**,
* each element an object with **exactly** the expected result field for that
  index (`campaignBudgetResult`, `campaignResult`, `campaignCriterionResult`,
  `adGroupResult`, `adGroupCriterionResult`, `adGroupAdResult`),
* whose sole member is `resourceName`, matching
  `customers/{this customer}/{collection}/{positive id}`.

A `partialFailureError` member, a missing or extra result, an out-of-order
result field, a resource name outside this customer, or any other status is
`AMBIGUOUS_REMOTE_STATE`. The GAQL readback is what actually proves the
hierarchy.

#### Constraint for any future UPDATE, REPLACE, or DELETE ticket

Two ordering caveats are stated rather than hidden: the parser **cannot**
distinguish two swapped results of the *same* result kind (two
`campaignCriterionResult` entries, or two `adGroupCriterionResult` entries), and
the resource-name check binds the customer and the collection, not which
criterion the provider created first. Google's own mutate guidance groups
operations by resource type, which is exactly the shape that makes same-kind
results indistinguishable.

**In v1 this is inert.** P04 creates and never mutates a sibling afterwards, and
the only two resource references it retains — `campaign_resource_ref` and
`ad_group_resource_ref` — come from result kinds that occur exactly once
(`campaignResult`, `adGroupResult`), so neither can be mis-associated. The
verification that matters is the whole-hierarchy semantic comparison, which is
order-independent because `collection_state` sorts its member fingerprints.

**It stops being inert the moment a later ticket wants a per-sibling
reference.** A future P05 UPDATE, REPLACE, or DELETE that retains an opaque
resource reference per criterion or per keyword, and later addresses one of them
individually, could associate a recorded reference with the wrong sibling. Such
a ticket must not read a per-sibling reference out of `mutateOperationResponses`
positionally. It must instead bind each sibling by a request-controllable
identity through a GAQL readback — the geo target constant, the language
constant, the schedule tuple, the keyword text and match type — exactly as the
semantic projection already does. Rule 11 makes this load-bearing for DELETE in
particular: `PROVEN_UNREFERENCED` cannot be proven about a resource whose
identity was inferred from an ambiguous ordinal.

## Verification, reconciliation, and recovery

`apply_guarded` records a proof only when its own post-acknowledgement readback
equals the approved `after` fingerprint *and* revision. `verify` reports that
proof and is **false without it** — it takes no third read and never returns
true from a matching read alone.

`reconcile` always returns `AMBIGUOUS_REMOTE_STATE` with `settled=False`. A
later matching read cannot attribute a create to this request, and repeating the
create would duplicate the hierarchy rather than converge, so this route has no
settlement or idempotency claim at all.

**Recovery disposition: retain refs, leave paused, never delete.** Everything
created is `PAUSED`, and rule 11 permits a delete only for a resource the
dependency engine proves `PROVEN_UNREFERENCED`. The adapter has no delete,
remove, or archive path. `SubPlan` requires at least one provider-request
recovery operation and refuses `RECOVER`, so `recovery_actions` names `PAUSE`:
the only non-destructive compensating operation, and a no-op against a hierarchy
this route already created paused. The declared disposition itself is carried by
the action's `recovery_semantics` and by the readback record's
`recovery_disposition`, both `RETAIN_REFS_LEAVE_PAUSED_NO_DELETE`.

## The readback record

`GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION` mirrors P03's record
and holds the hierarchy's opaque resource references **pseudonymized**:
`campaign_resource_ref` and `ad_group_resource_ref` are
`google_ads.<kind>:sha256:<digest>` over the acknowledged resource paths. A
create mints identifiers this workspace never chose; the plan already carries
the customer-configured campaign name, which is the handle a bounded recovery
uses, so the record keeps identity comparability without publishing a Google Ads
campaign or ad-group ID. This is a deliberate difference from P03, whose record
retains a campaign reference this workspace already knew before the write.

Besides the pseudonymized references the record holds only ack facts and local
observation facts: the bounded interval, the observed member count, the observed
customer status, the ordered acknowledged result kinds, the provider-chosen geo
target types, the observed campaign asset-automation settings, and the fixed
`NOT_PREDICTED` / `False` control values for delivery, conditional-write proof,
and idempotency proof. No request body, response body, bearer, developer token,
or provider diagnostic is retained.

`validate_google_ads_create_readback_history` requires every readback to name a
retained action, restate that action's plan digest, subplan digest, provider API
version, capability version, and predicted member count, and follow **exactly
one** durable `APPLY_STARTED` for the same attempt. A second readback for one
attempt is refused.

## Preconditions refused before egress

| Refusal | Code | Where |
| --- | --- | --- |
| campaign name already exists | `GOOGLE_ADS_CREATE_NAME_COLLISION` (plan) / `REMOTE_DRIFT` (execute) | planner read; adapter baseline |
| a near-variant campaign name exists | `GOOGLE_ADS_CREATE_NAME_COLLISION_NEAR_MATCH` (plan) / `REMOTE_DRIFT` (execute) | planner read; adapter baseline |
| candidate currency or timezone differs from the customer | `GOOGLE_ADS_CREATE_ACCOUNT_MISMATCH` | planner |
| customer status is not `ENABLED` | `GOOGLE_ADS_ACCOUNT_NOT_ENABLED` (plan) / `IDENTITY_MISMATCH` (execute) | planner; adapter identity |
| candidate not fully paused | `GOOGLE_ADS_CREATE_CANDIDATE_NOT_PAUSED` | planner, before configuration or any credential |
| supplied brief does not recompile to the retained candidate | `GOOGLE_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH` | planner |
| `resource_ref` names another retained candidate | `INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION` | planner |
| developer-token / OAuth / binding-key continuity broken | `GOOGLE_ADS_CREATE_EXECUTION_BINDING_DRIFT`, or the sealed session's own `CONFIGURATION_CHANGED` / credential refusals | factory admission |
| local fingerprint key changed | `GOOGLE_ADS_CREATE_FINGERPRINT_KEY_CHANGED` | adapter, before its first read |
| the ledger does not register a P04 record kind | `GOOGLE_ADS_CREATE_RECORD_KIND_NOT_INSTALLED` (plan) / `GOOGLE_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED` (execute) | before any credential |
| plan expired | `PLAN_EXPIRED` | factory and adapter |
| campaign name is not GAQL-quotable in this build | `GOOGLE_ADS_CREATE_NAME_NOT_QUERYABLE` | planner |

In the ordinary build the whole route stops two gates earlier: the installed
approval bridge refuses `COMPOSITE_EXECUTION_PLAN`
(`NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED`) before any credential is opened.

## Wire-fact provenance

Google's own reference pages have been rendering navigation-only or `404`
through this fetcher for several days. Where a claim could be read from the
official v25 protobuf definitions in `googleapis/googleapis` it is marked
**CITED**; where it could not, the exact unreachable source is named and the
claim is `UNVERIFIED`. Every fact below is a *local* engineering decision until
an authorized live readback exists.

| # | Wire fact this route relies on | Status | Source |
| --- | --- | --- | --- |
| 1 | The request message is `MutateGoogleAdsRequest` with `customer_id`, `mutate_operations`, `partial_failure`, `validate_only`, `response_content_type` | **CITED** | [v25 `google_ads_service.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/services/google_ads_service.proto), fetched 2026-09-09 |
| 2 | The response message is `MutateGoogleAdsResponse` with `mutate_operation_responses` and `partial_failure_error` | **CITED** | same file, fetched 2026-09-09 |
| 3 | `MutateOperation` oneof members `campaign_budget_operation`, `campaign_operation`, `campaign_criterion_operation`, `ad_group_operation`, `ad_group_criterion_operation`, `ad_group_ad_operation` | **CITED** | same file, fetched 2026-09-09 |
| 4 | `MutateOperationResponse` oneof members `campaign_budget_result`, `campaign_result`, `campaign_criterion_result`, `ad_group_result`, `ad_group_criterion_result`, `ad_group_ad_result` | **CITED** | same file, fetched 2026-09-09 |
| 5 | Proto `snake_case` maps to JSON `lowerCamelCase`, so the members above are `mutateOperations`, `partialFailure`, `validateOnly`, `responseContentType`, `mutateOperationResponses`, `campaignBudgetResult`, ... | **CITED** | [REST design: JSON mappings](https://developers.google.com/google-ads/api/rest/design/json-mappings), cited by [P03](google-ads-pause-execution.md#wire-fact-provenance) 2026-09-09 |
| 6 | `GoogleAdsService.Mutate` supports temporary resource names, which must be **unique negative numbers** and must be **defined before they are referenced** in the same request | **CITED** | [Mutate best practices](https://developers.google.com/google-ads/api/docs/mutating/best-practices), fetched 2026-09-09 ("each temporary resource name must use a unique negative number"; "can only be used after it's been defined in a resource") |
| 7 | With partial failure not enabled, a request with interdependent temporary-ID operations is a single atomic failure | **CITED** | [Partial failures](https://developers.google.com/google-ads/api/docs/best-practices/partial-failures), fetched 2026-09-09 ("treat the request as a single atomic failure") |
| 8 | `partial_failure` defaults to false | `UNVERIFIED (default not stated on any reachable page, 2026-09-09)` | the route never relies on the default: it always sends `"partialFailure": false` explicitly |
| 9 | The write route is `POST https://googleads.googleapis.com/v25/customers/{customer_id}/googleAds:mutate` | `UNVERIFIED (REST reference 404 on 2026-09-09)` | `.../rest/reference/rest/v25/customers/googleAds/mutate` and the v21 equivalent both returned HTTP 404. The host and `/v25/` segment are the read route's, which [`google-ads-edit-observation-contract.md`](../research/google-ads-edit-observation-contract.md) cites; the `:mutate` custom-method suffix follows the same `googleAds:search` pattern this workspace already uses |
| 10 | Resource-name format `customers/{customer_id}/{collection}/{id}` and the JSON member `resourceName` | **CITED** | [REST design: resource names](https://developers.google.com/google-ads/api/rest/design/resource-names), cited by [P03](google-ads-pause-execution.md#wire-fact-provenance) 2026-09-09 |
| 11 | `Campaign.start_date_time` / `end_date_time` use `"yyyy-MM-dd HH:mm:ss"` in the customer's time zone, with `00:00:00` / `23:59:59` for daily granularity | **CITED** | [v25 `campaign.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto), fetched 2026-09-09. This resolves the compiler contract's previously unresolved date-time item |
| 12 | `CampaignBudget.total_amount_micros` is used **only** when `period` is `CUSTOM_PERIOD`; `amount_micros` is the `DAILY` field; `delivery_method` defaults to `STANDARD`; `explicitly_shared` defaults to **true** | **CITED** | [v25 `campaign_budget.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_budget.proto), fetched 2026-09-09. The route sends `explicitlyShared: false` explicitly because the default is the wrong one |
| 13 | A non-shared `CampaignBudget` **derives its name from the attached campaign**, so this route sends no budget `name` | **CITED** | [v25 `campaign_budget.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_budget.proto), fetched 2026-09-09: "When creating a campaign budget through CampaignBudgetService, every explicitly shared campaign budget must have a non-null, non-empty name. Campaign budgets that are not explicitly shared derive their name from the attached campaign's name." Scope caveat: the *requirement* half of that comment is scoped to `CampaignBudgetService`, while this route creates through `GoogleAdsService.Mutate` `campaign_budget_operation`; the derivation sentence carries no such scope. Omitting the name matches the compiler contract's "the candidate does not invent a separate budget name" |
| 13a | With `explicitly_shared=false` the budget's **name and status stay in sync with the campaign's**, and `CampaignBudget.status` is output-only | **CITED** | same file, fetched 2026-09-09: "If false, the budget was created with the intention of only being used with a single campaign. The budget's name and status will stay in sync with the campaign's name and status", and `status` is "Output only. The status of this campaign budget. This field is read-only." A paused campaign therefore implies a paused-tracking budget. **Nothing in this route expects an independent budget status**: the semantic budget member's configuration is exactly `{delivery_method, explicitly_shared, period, total_amount_micros}`, the readback selects no `campaign_budget.status`, the mutate body sends no budget `status` or `name`, and the projection's paused check deliberately skips the budget member. `test_no_projection_or_readback_expects_an_independent_budget_status` pins all four |
| 14 | `Campaign.geo_target_type_setting.positive_geo_target_type` / `negative_geo_target_type` exist and are readable | **CITED** | [v25 `campaign.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto), fetched 2026-09-09 |
| 15 | `Campaign.asset_automation_settings` is `repeated Campaign.AssetAutomationSetting` with `asset_automation_type` and `asset_automation_status` | **CITED** | same file, fetched 2026-09-09 (`repeated AssetAutomationSetting asset_automation_settings = 88;`) |
| 16 | There is **no** account-scoped asset-automation resource in v25; the account control is web-interface only | **CITED** | [Asset automation settings](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings), fetched 2026-09-09 ("This account-level setting can only be configured in the Google Ads web interface"). `google/ads/googleads/v25/resources/customer_asset_automation_setting.proto` returned HTTP 404 |
| 17 | A GAQL string literal is `(' Char* ')` or `(" Char* ")` and the published grammar defines **no escape rule** | **CITED** | [GAQL grammar](https://developers.google.com/google-ads/api/docs/query/grammar), fetched 2026-09-09. This is why an unquotable campaign name is refused rather than escaped |
| 17a | GAQL defines `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN`, `NOT IN`, `CONTAINS ANY/ALL/NONE`, `LIKE`, `NOT LIKE`, `REGEXP_MATCH`, `NOT REGEXP_MATCH`, `IS NULL`, `IS NOT NULL`, `DURING`, `BETWEEN` -- and **no case-insensitive operator** | **CITED** | [GAQL grammar](https://developers.google.com/google-ads/api/docs/query/grammar), fetched 2026-09-09. The same page documents `LIKE` literal escaping with square brackets rather than backslashes |
| 17b | Whether `campaign.name =` compares case-sensitively | `UNVERIFIED (not documented on any reachable page, 2026-09-09)` | the grammar page states no case-sensitivity rule at all. This is why the plan probes a **closed local near-variant set** with byte-exact literals instead of asking the provider for a case-insensitive match |
| 18 | `campaign_budget.*` fields are selectable `FROM campaign` through the implicit join | `UNVERIFIED (field reference 404 on 2026-09-09)` | `.../fields/v25/campaign` and `.../fields/v21/campaign` both returned HTTP 404 |
| 19 | `Campaign.bidding_strategy_type` reports `TARGET_SPEND` for a standard `target_spend` strategy | `UNVERIFIED (enum reference unreachable on 2026-09-09)` | the field is `Output only. The type of bidding strategy` in the cited v25 `campaign.proto`; the specific enum value for `target_spend` was not re-read |
| 20 | `CampaignCriterion` create accepts `campaign`, `status`, `negative`, and the `location`/`language`/`ad_schedule` oneof; `AdGroupCriterion` create accepts `ad_group`, `status`, `negative`, `keyword`; `AdGroupAd` create accepts `ad_group`, `status`, `ad.final_urls`, `ad.responsive_search_ad` | `UNVERIFIED (reference pages unreachable on 2026-09-09)` | the field names are the compiler contract's own reviewed v25 shape, which `providers/google_ads_compiler.py` already pins; only the *create-operation envelope* around them is unverified |
| 21 | `RESOURCE_NAME_ONLY` is the v25 default for `response_content_type` | `UNVERIFIED` | not relied on: the route sends the value explicitly so the acknowledgement shape is fixed by the request |
| 22 | `AdGroupCriterion.type = KEYWORD` is a valid GAQL filter predicate | `UNVERIFIED (field reference 404 on 2026-09-09)` | used only to narrow the keyword readback; a response containing a non-keyword criterion is refused by the parser rather than ignored |

### What the fixture suite does and does not prove

`tests/test_google_ads_create_execution.py` uses `httpx.MockTransport`, a fake
credential store, and a fake installed approval bridge. Real in that suite: the
P04 contract, the recompile-and-bind promotion, the three-slot protected
credential lifecycle, the S3 record, the S4 route, a real signed receipt from an
ephemeral test key, a real `SafetyKernel`, and a real local ledger. It proves
the local state machine, the exact body this route would send, and every
refusal. It proves **nothing** about Google Ads accepting that body, about
provider approval, resource-creation order, idempotency, live serving, or
policy eligibility.

## Build dependency: two additive ledger record kinds

An ordinary build of this repository does **not** register
`GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN` or
`GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION`. Both are additive
`ledger.py` hunks applied by the coordinator; see the P04 handoff for the exact
text. Until they land:

* `plan execution create --provider google_ads --operation CREATE` refuses with
  `GOOGLE_ADS_CREATE_RECORD_KIND_NOT_INSTALLED` before reading configuration or
  any credential, and
* the adapter factory refuses with
  `GOOGLE_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED` at admission, before
  any credential, header, or provider request.

Neither is a fallback. There is no path that creates a hierarchy and then fails
to retain the observation that proves what it created.

## Open work

*Closed on 2026-09-09:* the non-shared campaign budget name. The v25
`campaign_budget.proto` states that budgets which are not explicitly shared
derive their name from the attached campaign's name, so omitting it is the
documented behaviour rather than a risk; see facts 13 and 13a. The only
remaining reservation is the scope caveat recorded there, and it is not a
compiler dependency request.

1. Facts 9, 18, 20, 22: re-read the v25 REST and field references when they are
   reachable again, and convert the `UNVERIFIED` rows.
2. Fact 17b: whether `campaign.name =` is case-sensitive. Until it is answered,
   the bounded near-variant probe is the only rule this route applies, with the
   limits stated above.
3. An authorized live readback in a Google Ads test account, which is the only
   thing that can move the capability-matrix row past `FIXTURE_VERIFIED`.
4. Campaign-level asset automation and AI Max: this route records the observed
   settings but has no way to *request* them, so a candidate whose text is
   expanded by automation fails verification rather than being reconciled.
5. The same-kind result-ordering constraint above, for whichever P05 ticket
   first needs a per-sibling resource reference.

# Google Ads offline provider audit rules and non-writing recommendations

Implemented: 2026-09-09. `src/adhelm/audits/google_ads.py`, evaluated through the
provider-audit seam in [`audit-contract.md`](audit-contract.md) and
`src/adhelm/audits/__init__.py`, and reached from the CLI by
`adhelm audit <record-digest> --input <policy> --provider-rules`.

This is a **fixture-verified local subset**. It promotes neither the Google Ads
`AUDIT` nor the `RECOMMEND` operation group in
[`capability-matrix.md`](../capability-matrix.md). No rule here makes a
provider request, reads a credential, appends a ledger record, or compiles a
plan; every input is a record this product already retained.

## What the rule table reads

| Input | Source | What it proves |
| --- | --- | --- |
| One normalized report | The retained `PERFORMANCE_OBSERVATION` the caller selected by record digest | Daily campaign- or ad-grain impressions, clicks, the exact provider `cost_micros` string, and the rounded USD minor-unit amount for the declared window |
| One inventory read | The `RESOURCE_SNAPSHOT` records named by an `INVENTORY_READ_COMPLETED` marker | Customer, campaign, ad-group, and ad identity, parents, declared status, and the normalized data-quality statuses the Google Ads inventory branch appends |
| An optional earlier inventory read | The read before it, selected the same way | Out-of-band change between two retained observations |
| An optional local creative catalog | Outstanding `CREATIVE_DEPLOYMENT` declarations for the same provider account | A **local** claim that a creative version names a Google Ads ad; never a provider-verified association |

Read selection, the `INVENTORY_READ_COMPLETED` marker semantics, and the
`COMPLETE_BOUNDED_READ` / `BOUNDED_NON_EXHAUSTIVE` / `READ_BOUNDARY_UNVERIFIED`
completeness declarations are the shared ones documented in
`src/adhelm/saved_audit_evidence.py`; this module adds no selection rule. History
retained before this product recorded read completions is a **legacy** read: it
is selected, declared `READ_BOUNDARY_UNVERIFIED`, and its absences are
suppressed rather than reported at low confidence, because the same record set
can fabricate both removals and creations.

`evaluate` additionally binds the evidence to the audited account: a retained
inventory whose provider or `account_ref` differs from the report envelope's
`source_account_ref` is refused, so no finding can cite one customer's records
against another's resources.

## Honesty bounds

1. **No exhaustive analytics claim.** Every fixed `googleAds:search` read walks
   documented page tokens to exhaustion *and* requests
   `searchSettings.returnTotalResultsCount`. When the provider declares that
   total and the retained rows equal it, the read is the strongest completeness
   this build can show. That is still a property of the adapter, and
   `AnalyticsCompleteness` in the shared seam has no exhaustive member at all.
   Every analytics-derived finding is therefore `BOUNDED_NON_EXHAUSTIVE`,
   `DELIVERY` is never `COVERED`, and a retained report that does not carry the
   `COMPLETE` quality status produces the explicit
   `GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` finding. `COMPLETE` states the
   read boundary, not final data: a window ending on the customer-local current
   day also carries `PROCESSING_PENDING`, and the two coexist. An inventory read
   whose provider declared no total produces
   `GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN` and caps `STRUCTURE` and
   `DRIFT` at `PARTIAL`.
2. **Presence is not content.** Ad scheduling, responsive-search components,
   the final URL, and the ad policy summary are read as presence and enum facts
   only. `_redact_ad_creative` in the adapter replaces the headline,
   description, and final-URL collections with their **lengths** immediately
   after the response is parsed and before the privacy screen, so no headline,
   description, or destination value can enter a local record, a projection, a
   log, or an error message. Those dimensions are `PARTIAL` at best and are
   never reported as healthy.
3. **The Cloud project access level is unknown.** Google publishes Test,
   Explorer, Basic and Standard access levels for the project that owns the
   OAuth client (developer tokens were retired on 2026-09-09), and no reviewed
   v25 read returns the level in use. `GoogleAdsIdentity.cloud_project_access_level` is therefore always
   `NOT_OBSERVABLE`, the retained account snapshot carries
   `GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE`, and
   `GA_POLICY_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE` says so on every
   audit. No rule assumes a quota, an environment, or a serving capability from
   it. The one adjacent fact that *is* documented is `customer.test_account`,
   which the read retains and `GA_POLICY_TEST_ACCOUNT_DECLARED` reports: a test
   customer neither serves real ads nor bills, so nothing retained from it is
   evidence of live performance.
4. **An unread dimension is never healthy.** `PLACEMENT`, `TRACKING`, and
   `NATIVE_CONVERSION` are `NOT_READ`; `BROWSER_MEASUREMENT` is
   `NOT_APPLICABLE` because a local audit runs no browser. Each carries an
   explicit finding anchored to the account snapshot rather than silence.
5. **Absence is weak or no evidence.** An absence rule drops to `LOW`
   confidence for a caller-declared subset, and absence-based drift and the
   local-binding absence rule are suppressed entirely when a read boundary is
   unproven. The coverage rationale says when a rule was suppressed.
6. **A local state-shape change is not a change at Google.** This ticket added
   `normalized_state.budget_type` to a retained campaign and
   `normalized_state.objective` to a retained ad, which rewrites the
   `normalized_state_digest` of every campaign and ad on the first read after
   the upgrade. `_state_deltas` therefore compares only the fields **both**
   retained reads returned. A field on one side's key set and absent from the
   other is ambiguous — the snapshot writer drops a `None`, so a campaign
   losing its end date removes the key exactly as a connector release that
   stopped retaining it would — and it is reported as
   `GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED`, never as an out-of-band change and
   never as a `STATE_CHANGED` drift observation. Every Google Ads snapshot now
   also records `GOOGLE_ADS_STATE_SHAPE_VERSION`, so a future change to the
   retained state is explicit; see
   [the read contract](../research/google-ads-read-contract.md).
7. **Only customer-declared thresholds.** Two rules apply a number, and both
   read it from the customer's `AuditPolicy`:
   `GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM` uses `maximum_cpc` and only when
   the retained bid carries the same currency, and
   `GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM` uses `minimum_impressions`.
   With neither declared, neither fires. This audit ships no optimization
   default.

## Coverage map

Every one of the fourteen audit dimensions carries exactly one status.

This table is **derived**, not restated. `_coverage_map` in
`src/adhelm/audits/google_ads.py` imports `GOOGLE_ADS_READ_SURFACES` from the
adapter and caps every dimension by it: a dimension no implemented fixed read
backs is `NOT_READ` whatever records a caller supplies, so `PLACEMENT`,
`TRACKING`, and `NATIVE_CONVERSION` cannot become readable by accident, and the
ceilings below follow the adapter's own declaration of what it reads rather
than a second copy maintained here.

The status ladder is `COVERED` → `PARTIAL` → `NOT_READ`:

- `STRUCTURE` is `COVERED` only for a `COMPLETE_BOUNDED_READ` inventory whose
  retained account snapshot also records that every search declared its total
  result count. Any weaker retained inventory — a declared subset, an unproven
  read boundary, or an undeclared total — is `PARTIAL`; `NOT_READ` means no
  inventory at all.
- `DRIFT` is `COVERED` only when **both** retained reads are complete bounded
  reads and the later read's pagination was proven, `PARTIAL` when an earlier
  read was supplied but either side is weaker, and `NOT_READ` when there is no
  earlier read.
- Every other dimension has `PARTIAL` as its ceiling and degrades to `NOT_READ`
  when the corresponding evidence is absent.

The statuses below are the maximum each dimension can reach.

| Dimension | Maximum status | Why |
| --- | --- | --- |
| `STRUCTURE` | `COVERED` | Customer, campaign, ad-group, and ad identity, parents, and declared status come from one whole bounded read whose total the provider declared and whose account snapshot recorded it |
| `DELIVERY` | `PARTIAL` | Impressions and clicks are read, but no retained report may be published as exhaustive |
| `SPEND` | `PARTIAL` | Retained cost rows, the exact-cent campaign budget, its period and sharing, and the campaign dates are read; account-level pacing is not |
| `BIDS` | `PARTIAL` | The campaign bidding strategy type and the exact-cent ad-group CPC bid are read; portfolio strategies, targets, ceilings, pacing, and bid modifiers are not |
| `OBJECTIVE` | `PARTIAL` | The advertising channel type is the documented intent this read returns; conversion goals are not read |
| `TARGETING` | `PARTIAL` | Only network settings, the geo target type setting, and whether an `AD_SCHEDULE` criterion exists |
| `PLACEMENT` | `NOT_READ` | No placement view, criterion, or report facet is requested by the fixed reads |
| `CREATIVE` | `PARTIAL` | Ad identity, type, and responsive-search component counts; never copy text, pinning, or assets |
| `DESTINATION_CONFIGURATION` | `PARTIAL` | Only whether an ad declared a final URL; no URL, display path, suffix, or parameter value |
| `TRACKING` | `NOT_READ` | Conversion actions, Google tag status, tracking templates, and URL parameters are not requested |
| `NATIVE_CONVERSION` | `NOT_READ` | The fixed report request projects impressions, clicks, and cost micros only |
| `BROWSER_MEASUREMENT` | `NOT_APPLICABLE` | A local audit executes no browser and loads no page |
| `POLICY` | `PARTIAL` | The ad policy summary approval and review statuses and the customer test-account flag are read; policy topics, customer status, and the Cloud project access level are not |
| `DRIFT` | `COVERED` | Only when both retained reads are whole bounded reads for the same account |

## Rule table

`RULE_TABLE_VERSION` is `google_ads.rules.2026-09-09.1`. It is part of every
finding's content identity, so an audit produced by a different table can never
be mistaken for one produced by this one. There are 56 rules.

### Structure

| Rule | Severity | Fires when |
| --- | --- | --- |
| `GA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP` | `MEDIUM` | A retained campaign has no retained ad group in the same read |
| `GA_STRUCTURE_AD_GROUP_WITHOUT_AD` | `MEDIUM` | A retained ad group has no retained ad in the same read |
| `GA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED` | `MEDIUM` | A group names a campaign absent from the same read |
| `GA_STRUCTURE_AD_PARENT_NOT_RETAINED` | `MEDIUM` | An ad names a parent absent from the same read |
| `GA_STRUCTURE_SERVING_INTENT_MISMATCH` | `MEDIUM` | A campaign or ad-group child declares `ENABLED` while its retained campaign or ad-group parent does not |
| `GA_STRUCTURE_STATUS_NOT_REVIEWED` | `INFO` | A status is outside the reviewed `ENABLED`/`PAUSED`/`REMOVED` enumeration |
| `GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN` | `MEDIUM` | The retained read records no declared total result count for every search — a search returned none, or the read retained no account snapshot to record it |

The serving-intent comparison deliberately ignores the account snapshot: its
effective status is the customer status this build does not read, so comparing
a campaign against it would manufacture a mismatch from a value that says
nothing about serving.

### Delivery and spend

| Rule | Severity | Fires when |
| --- | --- | --- |
| `GA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | `MEDIUM` (`LOW` confidence) | An `ENABLED` campaign has no impressions in the retained campaign-grain rows |
| `GA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS` | `MEDIUM` | A retained row declares more clicks than impressions |
| `GA_DELIVERY_METRICS_MISSING` | `INFO` | A retained row declares no impression or click count |
| `GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` | `MEDIUM` | The retained report envelope does not carry the `COMPLETE` quality status |
| `GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM` | `LOW` | The customer declared `minimum_impressions` and a campaign's retained rows fall below it |
| `GA_SPEND_WITHOUT_IMPRESSIONS` | `HIGH` | A retained row declares spend with zero impressions |
| `GA_SPEND_BUDGET_NOT_RETURNED` | `HIGH` | An `ENABLED` campaign carries no budget amount |
| `GA_SPEND_BUDGET_PERIOD_NOT_RETURNED` | `LOW` | A campaign returned no budget period |
| `GA_SPEND_BUDGET_EXPLICITLY_SHARED` | `MEDIUM` | A campaign draws on an explicitly shared budget, so the amount bounds a pool |
| `GA_SPEND_BUDGET_ACCELERATED_DELIVERY` | `MEDIUM` | An `ENABLED` campaign's budget uses accelerated delivery |
| `GA_SPEND_SCHEDULE_END_NOT_RETURNED` | `LOW` | An `ENABLED` campaign carries no end date |
| `GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS` | `INFO` | The retained report carries the `HALF_EVEN_TO_MINOR` rounding marker; reported once per report, not per row |

Spend comparisons read the exact retained value. `metrics.cost_micros`
routinely carries fractions of a cent, so `_spend_is_positive` compares the
retained native decimal string first and never lets a subminor cost round away
into an apparent zero. `GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS` restates the
read contract's own rule: consumers that need exact per-row cost sum the
retained micros and must not recompute a total from rounded row cents.

### Bids, objective, targeting, and placement

| Rule | Severity | Fires when |
| --- | --- | --- |
| `GA_BIDS_AD_GROUP_WITHOUT_CPC_BID` | `MEDIUM` | An ad group carries no CPC bid amount |
| `GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM` | `HIGH` | The customer declared `maximum_cpc` and a same-currency retained bid exceeds it |
| `GA_BIDS_AUTOMATED_STRATEGY_NOT_LOCALLY_BOUNDED` | `LOW` | A campaign uses a bidding strategy other than `MANUAL_CPC` |
| `GA_BIDS_STRATEGY_DETAIL_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `GA_OBJECTIVE_CHANNEL_OUTSIDE_COMPILABLE_SUBSET` | `LOW` | A campaign's advertising channel type is not `SEARCH` |
| `GA_OBJECTIVE_CONVERSION_GOAL_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `GA_TARGETING_AD_SCHEDULE_ABSENT` | `LOW` | An `ENABLED` campaign returned no `AD_SCHEDULE` criterion |
| `GA_TARGETING_CONTENT_NETWORK_ENABLED` | `MEDIUM` | An `ENABLED` campaign's network settings opt it into the Display content network |
| `GA_TARGETING_PARTNER_SEARCH_ENABLED` | `LOW` | An `ENABLED` campaign's network settings opt it into search partners |
| `GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED` | `LOW` | A campaign returned no network settings |
| `GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED` | `LOW` | A campaign returned no geo target type setting |
| `GA_TARGETING_CRITERION_VALUES_NOT_READ` | `INFO` | Always, once a campaign or ad group is retained |
| `GA_PLACEMENT_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

The three exposure rules — the ad-schedule absence and the two network
opt-ins — are guarded by `google_ads_declares_serving_intent`, like the budget
and end-date rules. A campaign that declares no intent to serve delivers
nowhere, so reporting where it could deliver would state a consequence the
retained status contradicts.

### Creative, destination, tracking, conversion, browser, and policy

| Rule | Severity | Fires when |
| --- | --- | --- |
| `GA_CREATIVE_RESPONSIVE_SEARCH_AD_NOT_RETURNED` | `LOW` | An ad returned no responsive-search-ad component set |
| `GA_CREATIVE_HEADLINES_BELOW_DOCUMENTED_MINIMUM` | `HIGH` | A responsive search ad declared fewer than three headlines |
| `GA_CREATIVE_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM` | `HIGH` | A responsive search ad declared fewer than two descriptions |
| `GA_CREATIVE_CONTENT_TEXT_NOT_READ` | `INFO` | Always, once an ad is retained |
| `GA_CREATIVE_LOCAL_BINDING_UNVERIFIED` | `LOW` | A local creative version declares a retained Google Ads ad |
| `GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` | `MEDIUM` | A local creative version declares an ad absent from a retained read with a proven boundary |
| `GA_DESTINATION_FINAL_URL_ABSENT` | `HIGH` | An ad returned no final URL |
| `GA_DESTINATION_VALUES_NOT_READ` | `INFO` | Always, once an ad is retained |
| `GA_TRACKING_CONVERSION_ACTION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `GA_TRACKING_TEMPLATE_AND_PARAMETERS_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `GA_NATIVE_CONVERSION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `GA_BROWSER_NOT_APPLICABLE` | `INFO` | Always, once an account snapshot is retained |
| `GA_POLICY_APPROVAL_RESTRICTED` | `HIGH` | An ad's approval status is `DISAPPROVED`, `APPROVED_LIMITED`, or `AREA_OF_INTEREST_ONLY` |
| `GA_POLICY_APPROVAL_STATUS_NOT_RETURNED` | `LOW` | An ad returned no policy approval status |
| `GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED` | `INFO` | An ad returned an approval status outside the reviewed enumeration |
| `GA_POLICY_REVIEW_STATUS_NOT_RETURNED` | `LOW` | An ad returned no policy review status |
| `GA_POLICY_REVIEW_STATUS_NOT_REVIEWED` | `INFO` | An ad returned a review status outside the reviewed enumeration |
| `GA_POLICY_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE` | `INFO` | Always, once an account snapshot is retained |
| `GA_POLICY_TEST_ACCOUNT_DECLARED` | `MEDIUM` | The retained customer declares `test_account` |
| `GA_POLICY_ACCOUNT_STATUS_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

The two component-count rules compare the retained bucket against Google's
documented minimum of three headlines and two descriptions. The exact counts
live on `GoogleAdsAd` for a caller holding the live read; what survives into
history is presence and the documented-minimum bucket, so no retained record
can be mistaken for creative capture. `creative_capture_status` stays
`CREATIVE_NOT_EXPOSED`.

An absent approval status is never an approval, and an unreviewed value is
surfaced rather than classified. The review status is read from the same policy
summary and is evaluated symmetrically against
`GOOGLE_ADS_REVIEWED_POLICY_REVIEW_STATUSES`, so a value this build has never
seen produces a finding instead of passing silently; `AD_POLICY_REVIEW_STATUS`
backs the `POLICY` dimension for the same reason.

The unread-surface disclosures — every rule ending `_NOT_READ` or
`_NOT_DISCLOSED` — describe what this build's fixed reads never request. That
is a property of the read contract, not of any one retained record, so they do
not depend on an account snapshot: a retained read that omits it still reports
all of them, anchored to the account the audited report names and cited to that
report's own record digest. Only the two rules that read an account-level
*value* need the snapshot that carries it, and the pagination rule fires when
that snapshot is missing as well, because an unrecorded total is not a proven
one — the `STRUCTURE` and `DRIFT` coverage rationale then says both that the
read is not proven whole and that no account snapshot recorded the total.

### Drift

| Rule | Severity | Fires when |
| --- | --- | --- |
| `GA_DRIFT_STATE_CHANGED` | `MEDIUM` | Two retained snapshots of one resource differ and this product applied no change |
| `GA_DRIFT_ABSENT_FROM_LATER_INVENTORY` | `MEDIUM` | A resource is in the earlier read and not the later one |
| `GA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY` | `LOW` | A resource is in the later read and not the earlier one |
| `GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED` | `INFO` | Two retained snapshots of one resource hold different normalized state **fields**, which is a local retention difference, not an observation of Google |

`GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED` names both retained connector
state-shape versions in its suggested action — `earlier <version>, later
<version>`, or `NOT_RECORDED` for a snapshot written before this product
recorded one — and says which reading they support: two different versions mean
this product changed what it retains, and one shared version means a value
became absent. Those versions are content, so they are part of the finding's
identity.

Every drift observation carries the interval between the two retained
observations, an `UNKNOWN` actor, and the changed field **paths** only. It
never invents an exact external change time, an actor, or a value. Both absence
rules are suppressed when either side is a legacy read with no
`INVENTORY_READ_COMPLETED` marker, and the coverage rationale states that the
suppression happened.

## Recommendations

Every recommendation is `NOT_EXECUTED`, `NOT_A_PLAN`, and
`NO_AUTHORIZATION_DIGEST_PRESENT`, references no plan, and carries no value
that could read as an authorization digest. The seam rejects any recommendation
whose operation kind this build cannot compile for Google Ads:
`COMPILABLE_OPERATION_KINDS["google_ads"]` is `{CREATE, PAUSE}`, plus the
always-available local `OBSERVE`. What the audit *emits* is a smaller set:
`OBSERVE` and `PAUSE` only. `CREATE` is what this build could compile, not
something this audit ever recommends — it appears solely as a declined
`considered_operation_kind` on two rows below.

| Operation | Bound rules | What it proposes |
| --- | --- | --- |
| `OBSERVE` | `GA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | Repeat the bounded campaign read over a compatible window |
| `OBSERVE` | `GA_SPEND_WITHOUT_IMPRESSIONS`, `GA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS`, `GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED`, `GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN` | Repeat the bounded read before any decision uses an inconsistent or unproven result |
| `OBSERVE` | the three drift rules | Read the bounded hierarchy again to bound the observation interval |
| `PAUSE` | `GA_SPEND_BUDGET_NOT_RETURNED`, `GA_TARGETING_CONTENT_NETWORK_ENABLED` | Names the existing `plan google-ads-campaign-pause create` route as the operation a marketer could ask for next |

The `PAUSE` row is a vocabulary statement. It names the route in prose, states
in its assumptions that the route resolves its own campaign identifier and
describes the pause as an offline plan, says that 0.1.0 executes no plan so the
pause is the marketer's manual step, and pauses nothing.

Its membership is exactly the rules the named route can act on, and about which
its hypothesis is true. The route pauses one named campaign, and the hypothesis
asserts that the campaign declares `ENABLED`, so every bound rule is
campaign-scoped **and** guarded by `google_ads_declares_serving_intent`. Both
are. `GA_SPEND_WITHOUT_IMPRESSIONS` is deliberately **not** bound here: it is a
report-row rule whose resource is whatever grain the retained row named — a
`google_ads.ad.*` reference for the AD-grain report — and there is no ad-level
pause route in this build, so the row would have named resources the route it
cites cannot accept. It keeps its `OBSERVE` row.

Everything else produces an explicit declined outcome rather than silence:

| Reason | Considered | Why it is declined |
| --- | --- | --- |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `UPDATE` | No Google Ads status, bid, budget, network, delivery-method, or ad-schedule update compiler exists |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `REPLACE` | No compiler adds a component, sets a destination, or appeals a policy decision on an existing ad |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` | The offline compiler produces only a complete new paused Search candidate, never a child of an existing parent |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` | A campaign outside the Search subset is neither changed nor recreated by this build |
| `EVIDENCE_INSUFFICIENT` | — | Missing, unreviewed, threshold-relative, or environment facts need a documentation review, a policy change, or a further read |
| `COVERAGE_NOT_READ` | — | An unread, presence-only, or inapplicable dimension yields no recommendation |

## Output bounds

One recommendation or declined row inlines at most
`MAXIMUM_INLINE_FINDING_IDS` (32) finding identifiers so that one output row
stays a constant size and the smallest page an agent can request always fits
the saved output bound. Nothing is dropped: each row states its complete
`bound_finding_count` and the `bound_rule_ids` that define its membership,
every finding remains an individual row in `findings`, and a capped row records
an explicit `BoundExceeded` entry resolved by `PAGE_FINDINGS_AND_MATCH_RULE_ID`.

A `BoundExceeded` entry identifies the exact row it qualifies. Its subject
therefore ends with the first rule identifier of the row's membership in
content order: two declined groups here share
`OPERATION_NOT_COMPILABLE_FOR_PROVIDER` with `CREATE`, so the reason and the
considered kind alone would produce two indistinguishable entries for two
different rows. `tests/test_google_ads_audit.py` pins that every emitted
subject is distinct.

## Evidence limits

- **Fixture-only.** `tests/test_google_ads_audit.py`,
  `tests/test_google_ads_audit_route.py`, and
  `tests/test_google_ads_read_completeness.py` are hermetic. No test performs a
  live Google Ads request.
- **No live Google Ads request has ever been run by this product**, for the
  audit or for the reads that produce its evidence.
- **No provider-UI reconciliation.** No finding has been compared against the
  Google Ads interface for an identical account, window, timezone, or currency.
- **The Cloud project access level is unproven.** Test, Explorer, Basic and
  Standard are documented, none is observable through a reviewed read, and no
  project has been exercised against a live customer. Quota behaviour, `RESOURCE_EXHAUSTED`
  semantics, and manager-route (`login-customer-id`) behaviour are classified
  from the HTTP status only and remain `UNVERIFIED`.
- **GAQL field compatibility is unverified.** The additive campaign settings,
  the `campaign_criterion` ad-schedule read, and the ad policy-summary and
  responsive-search projections are compiled from the published v25 field
  reference and exercised only against fixtures. No live response has confirmed
  that Google accepts these exact projections for a real customer.
- **This subset promotes nothing.** The Google Ads `AUDIT` and `RECOMMEND`
  operation groups remain `UNRESEARCHED` in the group-level matrix.

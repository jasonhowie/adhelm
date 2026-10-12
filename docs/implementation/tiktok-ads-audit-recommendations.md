# TikTok Ads offline provider audit and recommendations

Status: `FIXTURE_VERIFIED`. Rule table version `tiktok_ads.rules.2026-09-09.1`.
41 deterministic rules over retained local records. No live TikTok request was
made for this work.

`adhelm/audits/tiktok_ads.py` registers `evaluate` on the shared provider-rule
seam and is reachable through `audit --provider-rules` for a retained
`tiktok_ads` report. It makes no provider request, reads no credential, appends
no ledger record, and compiles no plan: every input is already-retained local
evidence.

## The five honesty bounds

1. **Nothing is exhaustive.** The reviewed TikTok reads walk numbered
   `page`/`page_size` pages and validate the declared `total_number` on every
   one, failing closed rather than returning a partial collection. That is a
   property of the adapter, not of a record set handed to the rule table, and
   `AnalyticsCompleteness` has no exhaustive member, so every analytics-derived
   finding stays `BOUNDED_NON_EXHAUSTIVE`. A retained account snapshot that does
   not declare `TIKTOK_PAGINATION_TOTAL_DECLARED` predates that proof and is
   reported by `TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED`.
2. **Unread is not healthy, and unproven field names are not requested.**
   TikTok's reference portal renders through JavaScript, and the
   TikTok-maintained SDK documents the `fields` parameter of `/campaign/get/`,
   `/adgroup/get/` and `/ad/get/` as a bare `list[str]` with no enumeration of
   returnable field names. Ad-group schedule, targeting, placement and
   optimization goal, ad creative association, video presence, ad copy,
   destination, report attribution and freshness, `secondary_status` and ad
   review state are therefore all declared `NOT_IN_REVIEWED_FIXED_READ` in
   `TIKTOK_ADS_READ_SURFACES` and reported as unread here. Every one of them
   reaches a reader, either through a rule's fixed prose or through the coverage
   entry of the dimension it backs in `_DIMENSION_SURFACES`; the ad group's
   optimization goal is mapped to `BIDS`, whose rationale says that no
   optimization goal is requested at all. Adding an unproven field name would be
   a fabricated capability that a fixture would silently accept.
3. **An enumeration this product cannot check is not an enumeration.** No
   fetchable first-party source publishes TikTok's `objective_type`, `bid_type`
   or advertiser `status` vocabularies. They are retained as presence facts only
   and get an explicit `NOT_DOCUMENTED_LOCALLY` disclosure. Only
   `operation_status` has a reviewed pair (`ENABLE`, `DISABLE`), and only the
   three reviewed budget modes are accepted by the read at all.
4. **`operation_status` does not fold a parent in.** Unlike Meta's
   `effective_status`, it is the value set on that exact object. The value that
   would propagate a parent, `secondary_status`, is not in the reviewed read, so
   an `ENABLE` child under a `DISABLE` parent is a genuine contradiction and gets
   `TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT`. The advertiser account is
   excluded from both sides of that comparison: its status is a different field
   with no reviewed vocabulary and must never be read as contradicting its own
   children. A campaign, ad group or ad whose `operation_status` is outside the
   reviewed pair is excluded from both sides for the same reason: it gets
   `TT_STRUCTURE_STATUS_NOT_REVIEWED`, and a value this table refuses to
   interpret may not then be read as "not serving" on the parent side either.
5. **No TikTok pause route exists in this build.**
   `COMPILABLE_OPERATION_KINDS["tiktok_ads"]` is `{"CREATE"}`, and that compiler
   produces one offline, explicitly non-executable paused candidate hierarchy —
   not a change to an existing resource. Every recommendation this table emits is
   therefore `OBSERVE`, and the pause a serving contradiction might otherwise
   suggest is an explicit declined row.

## Rule table

| Dimension | Rules | Rule identifiers |
| --- | --- | --- |
| STRUCTURE | 7 | `TT_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED`, `TT_STRUCTURE_AD_GROUP_WITHOUT_AD`, `TT_STRUCTURE_AD_PARENT_NOT_RETAINED`, `TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP`, `TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED`, `TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT`, `TT_STRUCTURE_STATUS_NOT_REVIEWED` |
| DELIVERY | 6 | `TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS`, `TT_DELIVERY_CLICKS_EXCEED_IMPRESSIONS`, `TT_DELIVERY_METRICS_MISSING`, `TT_DELIVERY_REPORT_ATTRIBUTION_NOT_READ`, `TT_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED`, `TT_DELIVERY_REPORT_FRESHNESS_NOT_READ` |
| SPEND | 6 | `TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET`, `TT_SPEND_BUDGET_AMOUNT_ABSENT`, `TT_SPEND_BUDGET_MODE_NOT_RETURNED`, `TT_SPEND_INFINITE_BUDGET_MODE`, `TT_SPEND_SCHEDULE_NOT_READ`, `TT_SPEND_WITHOUT_IMPRESSIONS` |
| BIDS | 3 | `TT_BIDS_PRICE_ABSENT`, `TT_BIDS_TYPE_ENUM_NOT_DOCUMENTED_LOCALLY`, `TT_BIDS_TYPE_NOT_RETURNED` |
| OBJECTIVE | 2 | `TT_OBJECTIVE_ENUM_NOT_DOCUMENTED_LOCALLY`, `TT_OBJECTIVE_NOT_RETURNED` |
| TARGETING | 1 | `TT_TARGETING_NOT_READ` |
| PLACEMENT | 1 | `TT_PLACEMENT_NOT_READ` |
| CREATIVE | 4 | `TT_CREATIVE_ASSOCIATION_NOT_READ`, `TT_CREATIVE_CONTENT_NOT_READ`, `TT_CREATIVE_LOCAL_BINDING_UNVERIFIED`, `TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` |
| DESTINATION_CONFIGURATION | 1 | `TT_DESTINATION_VALUES_NOT_READ` |
| TRACKING | 1 | `TT_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ` |
| NATIVE_CONVERSION | 1 | `TT_NATIVE_CONVERSION_NOT_READ` |
| BROWSER_MEASUREMENT | 1 | `TT_BROWSER_NOT_APPLICABLE` |
| POLICY | 3 | `TT_POLICY_ACCOUNT_STATUS_NOT_REVIEWED`, `TT_POLICY_AD_REVIEW_STATE_NOT_READ`, `TT_POLICY_APP_APPROVAL_AND_PERMISSIONS_NOT_DOCUMENTED` |
| DRIFT | 4 | `TT_DRIFT_ABSENT_FROM_EARLIER_INVENTORY`, `TT_DRIFT_ABSENT_FROM_LATER_INVENTORY`, `TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED`, `TT_DRIFT_STATE_CHANGED` |

Every rule identifier belongs to exactly one outcome group in
`RULE_OUTCOME_GROUPS`, so no finding can be produced that neither a
recommendation nor an explicit declined row accounts for.
`tests/test_tiktok_audit.py` pins the partition.

## Coverage map

Each dimension's status is the evidence supplied, **capped** by
`TIKTOK_ADS_READ_SURFACES` through `_DIMENSION_SURFACES`. A dimension no
implemented fixed read backs is `NOT_READ` whatever a caller supplies, and
`tests/test_tiktok_audit.py` proves the ceiling really is read from the surface
table by flipping one surface and watching only that dimension move.

| Dimension | Ceiling | Why |
| --- | --- | --- |
| STRUCTURE | `COVERED` with a whole bounded read | identity, parents and `operation_status` are read |
| DELIVERY | `PARTIAL` | never `COVERED`: no retained report may be published as the whole window |
| SPEND | `PARTIAL` | budget mode and exact decimal budget are read; no schedule field is requested |
| BIDS | `PARTIAL` | bid type and bid price are read; neither is checked against an enumeration, and no optimization goal is requested |
| OBJECTIVE | `PARTIAL` | presence only |
| TARGETING | `NOT_READ` | no targeting field is requested |
| PLACEMENT | `NOT_READ` | no placement field or facet is requested |
| CREATIVE | `PARTIAL` | backed by `AD_INVENTORY` alone: a retained ad snapshot proves only that a locally bound resource was returned by a bounded read |
| DESTINATION_CONFIGURATION | `NOT_READ` | no landing-page or tracking-parameter field is requested |
| TRACKING | `NOT_READ` | no pixel, event or Events API field is requested |
| NATIVE_CONVERSION | `NOT_READ` | the fixed report projects impressions, clicks and spend only |
| BROWSER_MEASUREMENT | `NOT_APPLICABLE` | a local audit executes no browser |
| POLICY | `PARTIAL` | the advertiser status is read but has no reviewed vocabulary; review state and app approval are not read at all |
| DRIFT | `COVERED` only when both snapshot sets are whole bounded reads | a declared subset or an unproven boundary can differ for unrelated reasons |

An unread or inapplicable dimension cites no evidence at all; a covered one names
its exact evidence sources and record count.

## Drift and marker semantics

Drift compares two retained snapshot sets for one advertiser.

* A per-resource value comparison is valid whatever the read boundary and always
  runs.
* Absence rules (`ABSENT_FROM_LATER`, `ABSENT_FROM_EARLIER`) run only when both
  sides have a proven read boundary. With `READ_BOUNDARY_UNVERIFIED` on either
  side they are suppressed, and the DRIFT coverage rationale says so out loud
  rather than leaving a quieter result to look like a cleaner one. With a
  declared subset they drop to `LOW` confidence.
* A **key-set** difference between two snapshots is never published as an
  out-of-band change at TikTok. `_state_deltas` compares only the fields both
  reads returned; the key-set difference becomes
  `TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED`, whose suggested action names both
  retained `TIKTOK_ADS_STATE_SHAPE_*` versions (or `NOT_RECORDED` for a snapshot
  written before the marker existed). Asserting a change at TikTok inside an
  interval this product never observed is what AGENTS.md rule 24 forbids.
* Every `DriftObservation` carries the interval between the two retained
  observations and an `UNKNOWN` actor, and reports only changed field paths,
  never their values.

The state-shape version lives in `data_quality_statuses`, never in
`normalized_state`, so recording it creates no drift of its own. Bump
`TIKTOK_ADS_STATE_SHAPE_VERSION` in the same commit as any change to what
`_tiktok_snapshots` retains.

## Recommendations: `OBSERVE` only

| Group | Kind | What it proposes |
| --- | --- | --- |
| delivery absence | `OBSERVE` | re-read the bounded campaign report; an absent row is not evidence of zero delivery |
| inconsistent rows | `OBSERVE` | re-read the same declared window |
| drift | `OBSERVE` | a fresh bounded inventory read to bound the observation interval |
| unbounded spend | `OBSERVE` | a fresh bounded hierarchy read to establish where, if anywhere, spend is bounded |
| unproven read boundary | `OBSERVE` | re-read with the current connector so the declared total is proved |

The **unbounded spend** group is where the Meta and Google tables propose a
compiled `PAUSE`. TikTok has no pause route in this build at all, so it yields a
re-read and nothing else.

| Declined group | Reason | Considered |
| --- | --- | --- |
| serving contradiction | `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `PAUSE` |
| childless campaign / ad group | `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` |
| budget mode / bid reconciliation | `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `UPDATE` |
| missing or unverifiable evidence | `EVIDENCE_INSUFFICIENT` | — |
| unread or undocumented dimensions | `COVERAGE_NOT_READ` | — |

Every recommendation carries `execution_status="NOT_EXECUTED"`,
`plan_status="NOT_A_PLAN"`, `authorization_status="NO_AUTHORIZATION_DIGEST_PRESENT"`,
no `proposed_plan_ref`, and no digest-like run anywhere in its prose. Expiry comes
from the user-declared `AuditPolicy.maximum_observation_age_seconds`; no threshold
in this table is invented.

## Bounds

A recommendation or declined row inlines at most
`MAXIMUM_INLINE_FINDING_IDS` (32) finding identifiers. Above that it records a
`BoundExceeded` entry naming its scope, a distinct subject, the bound, the
complete total, the omitted count, and the exact `rule_ids` that define
membership, resolved by `PAGE_FINDINGS_AND_MATCH_RULE_ID`. Nothing is dropped:
every finding stays an individual row. `BoundExceeded` subjects are distinct
because the outcome groups are disjoint and the subject names the first rule
identifier in content order. `tests/test_tiktok_audit.py` covers a run producing
more than 600 individually addressable findings.

## Account binding

`run_inventory` appends TikTok records keyed by `profile.account_ref`, the bare
decimal advertiser ID, while a `TikTokAdsClient` report envelope carries the
canonical `tiktok_ads.advertiser.<id>` reference. `retained_account_key` reduces
both spellings to the same key — one fixed prefix, nothing else, exactly like the
Meta mapping — so a report this build produced reaches its own retained
inventory, a genuinely different advertiser is still refused with
`INVALID_AUDIT_INPUT`, and a reference that reduces to nothing is refused rather
than joining every account.

## Evidence limits

Fixture-only, offline. No live TikTok request, no provider-UI reconciliation, no
sandbox proven. TikTok's objective, bid-type and advertiser-status vocabularies,
its rate-limit business codes, its app approval state and endpoint permissions,
and any returnable `fields` value beyond the reviewed projection are all
`NOT_DOCUMENTED_LOCALLY` because the reference portal requires JavaScript. This
promotes no TikTok Ads `AUTH`, `READ`, `AUDIT` or `RECOMMEND` operation group.

Tests: `tests/test_tiktok_audit.py`, `tests/test_tiktok_audit_route.py`,
`tests/test_tiktok_read_completeness.py`.

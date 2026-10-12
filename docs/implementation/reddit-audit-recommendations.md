# Reddit Ads offline provider audit rules and non-writing recommendations

Implemented: 2026-10-10. `src/adhelm/audits/reddit_ads.py`, evaluated through the
provider-audit seam in [`audit-contract.md`](audit-contract.md) and
`src/adhelm/audits/__init__.py`, and reached from the CLI by
`adhelm audit <record-digest> --input <policy> --provider-rules`.

This is a **fixture-verified local subset**. It promotes neither the Reddit Ads
`AUDIT` nor the `RECOMMEND` operation group in
[`capability-matrix.md`](../capability-matrix.md). No rule here makes a provider
request, reads a credential, appends a ledger record, or compiles a plan; every
input is a record this product already retained.

## What the rule table reads

| Input | Source | What it proves |
| --- | --- | --- |
| One normalized report | The retained `PERFORMANCE_OBSERVATION` the caller selected by record digest | Daily campaign- or ad-grain impressions, clicks, and exact USD spend (native micros plus a half-even minor-unit projection) for the declared UTC window |
| One inventory read | The `RESOURCE_SNAPSHOT` records named by an `INVENTORY_READ_COMPLETED` marker | Account, campaign, ad-group and ad identity, parents, declared status, and the campaign objective |
| An optional earlier inventory read | The read before it, selected the same way | Out-of-band change between two retained observations |
| An optional local creative catalog | Outstanding `CREATIVE_DEPLOYMENT` declarations for the same provider account | A **local** claim that a creative version names a Reddit ad; never a provider-verified association |

Read selection, the `INVENTORY_READ_COMPLETED` marker semantics, and the
`COMPLETE_BOUNDED_READ` / `BOUNDED_NON_EXHAUSTIVE` / `READ_BOUNDARY_UNVERIFIED`
completeness declarations are the shared ones documented in
`src/adhelm/saved_audit_evidence.py`; this module adds no selection rule.

## Account join

`run_inventory` retains Reddit snapshots under `profile.account_ref`, the bare
Reddit ad account ID, while a Reddit report envelope carries the canonical
`reddit_ads.account.<id>` reference. `retained_account_key` reduces both to the
same key by removing that one fixed prefix, exactly as it does for Meta, so a
report this build produced reaches its own retained inventory; a different
account still differs and is refused. `tests/test_reddit_audit_route.py` runs the
real read path end to end to prove it.

## Honesty bounds

1. **No exhaustive analytics claim.** The report is read as one provider
   response. Reddit's overview says reports paginate, but the reviewed material
   does not establish how a continuation of an initial `POST` is fetched, so the
   adapter fails closed with `REPORT_PAGINATION_UNVERIFIED` when a continuation is
   signalled and never follows a guessed request
   ([read contract](../research/reddit-ads-read-contract.md)). The adapter table
   records `REPORT_CONTINUATION: NOT_DOCUMENTED`. The `BOUNDED_NON_EXHAUSTIVE`
   label is a literal: the section carries it unconditionally and so does every
   report-derived rule, so a section and its findings cannot disagree. What does
   derive from the adapter table is the coverage cap: `_surface_backed` reports a
   dimension as `NOT_READ` when no fixed read this build implements backs it.
   `DELIVERY` and `SPEND` are never `COVERED`.
2. **An unread dimension is never healthy.** Bids, targeting, placement,
   destination, tracking, native conversion and policy are not retained by the
   fixed read, so their dimensions are `NOT_READ`; `BROWSER_MEASUREMENT` is
   `NOT_APPLICABLE` because a local audit runs no browser. Each carries an
   explicit finding anchored to the account snapshot rather than silence.
3. **Names are never retained.** The adapter tolerates a `name` field so a
   response does not fail, but no campaign, ad-group, ad or account name reaches
   a snapshot (`tests/test_reddit_read_completeness.py`).
4. **Absence is weak or no evidence.** An absence rule drops to `LOW` confidence
   for a caller-declared subset, and absence-based drift is suppressed entirely
   when a read boundary is unproven, because the same record set would otherwise
   fabricate both deletions and creations. The coverage rationale says when a
   rule was suppressed.
5. **A missing status is not a status.** When Reddit returned neither a
   configured nor an effective status the snapshot records
   `NOT_AVAILABLE_FROM_PROVIDER`. That is reported as `RD_STRUCTURE_STATUS_NOT_RETURNED`,
   is never read as an unreviewed value, and never makes a child look
   contradicted by its parent.

## Coverage map

Every one of the fourteen audit dimensions carries exactly one status.

This table is **derived**, not restated. `_coverage_map` in
`src/adhelm/audits/reddit_ads.py` caps every dimension by
`REDDIT_ADS_READ_SURFACES` in the adapter: a dimension no implemented fixed read
backs is `NOT_READ` whatever records a caller supplies. The statuses below are the
maximum each dimension can reach.

| Dimension | Maximum status | Why |
| --- | --- | --- |
| `STRUCTURE` | `COVERED` | Account, campaign, ad-group and ad identity, parents and declared status come from one whole bounded read; any other retained inventory is `PARTIAL` |
| `DELIVERY` | `PARTIAL` | Impressions and clicks come from one report response that is never labeled complete |
| `SPEND` | `PARTIAL` | Retained spend rows are read; budget, spend cap and schedule are not |
| `BIDS` | `NOT_READ` | Bid strategy and value are not retained |
| `OBJECTIVE` | `PARTIAL` | The objective is retained, but no reviewed objective enumeration exists to classify it |
| `TARGETING` | `NOT_READ` | Ad-group targeting is not retained |
| `PLACEMENT` | `NOT_READ` | No placement field or report facet is retained |
| `CREATIVE` | `PARTIAL` | Ad identity and hierarchy, plus local bindings checked against a retained inventory; never content |
| `DESTINATION_CONFIGURATION` | `NOT_READ` | Click URLs and tagging are not retained |
| `TRACKING` | `NOT_READ` | Pixel and conversion configuration are not retained |
| `NATIVE_CONVERSION` | `NOT_READ` | The fixed report requests impressions, clicks and spend only |
| `BROWSER_MEASUREMENT` | `NOT_APPLICABLE` | A local audit executes no browser and loads no page |
| `POLICY` | `NOT_READ` | Ad review state and the account status are not retained |
| `DRIFT` | `COVERED` | Only when both retained reads are whole bounded reads for the same account |

## Rule table

`RULE_TABLE_VERSION` is `reddit_ads.rules.2026-10-10`. It is part of every
finding's content identity, so an audit produced by a different table can never
be mistaken for one produced by this one. There are 28 rules.

### Structure

| Rule | Severity | Fires when |
| --- | --- | --- |
| `RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP` | `MEDIUM` | A retained campaign has no retained ad group in the same read |
| `RD_STRUCTURE_AD_GROUP_WITHOUT_AD` | `MEDIUM` | A retained ad group has no retained ad in the same read |
| `RD_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED` | `MEDIUM` | An ad group names a campaign absent from the same read |
| `RD_STRUCTURE_AD_PARENT_NOT_RETAINED` | `MEDIUM` | An ad names an ad group absent from the same read |
| `RD_STRUCTURE_AD_CAMPAIGN_MISMATCH` | `MEDIUM` | An ad's retained campaign is not the campaign of its retained ad group |
| `RD_STRUCTURE_SERVING_INTENT_MISMATCH` | `MEDIUM` | A resource declares `ACTIVE` while a retained parent with a returned status does not |
| `RD_STRUCTURE_STATUS_NOT_RETURNED` | `LOW` | A campaign, ad group or ad has no returned status |
| `RD_STRUCTURE_STATUS_NOT_REVIEWED` | `INFO` | A returned status is neither `ACTIVE` nor `PAUSED`, the two values this build reviewed |

### Delivery and spend

| Rule | Severity | Fires when |
| --- | --- | --- |
| `RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | `MEDIUM` (`LOW` confidence) | An `ACTIVE` campaign has no impressions in the retained campaign-grain rows |
| `RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS` | `MEDIUM` | A retained row declares more clicks than impressions |
| `RD_SPEND_WITHOUT_IMPRESSIONS` | `HIGH` | A retained row declares spend with zero impressions |
| `RD_SPEND_PACING_INPUTS_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

Spend comparisons use the exact retained native micros. A row whose half-even
cent projection is zero but whose micros are not is still spend; the minor-unit
amount is used only when a retained row carries no micros. An AD-grain report
carries native ad rows this build cannot join to a campaign, so campaign delivery
is evaluated from campaign rows only.

### Unread dimensions, objective, and creative

| Rule | Severity | Fires when |
| --- | --- | --- |
| `RD_BIDS_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `RD_OBJECTIVE_NOT_RETURNED` | `INFO` | A campaign snapshot carries no objective |
| `RD_OBJECTIVE_ENUM_NOT_REVIEWED` | `INFO` | A campaign carries an objective; Reddit has announced changes to the enumeration and none is pinned here |
| `RD_TARGETING_NOT_READ` | `INFO` | An ad group is retained |
| `RD_PLACEMENT_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `RD_CREATIVE_CONTENT_NOT_READ` | `INFO` | An ad is retained |
| `RD_CREATIVE_LOCAL_BINDING_UNVERIFIED` | `LOW` | A local creative version declares a retained Reddit ad |
| `RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` | `MEDIUM` | A local creative version declares an ad absent from the retained read |
| `RD_DESTINATION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `RD_TRACKING_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `RD_NATIVE_CONVERSION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `RD_BROWSER_NOT_APPLICABLE` | `INFO` | Always, once an account snapshot is retained |
| `RD_POLICY_REVIEW_STATE_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

A local binding is checked only against a retained inventory. With no inventory
there is nothing to check it against, so it yields no finding and is not
coverage; the audit never calls a binding absent from a read that did not happen.

### Drift

| Rule | Severity | Fires when |
| --- | --- | --- |
| `RD_DRIFT_STATE_CHANGED` | `MEDIUM` | Two retained snapshots of one resource differ and this product applied no change |
| `RD_DRIFT_ABSENT_FROM_LATER_INVENTORY` | `MEDIUM` | A resource is in the earlier read and not the later one |
| `RD_DRIFT_ABSENT_FROM_EARLIER_INVENTORY` | `LOW` | A resource is in the later read and not the earlier one |

Every drift observation carries the interval between the two retained
observations, an `UNKNOWN` actor, and the changed field **paths** only. It never
invents an exact external change time, an actor, or a value.

## Recommendations

Every recommendation is `NOT_EXECUTED`, `NOT_A_PLAN`, and
`NO_AUTHORIZATION_DIGEST_PRESENT`, references no plan, and carries no value that
could read as an authorization digest. The seam rejects any recommendation whose
operation kind this build cannot compile for Reddit Ads:
`COMPILABLE_OPERATION_KINDS["reddit_ads"]` is `{CREATE}`, plus the
always-available local `OBSERVE`. What the audit **emits** is `OBSERVE` only.
`CREATE` is what this build could compile, not something this audit recommends --
it appears solely as a declined `considered_operation_kind`.

| Operation | Bound rules | What it proposes |
| --- | --- | --- |
| `OBSERVE` | `RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | Repeat the bounded campaign read over a compatible window |
| `OBSERVE` | `RD_SPEND_WITHOUT_IMPRESSIONS`, `RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS` | Repeat the bounded read before any decision uses the row |
| `OBSERVE` | the three drift rules | Read the bounded hierarchy again to bound the observation interval |

Everything else produces an explicit declined outcome rather than silence:

| Reason | Considered | Why it is declined |
| --- | --- | --- |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `PAUSE` | No Reddit pause or status-update compiler exists |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `UPDATE` | Reconciling parent and child status would need a status update compiler |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` | The offline compiler produces only a complete new paused candidate, never a child of an existing parent |
| `EVIDENCE_INSUFFICIENT` | -- | Missing, unreviewed, inconsistent or unverifiable evidence needs a documentation review or a further read |
| `COVERAGE_NOT_READ` | -- | An unread or inapplicable dimension yields no recommendation |

## Output bounds

One recommendation or declined row inlines at most `MAXIMUM_INLINE_FINDING_IDS`
(32) finding identifiers so one output row stays a constant size. Nothing is
dropped: each row states its complete `bound_finding_count` and the
`bound_rule_ids` that define its membership, every finding remains an individual
row in `findings`, and a capped row records an explicit `BoundExceeded` entry
resolved by `PAGE_FINDINGS_AND_MATCH_RULE_ID`. The entry's subject ends with the
first rule identifier of the row's membership in content order, so two rows that
share a reason and a considered operation stay distinguishable.

## Evidence limits

- **Fixture-only.** `tests/test_reddit_audit.py`, `tests/test_reddit_audit_route.py`
  and `tests/test_reddit_read_completeness.py` are hermetic. No test performs a
  live Reddit request.
- **No live Reddit request has ever been run by this product**, for the audit or
  for the reads that produce its evidence.
- **No provider-UI reconciliation.** No finding has been compared with the Reddit
  Ads interface for an identical account, window, timezone or currency.
- **No sandbox exists.** Reddit documents no general test-account environment.
- **The reviewed status values are two.** `ACTIVE` and `PAUSED` are the only
  statuses this build reviewed; they were not re-fetched while this table was
  written, and any other value is surfaced as unreviewed rather than interpreted.
- **This subset promotes nothing.** The Reddit Ads `AUTH`, `READ`, `AUDIT` and
  `RECOMMEND` operation groups remain `UNRESEARCHED` in the group-level matrix.

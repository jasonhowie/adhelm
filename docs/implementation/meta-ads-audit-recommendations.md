# Meta Ads offline provider audit rules and non-writing recommendations

Implemented: 2026-09-09. `src/adhelm/audits/meta_ads.py`, evaluated through the
provider-audit seam in [`audit-contract.md`](audit-contract.md) and
`src/adhelm/audits/__init__.py`, and reached from the CLI by
`adhelm audit <record-digest> --input <policy> --provider-rules`.

This is a **fixture-verified local subset**. It promotes neither the Meta Ads
`AUDIT` nor the `RECOMMEND` operation group in
[`capability-matrix.md`](../capability-matrix.md). No rule here makes a
provider request, reads a credential, appends a ledger record, or compiles a
plan; every input is a record this product already retained.

## What the rule table reads

| Input | Source | What it proves |
| --- | --- | --- |
| One normalized report | The retained `PERFORMANCE_OBSERVATION` the caller selected by record digest | Daily campaign- or ad-grain impressions, clicks, reach, and exact native minor-unit spend for the declared window |
| One inventory read | The `RESOURCE_SNAPSHOT` records named by an `INVENTORY_READ_COMPLETED` marker | Account, campaign, ad-set, and ad identity, parents, the documented `effective_status`, and the normalized data-quality statuses the Meta inventory branch appends |
| An optional earlier inventory read | The read before it, selected the same way | Out-of-band change between two retained observations |
| An optional local creative catalog | Outstanding `CREATIVE_DEPLOYMENT` declarations for the same provider account | A **local** claim that a creative version names a Meta ad; never a provider-verified association |

Read selection, the `INVENTORY_READ_COMPLETED` marker semantics, and the
`COMPLETE_BOUNDED_READ` / `BOUNDED_NON_EXHAUSTIVE` / `READ_BOUNDARY_UNVERIFIED`
completeness declarations are the shared ones documented in
`src/adhelm/saved_audit_evidence.py`; this module adds no selection rule. A
retained snapshot set with **no** marker — history retained before this product
recorded read completions — is `READ_BOUNDARY_UNVERIFIED`, so its absences are
suppressed entirely rather than reported at low confidence.

## Honesty bounds

1. **No exhaustive analytics claim.** The reviewed inventory and insights reads
   both consume documented `paging.cursors.after` pagination and fail closed
   with `RESULT_LIMIT_REQUIRED` or `PAGINATION_LIMIT` rather than returning a
   partial page set. That is a property of the adapter, not of a record set
   handed to this module, and `AnalyticsCompleteness` in the shared seam has no
   exhaustive member at all. Every analytics-derived finding is therefore
   `BOUNDED_NON_EXHAUSTIVE`, `DELIVERY` is never `COVERED`, and a retained
   report that does not itself carry the `COMPLETE` quality status produces the
   explicit `MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` finding.
2. **Presence is not content.** Ad-set targeting and ad creative are read as
   presence facts only: whether a targeting object was returned, and whether it
   declared a geography, an age bound, or an interest/flexible spec; whether an
   ad names a creative, and whether that creative returned an image hash or one
   of the two documented link fields. No country, region, radius, age value,
   interest name, audience identifier, image hash, destination URL, headline,
   or body copy enters a local record. `object_story_spec`, `title`, `body`,
   and `asset_feed_spec` are **never requested**, so no ad copy reaches this
   process at all. Those dimensions are `PARTIAL` at best and are never
   reported as healthy.
3. **An unread dimension is never healthy.** `PLACEMENT`, `TRACKING`, and
   `NATIVE_CONVERSION` are `NOT_READ`; `BROWSER_MEASUREMENT` is
   `NOT_APPLICABLE` because a local audit runs no browser. Each carries an
   explicit finding rather than silence. Those findings describe the **read
   contract**, not any one retained record, so they do not vanish with the
   account snapshot: an audit with no retained inventory at all still reports
   every one of them, anchored to the account the audited report names and
   cited to that report's own record digest.
4. **Absence is weak or no evidence.** An absence rule drops to `LOW`
   confidence for a caller-declared subset, and absence-based drift is
   suppressed entirely when a read boundary is unproven, because the same
   record set would otherwise fabricate both deletions and creations. The
   coverage rationale says when a rule was suppressed.
5. **A withheld value is not an absent one.** The creative's image hash and
   destination link are screened by this product's local privacy rule one
   resource at a time, not by the whole-read screen. A landing page whose query
   string carries a contact address is ordinary advertiser content: the
   presence fact stands, the value is withheld exactly as it would have been
   anyway, and `MA_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN` records that it was
   withheld rather than absent. A match anywhere else — including the whole
   targeting object, where an uploaded audience really could carry a
   person-level value — still fails the read closed before any record exists.
6. **`effective_status` already folds parents in.** Meta documents a child's
   `effective_status` as reflecting its own status *or* a parent's, and
   publishes `CAMPAIGN_PAUSED` and `ADSET_PAUSED` for exactly that case. This
   table therefore has **no** parent/child serving-intent contradiction rule:
   the contradiction the OpenAI table reports cannot arise in a Meta read.
   `MA_STRUCTURE_PARENT_PAUSE_PROPAGATED` reports the documented propagation as
   an observation instead.

## Coverage map

Every one of the fourteen audit dimensions carries exactly one status.

This table is **derived**, not restated. `_coverage_map` in
`src/adhelm/audits/meta_ads.py` imports `META_ADS_READ_SURFACES` from the adapter
and caps every dimension by it: a dimension no implemented fixed read backs is
`NOT_READ` whatever records a caller supplies, so `PLACEMENT`, `TRACKING`, and
`NATIVE_CONVERSION` cannot become readable by accident, and the ceilings below
follow the adapter's own declaration of what it reads rather than a second copy
of it maintained here.

The status ladder is `COVERED` → `PARTIAL` → `NOT_READ`:

- `STRUCTURE` is `COVERED` only for a `COMPLETE_BOUNDED_READ` inventory. Any
  other retained inventory — a declared subset, or an unproven read boundary —
  is `PARTIAL`; `NOT_READ` means no inventory at all.
- `DRIFT` is `COVERED` only when **both** retained reads are complete bounded
  reads, `PARTIAL` when an earlier read was supplied but either side is weaker,
  and `NOT_READ` when there is no earlier read.
- Every other dimension has `PARTIAL` as its ceiling and degrades to `NOT_READ`
  when the corresponding evidence is absent.

| Dimension | Maximum status | Why |
| --- | --- | --- |
| `STRUCTURE` | `COVERED` | Account, campaign, ad-set, and ad identity, parents, and the documented `effective_status` come from one whole bounded read |
| `DELIVERY` | `PARTIAL` | Impressions, clicks, and reach are read, but no retained report may be published as exhaustive, and the fixed read returns no freshness timestamp |
| `SPEND` | `PARTIAL` | Retained spend rows, campaign and ad-set daily/lifetime budgets, the campaign spend cap, and both schedules; account-level pacing is not read |
| `BIDS` | `PARTIAL` | The campaign and ad-set bid strategy and the ad-set bid amount are retained; the billing event is read but no shared snapshot-state field retains it |
| `OBJECTIVE` | `PARTIAL` | The campaign objective and buying type against their documented enumerations; optimization goal and attribution setting are not read |
| `TARGETING` | `PARTIAL` | Only whether an ad set declared a targeting object, and whether it returned a geography, an age bound, or an interest/flexible spec |
| `PLACEMENT` | `NOT_READ` | The reviewed ad-set read requests no placement field and the fixed insights read requests no placement facet |
| `CREATIVE` | `PARTIAL` | Creative association and image-hash presence; never an image hash, headline, or body |
| `DESTINATION_CONFIGURATION` | `PARTIAL` | Only whether the creative returned one of the two requested link fields; no URL, query, or tagging value |
| `TRACKING` | `NOT_READ` | No pixel, dataset, custom conversion, or event-health field is requested |
| `NATIVE_CONVERSION` | `NOT_READ` | The fixed insights request projects impressions, clicks, spend, and reach only |
| `BROWSER_MEASUREMENT` | `NOT_APPLICABLE` | A local audit executes no browser and loads no page |
| `POLICY` | `PARTIAL` | Ad review state through the documented `effective_status` enumeration; the account's numeric status has no reviewed enumeration, and app review / Business Verification are not documented locally at all |
| `DRIFT` | `COVERED` | Only when both retained reads are whole bounded reads for the same account |

## Rule table

`RULE_TABLE_VERSION` is `meta_ads.rules.2026-09-09.1`. It is part of every
finding's content identity, so an audit produced by a different table can never
be mistaken for one produced by this one. There are **52** rules.

### Structure (6)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_STRUCTURE_CAMPAIGN_WITHOUT_AD_SET` | `MEDIUM` | A retained campaign has no retained ad set in the same read |
| `MA_STRUCTURE_AD_SET_WITHOUT_AD` | `MEDIUM` | A retained ad set has no retained ad in the same read |
| `MA_STRUCTURE_AD_SET_PARENT_NOT_RETAINED` | `MEDIUM` | An ad set names a campaign absent from the same read |
| `MA_STRUCTURE_AD_PARENT_NOT_RETAINED` | `MEDIUM` | An ad names a parent absent from the same read |
| `MA_STRUCTURE_STATUS_NOT_REVIEWED` | `INFO` | An effective status falls outside its node's documented enumeration |
| `MA_STRUCTURE_PARENT_PAUSE_PROPAGATED` | `INFO` | A retained child declares `CAMPAIGN_PAUSED` or `ADSET_PAUSED` |

The reviewed enumerations are per node and come from Meta's own reference
pages: campaigns publish `ACTIVE/PAUSED/DELETED/ARCHIVED/IN_PROCESS/WITH_ISSUES`,
ad sets add `CAMPAIGN_PAUSED`, and ads add `PENDING_REVIEW`, `DISAPPROVED`,
`PREAPPROVED`, `PENDING_BILLING_INFO`, and `ADSET_PAUSED`.

### Delivery (6)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | `MEDIUM` (`LOW` confidence) | An `ACTIVE` campaign has no impressions in the retained campaign-grain rows |
| `MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS` | `MEDIUM` | A retained row declares more clicks than impressions |
| `MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS` | `MEDIUM` | A retained row declares more reached people than impressions |
| `MA_DELIVERY_METRICS_MISSING` | `INFO` | A retained row declares no impression or click count |
| `MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` | `MEDIUM` | The retained report envelope does not carry the `COMPLETE` quality status |
| `MA_DELIVERY_REPORT_FRESHNESS_NOT_READ` | `INFO` | Always, once a report is audited: the fixed insights read returns no verified freshness timestamp |

### Spend (5)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_SPEND_WITHOUT_IMPRESSIONS` | `HIGH` | A retained row declares spend with zero impressions |
| `MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET` | `HIGH` | An `ACTIVE` campaign and **all** its retained ad sets carry no daily budget, lifetime budget, or spend cap |
| `MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END` | `LOW` | An `ACTIVE` campaign carries no retained stop time |
| `MA_SPEND_LIFETIME_BUDGET_WITHOUT_END_TIME` | `MEDIUM` | An ad set declares a lifetime budget and no end time, which Meta documents as a required pairing |
| `MA_SPEND_BOTH_BUDGET_FIELDS_RETAINED` | `LOW` | A resource returned both a daily and a lifetime budget and no reviewed rule says which governs |

Meta allows the budget to sit on the campaign (campaign budget optimization)
**or** on each ad set, so the unbounded-spend rule checks both levels before it
fires. Spend comparisons use the exact retained amount: the Meta read parses
`spend` as an exact decimal against a reviewed per-currency exponent, refuses
unsupported precision before a row exists, and accepts budget fields only as
integer minor units in the account's own currency. Nothing is rounded and
nothing is converted.

### Bids (4)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_BIDS_STRATEGY_NOT_RETURNED` | `MEDIUM` | A retained ad set returned no bid strategy |
| `MA_BIDS_STRATEGY_NOT_REVIEWED` | `INFO` | A retained bid strategy falls outside the documented enumeration |
| `MA_BIDS_CAPPED_STRATEGY_WITHOUT_BID_AMOUNT` | `MEDIUM` | An ad set declares a documented capped strategy and carries no bid amount |
| `MA_BIDS_BILLING_EVENT_NOT_RETAINED` | `INFO` | Always, once an ad set is retained |

`MA_BIDS_STRATEGY_NOT_RETURNED` is deliberately **ad-set scoped only**. Meta
documents the campaign `bid_strategy` as applying when campaign budget
optimization is enabled, so a campaign that legitimately has none would
otherwise be reported as a gap it does not have.

`MA_BIDS_BILLING_EVENT_NOT_RETAINED` records a real seam: the fixed ad-set read
requests `billing_event`, and the shared closed `NormalizedResourceState` in
`src/adhelm/ledger.py` has no field for it, so it never reaches local history and
this audit never sees it. Retaining it is a shared snapshot-state change
outside this ticket.

### Objective (4)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_OBJECTIVE_NOT_RETURNED` | `INFO` | A campaign returned no objective |
| `MA_OBJECTIVE_NOT_REVIEWED` | `INFO` | A campaign objective falls outside the documented enumeration |
| `MA_OBJECTIVE_BUYING_TYPE_NOT_RETURNED` | `INFO` | A campaign returned no buying type |
| `MA_OBJECTIVE_RESERVED_BUYING_TYPE_UNSUPPORTED` | `LOW` | A campaign declares the documented `RESERVED` buying type |

### Targeting and placement (6)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_TARGETING_NOT_DECLARED` | `MEDIUM` | An `ACTIVE` ad set returned no targeting object at all |
| `MA_TARGETING_GEO_ABSENT` | `MEDIUM` | An `ACTIVE` ad set's targeting object declared no geography, which Meta documents as required |
| `MA_TARGETING_AGE_ABSENT` | `LOW` | A returned targeting object declared neither `age_min` nor `age_max` |
| `MA_TARGETING_INTEREST_ABSENT` | `LOW` | A returned targeting object declared no `interests` and no `flexible_spec` |
| `MA_TARGETING_VALUES_NOT_READ` | `INFO` | Always, once an ad set is retained |
| `MA_PLACEMENT_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

The two targeting rules that describe a live delivery exposure are guarded by
the documented serving intent, exactly as the campaign spend rules are: an ad
set whose retained `effective_status` says it is not serving is not delivering
anywhere, so reporting it as an exposure would state a delivery consequence the
retained status contradicts.

### Creative, destination, tracking, conversion, browser (12)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_CREATIVE_NOT_ASSOCIATED` | `HIGH` | A retained ad returned no creative identifier |
| `MA_CREATIVE_IMAGE_HASH_ABSENT` | `MEDIUM` | A named creative returned no image hash |
| `MA_CREATIVE_CONTENT_TEXT_NOT_READ` | `INFO` | Always, once an ad is retained |
| `MA_CREATIVE_IMAGE_BYTES_NOT_PROVABLE` | `INFO` | Always, once an ad is retained |
| `MA_CREATIVE_LOCAL_BINDING_UNVERIFIED` | `LOW` | A local creative version declares a retained Meta ad |
| `MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` | `MEDIUM` | A local creative version declares an ad absent from the retained read |
| `MA_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN` | `INFO` | A returned image hash or destination link matched this product's local privacy screen |
| `MA_DESTINATION_LINK_ABSENT` | `HIGH` | A named creative returned neither `link_url` nor `object_url` |
| `MA_DESTINATION_VALUES_NOT_READ` | `INFO` | Always, once an ad is retained |
| `MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `MA_NATIVE_CONVERSION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `MA_BROWSER_NOT_APPLICABLE` | `INFO` | Always, once an account snapshot is retained |

`MA_CREATIVE_IMAGE_HASH_ABSENT` says what it can prove and no more: a video,
carousel, or dynamic creative legitimately returns no `image_hash`, and this
audit does not guess which kind it is looking at.
`MA_DESTINATION_LINK_ABSENT` is bounded the same way: a creative whose link
lives only inside the deliberately unrequested `object_story_spec` returns
nothing to this read.

### Policy (5)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_POLICY_AD_REVIEW_BLOCKED` | `HIGH` | A retained ad declares `DISAPPROVED` |
| `MA_POLICY_AD_REVIEW_PENDING` | `LOW` | A retained ad declares `PENDING_REVIEW` or `PREAPPROVED` |
| `MA_POLICY_AD_NEEDS_ATTENTION` | `MEDIUM` | A retained ad declares `WITH_ISSUES` or `PENDING_BILLING_INFO` |
| `MA_POLICY_ACCOUNT_STATUS_NOT_REVIEWED` | `INFO` | Always, once an account snapshot is retained |
| `MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

Ad review state is derived from the documented `effective_status` enumeration.
`META_AD_REVIEW_NOT_FLAGGED` means the retained status names no review or
billing problem; it is never a claim that Meta approved the ad, and no review
feedback field is ever requested.

`MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ` is the audit-side statement of
the same boundary `auth meta-token status` reports as
`app_review_status: NOT_DOCUMENTED_LOCALLY`: nothing in this build reads Meta's
app review outcome, access tier (Standard versus Advanced), or Business
Verification state, so no local record shows whether the configured app may
read another party's ad account.

### Drift (4)

| Rule | Severity | Fires when |
| --- | --- | --- |
| `MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED` | `INFO` | Two retained snapshots of one resource hold different normalized state **fields** |
| `MA_DRIFT_STATE_CHANGED` | `MEDIUM` | Two retained snapshots of one resource differ in a field **both** reads returned, and this product applied no change |
| `MA_DRIFT_ABSENT_FROM_LATER_INVENTORY` | `MEDIUM` | A resource is in the earlier read and not the later one |
| `MA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY` | `LOW` | A resource is in the later read and not the earlier one |

Every drift observation carries the interval between the two retained
observations, an `UNKNOWN` actor, and the changed field **paths** only. It
never invents an exact external change time, an actor, or a value.

Only fields **both** retained reads returned are compared as values. A field on
one side's key set and not the other is `MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED`
and produces **no** drift observation at all, because two indistinguishable
things produce that absence: a connector release that began or stopped
retaining the field — this change began retaining
`normalized_state.bid_strategy` on every Meta campaign — and a value that
became absent, since the snapshot writer drops a `None`. Publishing either as a
change at Meta, inside a recorded observation interval, would be a fabricated
remote observation and a violation of AGENTS.md rule 24. Every Meta snapshot
records `META_ADS_STATE_SHAPE_VERSION` beside its state, never inside it, so a
reader can tell the two apart; see
[the read contract](../research/meta-ads-read-contract.md).

`MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED` names both retained versions in its
suggested action — `earlier <version>, later <version>`, or `NOT_RECORDED` for
a snapshot written before this product recorded one — and says which reading
they support: two different versions mean this product changed what it retains,
and one shared version means a value became absent. Those versions are content,
so they are part of the finding's identity: a finding that names different
versions is a different finding.

## Recommendations

Every recommendation is `NOT_EXECUTED`, `NOT_A_PLAN`, and
`NO_AUTHORIZATION_DIGEST_PRESENT`, references no plan, and carries no value
that could read as an authorization digest. The seam rejects any recommendation
whose operation kind this build cannot compile for Meta Ads:
`COMPILABLE_OPERATION_KINDS["meta_ads"]` is `{CREATE, PAUSE}`, plus the
always-available local `OBSERVE`. What the audit *emits* is a smaller set:
`OBSERVE` and `PAUSE` only. `CREATE` is what this build could compile, not
something this audit ever recommends — it appears solely as a declined
`considered_operation_kind` on two rows below.

| Operation | Bound rules | What it proposes |
| --- | --- | --- |
| `OBSERVE` | `MA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | Repeat the bounded campaign insights read over a compatible window |
| `OBSERVE` | `MA_SPEND_WITHOUT_IMPRESSIONS`, `MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS`, `MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS`, `MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` | Repeat the bounded read before any decision uses the row |
| `OBSERVE` | the three drift rules | Read the bounded hierarchy again to bound the observation interval |
| `PAUSE` | `MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET`, `MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END` | Names the existing `plan meta-campaign-pause create` route as the operation a marketer could ask for next |

The `PAUSE` row is a vocabulary statement. It names the route in prose, states
in its assumptions that the route describes the pause as an offline plan, says
that 0.1.0 executes no plan so the pause is the marketer's manual step, and
pauses nothing. Its `spend_impact_minor` is null: this build
cannot bound the avoided spend of a campaign whose retained read returned no
budget at all.

Its membership is exactly the rules the named route can act on, and about which
its hypothesis is true. `plan meta-campaign-pause create` acts on one campaign,
and the hypothesis asserts that the named campaign declares the documented
`ACTIVE` effective status, so every bound rule is campaign-scoped **and**
guarded by `meta_ads_declares_serving_intent`. Both are.

`MA_SPEND_WITHOUT_IMPRESSIONS` is deliberately **not** bound here. It is a
report-row rule whose resource is whatever grain the retained row named — a
`meta_ads.ad.*` reference for the AD-grain insights read — and there is no
ad-level pause route in this build, so the row would have named resources the
route it cites cannot accept. It keeps its `OBSERVE` row.

`MA_POLICY_AD_REVIEW_BLOCKED` and `MA_POLICY_AD_NEEDS_ATTENTION` are declined
for the same reason from the other direction: they name one **ad**, the only
Meta pause route acts on a **campaign**, and pausing the whole campaign is not
what an ad-level review problem supports.

Everything else produces an explicit declined outcome rather than silence:

| Reason | Considered | Why it is declined |
| --- | --- | --- |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `UPDATE` | No Meta status, schedule, bid, or targeting update compiler exists |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `ASSOCIATE` | No creative, image, or destination association compiler exists |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` | The offline compiler produces only a complete new paused candidate, never a child of an existing parent |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` | The offline compiler produces auction campaigns only, so it proposes nothing for an existing reach-and-frequency reservation |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `PAUSE` | The findings name one ad; the only Meta pause route acts on a campaign |
| `EVIDENCE_INSUFFICIENT` | — | Missing, unreviewed, or unverifiable evidence needs a documentation review or a further read |
| `COVERAGE_NOT_READ` | — | An unread, presence-only, retained-state-limited, or inapplicable dimension yields no recommendation |

Every rule identifier in `RULES` belongs to **exactly one** of these groups.
`RULE_OUTCOME_GROUPS` is that partition, and `tests/test_meta_audit.py` pins
it, so no finding can be produced that neither a recommendation nor an explicit
declined outcome accounts for.

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
different rows. `tests/test_meta_audit.py` pins that every emitted subject is
distinct for an account large enough to exceed every bound at once.

## Evidence limits

- **Fixture-only.** `tests/test_meta_audit.py`, `tests/test_meta_audit_route.py`,
  `tests/test_meta_read_completeness.py`, and
  `tests/test_meta_token_status_disclosure.py` are hermetic. No test performs a
  live Meta request.
- **No live Meta request has ever been run by this product**, for the audit or
  for the reads that produce its evidence.
- **No provider-UI reconciliation.** No finding has been compared against Meta
  Ads Manager for an identical account, window, timezone, or currency.
- **App review and Business Verification are unproven.** Meta's Standard versus
  Advanced Access decision, app review outcome, and Business Verification state
  are not documented locally, are not read by any command, and are reported as
  `NOT_DOCUMENTED_LOCALLY` rather than assumed.
- **The Graph version pin is now corroborated, not proven in use.** The
  changelog fetched on 2026-09-09 lists `v26.0` as the latest version,
  introduced 2026-07-29, which matches the adapter's pin. No request has been
  sent to that version by this product.
- **This subset promotes nothing.** The Meta Ads `AUDIT` and `RECOMMEND`
  operation groups remain `UNRESEARCHED` in the group-level matrix.

## One account, two spellings

Retained Meta inventory records are keyed by the **native** ad-account
identifier, because `run_inventory` appends with `profile.account_ref` and a
`MetaAccountTokenProfile` declares that as the bare numeric ID. A
`MetaAdsClient` report envelope instead carries the canonical
`meta_ads.account.act_<id>` reference.

Both name one account, so both are reduced to the same retained key before they
are compared: `retained_account_key` in `src/adhelm/saved_audit_evidence.py`
strips the `meta_ads.account.` and `act_` prefixes for Meta and for no other
provider. A reference that reduces to nothing — `act_` alone does — is refused
as `INVALID_AUDIT_EVIDENCE_SELECTION` rather than returned as an empty key that
would compare equal to every other empty key.

One account can genuinely hold retained records under **both** spellings:
`run_inventory` keys by `profile.account_ref`, a `MetaAccountTokenProfile`
constrains that to the bare numeric identifier, and a generic `Profile` for
`meta_ads` accepts the `act_<id>` form. Selection therefore matches snapshots,
completion markers, and creative declarations on the derived key, so **every**
completed read for the account is a candidate:

- `retained_inventory_reads` counts them all, and the newest completed read is
  selected whichever spelling wrote it. Excluding one spelling would audit a
  stale hierarchy as current while under-counting the reads, and this module
  promises that nothing is silently dropped.
- `retained_account_spellings` in the provenance lists every retained spelling,
  so a reader can see that two were joined rather than one dropped.
- `account_ref` reports one retained spelling deterministically — the first in
  content order — so the same ledger yields the same evidence and the same
  provenance whichever spelling the caller typed.

`audit_provider` compares the same derived keys, so
`audit <digest> --provider-rules` on a report this build produced reaches the
inventory this build retained for it. The typed refusal is unchanged for a
genuinely different ad account, in either spelling, and two different accounts
still require an explicit `--account`. `tests/test_meta_audit_route.py` pins
the default route, the join, the determinism, and both refusals. The mapping is
exact and reversible for Meta only; no other provider is normalized, because
inventing a mapping elsewhere would risk joining two accounts that merely look
alike.

## Related read-side additions

The presence and enum facts this table reads are produced by the additive
read-side changes recorded in
[`meta-ads-read-contract.md`](../research/meta-ads-read-contract.md):
ad-set targeting presence, ad creative association and image-hash/link
presence, the campaign buying type and bid strategy, budget mode, schedule
presence, the ad review state, and the Graph rate-limit classification.

# OpenAI Ads offline provider audit rules and non-writing recommendations

Implemented: 2026-09-09. `src/adhelm/audits/openai_ads.py`, evaluated through the
provider-audit seam in [`audit-contract.md`](audit-contract.md) and
`src/adhelm/audits/__init__.py`, and reached from the CLI by
`adhelm audit <record-digest> --input <policy> --provider-rules`.

This is a **fixture-verified local subset**. It promotes neither the OpenAI Ads
`AUDIT` nor the `RECOMMEND` operation group in
[`capability-matrix.md`](../capability-matrix.md). No rule here makes a
provider request, reads a credential, appends a ledger record, or compiles a
plan; every input is a record this product already retained.

## What the rule table reads

| Input | Source | What it proves |
| --- | --- | --- |
| One normalized report | The retained `PERFORMANCE_OBSERVATION` the caller selected by record digest | Daily campaign or ad-grain impressions, clicks, and exact USD minor-unit spend for the declared window |
| One inventory read | The `RESOURCE_SNAPSHOT` records named by an `INVENTORY_READ_COMPLETED` marker | Account, campaign, ad-group, and ad identity, parents, declared status, and the normalized data-quality statuses the OpenAI inventory branch appends |
| An optional earlier inventory read | The read before it, selected the same way | Out-of-band change between two retained observations |
| An optional local creative catalog | Outstanding `CREATIVE_DEPLOYMENT` declarations for the same provider account | A **local** claim that a creative version names an OpenAI ad; never a provider-verified association |

Read selection, the `INVENTORY_READ_COMPLETED` marker semantics, and the
`COMPLETE_BOUNDED_READ` / `BOUNDED_NON_EXHAUSTIVE` / `READ_BOUNDARY_UNVERIFIED`
completeness declarations are the shared ones documented in
`src/adhelm/saved_audit_evidence.py`; this module adds no selection rule.

## Honesty bounds

1. **No exhaustive analytics claim.** The reviewed insights read does consume
   documented cursor pagination to completion and fails closed with
   `RESULT_LIMIT_REQUIRED` or `PAGINATION_LIMIT` rather than returning a
   partial page set. That is a property of the adapter, not of a record set
   handed to this module, and `AnalyticsCompleteness` in the shared seam has no
   exhaustive member at all. Every analytics-derived finding is therefore
   `BOUNDED_NON_EXHAUSTIVE`, `DELIVERY` is never `COVERED`, and a retained
   report that does not itself carry the `COMPLETE` quality status produces the
   explicit `OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` finding.
2. **Presence is not content.** Campaign targeting, campaign conversion-setting
   association, ad-group context hints, ad creative copy, and the ad creative
   destination are read as presence facts only. No targeting criterion, hint
   text, copy string, image URL, or landing page enters a local record, so
   those dimensions are `PARTIAL` at best and are never reported as healthy.
3. **An unread dimension is never healthy.** `PLACEMENT` and
   `NATIVE_CONVERSION` are `NOT_READ`; `BROWSER_MEASUREMENT` is
   `NOT_APPLICABLE` because a local audit runs no browser. Each carries an
   explicit finding anchored to the account snapshot rather than silence.
4. **Absence is weak or no evidence.** An absence rule drops to `LOW`
   confidence for a caller-declared subset, and absence-based drift is
   suppressed entirely when a read boundary is unproven, because the same
   record set would otherwise fabricate both deletions and creations. The
   coverage rationale says when a rule was suppressed.

## Coverage map

Every one of the fourteen audit dimensions carries exactly one status.

This table is **derived**, not restated. `_coverage_map` in
`src/adhelm/audits/openai_ads.py` imports `OPENAI_ADS_READ_SURFACES` from the
adapter and caps every dimension by it: a dimension no implemented fixed read
backs is `NOT_READ` whatever records a caller supplies, so `PLACEMENT` and
`NATIVE_CONVERSION` cannot become readable by accident, and the ceilings below
follow the adapter's own declaration of what it reads rather than a second copy
of it maintained here.

The status ladder is `COVERED` → `PARTIAL` → `NOT_READ`, and a dimension moves
down it as evidence weakens rather than dropping straight to `NOT_READ`:

- `STRUCTURE` is `COVERED` only for a `COMPLETE_BOUNDED_READ` inventory. Any
  other retained inventory — a declared subset, or an unproven read boundary —
  is `PARTIAL`; `NOT_READ` means no inventory at all.
- `DRIFT` is `COVERED` only when **both** retained reads are complete bounded
  reads, `PARTIAL` when an earlier read was supplied but either side is weaker,
  and `NOT_READ` when there is no earlier read.
- Every other dimension has `PARTIAL` as its ceiling and degrades to `NOT_READ`
  when the corresponding evidence is absent.

The statuses below are the maximum each dimension can reach.

| Dimension | Maximum status | Why |
| --- | --- | --- |
| `STRUCTURE` | `COVERED` | Account, campaign, ad-group, and ad identity, parents, and declared status come from one whole bounded read |
| `DELIVERY` | `PARTIAL` | Impressions and clicks are read, but no retained report may be published as exhaustive |
| `SPEND` | `PARTIAL` | Retained spend rows, the native lifetime spend limit, and the run schedule are read; account-level pacing is not |
| `BIDS` | `PARTIAL` | The campaign bidding type, the ad-group billing event, and the native maximum bid are read; bid strategy and pacing are not |
| `OBJECTIVE` | `PARTIAL` | The bidding type is the documented optimization intent; OpenAI documents no separate campaign objective field |
| `TARGETING` | `PARTIAL` | Only whether a campaign declared a non-empty targeting object, and how many context hints an ad group returned |
| `PLACEMENT` | `NOT_READ` | No placement configuration field or report facet is documented for the reviewed surfaces |
| `CREATIVE` | `PARTIAL` | Ad identity, chat-card type, the creative file association, and copy field presence; never copy text |
| `DESTINATION_CONFIGURATION` | `PARTIAL` | Only whether a creative declared a target URL; no URL, query, or tagging value |
| `TRACKING` | `PARTIAL` | Only the count of conversion event settings a campaign associates; pixel and event health are not read |
| `NATIVE_CONVERSION` | `NOT_READ` | The fixed insights request projects impressions, clicks, and spend only |
| `BROWSER_MEASUREMENT` | `NOT_APPLICABLE` | A local audit executes no browser and loads no page |
| `POLICY` | `PARTIAL` | The ad review status is read but has no reviewed enumeration; account review and serving state are not retained |
| `DRIFT` | `COVERED` | Only when both retained reads are whole bounded reads for the same account |

## Rule table

`RULE_TABLE_VERSION` is `openai_ads.rules.2026-09-09.2`. It is part of every
finding's content identity, so an audit produced by a different table can never
be mistaken for one produced by this one. There are 40 rules.

### Structure

| Rule | Severity | Fires when |
| --- | --- | --- |
| `OA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP` | `MEDIUM` | A retained campaign has no retained ad group in the same read |
| `OA_STRUCTURE_AD_GROUP_WITHOUT_AD` | `MEDIUM` | A retained ad group has no retained ad in the same read |
| `OA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED` | `MEDIUM` | A group names a campaign absent from the same read |
| `OA_STRUCTURE_AD_PARENT_NOT_RETAINED` | `MEDIUM` | An ad names a parent absent from the same read |
| `OA_STRUCTURE_SERVING_INTENT_MISMATCH` | `MEDIUM` | A child declares the documented `active` status while its retained parent does not |
| `OA_STRUCTURE_STATUS_NOT_REVIEWED` | `INFO` | A campaign status is outside the reviewed `active`/`paused` enumeration |

Only the campaign state enumeration is documented. Ad-group and ad reviewed
status sets are deliberately empty, so this audit makes no claim about whether
one of their status values is valid.

### Delivery and spend

| Rule | Severity | Fires when |
| --- | --- | --- |
| `OA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | `MEDIUM` (`LOW` confidence) | An `active` campaign has no impressions in the retained campaign-grain rows |
| `OA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS` | `MEDIUM` | A retained row declares more clicks than impressions |
| `OA_DELIVERY_METRICS_MISSING` | `INFO` | A retained row declares no impression or click count |
| `OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` | `MEDIUM` | The retained report envelope does not carry the `COMPLETE` quality status |
| `OA_SPEND_WITHOUT_IMPRESSIONS` | `HIGH` | A retained row declares spend with zero impressions |
| `OA_SPEND_LIMIT_NOT_RETURNED` | `HIGH` | An `active` campaign carries no native lifetime spend limit |
| `OA_SPEND_NATIVE_AMOUNT_NOT_PROJECTED` | `LOW` | A retained native micros amount had no exact USD minor-unit projection |
| `OA_SPEND_SCHEDULE_NOT_RETURNED` | `LOW` | An `active` campaign carries no retained end time |

Spend comparisons use the exact retained amount. The OpenAI read converts a
documented USD major-unit value to minor units exactly and refuses unsupported
precision before a row exists, and a subminor or unverified-currency native
micros amount is never rounded into a minor-unit total.

### Bids, objective, targeting, and placement

| Rule | Severity | Fires when |
| --- | --- | --- |
| `OA_BIDS_MAX_BID_NOT_RETURNED` | `MEDIUM` | An ad group carries no native maximum bid micros |
| `OA_BIDS_STRATEGY_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `OA_OBJECTIVE_BIDDING_TYPE_NOT_RETURNED` | `INFO` | A campaign snapshot carries no bidding type |
| `OA_OBJECTIVE_CONVERSION_BIDDING_UNSUPPORTED` | `LOW` | A campaign optimizes for conversions, which this build refuses to compile |
| `OA_TARGETING_NOT_DECLARED` | `MEDIUM` | An `active` campaign returned no non-empty targeting object |
| `OA_TARGETING_AD_GROUP_WITHOUT_CONTEXT_HINTS` | `LOW` | An ad group returned no context hints |
| `OA_TARGETING_CRITERIA_VALUES_NOT_READ` | `INFO` | Always, once a campaign or ad group is retained |
| `OA_PLACEMENT_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

### Creative, destination, tracking, conversion, browser, and policy

| Rule | Severity | Fires when |
| --- | --- | --- |
| `OA_CREATIVE_FILE_NOT_ASSOCIATED` | `HIGH` | A chat-card creative names no provider file |
| `OA_CREATIVE_COPY_INCOMPLETE` | `MEDIUM` | A chat-card creative returned no title or no body |
| `OA_CREATIVE_CONTENT_TEXT_NOT_READ` | `INFO` | Always, once an ad is retained |
| `OA_CREATIVE_FILE_BYTES_NOT_PROVABLE` | `INFO` | Always, once an ad is retained |
| `OA_CREATIVE_LOCAL_BINDING_UNVERIFIED` | `LOW` | A local creative version declares a retained OpenAI ad |
| `OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` | `MEDIUM` | A local creative version declares an ad absent from the retained read |
| `OA_DESTINATION_TARGET_URL_ABSENT` | `HIGH` | A chat-card creative returned no target URL |
| `OA_DESTINATION_VALUES_NOT_READ` | `INFO` | Always, once an ad is retained |
| `OA_TRACKING_NO_CONVERSION_SETTING_ASSOCIATED` | `MEDIUM` | A campaign returned no associated conversion event setting identifier |
| `OA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `OA_NATIVE_CONVERSION_NOT_READ` | `INFO` | Always, once an account snapshot is retained |
| `OA_BROWSER_NOT_APPLICABLE` | `INFO` | Always, once an account snapshot is retained |
| `OA_POLICY_AD_REVIEW_STATUS_NOT_RETURNED` | `LOW` | An ad returned no review status |
| `OA_POLICY_AD_REVIEW_ENUM_NOT_REVIEWED` | `INFO` | An ad returned a review status this build cannot classify |
| `OA_POLICY_ACCOUNT_SERVING_STATE_NOT_READ` | `INFO` | Always, once an account snapshot is retained |

`OA_CREATIVE_FILE_BYTES_NOT_PROVABLE` restates the boundary in
[the file read contract](../research/openai-file-read-contract.md): OpenAI Ads
documents no file metadata, listing, content, or hash read, so a retained file
association proves that an ad names a file, never that the provider holds a
particular local asset's bytes.

### Drift

| Rule | Severity | Fires when |
| --- | --- | --- |
| `OA_DRIFT_STATE_CHANGED` | `MEDIUM` | Two retained snapshots of one resource differ and this product applied no change |
| `OA_DRIFT_ABSENT_FROM_LATER_INVENTORY` | `MEDIUM` | A resource is in the earlier read and not the later one |
| `OA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY` | `LOW` | A resource is in the later read and not the earlier one |

Every drift observation carries the interval between the two retained
observations, an `UNKNOWN` actor, and the changed field **paths** only. It
never invents an exact external change time, an actor, or a value.

## Recommendations

Every recommendation is `NOT_EXECUTED`, `NOT_A_PLAN`, and
`NO_AUTHORIZATION_DIGEST_PRESENT`, references no plan, and carries no value
that could read as an authorization digest. The seam rejects any recommendation
whose operation kind this build cannot compile for OpenAI Ads:
`COMPILABLE_OPERATION_KINDS["openai_ads"]` is `{CREATE, PAUSE}`, plus the
always-available local `OBSERVE`. What the audit *emits* is a smaller set:
`OBSERVE` and `PAUSE` only. `CREATE` is what this build could compile, not
something this audit ever recommends — it appears solely as a declined
`considered_operation_kind` on two rows below.

| Operation | Bound rules | What it proposes |
| --- | --- | --- |
| `OBSERVE` | `OA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | Repeat the bounded campaign read over a compatible window |
| `OBSERVE` | `OA_SPEND_WITHOUT_IMPRESSIONS`, `OA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS`, `OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED` | Repeat the bounded read before any decision uses the row |
| `OBSERVE` | the three drift rules | Read the bounded hierarchy again to bound the observation interval |
| `PAUSE` | `OA_SPEND_LIMIT_NOT_RETURNED`, `OA_TARGETING_NOT_DECLARED` | Names the existing `plan openai-pause` route as the operation a marketer could ask for next |

The `PAUSE` row is a vocabulary statement. It names the route in prose, states
in its assumptions that the route describes the pause as an offline plan, says
that 0.1.0 executes no plan so the pause is the marketer's manual step, and
pauses nothing. Its `spend_impact_minor` is null: this build
cannot bound the avoided spend of a campaign whose retained read returned no
spend limit.

Its membership is exactly the rules the named route can act on, and about
which its hypothesis is true. `plan openai-pause` accepts a campaign identifier
only, and the hypothesis asserts that the named campaign declares the
documented `active` serving intent, so every bound rule has to be
campaign-scoped **and** guarded by `openai_ads_declares_serving_intent`.

`OA_TARGETING_NOT_DECLARED` is now guarded that way, like
`OA_SPEND_LIMIT_NOT_RETURNED` and `OA_SPEND_SCHEDULE_NOT_RETURNED` before it:
an undeclared targeting object decides only *where* a campaign delivers, and a
campaign that declares no intent to serve is not delivering anywhere, so
reporting it there would state a delivery consequence the retained status
contradicts. Before that guard the row could propose pausing a campaign already
declared `paused`, while telling the reading agent it was active.

`OA_SPEND_WITHOUT_IMPRESSIONS` is deliberately **not** bound here. It is a
report-row rule whose resource is whatever grain the retained row named — an
`openai_ads.ad.*` reference for the AD-grain insights read — and there is no
ad-level pause route in this build, so the row would have named resources the
route it cites cannot accept. It keeps its `OBSERVE` row.

Everything else produces an explicit declined outcome rather than silence:

| Reason | Considered | Why it is declined |
| --- | --- | --- |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `UPDATE` | No OpenAI status or bid update compiler exists |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `ASSOCIATE` | No creative, destination, or conversion association compiler exists |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | `CREATE` | The offline compiler produces only a complete new paused candidate, never a child of an existing parent, and refuses conversion bidding outright |
| `EVIDENCE_INSUFFICIENT` | — | Missing, unreviewed, or unverifiable evidence needs a documentation review or a further read |
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
different rows. The membership itself stays complete in the entry's own
`rule_ids`, which is what `PAGE_FINDINGS_AND_MATCH_RULE_ID` resolves against.

## Evidence limits

- **Fixture-only.** `tests/test_openai_audit.py`,
  `tests/test_openai_audit_route.py`, and
  `tests/test_openai_read_completeness.py` are hermetic. No test performs a
  live OpenAI request.
- **No live OpenAI request has ever been run by this product**, for the audit
  or for the reads that produce its evidence.
- **No provider-UI reconciliation.** No finding has been compared against
  OpenAI Ads Manager for an identical account, window, timezone, or currency.
- **No sandbox exists.** OpenAI Ads publishes no test-account environment, so
  there is no non-production surface on which to prove this table before a
  bounded, separately authorized live feasibility run.
- **This subset promotes nothing.** The OpenAI Ads `AUDIT` and `RECOMMEND`
  operation groups remain `UNRESEARCHED` in the group-level matrix.

## Account spellings

Retained OpenAI inventory records are keyed by the **native** account
identifier, because `run_inventory` appends with `profile.account_ref` and the
ledger's OpenAI snapshot validation requires that value to equal the account
resource reference with its `openai_ads.account.` prefix removed. An
`OpenAIAdsClient` report envelope instead carries the **canonical**
`openai_ads.account.<id>` reference. `retained_account_key` reduces both to the
native identifier with one fixed prefix, so the audit of a report this product
retained finds the inventory it retained, and `audit_provider` still refuses
evidence from a different account. (Until the 0.1.0 live read pilot command, this
was a recorded gap that left `audit --provider-rules` unable to join the two; LinkedIn
Ads had the same shape and the same fix.) `tests/test_openai_audit_route.py` pins the
join and the refusal; `tests/test_pilot_run_providers.py` runs it end to end.

# LinkedIn Ads audit rules and recommendations — 2026-09-08

`adhelm.audits.linkedin_ads` is the provider-specific half of the local audit. It
evaluates only records this product already retained: the selected normalized
report, the resource snapshots a completed inventory read appended, an optional
earlier snapshot set for drift, and an optional local creative catalog.

**It performs no live LinkedIn request.** It builds no client, holds no
transport, reads no credential, appends no ledger record, and compiles no plan.
Audit-time quota and access classification therefore does not arise here: a 429
becomes `LINKEDIN_ADS_RATE_LIMITED` (exit 3) and a 401/403 becomes
`PROVIDER_ACCESS_REQUIRED` (exit 4, or the LinkedIn-specific
`LINKEDIN_ADS_AUTHORIZATION_REQUIRED`) inside `adhelm.transport` and
`adhelm.providers.linkedin_ads` at read time, before any record exists to audit,
and no response body reaches an error message. `tests/test_linkedin_audit.py`
pins the no-read, no-append property by failing the audit if
`SafeHttpClient.request` or `Ledger.append` is reached.

## Entry point and registry seam

```python
audit_provider(report, source_record_digest=..., policy=..., as_of=..., evidence=...)
```

`adhelm.audit.audit_provider` runs the unchanged provider-neutral
`audit_report`, embeds that result verbatim, then dispatches to the registered
rule module for `report.envelope.provider`. A provider with no registered module
returns `rules_status="NO_PROVIDER_RULE_MODULE"` and no sections, so the other
five providers are untouched. `audit_report` itself is unchanged: identical
inputs still produce identical bytes.

The remaining P01 provider tickets add a provider by:

1. creating `adhelm/audits/<provider>.py` with a single
   `evaluate(context: ProviderAuditContext) -> ProviderAuditSections`, calling
   `register_provider_rule("<provider>", evaluate)` at module scope;
2. adding `"<provider>": "adhelm.audits.<provider>"` to `_RULE_MODULES` in
   `adhelm/audits/__init__.py`;
3. declaring the provider's compilable operation kinds in
   `COMPILABLE_OPERATION_KINDS`.

Shared types — `RetainedInventory`, `RetainedSnapshotRecord`,
`ResourceSnapshotState`, `RetainedCreativeCatalog`, `ProviderAuditCoverage`,
`ProviderAuditFinding`, `ProviderRecommendation`, `NoRecommendation`,
`DriftObservation` — live in `adhelm/audits/__init__.py` and are provider-neutral.
`adhelm.audit.build_finding` and `adhelm.audit.build_recommendation` give every
rule module the same RFC 8785 content identity as the report-only rules.

## Command contract

```text
adhelm audit <report-record-digest> --input <audit-policy.json> --provider-rules
           [--account <account-ref>] [--inventory-digest <record-digest>]
           [--drift | --no-drift] [--at <aware-timestamp>] [--cursor N] [--limit N]
```

Without `--provider-rules` the command is unchanged and byte-identical: it emits
the provider-neutral `AuditResult` under the command name `audit`. The
inventory-selection options are rejected with `INVALID_AUDIT_SELECTION` (exit 2)
unless `--provider-rules` is given.

With `--provider-rules` the command emits the `ProviderAuditResult` under the
command name `audit --provider-rules`, in the same bounded envelope, through the
same redacted sharing boundary, with outcome `INCOMPLETE_COVERAGE`. Its
`data.sections` rows (`findings`, `recommendations`, `no_recommendations`,
`drift_observations`, `bound_exceeded`) and the embedded `data.audit` rows page
together with `--cursor`/`--limit`, and `row_counts` reports complete totals for
all seven lists.
When the requested page would exceed the saved output byte preference the route
serves the largest page that fits and names where it stopped in `next_cursor`:
that is paging, not omission, and it never reports an internal bound as a caller
input error. `provider_audit_digest` covers the whole unpaged result and does not
change between pages when `--at` fixes the evaluation time.

`adhelm schema provider-audit-result` and `adhelm schema provider-audit-evidence`
are installed, and `adhelm discover` reports the route's coverage vocabulary,
`live_provider_reads: NONE`, `ledger_appends: NONE`,
`analytics_completeness: BOUNDED_NON_EXHAUSTIVE`, and
`evidence_level: FIXTURE_VERIFIED` with `live_verified: false` and
`provider_ui_reconciled: false`.

### Evidence selection

`adhelm.saved_audit_evidence.load_audit_evidence` selects everything from one
verified ledger checkpoint and makes no provider request.

`run_inventory` appends one `RESOURCE_SNAPSHOT` at a time and then appends an
`INVENTORY_READ_COMPLETED` marker naming exactly the snapshot records that read
produced, its resource counts, and its start and finish instants. A read is
selected from its marker, never inferred from the shape of the records, because
the durable prefix an interrupted append loop leaves behind is indistinguishable
from a finished read by shape alone. Selecting a prefix as current would report
every resource the run never reached as an out-of-band deletion, which
`AGENTS.md` rule 24 forbids.

| Record set | Completeness | Effect |
| --- | --- | --- |
| Named by a completion marker | `COMPLETE_BOUNDED_READ` | Absence is evidence; drift may be `COVERED` |
| Appended after a marker exists but named by none | not selectable | Never current, never a drift baseline; counted in provenance |
| Retained before this product recorded completions | `READ_BOUNDARY_UNVERIFIED` | Absence carries no information: absence-based drift is suppressed and absence rules drop to `LOW` |

Legacy history is split on a repeated resource reference — the only
reconstruction available for it — and reported with
`selected_inventory_read_boundary: LEGACY_READ_WITHOUT_COMPLETION_MARKER`. The
selected read is then required to be a whole hierarchy — exactly one account,
and every declared parent present in the same read — and a read that fails that
check is refused with `INVENTORY_READ_INCOMPLETE` (exit 3) rather than audited
as complete. Nothing is truncated or filled in. `--inventory-digest` selects the
read containing that snapshot record instead of the latest one, and `--no-drift`
skips the preceding read.

Retained evidence is bound to the audited report: `--account` defaults to the
report envelope's own account and evidence for a different account is refused
with exit 2.

| Condition | Code | Exit |
| --- | --- | --- |
| Report digest not in verified history | `REPORT_NOT_RETAINED` | 3 |
| No retained inventory read for the account | `INVENTORY_NOT_RETAINED` | 3 |
| Selected snapshot digest not retained | `INVENTORY_SNAPSHOT_NOT_RETAINED` | 3 |
| Selected read is not a whole hierarchy | `INVENTORY_READ_INCOMPLETE` | 3 |
| More than one retained account for the provider | `AUDIT_ACCOUNT_SELECTION_REQUIRED` | 2 |
| Retained evidence for another account than the report | `INVALID_AUDIT_INPUT` | 2 |
| Non-advertising provider, or malformed digest | `INVALID_AUDIT_EVIDENCE_SELECTION` | 2 |
| Inventory options without `--provider-rules` | `INVALID_AUDIT_SELECTION` | 2 |

Every omission is named in `data.provenance` rather than dropped silently:
`earlier_inventory_status` is `SELECTED`, `NOT_REQUESTED`,
`NO_EARLIER_RETAINED_READ`, or `OMITTED_INCOMPLETE_EARLIER_READ`, and
`interrupted_snapshot_record_count` and `legacy_snapshot_record_count` count the
records that were not selected. The drift baseline reports its own boundary
beside the selected read's, through `earlier_inventory_completeness`,
`earlier_inventory_read_boundary`, and
`earlier_inventory_completion_record_digest`, because the baseline's boundary is
what decides whether an absence between the two reads means anything. The creative catalog is built from retained
`CREATIVE_DEPLOYMENT` records for the same provider account whose own
`association_status` is `ACTIVE`; a closed or superseded association is not an
outstanding binding, and each entry is a local declaration, never a
provider-verified association.

## Coverage map

Coverage is derived from `LINKEDIN_ADS_READ_SURFACES` in the adapter, not
restated here, so adding a field to the fixed read is the only way to promote a
dimension.

| Dimension | Status | Why |
| --- | --- | --- |
| `STRUCTURE` | `COVERED` with a complete retained inventory, `PARTIAL` with a caller-declared subset, else `NOT_READ` | Identity, parents, and declared status are read |
| `DELIVERY` | `PARTIAL` (never `COVERED`) | The Ad Analytics finder documents no pagination |
| `SPEND` | `PARTIAL` | Native cost decimal is retained; budget and run schedule are not read, so pacing is not evaluated |
| `BIDS` | `NOT_READ` | Bid amount and strategy are not requested |
| `OBJECTIVE` | `PARTIAL` with campaigns, else `NOT_READ` | The objective is retained; no reviewed enumeration exists to classify it |
| `TARGETING` | `NOT_READ` | Targeting criteria are not requested |
| `PLACEMENT` | `NOT_READ` | No placement field or report facet is requested |
| `CREATIVE` | `PARTIAL` with creatives, else `NOT_READ` | Identity, intended status, and campaign association are read; content is not |
| `DESTINATION_CONFIGURATION` | `NOT_READ` | No landing page or tagging field is requested |
| `TRACKING` | `NOT_READ` | No tracking configuration read exists in this build |
| `NATIVE_CONVERSION` | `NOT_READ` | The fixed report requests impressions, clicks, and cost only |
| `BROWSER_MEASUREMENT` | `NOT_APPLICABLE` | A local audit executes no browser and loads no page |
| `POLICY` | `NOT_READ` | Creative review/rejection state and account serving statuses are not requested |
| `DRIFT` | `COVERED` with a snapshot pair, else `NOT_READ` | Drift needs two retained inventories for one account |

A `NOT_READ` or `NOT_APPLICABLE` dimension cites no evidence and yields no
recommendation. An unread dimension is never reported as healthy.

## Rule table

Every rule has fixed prose, a fixed severity, and a fixed dimension. Findings
are sorted by content identifier, so identical retained evidence always yields
identical bytes.

| Rule | Finding type | Dimension | Severity | Fires when |
| --- | --- | --- | --- | --- |
| `LI_STRUCTURE_GROUP_WITHOUT_CAMPAIGN` | `LINKEDIN_CAMPAIGN_GROUP_WITHOUT_CAMPAIGN` | STRUCTURE | LOW | A retained group has no retained child campaign |
| `LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE` | `LINKEDIN_CAMPAIGN_WITHOUT_CREATIVE` | STRUCTURE | MEDIUM | A retained campaign has no retained creative |
| `LI_STRUCTURE_CREATIVE_PARENT_NOT_RETAINED` | `LINKEDIN_CREATIVE_PARENT_CAMPAIGN_NOT_RETAINED` | STRUCTURE | MEDIUM | A creative's parent campaign is absent from the same inventory |
| `LI_STRUCTURE_CAMPAIGN_PARENT_NOT_RETAINED` | `LINKEDIN_CAMPAIGN_PARENT_GROUP_NOT_RETAINED` | STRUCTURE | MEDIUM | A campaign's parent group is absent from the same inventory |
| `LI_STRUCTURE_SERVING_INTENT_MISMATCH` | `LINKEDIN_SERVING_INTENT_MISMATCH_WITH_PARENT` | STRUCTURE | MEDIUM | An `ACTIVE` resource has a non-`ACTIVE` retained parent |
| `LI_STRUCTURE_STATUS_NOT_REVIEWED` | `LINKEDIN_STATUS_VALUE_NOT_IN_REVIEWED_ENUM` | STRUCTURE | INFO | A status is outside the reviewed enumeration the adapter compiles |
| `LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS` | `LINKEDIN_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_IMPRESSIONS` | DELIVERY | MEDIUM | An `ACTIVE` campaign has no impressions in the retained campaign-grain rows |
| `LI_DELIVERY_CLICKS_EXCEED_IMPRESSIONS` | `LINKEDIN_CLICKS_EXCEED_IMPRESSIONS` | DELIVERY | MEDIUM | A retained row declares more clicks than impressions |
| `LI_DELIVERY_METRICS_MISSING` | `LINKEDIN_DELIVERY_METRICS_MISSING_ON_ROW` | DELIVERY | INFO | A retained row declares no impression or click count |
| `LI_SPEND_WITHOUT_IMPRESSIONS` | `LINKEDIN_SPEND_WITHOUT_IMPRESSIONS` | SPEND | HIGH | A retained row declares spend with zero impressions |
| `LI_SPEND_PACING_INPUTS_NOT_READ` | `LINKEDIN_BUDGET_AND_SCHEDULE_NOT_READ` | SPEND | INFO | An account snapshot is retained (pacing inputs are not read) |
| `LI_BIDS_NOT_READ` | `LINKEDIN_BID_CONFIGURATION_NOT_READ` | BIDS | INFO | An account snapshot is retained |
| `LI_OBJECTIVE_NOT_RETURNED` | `LINKEDIN_CAMPAIGN_OBJECTIVE_NOT_RETURNED` | OBJECTIVE | INFO | A retained campaign carries no objective |
| `LI_OBJECTIVE_ENUM_NOT_REVIEWED` | `LINKEDIN_CAMPAIGN_OBJECTIVE_ENUM_NOT_REVIEWED` | OBJECTIVE | INFO | A retained campaign carries an objective this build cannot classify |
| `LI_TARGETING_NOT_READ` | `LINKEDIN_TARGETING_CRITERIA_NOT_READ` | TARGETING | INFO | At least one campaign is retained |
| `LI_PLACEMENT_NOT_READ` | `LINKEDIN_PLACEMENT_NOT_READ` | PLACEMENT | INFO | An account snapshot is retained |
| `LI_CREATIVE_CONTENT_NOT_READ` | `LINKEDIN_CREATIVE_CONTENT_NOT_READ` | CREATIVE | INFO | At least one creative is retained |
| `LI_CREATIVE_STATUS_NOT_RETURNED` | `LINKEDIN_CREATIVE_STATUS_NOT_RETURNED` | CREATIVE | LOW | A retained creative carries no declared intended status |
| `LI_CREATIVE_LOCAL_BINDING_UNVERIFIED` | `LINKEDIN_LOCAL_CREATIVE_BINDING_UNVERIFIED` | CREATIVE | LOW | A local creative version declares a retained LinkedIn creative |
| `LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` | `LINKEDIN_LOCAL_CREATIVE_BINDING_WITHOUT_RETAINED_RESOURCE` | CREATIVE | MEDIUM | A local creative version declares a LinkedIn creative absent from inventory |
| `LI_DESTINATION_NOT_READ` | `LINKEDIN_DESTINATION_AND_UTM_NOT_READ` | DESTINATION_CONFIGURATION | INFO | An account snapshot is retained |
| `LI_TRACKING_NOT_READ` | `LINKEDIN_TRACKING_CONFIGURATION_NOT_READ` | TRACKING | INFO | An account snapshot is retained |
| `LI_NATIVE_CONVERSION_NOT_READ` | `LINKEDIN_NATIVE_CONVERSION_NOT_READ` | NATIVE_CONVERSION | INFO | An account snapshot is retained |
| `LI_BROWSER_NOT_APPLICABLE` | `LINKEDIN_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT` | BROWSER_MEASUREMENT | INFO | An account snapshot is retained |
| `LI_POLICY_REVIEW_STATE_NOT_READ` | `LINKEDIN_POLICY_REVIEW_STATE_NOT_READ` | POLICY | INFO | An account snapshot is retained |
| `LI_DRIFT_STATE_CHANGED` | `LINKEDIN_OUT_OF_BAND_STATE_CHANGE_OBSERVED` | DRIFT | MEDIUM | Two retained snapshots of one resource differ |
| `LI_DRIFT_ABSENT_FROM_LATER_INVENTORY` | `LINKEDIN_RESOURCE_ABSENT_FROM_LATER_INVENTORY` | DRIFT | MEDIUM | A resource is in the earlier inventory only |
| `LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY` | `LINKEDIN_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY` | DRIFT | LOW | A resource is in the later inventory only |

Structure rules that reason from an absence drop to `LOW` confidence when the
caller declares the retained inventory a subset rather than a complete bounded
read.

## Read surfaces

`LINKEDIN_ADS_READ_SURFACES` in `adhelm/providers/linkedin_ads.py` records what
the reviewed 202608 read actually returns. Two values matter for this audit:

- `CREATIVE_DELIVERY_REPORT` is `IMPLEMENTED_FIXED_READ_BOUNDED_NON_EXHAUSTIVE`.
  A creative-level delivery read already exists: `run_ad_report` compiles the
  public `AD` grain to LinkedIn's documented `CREATIVE` reporting pivot on the
  same Ad Analytics finder. No new finder was pinned for this ticket, so
  `docs/research/linkedin-ads-read-contract.md` is unchanged.
- `ANALYTICS_PAGINATION` is `NOT_DOCUMENTED`, which is what forces every
  report-derived finding to `BOUNDED_NON_EXHAUSTIVE`.

`NOT_IN_REVIEWED_FIXED_READ` means this build does not request the field. It is
not a claim that LinkedIn documents no such field anywhere; promoting a
dimension requires reviewing and implementing that read, then updating the
research contract.

## Analytics completeness

The reviewed Ad Analytics finder has no documented pagination
(`docs/research/linkedin-ads-read-contract.md`), so a retained LinkedIn report
is never `COMPLETE`. Every finding derived from it carries
`analytics_completeness="BOUNDED_NON_EXHAUSTIVE"`. The vocabulary has no
exhaustive member at all, so no finding, recommendation, or section in this
package can publish one. The zero-impression rule therefore states in its own
observation that the absence of rows is not proof of zero delivery, and no rule
phrases a retained aggregate as a total. No pagination is invented anywhere.

Declared-threshold cost comparison stays in the provider-neutral audit: its
`maximum_cpc` rule already compares LinkedIn's retained native decimal. The
provider rules add no universal CPC, volume, freshness, or performance
threshold, so a cost anomaly is reported only against a user-declared value or
an internal inconsistency in the retained row itself.

Spend comparisons use the retained native decimal
(`linkedin_ads_cost_in_local_currency`), not the rounded cent projection, so a
fractional-cent charge with zero impressions still fires.

## Drift

Drift compares two retained snapshot sets with the same path semantics as local
history's snapshot diff; `tests/test_linkedin_audit.py` pins that equivalence
against `adhelm.inventory._snapshot_deltas`. Each `DriftObservation` carries the
changed field **paths only**, never their values, the record digests it
compared, the observation interval bounded by the two retained observation
times, and `observed_actor="UNKNOWN"`. No exact external change time or actor is
invented, per AGENTS rule 24.

A per-resource state comparison is valid whatever the read boundary. A set
difference is not: `RESOURCE_ABSENT_FROM_LATER_INVENTORY` and
`RESOURCE_ABSENT_FROM_EARLIER_INVENTORY` are suppressed entirely when either
side is `READ_BOUNDARY_UNVERIFIED`, and drop to `LOW` confidence over a declared
subset. The `DRIFT` dimension is `COVERED` only when both sides are whole reads.
Suppression is an omission, so it is named: the `DRIFT` coverage rationale
carries the unproven-boundary caveat when *either* side is unproven and states
that resource appearance and disappearance were not evaluated at all.

## Recommendations

Every finding is either recommended on or explicitly declined. Each row states
the complete `bound_finding_count` it rests on and the `bound_rule_ids` that
define its membership, and inlines the first `MAXIMUM_INLINE_FINDING_IDS` (32)
identifiers. The inline cap keeps one output row a constant size so the smallest
page an agent can request always fits the output bound, whatever the size of the
account; reaching it is not a caller error and never fails the audit. Nothing is
dropped: every finding above the cap remains its own row in `findings`, reachable
by paging and matching `rule_id` against the row's `bound_rule_ids`, and an
explicit `bound_exceeded` entry records the scope, the cap, the total, the
omitted count, the rule identifiers, and that resolution. `bound_exceeded` pages
and is counted in `row_counts` like every other list.

Each recommendation is bound to one or more findings by finding identifier,
names an operation kind in the product's vocabulary, and is explicitly typed
`execution_status=NOT_EXECUTED`, `plan_status=NOT_A_PLAN`,
`authorization_status=NO_AUTHORIZATION_DIGEST_PRESENT`, with
`proposed_plan_ref=None`. Recommendation prose is rejected if it contains a
64-character hexadecimal run that could be mistaken for an authorization digest.

This build compiles only an offline, non-executable LinkedIn `CREATE` candidate
(a complete new campaign group, campaign, and creative). It compiles no
LinkedIn pause, update, association, or activation. Every LinkedIn
recommendation therefore uses the local `OBSERVE` operation — read the same
bounded evidence again — and the model rejects any other mutation kind for this
provider.

Where an operation would be the natural follow-up but is not compilable, the
result carries an explicit `NoRecommendation` instead:

| Reason | Example |
| --- | --- |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | Pausing a zero-impression campaign (`considered_operation_kind="PAUSE"`) |
| `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` | Reconciling a parent/child status (`UPDATE`), associating a creative (`ASSOCIATE`), adding a campaign to an existing group (`CREATE`) |
| `EVIDENCE_INSUFFICIENT` | Unreviewed status or objective enumerations, unverified local creative bindings, missing row metrics |
| `COVERAGE_NOT_READ` | Every dimension the fixed read does not cover |

## Evidence limits

- **Fixture-only.** No test or evaluation in this ticket made a LinkedIn
  request, used a customer credential, or reconciled against the LinkedIn
  interface. The rule table is `FIXTURE_VERIFIED` at best.
- No audit output establishes serving state, delivery truth, creative content,
  policy approval, targeting, tracking, or conversion health. It reports what
  the reviewed fixed read returned and what it does not return.
- A retained analytics report is never exhaustive; absence is not zero.
- A local creative binding is a local declaration, never a provider-verified
  association. Inventory snapshots carry
  `CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER`.
- Drift reports that two retained observations differ. It does not prove who
  changed anything, or when inside the interval.
- Findings and recommendations carry no names, URLs, query values, creative
  text, raw payloads, or provider response bodies; they cite record digests
  only.

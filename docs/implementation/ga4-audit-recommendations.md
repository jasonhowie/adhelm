# GA4 audit rules and recommendations (O2.4)

Written 2026-10-10 for ticket O2.4 of the 0.1.0 build plan. `adhelm.audits.ga4`
is the GA4 half of the local provider audit. It evaluates only records this
product already retained: the selected GA4 Data API report and, optionally, one
retained GA4 Admin inventory observation.

## Gap list

Measured against the [Data aggregate
contract](../research/ga4-data-aggregate-contract.md), the [Admin read
contract](../research/ga4-admin-read-contract.md), the GA4 rows of the internal
requirement map (`DOD-02-03`, `DOD-02-05`, `DOD-02-10`, which are the GA4
counterparts of the `DOD-01-01`/`DOD-01-03`/`DOD-01-04` audit rows) and
[`reporting-schema.md`](../reporting-schema.md). The list is limited to
read-only behaviour and was verified against the integration tip before any code
changed.

| # | Gap on the integration tip | Evidence |
| --- | --- | --- |
| G1 | No GA4 rule module. `audit --provider-rules` on a GA4 report returns `NO_PROVIDER_RULE_MODULE`, so there is no per-dimension coverage table and no GA4 finding or recommendation, although the release definition requires audit rules with per-dimension coverage and evidence-backed recommendations for every provider | `provider_rule("ga4")` is `None`; `ga4` is absent from `_RULE_MODULES` |
| G2 | The evidence model cannot carry GA4 Admin configuration. `load_audit_evidence` refuses every non-advertising provider with `INVALID_AUDIT_EVIDENCE_SELECTION`, and the retained Admin inventory is a `MEASUREMENT_OBSERVATION` (provider `ga4_admin`) rather than a `RESOURCE_SNAPSHOT` set. Nothing binds a `ga4.property.N` report to a `ga4_admin.property.N` observation at audit time | `saved_audit_evidence.py`; `measurement_records.Ga4MeasurementSnapshot` |
| G3 | Report-quality facts collapse into one generic `REPORT_QUALITY_PARTIAL` finding. Thresholding, sampling, the `(other)` roll-up, restricted metrics, an incomplete page walk, a provider-declined read and a key event that is withheld versus never returned are all retained, but no audit output distinguishes them, so the "never masquerade as zero" rule (`DOD-02-05`) stops at the ledger. A standard `run_report` read and a zero-row aggregate keep a roll-up or metric restriction only as the envelope status `PROVIDER_REPORTED_UNKNOWN`, and an aggregate whose pages disagree on sampling keeps that only as `ga4_sampling_consistency`; each needed a rule of its own | `audit._has_incomplete_quality`; `Ga4AggregateReport._report_provenance` |
| G4 | A report with no rows keeps only its envelope quality statuses. The report-level completeness facts persist as row extensions, so an empty aggregate carries no `ga4_completeness`. The audit must derive what it can from the envelope and say what it cannot know. Persistence is not changed here | `Ga4AggregateReport.to_normalized_report` |
| G5 | GA4 Admin configuration is not audited: an empty key-event list, an unspecified counting method, event create/edit rules whose content is held only as fingerprints, and an inventory older than the customer's declared maximum age | `Ga4AdminInventory` fields |
| G6 | No cross-read check. The Data API cannot enumerate key events, so a key-event report is built from a caller-supplied list. Nothing relates the key events a report declared to the retained Admin inventory | contract section "no `keyEvents:<name>` metric" |
| G7 | No timezone/date-boundary or freshness finding. A window may end on the property-local date of observation, and the Data API returns no freshness field (`data_fresh_through` is always `null`), so the latest day is provisional | contract sections "Freshness" and S7 |
| G8 | The provider-neutral audit reports `NATIVE_CONVERSION` as `PARTIAL` for a GA4 report because it counts any conversion row. GA4 key events are not provider-native conversions (AGENTS rule 16, `Ga4AggregateReport.key_event_disclosure`) | probe: `[('NATIVE_CONVERSION', 'PARTIAL')]` for a report whose only conversion row has `conversion_source="GA4"` |
| G9 | No recommendation, and no statement of what this build can compile for GA4. `COMPILABLE_OPERATION_KINDS` had no GA4 entry although the product compiles a fixed GA4 Admin key-event `CREATE`, so the registry understated the product, and every candidate follow-up needed an explicit reasoned decline rather than silence. Compilable and recommended are different questions: the registry answers the first, the rule table the second | `audits/__init__.py`; `providers/ga4_admin_compiler.py`, `ga4_key_event_candidates.py`, `saved_ga4_create_plans.py`, `ga4_create_execution_workflow.py` |
| G10 | Several dimensions have no GA4 read behind them: destination and link configuration, consent/retention policy, attribution settings (Admin `v1alpha` `getAttributionSettings` is deliberately not called) and every data stream other than the one configured web stream. Each must be explicit `NOT_READ`, never healthy. GA4 Admin drift is already compared by `measurement compare` over two retained observations, so the audit leaves `DRIFT` as `NOT_READ` and points there rather than duplicating it | `docs/research/ga4-data-aggregate-contract.md` "Attribution"; `measurement_comparison.py` |
| G11 | Documentation: the capability matrix has no GA4 audit row | `docs/capability-matrix.md` |

### Not gaps for this ticket

- Live GA4 requests, GA4 UI reconciliation, Realtime and processed-report
  verification (`DOD-02-08`), and quota evidence. All remain `L01`/`M03` work.
- Any GA4 write, key-event creation or edit plan, and any change to what the
  Data or Admin reads request.
- Persisting report-level facts for empty reports (G4 records the limit only).
- The `discover` text for `provider_audit`, which describes only LinkedIn's
  analytics semantics. It lives in `cli.py`, a shared surface owned by the CLI
  ticket. `discover` already lists `ga4` among the rule modules because that list
  is derived from `_RULE_MODULES`.
- Campaign join creation (`measurement join`); the audit reports join state, it
  never resolves it.

**It performs no live GA4 request.** It builds no client, holds no transport,
reads no credential, appends no ledger record and compiles no plan. Audit-time
quota and access classification therefore does not arise here: a 429 or a
401/403 is classified inside `adhelm.transport` and the GA4 read adapters at read
time, before any record exists to audit, and no response body reaches an error
message. `tests/test_ga4_audit.py` and `tests/test_ga4_audit_route.py` pin the
no-request, no-append property by failing the audit if `SafeHttpClient.request`
or `Ledger.append` is reached.

## Entry point and registry seam

`ga4` is registered in `_RULE_MODULES`, so `adhelm.audit.audit_provider` dispatches a
GA4 report to `adhelm.audits.ga4.evaluate` exactly as it does for the advertising
providers. The provider-neutral `audit_report` result is embedded unchanged,
except for the coverage correction below.

Two shared additions carry the Admin evidence:

- `RetainedGa4AdminObservation` (record digest, read-finish instant, the closed
  `Ga4AdminInventory`) and the optional `ProviderAuditEvidence.ga4_admin` field.
  The `RETAINED_MEASUREMENT_OBSERVATION` evidence source names it in coverage.
- `audit_provider` refuses `ga4_admin` evidence for a report of another provider,
  or for a different property, with `INVALID_AUDIT_INPUT` (exit 2). The only join
  between a `ga4.property.N` report and a `ga4_admin.property.N` observation is
  the numeric property identifier. It also refuses an observation that finished
  after the audit's evaluation time (`--at`): configuration evidence that did not
  yet exist cannot be evaluated, and its negative age would otherwise never be
  stale.
- `ProviderAuditEvidence.ga4_requested_property_ref`, the property the caller
  asked to audit evidence for. The loader records it whether or not an
  observation is retained, and `audit_provider` refuses a property that is not
  the report's own with `INVALID_AUDIT_INPUT` (exit 2). The outcome therefore does
  not depend on what happens to be retained for the requested property.

`audit_report` changes in one place: `NATIVE_CONVERSION` is `PARTIAL` only for
provider-sourced conversion rows. A GA4 key-event row (`conversion_source="GA4"`)
and a CRM row no longer count, so a GA4 report reports `EVIDENCE_MISSING`
(AGENTS rule 16). Provider-sourced rows are unchanged.

## Command contract

```text
adhelm audit <ga4-report-record-digest> --input <audit-policy.json> --provider-rules
           [--account <ga4.property.N>] [--inventory-digest <measurement-record-digest>]
           [--at <aware-timestamp>] [--cursor N] [--limit N]
```

The route, paging, redaction and `INCOMPLETE_COVERAGE` outcome are the shared
`audit --provider-rules` contract described in
[LinkedIn audit rules](linkedin-audit-recommendations.md#command-contract).
`cli.py` is unchanged. `--drift` and `--no-drift` have no effect on a GA4 audit.

### Evidence selection

`adhelm.saved_ga4_audit_evidence.load_ga4_audit_evidence` selects, from one
verified ledger checkpoint, the retained `MEASUREMENT_OBSERVATION` for provider
`ga4_admin` whose account is the audited report's property. Without
`--inventory-digest` it takes the most recent one. It makes no provider request,
reads no credential, and appends no record.

The Admin observation is **optional evidence**. A property with none retained is
audited from its report alone: `provenance.admin_inventory_status` is
`NOT_RETAINED`, `STRUCTURE` is `NOT_READ`, and `GA4_ADMIN_INVENTORY_NOT_RETAINED`
says so. The inventory was listed to its last page by the read that produced it,
so an absent key event is real evidence about the one web stream that was read.

| Condition | Code | Exit |
| --- | --- | --- |
| Report digest not in verified history | `REPORT_NOT_RETAINED` | 3 |
| `--inventory-digest` names no Admin observation of this property | `INVENTORY_SNAPSHOT_NOT_RETAINED` | 3 |
| `--inventory-digest` is not a lowercase SHA-256 digest | `INVALID_AUDIT_EVIDENCE_SELECTION` | 2 |
| The account is not a GA4 property reference | `INVALID_AUDIT_EVIDENCE_SELECTION` | 2 |
| `--account` names a property other than the report's, whether or not an observation is retained for it | `INVALID_AUDIT_INPUT` | 2 |
| Retained Admin evidence for another property or provider | `INVALID_AUDIT_INPUT` | 2 |
| Retained Admin evidence finished after the `--at` evaluation time | `INVALID_AUDIT_INPUT` | 2 |
| Report timezone is not an IANA zone | `INVALID_AUDIT_INPUT` | 2 |

`--account` defaults to the audited report's own property. An explicit `--account`
that names a different property is refused with `INVALID_AUDIT_INPUT` (exit 2)
and emits no finding, whether or not an observation is retained for that other
property; it is never silently replaced by the report's property and never mixed
with another property's observation. With no observation retained for the
report's own property the audit proceeds without Admin evidence and says so in
the provenance. With `--at` earlier than the newest retained observation, that
observation is refused (exit 2); select an earlier one with `--inventory-digest`.
When several web streams of
one property were read, the latest observation is used and
`retained_admin_observation_count` shows how many exist; select another with
`--inventory-digest`. The property identifier itself is not echoed into the
provenance.

## Coverage map

| Dimension | Status | Why |
| --- | --- | --- |
| `STRUCTURE` | `PARTIAL` with an Admin observation, else `NOT_READ` | Key events and the event create/edit rules of one configured web stream are read; other streams, custom dimensions, audiences and product links are not |
| `DELIVERY`, `SPEND`, `BIDS`, `OBJECTIVE`, `TARGETING`, `PLACEMENT`, `CREATIVE` | `NOT_APPLICABLE` | GA4 delivers no advertising and holds none of these |
| `DESTINATION_CONFIGURATION` | `NOT_READ` | Stream URLs, cross-domain settings, referral exclusions and product links are not requested |
| `TRACKING` | `PARTIAL` (never `COVERED`) | Report quality, key-event cells and key-event configuration are evaluated; attribution settings, browser firing, processed receipt, GA4-interface comparison and freshness are not evidenced |
| `NATIVE_CONVERSION` | `NOT_APPLICABLE` | A GA4 key event is not a provider-native conversion |
| `BROWSER_MEASUREMENT` | `NOT_APPLICABLE` | A local audit executes no browser |
| `POLICY` | `NOT_READ` | Consent, collection, retention and signals settings are not requested |
| `DRIFT` | `NOT_READ` | `measurement compare` already compares two retained Admin observations with an `UNKNOWN` actor; the audit reads one |

A `NOT_READ` or `NOT_APPLICABLE` dimension cites no evidence and yields no
recommendation. An unread dimension is never reported as healthy.

## Rule table

Every rule has fixed prose, a fixed severity and a fixed dimension, and the rule
table version is part of every finding's content identity. Findings are sorted by
content identifier, so identical retained evidence yields identical bytes.
Report-derived findings are `BOUNDED_NON_EXHAUSTIVE`; Admin-only findings are
`NOT_APPLICABLE` for analytics completeness.

The report-quality rules read the envelope quality statuses, which every GA4
report keeps, and the per-row extensions the fixed aggregate read persists. A
report read through the aggregate contract therefore yields the finer facts; any
other GA4 report (the standard pilot read, or Realtime) yields the envelope
statuses plus `GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED`. A roll-up or metric
restriction that GA4 flagged but that no finer rule explains is kept as one finding
built from the envelope status `PROVIDER_REPORTED_UNKNOWN`. It says GA4 flagged
something and this report does not say which it was, and the reason and the advice
depend on whether the report kind keeps the facts, not on whether it has rows. A
standard or Realtime read with rows (a kind that keeps no roll-up or restriction
fact) fires `GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN`, whose action is to read a fixed
aggregate shape. Every other unexplained flag fires
`GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE`: a report with no rows has no row to
carry the fact, and an aggregate can carry the flag without any persisted fact
naming a roll-up or a restricted metric (the adapter sets the status whenever
`schemaRestrictionResponse` is present, even with an empty
`activeMetricRestrictions`, and persists a restriction extension only when a metric
is named). Its action says this read cannot attribute the flag and does not send
the user to repeat the read just done. Pages of one aggregate that disagree on sampling add
`GA4_REPORT_SAMPLING_METADATA_INCONSISTENT`, so the warning that the sample ratio
itself is unreliable is not lost behind `GA4_REPORT_SAMPLED`.

| Rule | Finding type | Dimension | Severity | Confidence |
| --- | --- | --- | --- | --- |
| `GA4_REPORT_THRESHOLDED` | `GA4_REPORT_SUBJECT_TO_THRESHOLDING` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_SAMPLED` | `GA4_REPORT_SAMPLED_ESTIMATES` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_OTHER_ROW_ROLLUP` | `GA4_VALUES_ROLLED_INTO_OTHER_ROW` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_METRICS_RESTRICTED` | `GA4_METRICS_RESTRICTED` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_SAMPLING_METADATA_INCONSISTENT` | `GA4_SAMPLING_METADATA_DIFFERS_BETWEEN_PAGES` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN` | `GA4_PROVIDER_FLAGGED_UNRETAINED_ROLLUP_OR_RESTRICTION` | TRACKING | MEDIUM | MEDIUM |
| `GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE` | `GA4_PROVIDER_FLAGGED_UNATTRIBUTABLE_ROLLUP_OR_RESTRICTION` | TRACKING | MEDIUM | MEDIUM |
| `GA4_REPORT_INCOMPLETE` | `GA4_REPORT_PAGE_WALK_INCOMPLETE` | TRACKING | HIGH | HIGH |
| `GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER` | `GA4_PROVIDER_DECLINED_TO_REPORT` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_NO_ROWS` | `GA4_REPORT_HOLDS_NO_ROWS` | TRACKING | LOW | HIGH |
| `GA4_KEY_EVENT_THRESHOLD_WITHHELD` | `GA4_KEY_EVENT_COUNT_WITHHELD_BY_THRESHOLDING` | TRACKING | MEDIUM | HIGH |
| `GA4_KEY_EVENT_NOT_RETURNED` | `GA4_KEY_EVENT_HAS_NO_RETURNED_ROW` | TRACKING | MEDIUM | MEDIUM |
| `GA4_ROWS_JOIN_UNRESOLVED` | `GA4_ROWS_NOT_JOINED_TO_PROVIDER_CAMPAIGNS` | TRACKING | INFO | HIGH |
| `GA4_ROWS_JOIN_CONFLICT` | `GA4_ROW_JOIN_CONFLICT` | TRACKING | MEDIUM | HIGH |
| `GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME` | `GA4_SOURCE_AND_MEDIUM_WITHOUT_CAMPAIGN_NAME` | TRACKING | LOW | MEDIUM |
| `GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE` | `GA4_WINDOW_ENDS_ON_THE_OBSERVATION_DATE` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_REALTIME_NOT_PROCESSED` | `GA4_REALTIME_REPORT_IS_NOT_PROCESSED_DATA` | TRACKING | MEDIUM | HIGH |
| `GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED` | `GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED` | TRACKING | INFO | HIGH |
| `GA4_NO_KEY_EVENTS_CONFIGURED` | `GA4_PROPERTY_HAS_NO_KEY_EVENTS` | TRACKING | MEDIUM | HIGH |
| `GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED` | `GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED` | TRACKING | LOW | HIGH |
| `GA4_EVENT_RULES_CONTENT_NOT_INSPECTED` | `GA4_EVENT_RULE_CONTENT_OPAQUE` | TRACKING | LOW | HIGH |
| `GA4_ADMIN_INVENTORY_STALE` | `GA4_ADMIN_OBSERVATION_OLDER_THAN_DECLARED_MAXIMUM` | TRACKING | MEDIUM | HIGH |
| `GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN` | `GA4_DECLARED_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN_INVENTORY` | TRACKING | MEDIUM | LOW |
| `GA4_ADMIN_INVENTORY_NOT_RETAINED` | `GA4_ADMIN_INVENTORY_NOT_RETAINED` | STRUCTURE | INFO | HIGH |
| `GA4_DESTINATION_AND_LINKS_NOT_READ` | `GA4_DESTINATION_AND_PRODUCT_LINKS_NOT_READ` | DESTINATION_CONFIGURATION | INFO | HIGH |
| `GA4_POLICY_SETTINGS_NOT_READ` | `GA4_CONSENT_AND_RETENTION_SETTINGS_NOT_READ` | POLICY | INFO | HIGH |
| `GA4_ATTRIBUTION_SETTINGS_NOT_READ` | `GA4_ATTRIBUTION_MODEL_NOT_READ` | TRACKING | INFO | HIGH |
| `GA4_DRIFT_NOT_EVALUATED_BY_AUDIT` | `GA4_ADMIN_DRIFT_NOT_EVALUATED` | DRIFT | INFO | HIGH |
| `GA4_KEY_EVENT_NOT_A_PROVIDER_CONVERSION` | `GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION` | NATIVE_CONVERSION | INFO | HIGH |
| `GA4_BROWSER_NOT_APPLICABLE` | `GA4_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT` | BROWSER_MEASUREMENT | INFO | HIGH |

### What fires a rule

| Rule | Fires when |
| --- | --- |
| `GA4_REPORT_THRESHOLDED` | The envelope carries `THRESHOLDED`, or a row records `ga4_subject_to_thresholding=true` |
| `GA4_REPORT_SAMPLED` | The envelope carries `SAMPLED` |
| `GA4_REPORT_OTHER_ROW_ROLLUP` | A row records `ga4_other_row_aggregation=true`, which the adapter sets from `dataLossFromOtherRow` or any `(other)` dimension value |
| `GA4_REPORT_METRICS_RESTRICTED` | A row records `ga4_restricted_metrics` |
| `GA4_REPORT_SAMPLING_METADATA_INCONSISTENT` | A row records `ga4_sampling_consistency=SAMPLING_METADATA_INCONSISTENT`: the pages of the aggregate carried different sampling metadata. `GA4_REPORT_SAMPLED` still fires |
| `GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN` | The envelope carries `PROVIDER_REPORTED_UNKNOWN` (the adapter sets it for `dataLossFromOtherRow`, an active metric restriction, or inconsistent sampling) and none of `GA4_REPORT_OTHER_ROW_ROLLUP`, `GA4_REPORT_METRICS_RESTRICTED` or `GA4_REPORT_SAMPLING_METADATA_INCONSISTENT` fired to explain it. In practice: a standard or Realtime read GA4 flagged that has rows (`not aggregate and rows`) |
| `GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE` | The same condition as `GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN`, on every report that variant does not cover: a report with no rows (a zero-row aggregate or a zero-row standard read), where no row exists to carry the fact, or an aggregate whose persisted facts name no roll-up or restriction (for example `schemaRestrictionResponse` with an empty `activeMetricRestrictions`) |
| `GA4_REPORT_INCOMPLETE` | The persisted completeness is `INCOMPLETE_PAGE_BOUND_EXCEEDED` or `INCOMPLETE_ROW_COUNT_MISMATCH`, or the envelope carries `PARTIAL_PAGE_FAILURE` |
| `GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER` | The envelope carries `NOT_AVAILABLE_FROM_PROVIDER` (GA4 returned an `emptyReason`) |
| `GA4_REPORT_NO_ROWS` | The report holds no performance rows |
| `GA4_KEY_EVENT_THRESHOLD_WITHHELD` | A declared key event's persisted cell status is `THRESHOLD_WITHHELD` |
| `GA4_KEY_EVENT_NOT_RETURNED` | A declared key event's persisted cell status is `NOT_RETURNED` |
| `GA4_ROWS_JOIN_UNRESOLVED` | A campaign-shape row is `UNMAPPED` or `HEURISTIC` |
| `GA4_ROWS_JOIN_CONFLICT` | A campaign-shape row is `CONFLICT` |
| `GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME` | A campaign row with a measured session count has campaign `(not set)` and a source and medium that are none of `(not set)`, `(other)`, `(direct)`, `(none)` |
| `GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE` | `window_end` is on or after the observation instant's date in the report's property timezone; never evaluated for Realtime |
| `GA4_REPORT_REALTIME_NOT_PROCESSED` | The attribution label is `GA4_REALTIME` |
| `GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED` | The report has rows and none carries the aggregate completeness extension (every standard or Realtime report with rows) |
| `GA4_NO_KEY_EVENTS_CONFIGURED` | The Admin inventory lists no key event |
| `GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED` | One finding per key event whose counting method is `COUNTING_METHOD_UNSPECIFIED` |
| `GA4_EVENT_RULES_CONTENT_NOT_INSPECTED` | The Admin inventory lists at least one event create or edit rule |
| `GA4_ADMIN_INVENTORY_STALE` | The Admin observation is older than `maximum_observation_age_seconds` at `as_of` (exactly the maximum is fresh) |
| `GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN` | A `KEY_EVENT_TOTALS` report declared a different number of key events than the Admin inventory lists; cites both records. Counts only, because the inventory holds name fingerprints |
| `GA4_ADMIN_INVENTORY_NOT_RETAINED` | No Admin observation was supplied |
| `GA4_ATTRIBUTION_SETTINGS_NOT_READ`, `GA4_DESTINATION_AND_LINKS_NOT_READ`, `GA4_POLICY_SETTINGS_NOT_READ`, `GA4_DRIFT_NOT_EVALUATED_BY_AUDIT`, `GA4_KEY_EVENT_NOT_A_PROVIDER_CONVERSION`, `GA4_BROWSER_NOT_APPLICABLE` | Always: each states a coverage limit |

An absent row is never a zero. A key event GA4 reports as `0` is `OBSERVED` and
raises no finding; one GA4 returned no row for is either withheld (the report was
thresholded) or not returned (it was not), and the two are different findings.

## Recommendations

Every recommendation is `OBSERVE`, `NOT_EXECUTED`, `NOT_A_PLAN` and carries no
authorization digest, plan reference or spend impact.

| Recommendation | Findings | Why a read helps |
| --- | --- | --- |
| Repeat the bounded read | incomplete, provider-declined, no rows | A second compatible read shows whether the missing rows were real absence or a read that stopped early |
| Repeat the read for consistent sampling | pages disagree on sampling metadata | A second compatible read shows whether GA4 keeps reporting inconsistent sampling or the difference was transient. The Data API documents no snapshot isolation between pages |
| Take a processed read later | key event not returned, window includes the observation date, Realtime | A processed read after GA4's normal processing interval separates a processing delay from an event that does not occur. GA4 documents no processing guarantee |
| Re-read the Admin inventory, then the key-event aggregate | stale inventory, key-event count differs | Two close reads bound the observation interval of any difference. The actor and exact time of a change stay unknown |
| Read a fixed aggregate shape | completeness facts not retained | The aggregate read retains the facts that tell a withheld value from a zero |

Declined outcomes use `NoRecommendation`:

- `EVIDENCE_INSUFFICIENT` for thresholding, sampling, the `(other)` roll-up,
  restricted metrics, a flagged roll-up or restriction the report cannot
  attribute, and withheld key events. These describe how GA4 answered, so
  repeating the read does not change them.
- `EVIDENCE_INSUFFICIENT`, considering `CREATE`, for a property with no key
  events. Creating one needs an event contract the customer chooses; nothing in
  the evidence identifies it. Also for unresolved joins, untagged sessions, an
  unspecified counting method, opaque event rules and a missing Admin inventory,
  which need a customer declaration, a review in the GA4 interface or the
  advertising provider, or a read that has not been retained.
- `COVERAGE_NOT_READ` for the coverage-limit findings.

`COMPILABLE_OPERATION_KINDS` carries `"ga4": frozenset({"CREATE"})` because this
build compiles one GA4 Admin operation, the fixed key-event `CREATE`
(`providers/ga4_admin_compiler.py`, `ga4_key_event_candidates.py`,
`saved_ga4_create_plans.py`, `ga4_create_execution_workflow.py`). The registry
states what the product can compile; the GA4 rule table states what the audit
recommends, and that is `OBSERVE` only. It declines `CREATE` as
`EVIDENCE_INSUFFICIENT` because the retained evidence does not identify which
event the customer counts. The `ProviderRecommendation` validator still rejects
any other GA4 operation kind (for example `PAUSE`), which the product cannot
compile. More findings than one row can name (a report
may declare up to fifty key events, each its own finding) are bounded with an
explicit `BoundExceeded` entry; nothing is dropped.

## Evidence limits

- **Fixture-only.** Every test uses `httpx.MockTransport` or retained fixture
  records. No GA4 request was made and no property was queried.
- **No GA4 interface reconciliation**, no Realtime or processed-report
  verification, and no quota evidence. Those remain `L01` and `M03` work.
- **No exhaustiveness claim.** `AnalyticsCompleteness` still has no exhaustive
  member, and a report the adapter itself calls exhaustive is audited as
  `BOUNDED_NON_EXHAUSTIVE`: the Data API documents no snapshot isolation between
  pages.
- **Counts, not names, across reads.** The Admin inventory keeps key-event names
  as keyed fingerprints and the audit holds no key, so a report's key events are
  compared with the inventory by count only.
- **Empty reports lose report-level facts.** The aggregate completeness facts are
  persisted on rows, so a zero-row report keeps only its envelope statuses. A roll-up or restriction
  on such a report, on every standard read, and on an aggregate whose facts name none
  is reported as flagged by GA4 without saying which it was
  (`GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN` for a standard or Realtime read with rows,
  `GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE` otherwise).
  Persistence is not changed here.
- **No attribution model, freshness or consent state.** The Data API returns
  none, and the Admin attribution-settings read is not performed.
- **No drift here.** Out-of-band Admin changes are compared by `measurement
  compare`.

## Tests

`tests/test_ga4_audit.py` drives the real Data API adapter through an in-memory
transport and covers every rule with a positive and a negative case: thresholded
versus not-returned versus observed-zero key events, sampling, roll-up,
restriction, page-bound exhaustion, provider refusal, empty and empty-thresholded
reports, the property-timezone date boundary (an evening UTC date that is still the
local date), an invalid timezone refusal, join states, untagged sessions, standard
and Realtime reports, Admin findings, the exclusive staleness boundary, the
count cross-read in both directions, recommendation shape, the inline bound,
determinism and the no-request, no-append property, the provider-neutral coverage
correction (parametrized over GA4, HubSpot and Salesforce outcome rows), the flagged
roll-up and restriction on standard and zero-row reads, inconsistent versus
consistent page sampling, the GA4 compilable-operation entry, and refusal of
evidence for another property, another provider or a time after the audit.
`tests/test_ga4_audit_route.py` covers evidence selection and the CLI route end to
end: latest and explicit-digest selection, absence, another property's
observation, an explicit `--account` that is not the report's property (with and
without a retained observation), an observation finished after `--at`, malformed
and unretained digests, redaction, paging and an unchanged ledger.

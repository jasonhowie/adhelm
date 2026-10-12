# Deterministic GA4 <-> provider campaign joins (M00.2)

Implements the "deterministic joins" line of
[definition-of-done.md](../definition-of-done.md) section 2 (line 102) —
"Deterministic joins work; heuristic, conflicting, and unmapped rows remain
visible and excluded from authoritative totals" — on top of the M00.1 fixed
aggregate rows in [ga4-data-aggregates.md](ga4-data-aggregates.md), and the
side-by-side half of section 6 (line 249).

The join contract itself is
[reporting-schema.md](../reporting-schema.md#join-contract):
name-only, fuzzy, or inferred joins are never automatic authoritative joins.

## Scope

| Surface | Status |
| --- | --- |
| `src/adhelm/campaign_joins.py` | New. Declared contract, resolution engine, content-addressed join map. |
| `src/adhelm/saved_campaign_joins.py` | New. Retention and bounded selection of retained maps. |
| `src/adhelm/join_cli.py` | New. `measurement join create` / `catalog` / `show`. |
| `Ga4DataClient.run_aggregate_report` | One optional `join_map=` keyword. No request, host, or scope changes. |
| `Ga4AggregateRow` / `PerformanceRow` / `ConversionBreakdownRow` | Additive join fields and allowlisted extensions. |
| `measurement_comparison.build_side_by_side_comparison` | New, additive. The existing `compare_measurements` contract is unchanged. |
| `ledger.py` | Additive: the `CAMPAIGN_JOIN_MAP_RETAINED` record kind, its envelope-binding validator, and its chain validator on append and on scan. |
| `projections.py` | Additive: pseudonymize the customer-supplied `provider_campaign_name` on export. |
| `cli.py` | Additive: the two schemas and the three `measurement join …` command names. |
| GA4 Admin writes, CRM aggregates, live reconciliation | Out of scope. |

## Join keys are declared, never guessed

`CampaignJoinContract` is a closed customer declaration. It binds one GA4
property (by retained property reference and timezone) and one or more
advertising accounts, and lists `DeclaredCampaignJoin` entries. Each entry
names a **retained provider campaign reference** — the same opaque reference an
inventory read already persists — and the exact GA4 dimension values the
customer says belong to it.

`Ga4JoinDimensionKey.match_kind` selects which values are declared:

| `match_kind` | Declared values | Matches when |
| --- | --- | --- |
| `SESSION_CAMPAIGN_NAME` | `session_campaign_name` | GA4's `sessionCampaignName` is byte-equal |
| `SESSION_SOURCE_MEDIUM` | `session_source`, `session_medium` | both are byte-equal |
| `SESSION_CAMPAIGN_NAME_SOURCE_MEDIUM` | all three | all three are byte-equal |
| `UTM_CAMPAIGN_ID` | `utm_campaign_id` | GA4's `sessionCampaignName` is byte-equal to the declared controlled campaign ID |

Every comparison is exact and case-sensitive. No trimming, casefolding, Unicode
folding, punctuation stripping, prefix matching, or time-proximity heuristic
exists in this module.

`UTM_CAMPAIGN_ID` is the "controlled UTM campaign id convention" tier: the
customer declares that their `utm_campaign` value carries the advertising
platform's campaign ID, and GA4 reports that value back through
`sessionCampaignName`. It resolves to `EXACT_CONTROLLED_KEY` and populates
`campaign_join_key`; the other three kinds resolve to `EXPLICIT_MAPPING`.

A GA4 sentinel — `(not set)`, `(other)`, `(direct)`, `(none)` — is refused as a
declared key, and a row carrying one in its campaign, source, or medium is not
presented for joining at all. It is a provider aggregation or absence marker,
not a campaign identity. Such a row is not lost: it keeps `UNMAPPED`, the
`UNMAPPED` quality status, and the deferred join disclosure on the report
itself.

## The heuristic tier proposes; it never joins

`HeuristicJoinProposal` is emitted when a presented GA4 row is not claimed by
any declared key and its `sessionCampaignName` is byte-equal to an entry's
optional `provider_campaign_name`. The proposal carries
`status = HEURISTIC_PROPOSED` and `applied = false`, and the GA4 row's
resolution is `HEURISTIC` with its candidate references listed. Nothing is
joined: `join_map.joined_resolutions()` excludes it, no provider campaign
reference is minted on the row, and the side-by-side view counts it under
`excluded_join_status_counts`.

A customer accepts a proposal by adding the exact GA4 values to the contract
with `join_source = "ACCEPTED_HEURISTIC"`. That is then a declared join
(`EXPLICIT_MAPPING`) whose origin stays visible through `join_source`.

### Verified negative result: no connector retains a provider campaign name

No advertising connector in this product retains an advertising campaign
**name** in local history. `GoogleAdsCampaign`, `MetaAdsCampaign`,
`LinkedInAdsCampaign`, `TikTokAdsCampaign`, `RedditAdsCampaign` and
`OpenAIAdsCampaign` retain opaque references plus status, objective, budget,
bid and schedule facts only, and `inventory._snapshot` persists the same
subset. OpenAI's insight parser allows a `campaign_name` response key at the
read boundary but never stores it.

The heuristic tier therefore compares GA4's dimension value with a name the
**customer supplied in the contract**, and each proposal records
`name_provenance = CUSTOMER_SUPPLIED_PROVIDER_CAMPAIGN_NAME_NOT_RETAINED_HISTORY`
so that provenance is never mistaken for a retained provider fact. Making
provider campaign names part of retained inventory is a separate, deliberate
schema decision that this ticket does not take.

## Every state is explicit

`resolve_campaign_joins` resolves each presented GA4 dimension tuple to exactly
one `CampaignJoinResolution`:

| `join_status` | Meaning | Joined? |
| --- | --- | --- |
| `EXPLICIT_MAPPING` | Exactly one declared dimension key matched | yes |
| `EXACT_CONTROLLED_KEY` | Exactly one declared `UTM_CAMPAIGN_ID` key matched | yes |
| `HEURISTIC` | Only a name-equality proposal exists | no |
| `CONFLICT` | Contradictory identity, in either direction | no |
| `UNMAPPED` | No declared key and no proposal | no |

Conflict detection runs in both directions:

- `MULTIPLE_PROVIDER_CAMPAIGNS_DECLARE_ONE_GA4_VALUE` — two or more declared
  provider campaigns match one GA4 dimension tuple.
- `ONE_PROVIDER_CAMPAIGN_MATCHES_SEVERAL_GA4_VALUES_WITHOUT_DECLARED_MULTI_MAPPING`
  — one declared provider campaign matched several distinct GA4 tuples while
  `multi_mapping_declared` is false. A campaign that legitimately owns several
  GA4 values must say so; otherwise a broad `SESSION_SOURCE_MEDIUM` declaration
  would silently absorb every campaign on that source/medium.

A conflicting row carries `candidate_provider_campaign_refs` so the customer can
see what collided, and no `provider_campaign_ref`. There is no tie-break rule,
no "first declaration wins", and no silent merge.

A declared campaign whose GA4 values never appeared is reported in
`unmatched_declared_campaign_refs` rather than dropped.

## Determinism, content addressing, and restatement

- Presented rows are de-duplicated and sorted by
  `(sessionCampaignName, sessionSource, sessionMedium)`; declared entries are
  iterated in sorted `provider_campaign_ref` order and their keys in sorted key
  order. The map's model validator re-checks that ordering, so a hand-built map
  cannot smuggle in a different iteration order.
- `content_digest` is the RFC 8785 digest of the exact contract.
- `join_map_digest` is the digest of `{content_digest, boundary_status,
  resolutions, proposals}` and is re-derived by the model validator, so a
  tampered digest is refused rather than trusted.
- `CampaignJoinContract.version` starts at 1, and every later version must name
  its predecessor's `content_digest` in `supersedes_ref`. `validate_predecessor`
  refuses a skipped version, a renamed contract, a wrong digest, a rebound GA4
  property, and a rebound advertising account, so a restatement is visible
  instead of overwriting or re-targeting the prior declaration. A restatement
  revises which GA4 values belong to which campaign; binding a different
  property or account needs a new contract reference.
- The map is bound to `ga4_property.property_ref` and to the declared provider
  account references. `apply_join_map` and `build_side_by_side_comparison`
  refuse a report from a different property or an unbound account.

## Privacy is re-validated at this boundary

GA4 campaign names are free text and can contain an email address. The GA4 read
boundary already refuses one (`_bounded_string` runs `validate_data` on every
dimension value before it reaches a model). This module re-validates anyway,
because a caller can reach `resolve_campaign_joins` with rows it assembled
itself:

- `CampaignJoinContract` validates its whole canonical dump, so a declared key
  containing an email, phone number, or secret marker is refused with
  `SENSITIVE_DATA_RISK`;
- `resolve_campaign_joins` validates the presented rows before matching; and
- the retained record passes `validate_data` again inside `Ledger.append`.

## Timezone and date boundaries are a typed status, never aligned

A GA4 property and an advertising account can keep different timezones, so their
calendar-day boundaries are different instants. The contract declares both, and
the map records the fact rather than shifting either source:

```text
boundary_status      = ALIGNED | TIMEZONE_MISMATCH
timezone_mismatches[] = { provider, account_ref,
                          ga4_property_timezone, provider_account_timezone,
                          status = TIMEZONE_MISMATCH,
                          alignment = NOT_ALIGNED_DATE_BOUNDARIES_DIFFER }
```

`TIMEZONE_MISMATCH` propagates to every resolution's
`data_quality_statuses`, to the joined GA4 rows, and to each side-by-side row.
No date is re-bucketed, no window is re-cut, and no measurement is prorated.

## Reporting projection

`apply_join_map(report, join_map)` rebuilds one `Ga4AggregateReport` with the
resolved rows. It changes no request and is applied after the read is complete,
so a join failure can never be mistaken for a provider read failure. Applying a
second, different map to an already-resolved report is the typed refusal
`JOIN_MAP_ALREADY_APPLIED`: two declarations cannot both own one report's rows.

Additive `Ga4AggregateRow` fields:

```text
join_status            (now including HEURISTIC)
join_source            DECLARED | ACCEPTED_HEURISTIC, only on a declared join
join_map_digest        the exact map that resolved the row
provider_campaign_ref  only on a declared join
campaign_join_key      only on a controlled-key join
join_disclosure        DETERMINISTIC_PROVIDER_JOIN_DEFERRED
                     | DETERMINISTIC_PROVIDER_JOIN_APPLIED_FROM_DECLARED_JOIN_MAP
```

The model refuses a row that claims a provider campaign without a join map
digest, a resolved row without the applied disclosure, and a row whose
`UNMAPPED` quality status disagrees with its join status. A report cannot mix
two join maps.

`to_normalized_report()` then sets `PerformanceRow.campaign_ref` /
`campaign_join_key` from the resolution and persists
`ga4_join_source` and `ga4_join_map_digest` as allowlisted extensions, so a
retained row stays attributable to the exact declaration that joined it.
`resource_level` stays `ACCOUNT`: GA4 reports property traffic, and the join
records which provider campaign the customer says that traffic belongs to. It
does not turn a GA4 row into a provider campaign resource.

### Deliberately not emitted

A campaign-scoped `ConversionBreakdownRow` for the
`CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT` shape is still not emitted. The joined
`PerformanceRow` already carries `key_events` per campaign, and a second
conversion row for the same measurement would be an ambiguous duplicate rather
than new evidence.

## Side by side, with no derived cross-source measure

`build_side_by_side_comparison` places one retained advertising report next to
one retained, joined GA4 report. Each source keeps its own fields, window,
timezone, and attribution label:

```text
provider_impressions / provider_clicks / provider_spend
provider_window_start / provider_window_end / provider_timezone / provider_attribution_model
ga4_sessions / ga4_key_events / ga4_dimension_row_count
ga4_window_start / ga4_window_end / ga4_timezone / ga4_attribution_model
boundary_status, data_quality_statuses
causal_claim = false, derived_cross_source_metrics = false
disclosure = SOURCES_SIDE_BY_SIDE_NO_CAUSAL_OR_INCREMENTALITY_CLAIM
```

There is no ratio, cost-per-outcome, share, blended total, or attributed-credit
field, and no field is computed from both sources. `refuse_cross_source_derivation`
scans every emitted payload and fails closed with
`CROSS_SOURCE_DERIVATION_PROHIBITED` on a derived or causal term (`incremental`,
`causal`, `lift`, `roas`, `cpa`, `cost_per_*`, `conversion_rate`, `ratio`,
`per_session`, `attributed_credit`, `blended`, `true_conversions`, …). The only
fields allowed to name such a concept are the explicit negations above, and only
with their declared values.

The guard covers what the **product** writes: every field name, and the string
values of the closed label fields listed in `_PRODUCT_LABEL_KEYS`
(`join_status`, `join_source`, `match_kind`, `basis`, `name_provenance`,
`conflict_reasons`, `boundary_status`, `data_quality_statuses`, …). Retained
source data is never scanned. An advertising campaign may legitimately be named
`brand-lift-q4` or `q4-cpa-test`; GA4 returns that name verbatim, the read
boundary retains it verbatim, and censoring it would make a lawfully retained
record permanently unreviewable — a different and worse failure than the one
this guard exists to prevent.

`boundary_status` on the view is derived from the **retained reports being
shown**, not only from the declared account timezones: it is
`TIMEZONE_MISMATCH` when the contract declared one, when the two retained
reports were read in different timezones, or when the GA4 report's timezone
differs from the property timezone the contract declares.

Summing happens **within one source only** — provider daily rows for one
campaign, or GA4 dimension rows joined to one campaign — and a `null`
contributing cell makes the total `null`. A withheld or never-returned
measurement never becomes a zero beside a provider number.

## CLI

```text
measurement join create --input <contract.json> --ga4-report <record_digest> [--retain]
measurement join catalog [--cursor N] [--limit N]
measurement join show <record_digest> [--section rows|proposals] [--cursor N] [--limit N]
                                     [--provider-report <digest> --ga4-report <digest>]
```

`create` resolves the declared contract against the **exact dimension values
already retained** by an M00.1 GA4 aggregate read, so no GA4 request is made.
It refuses a non-GA4 report (`JOIN_SOURCE_INVALID`), a report from another
property (`JOIN_MAP_PROPERTY_MISMATCH`), a property-timezone disagreement
(`TIMEZONE_MISMATCH`), and a version-2+ contract without a retained,
exactly-named predecessor (`JOIN_MAP_CHAIN_INVALID`). Its receipt reports the
status counts, every conflict, every proposal (with `applied: false`), the
unmatched declared campaigns, and the timezone mismatches.

`show` renders the retained map's resolutions plus every heuristic proposal
with its `basis` and `name_provenance`, so a `HEURISTIC` row is reviewed as a
name coincidence rather than a retained provider fact. It optionally renders
the side-by-side view from two retained reports.

`--section` declares which retained list defines exhaustion — `rows` (the
default) or `proposals` — and `total_count`/`next_cursor` follow it. One cursor
window is applied to both lists, and `resolved_row_count` and
`heuristic_proposal_count` are always disclosed, so a reader can tell whether
the other list still has entries. When a page exceeds the configured output
bound the **window itself shrinks** rather than the tail of one list being
dropped, so the next page starts exactly where the previous one ended and no
retained resolution or proposal becomes unreachable.

`create` preflights its receipt with the exact `persisted` value and a
fixed-length placeholder digest, so the measured envelope is byte-identical in
size to the emitted one and a receipt that fits the bound precisely is not
refused.

The `discover` block stays truthful: these commands are fixture-verified and
open no socket.

### Retention is bound, chained, and explicit

`CAMPAIGN_JOIN_MAP_RETAINED` is registered in `src/adhelm/ledger.py` and
`measurement join create|catalog|show` is registered in `src/adhelm/cli.py`, so
retention and review are installed capabilities of this build.

Two ledger validators, mirroring the conversion-definition ones, keep the
retained history honest:

- `Ledger._validate_campaign_join_binding` refuses a record whose envelope does
  not name `provider = "ga4"`, `account_ref = contract.ga4_property.property_ref`
  and a local `LOCAL_CLI` declaration. A join map cannot be filed under an
  unrelated advertising account, and it cannot claim a remote observation time
  or an execution run.
- `Ledger._validate_campaign_join_history` runs under the changes-stream lock at
  append **and** over every verified historical prefix on scan. One contract
  reference has exactly one version-one declaration; every later version must
  extend the latest retained one with its exact content digest, a strictly later
  effective time, and the same GA4 property and advertising accounts. A
  competing registration is refused with `JOIN_MAP_CHAIN_INVALID` instead of
  forking the chain and shadowing a retained declaration that no selector could
  then reach.

`join_map_retention_installed()` remains a read-only check of the registered
record kind, so a build that removed it fails closed with
`JOIN_MAP_RETENTION_NOT_INSTALLED` rather than dropping the record silently.
`measurement join create` runs every refusal -- contract validation, property
and timezone binding, chain validation, the derived-measure guard, redaction
and the output bound -- **before** the durable append, so a `FAILED_CLOSED`
receipt can never leave a retained record behind (AGENTS rule 20).

### Customer-supplied campaign names never leave in cleartext

The join contract is the only record in this product that retains an
advertising campaign *name*. `projections._is_customer_supplied_name_field`
pseudonymizes `provider_campaign_name` with the same keyed opaque reference as
a provider identifier, so a local export keeps the name stable across runs
without revealing it. `measurement join show` surfaces each proposal's
`basis` and `name_provenance` so a `HEURISTIC` row is reviewable as a name
coincidence rather than a retained provider fact.

## Evidence limits

- **Fixture-only.** No socket is opened, no credential is read, no GA4 property
  and no advertising account is queried. `tests/test_ga4_campaign_joins.py`
  drives the one adapter test through `httpx.MockTransport`.
- **No live reconciliation.** GA4-UI and provider-UI reconciliation of a joined
  campaign is live work under `L01`; nothing here evidences it.
- **The declaration is not verified to be true.** The product records which GA4
  values the customer says belong to a campaign. It does not confirm that the
  campaign's traffic actually carried those UTMs, and it never infers the
  mapping.
- **Heuristic proposals depend on a customer-supplied name**, because no
  provider campaign name is retained. A proposal is evidence of a name
  coincidence, nothing more.
- **Retention is evidenced offline only.** The record kind, its envelope
  binding and its chain validator are exercised against a local ledger in a
  temporary directory; no shared or long-lived history is involved.
- **No CRM source is joined.** HubSpot and Salesforce aggregates stay out of
  scope here.

## Tests

`tests/test_ga4_campaign_joins.py` covers: declared name, source/medium,
combined and controlled-key joins; an accepted heuristic; case and whitespace
never normalizing into a join; sentinel refusal; a proposal that is reported and
never applied; a proposal losing to an explicit declaration; the disabled
proposal tier; both conflict directions and the declared multi-mapping that is
not a conflict; unmapped rows and unmatched declared campaigns; the timezone
mismatch status through resolutions and side-by-side rows; byte-identical
determinism under reordered input and a refused tampered digest; the privacy
validator on the contract, the presented rows and the retained payload; the
predecessor chain and its refusals; the adapter seam producing an identical
request with and without a map; the normalized projection carrying join state
into history; a null provider measurement never becoming zero beside GA4; a grep
over the whole emitted payload for every derived or causal term; the retention
refusal and the retained chain through the documented ledger record kind; and
`measurement join create` / `catalog` / `show` end to end.

`tests/test_ga4_campaign_joins_review.py` is the independent M00.2 review
suite. It pins that an ordinarily named campaign stays reviewable and
reportable, that a failed-closed `create` leaves no retained record, that a
second version-one map for one contract is refused rather than forking the
chain, that a restatement cannot rebind the GA4 property, that two retained
reports read in different timezones are not presented as aligned, that a local
export never ships a customer-supplied campaign name in cleartext, that the
retained envelope binds its GA4 property, that `show` surfaces proposal
provenance, that `measurement join create` is in the shared input-file
hardening battery, that the shipped documents do not understate the installed
capability, and that a second join map over one report is a typed refusal.

`tests/test_ga4_campaign_joins_rereview.py` pins the two residuals of that
round: every retained heuristic proposal is reachable by paging
`measurement join show`, and the retain preflight bounds the receipt exactly.

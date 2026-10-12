# Capability matrix

Last updated: 2026-10-10

The post-v1 CRM metadata and bounded website-discovery design in
[`future-conversion-architecture.md`](future-conversion-architecture.md) is
intentionally absent from this v1 matrix. It requires its own researched matrix
before implementation or a public claim.

Phase 0 first-party research is recorded in
[`provider-research/2026-09-04-provider-access.md`](provider-research/2026-09-04-provider-access.md).
That document is research, not fixture or live evidence. Advertising operation
cells below remain unchanged because the stable matrix has no research-only
column and reading documentation does not prove an operation.

## Release policy for 0.1.0

Every write row below describes code that ships and is refused at runtime. The
0.1.0 release-policy gate
([`release-policy-gate.md`](implementation/release-policy-gate.md)) refuses each
remote-mutation command with `WRITES_NOT_RELEASED` (exit `3`) before
configuration, input, credential-store, ledger `APPLY_STARTED` or egress. A
`FIXTURE_VERIFIED` write row is therefore not an available capability in 0.1.0,
and `adhelm discover` lists those commands as `UNRELEASED`. The bundled skills and
`guidance` text state this refusal and tell the agent to hand the change to the
marketer ([skills and guidance](implementation/skills-and-guidance.md)); they are
not provider capabilities and promote no row.

## Release labels for 0.1.0

Each released provider also carries a release label, `LIVE_PILOTED` or
`EXPERIMENTAL` ([`provider-release-labels.md`](implementation/provider-release-labels.md)).
The label states whether a committed, redacted live read pilot record
([`pilots/`](pilots/README.md)) exists for this build. It does not change any status
in this matrix: a `LIVE_PILOTED` provider's rows stay at the status their own evidence
supports, and no row is promoted because a pilot ran. No provider is `LIVE_PILOTED`
until the owner commits a record, so today every provider is `EXPERIMENTAL`.

## 0.1.1 read-pilot corrections

The [read-pilot patch](implementation/read-pilot-fixes.md) corrects Google Ads
v25 schedule field selection, adds separate primary/all-conversion aggregates
with exact fractional counts, and adds explicit opt-in campaign labels. A
pinned official field extract checks Google field names across read/planning
paths. Structured error codes/request identifiers, terminal OAuth handoff, and
packaged release-state checks have offline regression coverage. These changes
promote no whole operation group and supply no live evidence.

## Purpose

This is the release traceability ledger for provider feasibility and evidence.
It starts conservatively: knowledge from the maintainer's separate production
project may guide clean-room research, but that project's credentials, accounts,
live results, and fixtures do not prove this product's capability.

Every implementation PR that changes a connector capability updates this file
or its future machine-readable equivalent. A marketing or release claim may not
exceed the lowest evidenced status of its required operation.

## Stable statuses

| Status | Meaning |
| --- | --- |
| `UNRESEARCHED` | No current first-party-source review recorded for this product |
| `DOCUMENTED` | Current official API/access behavior and constraints are recorded |
| `FIXTURE_VERIFIED` | Implementation passes sanitized fixture and conformance tests |
| `LIVE_VERIFIED` | Behavior is proven on the required independent live accounts |
| `BLOCKED` | An external access/provider constraint prevents proof; exact next action is recorded |

`BLOCKED` is not parity and never counts as release completion. A row is
`LIVE_VERIFIED` only with an evidence-manifest reference, tested API version,
verification date, account pseudonym, and UI reconciliation where applicable.

## Required operation groups

Each advertising connector must independently evidence:

1. `AUTH`: authorization, refresh/reauthorization, revocation guidance, scopes,
   account discovery, and deterministic identity.
2. `READ`: paginated inventory, normalized reports, freshness, currency,
   timezone, attribution metadata, quotas, and version behavior.
3. `AUDIT`: structure, delivery, spend, bid, targeting, placement, creative,
   destination, tracking, conversion, policy, and drift diagnostics.
4. `RECOMMEND`: deterministic evidence references, observations separated from
   inference, assumptions, confidence, risk, expected/spend impact, and no
   automatic write.
5. `COMPILE`: typed campaign-brief compilation and explicit unsupported
   mappings before any write.
6. `PLAN_APPLY_SAFETY`: immutable account-bound plan, exact expiring authorization
   (direct user authorization or a decision under explicit bounded delegation),
   pre-apply identity/state re-read, drift rejection, spend/egress bounds,
   pre-egress ledger event, one-use execution, and post-apply verification.
7. `CREATE`: complete provider-native hierarchy in paused/draft state, including
   required child objects, customer-supplied creative, destination, tracking,
   budget, bid, schedule, targeting, placement, and conversion configuration.
8. `VALIDATE`: pre-activation destination/redirect, controlled UTM, provider
   conversion reference, GA4 expectation, and configured GTM verification.
9. `ACTIVATE`: separate spend-explicit authorization, activation, and verification.
10. `MAINTAIN`: pause/archive and guarded supported changes or safe replacement
   for status, spend controls, bid, schedule, targeting, destination, tracking,
   and creative association.
11. `RECOVER`: idempotency/reconciliation, partial and ambiguous outcomes,
   restoration/replacement, and redacted evidence.
12. `HISTORY_EXPORT`: allowlisted snapshots, planned/attempted/applied/verified/
   recovered/out-of-band change events, append-only JSONL integrity, and
   deterministic CSV projection.
13. `CREATIVE_CAPTURE`: exact provider copy/CTA/assets, local visual/palette/text
    analysis, stable creative versions, effective-dated bindings, and correctly
    granular creative-performance reporting.

## Initial advertising-platform matrix

No complete production operation group or live workflow has been evidenced.
The table below remains the conservative group-level release matrix. The
following narrower implementations must not promote their whole operation group:

| Provider/surface | Exact implemented subset | Evidence level | Evidence |
| --- | --- | --- | --- |
| Google Ads v25 | Fixed customer identity; paginated campaign/ad-group/ad inventory; fixed daily campaign and native-ad impressions/clicks/USD cost and separate primary/all-conversion counts and values, exact micros preserved beside the half-even cents; canonical int64/daily-date and account-timezone campaign date-time wire spellings; a repeated resource-day refused; the customer-local open day marked `PROCESSING_PENDING`; a day without a row left absent, never zero-filled; typed `429`/`401`/`403` classification with bounded Cloud-project error codes and screened diagnostics; non-USD customers refused | `FIXTURE_VERIFIED` | [read contract](research/google-ads-read-contract.md), `tests/test_google_ads.py`, `tests/test_google_ads_read_completeness.py`, `tests/test_google_ads_read_integrity.py`, `tests/test_inventory.py`; fixture-only with `httpx.MockTransport`. No live request, no provider-UI reconciliation, and no promotion of the Google Ads `READ` operation group. Quota values, `Retry-After` behavior, zero-activity-day behavior and the no-end-date representation are `UNVERIFIED` |
| Meta Marketing API Graph v26 | Fixed account identity; paginated campaign/ad-set/ad inventory with campaign objective/buying-type/bid-strategy enums, ad-set budget mode, schedule and targeting **presence**, ad creative association and image-hash/link **presence**, and documented `effective_status`/review state; fixed daily campaign/ad impressions/clicks/spend/reach at declared grain, with ASCII-only number and date grammars, a repeated resource-day refused, the account's open calendar day marked `PROCESSING_PENDING`, and absent or `null` metrics refused rather than read as zero; typed Graph rate-limit classification for a `200` error envelope and for HTTP `401`/`403`/`429` | `FIXTURE_VERIFIED` | [read contract](research/meta-ads-read-contract.md), `tests/test_meta_ads.py`, `tests/test_meta_read_completeness.py`, `tests/test_meta_independent_review.py`, `tests/test_inventory.py`; **retention** is presence-only, the request is not: the fixed read asks Graph for the whole `targeting` object and for `image_hash`, `link_url`, and `object_url`, reduces each to a boolean in memory, and retains no value. Ad copy (`object_story_spec`, `title`, `body`, `asset_feed_spec`) is never requested at all. A local privacy screen matching a returned creative value withholds that one fact; matching anywhere else fails the read closed. Fixture-only with `httpx.MockTransport`: no live request, no provider-UI reconciliation, no promotion of the Meta Ads `READ` operation group. A Graph rate-limit code delivered under an HTTP `4xx` status is not typed (the shared transport extracts screened numeric codes but the Meta adapter does not classify those HTTP error codes), non-reviewed currencies are refused, and Meta's settling time for a closed day, quota values and archived-object listing are `UNVERIFIED` |
| LinkedIn Marketing API 202608 | Fixed account, campaign-group, campaign, and scoped creative inventory; fixed daily campaign and native creative-pivot aggregate reads, always bounded and non-exhaustive (the Ad Analytics finder documents no pagination); typed `429` rate-limit and permission-naming `401`/`403` classification from the observed status alone; repeated resource-days refused; an open current UTC day marked `PROCESSING_PENDING`; USD only, other currencies refused | `FIXTURE_VERIFIED` | [read contract](research/linkedin-ads-read-contract.md), `tests/test_linkedin_ads.py`, `tests/test_linkedin_read_completeness.py`, `tests/test_inventory_new_providers.py`; fixture-only with `httpx.MockTransport`. No live request, no provider-UI reconciliation, and no promotion of the LinkedIn `READ` operation group. Quota values, `Retry-After` behavior, finder default status coverage and zero-day behavior are `UNVERIFIED` |
| TikTok Marketing API v1.3 | Fixed advertiser/campaign/ad-group/ad inventory; fixed daily campaign/ad impressions/clicks/spend reads | `FIXTURE_VERIFIED` | [read contract](research/tiktok-ads-read-contract.md), `tests/test_tiktok_ads.py`, `tests/test_inventory_new_providers.py` |
| Reddit Ads API v3 | Fixed account/campaign/ad-group/ad inventory retaining identity, parents, declared status and the campaign objective only; fixed daily campaign/ad report request and one-response parsing that fails closed on a signalled continuation, a repeated resource-and-day row, or a missing metric, and keeps exact native spend micros | `FIXTURE_VERIFIED` | [read contract](research/reddit-ads-read-contract.md), `tests/test_reddit_ads.py`, `tests/test_reddit_ultra_review.py`, `tests/test_reddit_read_completeness.py`, `tests/test_inventory_new_providers.py`; report continuation is `NOT_DOCUMENTED` and is neither guessed nor claimed, so no Reddit report is exhaustive |
| Reddit Ads local OAuth lifecycle and offline provider audit | `auth reddit-token enroll/refresh/status/remove-local` for a confidential developer-application client (HTTP Basic with the app ID and secret, the secret taken from a hidden prompt and kept in its own credential slot, `duration=permanent`, local removal of the secret, refresh token and state with provider-revocation guidance and no revocation call); deterministic 28-rule table over the retained Reddit report, one completed inventory read, an optional earlier read, and local creative-binding records, with an explicit coverage status per audit dimension derived from `REDDIT_ADS_READ_SURFACES` and non-writing recommendations of `OBSERVE` only -- every other considered kind appears as an explicit declined row | `FIXTURE_VERIFIED` | [OAuth contract](research/reddit-oauth-contract.md), [token lifecycle](implementation/reddit-token-lifecycle.md), [audit rules](implementation/reddit-audit-recommendations.md), `tests/test_reddit_oauth_contract.py`, `tests/test_reddit_token_lifecycle.py`, `tests/test_reddit_audit.py`, `tests/test_reddit_audit_route.py`; fixture-only with `httpx.MockTransport` and a fake credential store. No live Reddit request, no registered Reddit application, no provider-UI reconciliation, the loopback redirect and token-endpoint behavior are `UNVERIFIED`, targeting/bids/budgets/schedules/placement/ad content/ad review state/destinations/tracking/conversions are not retained so those dimensions are `NOT_READ`, and no promotion of the Reddit Ads `AUTH`, `READ`, `AUDIT` or `RECOMMEND` operation group |
| OpenAI Ads v1 | Account identity, bounded campaign/ad-group/ad hierarchy, fixed daily campaign/native-ad impressions/clicks/USD spend | `FIXTURE_VERIFIED` | [research and provenance](research/openai-clean-room-reference.md), [hierarchy read contract](research/openai-hierarchy-read-contract.md), `tests/test_openai_ads.py`, `tests/test_inventory.py`, `tests/test_openai_inventory_money.py` |
| GA4 Data v1beta and readonly Admin identity | Fixed allowlisted aggregate reports, declared-event realtime reads, property/web-stream identity | `FIXTURE_VERIFIED` | [Data API contract](research/ga4-data-aggregate-contract.md), [Admin read contract](research/ga4-admin-read-contract.md), `tests/test_ga4.py`, `tests/test_reporting.py` |
| GA4 declared campaign joins | Customer-declared GA4 dimension-to-provider-campaign contract, exact case-sensitive resolution to `EXPLICIT_MAPPING`/`EXACT_CONTROLLED_KEY`, a proposal-only `HEURISTIC` tier that is never applied, two-direction `CONFLICT` detection, `UNMAPPED` visibility, a typed GA4-property/provider-account timezone mismatch, a content-addressed predecessor-linked join map, and provider/GA4 rows side by side with no derived cross-source or causal field | `FIXTURE_VERIFIED` | [join contract](implementation/ga4-campaign-joins.md), `tests/test_ga4_campaign_joins.py`; fixture-only with `httpx.MockTransport`. No live GA4 or provider request, no GA4-UI or provider-UI reconciliation, no verification that a customer declaration is true, and no promotion of the GA4 `READ` operation group. The heuristic tier compares a customer-supplied name; explicitly requested Google report labels do not establish a join, which exports pseudonymize. Retained maps are envelope-bound to their GA4 property and chain-validated at append and on scan |
| LinkedIn offline provider audit | Deterministic rule table over retained LinkedIn report, inventory-read, drift, and local creative-binding records, with an explicit coverage status per audit dimension and non-writing recommendations | `FIXTURE_VERIFIED` | [audit rules](implementation/linkedin-audit-recommendations.md), `tests/test_linkedin_audit.py`, `tests/test_saved_audit_evidence.py`; no live request, no provider-UI reconciliation, and no promotion of the LinkedIn `AUDIT` or `RECOMMEND` operation group |
| OpenAI offline provider audit | Deterministic rule table over the retained OpenAI report, one completed inventory read, an optional earlier read, and local creative-binding records, with an explicit coverage status per audit dimension, presence-only targeting/creative/destination/tracking facts, and non-writing recommendations of `OBSERVE` and `PAUSE` only -- the compilable-operation set is wider than the set this audit emits, and every other considered kind appears as an explicit declined row | `FIXTURE_VERIFIED` | [audit rules](implementation/openai-audit-recommendations.md), [key lifecycle](implementation/openai-account-key-lifecycle.md), `tests/test_openai_audit.py`, `tests/test_openai_audit_route.py`, `tests/test_openai_read_completeness.py`, `tests/test_openai_account_key_lifecycle.py`; no live request, no provider-UI reconciliation, no sandbox exists, and no promotion of the OpenAI `AUTH`, `READ`, `AUDIT` or `RECOMMEND` operation group |
| Google offline provider audit and local OAuth status | Deterministic rule table over the retained Google Ads report, one completed inventory read, an optional earlier read, and local creative-binding records, with an explicit coverage status per audit dimension derived from the adapter's own read-surface table, presence-only ad-schedule/network/geo/creative/destination/policy facts, and non-writing recommendations of `OBSERVE` and `PAUSE` only -- the compilable-operation set is wider than the set this audit emits, and every other considered kind appears as an explicit declined row; plus `auth google-oauth status` reporting masked identity, scopes, token expiry tracking, Cloud project access level reported `NOT_OBSERVABLE`, and separated revocation guidance without contacting Google | `FIXTURE_VERIFIED` | [audit rules](implementation/google-ads-audit-recommendations.md), [OAuth status](implementation/google-oauth-status.md), [read contract](research/google-ads-read-contract.md), `tests/test_google_ads_audit.py`, `tests/test_google_ads_audit_route.py`, `tests/test_google_ads_read_completeness.py`, `tests/test_google_oauth_status.py`; no live Google request, no provider-UI reconciliation, the Cloud project's Test/Explorer/Basic/Standard access level is unproven and unobservable through any reviewed read, GAQL field compatibility for the additive projections is fixture-only, and no promotion of the Google Ads `AUTH`, `READ`, `AUDIT` or `RECOMMEND` operation group |
| Meta offline provider audit | Deterministic 52-rule table over the retained Meta report, one completed inventory read, an optional earlier read, and local creative-binding records, with an explicit coverage status per audit dimension derived from `META_ADS_READ_SURFACES`, presence-only targeting/creative/destination facts, and non-writing recommendations of `OBSERVE` and `PAUSE` only -- the compilable-operation set is wider than the set this audit emits, and every other considered kind appears as an explicit declined row | `FIXTURE_VERIFIED` | [audit rules](implementation/meta-ads-audit-recommendations.md), [read contract](research/meta-ads-read-contract.md), [token lifecycle](implementation/meta-token-credential-lifecycle.md), `tests/test_meta_audit.py`, `tests/test_meta_audit_route.py`, `tests/test_meta_read_completeness.py`, `tests/test_meta_token_status_disclosure.py`; fixture-only with `httpx.MockTransport` and a fake credential store. No live Meta request, no provider-UI reconciliation, no sandbox proven, app review / access tier / Business Verification `NOT_DOCUMENTED_LOCALLY` and unproven, the Graph `v26.0` pin corroborated against the changelog fetched 2026-09-09 but never exercised, and no promotion of the Meta Ads `AUTH`, `READ`, `AUDIT` or `RECOMMEND` operation group |
| TikTok offline provider audit and local app auth-code lifecycle | Deterministic 41-rule table over the retained TikTok report, one completed inventory read, an optional earlier read, and local creative-binding records, with an explicit coverage status per audit dimension derived from `TIKTOK_ADS_READ_SURFACES`, presence-only objective/budget-mode/bid-type facts, a parent/child serving-intent contradiction rule that `operation_status` genuinely permits, and non-writing recommendations of `OBSERVE` **only** -- no TikTok pause, status-update, or any other route that changes an existing TikTok resource exists in this build, so the pause a serving contradiction might suggest is an explicit declined row; plus `auth tiktok-token enroll|refresh|status|remove-local` exchanging an operator-supplied one-hour single-use authorization code through one fixed pinned JSON POST into the profile's ordinary access-token slot plus three protected slots, tracking expiry only when the response declares one, reporting advertisers as salted pseudonyms, refusing `refresh` with a typed `TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED` diagnostic, and separating local removal from unverified provider revocation | `FIXTURE_VERIFIED` | [audit rules](implementation/tiktok-ads-audit-recommendations.md), [token lifecycle](implementation/tiktok-token-lifecycle.md), [OAuth contract](research/tiktok-oauth-contract.md), [read contract](research/tiktok-ads-read-contract.md), `tests/test_tiktok_audit.py`, `tests/test_tiktok_audit_route.py`, `tests/test_tiktok_read_completeness.py`, `tests/test_tiktok_token_credentials.py`, `tests/test_tiktok_token_workflow.py`, `tests/test_tiktok_independent_review.py`; fixture-only with `httpx.MockTransport`, a fake credential store, and a private Typer root for the enrollment cases; the `auth tiktok-token` group is registered on `adhelm.cli` and all four leaves are discoverable. No live TikTok request, no provider-UI reconciliation, no sandbox proven or even addressable (its base URL is on no first-party page this build could fetch, so `--environment SANDBOX` is a typed refusal, not a guessed host). TikTok's reference portal renders through JavaScript, so the contracts are pinned from the TikTok-maintained SDK; the `fields` parameter is documented there as a bare `list[str]` with no returnable field names, so ad-group schedule/targeting/placement/optimization goal, ad creative association/video/copy, destination and `secondary_status` are declared unread rather than requested with a guessed name. Permission scopes, app approval, access-token expiry, any refresh capability, the rate-limit business-code vocabulary, the exchange response's `advertiser_ids`/`scope` field names, and the provider-side revocation path are all `UNVERIFIED`. No promotion of the TikTok Ads `AUTH`, `READ`, `AUDIT` or `RECOMMEND` operation group |
| GTM local OAuth lifecycle and authorization classification | A `gtm` READ profile through the shared `auth google-oauth enroll`, `reconnect` and `status` leaves and `auth remove-local`: a draft limited to a numeric account and exactly `tagmanager.readonly` with no currency or manager route; one bound bundle and no secondary slot; the bearer refreshed once before the first request of a read and shared by every request; a rejected refresh, an expired refresh token, a missing grant, a reconnect-required grant and another provider's bundle all stopping before any Tag Manager request; status reporting `NOT_OBSERVABLE` for the Cloud project access level and GTM-specific revocation guidance that keeps Tag Manager user permissions separate from the grant; and one classifier mapping `401` to `GTM_AUTHORIZATION_REJECTED` and `403` to `GTM_ACCESS_NOT_PERMITTED` (both exit 4, no response body read) at every GTM request site, the `403` deliberately not narrowed because Google documents the quota refusal as a `403` | `FIXTURE_VERIFIED` | [GTM auth lifecycle](implementation/gtm-auth-lifecycle.md), [OAuth status](implementation/google-oauth-status.md), `tests/test_gtm_auth_lifecycle.py`, `tests/test_gtm_inventory.py`, `tests/test_gtm_inventory_retention.py`; fixture-only with an in-memory credential store, `httpx.MockTransport` and seams for the browser grant and the refresh. No live Google authorization, refresh, revocation or Tag Manager request, no verification that the enrolled user can read any container, a long read can outlive its single bearer snapshot and then fails closed, the project quota is not paced, and no promotion of the GTM `AUTH` operation group: the `GTM Web API/browser` row below stays `UNRESEARCHED` |
| GA4 offline provider audit | Deterministic 30-rule table over a retained GA4 Data API report and, when retained, the GA4 Admin key-event and event-rule inventory for the same property, with an explicit coverage status per audit dimension, per-fact findings that keep a thresholded, sampled (and inconsistently sampled), rolled-up, restricted, incomplete, provider-declined or empty read -- including a roll-up or restriction a standard read, a zero-row read or an aggregate that names none can only report as flagged by GA4, and a withheld or never-returned key event -- distinct from a measured zero, a property-timezone date-boundary finding, a count-only cross-read of declared key events against the Admin inventory, and non-writing `OBSERVE` recommendations only; the compilable-operation registry lists the fixed key-event `CREATE` this build can compile, but the audit recommends no GA4 operation and declines `CREATE` as `EVIDENCE_INSUFFICIENT`, and every other considered outcome is an explicit declined row; an `--account` that is not the report's property, and an Admin observation finished after the evaluation time, are refused | `FIXTURE_VERIFIED` | [audit rules](implementation/ga4-audit-recommendations.md), `tests/test_ga4_audit.py`, `tests/test_ga4_audit_route.py`; fixture-only with `httpx.MockTransport` and retained ledger records. No live GA4 request, no GA4-interface reconciliation, no Realtime or processed-report verification, no drift (`measurement compare` owns it), no attribution model or consent state, and no promotion of the GA4 `AUDIT` or `RECOMMEND` operation group. A GA4 key event is never counted as native conversion evidence |
| Shared local runtime | Normalized snapshot persistence after complete fixed inventory, explicit external-change observations, retained report catalog/show, evidence-bounded local audit, checkpointed history, redacted projections, image features and creative identity | Local offline tests only | [audit contract](implementation/audit-contract.md), `tests/test_inventory.py`, `tests/test_audit.py`, `tests/test_execution_recovery_review.py` |
| GTM/event/consent | Pure validators over supplied declarations and inventory | Local offline tests only | `tests/test_measurement.py`; no provider discovery, browser execution, or write adapter |
| Customer conversion definitions | Closed customer declarations, canonical content digest, local version-chain retention and bounded review | Local offline tests only | [definition contract](implementation/conversion-contracts.md), [history](implementation/conversion-history.md), `tests/test_conversion_contracts.py`, `tests/test_conversion_history.py`; no source, consent, or execution proof |
| Meta offline campaign compiler | Fixed paused US Facebook-feed traffic/image hierarchy, exact USD lifetime budget and content-bound creative, bounded CLI preview | Local offline tests only | [compiler contract](research/meta-compiler-contract.md), `tests/test_meta_compiler.py`, `tests/test_meta_compiler_ultra_review.py`, `tests/test_compile_cli_new_providers.py`; no execution or live eligibility proof |
| Google Ads v25 guarded campaign PAUSE | One retained `ENABLED` -> `PAUSED` plan executed through the provider-neutral `execute plan` leaf: fixed customer/campaign readback, one `customers/{id}/campaigns:mutate` with `update_mask=status`, a closed `results[].resourceName` acknowledgement subset, one retained readback observation, and no retry | `FIXTURE_VERIFIED` | [execution contract](implementation/google-ads-pause-execution.md), `tests/test_google_ads_pause_execution.py`, `tests/test_generic_execution_route.py`; fixture-only with `httpx.MockTransport` and a fake credential store. No live Google Ads request, no installed approval host, and no promotion of the Google Ads `PLAN_APPLY_SAFETY` or `MAINTAIN` operation group: a 0.1.0 build refuses at the release-policy gate (`WRITES_NOT_RELEASED`) before any credential and independently at the uninstalled approval bridge |
| Google Ads v25 guarded paused Search hierarchy CREATE | One retained offline Search/RSA candidate promoted to an executable plan and executed through the provider-neutral `execute plan` leaf: recompile-and-bind of the retained candidate digest, one bounded customer-identity read and one bounded byte-exact campaign-name probe covering the planned name and a closed near-variant set (case, single-space padding, Unicode normalization form) that refuses a collision or a near match before egress, a locally keyed semantic before/after hierarchy projection with no native identifier, one atomic `customers/{id}/googleAds:mutate` with ordered temporary resource IDs and `partialFailure=false`, a closed ordered `mutateOperationResponses[]` acknowledgement subset, a GAQL readback of the created hierarchy compared with the predicted projection, one retained pseudonymized readback observation, every entity `PAUSED`, and no retry or delete | `FIXTURE_VERIFIED` | [execution contract](implementation/google-ads-create-execution.md), `tests/test_google_ads_create_execution.py`, `tests/fixtures/google_ads_search_campaign_create_mutate_body.json`, `tests/test_generic_execution_route.py`; fixture-only with `httpx.MockTransport` and a fake credential store. No live Google Ads request, no installed approval host, no activation, and no promotion of the Google Ads `PLAN_APPLY_SAFETY`, `CREATE`, or `ACTIVATE` operation group: a 0.1.0 build refuses at the release-policy gate (`WRITES_NOT_RELEASED`) before any credential and independently at the uninstalled approval bridge, and additionally refuses before any credential when its ledger does not register the two additive Google Ads CREATE record kinds. Campaign budget quota and the account asset-automation setting have no readable v25 surface and are recorded as explicit `NOT_READABLE` statuses; `geo_target_type_setting` and campaign asset automation are provider-chosen and recorded from the readback, never authorized |
| Google Ads v25 guarded Search hierarchy ACTIVATE | The separate spend-explicit activation rule 8 requires, executed through the provider-neutral `execute plan` leaf: one retained P04 CREATE plan promoted by its record digest, one bounded customer-identity read and one bounded whole-hierarchy read, a locally keyed semantic before/after projection with no native identifier (`PAUSED` members to `ENABLED` members, the output-only budget status excluded), the observed `CampaignBudget.total_amount_micros` carried as the subplan's `max_spend` so the `COMPOSITE_EXECUTION_PLAN` subject's per-currency bound is the campaign-lifetime exposure a reviewer signs and can never summarize as `NOT_SPEND_CAPABLE`, review lines stating that activation begins delivery and spend, one atomic `customers/{id}/googleAds:mutate` with `update_mask=status` per operation and no temporary resource ID, a closed ordered `mutateOperationResponses[]` acknowledgement subset, a GAQL readback compared with the predicted projection, one retained pseudonymized readback observation, and a precomputed recovery pause plan digest recorded as an unauthorized proposal | `FIXTURE_VERIFIED` | [execution contract](implementation/google-ads-activate-execution.md), `tests/test_google_ads_activate_execution.py`, `tests/fixtures/google_ads_search_campaign_activate_mutate_body.json`; fixture-only with `httpx.MockTransport` and a fake credential store. **No live Google Ads request, no activation ever executed, and no spend ever incurred.** No installed approval host and no promotion of the Google Ads `PLAN_APPLY_SAFETY` or `ACTIVATE` operation group: a 0.1.0 build refuses at the release-policy gate (`WRITES_NOT_RELEASED`) before any credential and independently at the uninstalled approval bridge, and additionally refuses before any credential when its ledger does not register the two additive Google Ads ACTIVATE record kinds. Refused before egress: an already-`ENABLED` or `REMOVED` member, a budget or campaign-date change since the CREATE plan was authorized, a past `end_date_time`, an ad with no `final_urls`, a `DISAPPROVED` ad, a non-`TARGET_SPEND` bidding strategy, and a non-Search campaign. Ad policy approval status is read where v25 returns it and disclosed as `NOT_READ_NO_POLICY_SUMMARY_RETURNED` where it does not; conversion-dependent activation is out of scope and stated as such; promoting a campaign from an inventory read rather than a retained CREATE plan is not implemented and is refused explicitly |
| Google Ads v25 guarded Search maintenance and replacement | Four documented maintenance operations on an existing paused or enabled Search hierarchy, each its own single-request executable plan on the provider-neutral `execute plan` leaf under the registered pair `(google_ads, UPDATE)` (the archive's `(google_ads, ARCHIVE)` pair is deliberately **not registered**, so `discover` never lists it): campaign budget amount (`update_mask=totalAmountMicros`, bound = the new campaign-lifetime total), ad-group max-CPC bid (`update_mask=cpcBidMicros`, bound = the campaign budget, because a bid change is spend-affecting and never `NOT_SPEND_CAPABLE`), ad-schedule replacement (v25 criteria are `Immutable`, so every existing schedule criterion is removed and the replacements created in one atomic `partialFailure=false` mutate, with no temporary resource ID because nothing references a created criterion), and archive (campaign `status=REMOVED`, irreversible, `NOT_SPEND_CAPABLE` on an explicit zero cap, `IRREVERSIBLE_ARCHIVE` stated in the review lines, refused unless the request itself acknowledges irreversibility and refused twice -- at planning and against the fresh pre-egress read -- unless the campaign is already paused). **An archive is delete-class, so AGENTS rule 11 also demands a read-derived `PROVEN_UNREFERENCED` dependency proof over five reference surfaces, and no cited v25 read reaches any of them: in this build an archive request is refused as `EXECUTION_PLAN_NOT_COMPILABLE` because the `(google_ads, ARCHIVE)` pair is unregistered, with nothing retained (the planner's own rule 11 gate, `GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN`, fires only once the pair is registered, which today happens only in the fake-read tests), and the five-surface list is itself uncited and provisional.** The archive wire body, review, execution and readback are verified only against a fake stand-in for that future read, so the archive operation is not an available capability in this build, is not registered, and is not advertised by `discover`. Shared with ACTIVATE: the locally keyed semantic before/after projection, the closed ordered acknowledgement subset, and one retained pseudonymized readback observation per attempt, which records the previous and new value | `FIXTURE_VERIFIED` | [execution contract](implementation/google-ads-maintain-execution.md), `tests/test_google_ads_maintain_execution.py`, the four `tests/fixtures/google_ads_search_campaign_maintain_*_mutate_body.json` golden bodies; fixture-only with `httpx.MockTransport` and a fake credential store. **No live Google Ads request, nothing ever archived, and no spend ever incurred.** No installed approval host and no promotion of the Google Ads `PLAN_APPLY_SAFETY` or `MAINTAIN` operation group: a 0.1.0 build refuses at the release-policy gate (`WRITES_NOT_RELEASED`) before any credential and independently at the uninstalled approval bridge, and additionally refuses before any credential when its ledger does not register the two additive Google Ads MAINTAIN record kinds. Recovery is the recorded previous value and a new authorized plan; nothing reverts automatically and an archive is not reversible at all. Targeting, destination, tracking, and creative-association maintenance are out of this subset; the JSON `FieldMask` lowerCamelCase encoding for multi-word paths is `UNVERIFIED` against the Google Ads REST surface and needs a live readback |
| OpenAI Ads v1 guarded paused chat-card hierarchy CREATE | One retained offline chat-card candidate promoted to an executable plan and executed through the provider-neutral `execute plan` leaf as the first journaled child-egress operation: recompile-and-bind of the retained candidate digest, one private-vault creative resolved by content digest and re-decoded against the compiled asset identity, one bounded ad-account identity and exact campaign-name-collision read, a locally keyed semantic before/after hierarchy projection with no native identifier, then `1 + 1 + N + M` ordered children -- `POST /v1/upload`, `POST /v1/campaigns`, one `POST /v1/ad_groups` per group, one `POST /v1/ads` per ad -- each with its own durable `CHILD_APPLY_STARTED`, its own one-use permit consumed immediately before the POST, and a closed `id`+`status: paused` acknowledgement subset; a bounded GET readback of the created campaign, ad groups and ads compared with the predicted projection; one retained pseudonymized readback observation; every entity `paused`; and no retry, no delete, and no repair of a partial hierarchy | `FIXTURE_VERIFIED` | [execution contract](implementation/openai-create-execution.md), `tests/test_openai_create_execution.py`, `tests/fixtures/openai_ads_chat_card_hierarchy_create_requests.json`, `tests/test_kernel_child_egress.py`, `tests/test_generic_execution_route.py`; fixture-only with `httpx.MockTransport` and a fake credential store. No live OpenAI Ads request, no installed approval host, no activation, and no promotion of the OpenAI Ads `PLAN_APPLY_SAFETY`, `CREATE`, or `ACTIVATE` operation group: a 0.1.0 build refuses at the release-policy gate (`WRITES_NOT_RELEASED`) before any credential and independently at the uninstalled approval bridge, and additionally refuses before any credential in any build whose ledger does not register the two additive OpenAI Ads CREATE record kinds. OpenAI documents no create response schema for ad groups or ads, no idempotency or settlement resource, and no file read of any kind, so the acknowledgement subsets are reviewed assumptions from the documented resource objects, an ambiguous child is never retried, and the creative association is `PROVEN_UPLOAD_ASSOCIATION` from the paused ad's own readback only -- provider bytes stay `PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ` |
| Meta Ads Graph v26.0 guarded paused single-image hierarchy CREATE | One retained offline Meta traffic/single-image candidate promoted to an executable plan and executed through the provider-neutral `execute plan` leaf as the second journaled child-egress operation: recompile-and-bind of the retained candidate digest, one private-vault creative resolved by content digest and re-decoded against the compiled asset identity, one bounded ad-account identity read and a bounded, paged read of the campaign names the account's `campaigns` edge lists (no status filter) that refuses an exact or near-variant collision, a locally keyed semantic before/after hierarchy projection with no native identifier, then five ordered children -- `POST /adimages`, `/campaigns`, `/adsets`, `/adcreatives`, `/ads` -- each with its own durable `CHILD_APPLY_STARTED`, its own one-use permit consumed immediately before the POST, and a closed `id` (or image `hash`) acknowledgement subset; a bounded GET readback of the created campaign, ad set, creative and ad plus the account's `adimages` read for the uploaded hash, compared with the predicted projection; one retained pseudonymized readback observation whose relation and `every_pausable_entity_configured_status` state only what was observed (an unpaused or unreadable hierarchy is recorded as such); every created pausable entity `PAUSED`; and no retry, no delete, and no repair of a partial hierarchy | `FIXTURE_VERIFIED` | [execution contract](implementation/meta-create-execution.md), `tests/test_meta_create_execution.py`, `tests/test_meta_create_execution_review.py`, `tests/fixtures/meta_ads_hierarchy_create_requests.json`, `tests/test_kernel_child_egress.py`, `tests/test_generic_execution_route.py`; fixture-only with `httpx.MockTransport` and a fake credential store. No live Meta request, no installed approval host, no activation, and no promotion of the Meta Ads `PLAN_APPLY_SAFETY`, `CREATE`, or `ACTIVATE` operation group: a 0.1.0 build refuses at the release-policy gate (`WRITES_NOT_RELEASED`) before any credential and independently at the uninstalled approval bridge, and additionally refuses before any credential in any build whose ledger does not register the two additive Meta Ads CREATE record kinds. The wire facts were checked offline only against a v25.0.2 copy of the official generated SDK (field names, not behavior); the create and upload response shapes, the multipart field name, the creative `image_hash` readback, the 32-lowercase-hexadecimal image-hash grammar, whether the campaigns edge lists archived or deleted campaigns, whether a just-created campaign is visible on the immediate list read, and the ad-set `bid_strategy` beside a campaign budget are `UNVERIFIED` in the execution contract and are live-pilot checks. Meta documents no idempotency or settlement resource, so an ambiguous child is never retried, and the image association is an account-level hash match, with provider bytes `PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ` |
| Google Ads offline campaign compiler | Fixed paused Search/RSA hierarchy, exact USD campaign total budget, explicit keyword/geo/language/schedule criteria, bounded CLI preview | Local offline tests only | [compiler contract](research/google-ads-compiler-contract.md), `tests/test_google_ads_compiler.py`, `tests/test_compile_cli_new_providers.py`; no execution or live eligibility proof |

These subsets do not evidence OAuth lifecycle, complete inventory/report parity,
full audit coverage, creation, activation, native conversions, provider recovery,
or live eligibility. OpenAI snapshots retain native budget/bid micros with exact USD minor-unit
projections only; non-USD and subminor amounts carry explicit conversion gaps. LinkedIn's
reviewed Ad Analytics request has no documented pagination, and Reddit report
continuation/completeness remains unverified; neither report is an exhaustive
provider claim. Local composite recovery barriers do not prove provider
recovery. OpenAI's account-key lifecycle, refusal classification and audit rules are fixture-only:
the reviewed OpenAI pages document no key expiry, rotation endpoint, revocation route, or
`429`/`Retry-After` semantics, so those behaviours stay `UNVERIFIED`. Google Ads reads now
report whether the provider declared a total result count for every page walk, and a read
that did not is reported as incomplete rather than assumed whole; Google Ads ad copy,
destinations and criterion values are never retained, only counts and presence; and the
Google Ads Cloud project access level (developer tokens were retired on 2026-09-09) stays `NOT_OBSERVABLE` because no reviewed
v25 read returns it.
Official-source research for the six advertising channels, GA4 and GTM is recorded in the
per-provider contracts in [`docs/research/`](research/).

| Provider | AUTH | READ | AUDIT | RECOMMEND | COMPILE | PLAN_APPLY_SAFETY | CREATE | VALIDATE | ACTIVATE | MAINTAIN | RECOVER | HISTORY_EXPORT | CREATIVE_CAPTURE |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Google Ads | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| Meta Ads | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| LinkedIn Ads | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| TikTok Ads | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| Reddit Ads | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| OpenAI Ads | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |

All six remain product v1 scope, with operation-level access researched in
Phase 0 before locking the common connector interface. Each customer selects a
subset; an unconfigured provider or a customer's access denial does not block
unrelated configured providers. Customer-owned apps, keys, or OAuth may satisfy
access where allowed by the provider. Record who owns the app and who must
obtain each permission; OAuth consent does not establish developer-app approval.
Never infer working support from another customer's eligibility.

The required advertising matrix has six providers and thirteen operation
groups (78 cells). Removing a provider from launch scope does not promote any
remaining evidence status.

### Dated advertising research notes

| Provider | Access date | Candidate API/version | Research result; no operation promotion |
| --- | --- | --- | --- |
| Google Ads | 2026-09-04 | `v25` | OAuth access through the Cloud project's access level (developer tokens retired 2026-09-09), test accounts, quotas, Search/RSA hierarchy, and conversion resources documented; production access and all fixture/live proof pending |
| Meta Ads | 2026-09-04 | version unresolved; official pages observed `v25.0` examples and `v26.0` reference | Business app/access tiers, auth, traffic/single-image hierarchy, Insights, and Pixel resources documented; exact pin/app review/sandbox gaps and all proof pending |
| LinkedIn Ads | 2026-09-04 | `Linkedin-Version: 202608` | OAuth scopes, Development/Standard tiers, cursor paging, website-visits DSC, and conversion-rule boundaries documented; app/two-org/page-role access and all proof pending |
| TikTok Ads | 2026-09-04 | portal API `v1.3`, recheck before fixtures | app auth, sandbox, tiered limits, traffic/single-video hierarchy, and Pixel resources documented; production access/current contract and all proof pending |
| Reddit Ads | 2026-09-04 | Ads API `v3` | OAuth scopes, endpoint-group quota model, pagination, and paused traffic/link-ad hypothesis documented; no general sandbox and all schema/live proof pending |
| OpenAI Ads | 2026-09-04; overview, authentication and index rechecked 2026-09-09 | Advertiser API `v1` | account-scoped key issued in the Ads Manager Settings tab, public hierarchy/insights/bulk/conversion APIs, and 600 endpoint/1,200 overall rpm enforced by ad account and IP address documented; key expiry, rotation, revocation, and `401`/`403`/`404`/`429` semantics are absent from every reviewed page; account/feature access, no sandbox, and all proof pending |

## Advertising backlog outside v1

Apple Search Ads / App Store advertising is backlogged and has no required v1
matrix row. No research, implementation, credential, live-test, soak, or release
evidence for Apple is required. Promotion follows the separate scope decision
in [`product-brief.md`](product-brief.md#post-v1-advertising-backlog); this entry
is not a claim of documented, implemented, or live-verified support.

## Initial measurement matrix

The product contract and primary documentation surfaces are identified. Narrow
fixture-tested reads and local validators are listed above; no complete
measurement workflow or live behavior is evidenced.

| Surface | Research | Auth | Inventory/report | Audit/compile | Stage/edit | Publish/verify | Restore/recover |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GA4 Data API | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | N/A | `UNRESEARCHED` | N/A |
| GA4 Admin API | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| GTM Web API/browser | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |

GTM may be `NOT_CONFIGURED_OPTIONAL` for a customer profile. That does not lower
the required release status of the GTM adapter itself. The dated research
record distinguishes GA4 Data `v1beta`, GA4 Admin key events `v1beta`, GA4 Admin
event rules `v1alpha`, and GTM API `v2`; `DOCUMENTED` proves none of those
operation cells.

The installed `measurement gtm` read subset has fixture-verified account/web-container
identity, feature availability, live/latest version counts, opaque-code markers,
accessible workspace metadata, and normalized history/review integration. See
[identity contract](research/gtm-identity-read-contract.md),
[workflow review](implementation/gtm-workflow-ultra-review.md), and
[measurement history](implementation/measurement-history.md). This subset does
not promote the full inventory group: workspace contents, dependency proof,
staging, compiler/browser verification, writes and live evidence remain required.

The opt-in `measurement gtm --full-inventory` subset is separately
fixture-verified: every documented collection of the live version, the latest
version and each accessible workspace, per-collection pagination completeness,
live/latest equality status, hashed and classified custom code, and raw
reference edges retained as a `gtm_web_inventory` observation. See
[GTM inventory](implementation/gtm-inventory.md). An inventory larger than one
1 MiB ledger record is retained as bounded chunk records plus a commit record
([retention beyond one record](implementation/gtm-inventory.md#retention-beyond-one-record));
that changes how it is stored, not what is proven. This is not a group
promotion. It proves no dependency graph, no `PROVEN_UNREFERENCED`
classification, no GTM UI reconciliation, no two-organization coverage, and no
live GTM behavior; the `GTM Web API/browser` row above stays `UNRESEARCHED` for
every operation cell.

The derived `measurement gtm-graph` subset is separately fixture-verified: a
scoped dependency graph over that retained inventory, a fail-closed
`PROVEN_UNREFERENCED` classification whose every refusal reason is named, a
read-only audit rule table, and the existing stage gate run on retained evidence
instead of a caller-supplied structure. See
[GTM dependency graph](implementation/gtm-dependency-graph.md). This is not a
group promotion. It is read-only and derived: it makes no provider request,
retains nothing, compiles no GTM operation
(`COMPILABLE_OPERATION_KINDS["gtm"]` is empty), and recommends no deletion or
cleanup. It proves no live GTM behavior, no GTM UI reconciliation, no
two-organization coverage, and no staging, cleanup or publish capability; the
`GTM Web API/browser` row above stays `UNRESEARCHED` for every operation cell.

## Promoted conversion integration matrix

The following operations are now in implementation scope under
[`conversion-integrations.md`](conversion-integrations.md). Scope promotion
changes no evidence status. These rows refine native measurement claims rather
than claiming one shared native-event API or parity across providers. Each
customer selects the destinations it needs; any unsupported or unsafe operation
fails explicitly before side effects. The all-six advertising release gate below
is unchanged.

| Provider | Research | Native resource inventory/setup | Spend-affecting association | Reviewed tag/pixel configuration | Browser and native receipt | Processed native reporting |
| --- | --- | --- | --- | --- | --- | --- |
| Google Ads | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| Meta Ads | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| LinkedIn Ads | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| TikTok Ads | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| Reddit Ads | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |
| OpenAI Ads | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |

Native conversion resource setup, association, and tag configuration require
independent authorization decisions. An explicit delegation may cover them;
resource-creation authority never implies activation or publication authority.
Published support names the exact evidenced subset and remaining provider gaps.
The research column means only that official resource/tag options and regulated
exclusions were reviewed on 2026-09-04.

## Promoted optional CRM aggregate matrix

HubSpot and Salesforce executable read-only aggregate connectors are in scope;
CRM metadata discovery and CRM configuration writes remain outside v1. Missing
customer CRM profiles do not block default doctor, unrelated advertising,
default tests, or packaging. A public connector claim requires actual adapter
and real-account evidence regardless of that customer optionality.

| Source | Research | Auth/identity/revocation | Fixed source-side aggregate query | Minimum-cell and response boundary | Campaign joins/model/freshness | Qualified pipeline/revenue | Live UI reconciliation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HubSpot | `DOCUMENTED` | `UNRESEARCHED` | `BLOCKED` | `BLOCKED` | `UNRESEARCHED` | `BLOCKED` | `UNRESEARCHED` |
| Salesforce | `DOCUMENTED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` | `UNRESEARCHED` |

An outcome unavailable through safe source-side aggregates is recorded as a
specific blocker or unsupported report; retrieving individual records or
substituting campaign traffic cannot satisfy a pipeline/revenue cell. Research
must establish edition, scopes, aggregation/suppression, attribution, supported
fields, query cost, pagination, and bounded-window behavior for each report.

HubSpot blocker recorded 2026-09-04: stable `2026-03` campaign metrics expose
sessions, influenced contacts, and new contacts only. The `2026-09-beta`
revenue aggregate has no qualified-stage filter and no source-side minimum-cell
threshold. Record-level fallback is prohibited. Resolution requires a safe
provider aggregate or an explicit product-owner contract decision.

Salesforce research recommends direct version-pinned REST with a fixed reviewed
aggregate SOQL `GROUP BY`/`HAVING` contract and customer-declared
models/stages. Grouped results cannot use a query locator, so the compiler must
bound output below 2,000 groups. These facts do not promote any executable
Salesforce cell.

## Research record required before implementation

For each provider and operation group, record:

- current first-party documentation URL and access date;
- API name/version and retirement policy;
- authentication flow, exact scopes, token lifecycle, and whether customer-owned
  applications are supported;
- approval, certification, account-tier, geographic, or commercial access
  requirements;
- sandbox/test-account availability and how it differs from production;
- supported object graph, create/update limitations, review states, and
  irreversible or non-idempotent behavior;
- pagination, rate limits, concurrency, error model, asynchronous jobs, and
  ambiguous-timeout reconciliation;
- currency, timezone, attribution, reporting-latency, and data-retention rules;
- the narrow v1 format/object subset that still satisfies the common outcome;
- known gaps, exact blocker, owner, next action, and last retest date; and
- linked fixture, test, live evidence, and UI-reconciliation references as they
  become available.

Use primary provider documentation, API schemas, and provider responses. Do not
promote a status based only on memory, third-party articles, or behavior in the
maintainer's separate production project.

## Release rule

`v1.0.0` cannot ship until all required advertising operation groups and all
required GA4/GTM rows reach `LIVE_VERIFIED` under the exact evidence counts in
[`definition-of-done.md`](definition-of-done.md). Provider-specific extras may
ship, but do not compensate for a missing common operation.

The promoted CRM/native baseline in `conversion-integrations.md` is also
required: both CRM adapters must prove a qualified-outcome and pipeline/revenue
aggregate workflow, and every ad provider must prove a declared native
conversion mapping across its applicable operation/evidence cells. Unsupported
additional formats may be disclosed, but an empty advertised subset or an
error-only implementation cannot satisfy the new scope. Missing customer
credentials do not relax product evidence or block unrelated customer workflows.

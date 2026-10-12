# V1 requirement, test, and evidence traceability

Last updated: 2026-10-11

## Gate interpretation

Stable IDs below cover every release area in
[`definition-of-done.md`](definition-of-done.md) sections 1-10. IDs are never
reused; revised requirements retain their ID and change history.

Current Phase 0 output is research and contract documentation only. `DOC_READY`
or `RESEARCH_ONLY` does **not** constitute `FIXTURE_VERIFIED`,
`LIVE_VERIFIED`, release evidence, or implementation. No row currently has
fixture or live evidence. `COMPLETE` is impossible until every required live
test passes, the 30 consecutive calendar-day soak passes, release artifacts are
reproduced and signed, final smoke evidence closes, and the exact passing commit
has a signed `v1.0.0` tag.

Implementation phases use the numbered engineering phases of the build plan:
P1 safety kernel, P2 read/audit/report, P3 creation/management, P4 measurement,
P5 operating UX, P6 hardening/distribution, and P7 live validation/soak/release.
Evidence paths are planned local manifest locations, not files that exist.

Status vocabulary:

- `NOT_STARTED`: executable implementation/evidence has not begun.
- `RESEARCH_ONLY`: official sources are recorded; no executable proof.
- `DOC_READY`: the Phase 0 document exists; implementation/review evidence is
  still absent.
- `BLOCKED_EXTERNAL`: a required provider/access behavior is currently
  unavailable; this never counts as parity.

Test classes use the definitions in [`evidence-model.md`](evidence-model.md).

## 1. Complete lifecycle parity on six ad platforms

Every row applies independently to Google Ads, Meta Ads, LinkedIn Ads, TikTok
Ads, Reddit Ads, and OpenAI Ads.

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-01-01` | Auth, refresh/re-auth, revocation, account identity, scopes, versions, pagination, quota, money/time/attribution/freshness reporting | P2, P7 | conformance + live | `evidence/v1/DOD-01-01/` fixture/live per provider | `RESEARCH_ONLY` | adapters, credentials, account access, live proof |
| `DOD-01-02` | Inventory/report UI reconciliation with identical query context | P2, P7 | live + UI reconciliation | `evidence/v1/DOD-01-02/` UI comparison per provider | `NOT_STARTED` | adapters and external accounts |
| `DOD-01-03` | Required audit coverage | P2, P7 | fixture + conformance + live | `evidence/v1/DOD-01-03/` findings and live checks | `NOT_STARTED` | audit engine and normalized reads |
| `DOD-01-04` | Evidence-backed non-writing recommendations | P2, P5, P7 | unit/property + live | `evidence/v1/DOD-01-04/` recommendation records | `NOT_STARTED` | schemas and report evidence |
| `DOD-01-05` | Deterministic brief-to-native compile with visible loss/unsupported mappings | P3 | golden + conformance | `evidence/v1/DOD-01-05/` compile vectors per provider | `RESEARCH_ONLY` | provider mappings remain hypotheses |
| `DOD-01-06` | Complete paused/draft hierarchy and serving inputs | P3, P7 | fixture + live | `evidence/v1/DOD-01-06/` create/read-back/UI | `RESEARCH_ONLY` | adapters, mutation access, live accounts |
| `DOD-01-07` | Objective, target, budget, bid, schedule, placement, URL/UTM, conversion and creative operations | P3, P7 | conformance + live | `evidence/v1/DOD-01-07/` field coverage and read-back | `RESEARCH_ONLY` | exact formats need fixture/live proof |
| `DOD-01-08` | Separate account-bound spend-explicit activation authorization | P1, P3, P7 | property + fault + live | `evidence/v1/DOD-01-08/` grant/plan/result | `NOT_STARTED` | safety kernel and bounded live spend |
| `DOD-01-09` | Pause/archive and supported maintenance/replacement | P3, P7 | conformance + live | `evidence/v1/DOD-01-09/` operation/read-back | `RESEARCH_ONLY` | mutability maps and access |
| `DOD-01-10` | Content-addressed plans, before/after, spend, re-read, drift rejection, ambiguity discipline | P1, P3 | property + conformance + fault | `evidence/v1/DOD-01-10/` plan/drift/transport cases | `DOC_READY` | implementation absent |
| `DOD-01-11` | Verification, partial/ambiguous results, recovery, deterministic redacted evidence | P1, P3, P6, P7 | fault + live | `evidence/v1/DOD-01-11/` result/recovery/export | `DOC_READY` | implementation and live faults absent |
| `DOD-01-12` | Compile all selected subplans before write; honest non-atomic composite outcomes | P3, P7 | integrated conformance + live fault | `evidence/v1/DOD-01-12/` composite run | `DOC_READY` | coordinator and all-six live profiles |

## 2. GA4 reporting and administration

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-02-01` | Separate least-privilege Data/Admin profiles, revocation, insufficient scope | P2, P4, P7 | conformance + live | `evidence/v1/DOD-02-01/` auth cases | `RESEARCH_ONLY` | implementation and GA4 profiles |
| `DOD-02-02` | Property/stream identity before operations | P2, P4 | conformance + live | `evidence/v1/DOD-02-02/` identity read-back | `RESEARCH_ONLY` | adapter and access |
| `DOD-02-03` | Aggregate pagination/timezone/currency/attribution/freshness/threshold/sample/date behavior | P2, P7 | fixture + live + UI | `evidence/v1/DOD-02-03/` reports | `RESEARCH_ONLY` | Data adapter and representative property |
| `DOD-02-04` | Deterministic joins and visible heuristic/conflict/unmapped states | P2 | property + fixture | `evidence/v1/DOD-02-04/` join vectors | `NOT_STARTED` | reporting implementation |
| `DOD-02-05` | Missing/stale/duplicate/thresholded/sampled/pending distinct from zero | P2, P4 | fixture + live | `evidence/v1/DOD-02-05/` status cases | `RESEARCH_ONLY` | adapters and live quality cases |
| `DOD-02-06` | v1alpha event rules and v1beta key-event guarded operations | P4, P7 | conformance + live | `evidence/v1/DOD-02-06/` plans/results | `RESEARCH_ONLY` | edit access and alpha stability |
| `DOD-02-07` | Counting/default value/currency, immutability, prospective behavior | P4, P7 | fixture + live | `evidence/v1/DOD-02-07/` config/read-back | `RESEARCH_ONLY` | Admin adapter and property |
| `DOD-02-08` | Browser, Realtime, processed report, and UI verification | P4, P7 | browser measurement + live/UI | `evidence/v1/DOD-02-08/` four-stage evidence | `NOT_STARTED` | harness and disposable property/site |
| `DOD-02-09` | No prohibited person/raw/sensitive data | P1, P4, P6 | negative + network capture | `evidence/v1/DOD-02-09/` rejection/capture | `DOC_READY` | enforcement/tests absent |
| `DOD-02-10` | No implication that GA4 key event is a native ad conversion | P4, P6 | docs/output contract | `evidence/v1/DOD-02-10/` output snapshots/review | `DOC_READY` | implementation output review absent |

## 3. GTM Web capability

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-03-01` | Clean install works without GTM; optional status isolated | P4, P6 | install + conformance | `evidence/v1/DOD-03-01/` clean-install result | `NOT_STARTED` | executable/package absent |
| `DOD-03-02` | Complete workflow on two organizations/containers other than the maintainer's development accounts | P4, P7 | live | `evidence/v1/DOD-03-02/` two profile manifests | `RESEARCH_ONLY` | adapter, credentials, containers |
| `DOD-03-03` | Separate read/edit-version/publish auth and safe failures | P1, P4, P7 | conformance + live | `evidence/v1/DOD-03-03/` scope matrix | `RESEARCH_ONLY` | auth implementation/access |
| `DOD-03-04` | Complete paginated inventory and UI reconciliation | P4, P7 | fixture + live/UI | `evidence/v1/DOD-03-04/` inventory comparison | `RESEARCH_ONLY` | full resource adapter |
| `DOD-03-05` | Dependency coverage and delete-only `PROVEN_UNREFERENCED` | P4 | graph fixtures + negative | `evidence/v1/DOD-03-05/` graph corpus | `DOC_READY` | graph implementation absent |
| `DOD-03-06` | Required audit diagnostics positive/negative cases | P4 | fixture + browser | `evidence/v1/DOD-03-06/` diagnostic corpus | `DOC_READY` | adapter/harness absent |
| `DOD-03-07` | Dedicated-workspace cleanup operations | P4, P7 | conformance + live | `evidence/v1/DOD-03-07/` staged/read-back changes | `NOT_STARTED` | adapter and safe containers |
| `DOD-03-08` | Live/latest mismatch and full workspace fail closed | P4 | fixture + live negative | `evidence/v1/DOD-03-08/` conflict results | `DOC_READY` | implementation and profiles |
| `DOD-03-09` | Typed tag/trigger creation, static check, quick preview, browser preview | P4, P7 | conformance + browser/live | `evidence/v1/DOD-03-09/` staged preview | `RESEARCH_ONLY` | typed allowlist and harness |
| `DOD-03-10` | Immutable version equals browser-tested draft or is retested | P4 | golden + live | `evidence/v1/DOD-03-10/` canonical digest proof | `DOC_READY` | canonical GTM serializer |
| `DOD-03-11` | Separate publish, drift check, browser/Realtime/processed verify | P4, P7 | browser + live/UI | `evidence/v1/DOD-03-11/` publish chain | `RESEARCH_ONLY` | publish access and harness |
| `DOD-03-12` | Denied/granted consent behavior | P4, P6 | browser network capture | `evidence/v1/DOD-03-12/` consent traces | `DOC_READY` | CMP contract/harness |
| `DOD-03-13` | Disposable benign verifier failure restores prior live version | P4, P7 | controlled live fault | `evidence/v1/DOD-03-13/` restore drill | `NOT_STARTED` | disposable site/container/property |
| `DOD-03-14` | Publish/restore timeout ambiguity and no blind retry | P4 | fault + live read-back | `evidence/v1/DOD-03-14/` transport cases | `DOC_READY` | implementation absent |
| `DOD-03-15` | Missing signal returns safe site-instrumentation contract | P4 | fixture + negative | `evidence/v1/DOD-03-15/` diagnostic/contract | `DOC_READY` | compiler absent |
| `DOD-03-16` | Custom code classified locally and never leaks | P1, P4, P6 | canary + network/output scan | `evidence/v1/DOD-03-16/` leak tests | `DOC_READY` | scanner/enforcement absent |

## 4. Privacy, consent, and local-operation properties

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-04-01` | No mandatory hosted/vendor telemetry/update/license/meter services | P1, P6 | architecture + install/network | `evidence/v1/DOD-04-01/` dependency/capture review | `DOC_READY` | executable review pending |
| `DOD-04-02` | Every egress allowlisted/explained; undeclared fails closed | P1, P4, P6 | network capture + negative | `evidence/v1/DOD-04-02/` captures | `DOC_READY` | transport/browser enforcement absent |
| `DOD-04-03` | OS credential storage; secrets absent from all surfaces/history | P1, P6 | secret/history scan + fixture | `evidence/v1/DOD-04-03/` scan reports | `DOC_READY` | credential implementation absent |
| `DOD-04-04` | Separate read/edit/activation/publish scopes where possible | P1, P3, P4, P7 | conformance + live | `evidence/v1/DOD-04-04/` profile matrix | `RESEARCH_ONLY` | provider-specific access |
| `DOD-04-05` | Regulated input/data rejection before all sinks | P1, P4, P6 | property + adversarial + fixture | `evidence/v1/DOD-04-05/` rejection matrix | `DOC_READY` | classifiers/schemas absent |
| `DOD-04-06` | Protocol mechanics narrowly transmitted; raw mechanics excluded; exports pseudonymized | P1, P6 | network/output capture | `evidence/v1/DOD-04-06/` capture/export checks | `DOC_READY` | implementation absent |
| `DOD-04-07` | PHI/PII canaries rejected on every input/response surface | P1, P6 | adversarial + fixture | `evidence/v1/DOD-04-07/` canary matrix | `NOT_STARTED` | surface inventory implementation |
| `DOD-04-08` | Existing CMP checked only against declared policy; no install/override | P4 | fixture + browser | `evidence/v1/DOD-04-08/` consent cases | `DOC_READY` | GTM/browser implementation |
| `DOD-04-09` | Local deterministic redacted previewed user-initiated evidence/support | P1, P5, P6 | golden + output review | `evidence/v1/DOD-04-09/` bundle fixtures | `DOC_READY` | generator absent |
| `DOD-04-10` | Threat/data-flow/retention/auth/browser/custom-code/recovery/incident docs independently reviewed | P0, P6 | doc + independent review | `evidence/v1/DOD-04-10/` review record | `DOC_READY` | incident procedure and independent review pending |
| `DOD-04-11` | Product copy avoids unsupported compliance/locality claims | P0, P6 | docs/output scan | `evidence/v1/DOD-04-11/` claim scan | `DOC_READY` | final product/output review pending |

## 5. Customer-optional CRM attribution and native conversions

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-05-01` | Clean install/default commands work without CRM tools/config | P4, P6 | clean install + conformance | `evidence/v1/DOD-05-01/` absence cases | `NOT_STARTED` | executable/package absent |
| `DOD-05-02` | Fixed HubSpot/Salesforce source aggregates, auth, paging, privacy, joins, rejection | P4, P7 | fixture + live/UI | `evidence/v1/DOD-05-02/` per-CRM manifests | `BLOCKED_EXTERNAL` | HubSpot lacks required qualified+revenue thresholded aggregate |
| `DOD-05-03` | Every CRM outcome live-reconciles; traffic cannot substitute | P4, P7 | live/UI | `evidence/v1/DOD-05-03/` report comparisons | `BLOCKED_EXTERNAL` | HubSpot contract unavailable; Salesforce account needed |
| `DOD-05-04` | Native resources, association, reviewed tag, receipt/report states | P4, P7 | conformance + browser + live | `evidence/v1/DOD-05-04/` mapping dimensions/provider | `RESEARCH_ONLY` | exact safe subset/access unproved |
| `DOD-05-05` | No GA4 substitution, person uploads, unsafe identifiers, or arbitrary JS | P1, P4, P6 | negative + network/output | `evidence/v1/DOD-05-05/` rejection cases | `DOC_READY` | enforcement absent |
| `DOD-05-06` | Both CRM minimum workflows and one safe native mapping per ad provider | P4, P7 | integrated live/UI | `evidence/v1/DOD-05-06/` baseline manifest | `BLOCKED_EXTERNAL` | HubSpot blocker plus all native live proof |
| `DOD-05-07` | Missing optional access stays isolated; selected composite fails preflight | P1, P4 | conformance + negative | `evidence/v1/DOD-05-07/` optionality cases | `DOC_READY` | capability/preflight implementation absent |

## 6. Reporting and evidence correctness

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-06-01` | All rows/findings/recommendations/evidence match schemas | P1, P2, P5 | schema/property + conformance | `evidence/v1/DOD-06-01/` validation corpus | `DOC_READY` | schemas absent |
| `DOD-06-02` | Integer money and explicit time/currency/attribution/freshness/quality | P1, P2 | property + fixture | `evidence/v1/DOD-06-02/` golden vectors | `DOC_READY` | implementation absent |
| `DOD-06-03` | Null/unavailable/pending/sampled/thresholded/zero distinct | P1, P2 | property + fixture | `evidence/v1/DOD-06-03/` status vectors | `DOC_READY` | implementation absent |
| `DOD-06-04` | Sources remain side by side; no causal claim | P2, P5 | fixture + output review | `evidence/v1/DOD-06-04/` report snapshots | `DOC_READY` | report renderer absent |
| `DOD-06-05` | Evidence has sufficient digests while excluding prohibited content | P1, P6 | golden + leak scan | `evidence/v1/DOD-06-05/` manifests/scans | `DOC_READY` | evidence implementation absent |

## 7. Durable history and uniform creative intelligence

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-07-01` | Fsynced pre-egress start and complete mutation/observation envelopes | P1, P3, P4 | property + process fault | `evidence/v1/DOD-07-01/` ordering traces | `DOC_READY` | ledger absent |
| `DOD-07-02` | Unresolved starts block retry across termination boundaries | P1, P3 | process fault + conformance | `evidence/v1/DOD-07-02/` crash matrix | `DOC_READY` | ledger/coordinator absent |
| `DOD-07-03` | Verify corruption/duplicates/torn tail/concurrency | P1 | property + process fault | `evidence/v1/DOD-07-03/` ledger corpus | `DOC_READY` | ledger absent |
| `DOD-07-04` | Snapshot diff records external changes honestly | P2, P3, P7 | fixture + live/UI | `evidence/v1/DOD-07-04/` diff records | `DOC_READY` | adapters/snapshots absent |
| `DOD-07-05` | Restatements append revisions and as-of reproduces prior reports | P1, P2 | property + fixture | `evidence/v1/DOD-07-05/` revision vectors | `DOC_READY` | ledger/report implementation absent |
| `DOD-07-06` | Upcast/rotation/migration/archival/compaction preserve history | P1, P6 | property + migration | `evidence/v1/DOD-07-06/` migration corpus | `DOC_READY` | implementation absent |
| `DOD-07-07` | SQLite rebuild and optional DuckDB use verified checkpoints | P1, P5 | integration + process fault | `evidence/v1/DOD-07-07/` rebuild/query cases | `DOC_READY` | indexes absent |
| `DOD-07-08` | Retention defaults/policies/dependencies/receipts/truthful gaps | P1, P5, P6 | property + process fault | `evidence/v1/DOD-07-08/` retention cases | `DOC_READY` | cleanup engine absent |
| `DOD-07-09` | Deterministic JSON/CSV checkpoint projections across OSes | P1, P5, P6 | golden + cross-platform | `evidence/v1/DOD-07-09/` byte digests | `DOC_READY` | projector/CI absent |
| `DOD-07-10` | Common creative identity/features/provenance/missing statuses | P3, P5 | golden + conformance + live | `evidence/v1/DOD-07-10/` creative manifests | `DOC_READY` | analyzer/adapters absent |
| `DOD-07-11` | Local image/video/text extraction deterministic and non-inferential | P3, P5 | golden | `evidence/v1/DOD-07-11/` analyzer corpus | `NOT_STARTED` | analyzer absent |
| `DOD-07-12` | Material changes version; association-only changes deployments; mixed windows labeled | P3, P5 | property + fixture + live | `evidence/v1/DOD-07-12/` identity/join vectors | `DOC_READY` | implementation absent |
| `DOD-07-13` | Responsive/dynamic component non-inference | P2, P3, P5 | fixture + live | `evidence/v1/DOD-07-13/` grain cases | `DOC_READY` | provider report mappings absent |
| `DOD-07-14` | Multi-rendition metrics never fan out or double count | P2, P5 | property + fixture | `evidence/v1/DOD-07-14/` conservation vectors | `DOC_READY` | report implementation absent |
| `DOD-07-15` | Controlled cross-platform creative/version comparison | P5, P7 | integrated live | `evidence/v1/DOD-07-15/` comparison manifest | `NOT_STARTED` | all provider profiles/creative data |
| `DOD-07-16` | Creative CSV reconstructs normalized model; media retention/scans obey policy | P1, P5, P6 | projection + privacy | `evidence/v1/DOD-07-16/` reconstruction/scans | `DOC_READY` | vault/projector absent |

## 8. Installation and distribution contract

Change history: `DOD-08-04`, `DOD-08-05`, `DOD-08-06` and `DOD-08-16` were revised on
2026-10-11 for the free, open-source model decided in
[ADR 0013](adr/0013-open-source-release.md). Each keeps its ID; the earlier wording
described editions and paid distribution.

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-08-01` | Reproducible signed macOS/Windows/Linux and OCI | P6, P7 | reproducibility + signing | `evidence/v1/DOD-08-01/` artifact manifests | `DOC_READY` | code/builds and signing identities absent |
| `DOD-08-02` | Checksums, SBOM, dependency/license scans, provenance | P6, P7 | supply-chain/reproducibility | `evidence/v1/DOD-08-02/` scan/attestation files | `DOC_READY` | build pipeline absent |
| `DOD-08-03` | Fresh install/init/update opt-in/out/upgrade/uninstall | P6 | clean-machine matrix | `evidence/v1/DOD-08-03/` platform results | `NOT_STARTED` | package absent |
| `DOD-08-04` | Release status and provider labels discoverable; no editions; unreleased writes refused at one gate | P1, P5, P6 | conformance + install | `evidence/v1/DOD-08-04/` release-policy cases | `DOC_READY` | v1 release-policy coverage of the full command surface absent |
| `DOD-08-05` | Free Apache-2.0 distribution without heartbeat/meter; builds run indefinitely; bring-your-own provider apps | P6 | install/offline/network | `evidence/v1/DOD-08-05/` offline and no-egress checks | `DOC_READY` | v1 package and release pipeline absent |
| `DOD-08-06` | Upgrades preserve history/profile/recovery compatibility | P1, P6 | migration + process fault | `evidence/v1/DOD-08-06/` compatibility matrix | `DOC_READY` | schema/package implementation |
| `DOD-08-07` | External provider constraints disclosed | P0, P6 | docs/capability review | `evidence/v1/DOD-08-07/` reviewed disclosures | `RESEARCH_ONLY` | final versions/access may change |
| `DOD-08-08` | Versioned agent instructions/schema/capability/noninteractive diagnostics | P5 | deterministic caller + contract | `evidence/v1/DOD-08-08/` caller results | `DOC_READY` | CLI absent |
| `DOD-08-09` | Agent-mode bounds, exits, no hangs, pending and ambiguity continuation | P1, P5 | conformance + process fault | `evidence/v1/DOD-08-09/` protocol cases | `DOC_READY` | CLI/kernel absent |
| `DOD-08-10` | Non-CLI marketers complete agent-led workflow; named-host evidence | P5, P7 | usability + live | `evidence/v1/DOD-08-10/` consented study records | `NOT_STARTED` | executable and partner sessions |
| `DOD-08-11` | Artifact preferences/defaults/overrides/rules/reuse/failures | P5 | property + usability | `evidence/v1/DOD-08-11/` preference matrix | `DOC_READY` | renderer/preferences absent |
| `DOD-08-12` | Inline and optional artifacts preserve authoritative facts | P5 | golden + usability | `evidence/v1/DOD-08-12/` render comparisons | `DOC_READY` | rendering absent |
| `DOD-08-13` | Skills packaged, compatible, offline-tested, named-host validated | P5, P7 | package + offline + usability | `evidence/v1/DOD-08-13/` skill manifests/results | `NOT_STARTED` | executable command contract/host studies |
| `DOD-08-14` | Adversarial agent/artifact cases cannot bypass kernel | P1, P5, P6 | adversarial + conformance | `evidence/v1/DOD-08-14/` negative corpus | `DOC_READY` | kernel/renderer absent |
| `DOD-08-15` | Agent-visible sharing is redacted; core works without model | P1, P5, P6 | leak scan + deterministic caller | `evidence/v1/DOD-08-15/` output/caller results | `DOC_READY` | implementation absent |
| `DOD-08-16` | Open maintenance boundary documented (contributing, security); paid support, services and sale not gates | P6 | docs review | `evidence/v1/DOD-08-16/` reviewed maintenance policy | `NOT_STARTED` | maintenance policy review not recorded |

## 9. Live validation and soak

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-09-01` | Six ad providers, GA4, GTM live on accounts other than the maintainer's development accounts | P7 | live | `evidence/v1/DOD-09-01/` profile manifests | `NOT_STARTED` | implementation and credentials |
| `DOD-09-02` | LinkedIn lifecycle on two external organizations | P7 | live/UI | `evidence/v1/DOD-09-02/` two org manifests | `NOT_STARTED` | LinkedIn app/org access |
| `DOD-09-03` | Each provider full paused-create/minimal-activate/pause/reconcile flow | P7 | live/UI | `evidence/v1/DOD-09-03/` six lifecycle runs | `NOT_STARTED` | all adapters/accounts |
| `DOD-09-04` | CLI and UI changes captured in verified JSONL/CSV during soak | P7 | live/UI + ledger | `evidence/v1/DOD-09-04/` change/export evidence | `NOT_STARTED` | ledger/adapters/accounts |
| `DOD-09-05` | One creative format/provider and one cross-platform family comparison | P7 | live/UI | `evidence/v1/DOD-09-05/` creative manifests | `NOT_STARTED` | analyzer/adapters/assets/accounts |
| `DOD-09-06` | All-six integrated pre-write fault and clean happy path | P7 | integrated live fault | `evidence/v1/DOD-09-06/` partial/clean runs | `DOC_READY` | harness, all adapters/profiles |
| `DOD-09-07` | Required auth/quota/paging/drift/partial/ambiguity/review/async failure drills | P7 | live fault | `evidence/v1/DOD-09-07/` drill matrix | `DOC_READY` | executable and safe profiles |
| `DOD-09-08` | Thirty consecutive qualifying calendar days | P7 | soak | `evidence/v1/DOD-09-08/` daily signed entries | `NOT_STARTED` | all live gates must pass first |

## 10. Release evidence closes the goal

| Requirement ID | Release area | Phase | Test class | Evidence location/type | Current status | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| `DOD-10-01` | Final reversible smoke on every ad provider from exact commit | P7 | release smoke + live | `evidence/v1/DOD-10-01/` six smoke runs | `NOT_STARTED` | release commit and all prior gates |
| `DOD-10-02` | Final GA4 and reversible GTM smokes from exact commit | P7 | release smoke + browser/live | `evidence/v1/DOD-10-02/` measurement smokes | `NOT_STARTED` | release commit/access |
| `DOD-10-03` | Machine manifest indexes every check/version/profile/commit/digest/review | P6, P7 | manifest validation | `evidence/v1/manifest.json` | `DOC_READY` | evidence not produced |
| `DOD-10-04` | Final JSONL heads and CSV manifest recorded | P7 | ledger/projection verification | `evidence/v1/DOD-10-04/` checkpoints | `NOT_STARTED` | final live ledger absent |
| `DOD-10-05` | Signed binaries/OCI reproduce and match manifest | P6, P7 | reproducibility + signing | `evidence/v1/DOD-10-05/` build/signature proof | `DOC_READY` | build system and signing identities |
| `DOD-10-06` | No waiver or mock/fixture/development-account/stale-only evidence | P7 | independent evidence review | `evidence/v1/DOD-10-06/` review decision | `NOT_STARTED` | complete evidence set absent |
| `DOD-10-07` | Signed `v1.0.0` tag on exact passing commit, last | P7 | signature/tag verification | `evidence/v1/DOD-10-07/` public key/tag proof | `NOT_STARTED` | every prior row, signer selection, final review |

## Current blocking summary

- HubSpot cannot currently satisfy `DOD-05-02`, `DOD-05-03`, or `DOD-05-06`
  because no reviewed source-side aggregate supplies qualified outcomes plus
  pipeline/revenue with a source-enforced minimum-cell threshold.
- Provider app/account access, all executable adapters, fixture/conformance
  suites, external profiles, and live/UI evidence remain absent.
- Release-signing identities and protected flows remain pending in
  [`ADR 0011`](adr/0011-release-signing-and-reproducibility.md).
- Regardless of implementation progress, `DOD-09-08` requires a qualifying
  30-day soak, followed by final commit smokes, reproducibility/signing review,
  and `DOD-10-07`. Phase 0 cannot close any of those gates.

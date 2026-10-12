# Definition of done

Last updated: 2026-10-11

## North star

> From an approved campaign brief to a verified conversion signal, a B2B
> marketer can direct their agent through one signed local CLI to review,
> create, launch, maintain, optimize, and report paid media across all six
> launch platforms; configure
> and verify GA4 key events; audit and safely manage GTM; and export redacted
> evidence—using customer-controlled credentials, without a required vendor
> service or vendor-imposed usage meter.

The product covers the complete paid-media operating lifecycle, not all of B2B
strategy. The six advertising connectors are an all-or-nothing release gate,
not an atomic cross-provider transaction. GTM is optional for a customer but its
advertised adapter is required release work. HubSpot and Salesforce remain
customer-optional executable aggregate attribution adapters in v1.

Campaign operations and measurement assurance are one bundled release. Apple
Search Ads / App Store advertising is backlogged and creates no v1 credential,
implementation, validation, soak, or release requirement.

## Meaning of complete

The project is `COMPLETE` only when every required item below has linked,
redacted evidence and the exact passing commit is signed and tagged `v1.0.0`.
Compiling code, a polished CLI, fixture-only connectors, results from the
maintainer's own development accounts, or reaching the credential handoff does
not satisfy this definition.

The post-v1 conversion architecture assistant creates no v1 capability,
credential, live-validation, soak, evidence, or completion requirement. It
cannot delay `COMPLETE`. Promotion requires a separate product-owner goal,
capability matrix, threat model, definition of done, and release claim.

### Authorization evidence applies to every mutation

Direct exact-plan approval and explicit bounded user delegation are both valid
paths under `authorization-policy.md`. Per-change human presence, biometric
approval, or a separate approval UI is not a release requirement. Every old
reference below to an approval means a one-use digest-bound authorization from
one of these paths; evidence must identify which path was used.

Tests prove policy scope, cumulative spending/change limits, expiry/revocation,
atomic consumption across concurrent processes and restarts, no agent policy
self-expansion, and no redundant prompts for covered work. Activation and
publish need their own permitted operation kinds but may be delegated. Current
privacy, consent, drift, ledger, ambiguity, and recovery guarantees remain.

### 1. Complete lifecycle parity on six ad platforms

Google Ads, Meta Ads, LinkedIn Ads, TikTok Ads, Reddit Ads, and OpenAI Ads each
complete:

```text
brief -> inspect -> report -> audit -> recommend -> plan -> approve
-> create paused/draft -> validate -> separately approve activation
-> activate -> verify -> maintain/reconcile -> export evidence
```

For every provider:

- authorization, refresh/reauthorization, revocation guidance, account discovery,
  deterministic identity, scopes, API version, pagination, quotas/rate limits,
  currency, timezone, attribution, freshness, and normalized reporting work;
- inventory and reports reconcile to the provider UI for identical accounts,
  dimensions, dates, timezone, and currency within documented tolerances;
- audits cover structure, delivery, spend, bids, targets, placements, creative,
  destination, tracking, conversion health, drift, and relevant policy state;
- recommendations cite evidence, separate observations from assumptions, expose
  confidence/risk/expected impact, and never write automatically;
- a typed campaign brief deterministically compiles to a complete provider-
  native spec, with every unsupported or lossy mapping visible before writes;
- a valid campaign hierarchy, including required child resources and serving
  inputs, can be created in paused or provider-native draft state;
- supported objective, targeting, budget, bid, schedule, placement, destination,
  controlled UTM, conversion configuration, and customer-supplied creative
  upload/reference/association are live-verified;
- activation is a distinct, expiring, account-bound, spend-explicit approval;
- pause/archive and the documented maintenance/replacement operations for status,
  spend controls, bid, schedule, targeting, destination, tracking, and creative
  association are live-verified;
- every plan is content-addressed, shows before/after state and maximum spend,
  re-reads state, rejects drift, and never blindly retries ambiguity; and
- verification, partial/ambiguous results, recovery, and deterministic redacted
  evidence satisfy [`lifecycle-contract.md`](lifecycle-contract.md).

Provider-native object differences are expected. A fixture, SDK wrapper, TODO,
manual routine UI completion step, or reporting-only connector is incomplete.
All selected provider subplans must compile before any provider write; apply is
non-atomic and reports per-provider outcomes honestly.

### 2. GA4 reporting and administration

- Read-only Data API and separate Admin API edit authorization profiles are
  implemented with least-privilege scope and independently tested revocation and
  insufficient-scope behavior.
- Expected property and stream identity are verified before each operation.
- Aggregate campaign/key-event reporting works across pagination, timezone,
  currency, attribution, freshness, thresholding, sampling, and date boundaries.
- Deterministic joins work; heuristic, conflicting, and unmapped rows remain
  visible and excluded from authoritative totals.
- Missing, stale, duplicate, thresholded, sampled, and processing-pending states
  never masquerade as zero.
- Supported event-create/edit rules and key-event create/update/decommission
  operations pass plan/approve/drift/apply/verify live tests.
- Counting behavior and supported default value/currency are represented; key-
  event immutability and prospective/no-backfill behavior are explicit.
- A test event is observed in a real browser and GA4 Realtime, then reconciled
  in a processed standard report and the GA4 UI.
- No audience export, raw event export, client ID, user ID, person-level row, or
  prohibited sensitive field enters the product.
- Documentation and output never imply that a GA4 key event automatically
  becomes a Google Ads conversion or another provider's native signal.

### 3. GTM Web capability

- A clean customer install with no GTM still passes `doctor`, reporting, ad
  operations, tests, packaging, and release checks; GTM reports
  `NOT_CONFIGURED_OPTIONAL`.
- The shipped GTM Web adapter passes the complete measurement workflow on at
  least two organizations/containers other than the maintainer's development
  accounts.
- GTM read, edit/version, and publish authorization are separately represented;
  revocation and insufficient scopes fail closed without leaking auth data.
- Inventory reconciles to the GTM UI across pagination for all applicable
  resource types and every accessible workspace promised in
  [`measurement-control.md`](measurement-control.md), including live/latest
  version identity, destinations, Google tag configurations, zones, and
  container-feature-gated resources.
- Dependency fixtures cover direct, indirect, sequencing, exception,
  destination/Google-tag configuration, zone, custom-template, custom-code
  string, broken, and dynamic references across live/latest state and all
  accessible pending workspaces. An unreadable applicable surface blocks
  deletion/publication; no test or live cleanup deletes a resource not
  classified `PROVEN_UNREFERENCED`.
- Duplicate, double-firing, broad-trigger, stale/paused tag, broken/orphaned,
  consent-order, sensitive-data, unapproved-domain, dirty-workspace,
  workspace-conflict, and live-drift diagnostics have positive and negative
  tests.
- Evidence-backed cleanup can consolidate, tighten, rename/document, pause,
  migrate, and safely remove supported resources inside a dedicated workspace.
- A live/latest mismatch and a full workspace allocation both fail closed. The
  CLI never silently calls `set_latest`, reuses/deletes a customer workspace,
  syncs away customer work, or resolves a customer conflict.
- A Google/GA4 tag and conversion trigger are created from an approved event
  contract, staged, statically checked, passed through GTM `quick_preview`, and
  tested in a real browser before version creation.
- The created immutable version's canonical resource digest/fingerprints equal
  the browser-tested draft; otherwise the preview/approval is invalidated and
  the exact immutable version is browser-tested before publish.
- The immutable version receives a separate publish approval, is published only
  after a live-state drift check, fires exactly as intended, is observed in GA4
  Realtime, and later appears in processed reporting.
- Denied and granted consent states are browser-tested; denied state sends no
  disallowed request and no code path overrides denial.
- On a disposable, traffic-free validation site, GTM container, and GA4
  property, a benign synthetic verifier failure after publish produces
  `RESTORE_REQUIRED`; the exact previously live version is republished and
  verified. Never knowingly deploy broken or data-leaking configuration to a
  customer production container to test recovery.
- Timeout/interruption tests prove publish and restore are not blindly retried
  and produce `AMBIGUOUS_REMOTE_STATE` when state cannot be proven.
- Missing site/data-layer signals produce `NEEDS_SITE_INSTRUMENTATION` plus a
  machine-readable safe event contract, never scraped form values or invented
  lead semantics.
- Existing custom HTML, custom templates, and other custom-code resources are
  locally classified, but raw source never reaches logs, evidence, support
  bundles, telemetry, or remote analysis.

### 4. Privacy, consent, and local-operation properties

- No mandatory vendor proxy, hosted database, telemetry, crash upload, automatic
  updater, license heartbeat, or command/account/spend/row/API-call meter exists.
- Network-capture tests allowlist and explain every ad, GA4, GTM, browser-preview,
  and optional update destination. Unapproved egress fails closed.
- Interactive secrets use the operating system credential store and are absent
  from source, Git history, config, process arguments, plans, diffs, logs,
  evidence, errors, fixtures, and support bundles.
- Read, edit, activation, and publish scopes are separated where providers
  permit it.
- Regulated mode accepts only declared public marketing hosts/routes and rejects
  person-level identifiers, audience files, raw leads, DOM/page text, entered
  form values, sensitive URL/query/data-layer values, sensitive event semantics,
  raw click IDs, and person-level conversion data before display, persistence,
  logging, evidence, or transmission.
- Schema-approved operational request bodies and account/resource identifiers
  are transmitted only to the exact authorized provider operation. Approved
  consent-granted browser tests may use provider-managed cookies. Raw cookies,
  auth headers, and raw request/response bodies never enter display, persistence,
  logs, evidence, or support bundles; operational IDs are pseudonymized on
  export.
- PHI/PII fixtures placed in every relevant input and remote-response surface are
  rejected before egress or evidence creation.
- The CLI validates an existing CMP only against a customer-declared consent
  contract; it does not invent policy, install a CMP, default to acceptance, or
  override denial.
- Logs, evidence, and support bundles are local, deterministic, redacted,
  previewable, and user-initiated. Raw provider responses are not persisted by
  default.
- Threat model, data-flow/egress map, retention behavior, credential rotation and
  revocation, browser-driven egress, custom-code risk, non-atomic recovery, and
  incident process are documented and independently reviewed.
- Product copy does not claim HIPAA or GLBA compliance, an air gap, zero legal or
  security review, or that configured provider traffic stays local.

### 5. Customer-optional CRM attribution and native conversions

- A clean install, default `doctor`, all default commands, required tests, and
  packaging succeed with no HubSpot/Salesforce CLI, MCP, configuration, or
  credentials.
- HubSpot and Salesforce fixed aggregate adapters pass source-side aggregation,
  account identity/scopes, pagination, revocation, minimum-cell privacy,
  deterministic mapping, attribution labeling, and rejection tests. They retrieve
  no raw CRM records and expose no generic query or tool passthrough.
- Each advertised CRM outcome has live source/UI reconciliation on an authorized
  independent account, including qualified outcomes/pipeline/revenue where
  supported; traffic totals do not substitute for unavailable business outcomes.
- Provider-native conversion resources, campaign-goal associations, and reviewed
  native event/tag configuration follow `conversion-integrations.md`. Record
  exact supported formats, event contracts, consent/egress behavior, and verified
  configuration, browser, destination receipt, and processed-report states.
- Never advertise unsupported native signals or substitute a GA4 key event for
  destination-native verification. Unsafe identifier requirements fail closed;
  no person-level uploads, raw CRM rows, or arbitrary JavaScript are introduced.
- The minimum in `conversion-integrations.md` is required: both CRM adapters
  prove at least one qualified-outcome and pipeline/revenue aggregate workflow,
  and every ad provider proves at least one safe native conversion mapping
  through its applicable setup, association, signal, receipt, and reporting
  evidence. Empty advertised subsets and error-only adapters do not satisfy it.
  All unproven additional operation/format cells remain explicit.
- Missing optional CRM configuration, or one customer's unavailable advertising
  access, never blocks independent configured workflows. Explicitly selected
  composite operations still fail preflight when a required step is unavailable.

CRM metadata discovery, website crawling, and the remainder of
`future-conversion-architecture.md` remain post-v1. Their absence is not a
completion requirement for the promoted aggregate/native conversion subset.

### 6. Reporting and evidence correctness

- Every normalized row, join state, data-quality status, audit finding,
  recommendation, and evidence envelope follows
  [`reporting-schema.md`](reporting-schema.md).
- Money uses integer minor units; timezones/date boundaries, currencies,
  attribution models/windows, freshness, thresholding, and processing delays are
  explicit.
- Null, unavailable, pending, sampled, thresholded, and zero are distinguishable.
- Provider, GA4, and future CRM results remain side by side; no output presents
  correlation or attributed credit as causal incrementality.
- Evidence retains sufficient version, query, plan, approval, identity, state,
  graph, result, and recovery digests for deterministic review while excluding
  raw payloads, credentials, identifiers, sensitive values, and custom code.

### 7. Durable history and uniform creative intelligence

- Every remote write appends and durably syncs a schema-valid `APPLY_STARTED`
  record before network egress, then appends the observed result, read-back
  verification, and any recovery outcome. Plans, approvals, externally observed
  changes, GA4/GTM changes, and superseding performance observations use the
  same versioned common record envelope without secrets or raw provider bodies.
- An unresolved `APPLY_STARTED` forces reconciliation and can never be treated
  as permission to retry. Tests cover process termination before egress, during
  egress, and before result append, plus partial provider outcomes.
- `history verify` detects duplicate IDs or sequences, digest-chain breaks,
  unsupported schemas, modified middle records, and corrupt manifests. A torn
  final line has a documented recoverable path; corruption elsewhere fails
  closed. Concurrent writers cannot interleave or lose records.
- Reconciliation compares allowlisted normalized snapshots and records changes
  made through provider UIs or automations with an observation interval and an
  unknown actor unless provider-native evidence proves more. Native provider
  change histories supplement rather than replace local snapshot diffing.
- Provider conversion backfills and restatements append new performance
  observations that supersede prior observations. An as-of high-watermark
  reproduces the report originally seen; prior records are never overwritten.
- Schema upcasters, segment rotation, migration, archival, and explicit
  compaction preserve old readable records, record identity, provenance, and
  digest verification. The CLI never silently deletes history.
- SQLite is the default disposable index, rebuilt from verified JSONL after
  loss/staleness. Optional DuckDB analyses use consistent verified projections.
  Tests cover index/checkpoint consistency and interrupted rebuilds.
- Retention configuration defaults to preserving history. Test explicit saved
  cleanup policies, previews, protected recovery/foreign-key dependencies,
  surviving receipts, and truthful as-of coverage after pruning.
- JSONL is the canonical store. JSON and the normalized CSV bundle are generated
  only from a verified ledger high-watermark; repeated exports with identical
  inputs are byte-identical across supported operating systems and carry a
  manifest with schema versions, filters, row counts, high-watermarks, and
  SHA-256 digests. CSV tests cover RFC 4180 escaping, Unicode, null/status
  preservation, integer minor-unit money, stable ordering/newlines, and formula-
  injection protection, including reversible collisions between formula-leading
  and apostrophe-leading source strings.
- Every supported provider records stable creative-family, creative-version,
  asset-version, rendition, and effective-dated deployment identities. Exact
  supported platform copy, on-visual text, CTA label/type/location/timing,
  assets, palette/color proportions, format, execution, message taxonomy,
  provenance, confidence, and explicit missing/inapplicable statuses are
  available through the common creative schema.
- Local golden image, video, and text fixtures verify asset digests, dimensions,
  duration, palette, visual structure, OCR/transcript text, CTA/message
  extraction, timing, analyzer version, and deterministic results. The analyzer
  performs no identity, biometric, protected-trait, health, or financial-trait
  inference.
- Any asset, copy, CTA, destination, component ordering, or rendered-content
  change creates a new content-addressed creative version even when the provider
  keeps the ad ID. A pure ad-to-creative association change preserves the
  version and closes/opens the effective deployment interval. Windows crossing
  versions are split or labeled `MIXED_CREATIVE_VERSION`.
- Responsive search and dynamic-creative pools preserve component eligibility,
  pinning, and provider-reported exposure. No report attributes ad-level results
  to a headline, visual, color, or combination the provider did not expose.
- Multi-rendition and carousel fixtures prove ad-level metrics are never copied
  once per eligible asset. Exact rendition/card rows require provider-exposed
  grain; mutually exclusive partitions reconcile to their parent and explicitly
  non-additive metrics remain labeled and unsummed.
- A controlled cross-platform test holds the declared audience, offer, landing
  destination, and measurement contract constant; changes only the named
  creative dimensions; and proves ad-level performance joins to the correct
  effective creative versions with platform differences labeled as
  directional.
- The CSV creative tables reconstruct the normalized creative catalog, source
  assets, exact renditions, components, palettes, deployments, and performance
  joins without a lossy wide row. Imported/provider media is either transient or
  retained only in an explicitly configured local vault, and sensitive-data
  scans run before persistence or export.

The authoritative storage and analysis contracts are
[`local-ledger-and-exports.md`](local-ledger-and-exports.md) and
[`creative-intelligence.md`](creative-intelligence.md).

### 8. Installation and distribution contract

- Reproducible signed binaries run on the documented supported macOS, Windows,
  and Linux versions; a pinned OCI image is also reproducible.
- Release checksums, SBOM, dependency and license scans, and provenance
  attestations ship with each maintained build.
- Fresh-machine install, initialization, optional signed update check, update-
  check opt-out, upgrade, and uninstall work without engineering help.
- Every build discloses its release status separately from provider support and
  credential permissions: which commands are available, which remote-mutation
  commands are `UNRELEASED`, and each provider's release label. There are no
  editions or tiers. A build that has not released writes refuses every remote
  mutation at one release-policy gate, before credential-store access, ledger
  `APPLY_STARTED`, or network egress, even with broader provider credentials.
  Audit and report checks cannot mutate provider state or initiate browser
  measurement traffic, and findings, history, and safe exports stay useful in
  every build. See [`release-0.1.0.md`](release-0.1.0.md).
- AdHelm is free software under Apache-2.0
  ([ADR 0013](adr/0013-open-source-release.md)). There is no paid tier, license
  key, trial, vendor-registered provider application, token relay, or paid
  support, and no runtime license service, heartbeat, or meter on commands,
  executions, accounts, spend, rows, or API calls. A build stays runnable
  indefinitely. Users register their own provider applications from a documented
  per-provider setup guide and bring their own credentials. Upstream
  incompatibility is reported honestly.
- Upgrades preserve verified history and profile continuity. Shared
  schema changes cannot silently strand retained builds or interrupted work;
  supported migration/export and guarded recovery paths are documented/tested.
- Upstream provider quotas, approvals, access tiers, breaking changes, and
  certification requirements are disclosed as external constraints.
- Versioned agent instructions, schemas, capabilities, examples, noninteractive
  structured results, and actionable diagnostics implement the primary agent
  interface in
  [`lifecycle-contract.md`](lifecycle-contract.md#agent-facing-interface-and-human-artifacts).
  A deterministic caller exercises it without an external model in default CI.
- Agent-mode checks cover output bounds/pagination, no silent truncation,
  stable outcome/exit semantics, missing auth/approval without terminal hangs,
  pending-operation status checks, interrupted execution, and reconciliation of
  ambiguity without mutation replay.
- Primary usability validation includes marketers without CLI experience
  directing a customer-selected agent through report, diagnosis, plan, preferred
  review, direct or delegated authorization, execution, and verification. They
  need not
  type commands, construct JSON, or copy digests. Record host/version, time,
  founder/support intervention, failures, and confusion through consented local
  evidence. Named Claude Code/Codex compatibility claims require successful
  validation in that host; fixture-only checks do not prove the experience.
- User-configurable artifact modes, purposes, workflow-stage rules, formats,
  local destinations, detail bounds, reuse, and overwrite policy pass validation.
  Tests cover compact/on-request defaults, `OFF`, task overrides without changing
  saved defaults, rule matching, unchanged-input reuse, unsupported formats,
  missing destinations, and no implicit scheduling or remote upload.
- Both compact inline review with optional artifacts disabled and requested/rule-
  selected artifact workflows are usable. Optional files can explain findings,
  compare options, review integration plans, and report outcomes. Canonical
  source references persist independently of presentation; required review
  facts, plans, approvals, and durable history cannot be disabled by token or
  artifact preferences. No exact external-host token/billing cap is claimed.
- The versioned optimization skill package follows
  [`agent-skills.md`](agent-skills.md). Both skill folders and their referenced
  resources are bundled with release digests and tested CLI/schema compatibility.
  Offline scenarios validate evidence discipline, conversion semantics, output
  preferences, progressive loading, and absent/unsupported CLI behavior. Named-
  host usability evidence separately proves the skills work with that host;
  default skill checks require no external model, account, or provider calls.
- Negative cases include invented commands, stale instructions, unsafe fixes,
  injected provider instructions, misleading AI-written spend summaries, edited
  spreadsheets, stale HTML reviews, and malicious action links. None can bypass
  canonical plan validation, direct/delegated authorization, drift rejection, or the
  existing data/export boundary. Artifact edits proposing operational changes
  require recompilation and new approval; presentation edits do not change the
  canonical plan or approval. Opening a file/link never performs a remote mutation.
- Agent-visible outputs and artifacts use the existing redacted sharing policy.
  The selected agent's possible model egress is disclosed; raw payloads,
  credentials, and prohibited fields do not appear in its output or hidden
  artifact content. The CLI never automatically invokes an assistant. The
  deterministic core, secondary direct path, and default tests work without an
  AI account; that independence does not replace agent-led usability validation.
- Maintenance runs in the open under documented boundaries:
  [`CONTRIBUTING.md`](../CONTRIBUTING.md) states how changes are proposed and
  reviewed, and [`SECURITY.md`](../SECURITY.md) states how vulnerabilities are
  reported. No paid support, optional service delivery, or sale of the tool or
  brand is a requirement for product completion.

### 9. Live validation and soak

- All six ad providers, GA4, and GTM pass required live tests on accounts other
  than the maintainer's development accounts, with credentials supplied outside
  the repository.
- LinkedIn local auth, refresh/reauthorization, revocation, reporting, complete
  paused creation, activation, maintenance, and recovery are proven on at least
  two independent external organizations.
- At least one complete paused/draft campaign stack per advertising provider is
  planned, approved, created, verified, separately activated within a bounded
  zero-or-minimal-spend test, paused/archived, and reconciled or restored.
- For every provider, one CLI-authored change and one controlled out-of-band UI
  change are recovered in JSONL history and deterministic CSV exports with the
  correct resource, field delta, observation interval, origin, and verification
  state. The resulting ledger passes `history verify` throughout the soak.
- At least one supported creative format per provider is captured through the
  common creative schema and reconciled to the provider UI or asset source. A
  cross-platform comparison of one shared creative family proves exact-version
  joins and preserves provider-specific delivery caveats.
- One integrated acceptance starts from a single `StrategyBrief` targeting all
  six live test profiles, produces a unified inventory/report and evidence-
  backed recommendation, compiles all six subplans before any write, and binds
  one composite approval to every displayed subplan digest. A controlled test-
  harness transport fault then produces a verified no-effect failure for one
  provider after at least one earlier paused/draft subplan succeeds; the run
  stops under its declared policy, consumes started authorizations, reconciles
  every provider, emits complete partial-outcome evidence, creates a new plan
  for any remaining work, and safely archives/restores all test entities. A
  second clean run proves the happy path. No fault is injected into a provider
  after its mutation request begins.
- Failure drills cover expired auth, insufficient scope, rate limits, pagination
  interruption, malformed/partial responses, stale plans, remote drift, partial
  success, ambiguous timeouts, revoked access, network loss, provider review,
  and asynchronous completion.
- Thirty consecutive calendar days run the scheduled `doctor`, `report`,
  `audit`, GA4 health, and configured GTM health checks against validation
  profiles. A day counts only when every scheduled job runs and no product defect
  loses data or reports a false healthy state. Documented upstream downtime may
  count only when correctly detected/classified and recovered within provider
  policy.

### 10. Release evidence closes the goal

- One final reversible plan/approve/create/verify/activate/pause-or-restore smoke
  flow passes on every ad provider from the exact release commit.
- Final GA4 Admin/Realtime/processed verification and one reversible GTM
  publish/verify/restore smoke flow pass from that commit.
- A machine-readable evidence manifest indexes every required check, redacted
  result, independent review, account pseudonym, API/capability version, commit,
  artifact checksum, and completion timestamp.
- The release evidence records verified JSONL segment head digests and the CSV
  export manifest used for final account/change/creative reconciliation.
- Signed binaries and the OCI image reproduce from that commit and match the
  manifest checksums.
- No required criterion has an undocumented waiver or evidence pointing only to
  mocks, fixtures, the maintainer's development accounts, or stale provider
  behavior.
- The exact passing commit receives the signed `v1.0.0` tag only after final
  smoke tests and evidence review.

## Credential handoff checkpoint

Build with the product owner's existing test partners. Access is configured
per provider and customer; no universal vendor-owned app approval is assumed.
Document whether customer-owned apps/keys/OAuth are supported and distinguish
user consent from provider app review and account eligibility.

Early bounded live feasibility work is permitted when the relevant adapter's
identity, schema/privacy, transport, authorization, ledger, and recovery checks
pass and the partner authorizes that exact read or mutation scope. Unrelated
adapters, packaging, and final cross-platform tests need not be finished first.
This does not waive any final release gate or authorize account access by itself.

When a prepared workflow needs customer authorization, report
`READY_FOR_USER_AUTHORIZATION` with a per-provider checklist. Missing one
customer's LinkedIn permission does not stop independent Google/other workflows.
Never ask for secrets in chat; accept only supported local authentication flows.

For each of Google Ads, Meta Ads, LinkedIn Ads, TikTok Ads, Reddit Ads, OpenAI
Ads, GA4, and GTM, the handoff provides:

- exact read/edit/activation/publish scope requested;
- why it is required and whether it can create, publish, activate, or spend;
- the local authentication command/browser flow;
- expected account/property/container identity and how the user verifies it;
- revocation procedure;
- exact bounded live test and maximum possible spend/side effect;
- precomputed pause/archive/restore or reconciliation step; and
- redacted evidence that will be retained.

GA4 reporting versus administration and GTM read/edit/version versus publish are
requested separately. The user authenticates directly with each provider and
never pastes a secret into chat. No spend-capable activation or GTM publish test
runs unless direct approval or an explicit delegation policy covers its exact
bounded plan.

CRM authorization is requested only for a customer-selected implemented
aggregate adapter. Native conversion setup uses the selected destination
profile and declared consent/event contract; configuration, browser egress,
activation, and publish must each be covered by direct or delegated authority.

## Release-signing handoff

After the soak, final tests, and reproducible unsigned artifacts pass, the agent
reports `READY_FOR_RELEASE_SIGNING` if customer-controlled signing identities or
interactive notarization are still required. The handoff lists, without asking
for secret material in chat:

- each required identity and owner: macOS Developer ID/notarization identity,
  Windows Authenticode identity, checksum/release-manifest identity, OCI signing
  identity, and Git tag signing identity as selected by the release ADR;
- the exact local keystore, hardware-backed, OIDC, or CI-secret flow;
- certificate/key identifier and expected public identity, never private key;
- artifact digests, command, side effects, timestamp/notarization network
  destinations, verification command, expiry/rotation, and revocation path; and
- the evidence-manifest entries produced by signing.

Reproducibility means the unsigned payload and build inputs reproduce bit for
bit. Timestamped platform signatures and notarization wrappers are verified as
authentic bindings to that payload and provenance; they are not falsely claimed
to be byte-for-byte reproducible. OCI signing binds the immutable image digest,
and the signed Git tag binds the exact release commit. Signing credentials stay
outside source, chat, logs, fixtures, artifacts, and Git history.

## Completion states

| State | Meaning |
| --- | --- |
| `IMPLEMENTING` | Required code, tests, packaging, research, or documentation remains |
| `READY_FOR_USER_AUTHORIZATION` | Relevant workflow safety gates pass; its bounded live test needs scoped user authorization |
| `LIVE_VALIDATION_FAILED` | A required real-provider behavior failed or remains unproven |
| `SOAKING` | All required live checks pass; the 30-day reliability gate is running |
| `RELEASE_CANDIDATE` | Soak and technical gates pass; final evidence, smoke tests, or tag remains |
| `READY_FOR_RELEASE_SIGNING` | Unsigned payloads and gates pass; user-controlled signing/notarization is required |
| `COMPLETE` | Exact release commit passes every gate and is signed/tagged `v1.0.0` |

Only `COMPLETE` ends the build goal. An agent can begin today and may reach
`READY_FOR_USER_AUTHORIZATION` after credential-free implementation, but it
cannot truthfully reach `COMPLETE` before live validation and the 30-day soak.
An external API blocker does not authorize a partial release or a fabricated
success state.

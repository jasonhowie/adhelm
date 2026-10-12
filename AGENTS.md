# AGENTS.md

This is the contract for everyone who changes this repository, whether a person
or an AI agent. It states what the project is, what it must never do, and how a
change is verified. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the contribution
process.

## What this project is

AdHelm is a free, open-source (Apache-2.0), local, agent-first command-line tool
for operating and measuring B2B paid media. The marketer directs their own AI
agent; the agent discovers and invokes the typed `adhelm` CLI. Credentials and
canonical history stay on the user's machine. There is no paid tier, edition,
hosted service, vendor-registered provider application, token relay, pricing or
paid support. Users register their own provider applications and bring their own
credentials.

## Current release: 0.1.0

The first public release, `0.1.0`, is read-only. Read
[ADR 0013](docs/adr/0013-open-source-release.md) and the
[0.1.0 release definition](docs/release-0.1.0.md) before any other work. Where
they conflict with older text below or in the contract files, they win:

- `0.1.0` covers authentication lifecycle, inventory, aggregate reporting,
  audits, recommendations, local history, reconciliation and exports for Google
  Ads, Meta Ads, LinkedIn Ads, TikTok Ads, Reddit Ads, OpenAI Ads, GA4 and GTM.
- Every remote write is refused at runtime in `0.1.0` with `WRITES_NOT_RELEASED`
  (exit code `3`), before credential-store access, ledger `APPLY_STARTED` and
  network egress. No configuration value, environment variable, flag or edited
  file enables writes. Write code ships so that it can be reviewed; it is not a
  capability.
- Salesforce and HubSpot are out of `0.1.0`.
- Each provider carries one release label, `LIVE_PILOTED` or `EXPERIMENTAL`. The
  label states evidence, never a capability.
- [`definition-of-done.md`](docs/definition-of-done.md) remains the `v1.0.0`
  target. `0.1.0` is gated by the release definition and never claims
  `COMPLETE`.

All 29 non-negotiable rules below still apply.

## Reading order

Before changing product scope, architecture, implementation priorities, or
completion status, read these files completely and in this order:

1. `README.md`
2. `docs/product-brief.md`
3. `docs/lifecycle-contract.md`
4. `docs/creative-intelligence.md`
5. `docs/measurement-control.md`
6. `docs/reporting-schema.md`
7. `docs/local-ledger-and-exports.md`
8. `docs/capability-matrix.md`
9. `docs/attribution-sources.md`
10. `docs/definition-of-done.md`
11. `docs/agent-skills.md`
12. `docs/authorization-policy.md`
13. `docs/conversion-integrations.md`

After the v1 contract, `docs/future-conversion-architecture.md` is a design-only
roadmap reference. It is explicitly not a `v1.0.0` requirement or authorization
to expand the build.

[`backlog/phase-2-customer-hosted-mcp.md`](backlog/phase-2-customer-hosted-mcp.md)
is a design-only roadmap track. It adds no implementation, credentials, or
completion requirements. "Security lite" means reduced customer hosting control,
never weakened execution safeguards. Implementation requires separate promotion
of the track and its explicit contract changes.

The documents above are the source of truth. When a summary elsewhere becomes
stale, the documents win and the summary is corrected.

## Validating a change

Run these from a checkout with Python 3.12 and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen --python 3.12.13
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -n 2 --dist=loadfile --cov=adhelm --cov-report=term-missing
uv build
uv run python scripts/check_repository.py
```

Default tests use synthetic data and fake providers. They never read an
operating-system credential store, contact a provider, or use a person's data.
Do not weaken an assertion, add a skip without a stated reason, or turn a
failure into a refusal to get a check to pass.

## Non-negotiable rules

1. Never commit credentials, tokens, private keys, customer exports, or raw API
   payloads. Environment-file examples contain placeholders only.
2. Do not copy another private repository, its environment files, credentials,
   raw data, or Git history into this repository. Generic code must be
   deliberately extracted, provenance-recorded, adapted, and audited.
3. The default runtime has no vendor proxy, hosted database, telemetry, crash
   upload, automatic updater, or remote license heartbeat.
4. Store interactive secrets in the operating system credential store.
   Environment variables are acceptable only for explicitly configured CI use.
5. Request least-privilege scopes and support separate read, edit, activation,
   and publish profiles where the provider permits it.
6. Every remote mutation follows `doctor -> plan -> authorize exact digest ->
   re-read -> reject drift -> apply -> verify -> recover/export evidence`.
   Authorization can be direct or issued under an explicit user-configured
   delegation policy; fresh human confirmation is not required for covered
   work. Never retry an ambiguous write blindly or let an agent broaden its grant.
7. Compile and validate every provider subplan before the first write in a
   multi-provider run. The apply phase is non-atomic and must report per-provider
   results and bounded recovery actions honestly.
8. Create advertising entities in paused or provider-native draft state. Ad
   activation is a separate, spend-explicit, digest-bound authorization that
   may be explicitly delegated under the customer's policy.
9. Stage GTM changes only in a new dedicated workspace after inventorying every
   accessible workspace and proving GTM's live and latest version IDs match. A
   mismatch or unavailable workspace slot fails closed; never silently call
   `set_latest`, reuse/delete a customer workspace, or resolve customer work.
   Creating a version and publishing it are separate operations; publishing
   requires its own scope and authorization, direct or explicitly delegated.
10. GTM `quick_preview` proves synchronization/compiler status, not real-browser
    firing or GA4 receipt. The immutable version must canonically equal the
    browser-tested draft or be retested itself before publish. Runtime and
    processed-data verification are separate required steps.
11. A resource may be deleted only when the dependency engine proves it is
    `PROVEN_UNREFERENCED`. Age, naming, paused status, or apparent inactivity is
    never enough.
12. Regulated mode fails closed on undeclared or sensitive collection. It must
    not accept or transmit person-level identifiers, audience files, raw leads,
    entered form values, DOM/page text, sensitive URL/data-layer values, or
    person-level conversion events.
13. Existing custom HTML, custom templates, and any other custom-code resource
    may be inspected locally, hashed, classified, paused, or migrated through an
    approved plan. Never place raw source in logs, evidence, telemetry, support
    bundles, or remote analysis. Arbitrary JavaScript generation is not a v1
    capability.
14. The CLI may validate an existing CMP against a customer-declared consent
    contract. It never chooses legal policy, overrides denied consent, or
    silently installs a CMP.
15. Logs, plans, evidence, and support bundles are local, redacted, previewable,
    deterministic, and user-initiated. Raw provider responses are not persisted
    by default.
16. GA4 is required. Its Data API reporting profile is read-only; Admin API
    event/key-event changes require a distinct guarded edit profile. A GA4 key
    event is not automatically a Google Ads conversion or a native signal for
    another provider.
17. GTM is optional for an individual customer, but its advertised adapter is a
    required, live-tested v1 capability. An unconfigured customer receives
    `NOT_CONFIGURED_OPTIONAL`; unrelated ad workflows continue.
18. HubSpot and Salesforce fixed aggregate attribution connectors are v1 scope
    and optional per customer. Missing customer configuration never blocks core
    workflows. Advertised adapters need their own implementation and evidence;
    no raw CRM records, generic queries, or person-level uploads.
19. External CLIs and MCP servers remain customer-managed trust boundaries. Do
    not install, authenticate, upgrade, or invoke them automatically, and do not
    expose generic shell, SQL, SOQL, CRM-object, or MCP passthrough.
20. Do not ship stubs, fabricated results, or silent fallback behavior. A
    required capability is either evidenced at its declared status or fails
    explicitly before side effects.
21. Release artifacts are reproducible and signed, with checksums, SBOM,
    provenance, dependency/license scans, and the evidence required by
    `docs/definition-of-done.md`.
22. Canonical account, ad, measurement, performance, and creative history is
    append-only, schema-versioned JSONL. Append and fsync `APPLY_STARTED` before
    mutation egress, reconcile unresolved starts, and never maintain CSV through
    an independent write path. CSV/JSON are deterministic projections from a
    verified ledger high-watermark.
23. Every material asset, copy, CTA, destination, component-order, or rendered-
    content change creates a new content-addressed creative version, even when a
    provider retains an ad ID. A pure ad-to-creative association change preserves
    that version and closes/opens the effective-dated deployment. Capture visual,
    palette, exact text, CTA, message, format, analyzer provenance, and missing-
    data statuses uniformly. Never infer component-level performance when the
    provider exposes only ad-level delivery.
24. Reconciliation compares current allowlisted remote snapshots with local
    history and records out-of-band changes with an observation interval and
    `UNKNOWN` actor unless the provider proves more. It never invents an exact
    external change time or actor.
25. CRM metadata discovery and website crawling are post-v1, capability-gated
    work. Do not probe customer tools or sites, retrieve CRM records, submit
    forms, infer conversion semantics, or relax the v1 DOM/page-text boundary
    under the guise of implementing the roadmap design.
26. V1 bundles campaign operations and measurement assurance across Google Ads,
    Meta Ads, LinkedIn Ads, TikTok Ads, Reddit Ads, and OpenAI Ads. Apple Search
    Ads / App Store advertising is backlogged and cannot block v1 release.
27. The CLI is agent-facing first and human-facing second. The marketer directs
    their chosen agent; that agent discovers and invokes the typed CLI. Do not
    require the marketer to learn shell syntax. Ship versioned agent guidance,
    bounded noninteractive structured results, and actionable diagnostics.
28. Users configure whether, where, when, and for which purposes optional
    document/spreadsheet/HTML artifacts are created. Default to compact chat and
    artifacts on-request; respect saved rules and explicit task overrides.
    Artifacts derive from canonical reports/plans and support direct review
    without manual digest entry or explain policy-delegated execution. Edits
    proposing operational changes require a new compiled plan and direct or
    delegated authorization; model prose, file opening,
    or action links never independently authorize remote mutation. Agent-visible
    output and artifact content follow the existing redacted sharing boundary
    because the chosen agent may forward output to its model. No embedded model,
    automatic external agent invocation, or generic shell/MCP passthrough is
    implied.
29. Bundled optimization skills use installed CLI capabilities, load relevant
    guidance progressively, and honor output preferences. They cannot invent
    commands/results, promote unverified capabilities, extend v1 scope, or
    substitute for valid direct/delegated authorization. Token-saving settings
    do not suppress
    canonical history, mandatory plans, or critical review information.

Do not claim that this product is HIPAA compliant, GLBA compliant, air-gapped,
or exempt from legal, provider-policy, security, or vendor review. Local
execution reduces vendor custody; it does not stop configured providers from
receiving data.

Do not report `COMPLETE` until every required item in
`docs/definition-of-done.md` is evidenced and the exact passing commit is signed
and tagged `v1.0.0`.

## Scope clarifications

All six advertising providers remain v1 scope. Customers use only configured
providers; an individual LinkedIn access failure does not block unrelated
channels. Support customer-owned apps/keys/OAuth where permitted; user consent
does not replace provider-required app review. Bounded live feasibility work is
allowed on explicitly authorized accounts after the relevant safety checks,
without waiting for every unrelated offline gate.

Use SQLite as the default rebuildable local index over canonical JSONL, with
optional DuckDB analytics and explicit configurable retention. Native conversion
resources and reviewed tag/event setup are promoted alongside customer-optional
CRM aggregate attribution; see `docs/conversion-integrations.md`. Website
crawling, CRM metadata discovery, person-level uploads, and arbitrary JavaScript
remain out of scope.

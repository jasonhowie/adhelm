# Product brief

Last updated: 2026-10-11

## Product thesis

Build a local B2B paid-media and measurement control plane for privacy-conscious
healthtech and fintech marketers. The primary interface is for their AI agent
to operate; human command-line use is secondary. Marketers direct campaign work
conversationally and choose compact summaries or generated documents,
spreadsheets, or HTML artifacts to understand, plan, approve, and verify it.
Credentials and canonical operational
records remain in the customer's environment.

Defensible positioning:

> Give your AI agent a dependable local tool to operate paid media across six
> ad platforms, GA4, and optional GTM, with configurable outputs, direct or
> delegated
> authorization, and redacted evidence—using customer-controlled credentials,
> without a required vendor service or vendor-imposed usage meter.

This reduces vendor data custody and can narrow a customer's security-review
surface. It does not remove compliance duties, provider review, or network
egress: every configured advertising and analytics provider necessarily
receives the requests and measurement signals sent to it.

Do not call this the "entire B2B ad strategy." The product operationalizes an
approved paid-media strategy. Humans still own market selection, ICP,
positioning, offer, final creative judgment, legal policy, landing-page work,
sales operations, and causal or incrementality conclusions.

## Customer

The primary user is a capable growth or performance marketer who can describe
business goals and assess proposed changes through their chosen AI agent,
including Claude Code or Codex. They need not know shell syntax, construct JSON,
or operate a conventional CLI. The agent is the direct caller of the product;
the marketer is the decision-maker and approver. Technical marketers and growth
engineers can also operate the CLI directly as a secondary workflow. A marketing
leader or agency owner sponsors adoption, with security, privacy, or compliance
as an approver.

Test partners supply usability and live-validation opportunities. Their
participation is not evidence of adoption or retention until observed.

The adoption hypothesis is that enough marketers already work through agents,
or can learn that conversational workflow, to make a free open-source tool worth
maintaining. The agent discovers the installed tool contract, makes supported
calls, interprets results, creates artifacts when requested or configured, and
helps resolve roadblocks. Success is measured by completed marketing work and
informed review, not by learning terminal commands. Demand, named-host
compatibility, and the maintenance burden remain unvalidated. Provider access,
campaign judgment, consent decisions, and the chosen delegation policy remain
the marketer's responsibility.

Campaign operations and measurement assurance are one bundled product. The
`v1.0.0` proposition combines controlled campaign changes with GA4/GTM signal
verification; it is not a requirement to ship separate products or deliver a
service engagement before the software is useful.

The initial regulated profile is for B2B and HCP/professional campaigns on
declared public marketing pages using aggregate reporting. DTC patient
acquisition, consumer financial eligibility flows, authenticated customer
experiences, and person-level audience or conversion data are outside v1.

## North star

> From an approved campaign brief to a verified conversion signal, a B2B
> marketer can direct their agent through one signed local CLI to review,
> create, launch, maintain, optimize, and report paid media across all six
> launch platforms; configure
> and verify GA4 key events; audit and safely manage GTM; and export redacted
> evidence—using customer-controlled credentials, without a required vendor
> service or vendor-imposed usage meter.

## One-shot advertising-platform scope

- Google Ads
- Meta Ads
- LinkedIn Ads
- TikTok Ads
- Reddit Ads
- OpenAI Ads

All six connectors must satisfy the common release contract before public
`v1.0.0`. Internal implementation can be phased and a customer plan can target
any subset of configured providers. "All or nothing" does not mean a distributed
transaction: these APIs cannot commit or roll back atomically.

Each customer configures only selected providers. Missing LinkedIn approval
or credentials do not block Google Ads or another independently configured
channel. Provider support at product release and access in a customer profile
are different states.

A cross-provider run uses one composite digest over individually digest-addressed provider
subplans and exposes the exact per-provider authorization, result, ambiguous
state, and recovery action.

Provider object models are not falsely flattened. For example, a supported
Google search campaign may require keywords and responsive text components,
while a social campaign uses its own ad-set, targeting, and media structures.
Parity means the same operator outcome expressed through valid provider-native
objects.

## Complete paid-media operating lifecycle

The required operator lifecycle is:

```text
marketer intent through their chosen agent
-> approved campaign brief
-> inspect inventory
-> normalize reports
-> audit campaigns and measurement
-> recommend an evidence-backed action
-> create an immutable plan
-> present required review; generate optional artifacts per user preferences
-> approve its exact digest
-> create paused/draft entities or change existing state
-> validate destinations and measurement
-> separately approve activation or publish
-> verify remote and measurement state
-> maintain, reconcile, and recommend the next plan
-> export redacted evidence
```

Every advertising provider must support:

- local authorization, refresh or reauthorization, revocation guidance, account
  discovery, and deterministic account-identity checks;
- complete paginated inventory and normalized aggregate reporting with currency,
  timezone, attribution-window, freshness, and API-version metadata;
- audits of structure, spend, bidding, targeting, placement, destination,
  creative, tracking, conversion health, drift, and relevant policy risk;
- an evidence-backed recommendation record that exposes observations,
  assumptions, confidence, expected effect, spend impact, and risk;
- deterministic compilation from a typed local campaign brief into a complete
  provider-native plan before any provider is mutated;
- creation of a valid campaign hierarchy in paused or provider-native draft
  state, including all child resources required for later activation;
- supported objective, targeting, budget, bid, schedule, placement, destination,
  UTM, conversion-goal, and customer-supplied creative configuration;
- validation, upload/reference, and association of customer-supplied creative
  assets where the provider permits it—the product does not promise to invent
  final copy or creative;
- separately approved activation with the exact maximum spend and account
  identity visible before execution;
- guarded maintenance of status, budget, bid, schedule, destination, targeting,
  and creative association where mutable, plus replacement workflows where a
  provider requires immutable resources;
- pause/archive-first cleanup, remote-state verification, partial-failure
  reporting, and bounded recovery;
- deterministic, local, redacted evidence export;
- append-only local history for planned, attempted, applied, verified, recovered,
  and externally observed account/campaign/ad/creative changes; and
- uniform creative capture and analysis across exact visuals, palette, copy,
  on-visual text, CTA, message, format, rendition, and execution features.

A reporting-only connector, fixture, SDK wrapper, TODO, or routine manual UI
step does not satisfy parity. Detailed schemas and state transitions live in
[`lifecycle-contract.md`](lifecycle-contract.md).

Optimization may be delegated by the customer. The agent selects supported
changes within a user-configured policy; the deterministic kernel enforces
account, operation, spend, time, data, and recovery bounds for every exact plan.
Direct human approval remains available, but repeated confirmation is not
mandatory. Activation and publish are separate operations that may be explicitly
included in delegation. The agent cannot broaden its own grant. See
[`authorization-policy.md`](authorization-policy.md).

The deterministic core must not require an LLM. Optional model analysis is
explicit and previews the data that would leave the machine.

## Agent-first operation and artifact review

The primary workflow is marketer -> customer-selected agent -> typed local CLI
-> preferred review output -> direct or delegated authorization -> verified execution.
The agent operates the tool; the marketer directs the work conversationally.
Ship versioned agent instructions, discoverable schemas and capabilities,
noninteractive structured results, and actionable diagnostics for that workflow.
Large results use bounded pages or local artifact references, never unbounded
tool output or silently truncated data. Pending work exposes status checks;
ambiguous writes require reconciliation. Human terminal displays are secondary
renderings of the same typed results.

Compact conversational summaries are the default human output. The user controls
whether optional artifacts are created, where they are saved, their formats and
purposes, and when they are generated. Supported purposes span the lifecycle:

- audit and measurement reports explain findings and evidence;
- documents and spreadsheets compare campaign options, budgets, and integration
  mappings, with assumptions and missing data visible;
- plan review artifacts show exact proposed changes, accounts, spend exposure,
  prerequisites, approval boundaries, and recovery actions; and
- result artifacts explain applied, failed, pending, and recovered operations,
  verification evidence, and the next bounded action.

Artifact modes are off, on-request (default), or user-configured rules. Persist
user/profile preferences with explicit task overrides; a one-off request does
not silently change saved defaults. Rules may target selected workflows,
providers, and stages such as audit completion, plan review, verification,
meaningful change, or failure. They do not create a schedule or authorize any
provider operation. The user selects local destinations and supported formats.

To conserve model context, return compact summaries and bounded fields/rows,
load detailed evidence and skills only as needed, reuse unchanged artifacts,
and prefer deterministic projections/templates where useful. Do not generate
the same report in multiple formats unless requested or configured. The CLI can
bound its own output; it cannot promise an exact token or billing limit for a
customer's external agent unless that host supplies and enforces it.

Disabling optional presentation artifacts does not disable canonical plans,
approvals, JSONL history, or the minimum facts needed for safe human review.
Those facts can be reviewed inline without creating a document or spreadsheet.
The full preference contract is in
[`lifecycle-contract.md`](lifecycle-contract.md#output-and-artifact-preferences).

The customer's agent may generate selected documents, spreadsheets, or HTML
views from allowlisted reports, canonical CSV projections, and immutable plans.
The CLI supplies canonical review data and local artifact references, not a
required document SaaS, embedded model, or full web dashboard. At least one
deterministic local inline review rendering supports operation with optional
artifacts off. A generated summary cannot invent evidence, hide a partial
outcome, or supply authority for a write. Artifact edits proposing operational
changes become typed inputs that must be recompiled and approved; they never
change an already approved plan. The human can review and authorize the exact
plan inline or through selected artifacts without typing commands or manually
copying digests, or delegate bounded decisions once through a saved policy.

The agent is customer-managed and calls this product. The product does not
automatically install, authenticate, or invoke the agent. Its deterministic
core and default tests have no model, AI-account, external CLI, or MCP dependency.
This runtime independence does not make the agent-operated experience secondary.
Reference workflows target Claude Code and Codex; published compatibility claims
require evidence from the named host and version.

Agent-facing output follows the existing allowlisted, redacted sharing boundary
by default, because an agent may send tool output to its configured model.
Larger or richer artifacts require the same user-selected data scope and export
preview as other evidence. An external model is a separate customer-selected
data flow; local execution is not a promise that agent-consumed data stays local.
Never recommend pasting credentials, raw provider responses, custom-code source,
or prohibited personal/sensitive data into an assistant.

AI-produced inputs receive the same validation, capability, scope, approval,
drift, spend, and recovery checks as manually authored inputs. Model output and
provider-returned text are untrusted data. An assistant's recommendation or
claim of approval cannot create or broaden a user grant. Existing applicable
delegation supplies authorization without a new human confirmation. The
operational
contract is in [`lifecycle-contract.md`](lifecycle-contract.md#agent-facing-interface-and-human-artifacts).

## Bundled optimization skills

Ship a small versioned skill package with the CLI for the customer's agent:

- `ad-program-optimization`: diagnose delivery, spend/pacing, bidding, targeting,
  placement, and creative performance; prioritize evidence-backed actions and
  supported experiments against the marketer's goals and budget constraints.
- `conversion-optimization`: distinguish measurement faults from weak conversion
  performance; assess approved aggregate evidence and declared conversion
  meaning; recommend supported GA4/GTM changes and bounded conversion experiments.

Skills orchestrate supported CLI operations and provide reasoning guidance.
They honor the user's output preferences and load only relevant material; they
do not require an artifact for every recommendation. They cannot create API
access, substitute model confidence for evidence, bypass direct/delegated
authorization,
or turn unimplemented CRM/native-signal capabilities or post-v1 discovery and
crawling into verified support.
Machine recommendations and model interpretations remain separately labeled.

The package, compatibility discipline, and validation requirements are in
[`agent-skills.md`](agent-skills.md). Authored skill files do not prove that the
CLI or a live optimization workflow has been implemented.

## Durable history and creative intelligence

The canonical local history is schema-versioned, append-only JSONL. It records
normalized account and resource snapshots, every mutation lifecycle event, GA4/
GTM changes, restated performance observations, immutable creative revisions,
and effective-dated ad-to-creative deployments. Every reconciliation detects
out-of-band provider changes without inventing an external actor or exact time.

SQLite is the default rebuildable local operational index; optional DuckDB
supports analytical queries over verified local projections. JSONL remains
canonical. Versioned configuration lets users choose retention by stream,
media, exports, and cache; preserve history by default and apply automatic
cleanup only under an explicit saved policy with recovery dependencies protected.
CSV and JSON reports are deterministic projections from a verified JSONL high-
watermark, never independently maintained stores. The CSV bundle includes field-
level changes, resource snapshots, performance/conversions, creative catalog,
components, palette, deployments, and creative performance. See
[`local-ledger-and-exports.md`](local-ledger-and-exports.md).

Every exact creative receives a stable family, content-addressed version, asset,
and rendition identity. Local analysis captures exact platform and on-visual
text, CTA type/label/location/timing, dominant/background/accent colors and
proportions, visual structure, format, aspect/duration, opening/motion/audio,
message/hook/offer, and extraction provenance/confidence. Missing and
inapplicable fields have explicit statuses.

Performance joins through an effective-dated deployment, so a provider's in-
place edit cannot assign old results to new copy or visuals. Responsive/dynamic
component claims are allowed only at granularity the provider actually reports.
Core extraction is local; optional BYO vision/model enrichment follows the same
explicit egress boundary. See
[`creative-intelligence.md`](creative-intelligence.md).

## Measurement control

### GA4

GA4 is the required aggregate analytics and conversion-measurement source:

- The Data API profile is read-only and provides campaign-level aggregate
  reporting, joins, data-quality metadata, and event-health diagnostics.
- A separate Admin API edit profile can discover, create, update, and
  decommission supported event/key-event configuration through the same guarded
  plan/approve/apply/verify flow.
- A new signal is verified first in GA4 Realtime and later in processed standard
  reporting. Thresholded, sampled, stale, missing, duplicate, and pending states
  never masquerade as zero.
- Key-event configuration is prospective and does not backfill history. A GA4
  key event is not automatically a Google Ads conversion and does not supply a
  native conversion signal to the other five advertising platforms.

### GTM Web

GTM is optional for an individual customer but first-class in the product. A
customer without GTM gets `NOT_CONFIGURED_OPTIONAL`, and unrelated advertising
workflows continue. The shipped GTM adapter must nevertheless be complete and
live-validated before v1 claims GTM support.

V1 GTM support includes:

- account, container feature, destination, live/latest version, every accessible
  workspace, environment, tag, trigger, variable, Google tag configuration,
  template, folder, sequencing, exception, zone, applicable client/
  transformation, and consent inventory;
- dependency mapping and safe classifications, including
  `PROVEN_UNREFERENCED`, dynamic/unknown references, and broken references;
- audits for duplicates, double firing, stale/paused vendor tags, orphaned or
  broken resources, broad or fragile triggers, consent ordering, unexpected
  outbound domains, dirty workspaces, drift, and naming/ownership gaps;
- local inspection and classification of existing custom HTML, custom
  templates, and other custom-code resources without exposing raw source in
  logs, evidence, support bundles, telemetry, or remote analysis;
- evidence-backed cleanup and optimization plans that can consolidate
  duplicates, tighten triggers, normalize naming, migrate supported tags to
  reviewed templates, and remove only resources proven unreferenced;
- controlled creation and maintenance of Google tags, allowlisted GA4 event
  tags, variables, and custom-event/page/click/form-submission triggers using
  declared selectors or element state, never DOM text or entered values;
- a new dedicated workspace only after live/latest and workspace-conflict checks,
  static/dependency validation, GTM `quick_preview`, approved real-browser
  preview, immutable version creation with tested-state digest equality, separate
  publish approval, live-state drift rejection, browser and GA4 verification,
  and restoration to the previously live version when required; and
- an explicit `NEEDS_SITE_INSTRUMENTATION` outcome plus a machine-readable event
  contract when no safe site or data-layer signal exists.

The CLI can inspect an existing CMP and verify technical behavior against a
customer-declared consent contract. It does not choose legal policy, override a
denied state, or install a CMP automatically.

Customer-selected native conversion setup and reviewed provider-native tags
are now v1 scope, with separate capability, privacy, and live verification
requirements in [`conversion-integrations.md`](conversion-integrations.md).
Server-side GTM, Measurement Protocol, enhanced conversions, automatic CMP
installation, and arbitrary user custom-code generation remain post-launch work.
They are not permanent
architectural bans, but they require separate threat models, authorization,
data contracts, tests, and release claims. Enhanced conversions/user data and
arbitrary custom code remain prohibited in the v1 regulated profile.

See [`measurement-control.md`](measurement-control.md) for the complete safety,
privacy, consent, staging, publishing, and verification contract.

## Attribution and native conversion scope

- GA4 remains required, with distinct reporting and administration profiles.
- HubSpot and Salesforce are customer-optional executable aggregate attribution
  connectors in v1 scope, replacing documentation-only integration. Return
  campaign-level qualified outcomes, pipeline, and revenue only where the source
  can safely aggregate them; preserve counting, model, window, privacy thresholds,
  and mapping provenance. No raw CRM or person-level record retrieval.
- Provider-native conversion resources, campaign-goal associations, and reviewed
  native tag/event setup are scoped per destination. A GA4 key event does not
  automatically configure any native signal. Test each layer and expose missing,
  unsupported, privacy-incompatible, or processing-pending results.

These are new implementation targets, not implemented or verified connectors.
Customers enable only the sources/destinations they use; missing optional CRM
access never blocks core ad/GA4 work. Current API research and destination-specific
permissions determine the supported subset. The data boundary still excludes
person-level uploads, enhanced-conversion identifiers, arbitrary JavaScript,
website crawling, and automatic CRM metadata discovery.

See [`conversion-integrations.md`](conversion-integrations.md),
[`attribution-sources.md`](attribution-sources.md), and
[`measurement-control.md`](measurement-control.md).

## Post-v1 advertising backlog

Apple Search Ads / App Store advertising is backlogged. It is excluded from
v1's required connectors, common-format examples, capability evidence, auth
handoff, integrated acceptance, soak, and release smoke tests. Its absence must
not block installation, default commands, packaging, or release.

Promotion requires a separate product-owner scope decision, current provider
research, a supported format/object contract, and its own implementation and
live evidence. The backlog sets no delivery date and makes no current support
claim.

## Post-v1 conversion architecture direction

A capability-gated future assistant may inspect explicitly selected HubSpot or
Salesforce configuration metadata and a bounded set of approved public
marketing pages, forms, and CTAs. It then asks the marketer to define the real
business outcome, source of truth, qualifying transition, counting/deduplication,
value, consent, and destinations before compiling any configuration plan.

This is deliberately outside v1. A discovered button, form, page, or CRM stage
is only a conversion candidate; the product never invents that business meaning.
The future crawler is not a generic web crawler: it is scope-bound, non-
interactive, local, and prohibited from logging in, submitting forms, retaining
raw pages, or following undeclared egress. CRM discovery is metadata/config-only
and never reads contact, lead, account, deal/opportunity, member, submission, or
other person/business records.

Because page/form/CTA discovery necessarily parses public HTML and visible text,
it uses a separately approved future discovery-input class or a manifest-only
mode. That exception does not relax the v1 regulated measurement rule against
collecting DOM/page text or entered values. See
[`future-conversion-architecture.md`](future-conversion-architecture.md).

## Privacy and regulated-data boundary

The v1 regulated profile allows campaign configuration, aggregate performance,
event occurrence, explicitly declared low-cardinality event parameters,
selectors/element state without content, and controlled campaign parameters
such as UTMs on declared public marketing hosts and paths.

It rejects as product/measurement data before display, persistence, logging,
evidence, or transmission:

- customer, patient, member, account-holder, contact, or matched-audience lists;
- names, emails, phone numbers, authentication subject identifiers,
  CRM/customer/account-holder IDs, raw click IDs, and person-level records;
- health conditions, care status, financial hardship, credit, eligibility,
  underwriting, protected-class, or similar traits;
- entered form values, DOM/page text, and sensitive values in URLs, query
  strings, fragments, event names/parameters, or `dataLayer` payloads;
- person-level offline conversion events, `user_id`, or enhanced-conversion
  `user_data`; and
- undeclared external processing or browser-test egress.

The control plane necessarily sends schema-approved HTTP request bodies and
provider account/resource IDs for authorized operations. An approved consent-
granted browser test may also use provider-managed cookies. Those mechanics are
restricted to the exact approved operation and destination. Raw cookies,
authorization headers, and raw request/response bodies never enter display,
persistence, logs, evidence, or support bundles. Operational IDs remain local
when required for identity and safety and become keyed opaque references in
exports.

Real-browser validation can itself send data to providers. The exact hosts,
paths, event contract, consent contract, and endpoint allowlist are part of the
previewed plan and approval. The product does not claim that local execution
makes GA4 safe for PHI or makes a workflow HIPAA or GLBA compliant.

## Runtime and authorization

- Signed standalone binaries for supported macOS, Windows, and Linux versions,
  plus a pinned OCI image.
- No mandatory vendor cloud, telemetry, crash upload, automatic updater, or
  license heartbeat.
- Optional signed update-manifest check with disclosed network metadata and an
  opt-out.
- Named local profiles with separate read, edit, activation, and publish
  authorization where providers permit it.
- OS Keychain, Windows Credential Manager, or Secret Service for interactive
  credentials; environment credentials only for explicit CI.
- Customer-owned developer applications and credentials where provider policy
  permits them.
- A mandatory append-only local change ledger for every write. Explicit report/
  sync and creative-analysis commands append validated normalized observations
  by default; an explicit ephemeral read can opt out. Raw provider responses are
  never persisted by default.
- Customer-managed external CLIs and MCP servers are optional trust boundaries,
  never automatically installed, authenticated, or invoked dependencies.

## Provider access and implementation sequence

Build all six advertising providers for v1; configure access customer by
customer. The project registers no provider application of its own; support
customer-owned apps, API keys, and OAuth clients where the provider permits that
distribution/authentication pattern. OAuth user consent does not replace
developer-app review, account eligibility, or production access requirements
imposed by a provider.

Phase 0 records current official access and operation contracts. Implement and
validate each adapter independently with existing test partners as suitable
accounts become available. A bounded early feasibility test may use explicitly
authorized partner/disposable accounts once that test's safety checks pass;
complete unrelated adapters and packaging are not prerequisites. Mutations
still require direct or delegated authorization with exact bounds. Never request
secrets in chat or assume permissions transfer between customers.

An individual customer's missing access is an onboarding condition. It does
not block unrelated configured channels, and it does not waive the product's
all-six supported-capability evidence requirements. Report the exact missing
provider/app/account permission and next action; never silently omit a provider
explicitly selected in a composite mutation plan.

## Distribution and licensing

AdHelm is free software under the Apache-2.0 license
([ADR 0013](adr/0013-open-source-release.md)). There is no paid tier, edition,
license key, hosted service, vendor-registered provider application, or vendor
token relay. Every build ships one implementation. What a build has released is
discoverable through `adhelm discover`; it never depends on who obtained the
build.

### Release path

- `0.1.0` is the first public release and is read-only. It is gated by the
  [0.1.0 release definition](release-0.1.0.md), not by this brief's `v1.0.0`
  criteria. It ships as a PyPI wheel and sdist named `adhelm` and as a GitHub
  release, installable with `uv tool install adhelm` or `pipx install adhelm`.
  Write code ships and is refused at runtime by one release-policy gate.
- `v1.0.0` is the full contract in this brief and in
  [`definition-of-done.md`](definition-of-done.md), including released writes,
  signed standalone binaries and a pinned OCI image.
- When writes are released, a user who wants them builds and signs the native
  approval host with their own platform signing identity. No unsigned path
  weakens the approval threat model.

### Bring your own provider application

Users register their own developer applications and API keys and keep their
own credentials. The project owns and operates no provider application and
holds no user credential. Each provider has a setup guide at
`docs/setup/<provider>.md` that names the application or key to create, the
required scopes and redirect configuration, access-tier limits that matter for
read-only use, the enrollment command, and how to revoke.

Provider quotas, approvals, access tiers, breaking changes and certification
requirements belong to the provider. OAuth user consent does not replace
developer-app review, account eligibility, or production access requirements.
Report the exact missing provider, app or account permission and the next action.

### Read-only operation

Read-only describes remote effects, not an absence of local history. Audits,
reports and inventories cannot create or change advertising or measurement
resources, stage a GTM workspace, create preview or version resources, activate,
publish, or initiate browser measurement traffic. They do not crawl websites or
add CRM access. Missing runtime proof remains explicit. Use least-privilege read
access where providers permit it; when scopes are broader, disclose that fact
and keep the local read-only boundary. Never request edit access merely to
complete an audit.

Running another audit is user-directed or separately scheduled with permission;
artifact rules do not create monitoring. Missing evidence or no warranted change
is a valid result. Useful recommendations are never withheld or inflated, and
local history and safe exports are always available.

### No meter, no heartbeat

No runtime license service, heartbeat, device fingerprint, trial expiry, usage
quota, renewal barrier, or meter on commands, executions, accounts, spend, rows,
or provider API calls exists. A build stays runnable indefinitely; upstream API
changes can still make an older connector incompatible, and that is reported
honestly. Reading and exporting history, reconciling interrupted work and guarded
recovery are available in every build. An upgrade must not silently migrate
shared history or configuration so that a retained build cannot read or recover
it; disclose version compatibility and preserve a verified migration/export path.
Provider permissions and exact direct or delegated authorization remain required
for every operation.

### Contributions and maintenance

Contributions use the Developer Certificate of Origin, not a CLA; see
[`CONTRIBUTING.md`](../CONTRIBUTING.md). The AdHelm name and marks are not
licensed by Apache-2.0, and forks that redistribute must not imply endorsement.
The maintainers and community support the project in the open through public
issues, and vulnerabilities are reported privately as
[`SECURITY.md`](../SECURITY.md) describes. No paid support, onboarding or
consulting is promised. Documented provenance and licenses, reproducible
releases, and operating instructions keep the project understandable to any new
maintainer.

## Public release gate

Do not ship `v1.0.0` until every criterion in
[`definition-of-done.md`](definition-of-done.md) is evidenced, including:

1. complete live lifecycle parity across all six advertising providers;
2. live GA4 Data and Admin workflows;
3. live GTM inventory, cleanup, controlled creation, preview, publish,
   verification, and restore workflows;
4. clean installation and operation without optional GTM, HubSpot, Salesforce,
   or external CLI/MCP configuration;
5. privacy, redaction, egress, secret-storage, drift, ambiguity, and restoration
   failure tests;
6. the minimum CRM aggregate and per-provider native conversion workflows in
   `conversion-integrations.md`, independently of individual customer setup;
7. independent external-account validation, UI reconciliation, and the required
   30-day reliability soak; and
8. reproducible signed artifacts, checksums, SBOM, provenance, security review,
   evidence manifest, and a signed `v1.0.0` tag on the exact passing commit.

The post-v1 discovery/crawling design remains outside these release gates.

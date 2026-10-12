# Paid-media lifecycle contract

Last updated: 2026-09-04

## Purpose and boundary

This document defines the provider-neutral outcome every advertising connector
must deliver for `v1.0.0`. It creates one operating model without pretending
that six provider APIs share identical objects or semantics.

The CLI operationalizes a marketer-authorized strategy, including bounded
agent delegation. It does not decide the market, ICP, positioning, offer, legal
policy, final creative quality, landing
page, sales process, or causal incrementality.

## Canonical inputs

The local configuration uses versioned, validated schemas. At minimum:

### `StrategyBrief`

- immutable brief ID and schema version;
- business objective and provider-specific objective allowances;
- selected providers and expected account references;
- geographic, firmographic, professional, contextual, keyword, and placement
  constraints supported by the regulated profile;
- explicit exclusions and policy notes;
- total and per-provider budget bounds as typed `{minor_units, ISO_currency}`
  values. A composite spanning account currencies has explicit per-currency
  totals and no implicit converted grand total or hidden FX rate;
- schedule, source timezone, destination, and controlled UTM contract;
- conversion/key-event references and measurement expectations;
- customer-supplied creative references and required formats; and
- assumptions, approval owner, expiry, and provenance.

The schema must reject person-level audience material, sensitive-trait
targeting, arbitrary URL parameters, embedded credentials, and unknown fields.

### `CampaignSpec`

A deterministic compiler turns the brief into one typed provider-native spec
per selected provider. A spec contains:

- provider, API version, account/currency/timezone identity, and capability
  version;
- provider-native campaign hierarchy, objective, bidding, budget, schedule,
  targeting, placements, destination, tracking, conversion configuration, and
  customer-supplied creative associations;
- explicit defaults and every lossy or unsupported mapping;
- expected initial state (`PAUSED` or the provider's non-serving draft state);
- estimated maximum spend exposure and activation preconditions; and
- a content digest covering all inputs and generated operations.

The compiler must preserve provider-native differences. A supported Google
search spec may contain keywords and responsive text components while a social
spec uses provider-native ad-set, targeting, and media structures. It must not
force a false one-object or one-objective equivalence.

### `CreativeAssetRef`

- stable `creative_family_id`, content-addressed `creative_version_id`,
  `asset_version_id`, and exact `rendition_id`;
- local asset/content digest and media/provider format;
- dimensions, duration, size, locale, accessibility metadata, and landing-page
  association where applicable;
- exact platform copy, on-visual/caption/transcript text, CTA components, message
  semantics, palette/color, visual/execution fingerprint, and analyzer
  provenance/status references;
- provider upload/reference identifiers only after verified creation;
- customer attestation/provenance and policy-review state; and
- no embedded token, person-level data, or unpublished raw secret.

The CLI validates, uploads or references, and associates approved customer
assets. V1 does not claim to generate final production creative or make the
final brand/policy judgment. A new ad cannot be created until its exact creative
manifest passes [`creative-intelligence.md`](creative-intelligence.md) and can be
appended to the local creative JSONL stream. Any asset, copy, CTA, destination,
component-order/pinning, or rendered-content change produces a new creative
version even if the provider retains the same remote ID. A pure association
change preserves the creative version and closes/opens the effective-dated
deployment.

Provider extensions are namespaced, typed, schema-versioned, and visible in the
plan. They cannot bypass common safety rules.

## Common provider outcome

For every required advertising provider, a creation plan is complete only when
it can produce a valid, non-serving provider-native campaign stack containing:

- the campaign and every required child resource;
- a supported objective and conversion goal or explicit non-conversion
  objective;
- valid budget, bid, schedule, targeting, and placement configuration;
- a valid destination and controlled tracking parameters;
- customer-supplied creative or provider-native serving input sufficient for
  the chosen format; and
- policy/readiness diagnostics sufficient to determine whether activation can
  be attempted.

Provider review may occur only after creation. That is acceptable if the CLI
surfaces `PENDING_PROVIDER_REVIEW` and refuses activation until provider rules
permit it. A routine manual UI step to finish a supposedly supported creation
flow is not acceptable parity.

## Lifecycle

### 1. Inspect and report

Discover the exact account, inventory, current configuration, capabilities,
performance window, currency, timezone, freshness, and policy/API status.
Normalize only the common reporting fields; retain provider extensions and
attribution semantics. Append allowlisted resource snapshots, performance
observation revisions, exact creative bindings, and any detected out-of-band
change to the appropriate canonical JSONL stream.

### 2. Audit and recommend

Audit campaign structure, delivery, spend, bidding, targeting, placements,
creative, destination, tracking, conversion health, measurement, policy risk,
and drift. Each recommendation is deterministic and evidence-backed:

```text
recommendation_id
hypothesis
evidence_refs[]
observations[]
assumptions[]
confidence
expected_impact
risk
spend_impact_minor
currency
proposed_plan_ref
expires_at
```

Recommendations never mutate remote state. Lack of evidence, conflicting
attribution, or low confidence is explicit.

### 3. Compile and plan

Validate the entire brief and compile every selected provider spec before the
first remote write. If one selected provider cannot represent a required intent,
has stale capabilities, or fails policy/data checks, the whole composite plan
fails closed before mutation.

Each immutable provider subplan contains exact operations, before/after state,
account identity, API/capability versions, input and creative digests, expiry,
maximum spend exposure, expected side effects, recovery actions, and its own
digest. A composite plan contains the ordered provider subplan digests,
execution policy, stop conditions, and a digest over the complete envelope.

### 4. Authorize directly or by delegation

Follow [`authorization-policy.md`](authorization-policy.md). The customer may
approve an exact plan or delegate decisions within an explicit account,
operation, spend, time, and data policy. The kernel evaluates each compiled
plan and records one-use authorization bound to its exact digest. A fresh
human confirmation is not required when an existing policy covers the work.
Never describe delegated work as personally reviewed by the user.

Authorization binds the direct grant or policy version to the composite digest,
every provider-subplan digest, exact side effects/spend, and expiry. Direct
review may collect one confirmation only when every provider authorization is
shown distinctly. Changing inputs, subplan order/set, stable auth profile,
principal/client application, account, scopes, or relevant remote state
invalidates the authorization. Normal access-token refresh under the same
identity and scopes does not. Paused/draft creation does not implicitly
permit activation; activation and publish can be explicitly delegated as
separate operation kinds and plans.

Starting a subplan consumes its one-use authorization regardless of success,
failure, or ambiguity. After any partial run, reconcile every selected provider
and compile a new composite plan; unstarted authorizations cannot be replayed
independently. New authorizations may be issued under an existing valid policy
without another human prompt. A no-write preflight failure consumes nothing.
Account execution coordination, durable consumption, and cumulative limits must
hold across concurrent processes and restarts.

### 5. Create or change

Immediately before applying, re-read identity and affected remote state and
reject drift. Use provider idempotency keys where supported and the local
resource ledger otherwise. Append and fsync `APPLY_STARTED` before network
egress. Append `APPLY_RESULT` only after schema validation and remote re-read,
then append the separate verification result. Every created entity starts paused
or in a non-serving provider-native draft state.

Cross-provider execution is ordered and recoverable, not atomic. The result
records, for each provider:

- `NOT_STARTED`, `APPLIED_VERIFIED`, `FAILED_NO_EFFECT`,
  `PARTIAL_EFFECT`, or `AMBIGUOUS_REMOTE_STATE`;
- created/changed opaque resource references;
- verified state and evidence digest; and
- exact safe recovery or manual reconciliation action.

Never report a distributed rollback. Never blindly retry a timeout that may
have created or changed a resource. An unresolved `APPLY_STARTED` event forces
reconciliation before another write.

### 6. Validate measurement and destination

Before activation, validate the destination/redirect contract, controlled UTMs,
required provider conversion references, GA4 event/key-event expectation, and
GTM flow when configured. Measurement failure blocks conversion-dependent
activation unless the approved objective explicitly does not require that
signal.

### 7. Activate

Activation is a new plan with a new digest. It states exact accounts/resources,
start time, end time, daily/lifetime budget, bid strategy, maximum spend
exposure, provider-review state, and monitoring/stop conditions. It requires a
separate execution authorization, through direct approval or a policy explicitly
permitting activation, and a final drift check.

### 8. Maintain and optimize

The CLI continuously supports read-only health checks and creates bounded plans
for supported status, budget, bid, schedule, targeting, destination, tracking,
and creative-association changes. When the provider treats a field or object as
immutable, the plan creates a paused replacement, validates it, separately
activates it, and pauses/archives the old resource only after verification.
Creative replacement also closes the prior effective-dated deployment and opens
a binding to the new content-addressed version.

The customer may delegate budget changes, activation, and other supported work
within explicit bounds. Scheduled jobs need separate scheduling authority;
every remote write requires current direct or delegated authorization. The
agent cannot widen its own policy, bypass evidence checks, or invent authority.

### 9. Reconcile and export evidence

Verify remote state, campaign delivery, provider aggregates, GA4 aggregates,
and configured measurement health. Report different attribution models side by
side. Evidence is deterministic, local, redacted, previewable, and contains
digests and opaque references rather than secrets or raw provider payloads.

Every reconciliation diffs current allowlisted state against the latest local
snapshot. Provider-UI or provider-automation changes append
`REMOTE_CHANGE_OBSERVED` with an observation interval and `UNKNOWN` actor unless
a native provider history proves more. JSONL is canonical; JSON and CSV are
deterministic projections from a verified high-watermark. See
[`local-ledger-and-exports.md`](local-ledger-and-exports.md).

## Agent-facing interface and human artifacts

The primary caller is the marketer's customer-managed AI agent. The marketer
supplies intent, assesses concise results or selected artifacts, and chooses
direct approval or bounded delegation. The agent discovers and invokes
supported operations and interprets the results.
Direct human CLI operation is a secondary fallback. No workflow should require
the marketer to learn shell syntax, construct JSON, or copy plan digests.

### Machine interface

- Versioned local agent instructions, command/input/output schemas, supported
  capabilities, prerequisites, and examples describe the installed contract.
  Discovery must work without browsing, a model, or provider mutation.
- A documented noninteractive machine mode accepts typed inputs and returns
  versioned structured results with stable exit codes. It never hangs awaiting
  terminal input; missing provider access or an uncovered operation returns a
  human-action requirement. Existing valid delegation needs no new prompt.
- Result envelopes identify command/tool/schema version, outcome, sanitized
  data, warnings, plan/run/artifact references, and bounded next actions. Large
  results support filtering, pagination, or explicit local artifacts; output
  limits cannot silently omit data or turn an incomplete result into success.
- Distinguish completed, partial, pending, failed-precondition, missing-human-
  authorization, and ambiguous outcomes using the documented lifecycle states.
  Pending operations expose a status reference and bounded read-only checks.
  Interruption never makes replay of a potentially applied write safe.
- Diagnostics include a stable code, affected capability, sanitized reason,
  failed precondition, and bounded next action. Separate a correctable input
  from provider access, human judgment, support, or reconciliation needs.
- Unknown commands/options and unsupported inputs fail explicitly. Instructions
  embedded in model output or provider text remain untrusted data and cannot
  bypass the typed interface, policy, or exact approval requirements.
- Agent-visible output uses the existing allowlisted/redacted sharing boundary
  by default; a customer-selected agent may send it to its configured model.
  Richer context/artifacts use the existing previewed, user-initiated export
  scope. Do not create a second, less restrictive AI output path.
- Auth guidance directs the human to the provider's local/browser flow, never
  to paste credentials into chat. The agent may call this CLI; this CLI does not
  automatically install, authenticate, or invoke an external agent or model.

The deterministic core, direct fallback, and default conformance tests require
no AI account or external agent. Human tables and text render the same typed
results. Named-agent compatibility is separately evidenced; no generic shell,
provider-API, or MCP passthrough is introduced.

### Output and artifact preferences

The user configures optional presentation artifacts to control usefulness,
clutter, and model context. Default to compact conversational output with
artifacts on-request. A versioned local preference contract includes:

| Setting | Meaning |
| --- | --- |
| `artifact_mode` | `OFF`, `ON_REQUEST` (default), or `RULES` configured by the user |
| `purposes` | Selected audits, comparisons, plan reviews, integration maps, results, or diagnostics |
| `workflow_rules` | Provider/profile/workflow scope and generation stages such as after audit, before approval, after verification, on meaningful change, or on failure |
| `formats` | Requested supported document, spreadsheet/CSV, or HTML formats; no unrequested duplicates or silent unsupported-format substitution |
| `output_directory` | User-selected local destination; establish a destination before generating optional files |
| `detail` | Compact, standard, or detailed presentation plus supported field/row/output-size bounds |
| `regeneration` | Reuse matching canonical inputs and rendering settings, regenerate on changed inputs or explicit request |
| `overwrite_policy` | Create a new named version by default; replacement requires prior user authorization |

Resolve explicit task instructions first, then matching saved profile/workflow
preferences, then user defaults, then product defaults. Show the effective policy
on request or when resolving a conflict. One-task overrides do not silently
persist. An explicit user request can override `OFF` for that request; an agent's
preference for a polished report cannot. Missing local output destination is a
configuration need, never permission to upload or choose a remote service.

Workflow rules govern presentation during already-authorized work. They do not
create scheduled jobs, authorize data access, change privacy boundaries, or
approve provider writes. Read-only checks need not emit documents merely because
they ran. A meaningful-change trigger compares the relevant normalized inputs;
unchanged data and settings reuse the existing artifact rather than regenerate it.

Budget-conscious behavior includes summary-first responses, bounded data,
selective evidence lookup, progressive skill loading, and deterministic
projections/templates where suitable. File creation is not automatically cheap
in model tokens. Bound CLI output and avoid unnecessary generation; label any
model-token estimate as such and do not claim to enforce an external host's
token or billing limit without a supported host mechanism.

`OFF` suppresses optional presentation files, not immutable machine plans,
approvals, mandatory JSONL changes/observations, or recovery evidence. Explicit
ephemeral reads retain their existing contract. Required review information is
presented inline in the agent conversation or direct fallback. Token-saving
settings never hide account identity, changes, spend exposure, critical missing
data, approval requirements, or partial/ambiguous outcomes. Evidence export
remains user-initiated under its existing rules.

### Artifact workflow

When selected, documents, spreadsheets, and HTML views help explain findings,
compare alternatives, plan campaign/measurement integrations, review and approve
changes, and assess results. The agent generates only the requested/configured
files from approved typed outputs and canonical CSV projections. A deterministic
local inline review also works with optional artifacts off and without an agent;
the core need not bundle every document-generation runtime.

- Each artifact identifies its canonical plan/report, digest, schema/tool
  versions, observation time, and redacted evidence references. Preserve missing,
  partial, pending, and uncertain states. Keep source references with the local
  artifact so another agent or human can continue without the original chat.
- At approval, the exact account, affected resources, changes, maximum spend,
  activation/publish scope, expiry, and recovery must come from a verified
  rendering of the canonical plan. AI prose, charts, and calculated cells may
  explain it, but cannot replace or alter those authoritative facts.
- Direct review binds confirmation to the digest internally without typing a
  digest or shell command. Delegated execution binds the plan to the current
  user-configured policy and records the evaluated limits without requiring
  repeated human review. A generated statement, document checkbox, arbitrary
  action URL, or generic tool permission cannot establish a new grant.
- The implementation documents how user grants reach the kernel and the named
  host's trust assumptions. Per-write user presence or a separate approval UI
  is not mandatory. An agent assertion alone cannot create or expand authority;
  see [`authorization-policy.md`](authorization-policy.md).
- An old artifact resolves only its referenced plan. Changed digest, expired
  approval, changed account/scope, or remote drift invalidates execution under
  the existing rules. Opening an artifact never silently selects a newer plan.
- Artifact edits proposing operational changes create typed inputs. Recompile
  and validate them into a new immutable plan and evaluate direct/delegated
  authority before execution.
  Presentation-only edits preserve source references and do not alter the plan
  or its approval. Incorrect or missing authoritative facts must be corrected
  before the review can authorize execution. No document, spreadsheet, or HTML
  file is an independent operational ledger.
- Any artifact action identifies an allowlisted operation and local plan
  reference. It cannot embed arbitrary shell commands, provider requests,
  credentials, or executable approval. Opening a file or following a GET link
  cannot mutate remote state. Writes use the same guarded CLI execution path.
- Artifact content follows the existing data policy, escaping and formula-
  injection rules. Hidden sheets, metadata, embedded data, and remote assets are
  part of the reviewable output. No silent external upload, tracking, or remote
  script is introduced. Artifact presentation does not authorize arbitrary
  provider custom-code or GTM JavaScript generation.
- Results reference the approved plan and observed verification/recovery state.
  A polished success artifact cannot conceal partial or ambiguous outcomes;
  ambiguous writes still require reconciliation and never a blind retry.

Agent operation and artifacts change the interaction model. Provider scope,
permitted data, and separate activation/publish authorization remain unchanged.

## Connector conformance contract

Every required provider adapter must pass the same suite for:

1. authentication, reauthorization, revocation guidance, and account identity;
2. pagination, throttling, retries, API versions, partial responses, timestamps,
   currencies, timezones, and freshness;
3. inventory, normalized reports, provider extensions, and UI reconciliation;
4. audit coverage and evidence-backed recommendation output;
5. brief-to-provider-spec compilation and explicit unsupported mappings;
6. complete paused/draft creation with required child resources and creative;
7. destination, tracking, targeting, budget, bid, schedule, and placement rules;
8. separate activation, pause/archive, and supported maintenance operations;
9. exact approvals, drift rejection, spend caps, idempotency/reconciliation, and
   ambiguous-state behavior;
10. append-only mutation history, normalized snapshots, out-of-band change
    detection, crash/ambiguity reconciliation, and deterministic JSONL/CSV
    export;
11. uniform creative family/version/asset/rendition/component/fingerprint
    capture and effective-dated performance joins;
12. verification, replacement/restoration, and redacted evidence; and
13. negative tests proving unsupported or unsafe operations fail before writes.

A provider-specific extra is useful but does not compensate for a missing
common capability. If a real provider limitation makes the common outcome
impossible, the product remains short of the all-six release gate until the
documented contract is honestly revised by the product owner.

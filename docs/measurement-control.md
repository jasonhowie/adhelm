# Measurement control: GA4, GTM, and native conversions

Last updated: 2026-09-04

## Product contract

GA4 is the required aggregate analytics and conversion-measurement source. GTM
Web is an optional customer configuration and a required product capability.

| Surface | Customer requirement | `v1.0.0` implementation |
| --- | --- | --- |
| GA4 aggregate reporting | Required | Required and live-verified |
| GA4 event/key-event administration | Opt-in write authorization | Required and live-verified |
| GTM Web | Optional per customer | Required adapter and live workflow |
| HubSpot/Salesforce | Optional per customer | Executable source-side aggregate attribution connectors in scope; evidence pending |
| Server-side GTM | Optional advanced | Post-launch, capability-gated |
| Measurement Protocol | Optional advanced | Post-launch, capability-gated |
| Enhanced conversions/user data | Prohibited in regulated v1 | Post-launch explicit opt-in only outside regulated mode |
| Native conversion resources and tag/pixel configuration | Customer selects destinations | Typed provider-specific operations in scope; per-operation evidence required |
| CMP installation | Not a default capability | Existing-CMP audit only in v1 |
| Arbitrary custom code generation | Prohibited in v1 | Separately gated future capability |

A customer that does not use GTM receives `NOT_CONFIGURED_OPTIONAL`; its ad and
GA4 workflows remain usable. Because the product advertises GTM support, the GTM
adapter itself is not optional release work.

All six advertising providers remain in product v1 scope; each customer uses
only its selected subset. A missing or ineligible optional provider profile
must not block unrelated workflows. Customer-owned applications, keys, and
OAuth are supported where the provider permits them; OAuth consent alone is not
proof of developer-app approval or operation access.

All exact write authorizations may come directly from the user or from their
explicit bounded delegation policy. No per-write human confirmation is required
inside that policy. Stage, publish, native conversion setup, spend-affecting
association, and recovery remain separate action kinds, each requiring covered
authority. The model cannot widen its own policy or treat its recommendation as
authorization. See [`authorization-policy.md`](authorization-policy.md).

## GA4 contract

### Reporting profile

Use the read-only Google Analytics Data API for allowlisted aggregate dimensions
and metrics. V1 does not use audience exports, user-level explorations, client
IDs, user IDs, BigQuery event exports, or raw events.

The adapter must:

- discover and bind the expected property and web-stream identity;
- report campaign/source/medium/date aggregates using the normalized schema;
- preserve property timezone, currency, attribution-model context, date window,
  freshness, quota/pagination, and data-quality metadata;
- surface sampled, thresholded, stale, missing, duplicate, and processing states
  explicitly rather than replacing them with zero; and
- reconcile an identical request to the GA4 UI within documented tolerances.

### Administration profile

Use a distinct, opt-in GA4 Admin API authorization for supported configuration
writes. Plans may discover, create, update, or decommission:

- key events, including counting method and supported default value/currency;
- supported event-creation rules; and
- supported event-edit rules.

The exact property, stream when applicable, before/after representation, scope,
and resource fingerprint are part of the plan digest. Re-read immediately before
apply and reject drift. A key-event name is treated as immutable; rename means a
new resource plus an explicit decommission plan. Configuration is prospective
and does not backfill historical events.

A GA4 key event is not automatically a Google Ads conversion. Google Ads
link/import is a separate guarded provider operation. GA4 configuration also
does not provide native conversion signals to Meta, LinkedIn, TikTok, Reddit,
or OpenAI Ads.

### Verification

Verify a new or changed event contract in this order:

1. approved real-browser action and network trace;
2. GA4 Realtime observation;
3. processed standard Data API report after the normal processing interval; and
4. comparison to the intended counting behavior.

Use distinct diagnostics:

- `MISSING_EVENT`
- `STALE_EVENT`
- `DUPLICATE_FIRING`
- `THRESHOLDED`
- `PROCESSING_PENDING`
- `PROCESSING_TIMEOUT`

Realtime success is not processed-report success.

## GTM Web contract

### Inventory

Inventory the selected account, Web container and feature flags, destinations,
live and latest container-version IDs, every accessible workspace and its
pending changes, environments, tags, triggers, built-in and custom variables,
Google tag configurations, templates, folders, firing/exception references,
sequencing, zones, consent settings, and any clients or transformations the
container reports as applicable. Preserve provider IDs and fingerprints locally;
redact account identity in exported evidence.

Existing custom HTML, custom templates, and any other custom-code surface are
inspected only on the local machine. The CLI may record a digest,
classifications, detected capability categories, and outbound domains. It never
places raw source in logs, telemetry, evidence, support bundles, or remote
analysis.

### Dependency graph

Build a graph covering supported direct and indirect references:

```text
destinations and Google tag configs -> tags -> triggers/exceptions -> variables
zones/child containers ----------------^      -> built-ins and templates
tags -> setup/teardown sequencing -> consent requirements
applicable clients/transformations/custom code -> referenced resources/domains
```

Bind nodes to account/container/workspace/version, provider ID, and fingerprint.
Classify every discovered edge as:

- `PROVEN`: a supported reference was resolved;
- `DYNAMIC_OR_UNKNOWN`: static analysis cannot prove the target; or
- `BROKEN`: an explicit reference cannot be resolved.

A resource is `PROVEN_UNREFERENCED` only when every applicable resource surface,
supported reference form, and every accessible pending workspace has been
inventoried across live and latest versions and no edge reaches it. If a
resource type/workspace cannot be read or a custom/dynamic reference cannot be
proven, classify the relevant graph `DYNAMIC_OR_UNKNOWN` and prohibit deletion
and publication of a cleanup depending on that proof. "Old," "paused," "not
recently observed," or "not in the static graph" alone never authorizes
deletion. Dynamic, custom-template, or unresolved resources remain
recommendation-only.

### Audit

The audit detects and explains:

- duplicate Google tags and GA4 event tags;
- duplicate or conflicting firing and exception behavior;
- orphaned resources and broken references;
- paused/stale vendor tags and unexplained outbound domains;
- overbroad All Pages, DOM, click, and form-submission triggers;
- fragile DOM dependencies that should become declared `dataLayer` events;
- sequencing, initialization, and consent-order errors;
- secret-like strings, sensitive collection risk, unsafe browser APIs, unknown
  templates, and custom HTML risk;
- dirty shared workspaces, live-version drift, environment drift, and merge
  conflicts; and
- naming, ownership, description, and documentation gaps.

An audit is read-only. Findings include evidence, confidence, effect, risk, and
the smallest safe remediation.

### Cleanup and optimization

After exact authorization, the CLI may create a dedicated workspace and stage
dependency-aware changes only after inventorying every existing workspace and
proving `live_version_id == latest_version_id`. If latest differs from live, the
CLI returns `WORKSPACE_CONFLICT` and requires a separate, explicit reconciliation
decision; it never calls `set_latest`, syncs away customer work, or chooses a
baseline silently. If no workspace slot is available, return
`WORKSPACE_SLOT_REQUIRED`; never reuse or delete a customer workspace.

Within that safe workspace, the CLI may stage changes that:

- consolidate duplicate supported tags;
- tighten overbroad triggers;
- migrate supported configurations to reviewed official templates;
- pause unsafe or obsolete vendor tags;
- normalize names, folders, descriptions, and ownership metadata;
- repair supported broken references; and
- delete only `PROVEN_UNREFERENCED` resources.

No cleanup plan edits a customer's unrelated existing workspace or silently
resolves a merge conflict. A resource's apparent inactivity is a recommendation
signal, not deletion proof.

### Controlled creation

V1 may create or update:

- Google tag configuration and allowlisted GA4 event tags;
- provider-native tags/pixels using individually reviewed pinned configurations
  with proved payload, automatic-collection, consent, and egress behavior;
- reviewed variables containing declared, low-cardinality, nonsensitive values;
- Custom Event triggers; and
- page, click, and form-submission triggers based only on allowlisted host/path,
  selectors, and element state—not page text, DOM content, or entered values.

Prefer a declared `dataLayer` event with a versioned machine-readable event
contract. If a business event does not safely exist, return
`NEEDS_SITE_INSTRUMENTATION` and write the local implementation contract. Do not
invent what qualifies as a lead or scrape form data to simulate one.

Controlled UTM fields and a stable campaign join key are allowed. Arbitrary
query parameters, identifiers, tokens, and sensitive values are rejected.

V1 may audit, classify, pause, and plan migration of existing custom HTML. It
does not generate arbitrary JavaScript. Native tag/pixel setup is now in scope
only through individually researched and tested typed provider operations; no
cross-provider parity is inferred. Existing vendor tags may still be
inventoried, audited, safely disabled, or migrated under this contract.

## Provider-native conversion contract

The product includes native conversion resource setup, explicit optimization
association, and reviewed native tag/pixel configuration for selected ad
platforms. These are separately evidenced operations; they do not transform a
GA4 key event or CRM aggregate into another provider's conversion signal. The
scope, mapping, consent, data, authorization, and verification contract is in
[`conversion-integrations.md`](conversion-integrations.md).

Resource existence, correct browser firing, native receipt, processed native
reporting, and optimization association are independent verification dimensions.
GA4 Realtime is not evidence of native receipt. If safe instrumentation or a
supported API is absent, return the missing capability before side effects.
CRM-qualified pipeline and revenue remain source-side aggregate reporting;
person-level CRM events, identifiers, offline uploads, and enhanced conversion
data are excluded. Metadata discovery and crawling remain post-v1.

## GA4/GTM execution

### Stage, preview, publish, verify, and restore

GTM and GA4 Admin have different remote semantics. GTM supports an unpublished
workspace and container version; GA4 Admin changes are live writes with no
equivalent staging surface.

The GTM workflow is:

```text
doctor
-> inventory and dependency graph
-> audit
-> immutable plan
-> authorize-stage (direct or covered delegation)
-> stage in dedicated workspace
-> static and dependency validation
-> GTM quick_preview
-> approved real-browser or Tag Assistant preview
-> create immutable container version
-> prove created-version canonical digest equals the browser-tested draft,
   or browser-preview the exact immutable version
-> authorize-publish (direct or covered delegation)
-> re-read live state and reject drift
-> publish
-> browser/network verification
-> GA4 Realtime verification
-> processed-report verification
-> restore prior version when required
-> export redacted evidence
```

GTM `quick_preview` is compiler and synchronization evidence only. It is not
proof that a browser loaded the tag, consent allowed it, the event fired once,
or GA4 received and processed it.

`create_version` can synchronize the product workspace against GTM's latest
version while creating the immutable version. Canonically serialize all
applicable resources and require the created-version digest/fingerprints to equal
the exact browser-tested draft. If they differ, invalidate the preview and
authorization and test the immutable version itself before publication. A plan may
never publish a version merely because a different workspace state passed
browser preview.

Publish authorization binds the immutable container-version digest, base
live-version ID, affected resources, expected outbound domains, browser host/path, event
contract, consent contract, GA4 changes, stable auth profile/principal/client
application and scopes, validation results, and expiry. Access-token refresh
under the same identity/scopes does not alter the digest. Any other change
invalidates that execution authorization. Publishing requires its own scope
and authorization decision, separate from staging; an explicitly covering
delegation may authorize both decisions without another human confirmation.

Immediately before publish, require the live version still equals the approved
base and the latest version equals the exact product-created immutable version.
Any other live/latest relationship is remote drift and invalidates publication.

If post-publish validation fails, propose republishing the exact previously live
version. Restore is another remote write, not an atomic rollback. Re-read state
after timeouts, never retry blindly, and report `AMBIGUOUS_REMOTE_STATE` when the
result cannot be proven. Restoration cannot unsend data already transmitted.

The GA4 Admin workflow is:

```text
doctor
-> inventory and audit
-> immutable GA4 change plan with captured before state
-> authorize-apply (direct or covered delegation)
-> re-read property state and reject drift
-> apply live Admin API change
-> verify configuration
-> approved browser event
-> Realtime verification
-> processed-report verification
-> compensate/decommission from captured before state when required
-> export redacted evidence
```

A composite GA4/GTM conversion setup is ordered and recoverable, not atomic:
prepare and browser-preview the GTM workspace first, then obtain exact
authorizations, apply the GA4 configuration, publish the immutable GTM version, and verify both.
The plan must include the prior state and an approved compensation action for
each surface. If GTM publish or runtime verification fails after the GA4 write,
restore GTM and either compensate the GA4 change or leave it explicitly marked
`GA4_COMPENSATION_REQUIRED`; never describe the pair as rolled back atomically.

## Consent contract

The customer declares:

- allowed public marketing hosts and paths;
- existing CMP events/categories and expected consent states;
- which tags may run in denied and granted states; and
- approved outbound endpoint domains for preview and production.

The CLI validates that consent defaults run before dependent tags and tests both
denied and granted states. It never overrides denied consent, selects the legal
policy, creates default-accept behavior, or installs a CMP automatically.
Regulated mode returns `CONSENT_CONTRACT_REQUIRED` when the policy is absent or
ambiguous.

## Regulated data and egress contract

Local operation does not make GA4 or advertising measurement safe for PHI. GA4
receives behavioral data, Google prohibits sending PII, and Google does not
offer a GA4 HIPAA BAA. The product makes no HIPAA, GLBA, air-gap, or legal
compliance claim.

In regulated mode, only declared public marketing hosts/routes are testable.
Authenticated, care/member, financial-account, eligibility, underwriting, and
other sensitive application routes fail closed.

Never collect, retrieve for product use, or transmit as measurement/business
data:

- form-entered values or DOM/page text;
- names, emails, phone numbers, customer/CRM/account-holder IDs, authentication
  subject IDs, or raw click IDs;
- health or financial traits, care status, hardship, eligibility, underwriting,
  or protected-class signals;
- sensitive values in event names, parameters, paths, URLs, queries, fragments,
  or `dataLayer` payloads; or
- `user_id`, enhanced-conversion `user_data`, PHI, or person-level offline
  conversions.

Allow event occurrence, declared low-cardinality nonsensitive parameters,
selectors/state without content, and controlled UTMs. Reject unexpected fields
before network transmission or evidence creation.

Schema-approved provider operations necessarily use account/resource IDs and
HTTP request bodies, and an approved consent-granted browser test may use
provider-managed session/measurement cookies. Permit those mechanics only for
the exact operation and destination in the approved plan. Keep raw cookies,
authorization headers, and raw request/response bodies out of display,
persistence, logs, evidence, and support bundles. Keep operational account and
resource IDs local where identity and safety require them; exports use keyed
opaque references.

Real-browser testing triggers network egress. Show the exact endpoint allowlist
and relevant payload-field names in the plan before authorization. Regulated mode blocks
unapproved destinations. Evidence may retain endpoint domains and sanitized
field names, never cookies, request bodies, query values, or identifiers.

## Stable measurement statuses

| Status | Meaning |
| --- | --- |
| `NOT_CONFIGURED_OPTIONAL` | Optional GTM, CRM, or native destination is unconfigured; unrelated workflows remain usable |
| `AUDIT_COMPLETE` | Inventory and diagnostics completed without mutation |
| `PLAN_READY` | Content-addressed measurement plan is ready |
| `STAGE_APPROVAL_REQUIRED` | Exact staging plan lacks direct authorization or covering delegation |
| `STAGED` | GTM changes exist only in the dedicated workspace; no live publish occurred |
| `GA4_APPLY_APPROVAL_REQUIRED` | Exact GA4 Admin change lacks direct authorization or covering delegation |
| `GA4_APPLIED_UNVERIFIED` | GA4 Admin change is live but event verification is incomplete |
| `GA4_COMPENSATION_REQUIRED` | A composite failure requires the approved GA4 compensating action or explicit acceptance |
| `GA4_COMPENSATED` | Captured GA4 before state or approved decommission action was applied and verified |
| `VALIDATION_FAILED` | Static, compiler, consent, browser, or event validation failed |
| `PUBLISH_APPROVAL_REQUIRED` | Immutable GTM version passed preview but lacks publish authority |
| `PUBLISHED_UNVERIFIED` | Version is live but runtime proof is incomplete |
| `REALTIME_VERIFIED` | Browser behavior and GA4 Realtime passed |
| `PROCESSING_PENDING` | Realtime passed; standard GA4 reporting is not ready |
| `VERIFIED` | Processed reporting confirms the intended result |
| `RESTORE_REQUIRED` | Published state failed verification or policy |
| `RESTORED` | Previously live GTM version was republished and verified |
| `AMBIGUOUS_REMOTE_STATE` | Timeout/interruption prevents proof; no blind retry |
| `FAILED_CLOSED` | Policy, auth, drift, unsupported config, or unsafe data blocked the operation |

Required diagnostic codes include:

- `NEEDS_SITE_INSTRUMENTATION`
- `CONSENT_CONTRACT_REQUIRED`
- `SENSITIVE_DATA_RISK`
- `UNAPPROVED_EGRESS`
- `DYNAMIC_DEPENDENCY`
- `WORKSPACE_CONFLICT`
- `WORKSPACE_SLOT_REQUIRED`
- `VERSION_DIGEST_MISMATCH`
- `REMOTE_DRIFT`
- `PUBLISH_SCOPE_REQUIRED`
- `MISSING_EVENT`
- `STALE_EVENT`
- `DUPLICATE_FIRING`
- `THRESHOLDED`
- `PROCESSING_PENDING`
- `PROCESSING_TIMEOUT`
- `GA4_COMPENSATION_FAILED`
- `RESTORE_FAILED`

## Capability-gated future work

These are not permanent exclusions, but none is part of the `v1.0.0` claim:

- server-side GTM inventory/configuration and customer cloud-runtime operations;
- Measurement Protocol with a fixed data schema and separately stored secrets;
- enhanced conversions outside regulated mode with explicit consent and data
  contracts—hashing does not remove privacy obligations;
- CMP installation from a pinned reviewed template and customer-supplied legal
  contract; and
- explicit user-supplied custom code in a high-risk non-regulated mode; and
- the remaining design-only discovery assistant in
  [`future-conversion-architecture.md`](future-conversion-architecture.md),
  including CRM configuration-metadata discovery and bounded public-site
  form/CTA inventory.

Each requires its own authorization, threat model, schema, tests, evidence, and
release claim. Do not expose a generic API, JavaScript, browser, or cloud-runtime
passthrough as a shortcut.

The future site inventory necessarily parses approved public HTML and visible
text locally. It therefore requires a separately authorized discovery-input
class or manifest-only mode; it does not relax v1's prohibition on using
DOM/page text or entered values as measurement data. No crawler or CRM
metadata-discovery authorization is required for `v1.0.0`; optional aggregate
CRM reporting is governed separately by the promoted integration contract.

## Primary references

- [Google Tag Manager API v2](https://developers.google.com/tag-platform/tag-manager/api/v2)
- [GTM API authorization scopes](https://developers.google.com/tag-platform/tag-manager/api/v2/authorization)
- [GTM `quick_preview`](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces/quick_preview)
- [GTM container-version publish](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions/publish)
- [GA4 Data API `runReport`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport)
- [GA4 Realtime reporting](https://developers.google.com/analytics/devguides/reporting/data/v1/realtime-basics)
- [GA4 Admin API key events](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents)
- [Consent mode guidance](https://developers.google.com/tag-platform/security/guides/consent)
- [Google Analytics PII guidance](https://support.google.com/analytics/answer/6366371)
- [Google Analytics HIPAA guidance](https://support.google.com/analytics/answer/13297105)

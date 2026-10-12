# Future conversion architecture assistant

Last updated: 2026-09-04

> **Status:** `POST_V1_ROADMAP_ONLY` for discovery and CRM configuration writes
>
> **Implementation:** not implemented
>
> **`v1.0.0` completion gate:** no
>
> **V1 credentials, validation, soak, or evidence required:** none

The executable source-side CRM aggregate attribution and typed native conversion
configuration subset has been promoted into product scope in
[`conversion-integrations.md`](conversion-integrations.md). That subset no longer
depends on promotion of this discovery roadmap. Its operation evidence remains
unverified; this change is not a working-support claim.

This document's remaining website crawling, CRM metadata discovery, candidate
inference, and CRM configuration-write design authorizes no implementation or
public claim. Promotion requires a separate product-owner goal, current official
API research, capability matrix, threat model, data contract, definition of done,
and release claim. The remaining roadmap must not delay v1 completion or relax
its no-record/no-DOM-content measurement boundary.

## Product intent

A future optional assistant can help a marketer answer three different
questions without pretending they are the same:

1. Which public-site actions and CRM state transitions could be useful signals?
2. Which of those actions does the business actually define as a conversion?
3. Which GA4, GTM, ad-platform, and CRM configuration changes would implement
   and verify that definition safely?

The product discovers candidates; the marketer defines the conversion. A form,
button, thank-you page, lifecycle stage, or closed deal is evidence to discuss,
not permission for the CLI to assign business meaning.

This is a **conversion architecture assistant**, not a generic crawler, CRM data
browser, autonomous optimization engine, or person-level attribution uploader.
It extends the same local plan/authorize/apply/verify/history model as the core
product only after a separate capability review and release claim.

## Proposed operator workflow

```text
declare discovery scope and data boundary
-> inspect existing GA4/GTM and provider conversion configuration
-> optionally inspect selected CRM metadata/configuration
-> optionally inventory approved public marketing pages/forms/CTAs
-> present candidate signals with provenance and uncertainty
-> interview the marketer about actual conversion semantics
-> create a versioned ConversionDefinition
-> map it to each selected destination and expose gaps/unsafe requirements
-> generate an immutable implementation and test plan
-> authorize each write boundary directly or under an explicitly covering delegation
-> stage/apply through the existing guarded adapters
-> test in browser/sandbox and verify downstream receipt
-> append decisions, changes, and evidence to local JSONL history
```

Discovery is read-only. No candidate becomes a conversion definition, key event,
provider optimization signal, CRM automation, or live tag until the user confirms
its semantics and authorizes the exact destination-specific plan, directly or
through an explicit bounded delegation governed by
[`authorization-policy.md`](authorization-policy.md).

## Conversion-definition interview

The CLI asks short, concrete questions and records the answers rather than
guessing from names such as `lead`, `demo`, `MQL`, or `opportunity`:

- What business outcome are we measuring?
- What exact occurrence makes it qualify, and which system is authoritative?
- Is a click or form submission enough, or is a later success/qualification
  state required?
- Which routes, forms, products, audiences, and outcomes are included or
  excluded?
- Does the action count once per person/account/opportunity, once per session,
  or every time? If person-level identity would be required, is that capability
  allowed in the selected privacy profile?
- Can the conversion be reversed, disqualified, reopened, or duplicated?
- Does it have a value and currency, and is that value fixed or source-derived?
- What attribution/lookback interpretation is expected?
- What consent category and data fields are allowed?
- Which systems should observe the signal, and which may optimize against it?

The resulting versioned record is conceptually:

```text
ConversionDefinition
  schema_version
  conversion_definition_id
  version
  supersedes_definition_ref
  status                     # DRAFT | APPROVED | SUPERSEDED
  display_name
  business_outcome
  authoritative_source
  qualifying_transition
  exclusions
  reversal_semantics
  counting_method
  deduplication_contract
  value_contract
  currency
  attribution_expectation
  consent_contract_ref
  allowed_parameter_schema
  prohibited_parameters
  selected_destinations
  owner
  effective_from
  confirmed_at
  confirmation_evidence_ref
  content_digest
```

Changing meaning creates a new version; it never silently rewrites historical
reports. Unknown semantics produce `USER_DEFINITION_REQUIRED`, not a guessed
event.

## Optional HubSpot and Salesforce discovery

Future CRM discovery is explicit, profile-bound, and metadata/configuration-only
by default. The user selects the CRM, local profile or customer-managed tool,
expected portal/org identity, and exact surfaces before any connection occurs.
The CLI does not probe installed tools, token caches, portals, or orgs on its own.

Allowlisted discovery may inspect only the configuration needed to discuss
conversion meaning, subject to current provider capability research:

- lifecycle/status and pipeline-stage definitions;
- object and property schema definitions, not record values;
- selected form definitions and opaque form identifiers;
- existing campaign join-field definitions;
- existing event, goal, attribution, workflow, or automation metadata whose
  definition can be read without retrieving members or execution records; and
- source editions, feature flags, timezone, currency, and attribution-model
  metadata needed to label availability and interpretation.

It must not retrieve contacts, leads, people, companies/accounts, individual
deals/opportunities, campaign members, activities, submissions, field values,
emails, domains, click IDs, transcripts, or free-form CRM records. Generic SQL,
SOQL, object search, shell, CLI, or MCP passthrough remains prohibited.

HubSpot/Salesforce CLIs and MCP servers remain customer-managed optional trust
boundaries. A future metadata-discovery adapter or wrapper gets its own scopes,
fixed calls, strict response allowlist, revocation path, real-account evidence, and current
official-source capability entry. Tool presence never becomes a core runtime
dependency.

## Bounded website discovery

The crawler accepts an explicit `DiscoveryScope`; it never treats the open web
or an entire registered domain as implicit authorization.

```text
DiscoveryScope
  mode                       # MANIFEST_ONLY | STRUCTURAL_WEB |
                             # EXACT_PUBLIC_CONTENT_LOCAL
  allowed_origins
  sitemap_urls
  allowed_path_prefixes
  excluded_path_patterns
  authenticated_routes_prohibited
  maximum_pages
  maximum_depth
  maximum_response_bytes
  maximum_total_bytes
  maximum_duration
  request_rate
  maximum_concurrency
  user_agent
  javascript_mode
  public_content_retention
  external_egress_allowlist
  consent_contract_ref
  expires_at
```

Required behavior:

- read declared sitemap indexes and sitemaps, canonicalize URLs, and optionally
  follow same-origin links only within the approved path/budget;
- honor robots directives by default, use a disclosed user agent, rate limit,
  bound page count/depth/response bytes/total bytes/time/concurrency, and cache
  only within the approved run;
- allow HTTPS by default; reject credentials in URLs, unsupported schemes and
  ports, loopback/private/link-local/metadata destinations, unsafe redirects,
  DNS rebinding, XML external entities, and oversized/decompression-bomb
  sitemaps. Revalidate the destination after every redirect and DNS resolution;
- reject login, member, patient, account, application, eligibility,
  underwriting, checkout, or other sensitive/authenticated routes;
- never authenticate, bypass access controls, submit a form, enter values,
  click a destructive CTA, create a lead, book a meeting, or trigger a purchase;
- use static HTTP parsing by default. JavaScript rendering is a separately
  previewed browser mode limited to approved routes and outbound domains;
- classify forms by structure, method, action origin, safe field-type inventory,
  stable selector/fingerprint, and surrounding route context—never by entered
  or submitted values;
- classify CTA links/buttons by stable selector/fingerprint, exact visible label
  when approved, destination class, placement, and relationship to a form or
  next-step route;
- discover existing allowlisted measurement hooks such as declared data-layer
  event names and non-secret analytics/tag identifiers without retaining raw
  scripts; and
- emit missing, blocked, ambiguous, duplicate, and stale states rather than
  claiming complete coverage.

### Public discovery content is a separate data class

Site discovery necessarily reads HTML and visible CTA text. That would
contradict the v1 measurement rule if treated as ordinary event data, so this
future capability introduces a narrow, explicit input class:

- only customer-approved, unauthenticated public marketing routes;
- raw HTML, rendered DOM, response bodies, and scripts remain transient local
  inputs and are never written to history, support bundles, telemetry, or a
  remote model;
- stored artifacts are allowlisted normalized route templates, form/CTA
  fingerprints, structural metadata, and—only when the user enables it—exact
  public CTA labels and public path components;
- query strings, fragments, cookies, form values, hidden values, personalized
  content, and sensitive identifiers are discarded before persistence;
- a manifest-only mode accepts a customer-supplied route/form/CTA inventory and
  performs no crawl; and
- optional BYO-model classification requires a second explicit preview of the
  exact redacted fields that would leave the machine.

This exception does not permit GTM runtime instrumentation to scrape DOM/page
text, and it does not weaken the core regulated measurement boundary.

In `STRUCTURAL_WEB` mode, exact labels are discarded after transient local
classification and the retained CTA meaning is `UNKNOWN` or a reviewed
normalized category. Exact public CTA labels may be retained only in the
separately enabled `EXACT_PUBLIC_CONTENT_LOCAL` mode after sensitive-text
screening and an explicit retention preview. Manifest-only is the recommended
starting mode for the strictest regulated customers.

## Candidate signal inventory

Discovery emits candidates with provenance; it does not silently turn them into
events:

```text
ConversionCandidate
  schema_version
  candidate_id
  source_kind               # SITE | GA4 | GTM | AD_PROVIDER | CRM_METADATA
  source_ref
  observed_at
  candidate_kind            # PAGE_VIEW | CTA_CLICK | FORM_START | FORM_SUBMIT |
                            # SUCCESS_ROUTE | SCHEDULE_COMPLETE | CRM_TRANSITION
  route_template
  element_fingerprint
  structural_context
  existing_signal_refs
  proposed_business_question
  confidence
  ambiguity_reasons
  sensitive_data_risks
  provenance
  status
```

Prefer an explicit application or `dataLayer` success event over a DOM click or
form-submit listener. A submit attempt is not necessarily a successful lead; a
thank-you page is not necessarily unique; a CRM stage name is not necessarily a
business-approved optimization goal. The assistant must surface those failure
modes during clarification.

## Destination mapping and implementation

Each confirmed definition produces one mapping row per selected destination:

```text
ConversionMapping
  schema_version
  mapping_id
  mapping_version
  supersedes_mapping_ref
  status                     # DRAFT | APPROVED | APPLIED | SUPERSEDED
  conversion_definition_version_ref
  destination_kind          # GA4 | GTM | AD_PLATFORM | HUBSPOT | SALESFORCE |
                            # LOCAL_REPORTING
  destination_profile_ref
  destination_resource_ref
  source_signal
  transform
  counting_and_dedupe
  value_and_currency
  consent_requirements
  required_fields
  prohibited_fields
  capability_status         # READY | CONFIG_REQUIRED | MANUAL_REQUIRED |
                            # UNSUPPORTED | REQUIRES_PERSON_LEVEL_DATA
  semantic_fidelity         # EXACT | LOSSY | REPORTING_ONLY
  verification_contract
  effective_from
  effective_to
  provenance
  created_at
  content_digest
```

Mappings preserve semantics instead of forcing false parity. A GA4 key event,
Google Ads conversion action/import, platform-native pixel/event, CRM lifecycle
transition, and aggregate CRM attribution row remain distinct resources with
distinct counting and attribution behavior.

The assistant may use the existing GA4/GTM/ad-provider adapters to create or
modify only capabilities already researched, typed, and guarded. A future CRM
configuration write surface needs a separate capability matrix, least-privilege
authorization, immutable plan, exact authorization decision, drift check, apply,
read-back, recovery, and live evidence. Generated implementation guidance is clearly
distinguished from an applied change.

Creating a conversion resource does not authorize attaching it to campaign
bidding or optimization. That spend-affecting association is a separate plan
and authorization decision. A bounded delegation may cover that decision only
when it explicitly includes spend-affecting associations.

If a mapping requires email, phone, click ID, contact/lead ID, user ID, enhanced
conversion data, or another person-level upload, regulated mode returns
`REQUIRES_PERSON_LEVEL_DATA` and does not build or execute the upload. Supporting
such a flow later requires a separate non-regulated profile, threat model,
legal/privacy review, destination-specific authorization, retention contract,
and release claim.

## Testing and verification

A plan is not complete because a tag or CRM field exists. Each safe supported
mapping defines its own verification chain, such as:

```text
static contract validation
-> staged GTM/browser test when applicable
-> exactly-once synthetic occurrence
-> GA4 Realtime
-> processed GA4 report
-> destination-native diagnostic/report
-> optional aggregate CRM outcome report after its normal delay
```

The test must prove allowed fields, consent behavior, deduplication, absence of
duplicate firing, and the expected destination identities. Synthetic tests use
disposable or explicitly approved non-production records; they never submit a
real person's information. Delayed, unsupported, thresholded, and unverified
destinations remain visible.

Discovery snapshots, user decisions, mapping versions, plans, authorization decisions,
changes, verification results, and supersessions use the canonical JSONL ledger.
CSV remains a deterministic projection. Raw pages and raw CRM responses do not
enter either format.

When promoted, discovery observations belong in a separate versioned
`discovery` JSONL stream rather than the low-volume mutation stream. Candidate,
page, form, CTA, and CRM-metadata CSVs are projections of that stream, never a
second source of truth.

## Promotion gate for a later release

Do not advertise this feature until all of the following are complete for the
exact promoted surfaces:

- current official-source feasibility and scope research for HubSpot,
  Salesforce, every destination ad platform, GA4, GTM, and browser behavior;
- a threat model and egress/data-flow review for CRM metadata and public-site
  discovery;
- manifest-only and bounded-crawl tests, including sitemap indexes, canonical/
  duplicate URLs, nested/gzipped/cyclic/malformed/oversized sitemaps, XXE,
  decompression bombs, malformed HTML, robots, redirects, loops, rate limits,
  SSRF/DNS rebinding, JavaScript-gated and embedded-form pages, auth/sensitive-
  route rejection, and a guarantee that forms are never submitted;
- fixed-schema CRM metadata fixtures plus real-org identity, scope, revocation,
  pagination, feature/edition, unexpected-field, and no-record-retrieval tests;
- interview tests proving ambiguous candidates cannot become conversions without
  user confirmation and that semantic changes create new versions;
- destination capability, privacy, plan/authorization/drift/apply/verify/recovery,
  and cross-system partial-failure tests;
- network captures proving no undeclared crawl, CRM, model, analytics, or
  provider egress;
- deterministic redacted JSONL/CSV evidence with no raw HTML, raw CRM payload,
  secrets, form values, query values, cookies, or person-level records; and
- bounded live validation on non-customer/disposable properties and explicitly
  authorized public sites—including one static and one JS/embed-heavy site—and
  independent CRM organizations before any production claim.

Until separately promoted, the remaining discovery and CRM-write design is
roadmap context only. Its absence or failure cannot affect v1 installation,
tests, release qualification, or the meaning of `COMPLETE`. This exclusion does
not remove the separately promoted aggregate CRM and native conversion scope
from their own implementation and evidence contracts.

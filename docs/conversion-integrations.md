# CRM attribution and native conversion integrations

Last updated: 2026-09-04

## Scope and evidence

Executable HubSpot and Salesforce aggregate attribution connectors and typed
ad-platform native conversion configuration are in product implementation scope.
This is a scope decision, not evidence of working support. Their operation-level
research, implementation, access, and live evidence remain unverified in
[`capability-matrix.md`](capability-matrix.md). Advertise only the exact evidenced
provider, API version, account eligibility, configuration, and reporting grain.

The v1 baseline includes both CRM adapters, each with at least one fixed
campaign-attributed aggregate workflow covering qualified outcomes and
pipeline/revenue, and at least one declared safe native conversion mapping per
advertising provider. Each native mapping must evidence its resource setup,
applicable campaign association, reviewed signal configuration, browser/native
receipt, and processed reporting. Provider-native equivalences or inapplicable
steps need explicit researched mappings, never empty advertised coverage.
If this baseline cannot be implemented within the data boundary, record the
blocker for a product-owner scope decision; an error-only adapter is not support.

Customers select their integrations and supply their own app, keys, or OAuth
authorization where the provider supports that arrangement. Provider access
requirements remain provider-specific; possession of an account or OAuth consent
does not prove access to every operation. Missing optional customer configuration
returns `NOT_CONFIGURED_OPTIONAL`; it does not block unrelated workflows. A
selected but unavailable operation returns an actionable access or capability
failure before side effects. Do not substitute an unsupported method or fabricate
an outcome.

This scope does not include CRM metadata discovery, CRM configuration writes,
website crawling, person-level CRM retrieval, offline conversion uploads,
enhanced-conversion identifiers, server-side event relays, arbitrary JavaScript,
or automatic CMP installation. The remaining discovery design is in
[`future-conversion-architecture.md`](future-conversion-architecture.md).

## Customer-declared conversion meaning

The customer supplies a versioned conversion contract before configuration:

```text
ConversionContract
  schema_version
  contract_id
  version
  supersedes_ref
  business_outcome
  authoritative_source
  qualifying_occurrence
  exclusions
  counting_and_deduplication
  value_and_currency
  allowed_parameter_schema
  prohibited_parameters
  consent_contract_ref
  destination_profiles
  effective_from
  content_digest
```

Meaning can come from an explicit user instruction or a previously configured
contract. A model may propose it, but must not infer a qualified lead or purchase
from a click, form attempt, URL, or CRM stage label. Missing meaning returns
`USER_DEFINITION_REQUIRED`. Changes create a new version; historical reporting
retains its original version and attribution interpretation.

## Read-only CRM attribution

The executable connectors use fixed reviewed source-side aggregate operations.
Their only reporting inputs are allowlisted campaign/date/model counts and
currency amounts. No record retrieval followed by local aggregation is allowed.
Unsupported reports, fields, editions, models, and join keys remain explicit
gaps. An available campaign count does not prove qualified-pipeline or revenue
coverage.

Each selected source needs an expected portal/org identity, customer-controlled
read profile, fixed query-contract version, bounded windows and pagination,
timezone, currency, attribution model, controlled campaign join key, and an
explicit minimum-cell policy. The source query/report must suppress identifying
small cells before they enter the process. If the provider cannot enforce the
declared aggregation and suppression contract, fail closed for that report;
fetching detailed rows is never a fallback. Bound overlapping query windows and
dimensions to prevent suppressed values being recovered through differencing.

Use the normalized aggregate and join contract in
[`attribution-sources.md`](attribution-sources.md) and
[`reporting-schema.md`](reporting-schema.md). Preserve null, delayed, suppressed,
unmapped, and edition-unavailable results. GA4, CRM, and each ad platform retain
separate totals and model labels. Do not sum them into one conversion count or
claim causal revenue attribution.

Customer-managed CLI/MCP integrations, if chosen as a backend, remain explicitly
selected trust boundaries with fixed typed calls. No automatic installation,
authentication, token-cache inspection, metadata discovery, or generic query
passthrough is permitted. Connector tests use sanitized aggregate fixtures;
customer credentials are never prerequisites for default tests or packaging.

## Native destination operations

For each selected advertising provider, independently research and implement:

1. inventory and diagnostics for native conversion resources, available event
   sources, current optimization associations, and destination identity;
2. typed create/update/decommission of the provider's supported conversion
   resources, with counting, value, attribution, and deduplication semantics;
3. an explicit plan to associate an eligible conversion resource with campaign
   optimization or bidding, including its spend impact;
4. supported native tag/pixel configuration through reviewed, pinned templates
   or an existing declared application event, with a payload and egress allowlist;
   and
5. independent configuration, runtime, receipt, and processed-report evidence.

Each operation has its own matrix entry. A provider that exposes reporting but
no safe configuration API does not gain configuration support by analogy. An
instruction to a customer to instrument their site is a pending dependency,
never an applied change or successful verification. Template support includes
the exact version, permissions, automatic collection defaults, outbound domains,
and consent behavior. Existing custom-code inspection stays local; introducing
raw custom HTML or generated JavaScript is not a workaround.

A GA4 key event is not automatically a Google Ads conversion action, an imported
conversion, a native pixel event, or another platform's optimization signal.
Those resources and associations require separate typed mappings. CRM aggregate
outcomes are reporting evidence; they cannot train a native conversion system
by uploading people, identifiers, or individual deal outcomes under this scope.

Every mapping records the conversion-contract version, provider and account,
native resource and event source, semantic differences, permitted fields,
counting/deduplication, value/currency, consent and egress, capability status,
verification contract, and effective interval. A missing safe event returns
`NEEDS_SITE_INSTRUMENTATION`. A mapping that requires person-level identifiers or
uploads returns `REQUIRES_PERSON_LEVEL_DATA` and fails before egress.

## Authorization and execution

All writes follow doctor, immutable plan, exact authorization, state re-read,
drift rejection, durable pre-egress journal, apply, verify, and bounded recovery.
Exact authorization is either direct authorization of the plan or a decision
under an explicit user-configured delegation policy as defined in
[`authorization-policy.md`](authorization-policy.md). Per-write human
confirmation is not mandatory inside that policy. The model cannot create or
widen its own authority, alter the data contract, or treat a recommendation as
authorization.

Resource creation, spend-affecting association, and GTM publication are separate
action kinds. A delegation must explicitly cover the relevant kinds, accounts,
scopes, spend, expiry, data, and egress bounds; permission to create a resource
does not imply permission to select it for bidding or publish a tag. Re-read and
re-evaluate authorization for the exact plan before each write boundary.

Compile every selected provider and measurement subplan before the first write.
Cross-system execution is non-atomic. Capture before state, dependency ordering,
and bounded compensation for each surface. After a timeout reconcile before any
retry. Recovery cannot unsend events or retroactively alter observed attribution.

## Verification and claims

Track these dimensions separately for every mapping:

| Dimension | Evidence required |
| --- | --- |
| Configuration | Read-back proves the planned resource, destination identity, and settings |
| Browser behavior | Approved synthetic event and network inspection prove consent, allowed fields, expected occurrence count, and no undeclared destinations |
| Native receipt | Provider diagnostic proves receipt by the intended native event source; network success alone is insufficient |
| Processed reporting | Provider aggregate reporting confirms the expected signal after its declared processing interval |
| Optimization association | Read-back proves the intended campaign/bidding association; this does not prove optimization quality or incremental lift |
| CRM attribution | Fixed aggregate report reconciles to the source UI/query with the same model, filters, window, currency, suppression, and join semantics |

Persist dimension-specific `NOT_APPLICABLE`, `UNVERIFIED`, `PASSED`, `FAILED`,
`PROCESSING_PENDING`, or `NOT_AVAILABLE_FROM_PROVIDER` with evidence references.
`NOT_APPLICABLE` needs a documented reason; a provider that offers no observable
receipt evidence must not be called receipt-verified. Realtime or processed
GA4 success cannot substitute for another provider's receipt/reporting evidence.
Do not claim an end-to-end verified mapping while a required dimension remains
unverified, failed, unavailable, or pending.

Browser tests use declared public nonsensitive routes and synthetic events;
they never submit forms, create CRM records, or send a real person's details.
Test denied and granted consent, unexpected fields, duplicate firing, missing
instrumentation, wrong destination, delayed reporting, drift, revocation,
ambiguous writes, and partial recovery. The configured tag must fail closed if
automatic collection cannot meet the declared data boundary. Provider-managed
measurement cookies are limited to the exact consent-granted browser mechanics
permitted by [`measurement-control.md`](measurement-control.md); they do not
authorize identity enrichment, event-row capture, or person-level uploads.

Plans, mapping versions, normalized reports, authorization decisions, changes,
and redacted verification belong in canonical local history. Raw CRM rows,
provider responses, cookies, identifiers, form values, DOM/page text, and custom
source never enter logs, reports, artifacts, or support bundles. Claims require
the operation-level fixtures and live evidence described by
[`definition-of-done.md`](definition-of-done.md), independently of whether a
particular customer has configured the optional integration.

# Attribution sources

Last updated: 2026-09-04

## Product boundary

Attribution sources provide evidence beside the six advertising-platform
connectors. Their attribution models are preserved rather than merged into a
claim of causal truth. GA4 has two deliberately separate surfaces: read-only
aggregate reporting and guarded Admin API configuration. Optional CRM sources
remain read-only evidence boundaries and never receive advertising mutation
capabilities.

| Source | Launch status | Supported role |
| --- | --- | --- |
| GA4 | Required | Aggregate web conversions plus separately authorized event/key-event administration |
| HubSpot | Optional per customer | Executable fixed-schema source-side aggregate attribution connector in implementation scope |
| Salesforce | Optional per customer | Executable fixed-schema source-side aggregate attribution connector in implementation scope |

HubSpot and Salesforce executable aggregate connectors are in implementation
scope. Their presence in this contract is not a support claim: current capability
and live evidence remain unverified. Customers explicitly select and configure
each source using customer-owned applications or authorization where supported;
default commands omit unconfigured CRM sources. Explicit selection or
`doctor --include-optional` reports `NOT_CONFIGURED_OPTIONAL` when a profile is
absent. Missing customer credentials never block unrelated advertising work,
default tests, or packaging. A selected configured source that fails must report
its own failure rather than a fabricated empty result.

CRM aggregate attribution is separate from CRM conversion-definition discovery.
Metadata discovery and configuration writes remain post-v1 and do not authorize
record access. See the promoted execution boundary in
[`conversion-integrations.md`](conversion-integrations.md) and the remaining
roadmap in [`future-conversion-architecture.md`](future-conversion-architecture.md).

The normalized reporting, join, data-quality, and evidence contracts are in
[`reporting-schema.md`](reporting-schema.md). The authoritative GA4/GTM write and
verification contract is in [`measurement-control.md`](measurement-control.md).

## Common privacy contract

Every aggregate reporting source and executable CRM attribution/reporting
adapter must obey these rules. A separately promoted configuration-metadata
discovery adapter follows the allowlisted contract in
[`future-conversion-architecture.md`](future-conversion-architecture.md). GA4
Admin writes instead follow the stricter measurement plan and verification
contract linked above.

1. Query reporting aggregates, not people or individual business records.
2. Aggregate in the source system before data enters this process.
3. Use fixed, reviewed queries and a response-field allowlist. There is no
   arbitrary SQL, SOQL, object search, MCP-tool, or shell passthrough.
4. Reject unexpected fields before display, persistence, logs, or evidence
   export.
5. Never retrieve or persist names, emails, phone numbers, domains, contact or
   lead IDs, campaign-member rows, CRM Company/Account objects, individual
   Deal/Opportunity rows, account-holder records, activity bodies, transcripts,
   raw click IDs, or free-form CRM payloads. Advertising-account identity
   metadata and opaque source references are allowed.
6. Keep credentials in the provider's or operating system's credential store.
   Do not inspect, copy, log, or migrate an external tool's token cache.
7. Do not install, update, authenticate, or invoke an external CLI or MCP server
   automatically. The user selects and configures that trust boundary.
8. Never send attribution output to a vendor service or cloud LLM unless the
   user explicitly configures and previews that separate data flow.
9. Label every result with its provider, date window, timezone, currency,
   freshness, and attribution model. "CRM-attributed" is not causal truth.
10. Treat small aggregate cells as potentially identifying. An executable CRM
    adapter must enforce a documented minimum-cell policy in the source query
    before results enter the process, and validate the aggregate response before
    display or persistence. It fails closed when the policy is unset or cannot
    be enforced and tests below-threshold and differencing rejection. Detailed
    record retrieval followed by local aggregation is never a fallback.

## Normalized aggregate

An attribution row may contain only the fields needed to compare campaign-level
outcomes:

```text
provider
source_account_ref
report_kind
campaign_join_key
window_start
window_end
timezone
attribution_model
conversion_count
influenced_opportunity_count
won_opportunity_count
attributed_pipeline_amount_minor
attributed_won_amount_minor
currency
observed_at
source_report_hash
```

Counts and amounts may be null when the source cannot report them. Money is an
integer in the currency's minor unit, never a floating-point value. Provider
account IDs become keyed, locally generated opaque references in exports rather
than unsalted hashes. `source_report_hash` covers only the validated normalized
aggregate, never a rejected or raw source response. The raw source response is
not stored.

The preferred campaign join key is an ad-platform campaign ID stored in a
controlled UTM or CRM external-ID field. A controlled UTM campaign value is the
fallback. Name-only matching is labeled `HEURISTIC` or `UNMAPPED`; it must not
silently join records.

GA4 and CRM results must be shown side by side. Their models and lookback
windows answer different questions, so the product must not merge them into one
supposedly authoritative
conversion or revenue number.

## GA4: required measurement source

### Read-only reporting

Use the read-only [Google Analytics Data API
`runReport`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport)
surface with allowlisted aggregate campaign dimensions and configured key
events. Do not use audience exports, user-level explorations, client IDs, user
IDs, or BigQuery event exports.

The adapter preserves pagination and data-quality metadata. GA4 can withhold
small or sensitive report slices through [data
thresholding](https://support.google.com/analytics/answer/9383630); a thresholded
or sampled result is labeled, not treated as zero.

Required validation includes:

- read-only authorization and expected property/stream identity;
- timezone, currency, pagination, and date-boundary behavior;
- source/medium/campaign normalization and deterministic joins;
- configured event/key-event discovery and missing/stale/duplicate diagnostics;
- thresholding, sampling, freshness, and processing metadata; and
- comparison with the GA4 UI for identical dimensions, filters, and dates.

### Separately authorized administration

GA4 Admin API support is part of v1 measurement control, not CRM attribution.
A distinct edit profile may plan, authorize, create, update, and decommission
supported event-creation rules, event-edit rules, and key-event configuration.
Every change is property-bound, content-addressed, drift-checked, prospective,
and verified first through Realtime and later through processed reporting.

A GA4 key event is not automatically a Google Ads conversion and is not a
native conversion signal for the other five ad platforms. Those are separate
provider capabilities. See [`measurement-control.md`](measurement-control.md).

## HubSpot: optional aggregate connector

### Preferred boundary

Implement a typed read-only connector for fixed source-side aggregate reports
that pass the common privacy contract. No generic CRM-object API or record-level
file import is included. API/report availability, scope, edition, response
fields, aggregate suppression, and coverage of qualified pipeline/revenue must
be researched per operation before implementation. If an outcome is not safely
available, return an explicit missing capability; do not infer it from traffic
or retrieve contacts/deals. HubSpot tracking, associations, lifecycle
configuration, subscription tier, and attribution model affect the result;
label it `CRM_ATTRIBUTED_HUBSPOT` with those limitations.

The links below are research entry points, not evidence that every desired
aggregate can be retrieved under the contract.

Useful official reporting references:

- [Campaigns API overview and
  scopes](https://developers.hubspot.com/docs/api-reference/latest/marketing/campaigns/guide)
- [Aggregate campaign
  metrics](https://developers.hubspot.com/docs/api-reference/latest/marketing/campaigns/reports/get-metrics)
- [Attribution-report
  behavior](https://knowledge.hubspot.com/reports/understand-attribution-reporting)
- [Traffic-source property
  semantics](https://knowledge.hubspot.com/properties/understand-traffic-source-properties)
- [Ads attribution
  prerequisites](https://knowledge.hubspot.com/ads/ads-attribution-in-hubspot)

The legacy aggregate revenue endpoint may be useful for research, but its
[legacy status](https://developers.hubspot.com/docs/api-reference/legacy/marketing/campaigns/reports/get-revenue)
makes it an unsuitable new production dependency without a fresh lifecycle
decision.

### CLI and MCP knowledge

HubSpot currently has three different tool surfaces that must not be conflated:

- The first-party [HubSpot Agent
  CLI](https://developers.hubspot.com/docs/developer-tooling/local-development/agent-cli/guide)
  is CRM-oriented and supports machine-readable pipelines, dry-run patterns,
  and local operation history. It is [public
  beta](https://developers.hubspot.com/changelog/hubspot-agent-cli-available-in-public-beta),
  has broad read/write capabilities, and remains a user-managed reference—not a
  bundled dependency or generic CRM gateway.
- The [remote HubSpot MCP
  server](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server)
  is an OAuth-based remote service with broad CRM reads and writes. Reference it
  for users who deliberately choose that boundary; never invoke it
  automatically or use it as the regulated-mode default.
- The local [HubSpot Developer
  MCP](https://developers.hubspot.com/docs/developer-tooling/local-development/developer-mcp/setup)
  helps build HubSpot apps and search developer documentation. Its [tool
  list](https://developers.hubspot.com/docs/developer-tooling/local-development/developer-mcp/tools)
  is not a CRM-attribution data source.

Do not install HubSpot's general skill packs. Their person search, enrichment,
audience, and mutation workflows exceed this product's data boundary.

## Salesforce: optional aggregate connector

### Preferred boundary

Implement fixed source-side aggregate attribution queries under an explicitly
selected read profile. Research either a direct API backend or a narrowly typed
wrapper around an already installed [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli).
Backend selection is explicit and never a silent fallback. A CLI backend uses
customer authentication through [`sf org login
web`](https://developer.salesforce.com/docs/platform/salesforce-cli-reference/guide/cli_reference_org_login_web.html).
It stores only the selected alias and expected org identity, passes an explicit
`--target-org` on every call, invokes `sf` with an argument array rather than a
shell, and parses only allowlisted aggregate JSON in memory.

The [`sf data query`](https://developer.salesforce.com/docs/platform/salesforce-cli-reference/guide/cli_reference_data_query.html)
backend, if selected and proven, accepts only fixed reviewed aggregate SOQL.
Never accept user-supplied SOQL, inspect `.sf` or `.sfdx` authorization files,
call commands revealing tokens/auth URLs, or install/update `sf`. Schema and CRM
metadata discovery are still post-v1; query fields come from a reviewed contract
and explicit customer configuration, not runtime object discovery.

Salesforce Customizable Campaign Influence relates Campaign, Opportunity, and
CampaignInfluence data. Preserve the configured influence model because primary
campaign source, custom models, and Account Engagement models allocate credit
differently. See [How Customizable Campaign Influence
Works](https://help.salesforce.com/s/articleView?id=sales.campaigns_influence_customizable_understanding.htm&language=en_US).

Aggregate inside Salesforce and return only counts and currency amounts grouped
by a controlled campaign join key, date bucket, and influence model. Never
select `ContactId`, Contact, Lead, CampaignMember, Person Account, email, phone,
or arbitrary fields. Bulk export is not a fallback because it writes local
record-level files and does not support aggregate functions; split aggregate
queries into bounded windows instead.

### MCP knowledge

[Salesforce Hosted MCP Servers](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/hosted-mcp-servers-overview.html)
are a possible user-selected advanced boundary. The generic SObject read server
can query whatever the authenticated user may read, which is broader than this
product's contract. A narrowly permissioned custom server exposing one
aggregate attribution tool is the only acceptable regulated-mode shape, and it
remains optional.

Salesforce's CLI commands for managing Agentforce MCP registrations remain a
[developer-preview
surface](https://developer.salesforce.com/docs/ai/agentforce/guide/agent-dx-mcp.html)
and must not support production attribution. Salesforce DX MCP is development
tooling, not a CRM-reporting adapter.

For a selected CLI backend, document revocation with [`sf org
logout`](https://developer.salesforce.com/docs/platform/salesforce-cli-reference/guide/cli_reference_org_logout.html)
and test that an expired or revoked authorization fails closed without exposing
authentication output.

## Claim discipline

Public copy may say "connects to HubSpot" or "connects to Salesforce" only after
the corresponding executable adapter has passed operation-level real-account
validation. Until then label it planned or unverified. A tested campaign metric
does not authorize a qualified-pipeline/revenue claim. Aggregate-file import is
not an implicit fallback and needs its own promoted schema and rejection tests.

Do not claim that either CRM source replaces GA4, proves incrementality, reveals
causality, or is available in every customer edition and configuration.

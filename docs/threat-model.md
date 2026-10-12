# Threat model

Last updated: 2026-09-04

## Scope and assets

This model covers the local CLI, agent-facing output, profiles, OS credential
store handles, plans/authorizations, canonical JSONL, SQLite projections,
creative assets, optional artifacts, provider transports, and approved browser
verification. Provider systems, the customer's selected agent/model, operating
system administrators, and user-managed external CLIs/MCP servers are separate
trust boundaries.

Protected assets are credentials, exact account identity, authorization and
spend bounds, ledger integrity/replay state, customer creative, allowed
aggregate reports, conversion semantics, and redacted evidence.

Local execution reduces vendor custody; it does not create an air gap, protect
against an administrator replacing all local files, or establish HIPAA/GLBA
compliance.

## Threats and mandatory controls

| Threat | Boundary | Required prevention/detection | Failure behavior and test |
| --- | --- | --- | --- |
| Secret disclosure | config, args, logs, output, artifacts, Git | OS credential-store references; exact redaction; secret/history scans | reject before persistence; seeded-secret tests across every surface |
| Wrong account/principal | auth to provider | bind profile to principal, app, scopes, and remote identity; re-read before write | `FAILED_CLOSED`; identity-swap and refresh tests |
| Prompt/provider-text injection | agent/provider input | closed schemas; treat text as data; no generic shell/API/action-link execution | input error; injected-instruction fixtures |
| Authorization replay/escalation | agent to kernel | digest-bound one-use grant, policy limits, coordinator locks, durable claim | block and reconcile; concurrent/restart/property tests |
| Spend or publish escalation | plan to provider | typed currency bounds; separate activation/publish operations and profiles | no write; wrong-operation and cumulative-limit tests |
| Drift/race | read-plan-write | before fingerprints, immediate re-read, conditional writes where available | invalidate plan; disclose residual race; drift tests |
| Timeout duplicate write | transport | idempotency only where documented; pre-egress `APPLY_STARTED`; read-back | `AMBIGUOUS_REMOTE_STATE`; no blind retry fault tests |
| Partial multi-provider effect | coordinator | compile all first, ordered subplans, per-provider outcomes and recovery | stop per policy, reconcile all, new plan; integrated fault test |
| Ledger tamper/corruption | filesystem | sequence/digest chain, fsync, locks, checkpoints, verified projections | middle corruption fails closed; torn-tail recovery tests |
| Sensitive data collection | schema/browser/provider response | regulated allowlists; reject person IDs, raw CRM, DOM/text/values, sensitive URLs/events | reject before display/persistence/egress; PHI/PII fixtures |
| Undeclared egress | HTTP, browser, update | capability endpoint classes plus plan-bound host/path/domain allowlists | `UNAPPROVED_EGRESS`; network-capture tests |
| Consent bypass | browser measurement | customer-declared contract; denied/granted tests; no default accept | denied state emits no disallowed request |
| Custom-code exfiltration | GTM inventory/artifacts | local-only source inspection; retain digest/classification/domain only | source-leak canary tests; dynamic references block deletion |
| Spreadsheet/HTML injection | optional artifacts | reversible formula escaping, HTML escaping, no remote assets/scripts/actions | golden malicious-content tests |
| Malicious/overbroad external tool | CLI/MCP boundary | never auto-install/auth/invoke; fixed typed adapter only if promoted | absent by default; no generic passthrough |
| Retention destroys recovery | local cleanup | explicit policy, dependency proof, durable intent/receipt, protected starts/grants | unknown dependency blocks; interrupted cleanup tests |
| Supply-chain/release substitution | build/update | pinned inputs, scans, SBOM, provenance, checksums, signatures, signed tag | verification failure blocks release/update |

## Data classes

- Allowed locally: operational IDs needed for safety, approved campaign
  configuration, aggregate metrics, exact customer creative after scanning,
  digests, plans, and authorization evidence.
- Allowed to configured providers only: schema-approved operation bodies,
  account/resource IDs, and consent-granted browser mechanics for the exact
  authorized destination.
- Exportable after preview: normalized aggregates and keyed opaque references,
  versions, endpoint domains, digests, statuses, and recovery evidence.
- Prohibited: credentials; raw payloads/cookies/headers; person-level CRM or ad
  data; audience files; entered values; DOM/page text; sensitive URLs/events;
  raw click IDs; enhanced identifiers; custom-code source.

## Out-of-scope trust assumptions

Equivalent-user hostile local processes can read user-accessible files or drive
the user's applications; ordinary files are not a security boundary against
them. Provider availability, policy review, and attribution correctness remain
external constraints. Website crawling, CRM metadata discovery, hosted MCP,
server-side GTM, person-level uploads, arbitrary JavaScript, and Apple
advertising are outside v1 and require separate threat models before promotion.

Independent security review and remediation evidence remain release gates in
[`definition-of-done.md`](definition-of-done.md).

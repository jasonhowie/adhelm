# Phase 2A — Assisted customer-hosted MCP

Last updated: 2026-09-04

> **Status:** `POST_V1_BACKLOG`
>
> **Implementation:** not implemented
>
> **V1 completion requirement:** none

## Outcome and boundary

A company installs the product in its own cloud account with our assistance.
Its marketer connects a supported agent, authorizes selected provider accounts,
asks for work conversationally, and reviews exact changes through a trusted
interaction or configures a bounded delegation policy. Its technical owner
maintains the installation after a documented handoff. Credentials, canonical
history, selected media, and operational logs
remain under customer infrastructure administration.

This track follows the [Phase 2 roadmap](README.md). It assumes the required
core workflows have been built and evidenced; there is currently no executable
engine to wrap. It adds no v1 implementation, authentication, soak, or release
requirement. Promotion selects a deployment profile and its own evidence gates.

## Customer experience to build

1. The company names an infrastructure owner and marketing account administrator.
   They choose the supported cloud target, region, network route, identity
   provider, storage policy, and permitted agents.
2. A signed deployment package previews resources, permissions, estimated cloud
   cost, outbound destinations, and any installation assistance access. The
   customer authorizes the infrastructure changes in its own environment.
3. A browser setup wizard checks HTTPS, login, storage durability, secret-store
   access, backup configuration, and agent reachability. Diagnostics identify
   the failed check, responsible owner, and bounded next action.
4. The account administrator authorizes each selected provider through a
   supported flow and confirms account/property/container identities. The wizard
   distinguishes missing API access from an expired token or insufficient scope.
5. The company installs or configures the tested MCP connection in its selected
   agent host. Marketers sign in individually; the service enforces their
   organization, account, and operation permissions.
6. The marketer requests work and receives compact results or selected artifacts.
   An immutable plan receives direct authorization or a one-use authorization
   under the customer's explicit delegation policy, without a redundant prompt.
   Activation and GTM publish require separate operation authority, which may
   be explicitly delegated. Pending work has a durable status reference that
   survives the original conversation.
7. The infrastructure owner practices upgrade, recovery, user removal, and
   provider revocation, then accepts the maintenance handoff. Installation
   assistance access expires or is revoked as agreed.

The marketer should not need to administer containers, construct JSON, type
shell commands, or copy plan digests. Someone at the company still owns service
operations. A company without that owner needs an agreed operations service or
a later vendor-hosted service, which is not planned for the open-source release.

## Reference deployment to validate

Start with one supported cloud and a single customer installation. Select the
cloud from pilot requirements; AWS, Azure, and Google Cloud are candidates, not
current compatibility claims. Prefer a Linux VM running signed, pinned
containers, persistent encrypted storage, and one active mutation executor.
Expand to a managed container platform only when its storage and process
lifecycle meet the same guarantees. Kubernetes and multi-region availability
are not assumed prerequisites.

| Component | Proposed responsibility | Evidence needed before support |
| --- | --- | --- |
| HTTPS endpoint and authentication | MCP calls, setup, trusted authorization, and role checks | Tested identity integration, TLS/Origin checks, revocation, and unauthorized-call rejection |
| Shared application engine | Typed provider operations through the same safety kernel as the CLI | Transport conformance with matching canonical plans and outcomes |
| Durable job execution | Bounded work, status checks, and reconciliation after interruptions | Restart, duplicate delivery, ownership loss, and timeout drills |
| Canonical storage | JSONL streams, immutable plans, and selected content-addressed assets | Fsync/locking proof, integrity verification, restore, and deterministic exports |
| Credential adapter | Customer secret store accessed by a restricted workload identity | Least privilege, refresh, rotation, revocation, and no secret leakage |
| Browser and media workers | Approved measurement verification and deterministic creative analysis | Resource limits, isolated sessions, egress policy, and clean teardown |
| Operations package | Backups, health diagnostics, signed upgrades, and support export | Tested runbooks and explicit customer ownership |

AWS EBS and Secrets Manager are examples of relevant cloud building blocks;
they are not a selected or tested deployment stack. See
[EBS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/storage_ebs.html) and
[Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html).
Record exact services, regions, versions, sizes, cost assumptions, and permissions
in the deployment decision after research and measurement.

## Work breakdown and dependency order

### 2A.0 — Feasibility and pilot definition

- [ ] Assign a product owner, technical owner, and pilot customer operations
  owner. Select the first cloud, network pattern, agent host/version, providers,
  formats, and operations. Keep unsupported operations explicitly unavailable.
- [ ] Create a deployment-specific capability matrix covering the current
  required ad providers, GA4 Data/Admin, and GTM. Recheck customer-owned app
  support, hosted callback URLs, production API rights, scopes, refresh/revocation, quotas,
  and regional restrictions. Record blocked rows and owner actions honestly.
- [ ] Map agent -> MCP authentication separately from service -> provider
  authorization. Decide who registers each application; adding an MCP URL never
  substitutes for a provider application or account permission.
- [ ] Choose a private endpoint for an agent running on an approved network or
  an authenticated route reachable from the selected cloud agent. A cloud
  connector does not inherit the employee's VPN. Validate the actual caller
  location, gateway behavior, and identity flow without publishing a public
  unauthenticated endpoint.
- [ ] Define the proposed customer-server boundary for OS credentials, local
  paths, approved artifact destinations, browser execution, and custom-code
  inspection. Obtain the future contract decision before replacing a local-only
  behavior with a server adapter.

Exit evidence: dated feasibility and custody decisions, explicit pilot scope,
prerequisites the customer can satisfy, and a separately approved test plan.

### 2A.1 — Shared engine and typed MCP interface

- [ ] Expose fixed, versioned tool schemas over authenticated Streamable HTTP.
  Discovery reports capabilities, prerequisites, output bounds, and versions.
  CLI and MCP call one application engine; no generic shell, provider request,
  browser, SQL, or external MCP passthrough is introduced.
- [ ] Define typed operations for inventory/report/audit, plan compilation,
  direct authorization, policy evaluation, execution, status, reconciliation,
  and explicit export.
  Names are selected from the implemented contract, not invented in skills.
  Requesting authorization cannot create or broaden a user grant.
- [ ] Bind each tool, resource, plan, job, and artifact to the authenticated
  organization and authorized accounts. Treat caller-supplied account and
  organization references as selectors to validate, never proof of permission.
- [ ] Implement bounded results, pagination, actionable diagnostics, and durable
  run references. HTTP/MCP request identifiers are not execution authorizations.
  Repeating a call checks the existing operation instead of replaying a write.
- [ ] Adapt versioned agent guidance and optimization skills to discover the
  installed transport and capabilities. Preserve progressive loading, compact
  output, artifact preferences, and explicit unsupported/missing states.

Exit evidence: offline conformance for both interfaces, bounded outputs,
unsupported-operation rejection, and authenticated host compatibility tests.
The deterministic core and default v1 tests remain independent of MCP/models.

### 2A.2 — Identities, credentials, and exact authorization

- [ ] Build the setup wizard and a headless customer-secret-store adapter using
  workload identity. Secrets never enter chat, tool arguments, logs, evidence,
  URLs, or deployment templates. Do not inspect another tool's credential cache.
- [ ] Implement separate user roles and provider read/edit/activation/publish
  profiles where supported. Removing a user, changing their account permissions,
  or revoking a provider grant blocks subsequent protected operations.
- [ ] Implement MCP authorization with token audience validation, approved
  redirects, protected discovery, and defenses against request forgery and
  authorization-flow confusion. Provider credentials are not passed through
  agent-facing authentication. Company SSO alone is not proof of MCP compatibility.
- [ ] Build a trusted canonical review with account, operations, maximum spend,
  scopes, expiry, preconditions, and recovery. Bind direct user authorization
  or a deterministic decision under the customer's current delegation policy
  to the exact plan and provider subplan digests. Inherit policy provenance,
  cumulative limits, revocation, and coordination requirements from
  [authorization-policy.md](../docs/authorization-policy.md). Policy setup is
  user-controlled; covered work needs no fresh human confirmation.
  Generic agent permission, model text, file opening, and GET links cannot
  create or broaden authority. Optional presentation artifacts can remain off.
- [ ] Invalidate authorization on changes to bound deployment identity, user
  authority or policy, account, provider principal/application/scopes, plan, or
  live state. Normal access-token refresh under the same bound provider identity preserves the
  existing rule. Recheck authority and drift immediately before execution.
- [ ] Separate application-user identity needed for access/audit from prohibited
  person-level marketing data. Restrict identity metadata to the security
  subsystem and pseudonymous audit references; never feed it to ad/GA4 payloads.

Exit evidence: no cross-user/account access; forged/stale/replayed authorizations and
revoked grants fail before egress; bounded live authorization works in the
selected agent through direct review and policy-covered execution, including
rejection of policy self-expansion and activation/publish without specific grants.

### 2A.3 — Durable jobs, history, and worker boundaries

- [ ] Keep schema-versioned append-only JSONL canonical. Append and fsync
  `APPLY_STARTED` before mutation egress, then record result, verification, and
  recovery. Queues, indexes, and cached reports cannot be competing operational
  histories; CSV/JSON projections use verified high-watermarks.
- [ ] Serialize mutation ownership for each affected provider account and ledger
  stream, including calls from multiple marketers. Do not allow an independent
  laptop CLI and server to spend the same authorization. Additional workers or
  takeover require demonstrated fencing; lease expiry alone cannot prove the
  previous process stopped sending provider requests.
- [ ] Cover interruption before egress, during a request, and before result
  append; duplicate job delivery; disk exhaustion; lock contention; auth expiry;
  partial multi-provider outcomes; and loss of job ownership. Unresolved starts
  force reconciliation. Cancellation cannot undo a request already sent.
- [ ] Preserve one-use provider authorizations and composite stop/replan rules.
  Compile all selected subplans before the first write and never describe
  cross-provider compensation as an atomic rollback.
- [ ] Make backups and restore verifiable across streams, plans, asset digests,
  and checkpoints. A restored installation starts with writes disabled, does
  not resurrect consumed authorizations, and reconciles possible changes after the
  backup before issuing new plans. Document recovery-time and data-loss targets.
- [ ] Run approved browser sessions and media extraction with isolated temporary
  storage, limited resources, and declared endpoints. Block arbitrary URLs,
  redirects to internal services/cloud metadata, secrets, and sensitive data.
  Do not expose generic browsing or permit form-value/DOM-text collection.
- [ ] Keep GTM custom-code inspection inside the explicitly approved customer
  compute boundary. Never return raw source to the agent, vendor, logs, or
  support bundle. Preserve workspace/dependency proofs, tested-version equality,
  separate publish authority, consent tests, Realtime and processed verification.
- [ ] Preserve creative content hashes, effective-dated deployments, analyzer
  provenance, missing-data statuses, and exact metric grain. Preview and validate
  selected uploads before they enter the configured customer asset store.

Exit evidence: crash/replay and storage drills preserve truthful state and
ledger integrity; measurement and creative checks pass on the selected server
environment; failed recovery leaves mutations blocked with actionable evidence.

### 2A.4 — Packaging, onboarding, and maintenance handoff

- [ ] Ship signed pinned containers, deployment templates, checksums, SBOM,
  dependency/license results, and provenance. Templates preview cost, IAM,
  network, storage, and deletion effects; installing infrastructure is a
  separate customer-authorized action from advertising operations.
- [ ] Implement resumable setup diagnostics without requesting secrets in chat
  or automatically installing/authenticating external agents or CLIs. Supply
  tested connection instructions for each advertised host/version.
- [ ] Translate artifact destinations into explicitly selected, access-controlled
  server locations or customer-initiated downloads. Do not treat a server path
  as a file on the marketer's laptop. Keep references authenticated; prevent
  cross-user access, path traversal, and unrequested uploads or generation.
- [ ] Provide customer-controlled health checks, capacity alerts, backup and
  restore procedures, incident escalation, and redacted support-bundle preview.
  No mandatory vendor telemetry, crash uploads, or remote license heartbeat.
  Schedule work only when separately enabled; scheduled writes also require a
  current policy covering the exact operation. Presentation rules do not create
  schedules or authorize writes.
- [ ] Document signed upgrade/rollback and schema compatibility. Drain or fence
  active work before upgrades; reverting software never rolls back provider
  state. Customer controls update timing and can recover without vendor access.
- [ ] Provide an opt-in local-to-server migration: stop the former writer,
  reconcile unresolved operations, validate and transfer the selected ledger
  checkpoints and asset digests, reauthorize providers through the destination
  credential store, and verify identities. Preserve history and schema versions;
  do not copy token caches, transfer active authorizations, or resume old queued
  writes. Recompile future work after cutover and test a safe return path.
- [ ] Use time-bounded, least-privilege installation assistance authorized by the
  customer. Record activity without secret/raw-payload capture, revoke access,
  and hand over named owners, runbooks, support boundaries, and ongoing costs.

Exit evidence: a fresh customer installation, upgrade, backup restore, provider
revocation, user removal, and assistance-access removal work from the runbook.
Record setup effort and support interventions instead of promising one-click setup.

### 2A.5 — Pilot validation and release decision

- [ ] Demonstrate report -> audit -> plan -> exact direct/delegated authorization
  -> paused/draft change -> verify -> separately authorized activation/publish
  where selected -> recover/export on independent profiles. Unsupported pilot
  operations stay unavailable; a reporting pilot does not prove write or full
  product parity. Exercise both authorization paths and configured stop
  conditions without requiring new human confirmation for valid policy-covered
  work.
- [ ] Have a marketer without CLI experience complete compact-output and
  requested-artifact workflows, then have a different authorized teammate
  inspect history without needing the original conversation.
- [ ] Have the customer's operations owner perform a documented recovery and
  maintenance task after installation assistance has been withdrawn.
- [ ] Run independent security review and an agreed deployment-specific
  reliability observation period with explicit success/failure criteria.
  Reference core live evidence and add deployment evidence; do not silently
  substitute the old v1 soak for hosted-runtime validation.
- [ ] Index exact engine/container/template/host versions, capabilities, account
  pseudonyms, authorizations, test results, source-ledger checkpoints, export hashes,
  and review findings in a release evidence manifest.
- [ ] Decide whether onboarding, recurring use, support time, recovery results,
  and willingness to pay justify support of this deployment. Expand clouds or
  capability claims only after separate evidence and a recorded scope decision.

Exit evidence: the selected deployment meets its promoted acceptance contract;
remaining limitations are visible and the customer accepts operational ownership.
This is not a second way to mark an incomplete v1 `COMPLETE`.

## Ownership and adoption measures

| Responsibility | Customer | Product team |
| --- | --- | --- |
| Cloud account, region, IAM, network, backups | Own and operate | Supply bounded templates and runbooks |
| Provider applications, account permissions, and operation authority | Authorize and verify identities | Explain prerequisites and implement supported flows |
| Product updates and incidents | Apply updates and handle local operations | Supply signed fixes and agreed support |
| Marketing judgment, consent contract, spending | Own decisions and exact authorization | Enforce declared policy and verify execution |
| Agent selection and output preferences | Configure and authorize | Supply tested guidance and bounded results |
| Installation help | Grant/revoke scoped access | Work within the agreed support boundary |

Measure customer technical prerequisites, elapsed deployment time versus approval
waiting time, IT/marketer hours, recurring support, infrastructure/media cost,
time to first useful report, and completion without shell use. Set pilot targets
before testing. No fixed price, resource size, or delivery date is established.

## Decisions to resolve before implementation

- [ ] First cloud and supported network/agent configuration; headless secret
  storage and identity integration; minimum browser/media resource sizing.
- [ ] Provider application ownership and approved server authorization paths.
- [ ] Customer-cloud interpretation of local-only processing, destinations,
  backup retention, and assistance access, with explicit contract deltas.
- [ ] Single-host availability targets and conditions for safe worker scaling.
- [ ] Pilot capability scope, reliability period, support terms, upgrade cadence,
  deployment costs, and acceptance targets.

Research pointers, checked during planning on 2026-09-04 and to be revalidated
for selected versions: [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization),
[Claude remote connector networking](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
and [Google Ads access prerequisites (developer tokens retired 2026-09-09)](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).
These references establish research inputs, not tested product compatibility.

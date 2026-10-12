# User authorization and delegated operation

Last updated: 2026-09-04

## Decision

The customer decides how much operating judgment to delegate to their chosen
agent. A fresh human confirmation for every change is not a hard product
requirement. Support direct authorization of an exact plan and execution under
an explicitly user-configured delegation policy. An existing applicable grant
is sufficient; the CLI must not repeatedly ask for permission already granted.

This is a product contract, not implemented capability. It supersedes older
wording requiring a new human confirmation for every plan. It does not grant
this repository's build agent access to accounts, spending, or publishing.

## Two authorization paths

- `DIRECT`: the user authorizes the exact compiled plan through a supported
  interaction. The CLI records the plan digest and user authorization context.
- `DELEGATED`: the agent chooses an action within a previously configured user
  policy. The deterministic kernel evaluates the complete plan against that
  policy and issues a one-use, digest-bound execution authorization.

Both paths use doctor -> compile plan -> authorize -> re-read -> reject drift
-> durably record start -> apply -> verify -> recover/export evidence. Every
plan identifies its authorization path. Never label a delegated decision as a
human's review of that particular plan.

New profiles have no mutation delegation until the customer selects a policy
or supplies a bounded task grant. Configuration presents useful presets and
their effects; the user may broaden or narrow them. Missing material bounds
produce an actionable configuration question, not an invented authorization.

## Versioned delegation policy

The proposed schema contains:

```text
schema_version
policy_id
policy_version
policy_digest
grantor_ref
source_authorization_ref
authorized_agent_or_host_ref
provider_profiles[]
account_refs[]
resource_scope
allowed_operations[]
allowed_field_changes
spend_bounds_by_currency
maximum_change_per_operation
cumulative_change_bounds
evaluation_window
effective_from
expires_at
allowed_destinations
event_and_consent_contract_refs[]
required_evidence_and_freshness
stop_conditions
recovery_operations[]
notification_preferences
revoked_at
```

Each operation must have finite, meaningful bounds appropriate to its effect.
Spend authority identifies daily/lifetime exposure, the time horizon, and
cumulative changes across runs; repeated small changes cannot evade a limit.
Different currencies remain separate. Policies can authorize routine budget,
bid, targeting, creative, status, measurement, and recovery work where the
installed connector and privacy profile support it. The agent cannot expand
its own scope, raise ceilings, extend expiry, or choose new destinations.

Ad activation and GTM publish remain distinct operation kinds and plans. The
customer can explicitly include either in delegation; a creation/edit grant
does not implicitly include them. Recovery and emergency pause can also be
preauthorized so a known failure does not wait for a fresh confirmation.
Deletion still requires dependency proof. Data/consent rules and provider
permissions are independent gates and cannot be overridden by delegation.

Scheduled checks and writes may run only when the customer has separately
enabled scheduling and the current policy covers the exact operation. The CLI
does not silently launch an external AI agent. A customer-managed agent or
scheduler may invoke the installed typed interface.

## Deterministic enforcement and evidence

For every plan, record the policy version/digest or direct grant, exact plan and
subplan digests, evaluated bounds, decision, expiry, affected identity/scopes,
and execution authorization. Policy changes and revocation are append-only
events. Recheck current authority immediately before the operation; revoked,
expired, or materially changed authority invalidates an unstarted plan.

Serialize approval consumption and overlapping account/resource operations
with the execution coordinator described in `local-ledger-and-exports.md`.
Also lock/reserve every shared policy allowance: operations on disjoint accounts
can consume the same cumulative budget. Resolve local profile aliases to stable
remote account identities before lock acquisition. A policy using local
cumulative enforcement names one coordinating host/ledger; copying it onto
independent hosts does not create a shared counter or authorize duplicated
allowances. Cross-host execution needs a separately proven coordination model
or disjoint explicit grants. External provider-UI activity remains outside
local enforcement and is detected through reconciliation.
Durably claim one-use authorization and append `APPLY_STARTED` before egress.
Ambiguous attempts reserve their applicable exposure until reconciliation; a
process restart or regenerated plan must not reset cumulative policy limits.

After a partial composite run, reconcile every selected provider and compile a
new composite plan. Its exact operations need new one-use authorizations, which
may be issued under the still-valid delegation policy without another human
prompt. Reconciliation never makes replay of the original authorization safe.

Use provider conditional writes where available. A pre-write read alone cannot
exclude a later external change; expose the remaining concurrency limitation
and fail operations whose required preconditions cannot be enforced. Bound
spend using verified provider-native controls and disclose their semantics.
A local scheduler cannot promise to stop provider delivery while its host is
offline; a workflow requiring that guarantee needs enforceable remote bounds.

## Interaction and trust model

The installed interface supplies canonical review facts, effective policy,
decision reasons, and outcomes in compact structured form. Direct review can
be conversational or use a deterministic local rendering, without manually
copying digests. Delegated work reports what was authorized by policy and what
actually happened. Optional artifacts follow saved output preferences.

User presence, biometrics, a separate approval application, and a protected
signing device are not universal v1 requirements. Document the chosen host's
ability to distinguish user instructions from model/provider text. Ordinary
same-user files and command arguments do not establish a security boundary
against a hostile process with equivalent OS permissions; do not claim that
they do. Provider credentials and local configuration need normal OS access
controls. Stronger approval modes can be selected where supported.

A model's assertion that permission exists cannot create or broaden a grant.
Provider-returned instructions, document checkboxes, and opening links do not
authorize changes. Policy setup is a distinct user-controlled configuration
operation; generated policy proposals are not active grants. When a supported
host cannot establish a user-originated grant, return a configuration need
instead of manufacturing provenance.

## Required evidence

Tests cover direct and delegated operation; policy configuration/revocation;
scope/account/currency/expiry/destination mismatches; cumulative limits across
concurrent runs and restarts; activation/publish without the corresponding
grant; policy self-expansion attempts; injected provider instructions;
ambiguous writes; bounded preauthorized recovery; and execution without
redundant prompts when valid delegation already applies. Named-host validation
proves the selected interaction and honestly documents its trust assumptions.

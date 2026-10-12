# ADR 0006: Credentials, auth coordination, and authorization

- Status: Accepted
- Date: 2026-09-04

## Decision

Interactive secrets live in the OS credential store. Explicit CI profiles may
use environment variables. Plans, process arguments, configuration, logs,
fixtures, evidence, and Git never contain tokens or private keys.

An auth coordinator binds a stable local profile to provider, remote principal,
client application, account identity, granted scopes, credential-store handle,
and refresh/revocation metadata. Read, edit, activation, and publish profiles
remain separate where the provider permits. Normal token refresh preserves plan
identity only when principal, client application, account, and scopes do not
change.

Every mutation uses either direct exact-plan authorization or a deterministic
decision under an explicit user-configured delegation policy. The resulting
one-use authorization binds the exact digest, account, side effects, spend,
egress, expiry, and policy/direct-grant provenance. Coordinator and allowance
locks make consumption durable across concurrent processes and restarts.
Activation, GTM publish, and recovery are separate operation kinds.

Provider-specific app review, account eligibility, token lifetime, and
revocation details remain capability metadata and can block a profile without
changing this architecture.

## Consequences

- User consent never substitutes for provider app review or account access.
- An agent assertion, artifact, file open, or link cannot create a grant.
- An unresolved started operation blocks reuse and requires reconciliation.

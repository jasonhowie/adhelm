# ADR 0004: Provider interface and conformance

- Status: Accepted
- Date: 2026-09-04

## Decision

Each adapter implements typed ports for `doctor`, identity, inventory, report,
audit, compile, plan, create-paused, validate, activate, maintain, reconcile,
recover, history, creative capture, and capability discovery. Unsupported
operations return a capability error before egress. Provider-native specs remain
distinct; the common interface standardizes outcomes, not object models.

The kernel owns authorization, coordinator locks, pre-egress ledger writes,
drift checks, egress policy, redaction, and outcome classification. Adapters own
API versions, native objects, pagination, quota handling, safe retries,
conditional/idempotent operations, read-back, and provider-specific recovery.

One conformance suite runs against every adapter using sanitized fixtures,
injected faults, and live profiles. Fixture and live evidence are separate.
Capability declarations identify provider, operation, API version, connector
version, format, auth profile, evidence status, and known limits.

The narrow formats in [`provider-contracts.md`](../provider-contracts.md) are
Phase 0 hypotheses. They do not become supported interface implementations
until fixture and live conformance evidence exists.

## Consequences

- No adapter may bypass the safety kernel with a generic HTTP or provider API
  passthrough.
- Read-back determines applied, failed, partial, or ambiguous state.
- A provider-specific feature cannot compensate for a missing common outcome.

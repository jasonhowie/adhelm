# ADR 0010: Live-test and integrated-fault harness

- Status: Accepted
- Date: 2026-09-04

## Decision

Use one harness with injectable transport boundaries and explicit test classes:
unit/property, sanitized fixture, conformance, fault, credentialed live,
UI reconciliation, browser/measurement, integrated all-six, soak, and release
smoke.

Fault injection occurs only before a selected provider's mutation request
begins, or at controlled response/read-back boundaries in disposable tests. The
required all-six partial-outcome scenario lets an earlier paused subplan
succeed, injects a verified no-effect pre-write transport fault for a later
provider, stops under policy, consumes only started authorizations, reconciles
all providers, replans, and cleans up. The harness never intentionally severs a
real request after egress merely to manufacture ambiguity.

Live profiles use external accounts, independent of the maintainer's own
projects, and credentials outside the repository. Spend and side effects are
bounded in the approved plan. GTM restore faults run only on a disposable
traffic-free site/container/property.

## Consequences

- Fixtures establish engineering confidence but never satisfy live gates.
- Every run emits the evidence metadata in
  [`evidence-model.md`](../evidence-model.md).
- [`live-test-design.md`](../live-test-design.md) owns profile isolation,
  cleanup, UI reconciliation, soak counting, and stop rules.

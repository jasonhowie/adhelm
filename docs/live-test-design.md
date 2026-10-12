# Live-test and integrated-fault design

Last updated: 2026-09-04

## Entry gates

Credentialed testing begins per capability, not per whole product, only after
its identity, schema/privacy, egress, exact authorization, pre-egress ledger,
read-back, ambiguity, and recovery fixture tests pass. The test owner supplies
credentials through the local provider flow, never chat or repository files.
Each plan names maximum spend/side effect and cleanup.

Research and documentation do not satisfy these gates. A sandbox result does
not become production live evidence.

## Profiles

An independent account or organization is one that is not part of the maintainer's
own projects.

| Profile | Purpose | Minimum isolation |
| --- | --- | --- |
| provider sandbox/test | object/schema feasibility where available | no customer production state; environment recorded |
| external ad validation | live lifecycle and UI parity | independent account; paused-first; bounded minimal spend |
| GA4 validation | Data/Admin, Realtime, processed reporting | disposable public nonsensitive stream/property where possible |
| GTM validation | full stage/version/publish/restore | independent; restore drill only on traffic-free disposable site/container/property |
| CRM aggregate validation | fixed source-side aggregate and UI parity | independent authorized account; no record retrieval |

LinkedIn requires two independent external organizations. GTM requires two
independent organizations/containers. All six advertising providers require
one full paused/create/activate/pause-or-archive flow and a controlled
out-of-band UI change.

## Per-provider lifecycle

1. Prove auth, revocation guidance, exact account identity, scopes, API version,
   quota behavior, and complete pagination.
2. Reconcile fixed-window inventory/reporting to the UI.
3. Compile an immutable narrow-format plan and record unsupported mappings.
4. Directly authorize it or evaluate it under an explicit bounded delegation.
5. Create the complete hierarchy paused/draft; read back and reconcile.
6. Validate destination, UTMs, GA4 expectation, and configured GTM/native signal.
7. Separately authorize minimal-spend activation; activate, observe, and pause.
8. Exercise documented maintenance/replacement and one UI-originated change.
9. Reconcile JSONL and deterministic CSV, then archive/restore test resources.

Do not run a mutation if provider review or serving controls cannot bound the
effect. A timeout after possible egress is ambiguous and is reconciled, not
retried.

## Integrated six-provider fault case

One `StrategyBrief` targets all six profiles. The harness produces one report
and recommendation, compiles all six subplans before egress, and binds one
composite authorization to their ordered digests.

After at least one earlier paused/draft subplan succeeds, the injectable
transport returns a proved no-effect failure immediately before a later
provider's mutation request starts. Execution stops under the declared policy.
Started authorizations remain consumed; every provider is re-read; partial
outcome evidence and recovery actions are emitted; remaining work receives a
new plan and authorization; all created objects are archived/restored. A second
run proves the happy path. No deliberate fault is injected after a live
provider mutation request begins.

## Measurement cases

- GA4: browser event, network allowlist, Realtime, processed report, and UI are
  separate evidence entries.
- GTM: inventory all workspaces; prove live equals latest; stage only in a new
  workspace; static/dependency check; `quick_preview`; browser preview; immutable
  digest equality; separately authorize publish; verify; restore.
- Consent: denied and granted states; denied emits no disallowed request.
- Native conversions: configuration, browser behavior, native receipt,
  processed reporting, and optimization association remain separate.
- CRM: source query must enforce aggregate `GROUP BY`/threshold semantics before
  return. No local aggregation fallback.

## Soak and stop rules

The 30-day soak begins only after all required live checks pass. A calendar day
counts only when scheduled `doctor`, `report`, `audit`, GA4 health, and configured
GTM health jobs run and no defect loses data or reports false health. Correctly
classified upstream downtime can count only under
[`definition-of-done.md`](definition-of-done.md).

Stop immediately on wrong identity, drift, unapproved egress, sensitive data,
unbounded spend, consent failure, unexpected serving, ledger integrity failure,
or unresolved ambiguity. Preserve redacted evidence and execute only the
preauthorized safe pause/restore/reconciliation path.

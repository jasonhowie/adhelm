# Recovery safety review

Reviewed 2026-09-04 against the lifecycle and local-ledger contracts. This is
implementation-stage evidence, not a release claim.

## Findings

| Severity | Finding | Reproduction | Status |
| --- | --- | --- | --- |
| P1 | A write with an ambiguous provider outcome and an immediately unchanged re-read was classified as `FAILED_NO_EFFECT`. An eventually consistent provider can expose the old state after accepting a write. | `test_ambiguous_write_with_an_immediately_unchanged_read_is_not_no_effect` returns `AMBIGUOUS_REMOTE_STATE` and retains the unresolved start. | Remediated and passing. `FAILED_NO_EFFECT` now requires an explicit failed outcome, evidence, and provider no-effect proof. |
| P1 | A later provider's locked preflight drift could occur after an earlier provider verified. The prior implementation raised an exception, left no all-provider recovery barrier, and allowed a new write before the selected providers were reconciled. | `test_partial_run_reconciles_every_selected_provider_after_restart_with_expired_grant` makes Google verify, changes Meta before Meta's locked check, restarts, reconciles both providers with an expired original grant, then permits a new plan. | Remediated and passing through the durable `COMPOSITE_RUN` barrier and read-only `reconcile_run`. |
| P1 | Reconciliation could credit an unstarted provider as `APPLIED_VERIFIED` solely because its current state equaled the plan's target. That recorded a local plan result for a remote change this run never attempted. | `test_reconciliation_never_credits_an_unstarted_provider_as_applied` leaves Meta without `APPLY_STARTED`, changes it externally to the target state, then reconciles it. | Remediated and passing: the kernel downgrades the unstarted result to `PARTIAL_EFFECT` and appends an `UNKNOWN` `REMOTE_CHANGE_OBSERVED` record. |
| P1 | `stop_on_non_verified=False` permitted later-provider egress after an ambiguous first provider. | `test_ambiguous_subplan_never_continues_to_later_provider_egress`. | Remediated and passing: unsafe false values are rejected and the kernel always stops after a non-verified outcome. |

## Recovery coverage added

[`tests/test_execution_recovery_review.py`](../../tests/test_execution_recovery_review.py)
adds hermetic fixture-adapter coverage for:

- stale post-write reads and ambiguous outcome classification;
- complete partial-run output after a later locked drift check;
- restart-safe reconciliation of every selected provider with an expired write
  grant;
- a fresh composite only after all selected providers settle;
- an unsettled reconciliation result that preserves the durable barrier;
- account identity mismatch during reconciliation; and
- an externally changed but unstarted provider that must not be credited to the
  local plan; and
- rejection of continued egress after ambiguity.

No provider calls, credentials, or raw payloads were used.

The integration also adds `tests/test_composite_ledger.py`: a global closing
record cannot replace matching durable participant proof. Both verified and
reconciled closures require the same run, plan, authorization reference,
provider, account, resource, and subplan; reconciliation additionally matches
the effect and evidence references. Six parameterized regressions cover
missing proof, an unrelated run, and an unrelated resource.

## Audit follow-up

The local audit review added two more fixture-only probes to the same test file.

- `test_ga4_key_event_count_cannot_satisfy_native_conversion_evidence_policy`
  verifies that a GA4 key-event aggregate remains missing native-conversion
  evidence and never marks that dimension `CHECKED`. The audit now requires a
  complete, mapped `PROVIDER` conversion row with declared action, contract,
  mapping, counting, join, attribution, and quality evidence before suppressing
  the policy's conversion-evidence finding.
- `test_google_ads_cpc_boundary_uses_exact_retained_micros` verifies that a
  20,001-micros cost across two clicks exceeds a one-minor-unit CPC threshold,
  even though the display-compatible money amount rounds to two minor units.

The audit CLI selects a retained `PERFORMANCE_OBSERVATION` through a verified
ledger checkpoint before evaluating it. It does not audit a caller-supplied
report as if it were retained evidence.

# Independent adversarial review

Reviewed 2026-09-04 against the mutation, ledger, privacy, reporting, export,
parser, and agent-output contracts. This review exercised only hermetic local
code and fixtures; it made no provider, customer, credential-store, or network
calls.

## Verified findings and regressions

| Severity | Finding | Reproduction | Current status |
| --- | --- | --- | --- |
| P0 | An `APPLY_RESULT` with `AMBIGUOUS_REMOTE_STATE` or `PARTIAL_EFFECT` cleared the unresolved-start check, allowing a newly authorized plan to run before reconciliation. | Make a post-apply state read fail, then submit a different plan and grant. | Remediated; `test_ambiguous_attempt_blocks_a_new_grant_until_reconciliation` proves the second apply is blocked. |
| P0 | A durable direct approval recorded only its authorization ID and plan digest, so a caller could reconstruct that grant with a later expiry. | Record a one-minute grant, advance the injected clock, then reuse its ID/digests with a future expiry. | Remediated; `test_durable_direct_grant_cannot_be_extended_by_reconstructed_input` proves the durable grant binding controls execution. |
| P1 | Untrusted adapter response references could reach `ExecutionResult` without privacy screening. | Return `person@example.com` as an opaque resource reference from an otherwise successful adapter. | Remediated; `test_adapter_response_cannot_return_sensitive_resource_reference` requires rejection or a safe result. |
| P0 | The HTTP wrapper could send sensitive JSON or query parameters to an allowed host because host checking happened without field screening. | Pass `email` in JSON or request parameters through a mock transport. | Remediated; `test_transport_rejects_sensitive_request_data_before_egress` proves the handler is never called. |
| P1 | Aggregate campaign/account rows could carry a creative identity, and reports could combine unlike currencies under one envelope. | Construct a campaign aggregate with a creative version, or an USD envelope containing EUR spend. | Remediated; the two report-grain/currency regressions in `tests/test_adversarial_review.py` reject both inputs. |
| P1 | A selected export directory could be an empty symlink, causing predictable artifact names to be created in its target. | Point an empty directory symlink at an unrelated target and export. | Remediated; `test_export_rejects_a_symlinked_destination_before_creating_artifacts` verifies no target artifact is created. |
| P1 | A ledger root could itself be a symlink, sending canonical history into an unrelated directory. | Replace `<root>/ledger` with a directory symlink, then append a change event. | Remediated; `test_ledger_rejects_a_symlinked_history_root_before_creating_records` verifies no target record is created. |
| P1 | Report provenance initially assembled only independently sampled nonempty stream heads, so a report/export could expose an incomplete or inconsistent source vector. | Start with an empty stream or append during head collection. | Remediated; `Ledger.checkpoint()` captures and validates all three streams under ordered locks, workflows and projections consume that snapshot, and observation provenance is checked against retained prefixes. |
| P1 | The OpenAI Ads workflow required both the configured raw account ID and its normalized local reference in the same query field, making a configured read fail after identity discovery. | Configure raw account `account_fixture`, discover that identity, then issue the normal report query. | Remediated; the workflow verifies the raw configured identity, then rebuilds the typed adapter query from the discovered local account reference. |
| P1 | GA4 breakdown and realtime partitions could be represented as repeated account/campaign totals without typed dimensions; future query windows and unsupported currency declarations also risked misleading report provenance. | Use a source/date/event partition, a future date window, or a mismatched query currency. | Remediated; parser output has structured partitions, future/currency checks fail before egress, and realtime no longer claims an unproved freshness instant. |
| Rechecked guard | A `GA4_EVENT` dimension value is a local event reference and needs contextual export redaction. | Redact a realtime event partition through the normal projection redactor. | `test_export_redacts_realtime_event_dimension_references` proves keyed opaque output. |
| Review correction | An earlier draft of this review incorrectly treated the OpenAI Ads read adapter as undocumented. | Read `docs/research/openai-clean-room-reference.md`, which records current OpenAI Ads documentation, fixed read routes, and the clean-room provenance boundary. | The adapter is fixture-tested only and makes no live-provider claim; the research note records the live-feasibility unknowns that still block a stronger status. |

## Remaining review limits

The implementation remains `IMPLEMENTING`. Required live-provider evidence,
the complete provider lifecycle, provider-native conditional-write semantics,
and release/soak gates remain contract work. The internal transport wrapper is
not a public passthrough, but adapters must bind every mutation method, path,
and body to an approved typed operation before a mutation capability can be
claimed.

`Ledger.verify()` remains a per-stream integrity check. `Ledger.checkpoint()`
is the intentionally stronger cross-stream trust boundary for reports, exports,
and index rebuilds; it validates the complete source vector and historical
creative references before exposing those derived views.

## Validation snapshot

- `uv run pytest -q tests/test_adversarial_review.py`: 10 passed.
- `uv run pytest -q`: 89 passed.
- `uv run mypy`: passed with no issues in 21 source files.
- `uv run ruff check src tests`: passed.

These checks validate the reviewed implementation snapshot. They do not supply
the live evidence or release artifacts required for a completion claim.

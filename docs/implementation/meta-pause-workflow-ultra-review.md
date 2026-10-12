# Meta campaign PAUSE workflow and CLI ultra review

Review date: 2026-09-06  
Scope: M41 configured planning, local retention, and historical review only.

This review exercised the M41 planner and typed CLI with fake EDIT-session
facts, local JSONL history, and monkeypatched private seams. It did not open a
credential store, call Meta, launch a native helper, issue an approval, or
perform a provider write.

The independent coverage in
`tests/test_meta_pause_workflow_ultra_review.py` checks that malformed
constructed request objects fail before configuration access; mismatched
configuration/profile digests and account currency/timezone facts stop after
the read but before retention; a backwards local read clock blocks a plan; the
final callback performs no ledger read while the append lock is active; and an
append followed by a local storage failure remains ambiguous while the single
retained record is left for inspection.

`tests/test_meta_pause_cli_ultra_review.py` checks that an invalid history
digest fails before configuration or sharing-key access; a complete redacted
historical view is sized before the real sharing key is obtained; a constructed
workflow result cannot serialize private fields; a FIFO passed as an artifact
target is rejected without being opened or reaching the planner; and discovery
plus schema inspection do not resolve the workflow.

During review, the CLI historical-view composition was corrected. M40 keeps its
direct caller-supplied output-limit validation as invalid input. The installed
CLI now builds a fixed-size probe and applies the saved output preference
itself, so an insufficient saved bound returns `OUTPUT_LIMIT_EXCEEDED` with a
precondition exit before the real HMAC sharing key is read.

Validation completed with:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m41-uv-cache uv run pytest -q \
  tests/test_meta_pause_cli.py \
  tests/test_meta_pause_cli_ultra_review.py \
  tests/test_meta_pause_workflow.py \
  tests/test_meta_pause_workflow_ultra_review.py \
  tests/test_meta_pause_planning_integration.py
# 35 passed

UV_CACHE_DIR=/private/tmp/adhelm-m41-uv-cache uv run mypy \
  tests/test_meta_pause_cli_ultra_review.py \
  tests/test_meta_pause_workflow_ultra_review.py
# Success: no issues found
```

Ruff check and format check pass for both independent test files. The reviewed
surface retains only a non-authorizing local plan. It does not establish a
native approval kind, provider POST acknowledgement contract, current campaign
state, spend cessation, execution permit, or remote mutation result.

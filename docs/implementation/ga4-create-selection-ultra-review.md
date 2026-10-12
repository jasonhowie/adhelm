# GA4 CREATE candidate-selection ultra review

This independent review covers
`select_ga4_key_event_create_candidate()` in
`src/adhelm/ga4_create_selection.py`. The tests use only temporary local JSONL
ledgers and synthetic declarations. They make no provider, credential,
browser, socket, authorization, plan, or mutation call.

The helper validates the public candidate digest, exact typed fresh input, and
aware evaluation instant before reading history. It then takes exactly one
verified ledger checkpoint. The selected candidate and latest definition for
its exact contract ID both come from that one snapshot. A later local append
after the checkpoint cannot be mixed into the returned selection; the result
retains the checkpoint digest so callers can identify that historical boundary.
This is not a currentness or reservation guarantee after the checkpoint locks
are released.

The latest retained definition always controls selection. A newer definition
blocks an older candidate even when its declared effective time is in the
future: the helper does not infer a scheduled fallback. If the selected current
definition itself is not effective at the explicit instant, selection fails.
The effective boundary is inclusive after UTC normalization.

Fresh input is rebuilt through the strict candidate-input boundary and compiled
again against the selected retained contract and record digest. The rebuilt
closed candidate must match the retained candidate byte-for-byte. Changes to
customer-definition prose, counting-rule prose, Boolean requiredness, or the
destination profile cannot reuse an older compilation. The retained compiler
candidate contains the normalized native request required internally, but does
not retain the fresh `EventContract` source prose. The detached selection has
no `fresh` field and suppresses its repr; any future agent-visible output must
still pass the existing redacted sharing boundary because the selection and
retained conversion contract are internal local values.

The review found no remaining source defect after the frozen implementation.
It separately verifies the related M23 OAuth lifecycle repair: the binding
registry records the sample taken at binding registration, so a local clock
sequence T0 → registration T0+2 → consume T0+1 rejects and consumes the
handoff instead of releasing a bearer.

`tests/test_ga4_create_selection_ultra_review.py` adds independent cases for
single-checkpoint/no-append operation, egress prohibition, future supersession
before recompilation, exact fresh structural and prose drift, malformed or
constructed public input before a checkpoint read, an unmixed historic snapshot
when history changes afterward, effective-time failure, and fresh-prose
non-retention in the candidate.

Validation completed with:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_create_selection.py \
  tests/test_ga4_create_selection_ultra_review.py \
  tests/test_saved_ga4_key_event_candidates.py \
  tests/test_conversion_contracts.py \
  tests/test_google_oauth_execution_binding.py::test_consume_rejects_clock_rollback_after_binding_registration \
  tests/test_google_oauth_execution_binding_ultra_review.py::test_clock_rollback_after_resolution_cannot_release_a_bearer
.venv/bin/ruff check src/adhelm/ga4_create_selection.py \
  tests/test_ga4_create_selection.py \
  tests/test_ga4_create_selection_ultra_review.py
.venv/bin/ruff format --check src/adhelm/ga4_create_selection.py \
  tests/test_ga4_create_selection.py \
  tests/test_ga4_create_selection_ultra_review.py
.venv/bin/mypy src/adhelm/ga4_create_selection.py
git diff --check
```

This selection remains a local prerequisite. A subsequent workflow must take
fresh configured EDIT-profile evidence, produce and retain an exact plan,
obtain valid authorization, re-read and reject drift before egress, verify the
result, and report recovery honestly.

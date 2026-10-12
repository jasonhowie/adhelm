# GTM retained-history ultra review

Date: 2026-09-04

This review exercised the fixture-only GTM retained-measurement path in
`measurement_records.py`, `saved_measurements.py`, `measurement_cli.py`, and
the shared CLI emitter. It did not call a provider, retrieve a customer
credential, or make a real network request.

The review added adversarial coverage for the persisted boundary. Parent-scoped
account, container, workspace, and version references are revalidated; duplicate
workspaces, a swapped live/latest role, resource totals above the fixed bound,
and custom-template markers that disagree with resource counts are rejected.
A ledger tamper test changes a GTM parent or version role, recomputes both the
snapshot digest and enclosing record digest, and still fails verified checkpoint
validation. This confirms that digest recomputation alone cannot make an invalid
GTM relationship trusted.

The tests also confirm that an unknown `model_copy` attribute is not serialized
into retained data or CLI output, while an explicitly serialized raw-source
field is rejected before a ledger record is created. A malformed public GTM
container ID stops before sharing-key access or the workflow reader. Paged GTM
review reconstructs every saved row once with exact cursors under a 4 KiB bound,
preserving the sequential-read, header-count, dependency, and staging
uncertainty. The shared emitter accounts for its terminating UTF-8 newline at
the exact byte limit.

Validation run:

```text
.venv/bin/pytest -q tests/test_gtm_identity.py tests/test_gtm_measurement_workflow.py tests/test_gtm_workflow_ultra_review.py tests/test_gtm_read.py tests/test_gtm_read_ultra_review.py tests/test_gtm_counts.py tests/test_gtm_measurement_history.py tests/test_gtm_history_ultra_review.py tests/test_saved_measurements.py tests/test_measurement_history_ultra_review.py tests/test_measurement_cli_integration.py tests/test_guidance.py tests/test_guidance_ultra_review.py
# 125 passed in 1.33s

.venv/bin/ruff check src/adhelm/cli.py src/adhelm/measurement_cli.py src/adhelm/measurement_records.py src/adhelm/saved_measurements.py tests/test_gtm_history_ultra_review.py
.venv/bin/ruff format --check src/adhelm/cli.py src/adhelm/measurement_cli.py src/adhelm/measurement_records.py src/adhelm/saved_measurements.py tests/test_gtm_history_ultra_review.py
.venv/bin/mypy src/adhelm/cli.py src/adhelm/measurement_cli.py src/adhelm/measurement_records.py src/adhelm/saved_measurements.py
```

This is fixture evidence for a bounded read and local-history review only. It
does not establish live GTM response conformance, credential lifecycle behavior,
workspace-content or dependency completeness, staging eligibility, compiler or
browser proof, consent behavior, GA4 receipt, publishing, recovery, release
evidence, or v1 completion.

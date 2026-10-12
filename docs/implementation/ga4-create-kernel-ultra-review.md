# GA4 CREATE kernel and guarded-adapter ultra review

This independent review covers the GA4 key-event CREATE safeguards in
`safety.py`, `ga4_create_plan_history.py`, `ledger.py`,
`ga4_create_execution_sources.py`, and the private configured adapter. The
tests use only the existing memory credential backend, local ledger fixtures,
`httpx.MockTransport`, and an in-memory kernel permit. They do not open an OS
credential store, native approval host, browser, or provider connection.

## Repaired finding

The initial start-history scan indexed retained plans by plan digest without
rejecting a second physical plan record with that digest. A subsequent
`APPLY_STARTED` could therefore have an ambiguous local plan source. The start
validator now tracks duplicate plan digests, and the current-source helper
requires exactly one canonical matching retained plan. The independent cases
cover both paths.

## Independent coverage

- A GA4 CREATE adapter returning `AMBIGUOUS`, or a `FAILED` response carrying
  claimed no-effect evidence, cannot be upgraded by a matching later state or
  a stale `verify()` success. Both stay `AMBIGUOUS_REMOTE_STATE`.
- A GA4 CREATE adapter without `GuardedAdapter` fails before capability,
  identity, state, mutation, or execution-ledger work.
- The configured guarded adapter rejects even a genuine in-memory permit until
  the kernel's durable `APPLY_STARTED` record exists; it performs no provider
  GET or POST in that rejection path.
- Duplicate retained plan records reject a new start and a current execution
  source check. A newer version of the selected conversion contract or a newer
  selected-profile EDIT observation rejects under the changes-stream append
  lock before a start is written.
- A later GA4 candidate does not invalidate execution merely by existing.
  Candidates are parallel immutable alternatives. The execution binding pins
  the exact selected candidate while requiring the latest version of its own
  conversion contract and the latest EDIT observation for the selected
  profile.

The guarded adapter rechecks local source, configuration, and fingerprint-key
facts before fixed provider reads and immediately before consuming the kernel
permit. Its private runtime has no public bearer, generic transport, or
serialization surface. The review found no additional concrete adapter defect
after that source was frozen.

These are local integrity and ordering checks. They do not prove an atomic
cross-process provider reservation, current remote CREATE permission, a
provider-origin event, or a remote outcome after a transport failure. A
nonconfirmed GA4 CREATE remains recoverable only through reconciliation and a
new plan; it is never silently retried.

## Validation

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_ga4_create_kernel_ultra_review.py
# 10 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  tests/test_ga4_create_kernel_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check \
  tests/test_ga4_create_kernel_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy \
  src/adhelm/safety.py \
  src/adhelm/ga4_create_plan_history.py \
  src/adhelm/ga4_create_execution_sources.py \
  src/adhelm/providers/ga4_admin_key_event_create_adapter.py
git diff --check
```

All listed checks passed.

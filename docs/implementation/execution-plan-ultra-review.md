# Execution-plan retention ultra review

## Scope

This independent review covered the pure fixed OpenAI pause contract,
`EXECUTION_PLAN` record model, local plan retention and retrieval, and the
narrow ledger binding. It covered one local historical action only. It did not
add a command, credential lookup, provider request, authorization path, or
execution path.

## Reproduced findings and fixes

| Finding | Reproduction | Fix |
| --- | --- | --- |
| Nested `Literal` controls accepted JSON `true`, `1`, or `1.0` where the closed action requires integer version `1` or boolean `true`. | Revalidate a valid raw action after substituting boolean or float forms for its action/plan schema versions or `stop_on_non_verified`. | The retention boundary checks the raw control scalar types before model normalization. |
| A Python mapping with bytes or `Decimal` values could be serializer-coerced into a valid action. | Substitute bytes for an account reference or `Decimal` for native micros in a raw action mapping. | The action boundary now permits only JSON-shaped mappings and checks the canonical domain before validation. |
| `Ledger.append()` validated an execution-plan payload, then could serialize a caller-mutated original mapping. | Replace a complete self-consistent action while append is between preflight and directory setup; the old envelope and new payload previously persisted together. | Record construction now receives the detached validated payload, including the performance revision path. |
| Catalog and load metadata exposed Python `datetime` values. | Serialize a catalog or provenance mapping with ordinary JSON serialization. | Public metadata uses UTC JSON timestamp strings. |

## Verified boundaries

- The extracted contract preserves pinned pre-extraction action, composite, and
  subplan digests for fixed fixtures.
- Retention binds the ledger profile to the plan profile; the subplan's account
  authentication profile separately binds to the action binding. Provider,
  account, source, run ID, and remote timestamp mismatches fail before a ledger
  directory is created.
- Rehashed record-envelope or action-account tampering fails while selecting the
  verified checkpoint. Catalog pagination is newest-first, complete across its
  cursor boundary, and excludes the action, native identifiers, profile, and
  credential-binding reference.
- Malformed or incomplete constructed/copied models fail closed. Complete valid
  copies can round-trip through the same closed reconstruction.
- The record remains marked
  `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`,
  `HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE`, and non-authorizing. Retention
  does not establish current remote state or permit a future write.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q tests/test_openai_pause.py tests/test_openai_pause_ultra_review.py tests/test_saved_openai_pause_plans.py tests/test_execution_plan_ultra_review.py tests/test_ledger.py tests/test_composite_ledger.py
```

The command passed 130 tests. Ruff format/check and mypy passed for the four
retention source files and the two retention test files. All tests used local
fixtures; no customer credential, provider call, or provider mutation was
performed. The workspace-versus-credential-profile routing required by any
future CLI remains an explicit integration constraint.

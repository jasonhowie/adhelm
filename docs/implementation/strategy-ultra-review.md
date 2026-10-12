# Strategy compiler ultra review

Reviewed: 2026-09-05
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope

This review covered the closed offline aggregate in `src/adhelm/strategy.py`,
its builder tests, the adversarial regressions in
`tests/test_strategy_ultra_review.py`, and the implementation contract in
`docs/implementation/strategy-compilation.md`.

It used the existing local fixture briefs only. It made no provider request,
credential read, HTTP call, CLI invocation, history write, authorization, or
mutation.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | Direct `StrategyBrief` and `CompiledStrategy` construction trusted nested already-built Pydantic instances. A caller could use `model_copy()` or `model_construct()` to bypass closed business-intent flags, account references, native brief fields, or exact integer money checks. | The public aggregate boundaries now revalidate nested model instances through typed adapters before their cross-field bindings run. Raw mappings use the same typed JSON route, so valid serialized tuple/date fields continue to load while JSON floats remain invalid for `Money.minor_units`. |
| P1 | A non-zero-offset timezone was accepted as a strategy expiry, and whitespace-only assumptions or provenance were accepted. | Strategy expiry now requires a zero UTC offset. Assumptions and provenance entries must contain a non-space character. |
| P2 | A Python-mode model dump contains native dates and datetimes; using `json.dumps()` while revalidating it raised a `TypeError` on otherwise valid compiled strategies. | The closed adapters now serialize their typed input with `TypeAdapter.dump_json()` and immediately reparse it. This preserves strict JSON validation without a date/datetime failure or numeric coercion. |

## Verified behavior

- The aggregate accepts only one to six selected providers, with each expected
  account bound exactly once to its matching provider-native brief. Each
  wrapper retains its provider's reviewed objective/bidding literal and exact
  native budget. Per-currency caps are positive exact integers, preserve their
  ISO currency, and cover every selected native maximum without FX conversion.
- The compile boundary rejects a constructed brief with an unreviewed tracking
  flag before any child compiler runs. It revalidates constructed child
  candidate specs and totals before comparing them to a fresh compilation of
  the closed source.
- A late forced OpenAI child failure occurs only after the preceding five
  selected local child compilers were attempted and returns no
  `CompiledStrategy`. There is no partial aggregate, provider egress, or
  retained result.
- Source context, approval owner, expiry, assumptions, provenance, ordered
  child digests, typed totals, and each child's explicit offline planning
  boundary remain bound to the source brief. Redacted child reviews omit the
  fixture copy and destination values exercised here and keep
  `OFFLINE_CANDIDATE_NOT_EXECUTABLE` plus
  `FIXTURE_ONLY_NOT_LIVE_VALIDATED` visible.
- `CompiledStrategy` deliberately contains the full local source/native graph
  for integrity comparison. It is not safe to emit directly; an agent-facing
  command must produce a separate redacted projection.

## Retained limits

This is a local compilation candidate, not an executable cross-provider plan,
common-semantic proof, account identity/currency confirmation, provider
eligibility result, creative/native-conversion readback, tracking proof,
authorization, remote resource, delivery record, spend enforcement, drift
check, recovery result, or release evidence. A successful aggregate says only
that every selected fixture-native compiler produced its bounded local
candidate. It does not establish v1 or release completion.

Provider failures currently stop at the first failing selected child. That
fails closed with no aggregate, but it is not a multi-provider diagnostic
aggregation feature.

## Validation

```text
.venv/bin/pytest -q tests/test_strategy.py tests/test_strategy_ultra_review.py
# 16 passed

.venv/bin/ruff check src/adhelm/strategy.py tests/test_strategy.py tests/test_strategy_ultra_review.py
.venv/bin/ruff format --check src/adhelm/strategy.py tests/test_strategy.py tests/test_strategy_ultra_review.py
.venv/bin/mypy src/adhelm/strategy.py
# Success: no issues found in 1 source file
```

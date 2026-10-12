# M9 candidate-history ultra review

Reviewed: 2026-09-04
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope

This review covered the Meta and Google variants of the closed candidate
history record, retained-candidate helpers, candidate catalog/show CLI paths,
compiler retention receipt preflight, and normal verified-ledger projection
redaction. It used local fixture briefs, temporary directories, and fixed
in-memory HMAC keys. It made no provider request, read no customer credential,
and performed no provider mutation.

## Finding fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | Pydantic accepted an already-built Meta or Google compilation model through its nested-instance fast path. A caller could use `model_copy()` or `model_construct()` to set a literal planning gate false, then construct `CandidateCompilationRecord` directly without re-running the variant validator. Serialized ledger reads did revalidate it, but the public record boundary did not. | `CandidateCompilationRecord` now validates every compilation through the closed discriminated JSON adapter. It reparses model instances to defeat the fast path and also reparses raw mappings through JSON so strict tuple fields retain the existing JSON-array ledger behavior. A valid OpenAI JSON record continues to load normally. |

## Verified behavior

- The record discriminator admits only installed OpenAI, Meta, and Google
  candidate variants. Meta and Google retain literal unresolved planner gates,
  one declared creative reference, their typed review, and exact native-budget
  projection while omitting native serving requests, copy, destination URLs,
  Page/image values, targeting IDs, and provider authority.
- Rehashed serialized records with a false planning gate, altered native budget,
  or duplicate parent/role handle fail before ledger creation. This is structural
  validity after the candidate digest has been recomputed, not a superficial
  hash-mismatch check.
- Candidate show and ordinary verified export HMAC the top-level and nested
  account, campaign, creative-version, and role-handle references consistently.
  Equal raw references receive equal opaque values; raw fixture values are
  absent from both outputs.
- A retain receipt reserves the final terminal newline before append. The exact
  byte bound succeeds without sharing-key access; one byte less fails before
  retention with `OUTPUT_LIMIT_EXCEEDED` and an actionable no-append diagnostic.
- A mixed twelve-record catalog walk under the 1 KiB saved bound returns every
  newest-first record exactly once, advances by emitted rows, includes the
  newline in each bound, and never accesses the sharing key. Invalid retain
  input and invalid show selection also fail before key access or append.

## Retained limits

These records are local, fixture-verified compilation observations with
`OFFLINE_CANDIDATE_NOT_EXECUTABLE`; they are not remote resource evidence,
native requests, executable plans, authorization, identity confirmation,
creative-ledger proof, provider-file proof, drift check, or mutation evidence.
All declared creative references remain
`DECLARED_NOT_VERIFIED_IN_LEDGER`. Provider eligibility, delivery, conversion,
policy, placement, native readback, authorization, and recovery remain
unverified. This review does not establish release or v1 completion.

## Validation

```text
.venv/bin/pytest -q tests/test_candidate_history_m9_ultra_review.py tests/test_candidate_history_new_providers.py tests/test_saved_candidates.py tests/test_candidate_ultra_review.py tests/test_compile_cli.py tests/test_compile_cli_providers_ultra_review.py
# 58 passed

.venv/bin/ruff check src/adhelm/candidate_records.py src/adhelm/saved_candidates.py src/adhelm/candidate_cli.py src/adhelm/compile_cli.py src/adhelm/projections.py tests/test_candidate_history_m9_ultra_review.py
.venv/bin/ruff format --check src/adhelm/candidate_records.py src/adhelm/saved_candidates.py src/adhelm/candidate_cli.py src/adhelm/compile_cli.py src/adhelm/projections.py tests/test_candidate_history_m9_ultra_review.py
.venv/bin/mypy src/adhelm/candidate_records.py src/adhelm/saved_candidates.py src/adhelm/candidate_cli.py src/adhelm/compile_cli.py src/adhelm/projections.py
# Success: no issues found in 5 source files

git diff --check
```

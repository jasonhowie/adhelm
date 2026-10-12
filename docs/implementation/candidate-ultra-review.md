# Retained offline candidate ultra review

Reviewed: 2026-09-04

## Scope

This review covered the closed candidate schema, candidate history helpers,
candidate and compiler CLI paths, canonical-ledger binding, ordinary redacted
exports, and the OpenAI compiler's authorization boundary. It used local
fixtures only; it made no provider request and accessed no credentials.

## Finding fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | A caller of the local ledger API could append a valid `CANDIDATE_COMPILED` payload with an arbitrary `run_id`. A rehashed local candidate record with that field also passed verified reads. This made an explicitly non-executable compilation observation appear connected to a composite execution run. | Candidate append and ledger verification now reject every non-null `run_id` with `INVALID_CANDIDATE_PROVENANCE`. `tests/test_candidate_ultra_review.py` covers both direct append and a rehashed on-disk record. |

## Verified behavior

- Retention recompiles the closed brief and writes only the normalized review,
  digests, bounded USD spend, declared creative references, and literal unmet
  preconditions. It retains no native request, copy, destination, provider file
  ID, approval, subplan, or execution plan.
- Candidate catalog and show select only `Ledger.checkpoint()` records. Show
  and normal exports apply the private reference pseudonymization boundary to
  account, campaign, and creative identifiers.
- The retain receipt is checked against the output bound before append. A
  failure after an append attempt reports `CANDIDATE_RETENTION_UNVERIFIED`,
  exposes no receipt or persistence claim, and requires verified-history
  inspection before another attempt.
- A candidate cannot satisfy the mutation kernel's `CompositePlan`,
  authorization, `APPLY_STARTED`, or composite-run schemas. Fresh compilation,
  verified state, ordinary plan construction, and direct or delegated
  authorization remain required before any egress.

## Retained limits

The record is local planning evidence, not provider evidence. Declared creative
references remain `DECLARED_NOT_VERIFIED_IN_LEDGER`; provider file association,
provider byte equality, identity, drift, parent-result binding, mutation
responses, recovery, and authorization are all unresolved. The candidate is
therefore correctly and permanently labeled
`OFFLINE_CANDIDATE_NOT_EXECUTABLE` in this slice.

## Validation

```text
.venv/bin/pytest -q tests/test_saved_candidates.py tests/test_compile_cli.py tests/test_candidate_ultra_review.py
# 18 passed
.venv/bin/ruff format --check src/adhelm/ledger.py tests/test_candidate_ultra_review.py
.venv/bin/ruff check src/adhelm/ledger.py tests/test_candidate_ultra_review.py
.venv/bin/mypy src/adhelm/ledger.py tests/test_candidate_ultra_review.py
# clean
```

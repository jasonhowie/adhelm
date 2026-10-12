# Safety input-boundary ultra review

Reviewed: 2026-09-04  
Reviewed source commit: `9b8745ccc32c84f890b4a42b76a84bc48f33c955`

## Scope

This review covered the public-input boundary of `src/adhelm/safety.py`, including
direct grants, delegation policies, execution, and reconciliation. It used
local fake hosts, adapters, and ledger fixtures only; no provider, credential,
or real approval-host request was used.

## Findings fixed

| Severity | Reproduced issue | Fix |
| --- | --- | --- |
| P1 | `model_copy()` and `model_construct()` could bypass nested Pydantic validation. Forged agent-origin assertions and negative nested spend could reach a trusted host, ledger lookup/reservation, or adapter identity read. | Every public safety entry point reparses its plan, authorization, grant, or policy through the closed model before host, ledger, lock, or provider-adapter work. Invalid input returns `INVALID_SAFETY_INPUT`. |
| P1 | `record_direct_grant()` could call the approval host before rejecting a grant bound to a different plan. | Plan/grant expiry and digest binding run before host verification, so a wrong-plan grant cannot reach the host or durable history. |
| P2 | A policy revocation timestamp could be naive, and a crafted timezone object could have `tzinfo` while returning no UTC offset. Such values could reach policy host/history handling. | Revocation, effective, and expiry timestamps require a real UTC offset and are normalized to UTC before the host boundary. |

## Verified boundary

- `execute()`, `record_direct_grant()`, policy install/revoke, and
  `reconcile_run()` revalidate their public models and nested values before
  their corresponding side-effect boundary.
- Invalid agent-origin source assertions, malformed nested money, wrong-plan
  grants, and invalid timestamps do not call the fake approval host, adapter,
  or ledger and do not append a record or create an unresolved run.
- Revalidation also applies the existing literal-data validation to the
  reconstructed model instead of trusting an in-process object.

## Remaining limits

This is a local kernel boundary, not an installed approval host or mutation
runtime. It does not authenticate a human interaction, configure a host,
obtain credentials, or prove a provider adapter's remote behavior. Real writes
still require a deployed trusted host, registered typed adapter, current
provider identity/state reads, durable journaling, and the existing exact
authorization and recovery flow.

## Validation

```text
uv run pytest -q tests/test_safety.py tests/test_safety_input_boundary.py tests/test_safety_input_ultra_review.py tests/test_execution_recovery_review.py tests/test_adversarial_review.py
# 49 passed
uv run ruff check src/adhelm/safety.py tests/test_safety_input_ultra_review.py
uv run ruff format --check src/adhelm/safety.py tests/test_safety_input_ultra_review.py
uv run mypy src/adhelm/safety.py
# clean
```

# OpenAI pause-plan view ultra review

Reviewed: 2026-09-05  
Status: `IMPLEMENTING`; deterministic fixture-only library evidence.

## Scope

This review covers the local review projection in
`src/adhelm/plan_views.py`, the baseline tests in
`tests/test_openai_pause_view.py`, and the adversarial regressions in
`tests/test_pause_plan_view_ultra_review.py`. It also reviews the isolated
installed-wheel smoke wrapper in `scripts/validate_development.py` and its
regression in `tests/test_validate_development.py`.

The review projection accepts a supplied action, private sharing key, and
review time. It does not construct a provider client, read a credential store,
read or append the ledger, issue a provider request, or perform a mutation.
Tests use only the existing in-memory OpenAI fixture transport and never call a
real provider, keychain, or DNS service.

## Findings fixed

| Severity | Reproduction | Fix |
| --- | --- | --- |
| P1 | A self-consistent `model_construct()` action could replace the account timezone with a non-IANA value, an unsupported bid mode, or a malformed native campaign ID, rebuild the fixed plan, and have the view present that value as a review fact. | The view now detaches and revalidates the action, then verifies the source reader's IANA timezone, native-ID, local-reference, and supported bid-mode constraints before HMAC redaction or rendering. |
| P1 | A malformed nested constructed value caused Pydantic's ordinary serializer to emit a warning containing that value's `repr` before closed-action validation rejected it. | Revalidation serializes with warnings promoted to errors, catches the failure, and suppresses the original exception context. The review returns the bounded `INVALID_PLAN` diagnostic without exposing the malformed representation. |
| P2 | The view independently reconstructed zero incremental spend instead of reading the sole validated subplan's exact money value. That happened to agree with the fixed pause contract, but weakened the source-fidelity boundary. | The review now derives the displayed money object from the validated subplan. The fixed pause plan still has zero incremental spend in its account currency. |

## Verified boundaries

- The review preserves the exact composite digest, ordered subplan digest,
  expiry, account currency/timezone, native lifetime-budget micros, bid mode,
  start time, end time, and active-to-paused state transition from the
  reconstructed action.
- The review accepts an expired plan for inspection and marks it expired at the
  supplied aware review instant. It does not refresh the plan, extend its
  expiry, or change its digest.
- Account, campaign, credential-profile, and workspace-profile references are
  HMAC-pseudonymized with the supplied private sharing key. Native client IDs,
  the credential-binding HMAC, raw credential, and sharing-key bytes are absent
  from the canonical review. The two opaque profile fields preserve whether the
  original workspace and credential profile identities were equal without
  exposing either local reference.
- The output remains explicitly
  `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`; `review_authorizes_execution`
  is always false. It records only the documented gaps for conditional writes,
  idempotency, ambiguity, and possible residual delivery/billing.
- The installed-wheel smoke wrapper runs each installed CLI call under blocked
  socket connect and DNS resolver functions, a blocked credential backend, and
  a temporary credential-lock root below the smoke data directory. Its schema
  and configuration smoke also verifies that the OpenAI account-key profile
  remains closed and key-free.

## Remaining limits

This review proves internal action consistency and safe presentation only. A
same-process caller can still construct a different syntactically valid action;
the view has no ledger provenance, current provider read, approval-host, or
authorization capability that could prove its baseline came from a provider.
It cannot turn such an object, an opened review, or a review digest into a
grant, execution result, fresh provider observation, or live validation claim.

The installed-smoke guards are a regression boundary for this development
script, not a general operating-system sandbox for arbitrary code. Provider
identity, credential lifecycle, current-state readback, exact authorization,
ledger pre-egress records, pause execution, recovery, and live validation stay
governed by their separate contracts and are not evidenced here.

## Validation

```text
.venv/bin/pytest -q tests/test_validate_development.py tests/test_openai_pause.py tests/test_openai_pause_ultra_review.py tests/test_openai_pause_view.py tests/test_pause_plan_view_ultra_review.py
# 63 passed

.venv/bin/ruff check src/adhelm/plan_views.py tests/test_pause_plan_view_ultra_review.py tests/test_validate_development.py
.venv/bin/ruff format --check src/adhelm/plan_views.py tests/test_pause_plan_view_ultra_review.py tests/test_validate_development.py
.venv/bin/mypy src/adhelm/plan_views.py tests/test_pause_plan_view_ultra_review.py tests/test_validate_development.py
# clean
```

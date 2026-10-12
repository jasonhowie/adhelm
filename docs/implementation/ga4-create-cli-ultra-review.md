# GA4 key-event CREATE CLI ultra review

This independent fake-only review covers the installed root commands in
`ga4_create_cli.py`: `plan ga4-key-event create`, `catalog`, and `show`, plus
their discovery and root-only schemas. It uses the existing memory credential
backend and `httpx.MockTransport`; it does not open a real credential store,
browser, provider connection, or native host, and it sends no CREATE request.

The review added 18 adversarial cases in
`tests/test_ga4_create_cli_ultra_review.py`. They invoke the root Typer app,
not just the command module.

## Repaired findings

- A valid-shape workflow result could claim an unretained plan-record digest.
  The receipt path now reloads that exact digest from the verified ledger,
  requires canonical action, action-digest, plan-digest, and observation
  binding equality, and derives the receipt only from the stored record.
  Forged results fail as `GA4_CREATE_PLAN_RESULT_INVALID`.
- A local ledger read failure after a workflow append was initially folded into
  the invalid-result diagnostic. The command now returns the fixed ambiguous
  `GA4_CREATE_PLAN_RECEIPT_UNVERIFIED` result with history/catalog and
  no-blind-repeat guidance. The independent regression performs a real fake
  GET-and-append sequence before injecting the receipt read failure.
- `show` forwarded loader provenance without a closed output boundary. It now
  validates an exact allowlisted provenance surface before the zero-key sizing
  probe or sharing-key access, so injected event or token fields fail as
  `INVALID_GA4_CREATE_PLAN_REVIEW` without disclosure. `catalog` likewise
  rejects extra or promoted fields and supplies fixed command-level
  no-authority controls.
- The CLI initially preflighted artifact policy and output size from one
  configuration snapshot but let the workflow load a later snapshot. It now
  passes the preflight configuration digest into the workflow. A matching
  `GENERATE` rule introduced between those steps returns
  `CONFIGURATION_DRIFT` before protected-key access, provider GETs, retention,
  or file creation.

## Independent coverage

The review verifies:

- the minimum 1,024-byte saved bound returns only a compact, zero-spend,
  no-authority receipt after the fixed GET-only planning workflow, without a
  sharing-key read;
- duplicate JSON keys, malformed JSON, float schema versions, and over-1-MiB
  input fail before configuration, workflow, provider, key, or retention work;
- matching complete and incomplete `plan_review` artifact rules fail closed
  without an artifact fallback;
- receipt-size failure occurs before provider, key, or ledger work;
- catalog is credential-, sharing-key-, provider-, and per-record-lookup-free,
  has bounded pagination, and reports fixed false authority and mutation facts;
- show is a complete redacted historical review, marks expiry at review time,
  has no provider lookup, and obtains the sharing key only after its complete
  zero-key review fits; and
- root discovery and root-only schemas expose closed request and retained-plan
  shapes without an execution command or authority.

The retained plan and all CLI results remain historical planning evidence.
They establish neither a current Google state, a reservation, a provider CREATE
permission, a protected approval, nor authorization to mutate. Configuration
is checked at the defined local snapshot boundaries; this is not a claim of
atomicity against external edits that occur and revert between those checks.
No GA4 CREATE plan artifact renderer is installed.

## Validation

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_create_cli.py \
  tests/test_ga4_create_cli_ultra_review.py \
  tests/test_ga4_create_workflow.py \
  tests/test_ga4_create_workflow_ultra_review.py \
  tests/test_ga4_create_selection.py \
  tests/test_ga4_create_selection_ultra_review.py \
  tests/test_ga4_create_contract.py \
  tests/test_ga4_create_contract_ultra_review.py \
  tests/test_ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation_ultra_review.py \
  tests/test_google_oauth_execution_binding.py \
  tests/test_google_oauth_execution_binding_ultra_review.py
# 186 passed

.venv/bin/ruff check src/adhelm/ga4_create_cli.py \
  tests/test_ga4_create_cli.py tests/test_ga4_create_cli_ultra_review.py
.venv/bin/ruff format --check src/adhelm/ga4_create_cli.py \
  tests/test_ga4_create_cli.py tests/test_ga4_create_cli_ultra_review.py
.venv/bin/mypy src/adhelm/ga4_create_cli.py src/adhelm/ga4_create_workflow.py
git diff --check
```

All listed scoped static checks passed.

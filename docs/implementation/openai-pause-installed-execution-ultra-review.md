# OpenAI pause installed-execution ultra review

## Scope

This independent review covers the private retained-plan OpenAI pause workflow.
It uses synthetic retained plans, an ephemeral signed bridge candidate, mocked
credential functions, and mock HTTP only. It does not launch a helper or host,
invoke OS authentication or a credential store, or contact OpenAI.

## Boundaries reviewed

- A historical action without an explicit workspace binding is rejected before
  configuration, credential, bridge, or provider work.
- The current profile must be the exact configured `OS_STORE` OpenAI `EDIT`
  profile selected by the retained action. A CI source or changed profile ID
  stops before native review.
- A plan that expires after its native receipt claim never reaches the kernel's
  provider path. The durable approval record remains consumed and no
  `APPLY_STARTED` or POST is added.
- A no-op configuration write can complete while native approval is in flight,
  proving that a configuration transaction is not held across the native wait.
- A shape-valid direct grant with the same plan but a changed grantor fails the
  SafetyKernel durable-authorization comparison before HTTP.

## Result and limits

No remaining OpenAI workflow defect was reproduced. The workflow rechecks the
retained source and credentials after approval, while SafetyKernel independently
requires the exact durable grant before provider reads and egress. These checks
coordinate the local process and append-only ledger; they do not make provider
state, OS credential storage, or a native bridge transaction atomic.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_openai_pause_execution_workflow.py \
  tests/test_openai_pause_execution_workflow_ultra_review.py
```

The focused fake-only command passed 25 cases, including six independent
cases. It does not execute configured native code or make a network request.

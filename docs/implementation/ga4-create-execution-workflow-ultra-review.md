# GA4 CREATE configured execution workflow ultra review

This independent review covers the receipt-to-kernel entry point in
`ga4_create_execution_workflow.py` and its current-source helper in
`ga4_create_execution_sources.py`. It exercises only ephemeral P-256 receipt
fixtures, the existing in-memory OAuth fixture, canonical local ledger files,
and `httpx.MockTransport`. It does not call Google, open a browser, access an
OS credential store, or invoke the native approval host.

## Boundary verified

The execution request accepts only the exact retained-plan digest and a fresh
typed candidate input. It rebuilds the request before use, reloads the exact
retained action, verifies the configured GA4 EDIT target/profile/configuration
binding, requires a current fingerprint key, and validates the selected local
candidate, conversion contract, and EDIT observation before receipt
acceptance. The receipt is verified against the separately supplied pinned
trust and its exact locally constructed challenge; it cannot select a trust
configuration or plan record itself.

After durable receipt claim and direct-grant recording, the private configured
adapter factory repeats current source, configuration, and fingerprint-key
checks before it resolves OAuth and again before fixed provider reads. The
SafetyKernel independently checks the durable grant, exact plan, expiration,
unresolved execution history, guarded adapter, and durable start before its
one-use permit can reach the fixed POST.

## Independent coverage

`tests/test_ga4_create_execution_workflow_ultra_review.py` adds eight
fake-only cases:

- a constructed exact-class challenge with invalid wire bytes fails without an
  approval claim, OAuth resolution, or provider request;
- a receipt signed under another P-256 key and an otherwise valid challenge
  evaluated under a deliberately smaller trusted host byte limit both fail
  before OAuth or provider traffic;
- configuration drift stops before fingerprint-key access or OAuth binding
  resolution;
- a relevant conversion-contract supersession after receipt claim but before
  private adapter construction blocks the factory before any provider request;
- fingerprint-key rotation after claim blocks before OAuth resolution and
  provider traffic;
- a simulated failure after durable receipt claim leaves the claim consumed;
  replaying the exact receipt returns `APPROVAL_RECEIPT_REPLAY` and cannot
  reach the provider; and
- a confirmed fake execution serializes neither the bearer, refresh token, nor
  raw OIDC subject in its result or complete canonical ledger checkpoint.

No additional concrete workflow or source-validator defect was reproduced.

## Limits

The pinned trust is deliberately supplied by a future installed integration;
this library does not discover, enroll, rotate, or persist trust. The local
fingerprint and observation HMACs establish current-key local integrity, not
Google origin, a live `analytics.edit` grant, CREATE permission, duplicate
reservation, provider atomicity, or a remote result after a lost response. A
failure after receipt claim is intentionally non-retryable with that receipt.
The configured path remains an internal development-fixture surface, with no
installed mutation CLI or live-conformance claim.

## Validation

```sh
.venv/bin/pytest -q tests/test_ga4_create_execution_workflow_ultra_review.py
# 8 passed

.venv/bin/pytest -q \
  tests/test_ga4_create_execution_workflow.py \
  tests/test_ga4_create_execution_workflow_ultra_review.py \
  tests/test_ga4_create_kernel_ultra_review.py \
  tests/test_ga4_admin_key_event_create_adapter.py
# 30 passed

.venv/bin/ruff check tests/test_ga4_create_execution_workflow_ultra_review.py
.venv/bin/ruff format --check tests/test_ga4_create_execution_workflow_ultra_review.py
.venv/bin/mypy \
  src/adhelm/ga4_create_execution_workflow.py \
  src/adhelm/ga4_create_execution_sources.py
git diff --check
```

All listed checks passed.

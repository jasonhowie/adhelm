# Compiler CLI ultra review

Reviewed: 2026-09-04  
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope and evidence

This review covered the offline Meta and Google preview wrappers in
`src/adhelm/compile_cli.py`, their registration and versioned input schemas in
`src/adhelm/cli.py`, the existing OpenAI candidate-retention receipt preflight,
the builder coverage in `tests/test_compile_cli_new_providers.py`, and the
adversarial regressions in
`tests/test_compile_cli_providers_ultra_review.py`.

The CLI registration was inspected but not changed. The review used fixture
briefs, an in-memory fixed redaction key, and temporary local directories. It
did not read a customer credential, invoke provider HTTP, retain a Meta or
Google candidate, mutate a provider, or claim an executable plan.

## Findings and independent checks

| Severity | Finding | Regression and disposition |
| --- | --- | --- |
| P1 | An over-limit Meta or Google preview read or created the OS sharing key before `emit` rejected the result. The same ordering existed for a non-retained OpenAI preview. | `_ensure_redacted_preview_fits` produces the exact final redacted envelope with a fixed in-memory HMAC key before `export_key()`. Opaque HMAC references are fixed-length ASCII for every valid key. The exact-boundary regression covers Meta, Google, and OpenAI previews: the byte limit including the terminal newline succeeds; one byte less returns `OUTPUT_LIMIT_EXCEEDED`, touches no sharing key, and leaves only the saved configuration. |
| P1 | OpenAI `--retain` compared the receipt bytes without the newline written by `emit`. At that boundary, local retention could occur before output failed. | The preflight now adds the terminal newline. `test_openai_retain_preflight_counts_the_newline_before_append` verifies that an exact raw-byte limit rejects before the retention call and that raw bytes plus one succeeds and retains exactly once. |
| Check | Caller-selected account and role labels, native references, copy, destination URLs, Page/image values, and creative identifiers require explicit output-boundary evidence; no leak was found in this review. | Meta and Google adversarial previews substitute private values across account, hierarchy, targeting, creative, and serving fields. The result exposes only opaque references and permitted summaries; every substituted raw value is absent. No data directory is created during a successful non-retained preview when the sharing-key function is mocked. |
| P2 | A generic output-limit diagnostic instructed users to request a smaller page or use `--limit`, neither of which exists for compiler previews. | The preview preflight now says to increase saved `max_output_bytes` or reduce the brief and states that no candidate was retained or provider action sent. The boundary regression asserts the actionable diagnostic. |
| Check | Malformed-input rejection before sharing-key access and local state creation required independent validation; this invariant held. | The malformed Meta JSON regression patches key access to fail if called, receives `INVALID_JSON`, and confirms no data directory exists. Builder coverage separately verifies unknown fields fail as `SCHEMA_VALIDATION_FAILED` before key access or history. |

## Retained limits

All three commands return `OFFLINE_CANDIDATE_NOT_EXECUTABLE`. Meta and Google
previews are non-retained, use no provider action, and report
`FIXTURE_VERIFIED_LIVE_UNVERIFIED`. A returned digest or paused request does
not establish a remote resource or executable plan.

The wrappers do not establish customer identity, account currency or timezone,
native-ID binding, placement or policy eligibility, targeting behavior,
creative transformation, delivery, conversion behavior, provider response
semantics, authorization, drift handling, recovery, or release readiness.

## Validation

```text
.venv/bin/pytest -q tests/test_compile_cli.py tests/test_compile_cli_new_providers.py tests/test_compile_cli_providers_ultra_review.py tests/test_meta_compiler.py tests/test_meta_compiler_ultra_review.py tests/test_google_ads_compiler.py tests/test_google_ads_compiler_ultra_review.py tests/test_openai_compiler.py tests/test_openai_compiler_ultra_review.py
# 67 passed in 0.64s

.venv/bin/ruff check src/adhelm/compile_cli.py tests/test_compile_cli_providers_ultra_review.py
.venv/bin/ruff format --check src/adhelm/compile_cli.py tests/test_compile_cli_providers_ultra_review.py
.venv/bin/mypy src/adhelm/compile_cli.py tests/test_compile_cli_providers_ultra_review.py
# Success: no issues found in 2 source files

git diff --check
```

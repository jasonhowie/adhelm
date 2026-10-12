# OpenAI pause credential-binding key ultra review

Reviewed: 2026-09-05  
Status: `IMPLEMENTING`; local fake-store evidence only.

## Scope

This review covers the dedicated binding-key lifecycle in
`src/adhelm/credentials.py`, reserved-reference enforcement in
`src/adhelm/models.py` and `src/adhelm/credential_setup.py`, and
`tests/test_openai_pause_binding_key_ultra_review.py`. It used only
in-memory fake stores and redirected temporary lock roots. It did not read or
write a real keychain, provider credential, provider, filesystem fallback, or
network endpoint.

## Findings fixed

| Severity | Reproduction | Fix |
| --- | --- | --- |
| P1 | The export-pseudonymization key was the only reserved internal reference, so a future binding-key slot could be configured as a primary, secondary, or CI provider secret. | The new binding-key reference is reserved case-insensitively at model, configuration, credential-access, and credential-setup boundaries. Forged `model_copy()` values fail before store access. |
| P1 | Reusing the export key for a credential binding would couple projection pseudonyms to pause-plan bindings and make their rotations inseparable. | A different fixed OS-store reference holds a separately generated 32-byte key. Export-key code and storage semantics remain unchanged. |
| P1 | An optimistic generated key could bind a plan even when the OS store silently dropped or replaced the write. | The binding-key lifecycle holds the existing per-reference lock, validates the generated encoding before mutation, and requires exact post-write readback. |
| P1 | Accepting an account-key provider secret from CI does not establish a safe lifecycle for a second private binding key. | Explicit-CI profiles fail before any ambient environment or OS-store access; no undocumented environment variable or file fallback exists. |
| P1 | Store values or backend exceptions could expose a raw binding key in an error path. | All malformed, write, readback, and backend failures collapse to a bounded diagnostic with suppressed formatted exception context. |

## Verified boundaries

- The key is exactly 32 decoded bytes and is reused only from its dedicated
  slot after a valid read.
- A held case-insensitive lock returns `CREDENTIAL_SETUP_BUSY` before the
  fake backend is opened. Existing process-level lock coverage exercises the
  same lock primitive.
- Missing/corrupt values are not overwritten. Dropped writes, replacement
  writes, read errors, and write errors never report the generated or stored
  value.
- The separate export slot can initialize in the same fake store without
  sharing a reference, byte value, or mutation path with the binding slot.
- The review does not claim a live OpenAI key, provider identity, plan,
  authority, pause operation, or successful credential rotation.

## Validation

```text
.venv/bin/pytest -q tests/test_openai_pause_binding_key_ultra_review.py \
  tests/test_credential_boundary.py tests/test_credential_ultra_review.py \
  tests/test_credential_setup.py tests/test_openai_account_key_profile_ultra_review.py
# 49 passed

.venv/bin/ruff check src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_setup.py tests/test_openai_pause_binding_key_ultra_review.py
.venv/bin/ruff format --check src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_setup.py tests/test_openai_pause_binding_key_ultra_review.py
.venv/bin/mypy src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_setup.py tests/test_openai_pause_binding_key_ultra_review.py
# clean
```

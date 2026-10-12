# Binding-key independent review

Reviewed: 2026-09-05  
Status: local fake-store evidence only.

## Scope

This independent review exercised the OpenAI pause credential-binding key in
`credentials.py`, its reserved-reference model boundary, and credential setup
with fake stores and temporary lock roots. It made no keychain, provider,
network, credential, plan, or execution request.

## Results

The process-concurrency regression invokes the real
`openai_pause_binding_key` helper in two spawned processes against one shared
fake store. The first process holds the actual case-insensitive credential lock
while its fake backend is paused. The second process returns
`CREDENTIAL_SETUP_BUSY` before its backend can run. Once released, the first
process alone writes and returns a 32-byte decoded key. This verifies the
binding-key call holds the lock across its read, initialization, write, and
readback sequence; it does not claim protection from a non-cooperating same-UID
process or a keychain mutation outside that lock.

Additional negative tests show that a fake backend exception is converted to
the bounded `OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE` result without the private
exception text in the formatted traceback. A `model_copy()` profile containing
an arbitrary non-string credential reference fails as `PROFILE_INVALID` before
the backend is opened. A constructed legacy Google Ads secondary reference set
to the internal binding-key name fails configuration revalidation before
credential setup opens its backend.

No additional source defect was reproduced. The binding key remains a local
HMAC input only: it does not prove a provider identity, credential validity,
authorization, current state, or remote effect.

## Validation

```text
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_binding_key_independent_review.py \
  tests/test_openai_pause_binding_key_ultra_review.py \
  tests/test_credential_boundary.py tests/test_credential_ultra_review.py \
  tests/test_credential_setup.py tests/test_openai_account_key_profile_ultra_review.py
# 53 passed

PYTHONPATH=src:tests .venv/bin/ruff check tests/test_binding_key_independent_review.py
PYTHONPATH=src:tests .venv/bin/ruff format --check tests/test_binding_key_independent_review.py
PYTHONPATH=src .venv/bin/mypy src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_setup.py
# clean
```

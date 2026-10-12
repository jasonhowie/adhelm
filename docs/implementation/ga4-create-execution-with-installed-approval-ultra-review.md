# GA4 CREATE installed-execution ultra review

## Scope

This independent review covers the private GA4 CREATE installed-approval
sibling with synthetic bridge, OAuth, and HTTP fixtures. It does not launch a
helper or host, access a real credential store, authenticate a user, or call
GA4.

## Finding and repair

The initial post-bridge grant gate verified the approval kind, policy absence,
plan digest, and subplan tuple, but a model-copied grant with the same plan and
a changed grantor could still reach configured-adapter construction before
SafetyKernel compared its durable authorization digest. The repair calls
`SafetyKernel._ensure_durable_authorization` after the post-approval source
checks and before importing or constructing the configured adapter. Kernel
execution repeats that check under its coordinator lock.

The regression changes only `grantor_ref`, requires
`USER_APPROVAL_HOST_REQUIRED`, and makes the configured factory fail the test
if it is entered. It confirms zero OAuth or HTTP work after the malformed
result. The other independent checks prove expiry after a native claim blocks
OAuth/provider work and that a no-op configuration write can complete during
native review, so no configuration transaction spans the wait.

## Limits

The workflow still uses the existing guarded adapter and SafetyKernel for the
durable `APPLY_STARTED`, permit, provider read, one POST, readback, and
recovery behavior. This review establishes no live bridge, provider permission,
Keychain enrollment, native signing, or remote mutation evidence.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_ga4_create_execution_with_installed_approval.py \
  tests/test_ga4_create_execution_with_installed_approval_ultra_review.py
```

The focused sibling plus independent command passed 11 fake-only cases: eight
owner cases and three independent cases. The archive validator will rerun the
integrated native and Python groups.

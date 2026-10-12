# OpenAI account-key and current-read ultra review

Reviewed: 2026-09-05
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope

This review covered the OpenAI Ads account-key variant and the kernel's
one-use egress permit in `src/adhelm/safety.py` and
`src/adhelm/egress_permit.py`, plus the fixed current-campaign read in
`src/adhelm/providers/openai_ads.py`. It also covered their builder tests and
the independent adversarial regressions in
`tests/test_account_key_identity_ultra_review.py` and
`tests/test_openai_campaign_current_ultra_review.py`.

All checks used synthetic adapters, mock HTTP transports, fixture tokens, and
temporary local ledgers. They did not read a customer credential, contact an
external provider, or issue a provider mutation.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | A failed current campaign GET left the older cached campaign and its cached child groups usable. A later child request could then proceed from a parent whose current read had failed. | Campaign and dependent child cache entries are cleared before a refresh or current GET begins. Transport, schema, returned-resource, and account-binding failures now leave no usable old parent. |
| P1 | Public OpenAI read queries built with `model_copy()` or `model_construct()` could bypass page and record bounds. A malformed expected account ID was discovered only after a token and account request. | Every public query is revalidated through its strict JSON boundary before transport, and caller account/campaign IDs are checked before their request. The regressions assert no additional token or HTTP access. |
| P1 | The permit context remained active through response parsing, readback, and verification. A guarded adapter could retain an unconsumed permit in a copied context and claim it after dispatch returned. | The kernel still issues a permit only after its durable `APPLY_STARTED` append, but invalidates its shared state in a `finally` immediately around `apply_guarded`. Return and exception paths both deny the delayed claim before readback. |
| P2 | A prohibited raw value in an OpenAI response could escape from the raw-data validator as its underlying privacy diagnostic. | The response boundary normalizes that failure to a generic `PROVIDER_RESPONSE_INVALID` diagnostic, without copying the raw value into the error or safe inventory result. |

## Verified behavior

- The account-key identity is a closed OpenAI-only variant. It contains the
  planned account reference, stable account ID, selected profile, and private
  credential-binding HMAC reference, with `NOT_DOCUMENTED` scope status. It
  cannot impersonate the scoped principal/application/scope model. Exact
  profile, account, credential, and identity-kind mismatches fail at preflight,
  immediate reread, or reconciliation; existing scoped-plan digests remain
  pinned by regression coverage.
- Revalidation rejects constructed plan and adapter identity objects before
  adapter access. The guarded adapter requirement fails before a durable start
  when an account-key plan receives the legacy adapter interface.
- A permit is nonconstructible, nonserializable, bound to the composite,
  subplan, and attempt, and can be claimed once while the guarded dispatch is
  active. Copied contexts cannot claim it after dispatch returns or raises.
  A reviewed guarded adapter must consume it immediately at its fixed mutation
  dispatch.
- The current read uses only the fixed campaign GET after same-session identity
  discovery and complete bounded campaign discovery. It rejects an unlisted
  parent, mismatched returned account/resource, missing status, and string,
  float, or boolean native budget/timestamp values.
- The safe campaign model retains only the reviewed binding/status/budget and
  schedule subset. Response copy and description are omitted, and prohibited
  payload data fails before a safe model is returned. This library result still
  carries native client references for internal parent binding; it is not an
  agent-facing redacted projection.

## Retained limits

The permit is an in-process misuse boundary for reviewed adapter code. It does
not resist hostile code with the same Python-process or operating-system
privileges, grant a provider permission, establish authority, or prove an
actual HTTP dispatch used it. The future adapter must enforce permit consumption
at its fixed provider mutation call.

The campaign GET is read-only fixture evidence. It proves neither a current
execution identity/profile, a remote revision or conditional-write token,
provider eligibility, durable resource state, pause capability, delivery,
conversion behavior, authorization, recovery result, v1 completion, nor
release readiness.

## Validation

The initial scoped results below precede the additional permit-expiry guard.
That guard received a separate [pause ultra review](openai-pause-ultra-review.md),
with direct-grant, plan and delegated-policy expiry regressions. The development
validation record identifies the exact source snapshot covering both changes.

```text
.venv/bin/pytest -q \
  tests/test_account_key_identity.py \
  tests/test_account_key_identity_ultra_review.py \
  tests/test_egress_permit.py \
  tests/test_safety.py \
  tests/test_safety_input_ultra_review.py \
  tests/test_openai_campaign_current.py \
  tests/test_openai_campaign_current_ultra_review.py \
  tests/test_openai_ads.py \
  tests/test_openai_read_ultra_review.py
# 100 passed

.venv/bin/pytest -q \
  tests/test_openai_inventory_money.py \
  tests/test_openai_measurement_workflow.py \
  tests/test_openai_workflow_ultra_review.py
# 36 passed

.venv/bin/ruff check \
  src/adhelm/safety.py src/adhelm/egress_permit.py src/adhelm/providers/openai_ads.py \
  tests/test_account_key_identity.py tests/test_account_key_identity_ultra_review.py \
  tests/test_egress_permit.py tests/test_openai_campaign_current.py \
  tests/test_openai_campaign_current_ultra_review.py

.venv/bin/ruff format --check \
  src/adhelm/safety.py src/adhelm/egress_permit.py src/adhelm/providers/openai_ads.py \
  tests/test_account_key_identity.py tests/test_account_key_identity_ultra_review.py \
  tests/test_egress_permit.py tests/test_openai_campaign_current.py \
  tests/test_openai_campaign_current_ultra_review.py

.venv/bin/mypy src/adhelm/safety.py src/adhelm/egress_permit.py src/adhelm/providers/openai_ads.py
```

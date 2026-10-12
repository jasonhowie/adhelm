# OpenAI campaign pause ultra review

Reviewed: 2026-09-05
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope

This review covered the fixed OpenAI campaign-pause adapter in
`src/adhelm/providers/openai_pause.py`, its builder tests, and the independent
adversarial cases in `tests/test_openai_pause_ultra_review.py`. It also checked
the small shared permit-expiry change in `src/adhelm/safety.py` and
`src/adhelm/egress_permit.py` without modifying those owner-controlled files.

All checks used synthetic account/campaign fixtures, in-memory mock HTTP
transports, fixture credentials, and temporary local ledgers. No customer
credential or data was read, and no provider request or mutation was issued.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | An authorization or plan could expire during the adapter's required current reads after `APPLY_STARTED` and before its fixed POST. | The kernel now binds a trusted expiry check to the one-use permit and runs it under the consumption lock immediately before dispatch; expiry failure permanently invalidates that permit. The adapter retains a close-to-egress plan-clock check as a second guard. |
| P1 | Invalid campaign path components, inventory bounds, naïve expiry values, or an already expired plan were rejected only after an account read, and could surface as model-validation errors. | Pause planning now validates those public values against an injected local clock before any provider read. It returns bounded `INVALID_INPUT` or `PLAN_EXPIRED` diagnostics. |
| P2 | The public convenience wrapper accepted any object exposing `execute`, even though the supported boundary is `SafetyKernel.execute`. | The wrapper now requires `SafetyKernel`; fixed egress remains additionally protected by the exact kernel-issued permit. |

## Verified behavior

- The closed action rebuilds its full composite plan from the account-key HMAC
  binding and baseline. Rehashed plans with altered account/profile/key/API/
  capability/egress fields fail validation. A self-consistent rehash with a
  different campaign reference, bid, budget, schedule, account reference,
  profile, key binding, timezone, or currency still fails the current source
  read before a durable start or POST.
- Credential binding is a domain-separated HMAC over the selected local profile
  and in-memory key, using an adapter-private key. The raw credential and HMAC
  key do not enter a model, plan, ledger record, error, or evidence reference.
  A profile/key rotation between the guarded current read and permit consumption
  blocks the POST.
- Each relevant read is account-bound and follows the reviewed identity GET,
  complete bounded campaign list, and current campaign GET sequence. The final
  guarded read verifies account status, timezone, currency, account/resource
  identity, campaign status, bid, budget, and schedule immediately before the
  permit can be consumed. Rotating account metadata at that point prevents
  egress.
- The adapter has one fixed `POST /v1/campaigns/{campaign_id}/pause` route. It
  cannot use direct `apply`, a forged permit, a mismatched permit, or a reused
  permit. Redirects are not followed; lost transport, 5xx, malformed success,
  and cross-resource response cases issue no retry and do not become verified.
- Verification needs both a parsed response for the exact campaign and an
  independent matching current GET. A cross-resource response with a matching
  later read is not verified. A prohibited value in a response is converted to a
  generic provider-response failure before history.

  Updated 2026-09-08 (ticket S2, `docs/implementation/kernel-generalization.md`):
  that cross-resource case now reports `AMBIGUOUS_REMOTE_STATE` and CLI exit 6,
  where this review recorded `PARTIAL_EFFECT` and exit 5. The adapter is
  unchanged; the kernel's uniform write rule applies the attribution gap that
  was previously limited to Meta PAUSE and GA4 CREATE to every provider, so a
  rejected acknowledgement is never settled by this caller's own later read.
  Both statuses require recovery and neither permits a blind retry.
- Plan identifiers and field-change paths are structural/digest-derived. Native
  campaign IDs are not interpolated into field paths or ledger resource
  references. A pause has `max_spend` of zero in the account currency, which
  means zero incremental authorized spend only; it does not prove zero residual
  delivery or billing.
- Reconciliation deliberately reports an ambiguous attempt as unsettled even if
  a subsequent read is unchanged. Without a documented provider settlement or
  idempotency proof, it never claims settled no-effect.

## Retained limits

This is a local fixture-tested adapter, not an exposed execution command or
live pause capability. It has no trusted approval-host integration, live
provider conformance evidence, current provider eligibility proof, conditional
write/revision token, documented idempotency behavior, or settlement proof.
The adapter therefore cannot prove who changed a campaign, that a timeout had
no effect, that delivery/billing has stopped, or that a future pause is safe to
retry.

The permit is an in-process misuse boundary for reviewed code. It does not
defend against hostile Python code with the same process privileges, supply a
provider permission, or establish authorization by itself. The core kernel,
durable history, configured approval host, provider access configuration, and
live validation still require their own evidence.

## Validation

```text
.venv/bin/pytest -q \
  tests/test_openai_pause.py \
  tests/test_openai_pause_ultra_review.py \
  tests/test_egress_permit.py \
  tests/test_account_key_identity.py \
  tests/test_openai_campaign_current.py \
  tests/test_openai_campaign_current_ultra_review.py \
  tests/test_safety.py \
  tests/test_safety_input_ultra_review.py
# 119 passed

.venv/bin/ruff check \
  src/adhelm/providers/openai_pause.py \
  tests/test_openai_pause.py \
  tests/test_openai_pause_ultra_review.py

.venv/bin/ruff format --check \
  src/adhelm/providers/openai_pause.py \
  tests/test_openai_pause.py \
  tests/test_openai_pause_ultra_review.py

.venv/bin/mypy src/adhelm/providers/openai_pause.py
```

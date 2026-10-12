# OpenAI conversion-setting reader ultra review

Reviewed: 2026-09-04

## Scope and source check

This review covered the fixed read client in
`src/adhelm/providers/openai_conversions.py`, its fixture tests, shared transport
boundary, and the local research note. It checked the current official OpenAI
Ads [Conversion Setup reference](https://developers.openai.com/ads/api-reference/conversion-setup)
and [Supported Events reference](https://developers.openai.com/ads/supported-events).
Those sources document `GET /conversions/event_settings`, the `limit`, `after`,
`before`, and `order` controls, the create-response setting fields, and the
standard-event vocabulary. They do not publish a list-response example, so the
repository's envelope convention remains fixture-only and must not be promoted
to live schema evidence without an authorized feasibility read.

No provider HTTP, credentials, customer account, conversion event, pixel, or
Conversions API key was accessed.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | `model_copy()` or `model_construct()` could bypass the strict query model and send a page limit above the documented maximum or an arbitrary order value. | The public method now reparses the query before identity verification, token retrieval, or HTTP. The regression proves no token or request occurs. |
| P1 | An empty list missing `first_id` and `last_id` passed the parser, so a malformed response could be reported as a verified absence of conversion settings. | The parser now requires the exact closed list-envelope field set for empty and nonempty pages. |
| P2 | An injected transport could buffer more than the adapter's declared 8 MiB response limit before the adapter's JSON parser rejected it. | Construction now rejects a transport whose response bound is not a positive integer at or below 8 MiB. |

## Verified boundary

- The client sends only the fixed current-account `GET` route and forward
  `after` pagination. It does not call recent-event, pixel, API-key, upload, or
  mutation routes.
- A query account must match a canonical reference regenerated from the native
  account ID, and every response account ID must match the supplied discovered
  identity. A mandatory identity-session verifier runs before token retrieval.
- The reader accepts only the documented standard event names, one source,
  a 30-day attribution window, and the bounded archive/version fields. It
  discards setting/source display names, source IDs, and raw campaign
  associations; it does not claim source health, browser firing, native event
  receipt, processed reporting, or optimization association.
- Sensitive or unknown response data fails closed and is not echoed in a
  diagnostic, returned model, or persistence path.

## Remaining limits

This is an isolated fixture-tested reader with no CLI, workflow, ledger, audit,
or compiler wiring. The verifier must be composed from one current identity
discovery session and read profile; a callback which merely compares strings
does not prove that requirement. It remains insufficient to claim a configured
setting is an active conversion signal or to enable conversion-optimized
campaign creation.

## Validation

```text
.venv/bin/pytest -q tests/test_openai_conversions.py tests/test_openai_conversions_ultra_review.py
# 33 passed
.venv/bin/ruff format --check src/adhelm/providers/openai_conversions.py tests/test_openai_conversions_ultra_review.py
.venv/bin/ruff check src/adhelm/providers/openai_conversions.py tests/test_openai_conversions_ultra_review.py
.venv/bin/mypy src/adhelm/providers/openai_conversions.py tests/test_openai_conversions_ultra_review.py
# clean
```

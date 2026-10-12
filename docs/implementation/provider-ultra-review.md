# Provider read and inventory review

Reviewed 2026-09-04 against the Google Ads and Meta Ads read contracts, the
reporting schema, the lifecycle contract, and the local-ledger contract. This
review used fixture transports and local ledger files only. It is implementation
evidence, not live-provider or release evidence.

## Findings

| Severity | Finding | Executable reproduction | Status |
| --- | --- | --- | --- |
| P1 | Google Ads and Meta Ads report inputs accepted arbitrary historical windows, contrary to the bounded-query contract. | Provider query models now reject a window wider than 367 inclusive days before transport. | Remediated; provider adapter tests cover both models. |
| P1 | Meta sent its fixed `page_size` to Graph even when the caller set a smaller maximum result count, which could overfetch raw rows before local rejection. | A maximum of one is sent as `limit=1`; a continuation at the exact maximum raises `RESULT_LIMIT_REQUIRED` without another request. | Remediated; Meta adapter regression covers request parameters and continuation behavior. |
| P1 | Monetary inventory state could contain a minor-unit amount without its ISO currency, leaving retained history uninterpretable after the response is discarded. | `test_currencyless_minor_snapshot_cannot_enter_canonical_history` attempts a direct malformed ledger append. | Remediated; schema validation returns `INVALID_LEDGER_PAYLOAD` before a ledger segment is created. |
| P1 | Sequential inventory reads could describe an orphaned child as complete inventory. | `test_orphan_meta_hierarchy_fails_before_any_snapshot_is_persisted` returns a campaign and an ad set pointing at another campaign. | Remediated; cross-resource hierarchy is checked after reads and before the first snapshot append. |
| P1 | Meta lifetime-only budgets were parsed but were previously at risk of being omitted; simultaneous daily and lifetime values had no truthful retained representation. | `test_meta_lifetime_budget_is_retained_as_currency_bound_state` checks lifetime-only values and simultaneous daily/lifetime values, including no invented effective budget. | Remediated; single budgets retain `budget_minor` plus type, while simultaneous values retain separate typed fields and currency. |
| P1 | A performance envelope could carry another provider's extension namespace, and conversion rows accepted unreviewed generic extensions. | `test_cross_provider_extensions_and_unreviewed_conversion_extensions_are_rejected` supplies a GA4 key to a Meta report and to a conversion row. | Remediated; report extensions are provider-prefix closed and conversion extensions are currently closed. |
| P2 | The central Google cost provenance guard rejected legal positive signed-64-bit micros, even though Google money values use that native range. | `test_google_native_cost_retains_signed_int64_precision_and_rejects_overflow` uses `9223372036854775807` and then the next integer. | Remediated; legal signed-64-bit input is retained exactly and overflow fails before model construction. |

## Validation

The focused local suite passed:

```text
uv run pytest -q tests/test_google_ads.py tests/test_meta_ads.py tests/test_inventory.py tests/test_reporting.py tests/test_provider_ultra_review.py
27 passed
```

`uv run ruff check tests/test_provider_ultra_review.py` and
`uv run ruff format --check tests/test_provider_ultra_review.py` also passed.

No live API calls, credentials, raw provider payloads, or customer data were
used. No remaining P0 or P1 issue was found in this bounded provider-read,
inventory, money, and provenance slice. This does not establish provider live
feasibility, overall release completeness, or the broader definition of done.

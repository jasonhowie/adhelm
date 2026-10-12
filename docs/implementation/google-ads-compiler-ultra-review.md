# Google Ads compiler ultra review

Reviewed: 2026-09-04  
Status: **IMPLEMENTING / UNVERIFIED**

## Scope and evidence

This review covered the offline v25 Search/RSA compiler in
`src/adhelm/providers/google_ads_compiler.py`, its fixture tests, the adversarial
regressions in `tests/test_google_ads_compiler_ultra_review.py`, and
`docs/research/google-ads-compiler-contract.md`. It used current primary
Google Ads v25 references for [Campaign](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign),
[CampaignBudget](https://developers.google.com/google-ads/api/reference/rpc/v25/CampaignBudget),
[TargetSpend](https://developers.google.com/google-ads/api/reference/rpc/v25/TargetSpend),
and [AdScheduleInfo](https://developers.google.com/google-ads/api/reference/rpc/v25/AdScheduleInfo).

No Google Ads SDK was installed. No provider HTTP, customer account,
credential, native identifier, upload, mutate, preview, or activation was
used.

## Findings and remediation

| Severity | Finding | Regression and disposition |
| --- | --- | --- |
| P1 | The candidate called non-v25 `maximize_clicks`, `start_date`, and `end_date` fields. v25's Campaign oneof uses `target_spend`, and its formal schedule fields are `start_date_time` and `end_date_time`. | The compiler now emits an empty `target_spend`, rejects its deprecated spend target/bid ceiling through the closed model, and emits `yyyy-MM-dd HH:mm:ss` boundaries at `00:00:00` / `23:59:59`. `test_emits_v25_campaign_date_target_spend_and_schedule_minute_enums` and `test_rejects_obsolete_or_extra_v25_campaign_bidding_and_date_fields` prevent regression. |
| P1 | The declared native `AdScheduleInfo` carried numeric minutes, but v25 uses `MinuteOfHour` enum symbols. | Numeric quarter-hour input is converted to `ZERO`, `FIFTEEN`, `THIRTY`, or `FORTY_FIVE` before the native candidate is created. The native model rejects an integer bypass. |
| P1 | Seven schedules could target one day although v25 permits six schedules per day. | Input and revalidated compiled output both enforce the six-per-day limit, while retaining the narrower local 21-schedule maximum. `test_revalidates_copied_names_schedule_limits_and_native_schedule_symbol_types` covers a copied seven-Monday input. |
| P1 | Revalidating a serialized candidate did not prove budget/child local bindings, the total spend cap, exact RSA text/CTA/destination identity, or destination-policy compliance. | The result now retains the declared destination policy and local ad label and checks the entire handle graph, fixed criterion bounds, native total micros, controlled URL, and recomputed RSA content digest. `test_output_revalidation_rejects_forged_hierarchy_creative_and_destination` and `test_output_revalidation_preserves_total_budget_and_local_ad_label` cover forged parents, text, destination, and spend. |
| P2 | Campaign/ad-group names permitted characters explicitly rejected by Google Ads, and native nested text/keyword values could be bypassed through copied models. | Names are nonblank, 255-byte bounded, and reject NUL/LF/CR. Native nested RSA/keyword models repeat their bounded validation so reparse does not trust `model_copy`. |
| P2 | The candidate could imply exact geographic reach or unchanged serving text even though it does not set/read geo target type, asset automation, or AI Max settings. | The planning boundary now explicitly requires geo-target-type plus asset-automation/AI-Max readback. The contract says location criteria and RSA text are not live-serving proof. |

## Retained boundaries

The corrected candidate is still only a local, paused, non-executable graph.
Local handles are not resource names or temporary IDs. A future guarded
executor must bind verified v25 create results before it can mutate; it must
also inspect identity, currency/timezone, campaign-total-budget eligibility,
geo mode, conversion/account-default goals, automation/AI Max, policy,
destination/tracking, drift, and authorization.

The formal v25 Campaign reference uses hyphenated date-time values, while one
official RSA sample presents compact date values. The source follows the
formal reference but does not claim live-wire proof. Fixture validation also
does not prove provider eligibility, policy approval, ID binding, creation
order, idempotency, recovery, delivery, conversion behavior, or release
completion.

## Validation

```text
uv run pytest tests/test_google_ads_compiler.py tests/test_google_ads_compiler_ultra_review.py -q
# 14 passed

uv run ruff check src/adhelm/providers/google_ads_compiler.py tests/test_google_ads_compiler.py tests/test_google_ads_compiler_ultra_review.py
uv run ruff format --check src/adhelm/providers/google_ads_compiler.py tests/test_google_ads_compiler.py tests/test_google_ads_compiler_ultra_review.py
uv run mypy src/adhelm/providers/google_ads_compiler.py
# clean
```

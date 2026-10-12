# OpenAI Ads read-path ultra review

Reviewed: 2026-09-04

## Scope and evidence

This review covered `src/adhelm/providers/openai_ads.py`, the OpenAI inventory
normalization in `src/adhelm/inventory.py`, and the OpenAI snapshot rules in
`src/adhelm/ledger.py`. It used the local [hierarchy read contract](../research/openai-hierarchy-read-contract.md)
and [write contract](../research/openai-write-contract.md), plus the current
public [Campaigns](https://developers.openai.com/ads/api-reference/campaigns),
[Ad Groups](https://developers.openai.com/ads/api-reference/ad-groups),
[Ads](https://developers.openai.com/ads/api-reference/ads), and
[Insights](https://developers.openai.com/ads/api-reference/insights)
documentation. No live provider request, credential, customer record, or raw
customer response was used.

## Findings and remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| P1 | A caller could construct a canonical-looking campaign or ad-group model and use it to issue a child request without a preceding discovery read. | The client now binds identities, campaigns, and ad groups to its completed bounded discovery session. `test_synthetic_canonical_parents_cannot_trigger_child_http` proves that synthetic parents fail before child HTTP. |
| P1 | The list parser accepted the plural `impressions` billing value and arbitrary creative types, including the fixture's undocumented `image` type. The documented billing values are singular `impression` and `click`; the reviewed native read shape is `chat_card`. | The parser enforces the documented billing relation to the discovered campaign and accepts only reviewed `chat_card` creative objects. `test_hierarchy_rejects_invalid_billing_event_before_ad_egress` and `test_unreviewed_creative_shape_fails_closed` cover the failures. |
| P1 | Generic resource snapshots could retain OpenAI native-micros fields with a forged provider namespace, absent parent, mismatched currency, unrelated account reference, or an AD campaign that disagreed with its ad group's campaign. A self-consistent rewritten retained record could evade append-only field validation alone. | OpenAI snapshots now require their native account/campaign/ad-group/ad namespaces, parent order, exact campaign-to-ad-group linkage, account currency, exact account binding, and resource-specific micros fields. Append and retained scans validate the same tree. `test_openai_snapshot_checks_namespace_currency_and_retained_tree` and `test_openai_ad_parent_chain_is_consistent_in_inventory_and_retained_ledger` prove these checks after digest-recomputed tampering. |
| P2 | Repeated native identifiers could be returned across pages or hierarchy collections. | Each list rejects repeated IDs; the hierarchy model also rejects duplicates and cross-account parent bindings. `test_typed_hierarchy_rejects_cross_account_parent_binding` covers the model boundary. |

## Money and reporting boundaries

Campaign lifetime budget and ad-group maximum bid micros are accepted only as
positive JSON integers at or below signed `int64`, then retained as decimal
strings. The public typed models enforce the same local bound. This preserves
values above JavaScript's exact binary-float range.
Shared minor-unit fields are emitted only for exact USD values divisible by
10,000; a subminor amount is retained as native micros and never rounded down.
Non-USD native micros remain native-only. OpenAI report spend is currently
USD-only and rejects another account currency before insights egress.

The fixed AD report is native AD grain. It retains only date, campaign/ad
references, impressions, clicks, and exact USD spend. It deliberately leaves
the ad-group parent and content-addressed creative version unset, with
`CREATIVE_NOT_EXPOSED` and `NOT_AVAILABLE_FROM_PROVIDER`; a separate inventory
read cannot prove either relation for a report row.

## Retained limits

The adapter remains fixture-verified, read-only code. It has no live account
eligibility evidence, OAuth/app-review evidence, delivery-latency proof,
pagination-variant proof, or production response-compatibility evidence. It
does not upload assets, create or update entities, pause or activate ads,
publish, submit conversions, or make a spend-capable request.

Lists are bounded to ten pages and a caller-selected resource or row limit.
The campaign, ad-group, and ad hierarchy is read sequentially, so it is not a
provider-atomic point-in-time snapshot. If a parent disappears or a child
relationship changes during collection, hierarchy closure rejects the local
inventory rather than persisting a mixed tree. Session-bound parent handles
cannot be reconstructed across a process boundary; a fresh identity and
bounded parent discovery are required.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_openai_ads.py tests/test_openai_inventory_money.py \
  tests/test_openai_read_ultra_review.py tests/test_inventory.py tests/test_ledger.py
# 46 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  src/adhelm/providers/openai_ads.py src/adhelm/inventory.py src/adhelm/ledger.py \
  tests/test_openai_ads.py tests/test_openai_inventory_money.py \
  tests/test_openai_read_ultra_review.py tests/test_inventory.py

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy \
  src/adhelm/providers/openai_ads.py src/adhelm/inventory.py src/adhelm/ledger.py
# clean
```

# Google Ads v25 read wire: independent review

This review covers the fixed generic Google Ads v25 read wire in
`src/adhelm/providers/google_ads.py` and the independent fake-only cases in
`tests/test_google_ads_read_wire_ultra_review.py`. It used `httpx.MockTransport`
and synthetic tokens only. It did not call Google, open a credential store,
write a ledger, create a provider mutation, or make any live-capability claim.

Google documents `searchSettings.returnTotalResultsCount` as a nested v25
Search request field, and documents `queryResourceConsumption` plus
`metricAttributes` in the Search response. Google also documents that GAQL
returns unselected resource names by default unless a query uses
`PARAMETERS omit_unselected_resource_names = true`.
[Search](https://developers.google.com/google-ads/api/reference/rpc/v25/GoogleAdsService/Search?transport=rest)
[GAQL structure](https://developers.google.com/google-ads/api/docs/query/structure)
[MetricAttributes](https://developers.google.com/google-ads/api/reference/rpc/v25/MetricAttributes)

The independent cases verify all six fixed generic queries use the nested
setting and finish with the resource-name omission parameter. They accept
valid bounded response metadata, including omitted protobuf-default attribute
members, and prove that metadata is discarded before a normalized report is
returned. They reject non-string, noncanonical, and overflowing resource
consumption values; malformed metadata objects; and values over the local
metric or nested-attribute bounds. A synthetic implicit `resourceName` is also
rejected under the fixed query contract, rather than becoming a new permissive
row schema.

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_google_ads.py \
  tests/test_google_ads_read_wire_ultra_review.py
# 17 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  tests/test_google_ads_read_wire_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check \
  tests/test_google_ads_read_wire_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy \
  tests/test_google_ads_read_wire_ultra_review.py
# clean
```

The review proves only bounded local request construction and response handling
against the published schema. It does not prove provider field compatibility,
OAuth or developer-token access, pagination behavior, quota behavior, or any
Google Ads mutation or approval capability.

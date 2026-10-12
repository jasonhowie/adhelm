# TikTok and LinkedIn fixed-read review

Reviewed on 2026-09-04 against the fixed-read adapters, source contracts, and
fixture suites. This review made no live provider call, used no customer
credential, and inspected no customer data.

## Findings and remediation

| Severity | Finding | Result |
| --- | --- | --- |
| P1 | TikTok daily reports allowed 367 days even though the documented synchronous daily-report limit is 30 inclusive days. | The query model now rejects a 31-day window before egress. |
| P1 | TikTok AD reporting sent three ID dimensions, although the documented basic report permits one ID dimension with the time dimension. | AD requests now use only `stat_time_day` and `ad_id`; they do not invent a campaign or ad-group join. |
| P1 | TikTok's documented default filters omit deleted resources, while the report could be labeled complete. | Fixed campaign/ad `STATUS_ALL` filters are serialized using the documented nested JSON filter form. |
| P1 | TikTok inventory money fields are documented JSON numbers, while binary-float parsing would lose decimal precision. | Response floats are parsed as `Decimal`; the conversion accepts only exact minor units. |
| P1 | TikTok page counters could describe a partial page as a complete result. | The client now binds returned page, page size, total, total pages, row count, and duplicate resource/day rows. |
| P2 | A Boolean response `code` compared equal to numeric zero in Python. | Envelope success requires `type(code) is int` and `code == 0`. |
| P2 | `date.fromisoformat()` accepts ISO week dates such as `2026-W01-1`, despite the adapter's date-only response contract. | The parser now requires the raw token to equal the normalized `YYYY-MM-DD` calendar date before accepting it. |
| P1 | LinkedIn Ad Analytics was sent undocumented paging controls and could claim completeness from incomplete metadata. | The adapter sends no `start` or `count`, treats the finder as non-paginated, and records `NOT_AVAILABLE_FROM_PROVIDER` rather than `COMPLETE`. |
| P1 | LinkedIn creative finder requests exceeded its documented 100-item page cap, omitted the required finder header, and expected a bare numeric creative ID. | Creative requests cap `pageSize` at 100, send `X-RestLi-Method: FINDER`, and require a `urn:li:sponsoredCreative:` ID. |
| P1 | LinkedIn inventory could accept a response page larger than the requested bounded page size. | The client rejects that response before returning normalized resources. |

TikTok evidence: [synchronous reporting](https://business-api.tiktok.com/portal/docs/run-a-synchronous-report/v1.3), [report filters](https://business-api.tiktok.com/gateway/docs/index?doc_id=1751625296192514), and [ad-group fields](https://business-api.tiktok.com/gateway/docs/index?doc_id=1739499616346114). LinkedIn evidence: [Ad Analytics reporting](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-01) and [creatives](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-creatives?view=li-lms-2026-06).

## Validation

`uv run pytest -q tests/test_tiktok_linkedin_ultra_review.py` passed: 9 tests.
The broader focused run, `uv run pytest -q tests/test_tiktok_ads.py tests/test_linkedin_ads.py tests/test_tiktok_linkedin_ultra_review.py`, passed: 18 tests.

The checks cover request path and parameters, account binding, UTC/report-date behavior, exact money parsing, result bounds, page consistency, closed response fields, and redacted AD-grain semantics. Ruff was also run on the review test.

## Evidence boundary

The reviewed TikTok material does not establish the exact lexical form of the
returned `stat_time_day` value. The implementation therefore accepts only the
canonical calendar-date form as a fail-closed assumption; it must not be
described as live provider verification. The LinkedIn source contract likewise
retains live application/access, complete hierarchy-inventory, non-USD
precision, and provider freshness/attribution evidence as unresolved release
requirements. Neither adapter is a claim of provider-live or release-ready
evidence.

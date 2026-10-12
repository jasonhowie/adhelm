# Reddit Ads fixed-read review

Reviewed on 2026-09-04 against the current Reddit Ads v3 fixed-read adapter,
its inventory integration, the local source contract, the official Reddit Ads
reference, and Reddit's official Postman workspace. This review made no live
provider call, used no customer credential, and inspected no customer data.

## Findings and remediation

| Severity | Finding | Result |
| --- | --- | --- |
| P1 | The original report request used the undocumented `day` breakdown and four AD identifiers. Reddit documents at most three breakdowns and explicitly recommends `ad_id` with `date`, without campaign/ad-group IDs. | Campaign requests now use `campaign_id,date`; AD requests use `ad_id,date`, emit no invented parent association, and retain the native AD grain. |
| P1 | The original parser expected a root list plus mandatory pagination. Official examples return `data.metrics`, `data.metrics_updated_at`, and may include `hour` on a daily result. | The parser now accepts that closed documented envelope, allows and validates UTC daily `hour`, drops it from the normalized row, and does not claim a complete/fresh report. |
| P1 | The original list continuation decoded and reconstructed provider `next_url` query parameters, despite Reddit requiring the URL be followed directly. This could change an opaque cursor or reject repeated keys. | The adapter validates fixed HTTPS origin/path/port and preserves original ASCII query bytes through a GET-only `opaque_read_query` transport path. The path rejects mutation, body, normal parameters, redirects, and malformed cursors. |
| P1 | The official adaptive-time guide shows a provider continuation at the fixed API root, `https://ads-api.reddit.com/api/v3?page.token=nextToken`; rebuilding every cursor under the original collection route rejected that documented shape. | The adapter now permits only the original collection path or the documented fixed API-root path, retains the returned path and byte-exact opaque query, and still pins HTTPS host and port. |
| P1 | A caller could construct a typed identity with a syntactically valid but different account ID, or a path-like `SafeRef`, then label the result as a claimed account. | Each public operation now validates the raw provider ID and requires the identity reference to equal this client's `reference_for("account", raw_id)` before egress. |
| P2 | A fixed 731-day query-model limit confused 24-month delivery retention with a documented request-duration limit. | The model no longer invents a duration limit. Execution enforces a calendar-based 24-month retention cutoff relative to the observed connector date. |
| P1 | The initial report body/fixtures did not establish how a `POST` report continuation is fetched, even though Reddit says reports paginate. | A returned report continuation now fails explicitly with `REPORT_PAGINATION_UNVERIFIED`; it is never silently treated as a complete result or followed by an invented request. |

Primary evidence: [Reddit Ads API v3 overview](https://ads-api.reddit.com/docs/v3/) documents that all list responses, including reports, paginate and that provider pagination URLs must be followed directly. [Get A Report](https://ads-api.reddit.com/docs/v3/api/get-a-report) documents UTC default reporting, three-breakdown maximum, native micro-unit spend, 24-month delivery retention, stabilization delay, and the `ad_id` recommendation. The official [adaptive-time guide](https://ads-api.reddit.com/docs/v3/guides/programs/reporting/add-adaptive-time) shows the API-root pagination URL shape. The official [Reddit Postman workspace reporting examples](https://www.postman.com/reddit-ads-api/reddit-ads-api-v3/documentation/nee6egu/use-cases) establish lowercase `date`/`ad_id` request fields and the nested `data.metrics` response shape.

## Validation

`uv run pytest -q tests/test_reddit_ultra_review.py` passed: 11 tests.

The review fixtures verify the documented campaign and AD report shapes, no
fabricated AD parents, nested report metrics, explicit continuation failure,
signed-int64 micro-spend handling, calendar retention separation, byte-exact
opaque cursor preservation with repeated keys and the documented API-root
cursor route, rejection of cursor use in a mutation context, and typed-identity
pre-egress binding. Ruff was run on the review test.

## Remaining evidence boundary

The official overview establishes that reports paginate but the reviewed public
material does not establish the method and full response contract for fetching a
continuation from an initial `POST` report. Multi-page report completion is
therefore unavailable by design: the adapter returns an explicit error rather
than a partial result. The adapter remains fixture-tested only. Live OAuth,
response-schema, report-boundary, pagination-continuation, freshness, and
attribution evidence are still required before any provider-verified or
release-ready claim.

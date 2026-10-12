# Google Ads EDIT observation ultra review

Reviewed on 2026-09-06 against the private M54 profile, credential-lifecycle,
session, and fixed v25 state-reader boundary. All exercises used fake local
credential stores, temporary configurations, and `httpx.MockTransport`; they
made no browser, credential-store, helper, or Google Ads request.

The reviewed boundary accepts only a Google Ads `EDIT` OAuth profile with the
same exact `adwords` scope used for local `READ`, while retaining purpose as a
local separation. Raw draft and profile inputs must explicitly carry a
non-null currency and IANA timezone for `EDIT`; existing `READ` defaults stay
unchanged. The dual-slot lifecycle binds the OAuth bundle and separate
developer-token slot to the selected profile without using generic
`credentials.credential()`.

The reader issues only the fixed v25 `googleAds:search` POST for the configured
10-digit customer. The independent cases require the exact nested
`searchSettings.returnTotalResultsCount` body, selected `resourceName` values,
positive canonical signed-64-bit campaign IDs, and one bounded result. They
also verify that `CLOSED` remains an observed customer status, that unknown
campaign statuses fail closed, and that a callback cannot add ambient cookie
state before the request.

The session cases prove that configuration drift, primary OAuth-bundle
rotation, developer-token rotation, expiry, and malformed constructed session
state stop a later campaign POST. Diagnostics remained fixed and omitted both
old and replacement fake credentials. The session releases configuration and
credential locks before resource HTTP; its private per-session `RLock`
intentionally serializes reads and close operations.

Two issues were repaired during review. The initial request placed
`returnTotalResultsCount` at the top level and accepted an absent selected
resource name. The reader now uses nested `searchSettings` and requires exact
derived customer and campaign resource names. The implementation documentation
also now distinguishes released configuration/credential locks from the
private session serialization lock.

Validation passed:

- `tests/test_google_ads_edit_observation_ultra_review.py`: 24 cases.
- The owner reader/session, profile, OAuth lifecycle, and existing Google Ads
  group: 187 passed.
- Strict mypy, scoped Ruff, and format checks passed for the independent test.

No open source finding remains in this observation-only slice. These results
do not establish account access, mutation permission, a write acknowledgement,
CAS or idempotency, delivery, spend, native approval, or execution authority.

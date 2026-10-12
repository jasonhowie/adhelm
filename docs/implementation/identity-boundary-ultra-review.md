# Typed provider-identity boundary review

Reviewed on 2026-09-04 using synthetic `httpx.MockTransport` fixtures only.
No provider call, customer credential, or customer data was used.

## Findings and remediation

| Severity | Finding | Result |
| --- | --- | --- |
| P1 | Meta and LinkedIn identity client IDs were `SafeRef` values that permit path separators. A directly constructed identity could escape the account-scoped route before response validation. | Each adapter validates the native account ID before egress, derives the route from that validated value, and requires the identity reference to match the client's `reference_for` function. |
| P1 | TikTok advertiser IDs were sent as query parameters after only matching an opaque claimed reference. A forged ordinary numeric ID could read a different advertiser while retaining the claimed provenance. | The adapter now validates the advertiser ID and requires the client-derived canonical reference before every inventory or report read. |
| P1 | GA4 validated `property_id` against the identity but allowed an arbitrary `query.account_ref`, so a result could be labeled with false property provenance. | Standard and realtime reads now require the canonical property reference. The workflow first verifies the user input against the configured raw property ID, then remaps the internal query to the discovered property reference. |
| P1 | Reddit had the same valid-but-different direct-identity provenance gap after its path traversal guard. | The shared regression suite verifies the Reddit raw-ID/reference binding, including a valid custom `reference_for` callback. |

The bounded check found no analogous route-selection bypass in Google Ads: its
customer route uses a strict ten-digit `GoogleAdsReadConfig` ID and is matched
against the discovered identity. OpenAI Ads uses fixed account-independent read
paths in the reviewed adapter.

## Validation

`uv run pytest -q tests/test_identity_boundary_ultra_review.py
tests/test_reddit_ultra_review.py` passed: 27 tests. The fixtures cover
malformed path-like IDs, syntactically valid but mismatched IDs, inventory and
report entry points, GA4 standard/realtime provenance, custom reference
derivation, and the Reddit wire-boundary regressions. The broader focused
provider/GA4/Reddit run passed 55 tests; Ruff and mypy were clean for these
changed files:

- `src/adhelm/providers/meta_ads.py`
- `src/adhelm/providers/tiktok_ads.py`
- `src/adhelm/providers/linkedin_ads.py`
- `src/adhelm/providers/ga4.py`
- `src/adhelm/workflows.py`
- `tests/test_ga4.py`
- `tests/test_identity_boundary_ultra_review.py`

This is a boundary-hardening review, not live provider evidence or a release
readiness claim.

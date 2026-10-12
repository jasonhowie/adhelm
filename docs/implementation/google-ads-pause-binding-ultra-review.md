# Google Ads PAUSE binding ultra review

This independent M56 review exercised the private Google Ads PAUSE planning
binding, facade, and retained-plan workflow with temporary configurations,
fake protected stores, a local ledger, and `httpx.MockTransport`. No real
provider or operating-system credential-store access, configured native host or
helper use, or configured wheel execution occurred.

Preparation is the only lifecycle path allowed to refresh OAuth data or create
and read back the dedicated PAUSE binding key. It takes the configuration lock
before the three case-insensitively ordered protected-slot locks. The issued
private snapshot binds the complete protected OAuth bundle, current bearer and
finite expiry, developer token, fixed key, and exact configuration, profile,
customer, manager, client, principal, and scope facts through a domain-separated
HMAC. The detached execution binding retains only the nonsecret facts and its
credential-binding digest; it has no OAuth token, developer token, slot name,
or protected-key material.

Every subsequent resource-read and retention check is no-refresh. The review
rotated the bearer and refresh material, developer token, fixed binding key,
and manager/configuration after the first identity read. Each later campaign
read failed before its HTTP request, used a fixed diagnostic, and did not
expose either old or replacement test material. A near-expiry initial bundle
refreshed once during planning; the fresh two-read observation did not refresh
again. A second no-refresh clock sample that reaches bearer expiry is
credential drift, while only a backwards sample is a clock error. Requested
plan expiry beyond the finite token window is rejected rather than shortened.

The retention callback now re-reads the configuration and all three protected
slots through the already-held transaction and captured store handles. It
rejects bundle, developer-token, binding-key, and configuration drift that
appears after context entry, as well as cross-thread and escaped callbacks. It
does not acquire a new backend, re-enter a credential lock, refresh, write,
open a provider route, or read the ledger. Resource reads fail while that
context is active; configuration and credential locks are otherwise released
before each fixed resource POST. The private session lock only serializes
session operations.

Three source-bound findings were repaired before this review froze:

- A callback initially checked only its captured snapshot. It now re-reads the
  held configuration and slots, so manual drift during the append window is
  detected without lock re-entry.
- `observe_campaign` initially validated the campaign identifier after the
  identity request. It now rejects malformed, noncanonical, Boolean, and
  out-of-range identifiers before header, protected-store, or resource work.
- Facade binding validators canonicalized copied model data before strict JSON
  validation. Raw numeric aliases such as `schema_version: 1.0` now fail before
  HTTP instead of being normalized to the valid integer representation.

The final fake-only group was:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m56-uv-cache uv run pytest -q tests/test_google_ads_pause_credentials.py tests/test_google_ads_pause_planning_session.py tests/test_google_ads_pause_workflow.py tests/test_google_ads_pause_workflow_integration.py tests/test_google_ads_pause_reserved_slots.py tests/test_google_ads_pause_binding_ultra_review.py
```

It passed 77 cases after the final timing regression. The independent suite
contains 19 cases; its Ruff format/check and strict mypy checks pass.

This evidence establishes a closed local planning and retention boundary only.
It does not establish provider authorization, a Google Ads mutation request or
acknowledgement, conditional-write or idempotency semantics, delivery or spend
outcomes, native approval, or execution authority.

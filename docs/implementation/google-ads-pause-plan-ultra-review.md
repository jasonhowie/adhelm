# Google Ads PAUSE plan ultra review

This review covered the M55 pure Google Ads v25 campaign PAUSE contract and
its retained-plan history and redacted historical-review boundaries.  It used
only typed fixtures, temporary local ledgers, and local projections.  No
configuration, credentials, Google Ads customer API, native approval route, or
provider mutation was used.

The reviewed contract is deliberately limited to one non-authorizing
configuration candidate, `ENABLED -> PAUSED`.  Its action ID and reconstructed
one-subplan `CompositePlan` commit the workspace, closed customer baseline,
declared EDIT OAuth/developer-token association, optional manager customer,
campaign observation, postcondition, expiry, and all fixed safety controls.
Changing a local execution-binding fact changes the action and plan digests
without changing the remote `StateRef` fingerprints.  Changing a remote
customer fact changes those remote fingerprints.  Both state references retain
`revision=None`.

The independent cases reject raw numeric and Boolean aliases, whitespace that
would otherwise normalize, redundant UTC spellings, invented state revisions,
and altered scopes, provider, egress, or zero-spend fields.  They cover the
five-minute observation limit, equality and strict-expiry boundaries,
cross-customer observations, malformed constructed actions, workspace mismatch
before append, and a final retention callback failure without a new plan record
or checkpoint change.

One review finding was repaired before freeze.  A raw retained record could
spell its duplicated top-level expiry as `.000000Z`; datetime normalization
made it equal to the canonical action expiry.  The record validator now accepts
only an aware typed instant or canonical `Z` text before comparing the action,
so the independently reproduced alias is rejected as
`INVALID_GOOGLE_ADS_PAUSE_PLAN_RECORD`.

Historical review and export tests confirm that workspace, customer, campaign,
profile, client, principal, credential-binding, configuration, and scope values
do not leave the boundary.  The review preserves only HMAC-redacted references
alongside allowlisted nonsecret customer state, `ENABLED` baseline, `PAUSED`
configuration-only postcondition, `NOT_PREDICTED` delivery, zero incremental
spend, and no-authority controls.  The output preflight uses a probe key before
the caller sharing key.

The final fake-only focused group was:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m55-uv-cache uv run pytest -q \
  tests/test_google_ads_pause_contract.py \
  tests/test_google_ads_pause_plan_history.py \
  tests/test_google_ads_pause_plan_views.py \
  tests/test_google_ads_pause_plan_ultra_review.py
```

It passed 99 cases after the expiry repair.  The independent suite contains 21 cases.
Ruff format/check and strict mypy pass for the independent test file.

This evidence establishes only a closed local plan and historical-review
boundary.  It does not establish current provider access, a Google Ads mutation
request or acknowledgement, conditional-write or idempotency semantics,
delivery or spend outcome, execution authority, or a live capability.

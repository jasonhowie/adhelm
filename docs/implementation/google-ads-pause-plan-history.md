# Google Ads campaign PAUSE plan history

M55 retains a closed Google Ads v25 campaign PAUSE proposal as local history.
It adds no provider request, credential access, approval, native bridge,
kernel route, CLI command, or mutation capability.

`GoogleAdsCampaignPausePlanRecord` wraps exactly one revalidated
`GoogleAdsCampaignPausePlannedAction`.  Its event kind is
`GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN`; provider is `google_ads`; the subplan must
be one `PAUSE` with zero incremental spend.  The record recomputes the action
and plan digests, binds expiry to the plan, and keeps
`execution_authority=false` and `requires_current_kernel_checks=true`.

The ledger envelope is narrow: it requires the exact workspace profile and
Google Ads customer from the plan, `LOCAL_CLI` source, and null run,
occurrence, and observation timestamps.  It records a local historical plan,
not a provider observation, a write, a grant, or a current remote fact.

`retain_google_ads_campaign_pause_plan()` accepts an optional final callback.
Configured callers must hold their configuration and credential continuity
context before calling it.  The callback runs under the ledger stream lock,
immediately before durable bytes are appended, and must only inspect already
held local state and a clock.  It must not read the ledger, call a provider, or
re-enter configuration or credential locking.  A caller that cannot establish
retention must report the existing uncertainty protocol; this helper never
retries a partial local write.

`load_google_ads_campaign_pause_plan()` and
`google_ads_campaign_pause_plan_catalog()` select only checkpoint-verified
records.  They return checkpoint and historical-provenance facts needed by a
later separately authorized workflow.  Loading or cataloging does not refresh
the customer, campaign, OAuth association, or provider permission.

The focused fake-only validation is:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_google_ads_pause_contract.py \
  tests/test_google_ads_pause_plan_history.py \
  tests/test_google_ads_pause_plan_views.py
```

This validates local record reconstruction, envelope binding, checkpoint
selection, callback ordering, and the companion redacted view.  It does not
establish a live Google Ads PAUSE capability, provider mutation semantics,
delivery outcome, idempotency, conditional write behavior, or execution
authority.

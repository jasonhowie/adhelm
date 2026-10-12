# Google Ads PAUSE planning session

`configured_google_ads_pause_planning_session(directory, profile_id,
expected_configuration_digest=None)` is the private preparation boundary for
one future Google Ads campaign `ENABLED` to `PAUSED` plan. It is separate from
the M54 `GoogleAdsEditSession`, whose random observation marker is unsuitable
for a retained plan.

The factory accepts only a configured Google Ads OAuth `EDIT` profile. It
captures the exact configuration digest before protected activity, then asks
the credential lifecycle for a sealed three-slot snapshot: the OAuth bundle,
the configured developer-token slot, and the fixed private PAUSE binding-key
slot. Initial preparation may refresh an OAuth bundle or create and read back
that binding key. It yields no token, developer token, raw protected key,
bundle, client, URL, query, or generic credential selector.

The public session is nonserializable and exposes only:

- `execution_binding`, a detached exact
  `GoogleAdsCampaignPauseExecutionBinding`. It declares the profile, client,
  derived principal, fixed `adwords` scope, optional manager customer,
  protected credential-binding reference, configuration/profile digests, and
  finite resolved/expiry times. It does not establish provider write
  permission or execution authority.
- `observe_campaign(campaign_id)`, which validates the canonical positive
  signed-64-bit campaign ID before any resource request. It performs a new
  fixed customer search followed by a fixed campaign search and returns a
  nonserializable `GoogleAdsCampaignPausePlanningObservation` containing only
  M55 `account_binding` and `observation_binding` models. The latter commits
  the `ENABLED` campaign baseline and a sequential non-atomic interval of at
  most five minutes. Identity is not separately exposed, so a caller cannot
  combine an older customer result with a later campaign state.
- `retention_recheck()`, an owner-thread-only context manager used immediately
  before a ledger append. It yields a callback that must run at least once.

Every fixed resource POST resolves a current **no-refresh** snapshot while the
configuration transaction and the OAuth, developer-token, and PAUSE-key locks
are held in casefolded reference order. Those locks are released before the
fixed Google Ads HTTP request. Any configuration, profile, bearer/bundle,
developer-token, binding-key, expiry, clock, or manager drift fails before the
next resource request. At the frozen M54 reader boundary, the new PAUSE drift
diagnostic is mapped to the existing safe Google Ads EDIT credential diagnostic
so the reader does not turn it into a generic provider error; this remains a
fail-closed refusal.
The session retains the greatest accepted clock sample across both fixed reads
and retention. A later sample or protected snapshot cannot move below that
floor, even when it remains after the original preparation time.

The retention callback re-reads configuration and all three protected slots
through the configuration transaction and backend/service handles already held
by its context. It may not acquire a new backend or lock, refresh or write a
credential, open a provider transport, read a ledger, or re-enter local locks.
The session rejects owner-thread reentry and competing-thread reads promptly
while retention is active. Escaped callbacks, callbacks on a closed session,
and a context exited without a callback are refused. A manual `config.json`
change or protected-slot change after retention opens is therefore detected
before the append callback completes.

This session does not retain a plan, call a mutation endpoint, construct an
authorization, invoke native approval, open a CLI route, prove a live provider
capability, or claim that Google Ads will accept a later PAUSE request. The M56
workflow separately builds and retains the non-authorizing M55 action under
this final local recheck.

Fake-only validation currently uses:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_google_ads_pause_planning_session.py \
  tests/test_google_ads_pause_workflow.py \
  tests/test_google_ads_pause_credentials.py
```

The tests use an in-memory protected store and `httpx.MockTransport`. They do
not contact Google, use an OS credential store, launch a native host, or
create an approval or mutation.

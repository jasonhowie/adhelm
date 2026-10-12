# Private Meta Ads token READ session

This module adds a private in-process bridge from the M37 Meta token bundle to
the already reviewed fixed Meta Ads read adapter. It is not a CLI command, a
generic credential resolver, an account mutation path, or a provider-access
claim for a configured profile.

`configured_meta_ads_token_read_session(directory, profile_id,
expected_configuration_digest=...)` accepts only the exact configuration
digest already computed by its caller from a validated configuration. It
checks that digest before the debug-token GET and again before every fixed
Meta GET and local retention recheck. A stale composition snapshot therefore
cannot retarget a read after a caller has acquired unrelated local
prerequisites such as an export key or a ledger checkpoint.

The factory accepts only a `META_ADS_ACCOUNT_TOKEN` profile with purpose
`READ`. It invokes the fixed Graph v26 `debug_token` inspector once, requiring
the declared application and `ads_read`. The inspector must describe an
expiring `USER` token. The returned account identity is then read first through
the fixed `MetaAdsClient`; its normalized native account ID, currency, and IANA
timezone must exactly equal the customer declarations. Those declarations are
still declarations until that fixed identity response matches them.

The yielded `MetaAdsTokenReadSession` exposes only:

- validated `identity`;
- `list_inventory(maximum_records)`, which reads campaigns, ad sets, and ads
  with the requested bound applied independently to each edge; and
- `run_report(query)`, which requires the caller's native numeric account ID,
  validates declared timezone/currency, then privately rebinds the query to
  the just-proved canonical account reference.

The session owns `httpx.Client(trust_env=False)` and a fixed-host
`MetaAdsClient`. The latter uses one guarded subject-token callback per adapter
GET and has transport retries disabled so a retry cannot reuse a bearer after
local credential drift. The callback acquires configuration then the one
credential-slot lock, rereads the canonical bundle, and compares a private
HMAC marker over the subject token, inspector token, binding key, profile, and
configuration. It checks the original finite inspection expiry before returning
only the subject token to the adapter. The inspector token is transient during
that reread and is never a field or result of the yielded session.

`retention_recheck()` is a separate session-owned context manager for callers
that have completed all remote reads and are about to write local history. It
acquires configuration then credential locks before any caller obtains a ledger
lock. Its zero-argument callback rereads those already-held protected facts,
performs no network or ledger I/O, and can be invoked repeatedly immediately
before each append. At least one successful callback is required. The callback
is bound to the context-owning thread and becomes invalid on context exit;
escaped or cross-thread callbacks fail before a protected read. Session network
methods are rejected during this retention window and after session closure.

This is local fake-tested preparation only. It does not prove an account can be
accessed until the fixed identity GET succeeds, does not make the inspection
observation an approval or mutation grant, does not make remote reads atomic
with local history writes, and does not replace a future fresh identity/current
authority check for an edit workflow. The caller must invoke the retention
callback immediately before its durable append; uncoordinated external edits
remain outside the local lock protocol.

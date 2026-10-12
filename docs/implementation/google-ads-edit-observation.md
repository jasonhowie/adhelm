# Google Ads EDIT observation

M54 adds a private, read-only Google Ads EDIT observation boundary. Its public
entry is:

```python
configured_google_ads_edit_session(directory, profile_id, expected_configuration_digest=None)
```

The yielded `GoogleAdsEditSession` exposes a detached `identity` and only
`read_campaign_state(campaign_id)`. It has no mutation, generic GAQL, URL,
body, token, transport, plan, retention, CLI, or serialization interface.

The factory first validates its local arguments and configuration/profile, then
resolves the sealed OAuth bundle.
Both profile values must describe `google_ads`, `EDIT`,
`OS_OAUTH_GOOGLE`, the exact `adwords` scope, a 10-digit target customer,
explicit declared currency and IANA timezone. The initial session stores only a
keyed continuity marker and nonsecret target facts. It never retains the
credential as session state.

Immediately before every fixed v25 `googleAds:search` POST, the session rereads
configuration and the protected value under configuration-then-credential
locks. It compares the configuration/profile/target/login customer/app/
principal/scope/bearer/expiry marker and rejects drift,
expiry, or clock rollback before the POST. The configuration and credential
locks are released before resource HTTP;
the session’s private lock may serialize its own fixed reads. The client has
`trust_env=False`, no ambient request state, a 64 KiB
response bound, and exactly one read attempt.

The reader proves one customer identity at session creation and later reads one
campaign by a positive signed-64-bit ID. Both use the fixed v25 route and
nested `searchSettings.returnTotalResultsCount=true`, require an exact one-row
response with selected resource-name binding, and reject unknown or unspecified
status values. A returned `CLOSED` customer status remains an observation; it
does not assert that the account is unusable, accessible, or eligible for a
future write.

No campaign mutation route is present. A Google Ads EDIT profile and its
adwords scope are local prerequisites for observation only; this work does not
claim a provider write grant, acknowledgement contract, idempotency, CAS,
delivery state, spend effect, or execution authority.

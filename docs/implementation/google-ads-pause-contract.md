# Google Ads campaign PAUSE contract

M55 adds a pure, closed representation of one Google Ads v25 campaign
configuration candidate:

```text
ENABLED -> PAUSED
```

The public pure API is in
`adhelm.providers.google_ads_pause_contract`:

```python
build_google_ads_campaign_pause_plan(
    *,
    workspace_profile_ref,
    account_binding,
    execution_binding,
    observation_binding,
    expires_at,
) -> GoogleAdsCampaignPausePlannedAction

revalidate_google_ads_campaign_pause_planned_action(value) \
    -> GoogleAdsCampaignPausePlannedAction
```

`GoogleAdsCampaignPausePlannedAction.to_data()` returns its complete closed
JSON-shaped value.  Revalidation rejects extra fields, altered derived
digests, scalar aliases, noncanonical UTC spelling, incomplete members, and a
plan that is not exactly reconstructed from the four bindings.

The action binds a local workspace; the v25 customer baseline; a declared,
nonsecret Google OAuth/developer-token association; and one sequential
customer/campaign observation.  Customer references use
`google_ads.customer.{ten-digit-id}`. Campaign references use
`google_ads.campaign.{customer-id}.{positive-signed-int64-id}`. An optional
`login_customer_client_ref` is an execution-association fact for a manager
route; it is neither substituted for the target customer nor used to infer a
manager relationship.

The account baseline retains `CANCELED`, `CLOSED`, `ENABLED`, or `SUSPENDED`.
`CLOSED` remains an observed provider value, including for test customers; it
does not assert eligibility or lack of access. The campaign baseline is
strictly `ENABLED`. The postcondition is `CONFIGURATION_ONLY` with configured
status `PAUSED` and `effective_delivery=NOT_PREDICTED`.

The execution binding contains only profile, installed client, derived OIDC
principal, the exact `https://www.googleapis.com/auth/adwords` scope, optional
login customer, a nonsecret credential-binding digest, configuration digests,
and the finite resolved/expiry interval. Its fixed status is
`LOCAL_OAUTH_AND_DEVELOPER_TOKEN_ASSOCIATION_ONLY`; it does not assert a
provider permission or write authority. It contains no OAuth bearer, developer
token, protected-slot reference, session HMAC marker, raw response, or generic
credential handle.

The plan has one `google_ads` `PAUSE` subplan, no provider revision,
`StateRef.revision=None`, zero incremental spend in the declared native
currency, and a fixed Google Ads host target. Its remote-only before
fingerprint includes the allowlisted customer and campaign baseline. Its after
fingerprint includes only the configuration postcondition. The action records
that state must be rechecked before egress, configuration readback is required
after a future pause, and reconciliation followed by a new exact
authorization is required after uncertainty. It explicitly has no execution
authority, live validation, conditional-write proof, or idempotency proof.

The observation interval is at most five minutes. The resolved association,
observation, and plan expiry must satisfy:

```text
resolved_at <= read_started_at <= read_finished_at < plan_expiry
plan_expiry <= expires_at
plan_expiry <= read_finished_at + 15 minutes
```

This module imports no configuration, credential, network, ledger, native
approval, CLI, or provider-mutation code and performs no I/O. It is not a
Google Ads mutation implementation. A later configured planning slice must
establish dual-slot continuity, perform a fresh paired identity/campaign
observation, retain the plan under a callback-safe recheck, and separately
research the v25 mutation request, acknowledgement, partial-failure,
conditional-write, and idempotency semantics before it can create a guarded
write path.

# Meta campaign-state reader

This development-only component reads the narrow campaign baseline required by
a later guarded Meta campaign-pause workflow.  It does not create a plan,
authorize or execute a mutation, retain an observation, claim provider
revision/compare-and-swap semantics, or assert that delivery or spend has
stopped.

`MetaCampaignStateReader` accepts only a `SafeHttpClient` configured for one
attempt, `graph.facebook.com` alone, an empty client query, `trust_env=False`,
no ambient HTTPX auth, cookies, or event hooks, and at most 65,536 response
bytes.  It takes a guarded subject-token callback and never accepts an
inspector token, a generic credential resolver, a URL, a query, or a mutation
method.

For each call it revalidates the supplied `MetaAdsIdentity` before the token
callback.  The identity must use the canonical proved account form
`meta_ads.account.act_<native-id>` and `act_<native-id>`.  A campaign ID is an
ASCII, nonzero decimal identifier of at most 40 digits.  The only request is:

```text
GET https://graph.facebook.com/v26.0/<campaign-id>
fields=id,account_id,configured_status,effective_status
```

The reader parses a bounded JSON object with exactly those four keys.  The
returned campaign and account IDs must be exact native decimal values; the
campaign ID must equal the request and the account ID must equal the proved
account after removing the identity's local `act_` route prefix.  It emits only
`campaign_client_ref`, its fixed
`meta_ads.campaign.<id>` reference, the native account ID without `act_`, and
the two status values.

The closed status sets follow the tagged Meta Business SDK 26.0.1 Campaign
model documented in
[the campaign-pause contract](../research/meta-ads-pause-contract.md):

- configured: `ACTIVE`, `ARCHIVED`, `DELETED`, `PAUSED`;
- effective: `ACTIVE`, `ARCHIVED`, `DELETED`, `IN_PROCESS`, `PAUSED`,
  `WITH_ISSUES`.

Unknown response fields, malformed or duplicate JSON, unrecognized status
values, body overflows, identity/account/campaign mismatch, and malformed
token callback values fail closed with bounded diagnostics.  A guarded
configuration-change signal remains a typed `CONFIGURATION_CHANGED` result.
The fixed local `META_TOKEN_CREDENTIAL_CHANGED`, `META_TOKEN_EXPIRED`, and
`CLOCK_INVALID` conditions also retain their code and fixed recovery action.
All other callback text, causes, next actions, and error codes are replaced
before they can reach output.  Each reader call obtains the subject bearer once
and makes one transport attempt.

This reader deliberately has no revision token, ETag, conditional request,
idempotency, cached state, persistent evidence, CAS assertion, authority
surface, or spend assertion. The configured EDIT session rechecks the complete
configuration and credential bundle before each fixed provider read and before
a guarded durable history append. A future mutation workflow needs its separate
authorization, drift and egress checks.

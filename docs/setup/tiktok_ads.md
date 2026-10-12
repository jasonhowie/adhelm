# TikTok Ads setup

This guide connects AdHelm to one TikTok Ads advertiser account with a developer app you
register in TikTok API for Business. AdHelm reads the account (identity, campaign
hierarchy, daily metrics) and changes nothing in it: every remote write is refused in
`0.1.0` ([release definition](../release-0.1.0.md)). TikTok Ads is labelled
`EXPERIMENTAL` until a live read pilot record for it is committed; the label and its
meaning appear in every output that names the provider.

TikTok's flow differs from the other OAuth providers. AdHelm opens no browser and listens
on no port: you open TikTok's authorization link yourself, copy a one-hour, single-use
authorization code from the page TikTok sends you to, and paste it into a hidden prompt.
Run that step yourself, in a terminal. Do not paste the app secret or the code into a chat
with your AI agent; the agent can run every other command in this guide.

Every command in a `guide:run` block below is run against a fake TikTok and TikTok Business
API in AdHelm's test suite, so the commands, file names and flags in those blocks are
checked on each change ([how](README.md)). A command named only in running text is not
run. The fake cannot check TikTok's portal or a live account, so the console steps are
cited to TikTok's documentation under [Sources](#sources), with the date each page was
fetched.

## Create the developer app

You need a TikTok for Business account, an approved developer registration, an approved
developer app, and an advertiser who can authorize it.

1. Sign up for a TikTok for Business account on the Developers portal if you have none,
   then choose **Become a Developer**. TikTok asks for a communication email that is a verified
   **company domain** email (it rejects personal and temporary addresses), a company name
   and website, and a description of how you will use the API. TikTok's page says it
   currently cannot onboard personal accounts or individual developers, so registration
   needs a company.
2. Once your developer profile is approved, choose **Create an App**. Give it a
   descriptive name, describe its intended uses and who can access it, and set an
   **advertiser redirect URL**. TikTok appends the authorization code to this URL and AdHelm
   never receives it, so use a URL you can read the address of. TikTok's page allows up to
   ten redirect URLs and says they may include localhost.
3. Under **Scope of permission**, select what the app needs to read. TikTok's endpoint list
   names the permission each read needs; for AdHelm's reads these are:

   | AdHelm reads | Endpoint | Permission TikTok lists |
   | --- | --- | --- |
   | The advertiser account | `/advertiser/info/` | Ad Account Management > Ad Account Information > Read Ad Account Information |
   | Campaigns | `/campaign/get/` | Ads Management > Campaign > Read Campaigns |
   | Ad groups | `/adgroup/get/` | Ads Management > Ad Group > Read Ad Groups |
   | Ads | `/ad/get/` | Ads Management > Ad > Read Ads |
   | The daily report | `/report/integrated/get/` | Reporting > Consolidated Report |

   TikTok arranges permissions in three levels, and an access token with a first-level
   permission reaches every endpoint beneath it, so choose the narrowest entries the
   console lets you.
4. Submit the app for review. TikTok says review may take two to three business days; the
   result shows under **My Apps** as **Verification Status**, and a rejected app can be
   edited and submitted again. A developer can have up to five apps.
5. In **My Apps**, open the app and note its **App ID** and **Secret** (**Basic
   Information**), and the **Advertiser authorization URL**. Keep the secret out of files
   and chat.
6. Know the advertiser ID of the account you will read: a decimal number, the form TikTok
   returns in `advertiser_ids`. If you do not have it, ask the account's administrator;
   the pages cited here do not say where the portal shows it.

## Scopes

TikTok identifies a permission by a numeric scope ID (for example, `4` is the first-level
**Reporting** permission). It returns the IDs the advertiser granted next to the access
token, and its documentation notes that a token reaches the endpoints the advertiser
granted, "not the permissions that you apply for when creating your developer
application".

AdHelm `0.1.0` does not read, compare or store those IDs. It records the permissions as
`NOT_DOCUMENTED_LOCALLY`, and the profile's `scopes` is a local label only. So AdHelm cannot enforce
least privilege for you: check the permission list on TikTok's authorization page before
you confirm it, and select only the permissions in the table above when you create the
app.

## Redirect and callback

AdHelm has no browser handoff or callback for TikTok. You complete TikTok's advertiser
authorization yourself:

1. Open the **Advertiser authorization URL** from **My Apps** in a browser as a user who
   can authorize the advertiser account.
2. Review the permission list, agree to the Platform Service Agreement and choose
   **Confirm**.
3. Choose **Send Code**, enter the verification code TikTok emails to the address
   associated with the ad account, and choose **Confirm**.
4. TikTok redirects the browser to your advertiser redirect URL with the authorization
   code appended. Copy the value of the `auth_code` parameter from the address bar. AdHelm never
   sees the URL, so the page itself does not matter to it. TikTok's page calls the value both
   `auth_code` and `code`, and its example URL carries a separate `code` parameter, so
   take the value of `auth_code`, the name of the parameter the token request uses.

The code is valid for one hour and can be used once. If it expires or is used, start over
from step 1.

## Access tiers and limits

The pages cited here describe no access tiers for reading. What gates you is
registration and review: a company developer profile, then an approved app (see above). TikTok also documents a
sandbox environment with mock data and its own base URL, `sandbox-ads.tiktok.com`.
`adhelm auth tiktok-token enroll` accepts `SANDBOX` as an `--environment` value but
refuses it, because `0.1.0` sends the token request only to the production host; use
`PRODUCTION`.

TikTok describes the access token from this exchange as long-term: it "does not expire",
and becomes invalid if the advertiser cancels the authorization or you revoke it. It
documents no refresh for it. AdHelm cannot track an expiry it was not given, so
`expiry_source` reads `NOT_DOCUMENTED_LOCALLY` and `access_token_expired` is empty, never
`false` (the status check under [Verify](#verify) pins both), and
`adhelm auth tiktok-token refresh` always refuses with exit code `4`. When a read stops
working, enroll again with a new code. The [Verify](#verify) section runs the refusal.

## Enroll

Create the profile. It holds no secret. `principal_ref` and `client_ref` are local labels
that the profile schema requires; AdHelm never sends them to TikTok, and the real app ID
comes from the hidden prompt, not from the profile. `credential_ref` is the local name of
the slot that will hold the access token. `adhelm schema profile` prints the profile's
schema.

```json guide:file=tiktok-ads-profile.json
{
  "schema_version": 1,
  "profile_id": "tiktok-read",
  "provider": "tiktok_ads",
  "account_ref": "123",
  "principal_ref": "tiktok-advertiser",
  "client_ref": "tiktok-app",
  "scopes": ["read"],
  "purpose": "READ",
  "currency": "USD",
  "timezone": "UTC",
  "credential_source": "OS_STORE",
  "credential_ref": "tiktok-read-token"
}
```

Replace `account_ref` with your advertiser ID, and `currency` and `timezone` with the
account's own three-letter currency code and IANA time zone. AdHelm compares them with
what TikTok reports and refuses to read on a mismatch. Add the profile:

```sh guide:run
adhelm init
adhelm config add-profile --input tiktok-ads-profile.json
```

Complete TikTok's authorization (see [Redirect and callback](#redirect-and-callback)),
then enroll while the code is still valid. This prompts, without echo, for the App ID, the
app secret and the authorization code, and makes the one token request:

```sh guide:run
adhelm auth tiktok-token enroll --profile tiktok-read --environment PRODUCTION --interactive
```

The result states that no TikTok resource was read and no authority to change anything
was granted. If TikTok lists advertisers for the grant and yours is not among them,
enrollment stops with `TIKTOK_TOKEN_ADVERTISER_NOT_GRANTED`; a grant that reaches no
advertiser at all stops with `TIKTOK_TOKEN_NO_ADVERTISERS_GRANTED`. Either way the code is
spent and you need a new one.

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "tiktok_ads",
    "purpose": "READ",
    "operation": "ENROLL",
    "environment": "PRODUCTION",
    "local_state": "READY",
    "advertiser_binding": "PROVEN_IN_EXCHANGE_RESPONSE",
    "granted_permissions": "NOT_DOCUMENTED_LOCALLY",
    "expiry_source": "NOT_DOCUMENTED_LOCALLY",
    "refresh_route": "NOT_DOCUMENTED",
    "app_approval_verified": false,
    "remote_mutations": false,
    "execution_authority": false
  }
}
```

## Verify

Read the local credential state. This contacts no TikTok endpoint:

```sh guide:run
adhelm auth tiktok-token status --profile tiktok-read
```

```json guide:expect
{
  "data": {
    "local_state": "READY",
    "access_token_slot_present": true,
    "app_secret_slot_present": true,
    "provider_authentication_verified": false,
    "access_token_expired": null,
    "expiry_source": "NOT_DOCUMENTED_LOCALLY"
  }
}
```

The refresh command is the other half of that statement: it always refuses, with exit
code `4`, and contacts no TikTok endpoint.

```sh guide:run
adhelm auth tiktok-token refresh --profile tiktok-read # exit 4
```

```json guide:expect
{ "diagnostics": [{ "code": "TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED" }] }
```

`doctor` checks local readiness only. GA4 is required for AdHelm, so until you have
enrolled it (see [GA4 setup](ga4.md)) `doctor` exits `3` and reports GA4 as
`NOT_CONFIGURED_REQUIRED`; TikTok Ads itself shows `CONFIGURED_UNVERIFIED`, because
`doctor` never calls a provider.

```sh guide:run
adhelm doctor # exit 3
```

```json guide:expect
{
  "outcome": "ATTENTION_REQUIRED",
  "data": {
    "remote_checks_performed": false,
    "providers": [
      { "provider": "ga4", "status": "NOT_CONFIGURED_REQUIRED" },
      { "provider": "tiktok_ads", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

`inventory` reads the advertiser's identity and its campaign, ad group and ad hierarchy,
and keeps the result in AdHelm's local history:

```sh guide:run
adhelm inventory tiktok_ads tiktok-read
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "tiktok_ads", "ephemeral": false } }
```

Then read a daily report. Create the report input (`adhelm schema tiktok-ads-report`
prints its schema), replacing `account_ref` with your advertiser ID, the dates with a
window that has ended in the account's time zone, and `currency` and `source_timezone`
with the profile's values. The input schema requires `query_contract_hash` as 64
lowercase hex characters, but AdHelm ignores the value you supply and records its own
digest of the query in the report, so any well-formed value, such as the zeros shown,
works.

```json guide:file=tiktok-ads-report.json
{
  "account_ref": "123",
  "report_id": "report.tiktok.campaign",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "source_timezone": "UTC",
  "currency": "USD",
  "level": "CAMPAIGN"
}
```

```sh guide:run
adhelm report tiktok-ads tiktok-read --input tiktok-ads-report.json
```

```json guide:expect
{ "outcome": "OK", "data": { "observation_persisted": true } }
```

`history verify` re-checks the append-only local ledger that the inventory and report
were written to:

```sh guide:run
adhelm history verify
```

```json guide:expect
{ "outcome": "OK", "data": { "status": "VERIFIED" } }
```

## Revoke

Revoking has two separate halves, and AdHelm can do only one of them.

1. **At TikTok.** TikTok's authorization FAQ gives the advertiser's route: open the TikTok
   For Business authorization settings page (`ads.tiktok.com/ac/page/authorizations`)
   and choose **Remove** next to the developer's authorization. TikTok also documents
   `/oauth2/revoke_token/`, which a developer calls with the app ID, app secret and the
   token. AdHelm never prints the access token and sends no revocation request, so use
   the advertiser's route. AdHelm cannot check that you did, and never claims the grant is
   revoked (`remote_revocation_verified` is always `false`). `adhelm auth tiktok-token
   status` prints the same two routes.
2. **On this machine.** Remove both stored credentials. This deletes only your local
   copies:

```sh guide:run
adhelm auth tiktok-token remove-local --profile tiktok-read
adhelm auth remove-local --profile tiktok-read
adhelm auth tiktok-token status --profile tiktok-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "access_token_slot_present": false } }
```

The first command removes the app secret, the granted advertiser list and AdHelm's
lifecycle record, the second the access token. Removal leaves your local history,
inventory snapshots and reports in place.

## Sources

Fetched from TikTok's own API for Business documentation. The portal renders only with
JavaScript, so each page was read from the documentation service that backs it. Where
TikTok's text and AdHelm's behavior differ, this guide says so above. TikTok shows no
update date on these pages.

- [Create a TikTok for Business account](https://business-api.tiktok.com/portal/docs?id=1738855099573250) (fetched 2026-10-11; no update date shown)
- [Register as a developer](https://business-api.tiktok.com/portal/docs?id=1738855176671234) (fetched 2026-10-11; no update date shown)
- [Create a developer app](https://business-api.tiktok.com/portal/docs?id=1738855242728450) (fetched 2026-10-11; no update date shown)
- [Authorization](https://business-api.tiktok.com/portal/docs?id=1738373141733378) (fetched 2026-10-11; no update date shown)
- [Authentication](https://business-api.tiktok.com/portal/docs?id=1738373164380162) (fetched 2026-10-11; no update date shown)
- [Permission scope](https://business-api.tiktok.com/portal/docs?id=1753986142651394) (fetched 2026-10-11; no update date shown)
- [List of available endpoints](https://business-api.tiktok.com/portal/docs?id=1735713875563521) (fetched 2026-10-11; no update date shown)
- [Revoke a long-term access token](https://business-api.tiktok.com/portal/docs?id=1739965949088770) (fetched 2026-10-11; no update date shown)
- [Authorization FAQs](https://business-api.tiktok.com/portal/docs?id=1766037914914818) (fetched 2026-10-11; no update date shown)
- [Sandbox accounts](https://business-api.tiktok.com/portal/docs?id=1738855331457026) (fetched 2026-10-11; no update date shown)

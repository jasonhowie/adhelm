# Reddit Ads setup

This guide connects AdHelm to one Reddit Ads ad account with a developer application you
create in your own Reddit Ads business. AdHelm reads the account (identity, campaign
hierarchy, daily metrics) and changes nothing in it: every remote write is refused in
`0.1.0` ([release definition](../release-0.1.0.md)). Reddit Ads is labelled
`EXPERIMENTAL` until a live read pilot record for it is committed; the label and its
meaning appear in every output that names the provider.

Run the interactive steps yourself, in a terminal on the machine whose browser you will
use. They open your system browser and prompt for the app ID and the app secret without
echo. Do not paste credentials into a chat with your AI agent; the agent can run every
other command in this guide.

Every command in a `guide:run` block below is run against a fake Reddit and Reddit Ads
API in AdHelm's test suite, so the commands, file names and flags in those blocks are
checked on each change ([how](README.md)). The fake models what AdHelm sends, so it
cannot tell you whether Reddit accepts it, and no live Reddit account has tested it. A
command named only in running text is not run. The console steps are cited to Reddit's
documentation under [Sources](#sources), with the date each page was fetched.

## Create the developer app

You need a Reddit Ads account, a business that you administer, and one developer
application in it.

1. Sign in to Reddit Ads as a business administrator. Reddit's page says only business
   administrators with a verified account can manage developer applications.
2. Open **Business > Developer Portal**, choose **Add Apps > Create an app**, and fill in:
   - **App name** and, optionally, a description and an **About URL**.
   - **Redirect URL**: enter exactly `http://127.0.0.1:8765/oauth/callback`. `8765` is the
     local port used in [Enroll](#enroll); pick another free port above 1023 if you
     prefer, and use it in both places. AdHelm needs a registered redirect on the
     loopback address, not the business-domain URL Reddit's page recommends, and
     Reddit's pages do not say that a loopback redirect is accepted. If Reddit refuses
     it, enrollment stops at the authorization step.
   - **Primary contact**: a business administrator.
3. Choose **Create App**. Reddit generates the app's ID and its secret. `enroll` asks for
   both at hidden prompts and stores the secret in your operating system's credential
   store, in a slot of its own. AdHelm takes neither from a command-line argument or an
   environment variable. Keep the secret out of chat and out of files.
4. Note your ad account's ID: the `{ad_account_id}` in Reddit's `/ad_accounts/{ad_account_id}`
   paths ([Get Ad Account](https://ads-api.reddit.com/docs/v3/api/get-ad-account)). The
   pages cited here do not say where Ads Manager shows it; the API lists accounts with
   List Ad Accounts By Business. AdHelm refuses to read if the account the grant reaches
   differs from the one in your profile.

## Scopes

Reddit documents four scopes: `adsread`, `adsconversions`, `adsedit` and
`adsdatadeletion`. AdHelm asks for exactly one:

```text
adsread
```

A `READ` profile cannot ask for `adsedit`, and AdHelm refuses to record it. Reddit's
authorization link takes a comma-separated list of scopes; AdHelm sends a single scope,
so the separator does not matter.

AdHelm's limit is bookkeeping: it cannot narrow what you consent to at Reddit. Check the
scope on Reddit's consent screen, and check that the username shown is the account you
mean to authorize; Reddit's page says to log out and back in if it is not.

## Redirect and callback

`enroll` opens your system browser at Reddit's authorization page and listens once on
`127.0.0.1` at the port you pass with `--redirect-port`, on the fixed path
`/oauth/callback`, for Reddit's single callback. It then closes the listener. The URL you
registered must match `http://127.0.0.1:<port>/oauth/callback` exactly. Another process
holding the port stops enrollment with `OAUTH_LOOPBACK_UNAVAILABLE`, and a local firewall
that blocks loopback connections will break it.

AdHelm sends `duration=permanent`, which Reddit documents as the setting for production
use; `temporary` would give a one-hour token that cannot be refreshed. Reddit says the
code from the redirect is valid for ten minutes and once. AdHelm uses Reddit's standard
authorization URL, not the System User URL (`ads.reddit.com/oauth`) that Reddit offers
as an additional, open-beta option.

## Access tiers and limits

Reddit documents no access tiers or approval step for a developer application beyond
the business-administrator and verified-account requirement above. It documents rate
limits per endpoint group, applied to each user who authorizes your application:

| Group | Limit | AdHelm reads here |
| --- | --- | --- |
| Campaign management read | 400 requests per 60 seconds | ad account, campaigns, ad groups, ads |
| Reporting | 60 requests per 60 seconds | the report |

An access token lasts one hour (`expires_in` 3600) or one day (86,400), whichever the
token response states; AdHelm accepts either. A permanent authorization also returns a
refresh token, which AdHelm stores and uses with `auth reddit-token refresh`. Reddit's
Ads pages state no expiry for it; AdHelm records none unless Reddit's response states
one, and `refresh` enforces it if it does.

## Enroll

Create the profile. It holds no secret. `principal_ref` and `client_ref` are local labels
that the profile schema requires; AdHelm never sends them to Reddit, and the real app ID
comes from the hidden prompt, not from the profile. `credential_ref` is the local name of
the slot that will hold the access token. `adhelm schema profile` prints the profile's
schema.

```json guide:file=reddit-ads-profile.json
{
  "schema_version": 1,
  "profile_id": "reddit-read",
  "provider": "reddit_ads",
  "account_ref": "a2_fixture",
  "principal_ref": "reddit-user",
  "client_ref": "reddit-app",
  "scopes": ["adsread"],
  "purpose": "READ",
  "currency": "USD",
  "timezone": "UTC",
  "credential_source": "OS_STORE",
  "credential_ref": "reddit-read-bearer"
}
```

Replace `account_ref` with your ad account ID, and `currency` and `timezone` with the
account's own three-letter currency code and IANA time zone. Add the profile:

```sh guide:run
adhelm init
adhelm config add-profile --input reddit-ads-profile.json
```

Enroll the OAuth grant. This prompts for the app ID (`Reddit client ID:`) and then the app
secret (`Reddit client secret:`), opens your browser and waits for Reddit's redirect to the
port you registered: sign in as the user who can open the ad account and approve the
consent screen. Reddit's authenticate page shows the token exchange authenticated with
`-u '<app ID>:<secret>'`, and AdHelm sends exactly that, as HTTP Basic, for the code
exchange and again on every refresh. An empty secret is refused at the prompt. If Reddit
rejects the exchange, `enroll` fails with `OAUTH_TOKEN_REJECTED` or
`OAUTH_TOKEN_ERROR_RESPONSE` and stores nothing, the secret included; check that you
entered the ID and secret of the same application and run it again.

```sh guide:run
adhelm auth reddit-token enroll --profile reddit-read --redirect-port 8765 --interactive
```

The result states that the account answered one identity read and that no authority to
change anything was granted:

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "reddit_ads",
    "purpose": "READ",
    "operation": "ENROLL",
    "granted_scopes": ["adsread"],
    "refresh_status": "GRANTED",
    "local_state": "READY",
    "account_identity_verified": true,
    "app_review_verified": false,
    "remote_mutations": false,
    "execution_authority": false
  }
}
```

## Verify

Read the local credential state. This contacts no Reddit endpoint:

```sh guide:run
adhelm auth reddit-token status --profile reddit-read
```

```json guide:expect
{
  "data": {
    "local_state": "READY",
    "granted_scopes": ["adsread"],
    "bearer_slot_present": true,
    "refresh_slot_present": true,
    "provider_authentication_verified": false
  }
}
```

Exchange the stored refresh token for a new access token. This is the one command that
shows the refresh path works: it reads the stored secret back and sends it with the
refresh token, and it re-checks that the new token reaches the same ad account:

```sh guide:run
adhelm auth reddit-token refresh --profile reddit-read
```

```json guide:expect
{ "data": { "operation": "REFRESH", "local_state": "READY", "account_identity_verified": true } }
```

`doctor` checks local readiness only. GA4 is required for AdHelm, so until you have
enrolled it (see [GA4 setup](ga4.md)) `doctor` exits `3` and reports GA4 as
`NOT_CONFIGURED_REQUIRED`; Reddit Ads itself shows `CONFIGURED_UNVERIFIED`, because
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
      { "provider": "reddit_ads", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

`inventory` reads the account's identity and its campaign, ad group and ad hierarchy, and
keeps the result in AdHelm's local history:

```sh guide:run
adhelm inventory reddit_ads reddit-read
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "reddit_ads", "ephemeral": false } }
```

Then read a daily report. Create the report input (`adhelm schema reddit-ads-report`
prints its schema), replacing `account_ref` with your ad account ID and the dates with a
window that has ended. The input schema requires `query_contract_hash` as 64 lowercase
hex characters, but AdHelm ignores the value you supply and records its own digest of the
query in the report, so any well-formed value, such as the zeros shown, works.

```json guide:file=reddit-ads-report.json
{
  "account_ref": "a2_fixture",
  "report_id": "report.reddit.campaign",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "currency": "USD",
  "level": "CAMPAIGN"
}
```

```sh guide:run
adhelm report reddit-ads reddit-read --input reddit-ads-report.json
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

1. **At Reddit.** Reddit's Ads documentation describes no page for removing a single
   authorization. It does say that deleting a developer application disconnects every
   service using it from the Ads API: **Business > Developer Portal** lists "Delete your
   app" among the things you can do with an application. That also cuts off any other
   tool that uses the same application. Reddit's account help pages refused an automated fetch, so this
   guide cites nothing about the authorized-apps list in a Reddit user's preferences.
   AdHelm's own revocation text points there without having checked it; treat that as
   unconfirmed. AdHelm sends no revocation request and never claims the grant is revoked
   (`remote_revocation_verified` is always `false`).
2. **On this machine.** Remove all three stored credentials. This deletes only your local
   copies, and it does not change the app or its secret at Reddit:

```sh guide:run
adhelm auth reddit-token remove-local --profile reddit-read
```

```json guide:expect
{
  "data": {
    "operation": "LOCAL_REMOVE_ONLY",
    "removed_slot_count": 3,
    "remote_revocation_verified": false
  }
}
```

```sh guide:run
adhelm auth remove-local --profile reddit-read
adhelm auth reddit-token status --profile reddit-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "bearer_slot_present": false } }
```

The first command removes the app secret, the refresh token and AdHelm's lifecycle
record, the second the access token. Removal leaves your local history, inventory
snapshots and reports in place.

## Browser handoff (0.1.1)

Interactive enrollment prints the validated authorization URL and the bounded
local callback wait to stderr. If no tab opens, open that URL in a browser on
the same machine, or rerun with `--no-browser` to skip automatic launch. Ctrl-C
cancels. Stdout remains JSON-only. Keep the URL in the local terminal and never
paste it, an authorization code, or a credential into chat.

## Sources

Fetched from Reddit's own Ads API documentation. Where Reddit's text and AdHelm's
behavior differ, this guide says so above.

- [Create a Developer Application](https://ads-api.reddit.com/docs/v3/guides/quick-start/create-dev-app) (fetched 2026-10-11; no update date shown)
- [Authenticate Your Developer Application](https://ads-api.reddit.com/docs/v3/guides/quick-start/authenticate) (fetched 2026-10-11; no update date shown)
- [Get Ad Account](https://ads-api.reddit.com/docs/v3/api/get-ad-account) (fetched 2026-10-11; no update date shown)
- [Rate Limiting](https://ads-api.reddit.com/docs/v3/guides/quick-start/rate-limiting) (fetched 2026-10-11; no update date shown)
- [System Users](https://ads-api.reddit.com/docs/v3/guides/programs/business/system-users) (fetched 2026-10-11; no update date shown)

# LinkedIn Ads setup

This guide connects AdHelm to one LinkedIn Ads ad account with a developer application you
create in your own LinkedIn developer account. AdHelm reads the account (identity,
campaign hierarchy, daily metrics) and changes nothing in it: every remote write is
refused in `0.1.0` ([release definition](../release-0.1.0.md)). LinkedIn Ads is labelled
`EXPERIMENTAL` until a live read pilot record for it is committed; the label and its
meaning appear in every output that names the provider.

Run the interactive steps yourself, in a terminal on the machine whose browser you will
use. They open your system browser and prompt for the app's client ID and client secret
without echo. Do not paste credentials into a chat with your AI agent; the agent can run
every other command in this guide.

Every command in a `guide:run` block below is run against a fake LinkedIn and LinkedIn
Marketing API in AdHelm's test suite, so the commands, file names and flags in those
blocks are checked on each change ([how](README.md)). A command named only in running
text is not run. The fake cannot check LinkedIn's Developer Portal or a live account, so
the console steps are cited to LinkedIn's documentation under [Sources](#sources), with
the date each page was fetched.

## Create the developer app

You need a LinkedIn Page, a developer application with the Advertising API product, and a
LinkedIn member who has a role on the ad account.

1. Create a LinkedIn Page for the app if you do not have one, and create a developer
   application in the LinkedIn Developer Portal.
2. On the application's **Products** tab, apply to the **Advertising API** and complete
   the access form. LinkedIn reviews the application; its pages give no review time, and
   AdHelm cannot speed it up. Until it is approved the application has none of the
   permissions in [Scopes](#scopes).
3. On the **Auth** tab, note the **Client ID** and **Client Secret**, and under redirect
   URLs add exactly `http://127.0.0.1:8765/oauth/callback`. `8765` is the local port used
   in [Enroll](#enroll); pick another free port above 1023 if you prefer, and use it in
   both places. LinkedIn's page tells you to add the redirect "via HTTPS to your server" and does not
   mention loopback addresses, so whether LinkedIn accepts this `http` URL is unverified.
   If it does not, enrollment stops at the authorization step.
4. Copy your nine-digit ad account ID from Campaign Manager. In the Development tier you
   also map the account to the application: in the Developer Portal open the application,
   go to the **Products** tab, choose **View Ad Accounts**, then **Add Ad Account**, enter
   the ID and save.

AdHelm asks for the client ID and the client secret at hidden prompts. Keep the secret
out of files, shell history and chat. AdHelm stores it in your operating-system credential
store and never prints it.

## Scopes

AdHelm asks LinkedIn for exactly two member permissions, both for the Advertising API
product:

```text
r_ads r_ads_reporting
```

LinkedIn describes them as "Retrieve your advertising accounts" and "Retrieve reporting
for your advertising accounts". A `READ` profile cannot ask for `rw_ads`, LinkedIn's
permission to manage ad accounts, and AdHelm refuses to record it. AdHelm's limit is
bookkeeping: it cannot narrow what you consent to at LinkedIn, so check the permissions
on LinkedIn's consent screen.

A granted permission is not enough by itself: LinkedIn requires the member to hold an ad
account role that allows reading the account, and the roles are defined on LinkedIn's
Ad Account Roles page. `enroll` proves only that the account answered one identity read;
it does not report the member's role (`account_role_verified` is `false`).

If you change the permissions your application requests, LinkedIn's page says members
must authorize again.

## Redirect and callback

`enroll` opens your system browser at LinkedIn's authorization page and listens once on
`127.0.0.1` at the port you pass with `--redirect-port`, on the fixed path
`/oauth/callback`, for LinkedIn's single callback. It then closes the listener. LinkedIn
matches the redirect against the URLs registered on the application, so the one you
added must be exactly `http://127.0.0.1:<port>/oauth/callback`. LinkedIn does not
document varying the loopback port, which is why AdHelm uses a fixed one. Another
process holding the port stops enrollment with `OAUTH_LOOPBACK_UNAVAILABLE`, and a local
firewall that blocks loopback connections will break it.

LinkedIn says the authorization code is valid for 30 minutes and must be used
immediately.

## Access tiers and limits

LinkedIn gives every application the **Development** tier of the Advertising API and
upgrades it only on request:

| Tier | Read | Edit | How you get it |
| --- | --- | --- | --- |
| Development | No limit on reading data for ad accounts you administer | Up to five ad accounts you administer; one test ad account may be created through the API | Default for every application |
| Standard | No limit | Unlimited | A support ticket on LinkedIn's Developer Support Portal, with a demonstration video |

For read-only use the Development tier is enough; the five-account limit is on edits,
which `0.1.0` never makes. This is a LinkedIn-side limit, not an AdHelm setting.

A LinkedIn access token lasts 60 days. LinkedIn issues programmatic refresh tokens only to
applications it has enabled for them, with a 365-day lifetime that does not reset when
you use it. Without one, AdHelm records `REFRESH_NOT_GRANTED`: run `enroll` again when the
token expires (LinkedIn's page says the consent screen is bypassed while the member's earlier
grant stands). With one, `adhelm auth linkedin-token refresh --profile linkedin-read`
exchanges it for a new access token and re-checks that the token reaches the same ad
account. LinkedIn also reserves the right to revoke any token at any time.

LinkedIn's account data supplies no time zone, so AdHelm reads LinkedIn in UTC: the profile
and the report input below both say `UTC`.

## Enroll

Create the profile. It holds no secret. `principal_ref` and `client_ref` are local labels
that the profile schema requires; AdHelm never sends them to LinkedIn, and the real client
ID comes from the hidden prompt, not from the profile. `credential_ref` is the local name
of the slot that will hold the access token. `adhelm schema profile` prints the profile's
schema.

```json guide:file=linkedin-ads-profile.json
{
  "schema_version": 1,
  "profile_id": "linkedin-read",
  "provider": "linkedin_ads",
  "account_ref": "456",
  "principal_ref": "linkedin-member",
  "client_ref": "linkedin-app",
  "scopes": ["r_ads", "r_ads_reporting"],
  "purpose": "READ",
  "currency": "USD",
  "timezone": "UTC",
  "credential_source": "OS_STORE",
  "credential_ref": "linkedin-read-bearer"
}
```

Replace `account_ref` with your ad account ID and `currency` with the account's
three-letter currency code. Add the profile:

```sh guide:run
adhelm init
adhelm config add-profile --input linkedin-ads-profile.json
```

Enroll the OAuth grant. This prompts for the client ID and then the client secret, opens
your browser and waits for LinkedIn's redirect to the port you registered: sign in as the
member who has a role on the ad account and approve the consent screen.

```sh guide:run
adhelm auth linkedin-token enroll --profile linkedin-read --redirect-port 8765 --interactive
```

The result states that the account answered one identity read and that no authority to
change anything was granted:

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "linkedin_ads",
    "purpose": "READ",
    "operation": "ENROLL",
    "granted_scopes": ["r_ads", "r_ads_reporting"],
    "unrequested_granted_scopes": [],
    "local_state": "READY",
    "account_identity_verified": true,
    "app_review_verified": false,
    "account_role_verified": false,
    "remote_mutations": false,
    "execution_authority": false
  }
}
```

`unrequested_granted_scopes` lists any permission LinkedIn echoed that AdHelm did not ask
for. AdHelm reports it and never records it as authority; it does not narrow the grant at
LinkedIn.

## Verify

Read the local credential state. This contacts no LinkedIn endpoint:

```sh guide:run
adhelm auth linkedin-token status --profile linkedin-read
```

```json guide:expect
{
  "data": {
    "local_state": "READY",
    "granted_scopes": ["r_ads", "r_ads_reporting"],
    "bearer_slot_present": true,
    "provider_authentication_verified": false
  }
}
```

`doctor` checks local readiness only. GA4 is required for AdHelm, so until you have
enrolled it (see [GA4 setup](ga4.md)) `doctor` exits `3` and reports GA4 as
`NOT_CONFIGURED_REQUIRED`; LinkedIn Ads itself shows `CONFIGURED_UNVERIFIED`, because
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
      { "provider": "linkedin_ads", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

`inventory` reads the account's identity and its campaign group, campaign and creative
hierarchy, and keeps the result in AdHelm's local history:

```sh guide:run
adhelm inventory linkedin_ads linkedin-read
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "linkedin_ads", "ephemeral": false } }
```

Then read a daily report. Create the report input (`adhelm schema linkedin-ads-report`
prints its schema), replacing `account_ref` with your ad account ID, the dates with a
window that has ended, and `currency` with the profile's value. The input schema
requires `query_contract_hash` as 64 lowercase hex characters, but AdHelm ignores the
value you supply and records its own digest of the query in the report, so any
well-formed value, such as the zeros shown, works.

```json guide:file=linkedin-ads-report.json
{
  "account_ref": "456",
  "report_id": "report.linkedin.campaign",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "source_timezone": "UTC",
  "currency": "USD",
  "level": "CAMPAIGN"
}
```

```sh guide:run
adhelm report linkedin-ads linkedin-read --input linkedin-ads-report.json
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

1. **At LinkedIn.** LinkedIn documents no revocation endpoint for this flow. The member
   who authorized the application removes it in LinkedIn: **Me > Settings & Privacy >
   Data privacy**, then under **Other applications** choose **Change** next to
   **Permitted Services** and **Remove** next to your application. This ends that
   member's grant, and it also stops any other tool that uses the same application.
   AdHelm sends no revocation request and never claims the grant is revoked (`remote_revocation_verified`
   is always `false`).
2. **On this machine.** Remove both stored credentials. This deletes only your local
   copies:

```sh guide:run
adhelm auth linkedin-token remove-local --profile linkedin-read
adhelm auth remove-local --profile linkedin-read
adhelm auth linkedin-token status --profile linkedin-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "bearer_slot_present": false } }
```

The first command removes the client secret, any refresh token and AdHelm's lifecycle
record, the second the access token. Removal leaves your local history, inventory
snapshots and reports in place.

## Browser handoff (0.1.1)

Interactive enrollment prints the validated authorization URL and the bounded
local callback wait to stderr. If no tab opens, open that URL in a browser on
the same machine, or rerun with `--no-browser` to skip automatic launch. Ctrl-C
cancels. Stdout remains JSON-only. Keep the URL in the local terminal and never
paste it, an authorization code, or a credential into chat.

## Sources

Fetched from LinkedIn's own documentation. Where LinkedIn's text and AdHelm's behavior
differ, this guide says so above.

- [Quick Start](https://learn.microsoft.com/en-us/linkedin/marketing/quick-start) (fetched 2026-10-11; page last updated 2026-02-16)
- [Increasing Access](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access) (fetched 2026-10-11; page last updated 2026-07-28)
- [3-legged OAuth, authorization code flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow) (fetched 2026-10-11; page last updated 2025-11-10)
- [Refresh Tokens with OAuth 2.0](https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens) (fetched 2026-10-11; page last updated 2025-05-31)
- [Third-party applications data use](https://www.linkedin.com/help/linkedin/answer/a519947) (fetched 2026-10-11; page shows only "Last updated: 2 years ago")

# Google Ads setup

This guide connects AdHelm to one Google Ads account with your own Google Cloud
project. AdHelm reads the account (identity, campaign hierarchy, daily metrics) and
changes nothing in it: every remote write is refused in `0.1.0`
([release definition](../release-0.1.0.md)). Google Ads is labelled `EXPERIMENTAL`
until a live read pilot record for it is committed; the label and its meaning appear in
every output that names the provider.

Run the interactive steps yourself, in a terminal on the machine whose browser you will
use. They open your system browser and prompt for a secret without echo. Do not paste
credentials, the client file or its contents into a chat with your AI agent; the agent
can run every other command in this guide.

Every command in a `guide:run` block below is run against a fake Google Ads API in
AdHelm's test suite, so the commands, file names and flags in those blocks are checked
on each change ([how](README.md)). A command named only in running text is not run. The
fake cannot check Google's console, so the console steps are cited to Google's
documentation under [Sources](#sources), with the date each page was fetched.

## Create the developer app

You need a Google Cloud project that you control, one OAuth client in it, and a Google
user who can open the Google Ads account.

1. Select or create a Google Cloud project and enable the **Google Ads API** in its API
   Library. Billing is optional; Google charges nothing for the API.
2. Open the **Google Ads API Overview** page in the Cloud console and read the project's
   access level (see [Access tiers and limits](#access-tiers-and-limits)). A `Test`
   project can reach only test accounts; apply for `Explorer` or `Basic` to read a
   production account.
3. In **Google Auth Platform**, register the app if the console asks you to, then:
   - **Audience:** choose `External` for a personal Google user, or `Internal` if your
     Google Workspace organization owns the project. While the publishing status is
     `Testing`, add your own Google user as a test user. Read the note on seven-day
     expiry under [Access tiers and limits](#access-tiers-and-limits) before you choose
     whether to publish.
   - **Data Access:** add the scope in [Scopes](#scopes).
4. In **Google Auth Platform > Clients**, choose **Create client**, set the application
   type to **Desktop app** and create it. In the confirmation dialog choose
   **Download JSON**: Google shows the client secret only when the client is created.
   Save the file as `client_secret.json` in the directory you will run the commands
   from.
5. Keep the file private to your user. On macOS and Linux:

   ```sh
   chmod 600 client_secret.json
   ```

   On Windows keep it inside your own user profile folder. AdHelm refuses a client file
   that another operating-system user can read, or that has other hard links.

The client file stays on your machine. AdHelm reads it once during enrollment and stores
the secret in your operating-system credential store; it is never written to AdHelm's
configuration or history, and never printed.

Your Google Ads **customer ID** is the ten-digit number without hyphens. If you reach
the account through a manager (MCC) account, you also need the manager's ten-digit ID;
Google requires it in the `login-customer-id` request header for that route.

## Scopes

AdHelm asks Google for exactly two scopes: `openid`, to bind the grant to one Google
user, and

```text
https://www.googleapis.com/auth/adwords
```

Google Ads has one scope for reading and writing. AdHelm cannot narrow it: the profile
you enroll is a `READ` profile, AdHelm's own commands only read, and `0.1.0` refuses
every remote write before it reads a credential. The Google-side grant is still broad,
which is the reason to revoke it when you are done (see [Revoke](#revoke)).

## Redirect and callback

A **Desktop app** client needs no redirect URI configured in the console; Google's
console asks for nothing more when you create one. `enroll` opens your system browser
and listens once on `127.0.0.1` on a random free port for Google's single callback, as
Google's installed-app flow describes, then closes the listener. A local firewall that
blocks loopback connections will break it. AdHelm accepts a client file whose
`redirect_uris` are only `http://localhost` or `http://127.0.0.1`, which is what Google
writes into a Desktop client download.

## Access tiers and limits

Google decides the API access level per Cloud project, from the project that holds your
OAuth client:

| Level | Production accounts | Operations per day | How you get it |
| --- | --- | --- | --- |
| Test | No, test accounts only | 15,000 | Automatic when you enable the API |
| Explorer | Yes | 2,880 (production), 15,000 (test) | Apply on the Overview page; Google usually upgrades automatically |
| Basic | Yes | 15,000 | Brand verification first, then apply on the Overview page |
| Standard | Yes | Unlimited, per-feature limits apply | Manual compliance review |

For read-only use, `Explorer` or `Basic` is enough to read a production account. This is
a Google-side limit, not an AdHelm setting.

**Developer token.** Google states that developer tokens were sunset on 2026-09-09: the
`developer-token` header is now optional and ignored by Google's servers, access level
comes from the Cloud project, Google tells new developers not to apply through a manager
account's API Center, and it plans to reject the header in a future major API version.
AdHelm `0.1.0` was built before that change. A Google Ads profile must still name a
developer-token credential, `auth google-oauth status` reports that slot, and every read
sends its value in the header. So enrollment includes a second, hidden-prompt step to
fill the slot. If you still hold a developer token, enter it. If you never had one,
enter a short non-secret placeholder such as `unused`; Google ignores the value. This is
a known limitation of `0.1.0` and will stop being needed when AdHelm stops requiring the
slot.

**Consent screen status.** A project whose publishing status is `Testing` is limited to
100 listed test users, and Google expires an authorization seven days after consent,
including the refresh token that offline access depends on. After that AdHelm cannot get
a new access token for the profile: reconnect with `adhelm auth google-oauth reconnect`
as the same Google user (it replaces the grant for the same user, client and scope only).
Publishing the app (`In production`) removes the seven-day limit; an app that is not
verified shows Google's "unverified app" warning during consent, which you can continue
past with **Advanced** for a client you own. A refresh token also stops working if it is
unused for six months or you revoke it, and Google keeps at most 100 per Google Account
per client.

## Enroll

Create the profile draft. It holds no secret and no Google subject: AdHelm learns the
subject from the signed-in Google user and writes the final profile itself.

```json guide:file=google-ads-draft.json
{
  "profile_id": "google-ads-read",
  "provider": "google_ads",
  "account_ref": "1234567890",
  "client_ref": "123456789012-exampleexampleexample.apps.googleusercontent.com",
  "scopes": ["https://www.googleapis.com/auth/adwords"],
  "purpose": "READ",
  "currency": "USD",
  "timezone": "UTC",
  "credential_ref": "google-ads-read-oauth",
  "secondary_credential_ref": "google-ads-read-developer-token"
}
```

Replace the example values:

- `account_ref`: your ten-digit customer ID, no hyphens. If you use a manager account,
  add `"login_customer_ref": "<manager ID>"` (ten digits) to the draft.
- `client_ref`: the `client_id` inside `client_secret.json`. AdHelm refuses the draft if
  it differs from the file.
- `currency` and `timezone`: the account's own three-letter currency and IANA time zone.
  AdHelm compares them with what Google reports and refuses to read on a mismatch.
- `credential_ref` and `secondary_credential_ref`: local names for the two credential
  slots. They must differ from each other and from the names of any other profile.

`adhelm schema google-oauth-profile-draft` prints the draft's schema. Run these commands
in the directory that holds both files:

```sh guide:run
adhelm init
adhelm schema google-oauth-profile-draft --root-only
```

Enroll the OAuth grant. This opens your browser: sign in as the Google user who can open
the account and approve the consent screen.

```sh guide:run
adhelm auth google-oauth enroll --input google-ads-draft.json --client-config client_secret.json --interactive
```

The result states that no Google resource was read and no authority to change anything
was granted:

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "google_ads",
    "purpose": "READ",
    "operation": "ENROLL",
    "resource_access_verified": false,
    "execution_authority": false
  }
}
```

Store the developer-token value (see above). The prompt does not echo what you type.

```sh guide:run
adhelm auth store --profile google-ads-read --slot SECONDARY --interactive
```

```json guide:expect
{
  "data": {
    "local_only": true,
    "slots": [{ "slot": "SECONDARY", "state": "PRESENT" }]
  }
}
```

## Verify

Read the local credential state. This contacts no Google endpoint:

```sh guide:run
adhelm auth google-oauth status --profile google-ads-read
```

```json guide:expect
{
  "data": {
    "provider": "google_ads",
    "local_state": "READY",
    "granted_scopes": ["openid", "https://www.googleapis.com/auth/adwords"],
    "developer_token_slot_state": "PRESENT",
    "provider_authentication_verified": false
  }
}
```

`doctor` checks local readiness only. GA4 is required for AdHelm, so until you have
enrolled it (see [GA4 setup](ga4.md)) `doctor` exits `3` and reports GA4 as
`NOT_CONFIGURED_REQUIRED`; Google Ads itself shows `CONFIGURED_UNVERIFIED`, because
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
      { "provider": "google_ads", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

The first Google request is `inventory`. It reads the account's identity and its
campaign, ad group and ad hierarchy, and keeps the result in AdHelm's local history:

```sh guide:run
adhelm inventory google_ads google-ads-read
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "google_ads", "ephemeral": false } }
```

Then read a daily report. Create the report input (`adhelm schema google-ads-report`
prints its schema), replacing the dates with a window that has ended in the account's
time zone, and `currency` and `source_timezone` with the profile's values.
The input schema requires `query_contract_hash` as 64 lowercase hex characters, but
AdHelm ignores the value you supply and records its own digest of the query in the
report, so any well-formed value, such as the zeros shown, works.

```json guide:file=google-ads-report.json
{
  "account_ref": "1234567890",
  "report_id": "report.google-ads.campaign",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "source_timezone": "UTC",
  "currency": "USD",
  "level": "CAMPAIGN"
}
```

```sh guide:run
adhelm report google-ads google-ads-read --input google-ads-report.json
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

1. **At Google.** Open `myaccount.google.com/linkedapps`, choose **Access to your Google
   Account**, select the app you enrolled, choose **See details**, then **Remove access**
   and **Confirm**. This ends the grant. It also interrupts anything else that uses the
   same OAuth client. To retire the client's secret, rotate it on the **Google Auth
   Platform > Clients** page. There is nothing to revoke for a developer token: Google no
   longer issues or checks them.
2. **On this machine.** Remove both stored credentials. This deletes only your local
   copies; it never revokes anything at Google and AdHelm cannot verify that you did
   (`remote_revocation_verified` is always `false`).

```sh guide:run
adhelm auth remove-local --profile google-ads-read
adhelm auth remove-local --profile google-ads-read --slot SECONDARY
adhelm auth google-oauth status --profile google-ads-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "primary_slot_present": false } }
```

Removal leaves your local history, inventory snapshots and reports in place. Delete
`client_secret.json` as well if you no longer need it.

## Sources

Fetched from Google's own documentation. Where Google's text and AdHelm's behavior
differ, this guide says so above.

- [Access levels](https://developers.google.com/google-ads/api/docs/access-levels) (fetched 2026-10-11; page last updated 2026-09-30)
- [Developer token](https://developers.google.com/google-ads/api/docs/api-policy/developer-token) (fetched 2026-10-11; page last updated 2026-10-07)
- [Set up a Google API Console project for authorization](https://developers.google.com/google-ads/api/docs/oauth/cloud-project) (fetched 2026-10-11; page last updated 2026-10-06)
- [Single-user authentication workflow](https://developers.google.com/google-ads/api/docs/oauth/single-user-authentication) (fetched 2026-10-11; page last updated 2026-10-06)
- [Call structure and login-customer-id](https://developers.google.com/google-ads/api/docs/concepts/call-structure) (fetched 2026-10-11; page last updated 2026-10-06)
- [OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2/native-app) (fetched 2026-10-11; page last updated 2026-09-14)
- [Using OAuth 2.0 to access Google APIs, refresh token expiration](https://developers.google.com/identity/protocols/oauth2) (fetched 2026-10-11; page last updated 2026-05-26)
- [Manage OAuth clients](https://support.google.com/cloud/answer/15549257) (fetched 2026-10-11; no update date shown)
- [Manage app audience](https://support.google.com/cloud/answer/15549945) (fetched 2026-10-11; no update date shown)
- [Remove a third-party app's access to your Google Account](https://support.google.com/accounts/answer/13533235) (fetched 2026-10-11; no update date shown)

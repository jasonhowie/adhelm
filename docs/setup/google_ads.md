# Google Ads setup

This guide connects AdHelm to one Google Ads account with your own Google Cloud
project. AdHelm reads the account (identity, campaign hierarchy, daily metrics) and
changes nothing in it: every remote write is refused in `0.1.0`
([release definition](../release-0.1.0.md)). Google Ads is labelled `EXPERIMENTAL`
until a live read pilot record for it is committed; the label and its meaning appear in
every output that names the provider.

Google retired Google Ads developer tokens on 2026-09-09. Access to the API now belongs
to the Google Cloud project that owns your OAuth client, so there is no developer token
to apply for, store or send, and AdHelm neither asks for one nor sends the
`developer-token` header. You do not need a Google Ads manager account unless you manage
several accounts through one.

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

You need a Google Cloud project that you control, an OAuth consent screen and one OAuth
client in it, and a Google user who can open the Google Ads account. The project is what
Google grants API access to, so use the same project for the steps below.

1. Select or create a Google Cloud project and enable the **Google Ads API** in its API
   Library. Billing is optional; Google charges nothing for the API. A new project starts
   at the `Test` access level.
2. Open the **Google Ads API Overview** page in the Cloud console and read the project's
   access level. A `Test` project can reach only test accounts; to read a production
   account, apply there for more access (see
   [Access tiers and limits](#access-tiers-and-limits)). Apply on this page, not on the
   API Center page inside Google Ads: Google does not process applications made there.
3. In **Google Auth Platform**, set up the OAuth consent screen if the console asks you
   to, then:
   - **Audience:** choose `External` for a personal Google user, or `Internal` if your
     Google Workspace organization owns the project. While the publishing status is
     `Testing`, add your own Google user as a test user. Read the note on seven-day
     expiry under [Access tiers and limits](#access-tiers-and-limits) before you choose
     whether to publish. Brand verification for Basic access needs `External` and
     `In production`.
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
Google requires it in the `login-customer-id` request header for that route. Without a
manager route, leave it out. A manager account is not otherwise needed to use the API.

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
OAuth client. AdHelm cannot read the level: no reviewed Google Ads read returns it, so
`auth google-oauth status` reports `cloud_project_access_level` as `NOT_OBSERVABLE` and
the audit says so rather than assuming a tier.

| Level | Production accounts | Operations per day | How you get it |
| --- | --- | --- | --- |
| Test | No, test accounts only | 15,000 | Automatic when you enable the API |
| Explorer | Yes | 2,880 (production), 15,000 (test) | Apply on the Overview page; Google may upgrade automatically |
| Basic | Yes | 15,000 | Brand verification first, then apply from Explorer on the Overview page |
| Standard | Yes | Unlimited, per-feature limits apply | Manual compliance review |

For read-only use, `Explorer` or higher is enough to read a production account; AdHelm
recommends `Basic` because its daily allowance is not cut to 2,880 operations. This is a
Google-side limit, not an AdHelm setting. The progression is Test, Explorer, Basic,
Standard: you apply for `Basic` from `Explorer`, never straight from `Test`.

**Apply for Explorer, then Basic.** Google's access-levels page says the project must be
at `Explorer` before it can apply for `Basic`.

1. On the **Google Ads API Overview** page, confirm the current level is `Test`, expand
   **Upgrade access level**, and choose **Apply for access** for `Explorer`. Google may
   upgrade the project automatically.
2. Complete brand verification for the project. In **Google Auth Platform**, make the
   app `External` and `In production`, fill in the **Branding** information, choose
   **Verify branding**, and when it succeeds choose **Publish branding**. Google says
   this takes a few minutes. It is required for every new `Basic` or `Standard`
   application.
3. Back on the Overview page, confirm no brand verification warnings remain and choose
   **Apply for access** for `Basic`. Google says these applications are automated and
   reviewed within minutes.

Managing a project's access level needs quota-management permission, which the Owner,
Editor, Quota Administrator and Service Usage Admin roles include.

**No developer token.** Google states that developer tokens were sunset on 2026-09-09:
the `developer-token` header is ignored by Google's servers and will be rejected in a
future major API version, and a leaked token cannot grant access to another project.
Google's "make your first call" page still says in its text that a token is required,
while its steps and example request send none; this guide follows the Developer token page,
which is the more recently updated of the two.
Google moved an existing token's access level only to the project that had made calls
with it in the 90 days before the sunset; a project whose token made no calls in that
period did not inherit access and starts at `Test`.

AdHelm never sends the header and has no credential slot for a token. A profile that the
published 0.1.0 wrote carries a `secondary_credential_ref` (null, or the name of a
developer-token entry). This release drops it on load and never reads or writes it. The
Google grant 0.1.0 stored was sealed against the old profile shape, so reconnect each
Google profile once with `adhelm auth google-oauth reconnect` as the same Google user; until you do, a read
stops with `GOOGLE_OAUTH_PROFILE_BINDING_STALE`, whose next action is the exact command. See
[Revoke](#revoke) for the entry 0.1.0 stored.

**If a read is refused.** A `Test`-level project that reads a production account gets
Google's `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` (a developer token that was never
used before 2026-09-09 gets it too). AdHelm reports it as
`GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` with a next action to apply for
`Explorer` access on the Overview page, then `Basic`, as above. Google's page names
`ACTION_NOT_PERMITTED` as the older-API-version form of the same error; on v25, the only
version AdHelm calls, it is the generic user-permission error, so AdHelm reports it as
the generic `GOOGLE_ADS_ACCESS_NOT_PERMITTED`. Google also documents a
known issue in which projects upgraded after 2026-09-09 can still get
`AUTHORIZATION_ERROR` on production accounts; AdHelm reports it as
`GOOGLE_ADS_AUTHORIZATION_ERROR`, cites that known issue and Google's workaround
(apply for `Explorer` access with a new project), and goes no further than the page says.
A second known issue can reject brand-verified users for `Basic` or `Explorer` when the
project is on a free trial or has a suspended billing account.

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
  "credential_ref": "google-ads-read-oauth"
}
```

Replace the example values:

- `account_ref`: your ten-digit customer ID, no hyphens. If you use a manager account,
  add `"login_customer_ref": "<manager ID>"` (ten digits) to the draft.
- `client_ref`: the `client_id` inside `client_secret.json`. AdHelm refuses the draft if
  it differs from the file.
- `currency` and `timezone`: the account's own three-letter currency and IANA time zone.
  AdHelm compares them with what Google reports and refuses to read on a mismatch.
- `credential_ref`: the local name of the credential slot. It must differ from the names
  of any other profile's slot.

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

There is no second credential to store: the OAuth grant is the only credential this
profile holds.

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
    "cloud_project_access_level": "NOT_OBSERVABLE",
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
2. **On this machine.** Remove the stored credential. This deletes only your local copy;
   it never revokes anything at Google and AdHelm cannot verify that you did
   (`remote_revocation_verified` is always `false`).

```sh guide:run
adhelm auth remove-local --profile google-ads-read
adhelm auth google-oauth status --profile google-ads-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "primary_slot_present": false } }
```

Removal leaves your local history, inventory snapshots and reports in place. Delete
`client_secret.json` as well if you no longer need it.

**A developer token stored by AdHelm 0.1.0.** The published 0.1.0 asked you to store a
developer-token value in a second slot, kept in the operating-system credential store under
the service `adhelm` and the name in the profile's `secondary_credential_ref`. This
release never reads or sends it, and `auth remove-local` no longer addresses it. Delete it with
your operating system's credential manager (Keychain Access, Credential Manager, or
`secret-tool`/Seahorse), or leave it: it is inert, and Google ignores developer tokens.
Re-enrolling under a new profile ID starts clean.

## Sources

Fetched from Google's own documentation. Where Google's text and AdHelm's behavior
differ, this guide says so above.

- [Developer token sunset](https://developers.google.com/google-ads/api/docs/api-policy/developer-token) (fetched 2026-10-12; page last updated 2026-10-07)
- [Access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels) (fetched 2026-10-12; page last updated 2026-09-30)
- [Brand verification](https://developers.google.com/google-ads/api/docs/api-policy/brand-verification) (fetched 2026-10-12; page last updated 2026-10-07)
- [Make your first call](https://developers.google.com/google-ads/api/docs/get-started/make-first-call) (fetched 2026-10-12; page last updated 2026-09-30)
- [Set up a Google API Console project for authorization](https://developers.google.com/google-ads/api/docs/oauth/cloud-project) (fetched 2026-10-11; page last updated 2026-10-06)
- [Single-user authentication workflow](https://developers.google.com/google-ads/api/docs/oauth/single-user-authentication) (fetched 2026-10-11; page last updated 2026-10-06)
- [Call structure and login-customer-id](https://developers.google.com/google-ads/api/docs/concepts/call-structure) (fetched 2026-10-11; page last updated 2026-10-06)
- [OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2/native-app) (fetched 2026-10-11; page last updated 2026-09-14)
- [Using OAuth 2.0 to access Google APIs, refresh token expiration](https://developers.google.com/identity/protocols/oauth2) (fetched 2026-10-11; page last updated 2026-05-26)
- [Manage OAuth clients](https://support.google.com/cloud/answer/15549257) (fetched 2026-10-11; no update date shown)
- [Manage app audience](https://support.google.com/cloud/answer/15549945) (fetched 2026-10-11; no update date shown)
- [Remove a third-party app's access to your Google Account](https://support.google.com/accounts/answer/13533235) (fetched 2026-10-11; no update date shown)

# GA4 setup

This guide connects AdHelm to one Google Analytics 4 property with your own Google Cloud
project. GA4 is required for AdHelm: `doctor` does not pass until a GA4 profile exists.
AdHelm reads the property's reporting data and its configuration (web stream, key events,
event rules) and changes nothing in it: every remote write, including GA4 Admin edits, is
refused in `0.1.0` ([release definition](../release-0.1.0.md)). A GA4 key event is not a
Google Ads conversion, and AdHelm does not treat it as one. GA4 is labelled
`EXPERIMENTAL` until a live read pilot record for it is committed.

Run the interactive steps yourself, in a terminal on the machine whose browser you will
use; they open your system browser. Do not paste credentials, the client file or its
contents into a chat with your AI agent; the agent can run every other command in this
guide.

Every command in a `guide:run` block below is run against a fake Google Analytics API in
AdHelm's test suite, so the commands, file names and flags in those blocks are checked
on each change ([how](README.md)). A command named only in running text is not run. The
fake cannot check Google's console, so the console steps are cited to Google's
documentation under [Sources](#sources), with the date each page was fetched.

## Create the developer app

AdHelm reads GA4 through two profiles, each with its own Google consent: a `ga4` profile
for the Analytics Data API (reports) and a `ga4_admin` profile for the Analytics Admin
API (configuration). Both are read-only in `0.1.0`, and they stay separate so that a
later release can authorize editing without widening the reporting profile.

You need a Google Cloud project that you control, one OAuth client in it, and a Google
user with access to the property. If you already created the client for
[Google Ads](google_ads.md#create-the-developer-app), reuse it and only add the scope
below under **Data Access**.

1. Select or create a Google Cloud project and enable both the **Google Analytics Data
   API** and the **Google Analytics Admin API**.
2. In **Google Auth Platform**, register the app if the console asks you to, choose the
   **Audience** (`External` for a personal Google user, or `Internal` for a Google
   Workspace organization that owns the project), add your own Google user as a test user
   while the status is `Testing`, and add the scope in [Scopes](#scopes) under **Data
   Access**. Read the seven-day note in [Access tiers and limits](#access-tiers-and-limits).
3. In **Google Auth Platform > Clients**, choose **Create client**, set the application
   type to **Desktop app** and create it. In the confirmation dialog choose **Download
   JSON**: Google shows the client secret only when the client is created. Save the file
   as `client_secret.json` in the directory you will run the commands from.
4. Keep the file private to your user. On macOS and Linux:

   ```sh
   chmod 600 client_secret.json
   ```

   On Windows keep it inside your own user profile folder. AdHelm refuses a client file
   that another operating-system user can read, or that has other hard links.

You also need three facts about the property:

- The **property ID**, a number. In Analytics, open **Admin**, select the property, then
  **Property Settings**. It is not the measurement ID (`G-` followed by letters and
  digits), and a Universal Analytics ID such as `UA-123456-1` is not supported.
- The **web stream ID**, a number. In **Admin**, open **Data streams** under **Data
  collection and modification**, select the web stream and read **Stream ID**. Google
  documents that finding it needs the **Editor** role or higher on the property; with a
  lower role, ask an Editor.
- The property's **reporting time zone** and **currency**. AdHelm compares them with what
  Google reports for the property and refuses to read on a mismatch.

## Scopes

For each of the two profiles AdHelm asks Google for exactly two scopes: `openid`, to bind
the grant to one Google user, and

```text
https://www.googleapis.com/auth/analytics.readonly
```

That scope lets AdHelm view reports and configuration and nothing else. AdHelm also has a
separate `analytics.edit` profile type for GA4 Admin edits; do not enroll it, because
`0.1.0` refuses every remote write and the broad edit scope would give Google-side
authority AdHelm cannot use.

## Redirect and callback

A **Desktop app** client needs no redirect URI configured in the console; Google's
console asks for nothing more when you create one. Each `enroll` opens your system
browser and listens once on `127.0.0.1` on a random free port for Google's single
callback, as Google's installed-app flow describes, then closes the listener. A local
firewall that blocks loopback connections will break it. AdHelm accepts a client file
whose `redirect_uris` are only `http://localhost` or `http://127.0.0.1`, which is what
Google writes into a Desktop client download.

## Access tiers and limits

- **Property role.** Your Google user needs access to the property in Analytics. Google
  describes the **Viewer** role as able to see settings and data, which is all AdHelm
  reads. Google's pages do not state a minimum role for each Admin API method, so if a
  read is refused with a permission error, check your role on the property first.
- **Quotas.** Data API core quotas are per property: a standard property gets 200,000
  tokens a day and 40,000 an hour with 10 concurrent requests; an Analytics 360 property
  gets ten times the tokens and 50 concurrent requests. Daily quotas reset at midnight
  Pacific time. The Admin API allows 1,200 requests a minute and 600 a minute
  per user. AdHelm's fixed reads are small, but a refused read is the provider's
  quota, not an AdHelm limit.
- **Consent screen status.** A project whose publishing status is `Testing` is limited to
  100 listed test users, and Google expires an authorization seven days after consent,
  including the refresh token. After that AdHelm cannot get a new access token for the
  profile: reconnect with `adhelm auth google-oauth reconnect` as the same Google user
  (it replaces the grant for the same user, client and scope only). Publishing the app
  (`In production`) removes the seven-day limit; an app that is not verified shows
  Google's "unverified app" warning during consent. A refresh token also stops working if
  it is unused for six months or you revoke it.

## Enroll

Create one profile draft for each of the two profiles. A draft holds no secret and no
Google subject. `account_ref` is the property ID; `credential_ref` is a local name that
must differ between the two profiles and from any other profile's.

```json guide:file=ga4-data-draft.json
{
  "profile_id": "ga4-read",
  "provider": "ga4",
  "account_ref": "123",
  "client_ref": "123456789012-exampleexampleexample.apps.googleusercontent.com",
  "scopes": ["https://www.googleapis.com/auth/analytics.readonly"],
  "purpose": "READ",
  "currency": "USD",
  "timezone": "America/Chicago",
  "credential_ref": "ga4-read-oauth"
}
```

```json guide:file=ga4-admin-draft.json
{
  "profile_id": "ga4-admin-read",
  "provider": "ga4_admin",
  "account_ref": "123",
  "client_ref": "123456789012-exampleexampleexample.apps.googleusercontent.com",
  "scopes": ["https://www.googleapis.com/auth/analytics.readonly"],
  "purpose": "READ",
  "currency": "USD",
  "timezone": "America/Chicago",
  "credential_ref": "ga4-admin-read-oauth"
}
```

Replace the example values in both: `account_ref` with your property ID, `client_ref` with
the `client_id` inside `client_secret.json` (AdHelm refuses the draft if it differs),
and `currency` and `timezone` with the property's own three-letter currency and IANA time
zone. `adhelm schema google-oauth-profile-draft` prints the draft's schema.

```sh guide:run
adhelm init
adhelm schema google-oauth-profile-draft --root-only
```

Enroll each profile. Each command opens your browser: sign in as the Google user with
access to the property and approve the consent screen, which will name only the Analytics
read-only scope.

```sh guide:run
adhelm auth google-oauth enroll --input ga4-data-draft.json --client-config client_secret.json --interactive
```

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "ga4",
    "purpose": "READ",
    "operation": "ENROLL",
    "resource_access_verified": false,
    "execution_authority": false
  }
}
```

```sh guide:run
adhelm auth google-oauth enroll --input ga4-admin-draft.json --client-config client_secret.json --interactive
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "ga4_admin", "purpose": "READ" } }
```

## Verify

Read the local credential state. This contacts no Google endpoint:

```sh guide:run
adhelm auth google-oauth status --profile ga4-read
```

```json guide:expect
{
  "data": {
    "provider": "ga4",
    "local_state": "READY",
    "granted_scopes": ["openid", "https://www.googleapis.com/auth/analytics.readonly"],
    "provider_authentication_verified": false
  }
}
```

`doctor` checks local readiness only and never calls a provider, so a configured profile
shows `CONFIGURED_UNVERIFIED`. With a GA4 profile enrolled it no longer reports GA4 as
missing:

```sh guide:run
adhelm doctor
```

```json guide:expect
{
  "outcome": "LOCAL_ONLY",
  "data": {
    "remote_checks_performed": false,
    "providers": [
      { "provider": "ga4", "status": "CONFIGURED_UNVERIFIED" },
      { "provider": "ga4_admin", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

The first Google requests are the two reads below. Use your own web stream ID in place of
`456`. The first reads the property's configuration through the Admin API and keeps it in
AdHelm's local history:

```sh guide:run
adhelm measurement ga4-admin ga4-admin-read 456
```

```json guide:expect
{
  "outcome": "CONFIGURATION_RETAINED",
  "data": {
    "provider": "ga4_admin",
    "evidence_kind": "CONFIGURATION_READ",
    "remote_mutations": false
  }
}
```

The second reads a report through the Data API. Create the report input, replacing
`property_id` and `account_ref` (both are the property ID), the dates with a window that
has ended in the property's time zone, and `source_timezone` and `currency` with the
profile's values. `metrics` may be any of `sessions`, `activeUsers`, `eventCount` and
`keyEvents`; `adhelm schema ga4-report` prints the schema, and
`adhelm schema ga4-aggregate-report` the fixed campaign and key-event shapes.
The input schema requires `query_contract_hash` as 64 lowercase hex characters, but
AdHelm ignores the value you supply and records its own digest of the query in the
report, so any well-formed value, such as the zeros shown, works.

```json guide:file=ga4-report.json
{
  "property_id": "123",
  "account_ref": "123",
  "report_id": "report.ga4.sessions",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "metrics": ["sessions"],
  "source_timezone": "America/Chicago",
  "currency": "USD"
}
```

```sh guide:run
adhelm report ga4 ga4-read 456 --input ga4-report.json
```

```json guide:expect
{ "outcome": "OK", "data": { "envelope": { "provider": "ga4" } } }
```

`history verify` re-checks the append-only local ledger that the configuration read and
the report were written to:

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
   and **Confirm**. Both profiles share one OAuth client, so this ends both grants. It
   also interrupts anything else that uses the same client. To retire the client's
   secret, rotate it on the **Google Auth Platform > Clients** page.
2. **On this machine.** Remove both stored credentials. This deletes only your local
   copies; it never revokes anything at Google and AdHelm cannot verify that you did
   (`remote_revocation_verified` is always `false`).

```sh guide:run
adhelm auth remove-local --profile ga4-read
adhelm auth remove-local --profile ga4-admin-read
adhelm auth google-oauth status --profile ga4-admin-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "primary_slot_present": false } }
```

Removal leaves your local history, configuration snapshots and reports in place. Delete
`client_secret.json` as well if you no longer need it.

## Sources

Fetched from Google's own documentation. Where Google's text and AdHelm's behavior
differ, this guide says so above.

- [Data API quickstart: prerequisites and scopes](https://developers.google.com/analytics/devguides/reporting/data/v1/quickstart-client-libraries) (fetched 2026-10-11; page last updated 2026-10-09)
- [Admin API quickstart: prerequisites and scopes](https://developers.google.com/analytics/devguides/config/admin/v1/quickstart-client-libraries) (fetched 2026-10-11; page last updated 2026-10-09)
- [Data API: finding the property ID](https://developers.google.com/analytics/devguides/reporting/data/v1/property-id) (fetched 2026-10-11; page last updated 2026-10-09)
- [Data API core quotas](https://developers.google.com/analytics/devguides/reporting/data/v1/quotas) (fetched 2026-10-11; page last updated 2026-10-09)
- [Admin API quotas](https://developers.google.com/analytics/devguides/config/admin/v1/quotas) (fetched 2026-10-11; page last updated 2026-10-09)
- [Stream ID](https://support.google.com/analytics/answer/12332343) (fetched 2026-10-11; no update date shown)
- [Roles and data-restriction access](https://support.google.com/analytics/answer/9305587) (fetched 2026-10-11; no update date shown)
- [OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2/native-app) (fetched 2026-10-11; page last updated 2026-09-14)
- [Using OAuth 2.0 to access Google APIs, refresh token expiration](https://developers.google.com/identity/protocols/oauth2) (fetched 2026-10-11; page last updated 2026-05-26)
- [Manage OAuth clients](https://support.google.com/cloud/answer/15549257) (fetched 2026-10-11; no update date shown)
- [Manage app audience](https://support.google.com/cloud/answer/15549945) (fetched 2026-10-11; no update date shown)
- [Remove a third-party app's access to your Google Account](https://support.google.com/accounts/answer/13533235) (fetched 2026-10-11; no update date shown)

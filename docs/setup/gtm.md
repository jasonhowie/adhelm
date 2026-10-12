# GTM setup

This guide connects AdHelm to one Google Tag Manager web container with your own Google
Cloud project. Tag Manager is optional: without a GTM profile every other workflow
continues, and `doctor --include-optional` lists it as `NOT_CONFIGURED_OPTIONAL`. AdHelm
reads the container's live and latest versions and its workspaces, and changes nothing
in it: staging, version creation and publishing are all refused in `0.1.0`
([release definition](../release-0.1.0.md)). A read here proves nothing about what a
visitor's browser runs. GTM is labelled `EXPERIMENTAL` until a live read pilot record for
it is committed.

Run the interactive step yourself, in a terminal on the machine whose browser you will
use; it opens your system browser. Do not paste credentials, the client file or its
contents into a chat with your AI agent; the agent can run every other command in this
guide.

Every command in a `guide:run` block below is run against a fake Tag Manager API in
AdHelm's test suite, so the commands, file names and flags in those blocks are checked
on each change ([how](README.md)). A command named only in running text is not run. The
fake cannot check Google's console, so the console steps are cited to Google's
documentation under [Sources](#sources), with the date each page was fetched.

## Create the developer app

You need a Google Cloud project that you control, one OAuth client in it, and a Google
user with access to the container. If you already created the client for
[Google Ads](google_ads.md#create-the-developer-app) or [GA4](ga4.md#create-the-developer-app),
reuse it and only add the scope below under **Data Access**.

1. Select or create a Google Cloud project and enable the **Tag Manager API**. Google's
   authorization page points to a setup tool that creates the project and enables the
   API in one step.
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

You also need two numbers. The Tag Manager API addresses a container as
`accounts/{accountId}/containers/{containerId}`, and both IDs are numbers. Neither is the
container's public ID (`GTM-` followed by letters and digits), which Google's reference
lists as a separate field, and AdHelm does not accept it. Google's documentation does not
say where the numbers appear in the Tag Manager web app. In practice they are the two
numbers in the address of a container's workspace page, after `accounts/` and
`containers/`; confirm them against the container you mean to read. AdHelm reads
**web** containers only and refuses any other kind.

## Scopes

AdHelm asks Google for exactly two scopes: `openid`, to bind the grant to one Google
user, and

```text
https://www.googleapis.com/auth/tagmanager.readonly
```

Google describes it as the scope to view containers. AdHelm refuses a GTM draft that names
any other scope, including the edit, version, publish and user-management scopes Google
also offers.

## Redirect and callback

A **Desktop app** client needs no redirect URI configured in the console; Google's
console asks for nothing more when you create one. `enroll` opens your system browser
and listens once on `127.0.0.1` on a random free port for Google's single callback, as
Google's installed-app flow describes, then closes the listener. A local firewall that
blocks loopback connections will break it. AdHelm accepts a client file whose
`redirect_uris` are only `http://localhost` or `http://127.0.0.1`, which is what Google
writes into a Desktop client download.

## Access tiers and limits

- **Container permission.** The OAuth grant does not give your Google user any Tag Manager
  access; it only lets AdHelm act as that user. The user needs at least the **Read**
  permission on the container, which lets them browse its tags, triggers and variables.
  Permissions are managed in Tag Manager under **Admin > User management**, and Tag
  Manager answers `403` to a user without access.
- **Quota.** Google limits a Cloud project to 10,000 Tag Manager API requests a day and
  0.25 requests a second, enforced over a 100-second window (25 requests per window), and
  answers an exhausted quota with `403`. A `403` therefore can mean missing permission, a
  disabled API or an exhausted quota; AdHelm reports all three and cannot tell them
  apart. The default read below is a handful of requests. The opt-in `--full-inventory`
  read is not paced and can reach the limit on a large container; see
  [the known limitation](../implementation/gtm-auth-lifecycle.md#known-limitation-the-full-inventory-read-is-not-paced).
- **Consent screen status.** A project whose publishing status is `Testing` is limited to
  100 listed test users, and Google expires an authorization seven days after consent,
  including the refresh token. After that AdHelm cannot get a new access token for the
  profile: reconnect with `adhelm auth google-oauth reconnect` as the same Google user
  (it replaces the grant for the same user, client and scope only). Publishing the app
  (`In production`) removes the seven-day limit; an app that is not verified shows
  Google's "unverified app" warning during consent. A refresh token also stops working if
  it is unused for six months or you revoke it.

## Enroll

Create the profile draft. It holds no secret and no Google subject. `account_ref` is the
numeric account ID; `credential_ref` is a local name that must differ from any other
profile's. A GTM draft takes no currency, developer token or manager account.

```json guide:file=gtm-draft.json
{
  "profile_id": "gtm-read",
  "provider": "gtm",
  "account_ref": "12345",
  "client_ref": "123456789012-exampleexampleexample.apps.googleusercontent.com",
  "scopes": ["https://www.googleapis.com/auth/tagmanager.readonly"],
  "purpose": "READ",
  "credential_ref": "gtm-read-oauth"
}
```

Replace `account_ref` with your account ID and `client_ref` with the `client_id` inside
`client_secret.json` (AdHelm refuses the draft if it differs).
`adhelm schema google-oauth-profile-draft` prints the draft's schema.

```sh guide:run
adhelm init
adhelm schema google-oauth-profile-draft --root-only
```

Enroll the OAuth grant. This opens your browser: sign in as the Google user with access
to the container and approve the consent screen, which will name only the Tag Manager
read-only scope.

```sh guide:run
adhelm auth google-oauth enroll --input gtm-draft.json --client-config client_secret.json --interactive
```

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "gtm",
    "purpose": "READ",
    "operation": "ENROLL",
    "secondary_credential_status": "NOT_APPLICABLE",
    "resource_access_verified": false,
    "execution_authority": false
  }
}
```

## Verify

Read the local credential state. This contacts no Google endpoint:

```sh guide:run
adhelm auth google-oauth status --profile gtm-read
```

```json guide:expect
{
  "data": {
    "provider": "gtm",
    "local_state": "READY",
    "granted_scopes": ["openid", "https://www.googleapis.com/auth/tagmanager.readonly"],
    "developer_token_slot_state": "NOT_REQUIRED",
    "provider_authentication_verified": false
  }
}
```

The first Tag Manager request is the read below. Use your own container's numeric ID in
place of `2`. AdHelm checks the account and container, reads the live and latest
versions and lists the container's workspaces, then keeps the counts and workspace
metadata in its local history:

```sh guide:run
adhelm measurement gtm gtm-read 2
```

```json guide:expect
{
  "outcome": "CONFIGURATION_RETAINED",
  "data": {
    "provider": "gtm",
    "evidence_kind": "CONFIGURATION_READ",
    "remote_mutations": false
  }
}
```

`history verify` re-checks the append-only local ledger that the read was written to:

```sh guide:run
adhelm history verify
```

```json guide:expect
{ "outcome": "OK", "data": { "status": "VERIFIED" } }
```

To retain every collection of the live and latest versions and each workspace, for the
dependency graph and audit, add `--full-inventory` to the same command. Mind the quota
note above.

## Revoke

Revoking has two separate halves, and AdHelm can do only one of them.

1. **At Google.** Open `myaccount.google.com/linkedapps`, choose **Access to your Google
   Account**, select the app you enrolled, choose **See details**, then **Remove access**
   and **Confirm**. This ends the grant. It also interrupts anything else that uses the
   same OAuth client. To retire the client's secret, rotate it on the **Google Auth
   Platform > Clients** page. Removing the grant does not remove your Google user's Tag
   Manager permissions, and removing those does not revoke the grant: change them in Tag
   Manager under **Admin > User management** if you want them gone.
2. **On this machine.** Remove the stored credential. This deletes only your local copy;
   it never revokes anything at Google and AdHelm cannot verify that you did
   (`remote_revocation_verified` is always `false`).

```sh guide:run
adhelm auth remove-local --profile gtm-read
adhelm auth google-oauth status --profile gtm-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "primary_slot_present": false } }
```

Removal leaves your local history and container snapshots in place. Delete
`client_secret.json` as well if you no longer need it.

## Sources

Fetched from Google's own documentation. Where Google's text and AdHelm's behavior
differ, this guide says so above.

- [Tag Manager API v2: authorization](https://developers.google.com/tag-platform/tag-manager/api/v2/authorization) (fetched 2026-10-11; page last updated 2026-07-30)
- [Tag Manager API v2: limits and quotas](https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas) (fetched 2026-10-11; page last updated 2026-07-16)
- [REST resource: accounts.containers](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers) (fetched 2026-10-11; page last updated 2025-03-06)
- [Tag Manager user permissions](https://support.google.com/tagmanager/answer/6107011) (fetched 2026-10-11; no update date shown)
- [OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2/native-app) (fetched 2026-10-11; page last updated 2026-09-14)
- [Using OAuth 2.0 to access Google APIs, refresh token expiration](https://developers.google.com/identity/protocols/oauth2) (fetched 2026-10-11; page last updated 2026-05-26)
- [Manage OAuth clients](https://support.google.com/cloud/answer/15549257) (fetched 2026-10-11; no update date shown)
- [Manage app audience](https://support.google.com/cloud/answer/15549945) (fetched 2026-10-11; no update date shown)
- [Remove a third-party app's access to your Google Account](https://support.google.com/accounts/answer/13533235) (fetched 2026-10-11; no update date shown)

# Meta Ads setup

This guide connects AdHelm to one Meta ad account with a Meta app you create yourself and
two access tokens you generate yourself. AdHelm reads the account (identity, campaign
hierarchy, daily metrics) and changes nothing in it: every remote write is refused in
`0.1.0` ([release definition](../release-0.1.0.md)). Meta Ads is labelled `EXPERIMENTAL`
until a live read pilot record for it is committed; the label and its meaning appear in
every output that names the provider.

Meta's flow differs from the OAuth providers. AdHelm has no browser handoff for Meta: you
generate the tokens in Meta's tools and paste each into a hidden prompt. Run that step
yourself, in a terminal. Do not paste a token or the app secret into a chat with your AI
agent; the agent can run every other command in this guide.

Every command in a `guide:run` block below is run against a fake Graph API in AdHelm's
test suite, so the commands, file names and flags in those blocks are checked on each
change ([how](README.md)). A command named only in running text is not run. The fake
cannot check Meta's App Dashboard or a live account, so the console steps are cited to
Meta's documentation under [Sources](#sources), with the date each page was fetched.

## Create the developer app

You need a Meta developer account, a Meta app, and a Facebook user who can open the ad
account.

1. Register as a Meta developer if you have not, sign in, and create an app in the App
   Dashboard. Meta lists a use case for it, **Measure ad performance data with Marketing
   API**; Meta's page says the app is created with a unique App ID and App Secret. Note
   the **App ID**. Keep the App Secret out of files and chat.
2. Find your ad account number in Ads Manager: open the account's **Settings** and read it
   in the **Account overview** section. In AdHelm it is digits only, without the `act_`
   prefix.
3. Generate a **user access token** with the `ads_read` permission, in the Graph API
   Explorer. Select your app, use the **User or Page** menu to get a user access token for it,
   and use the **Permission** menu to select only `ads_read`. The Explorer works for a
   user with an admin, developer or tester role on the app, and Meta's page for it says to
   grant only the permissions your app actually needs. This is the token AdHelm reads
   with.
4. Provide an **inspector token**, which AdHelm uses only to call Meta's `debug_token`: in
   `inspect`, and at the start of each `inventory` and `report`. Meta requires "an app access token or an app developer's user access
   token for the app" to call it. Choose one:
   - an **app access token**, which Meta builds from your App ID and App Secret (the
     `{app-id}|{app-secret}` form, or a token generated with the secret). It exposes your
     App Secret, so treat it as the most sensitive value in this guide. Meta warns that
     app access tokens are insecure for an app set to Native/Desktop.
   - an **app developer's user access token** for the same app, generated in the Explorer
     like the one above. It avoids handling the App Secret.

   Declare which kind you chose in `inspector_credential_kind` in [Enroll](#enroll).
5. Prefer a token that expires. Meta says short-lived user tokens last "about one to two
   hours" and long-lived ones "about 60 days", and gives a server-side call that exchanges
   one for the other using your App Secret ([Sources](#sources)). AdHelm supports
   expiring user tokens only: a token with no expiry, such as a system-user token, is
   refused with `META_TOKEN_EXPIRY_UNSUPPORTED`, and an expired one with
   `META_TOKEN_EXPIRED`.

## Scopes

AdHelm requires exactly one permission on the user token:

```text
ads_read
```

Meta's Marketing API pages describe it as the permission to read ad reports for ad
accounts you own or have been granted access to. AdHelm never asks for `ads_management`.
`adhelm auth meta-token inspect` checks that `ads_read` is present, that the token is a
`USER` token for your app, and how many other permissions it carries; it counts them
without naming them, and a `READ` profile never records `ads_management` even if the token
has it. That is bookkeeping: AdHelm cannot narrow the token. Grant only `ads_read` when
you generate it.

## Redirect and callback

None. AdHelm runs no OAuth flow for Meta and opens no browser or local listener; the
Graph API Explorer handles Meta's login and permission dialog. No redirect URL is needed
for the Explorer, so none is configured here.

## Access tiers and limits

Meta has two separate access concepts, which its Authorization page keeps apart. That
page says the tier labels were updated: "Standard Access" is now "Limited access" and
"Advanced Access" is now "Full access", while the per-permission levels keep the names
standard and advanced.

| Concept | Levels | What it means for read-only use |
| --- | --- | --- |
| Permission access level (per permission, per app) | Standard access, advanced access | Business apps get standard access automatically. Meta says standard access to `ads_read` and `ads_management` is sufficient if your app manages only your own ad account; managing other people's ad accounts needs advanced access, which requires App Review |
| Marketing API Access Tier (per app) | Limited access (default), Full access | Limited access is heavily rate-limited per ad account and is "for development only. Not for production apps running for live advertisers". Full access needs App Review, at least 500 Marketing API calls in the last 15 days and an error rate under 15% |

Meta also says Business Verification is required "if your app will access sensitive
data". AdHelm reads none of this: `adhelm auth meta-token status` reports
`app_review_status` and `business_verification_status` as `NOT_DOCUMENTED_LOCALLY`. If
reads are throttled, AdHelm reports `META_ADS_RATE_LIMITED`; the tier is Meta's setting
and not an AdHelm one.

## Enroll

Create the profile draft. It holds no token, and it names the app and the ad account but
no user. Currency and time zone are what you expect the account to report; AdHelm checks
them against Meta's answer and refuses to read on a mismatch. `adhelm schema
meta-token-profile-draft` prints the draft's schema.

```json guide:file=meta-ads-draft.json
{
  "schema_version": 1,
  "profile_id": "meta-read",
  "account_ref": "1234567890",
  "client_application_ref": "987654321012345",
  "currency": "USD",
  "timezone": "UTC",
  "purpose": "READ",
  "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN"
}
```

Replace the example values:

- `account_ref`: your ad account number, digits only.
- `client_application_ref`: your App ID.
- `currency` and `timezone`: the account's own three-letter currency code and IANA time
  zone.
- `inspector_credential_kind`: `CUSTOMER_APP_ACCESS_TOKEN` or
  `CUSTOMER_APP_DEVELOPER_USER_TOKEN`, matching the inspector token you chose.

Enroll the profile and its tokens. This prompts without echo, first for the user token
(`Meta USER token:`) and then for the inspector token (`Meta inspector token:`), then
stores them together as one bundle in your operating-system credential store. It makes no
request to Meta:

```sh guide:run
adhelm init
adhelm auth meta-token enroll --input meta-ads-draft.json --interactive
```

```json guide:expect
{
  "outcome": "OK",
  "data": {
    "provider": "meta_ads",
    "purpose": "READ",
    "operation": "ENROLL",
    "local_state": "READY",
    "provider_authentication_verified": false,
    "account_access_verified": false,
    "remote_mutations": false,
    "execution_authority": false
  }
}
```

## Verify

Read the local credential state. This contacts no Meta endpoint, and before the first
inspection it can say nothing about the token:

```sh guide:run
adhelm auth meta-token status --profile meta-read
```

```json guide:expect
{
  "data": {
    "local_state": "READY",
    "retained_inspection": "NOT_RETAINED",
    "token_validity": "NOT_INSPECTED_LOCALLY",
    "app_review_status": "NOT_DOCUMENTED_LOCALLY",
    "business_verification_status": "NOT_DOCUMENTED_LOCALLY"
  }
}
```

`inspect` makes the one `debug_token` request. It reports whether the token belongs to
your app, is a `USER` token and carries `ads_read`, and when it expires; it prints no
identifier, token or scope name. It does not prove the token can reach the ad account
(`account_access_verified` stays `false`); `inventory` below does.

```sh guide:run
adhelm auth meta-token inspect --profile meta-read
```

```json guide:expect
{
  "data": {
    "expected_application": "VERIFIED",
    "subject_type": "USER",
    "required_scope": "OBSERVED",
    "additional_granted_scope_count": 0,
    "declared_purpose": "READ",
    "account_access_verified": false
  }
}
```

`status` now reports what that inspection found. It keeps the expiry and refuses with
`META_TOKEN_EXPIRED` once the recorded expiry passes:

```sh guide:run
adhelm auth meta-token status --profile meta-read
```

```json guide:expect
{
  "data": {
    "retained_inspection": "MATCHES_INSTALLED_BUNDLE",
    "token_validity": "VALID_AT_LAST_INSPECTION",
    "purpose_clamped_scopes": ["ads_read"]
  }
}
```

`doctor` checks local readiness only. GA4 is required for AdHelm, so until you have
enrolled it (see [GA4 setup](ga4.md)) `doctor` exits `3` and reports GA4 as
`NOT_CONFIGURED_REQUIRED`; Meta Ads itself shows `CONFIGURED_UNVERIFIED`, because
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
      { "provider": "meta_ads", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

`inventory` reads the account's identity and its campaign, ad set and ad hierarchy, and
keeps the result in AdHelm's local history. It inspects the token again and checks the
account's identity first:

```sh guide:run
adhelm inventory meta_ads meta-read
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "meta_ads", "ephemeral": false } }
```

Then read a daily report. Create the report input (`adhelm schema meta-ads-report` prints
its schema), replacing `account_ref` with your ad account number and the dates with a
window that has ended in the account's time zone, and `source_timezone` with the
profile's. `action_attribution_windows` is required and takes `1d_click`, `7d_click` and
`1d_view`. The input schema requires `query_contract_hash` as 64 lowercase hex characters,
but AdHelm ignores the value you supply and records its own digest of the query in the
report, so any well-formed value, such as the zeros shown, works.

```json guide:file=meta-ads-report.json
{
  "account_ref": "1234567890",
  "report_id": "report.meta.campaign",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "source_timezone": "UTC",
  "action_attribution_windows": ["7d_click", "1d_view"],
  "level": "CAMPAIGN"
}
```

```sh guide:run
adhelm report meta-ads meta-read --input meta-ads-report.json
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

1. **At Meta.** Meta documents two ways to end a user token's permissions. In the Graph API
   Explorer, the **User or Page** menu can uninstall your app from your user, which
   destroys the current access token. And `DELETE /{user-id}/permissions/{permission-name}`
   revokes a permission when called with a user access token or an app access token for
   the app. AdHelm sends neither and never claims the token is revoked
   (`remote_revocation_verified` is always `false`). A short-lived user token also expires
   within hours on its own. If you used an app access token as the inspector token, it
   embeds your App Secret, so protect it as you would the secret. AdHelm's own revocation
   text points to Meta's Business Integrations settings, which this guide could not check.
2. **On this machine.** Remove the stored bundle, which holds both tokens. This deletes
   only your local copy, and only for the profile you name:

```sh guide:run
adhelm auth meta-token remove-local --profile meta-read
adhelm auth meta-token status --profile meta-read
```

```json guide:expect
{ "data": { "local_state": "MISSING", "retained_inspection": "NOT_RETAINED" } }
```

The first command deletes the profile's bundle from your operating-system credential store
and checks that it is gone. It also deletes that profile's entry in the
retained-inspection file, `meta-token-observations.json` in AdHelm's data directory, which
holds no token. Other Meta profiles keep their bundles and their retained inspections. It
makes no request to Meta, and it never decodes the stored bundle or uses either token. Its
output says whether a bundle and a retained inspection were found (`bundle_removed`,
`retained_inspection_removed`), so running it again reports `false` for both rather than a
deletion that did not happen.

The profile stays in your configuration, so `adhelm auth meta-token rotate` has nothing
left to replace and `enroll` refuses a profile ID that already exists. To connect the same
account again, enroll under a new `profile_id`.

Removal leaves your local history, inventory snapshots and reports in place.

## Sources

Fetched from Meta's own developer documentation. Where Meta's text and AdHelm's behavior
differ, this guide says so above.

- [Create an App with Meta](https://developers.facebook.com/docs/development/create-an-app) (fetched 2026-10-11; page last updated 2025-09-16)
- [Get started with the Marketing API](https://developers.facebook.com/docs/marketing-api/get-started) (fetched 2026-10-11; page last updated 2026-06-16)
- [Marketing API Authorization](https://developers.facebook.com/docs/marketing-api/overview/authorization) (fetched 2026-10-11; page last updated 2026-05-05)
- [Access Tokens for Meta Technologies](https://developers.facebook.com/docs/facebook-login/guides/access-tokens) (fetched 2026-10-11; page last updated 2025-11-25)
- [Long-lived access tokens](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived) (fetched 2026-10-11; page last updated 2026-06-30)
- [Graph API Explorer Guide](https://developers.facebook.com/docs/graph-api/guides/explorer) (fetched 2026-10-11; no update date shown)
- [Debug Token reference](https://developers.facebook.com/docs/graph-api/reference/debug_token) (fetched 2026-10-11; no update date shown)
- [User permissions reference](https://developers.facebook.com/docs/graph-api/reference/user/permissions/) (fetched 2026-10-11; no update date shown)

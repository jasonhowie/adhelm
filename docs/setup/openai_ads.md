# OpenAI Ads setup

This guide connects AdHelm to one OpenAI Ads ad account with an Advertiser API key you
issue yourself. AdHelm reads the account (identity, campaign hierarchy, daily metrics)
and changes nothing in it: every remote write is refused in `0.1.0`
([release definition](../release-0.1.0.md)). OpenAI Ads is labelled `EXPERIMENTAL` until
a live read pilot record for it is committed; the label and its meaning appear in every
output that names the provider.

Run the interactive step yourself, in a terminal. It prompts for the key without echo.
Do not paste the key into a chat with your AI agent; the agent can run every other
command in this guide.

Every command in a `guide:run` block below is run against a fake OpenAI Ads API in
AdHelm's test suite, so the commands, file names and flags in those blocks are checked
on each change ([how](README.md)). A command named only in running text is not run. The
fake cannot check OpenAI's Ads Manager, so the console steps are cited to OpenAI's
documentation under [Sources](#sources), with the date each page was fetched.

## Create the developer app

OpenAI Ads has no developer application to register. You need an OpenAI Ads ad account
and one Advertiser API key for it.

1. Sign in to Ads Manager (`ads.openai.com`) with a user who can use the ad account.
   OpenAI's documentation does not say which account roles may issue a key; if the
   Settings page does not offer one to you, ask the account's administrator.
2. Open the **Settings** page and create an Advertiser API key. Keep it private to your
   user. You will type it into a hidden prompt in [Enroll](#enroll); nothing else needs
   it.
3. Find the account's ID, currency and time zone. OpenAI documents one request that
   returns them and confirms the key works, `GET /v1/ad_account`, whose response carries
   `id`, `currency_code` and `timezone`. AdHelm cannot make this request for you: it has
   no command that reports an OpenAI account's ID, currency or time zone before a profile
   exists, so you run it yourself, outside AdHelm. This is a known gap in `0.1.0`, to be
   closed by a later release. Keep the key out of the command line, where other users of
   the machine can read it with `ps`, and out of your shell history. In a Unix shell, type
   it into a hidden prompt, send the header to `curl` on its standard input, then discard
   the key:

   ```sh
   printf 'Advertiser API key: '; read -rs OPENAI_ADS_API_KEY; echo
   printf 'Authorization: Bearer %s\n' "$OPENAI_ADS_API_KEY" |
     curl -G -H @- "https://api.ads.openai.com/v1/ad_account"
   unset OPENAI_ADS_API_KEY
   ```

   In Windows PowerShell, use `curl.exe` (not the `curl` alias):

   ```powershell
   $secure = Read-Host -AsSecureString 'Advertiser API key'
   $key = [System.Net.NetworkCredential]::new('', $secure).Password
   "Authorization: Bearer $key" | curl.exe -G -H "@-" "https://api.ads.openai.com/v1/ad_account"
   Remove-Variable key, secure
   ```

   These two sequences are not run by AdHelm's tests, which reach no provider. Read only
   the three fields. The response also carries the account's name and URL, so do not paste
   it into a chat with your AI agent.

   An account marked `active` can still have an outstanding review, which OpenAI's
   overview tells you to inspect before you create a campaign; AdHelm does not read it.

A key is scoped to one ad account, so a second account needs a second key and a second
profile.

## Scopes

OpenAI documents no permission scopes for the key. Its Authentication reference says
only that a key is sent as a bearer token and that "each Ads API key is scoped to one ad
account". It describes no read-only key. Assume the key can do everything OpenAI lets an
account key do, including changing campaigns.

AdHelm's profile therefore records `provider_scope_status` as `NOT_DOCUMENTED` and makes
no least-privilege claim. `purpose: READ` is AdHelm's own gate: the commands in this
guide only send `GET` requests, and `0.1.0` refuses every remote write before it reads a
credential. The key you issue is still broad, which is the reason to retire it when you
are done (see [Revoke](#revoke)).

## Redirect and callback

None. An OpenAI Ads key involves no OAuth consent, browser handoff, redirect URL or
callback, and AdHelm opens no local listener for it. The only interaction is the hidden
prompt in [Enroll](#enroll).

## Access tiers and limits

OpenAI documents no access tiers or approval step for an account key. It documents one
limit that matters for reading, enforced by both ad account and IP address:

| Scope | Limit |
| --- | --- |
| Per endpoint | 600 requests per minute |
| Overall | 1,200 requests per minute |

OpenAI documents no key expiry, rotation endpoint or revocation route (see
[Revoke](#revoke)), and no test or sandbox ad account, so AdHelm's fixtures are the only
evidence for this provider until the account owner runs a live read pilot.

## Enroll

Create the profile. It holds no secret: `credential_ref` is the local name of the slot
that will hold the key in your operating-system credential store.
`adhelm schema openai-account-key-profile` prints the profile's schema.

```json guide:file=openai-ads-profile.json
{
  "schema_version": 1,
  "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
  "provider": "openai_ads",
  "provider_scope_status": "NOT_DOCUMENTED",
  "profile_id": "openai-read",
  "account_ref": "account_fixture",
  "purpose": "READ",
  "currency": "USD",
  "timezone": "UTC",
  "credential_source": "OS_STORE",
  "credential_ref": "openai-read-key"
}
```

Replace the example values:

- `account_ref`: the `id` the account returned in step 3 above. AdHelm refuses to read if
  the account the key resolves to is a different one.
- `currency` and `timezone`: the account's own three-letter currency code and IANA time
  zone. AdHelm compares them with what OpenAI reports and refuses to read on a mismatch.
- `credential_ref`: a local name that no other profile uses. It is not the key.

Add the profile, then store the key. The prompt does not echo what you type, and `store`
refuses to overwrite a key that is already stored.

```sh guide:run
adhelm init
adhelm config add-profile --input openai-ads-profile.json
adhelm auth store --profile openai-read --interactive
```

```json guide:expect
{ "data": { "local_only": true, "slots": [{ "slot": "PRIMARY", "state": "PRESENT" }] } }
```

## Verify

Read the local credential state. This contacts no OpenAI endpoint:

```sh guide:run
adhelm auth openai-key status --profile openai-read
```

```json guide:expect
{
  "data": {
    "key_slot_present": true,
    "local_state": "UNVERIFIED",
    "provider_scope_status": "NOT_DOCUMENTED",
    "provider_revocation_verified": false,
    "remote_mutations": false
  }
}
```

`UNVERIFIED` is the correct answer here. AdHelm keeps a verification record for a key
only when the key is installed with `adhelm auth openai-key rotate --profile openai-read
--interactive`, which reads `GET /v1/ad_account` with the replacement key before it
replaces the old one. A first key stored with `auth store` has no such record, and
`rotate` refuses a replacement equal to the stored key. The first proof that the key
works is the `inventory` below.

`doctor` checks local readiness only. GA4 is required for AdHelm, so until you have
enrolled it (see [GA4 setup](ga4.md)) `doctor` exits `3` and reports GA4 as
`NOT_CONFIGURED_REQUIRED`; OpenAI Ads itself shows `CONFIGURED_UNVERIFIED`, because
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
      { "provider": "openai_ads", "status": "CONFIGURED_UNVERIFIED" }
    ]
  }
}
```

The first OpenAI request is `inventory`. It reads the account's identity and its
campaign, ad group and ad hierarchy, and keeps the result in AdHelm's local history:

```sh guide:run
adhelm inventory openai_ads openai-read
```

```json guide:expect
{ "outcome": "OK", "data": { "provider": "openai_ads", "ephemeral": false } }
```

Then read a daily report. Create the report input (`adhelm schema openai-report` prints
its schema), replacing `account_ref` with your account ID, the dates with a window that
has ended in the account's time zone, and `currency` and `source_timezone` with the
profile's values. The input schema requires `query_contract_hash` as 64 lowercase hex
characters, but AdHelm ignores the value you supply and records its own digest of the
query in the report, so any well-formed value, such as the zeros shown, works.

```json guide:file=openai-ads-report.json
{
  "account_ref": "account_fixture",
  "report_id": "report.openai.campaign",
  "query_contract_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "window_start": "2026-09-01",
  "window_end": "2026-09-07",
  "source_timezone": "UTC",
  "currency": "USD",
  "level": "CAMPAIGN"
}
```

```sh guide:run
adhelm report openai-ads openai-read --input openai-ads-report.json
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

1. **At OpenAI.** OpenAI documents where a key is issued, the Settings page in Ads
   Manager, and nothing about retiring one: no expiry, no rotation endpoint, no
   revocation screen and no revocation route appear in the pages cited below. Look for
   key management on the same Settings page. If it offers no way to delete the key, ask
   OpenAI, and treat the key as live until you have that in writing. AdHelm cannot read
   the key's state at OpenAI and never claims it is revoked
   (`provider_revocation_verified` is always `false`).
2. **On this machine.** Remove the stored key. This deletes the key and AdHelm's
   verification record for it from your credential store; it never revokes anything at
   OpenAI.

```sh guide:run
adhelm auth remove-local --profile openai-read
adhelm auth openai-key status --profile openai-read
```

```json guide:expect
{ "data": { "key_slot_present": false, "local_state": "MISSING" } }
```

Removal leaves your local history, inventory snapshots and reports in place.

## Sources

Fetched from OpenAI's own documentation. Where OpenAI's text and AdHelm's behavior
differ, this guide says so above.

- [Advertiser API overview](https://developers.openai.com/ads/api-overview) (fetched 2026-10-11; no update date shown)
- [Authentication reference](https://developers.openai.com/ads/api-reference/authentication) (fetched 2026-10-11; no update date shown)

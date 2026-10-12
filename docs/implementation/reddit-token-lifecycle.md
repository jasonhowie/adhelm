# Reddit Ads local token lifecycle

This describes the implemented local OAuth lifecycle for a configured Reddit
Ads profile: enroll, refresh, status, and local removal with revocation
guidance. The pinned wire contract and every `UNVERIFIED` item behind it live in
[`docs/research/reddit-oauth-contract.md`](../research/reddit-oauth-contract.md).

## Evidence limits

- **Fixture-only.** No live Reddit OAuth request has been made from this
  repository. Every test uses a fake credential store and either a fake token
  seam or `httpx.MockTransport`; the default test suite forbids outbound sockets.
- **No application or role is implied.** A successful local enrollment proves
  only that a token response was accepted and that the configured ad account
  answered one identity read. Results report `app_review_verified: false` and
  `account_role_verified: false`.
- **No mutation capability.** Nothing in this lifecycle creates, changes, or
  activates an advertising resource, and nothing revokes a grant at Reddit.
  Results report `remote_mutations: false` and `execution_authority: false`.
- **Two likely first-live-attempt failures are unproven.** (1) Whether Reddit
  accepts the exchange as sent: the developer application's ID and secret as HTTP
  Basic, which Reddit's Authenticate page shows as `-u '<app ID>:<secret>'`. If it
  does not, enrollment fails at the token exchange with `OAUTH_TOKEN_REJECTED`
  (HTTP 400/401/403) or `OAUTH_TOKEN_ERROR_RESPONSE` (an error object such as
  `invalid_client` returned in a 200 body) and stores nothing. (2) Whether
  Reddit accepts the literal-`127.0.0.1` loopback redirect. See the research
  document; neither has been exercised against Reddit.

## Prerequisites

1. A Reddit developer application that the customer created in their own Reddit
   Ads business, with its client ID and client secret available locally. The
   [setup guide](../setup/reddit_ads.md) walks through creating it.
2. A configured `reddit_ads` profile with `credential_source: OS_STORE`,
   `purpose: READ` or `EDIT`, `scopes` inside the per-purpose allowlist, and
   `account_ref` set to the Reddit ad account ID. The lifecycle never creates or
   edits a profile.
3. A redirect URI registered on the application that is exactly
   `http://127.0.0.1:<port>/oauth/callback`, using the same `<port>` passed to
   `--redirect-port`.

## Credential slots

Four distinct slots, all in the operating-system credential store:

| Slot | Service | Selector | Contents |
| --- | --- | --- | --- |
| Bearer | `adhelm` | the profile's own `credential_ref` | access token |
| Client secret | `adhelm.reddit-token-v1` | derived from the profile ID | the developer application's secret |
| Refresh token | `adhelm.reddit-token-v1` | derived from the profile ID | permanent refresh token, when granted |
| State | `adhelm.reddit-token-v1` | derived from the profile ID | non-secret canonical JSON: client ID, granted scopes, expiries, refresh status, identity salt and pseudonym, local state |

The bearer stays in the profile's ordinary slot so the existing read adapter,
`auth status` and `auth remove-local` keep working unchanged. The other three live
under a private service with a derived selector containing `#`, a character no
configured credential reference can contain, and the prefix is a reserved
credential reference. A refresh token can therefore never be written where a
bearer is read. The secret has a slot because every refresh sends it again; it is
never kept in the state slot and never accepted from an argument or the environment.

## Commands

All four are agent-facing and emit the standard bounded result envelope. None of
them prints, logs, or returns a client ID, client secret, access token, refresh
token, credential reference, or raw ad account ID.

### `auth reddit-token enroll --profile <id> --redirect-port <port> [--scopes <scope>]... --interactive`

1. Requires an explicit `--interactive` handoff and a local terminal.
2. Selects the configured profile and resolves the scope set from the closed
   per-purpose allowlist. The default is the least-privilege `adsread`; `adsedit`
   is admitted only for an `EDIT` profile and only when named explicitly.
3. Snapshots all four slots and requires every one of them to be empty, before
   any prompt or browser handoff.
4. Prompts, without echo, for the client ID and then the client secret. An empty
   or oversized secret, or one with control characters or spaces, is refused here
   with `SECRET_VALUE_INVALID`. A client ID that is empty, longer than 200
   characters, contains a space, a control character or a non-ASCII character, or
   contains a colon (which would move the HTTP Basic split) is refused with
   `REDDIT_TOKEN_CLIENT_ID_INVALID`, exit code 2, before any browser handoff.
5. Binds the loopback listener on the requested port and the fixed
   `/oauth/callback` path, opens the system browser at Reddit's authorization
   endpoint with a fresh 32-byte `state` and `duration=permanent`, and consumes
   exactly one callback.
6. Exchanges the code at Reddit's token endpoint as a confidential client: HTTP
   Basic with the client ID as the user name and the client secret as the
   password, neither in the form body.
7. Reads the configured ad account's identity with the new bearer and requires
   the returned account ID to match the profile.
8. Writes the state slot with a durable `ENROLL_STARTED`, then the client
   secret, the refresh token and the bearer, then rewrites the state slot as
   `READY`. Every write is read back exactly; an unverifiable write reports
   `CREDENTIAL_LOCAL_CHANGE_UNVERIFIED` with exit code 6 rather than success.

If Reddit returns no refresh token the state records `REFRESH_NOT_GRANTED` and
the one-hour bearer cannot be renewed; `refresh` then fails with exit code 4 and
that exact code rather than silently re-authorizing.

**Recorded permissions never exceed the requested set.** A granted scope echo
wider than the request is clamped to the requested set and the excess is reported
as `unrequested_granted_scopes`. A `READ` profile additionally refuses outright to
record `adsedit`, with `REDDIT_TOKEN_SCOPE_NOT_RECORDABLE`. As with LinkedIn, this
is local bookkeeping, not a narrowing of the grant at Reddit.

### `auth reddit-token refresh --profile <id>`

1. Under the credential locks: validates local state, requires
   `refresh_status: GRANTED`, reads the refresh token and the client secret (a
   missing secret slot is `REDDIT_TOKEN_ENROLLMENT_REQUIRED`, an unusable one
   `REDDIT_TOKEN_STATE_INVALID`, both before any request), writes a durable
   `REFRESH_STARTED`, then **releases every lock**.
2. With no lock held: posts the refresh grant, authenticated with the same HTTP
   Basic client ID and stored secret, then re-reads the ad account
   identity with the new bearer and compares its salted pseudonym with the
   enrolled one.
3. Reacquires the locks only to store a verified result: the new bearer, a
   rotated refresh token if one is returned (Reddit documents none), and a
   `READY` state.
4. Any failure between steps 1 and 3 leaves the previous bearer untouched. A
   failure the token transport classifies `NOT_DISPATCHED` provably issued no
   request, so the durable start is undone and the profile stays `READY`. Every
   other outcome -- a definite provider rejection, an uncertain dispatch, a
   failed or mismatched identity read -- marks the profile
   `REAUTHORIZATION_REQUIRED`, because the grant may have been consumed or the
   account may have changed.

A credential mutation lock is never held across an HTTP call; the tests assert
this by interleaving recorded lock and HTTP events.

### `auth reddit-token status --profile <id>`

Local only; it contacts no provider. Returns the profile configuration digest,
purpose, local state, granted scope list, access-token expiry and whether it has
passed, refresh-token expiry (null unless Reddit stated one), refresh status, the
identity pseudonym, which slots are present, and the revocation guidance. It
contains no secret and no credential reference. A ready state whose bearer slot
is empty is reported `UNVERIFIED`, never `READY`.

The identity pseudonym is `reddit_ads_account_v1.<hex>`, an HMAC of the ad
account ID under a random per-profile salt generated at enrollment.

### `auth reddit-token remove-local --profile <id>`

Deletes only the three Reddit-owned protected slots -- client secret, refresh
token and state -- and reports `LOCAL_REMOVE_ONLY` with
`remote_revocation_verified: false`. It leaves the profile's bearer slot to the
shared `auth remove-local` command, and a partially installed enrollment must be
cleared with both before a retry; the `REDDIT_TOKEN_CREDENTIAL_EXISTS` diagnostic
names both.

## Revocation guidance

Provider-side revocation and local removal are separate acts, and the local
commands never claim the former. This build does not call Reddit's revocation
endpoint. To revoke, sign in to Reddit as the enrolling user, look for the
application in the account's authorized-applications settings, and revoke it or
delete any app registration you created. That guidance has not been checked
against Reddit's current account pages, and the CLI strings say so (see the
research document). Revoking a shared application's grant can also interrupt
other uses of it, so identify the exact application first.

## Diagnostics and exit codes

| Situation | Code | Exit |
| --- | --- | --- |
| Callback `state` does not match the local attempt | `OAUTH_CALLBACK_STATE_MISMATCH` | 4 |
| User cancelled or denied the authorization | `OAUTH_AUTHORIZATION_DENIED` | 4 |
| Bounded browser window expired | `OAUTH_AUTHORIZATION_EXPIRED` / `OAUTH_CALLBACK_EXPIRED` | 4 |
| Provider granted fewer permissions than requested | `OAUTH_SCOPE_NOT_GRANTED` | 4 |
| Reddit rejected the token exchange or refresh (400/401/403) | `OAUTH_TOKEN_REJECTED` | 4 |
| Provider returned an OAuth error object instead of a token | `OAUTH_TOKEN_ERROR_RESPONSE` | 4 |
| Token outcome could not be classified (429, 5xx, redirect, malformed) | `OAUTH_TOKEN_OUTCOME_UNCERTAIN` / `OAUTH_TOKEN_RESPONSE_UNSUPPORTED` | 6 |
| No refresh token was returned | `REFRESH_NOT_GRANTED` | 4 |
| Stored refresh token is past a recorded expiry | `REDDIT_TOKEN_REFRESH_EXPIRED` | 4 |
| Profile blocked after a failed refresh | `REDDIT_TOKEN_REAUTHORIZATION_REQUIRED` | 4 |
| Refreshed credential resolves to a different ad account | `REDDIT_TOKEN_IDENTITY_MISMATCH` | 4 |
| Refresh returned fewer permissions than were enrolled | `REDDIT_TOKEN_SCOPE_REDUCED` | 4 |
| No complete local enrollment for this profile | `REDDIT_TOKEN_ENROLLMENT_REQUIRED` | 4 |
| A local credential write could not be read back | `CREDENTIAL_LOCAL_CHANGE_UNVERIFIED` | 6 |
| Provider granted a permission this purpose may never record | `REDDIT_TOKEN_SCOPE_NOT_RECORDABLE` | 4 |
| Stored lifecycle state unreadable or outside the allowlist | `REDDIT_TOKEN_STATE_INVALID` | 4 |
| Profile configuration changed after enrollment | `REDDIT_TOKEN_PROFILE_CHANGED` | 4 |
| Requested scopes outside the profile's allowlist | `REDDIT_TOKEN_SCOPE_REJECTED` | 2 |
| Entered client ID is not a usable OAuth client identifier | `REDDIT_TOKEN_CLIENT_ID_INVALID` | 2 |
| Entered client secret is empty, oversized or has spaces or control characters | `SECRET_VALUE_INVALID` | 2 |
| `--redirect-port` missing or unregisterable | `REDDIT_TOKEN_REDIRECT_PORT_INVALID` | 2 |
| Profile account reference is not a Reddit ad account ID | `REDDIT_TOKEN_ACCOUNT_REF_INVALID` | 2 |
| Existing local slot blocks a new enrollment | `REDDIT_TOKEN_CREDENTIAL_EXISTS` | 3 |

Denials from the Ads API (HTTP 401/403), whether in the enrollment identity read
or in a later `inventory` or `report reddit-ads`, keep the shared
`PROVIDER_ACCESS_REQUIRED` code and exit code every provider uses, with the HTTP
status attached. Only the next action is Reddit-specific: a `401` points an
`OS_STORE` profile enrolled with `auth reddit-token enroll` at
`auth reddit-token status` and `auth reddit-token refresh`, because an access
token lasts one hour, and says that an `EXPLICIT_CI` token has no local refresh
and must be replaced by whoever configured it (`select_reddit_ads_profile`
refuses any non-`OS_STORE` profile for these commands). A `403` says a refresh
will not change a permission or account-role refusal. Any other error, including
a transport failure, is re-raised untouched with its original exception chain.

## Egress

Two destinations, both HTTPS with no redirects followed and no ambient
environment configuration:

- `www.reddit.com` -- the OAuth authorization and token endpoints. The token POST
  uses the fixed-endpoint OAuth transport rather than the shared `SafeHttpClient`,
  because an OAuth token form carries `refresh_token` field names that the shared
  transport's generic privacy boundary correctly rejects.
- `ads-api.reddit.com` -- the fixed account identity read, through the ordinary
  `SafeHttpClient` allowlist.

[`docs/egress-map.md`](../egress-map.md) carries a dedicated row for the Reddit
OAuth endpoints, separate from the Reddit Ads API row.

## Known local risks

- **Denial of enrollment on the fixed callback port.** Reddit matches a
  registered redirect exactly, so this flow binds one operator-chosen port. Any
  other process running as the same OS user can hold that port and prevent
  enrollment, which surfaces as `OAUTH_LOOPBACK_UNAVAILABLE`. This is a
  denial-of-enrollment risk, not a credential-exposure one: the listener accepts
  only `127.0.0.1` peers, only the exact path and `Host`, and rejects any request
  with an `Origin` header or a body, and the code is bound to a fresh 32-byte
  `state` that a squatting process cannot predict.
- **A permanent refresh token is long-lived.** It has no expiry, so it stays
  valid until revoked at Reddit. Local removal does not revoke it; use the
  provider-side guidance when retiring a profile.
- **The application secret outlives local removal.** `remove-local` deletes the
  stored copy; the secret stays valid at Reddit, and anyone who copied it can still
  use it with the application. Retire it by deleting the application in the
  Developer Portal, which also cuts off every other use of that application.
- **A partially installed enrollment blocks retries** until both removal commands
  have run.

## Shared lifecycle module

The protected-slot state machine -- slot derivation and ownership, lock ordering,
read-back verification, and the `ENROLL_STARTED` / `REFRESH_STARTED` /
`REAUTHORIZATION_REQUIRED` journal -- exists once, in
`src/adhelm/protected_token_lifecycle.py`, with its CLI output projections and
command group in `src/adhelm/protected_token_cli.py` and slot-selector derivation
in `src/adhelm/protected_token_references.py`. LinkedIn and Reddit are two
`TokenProviderSpec` instances of it (credential-store service, reserved reference
prefix, slot set, account-ID validator, pseudonym domain, edit-scope name and
diagnostic-code prefix); `reddit_token_credentials.py` and
`linkedin_token_credentials.py` are thin wrappers that bind the credential
backend, mutation lock and clock seams. A lifecycle fix, such as the Windows
credential locks in O3.1, is made in one place. The two provider workflow
modules (`*_token_workflow.py`) remain separate because their browser and
identity steps genuinely differ.

## Shared OAuth module

`src/adhelm/oauth_enrollment.py` carries the provider-parameterized mechanics. Reddit
added two contract fields, both defaulting to the existing behavior so LinkedIn is
unchanged: `client_authentication` (`FORM_SECRET`, the client ID and secret in the
form body, or `BASIC_SECRET`, the same pair as the HTTP Basic user name and
password, Reddit's `-u '<app ID>:<secret>'`) and `user_agent`. It also made
the `bearer` token type case-insensitive.

# LinkedIn Ads local token lifecycle

This describes the implemented local OAuth lifecycle for a configured LinkedIn
Ads profile: enroll, refresh, status, and revocation guidance. The pinned wire
contract and every UNVERIFIED item behind it live in
[`docs/research/linkedin-oauth-contract.md`](../research/linkedin-oauth-contract.md).

## Evidence limits

- **Fixture-only.** No live LinkedIn OAuth request has been made from this
  repository. Every test uses a fake credential store and `httpx.MockTransport`;
  the default test suite forbids outbound sockets.
- **No app review is implied.** LinkedIn vets Advertising API access separately
  from member consent. A successful local enrollment proves only that a token
  response was accepted and that the configured ad account answered one identity
  read.
- **No account-role claim.** A granted scope does not establish an ad-account
  role. Results report `app_review_verified: false` and
  `account_role_verified: false`.
- **No mutation capability.** Nothing in this lifecycle creates, changes, or
  activates an advertising resource. Results report `remote_mutations: false`
  and `execution_authority: false`.
- **The loopback redirect is unproven against LinkedIn.** See the research
  document; this is the most likely reason a first live attempt fails.

## Prerequisites

1. A LinkedIn developer application the customer owns, approved for the
   Advertising API, with its client ID and client secret available locally.
2. A configured `linkedin_ads` profile with `credential_source: OS_STORE`,
   `purpose: READ` or `EDIT`, and `account_ref` set to the numeric LinkedIn ad
   account ID. The lifecycle never creates or edits a profile.
3. A registered redirect URL on the application's Auth tab that is exactly
   `http://127.0.0.1:<port>/oauth/callback`, using the same `<port>` passed to
   `--redirect-port`.

## Credential slots

Four distinct slots, all in the operating-system credential store:

| Slot | Service | Selector | Contents |
| --- | --- | --- | --- |
| Bearer | `adhelm` | the profile's own `credential_ref` | access token |
| Client secret | `adhelm.linkedin-token-v1` | derived from the profile ID | application client secret |
| Refresh token | `adhelm.linkedin-token-v1` | derived from the profile ID | programmatic refresh token, when granted |
| State | `adhelm.linkedin-token-v1` | derived from the profile ID | non-secret canonical JSON: client ID, granted scopes, expiries, refresh status, identity salt and pseudonym, local state |

The bearer deliberately stays in the profile's ordinary slot so the existing
read adapter, `auth status`, and `auth remove-local` keep working unchanged.
Everything else is under a private service with a derived selector that contains
`#`, a character no configured credential reference can contain, so a derived
selector can never collide with a customer-chosen one.

**This slot separation is the durable fix for the pilot's wrong-slot 401.** A
refresh token can no longer be written where a bearer is read, because the
refresh token is never entered by hand and never shares the bearer's selector.

## Commands

All four are agent-facing and emit the standard bounded result envelope. None of
them prints, logs, or returns a client ID, client secret, access token, refresh
token, credential reference, or raw ad account ID.

### `auth linkedin-token enroll --profile <id> --redirect-port <port> [--scopes <scope>]... --interactive`

1. Requires an explicit `--interactive` handoff and two local terminals.
2. Selects the configured profile and resolves the scope set from the closed
   per-purpose allowlist. Default is the least-privilege read pair
   `r_ads r_ads_reporting`. `rw_ads` is admitted only for an `EDIT` profile and
   only when named explicitly, for a future edit profile.
3. Snapshots all four slots and requires every one of them to be empty.
4. Prompts, without echo, for the client ID and then the client secret. Both
   prompts refuse to run unless stdin and stderr are both terminals, and a
   `getpass` fallback warning is treated as a failed prompt.
5. Binds the loopback listener on the requested port and the fixed
   `/oauth/callback` path, opens the system browser at LinkedIn's authorization
   endpoint with a fresh 32-byte `state`, and consumes exactly one callback.
6. Exchanges the code at LinkedIn's token endpoint as a confidential client.
7. Reads the configured ad account's identity with the new bearer and requires
   the returned account ID to match the profile.
8. Writes the state slot with a durable `ENROLL_STARTED`, then the client
   secret, refresh token and bearer, then rewrites the state slot as `READY`.
   Every write is read back exactly; an unverifiable write reports
   `CREDENTIAL_LOCAL_CHANGE_UNVERIFIED` with exit code 6 rather than success.

**Recorded permissions never exceed the requested set.** A provider may echo a
`scope` value wider than this authorization asked for, because the member
consented to more through some other application. The recorded contract is
clamped to exactly the requested set and the excess is reported separately as
`unrequested_granted_scopes` in the result; it is never stored as local
authority. A second, provider-specific check refuses outright to record `rw_ads`
against a `READ` profile, with `LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE`. A refresh
applies the same rule in the stricter direction: it renews an existing consent,
so it can never widen the recorded set and still fails closed on a narrower one.
The same allowlist is enforced when reading the state slot back, so a recorded
permission this purpose may never hold is reported `UNVERIFIED` rather than
`READY`, whoever wrote it.

**What the clamp does and does not do.** It limits only what this product
records and what it will enforce locally. It does not narrow the grant at
LinkedIn: if the member consented to a wider set, the bearer stored in the
profile's slot still carries that wider consent, and any other tool holding that
token could exercise it. The only thing preventing this product from performing
a LinkedIn write today is that no LinkedIn write path exists in this codebase at
all. Treat the clamp as least-privilege bookkeeping and a guard against a future
write path binding to an unintended permission, never as a claim that the token
itself is read-only. To actually narrow the grant, change the requested scope
set and re-authorize — LinkedIn documents that this invalidates previously
issued access tokens — or remove the application under Permitted Services.

### `auth linkedin-token refresh --profile <id>`

1. Under the credential locks: validates local state, requires
   `refresh_status: GRANTED`, reads the client secret and refresh token, writes
   a durable `REFRESH_STARTED`, then **releases every lock**.
2. With no lock held: posts the refresh grant, then re-reads the ad account
   identity with the new bearer and compares its salted pseudonym with the
   enrolled one.
3. Reacquires the locks only to store a verified result: the new bearer, a
   rotated refresh token when one is returned, and a `READY` state.
4. Any failure between steps 1 and 3 leaves the previous bearer untouched. A
   failure that the token transport classifies `NOT_DISPATCHED` provably issued
   no request, so the durable start is simply undone and the profile stays
   `READY` and refreshable. Every other outcome — a definite provider rejection,
   an uncertain dispatch, a failed or mismatched identity read — marks the
   profile `REAUTHORIZATION_REQUIRED`, because the grant may have been consumed
   or the account may have changed.

A credential mutation lock is never held across an HTTP call. This is asserted
directly in the tests by interleaving recorded lock and HTTP events.

### `auth linkedin-token status --profile <id>`

Local only; it contacts no provider. Returns the profile configuration digest,
purpose, local state, granted scope list, access-token expiry and whether it has
passed, refresh-token expiry, refresh status, the identity pseudonym, which
slots are present, and the revocation guidance. It contains no secret and no
credential reference.

The `enroll` and `refresh` result envelopes carry the same identity and expiry
fields plus `operation`, `granted_scopes`, and `unrequested_granted_scopes` --
the permissions the provider echoed that this authorization never requested,
reported for review and never recorded as local authority. Both also carry the
standing honesty flags `account_identity_verified`, `app_review_verified`,
`account_role_verified`, `remote_mutations`, and `execution_authority`.

The identity pseudonym is `linkedin_ads_account_v1.<hex>`, an HMAC of the ad
account ID under a random per-profile salt generated at enrollment. It is stable
for one profile, differs between profiles, and does not reveal the account ID.

### Recovering an interrupted enrollment

Enrollment refuses to run while any of the four slots is occupied, so a partial
install must be cleared before a retry. Two commands are needed because the two
halves belong to two contracts: `auth linkedin-token remove-local --profile <id>`
clears the protected LinkedIn slots and `auth remove-local --profile <id>`
clears the stored access-token entry. The
`LINKEDIN_TOKEN_CREDENTIAL_EXISTS` diagnostic names both.

### `auth linkedin-token remove-local --profile <id>`

Deletes only the three LinkedIn-owned protected slots — client secret, refresh
token, and state — and reports `LOCAL_REMOVE_ONLY` with
`remote_revocation_verified: false`. It deliberately leaves the profile's bearer
slot to the shared `auth remove-local` command.

## Revocation guidance

Provider-side revocation and local removal are separate acts, and the local
commands never claim the former.

- **Provider-side:** LinkedIn Settings & Privacy → Data privacy → Other
  applications → Permitted Services, then remove the developer application that
  was enrolled. See
  [LinkedIn's access-removal instructions](https://www.linkedin.com/help/linkedin/answer/a519947/third-party-applications-data-use)
  (checked 2026-09-08). Removing a shared application's grant can also interrupt
  other uses of that grant, so identify the exact application first.
- **Local-only:** `adhelm auth remove-local --profile <id>` deletes the stored
  access-token entry; `adhelm auth linkedin-token remove-local --profile <id>`
  deletes the client secret, refresh token, and lifecycle state.
- Local removal never revokes the grant at LinkedIn and is never evidence that
  it was revoked. LinkedIn separately reserves the right to revoke tokens at any
  time.

## Diagnostics and exit codes

| Situation | Code | Exit |
| --- | --- | --- |
| Callback `state` does not match the local attempt | `OAUTH_CALLBACK_STATE_MISMATCH` | 4 |
| Member cancelled or denied the authorization | `OAUTH_AUTHORIZATION_DENIED` | 4 |
| Bounded browser window expired | `OAUTH_AUTHORIZATION_EXPIRED` / `OAUTH_CALLBACK_EXPIRED` | 4 |
| Provider granted fewer permissions than the profile needs | `OAUTH_SCOPE_NOT_GRANTED` | 4 |
| LinkedIn rejected the token exchange or refresh (400/401/403) | `OAUTH_TOKEN_REJECTED` | 4 |
| Token outcome could not be classified | `OAUTH_TOKEN_OUTCOME_UNCERTAIN` | 6 |
| No refresh token was granted to this application | `REFRESH_NOT_GRANTED` | 4 |
| Stored refresh token is past its recorded expiry | `LINKEDIN_TOKEN_REFRESH_EXPIRED` | 4 |
| Profile blocked after a failed refresh | `LINKEDIN_TOKEN_REAUTHORIZATION_REQUIRED` | 4 |
| Refreshed credential resolves to a different ad account | `LINKEDIN_TOKEN_IDENTITY_MISMATCH` | 4 |
| LinkedIn denied the identity read (expired token, wrong slot, missing scope or role) | `LINKEDIN_ADS_AUTHORIZATION_REQUIRED` | 4 |
| Local profile recorded fewer permissions than a read needs | `LINKEDIN_ADS_SCOPE_MISSING` | 4 |
| No complete local enrollment for this profile | `LINKEDIN_TOKEN_ENROLLMENT_REQUIRED` | 4 |
| A local credential write could not be read back | `CREDENTIAL_LOCAL_CHANGE_UNVERIFIED` | 6 |
| Provider returned an OAuth error object instead of a token | `OAUTH_TOKEN_ERROR_RESPONSE` | 4 |
| Provider granted a permission this profile purpose may never record | `LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE` | 4 |
| Stored lifecycle state is unreadable or outside the purpose allowlist | `LINKEDIN_TOKEN_STATE_INVALID` | 4 |
| Requested scopes outside the profile's allowlist, or narrower than the profile declares | `LINKEDIN_TOKEN_SCOPE_REJECTED` | 2 |
| Entered client ID is not a usable OAuth client identifier | `LINKEDIN_TOKEN_CLIENT_ID_INVALID` | 2 |
| `--redirect-port` missing or unregisterable | `LINKEDIN_TOKEN_REDIRECT_PORT_INVALID` | 2 |

`LINKEDIN_TOKEN_SCOPE_REJECTED` and `LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE` are
deliberately separate codes with one exit code each. The first is a caller-input
failure an agent fixes by changing the arguments it passed; the second is an
authorization failure about what LinkedIn actually granted, which no change of
arguments can fix. No diagnostic code in this lifecycle carries two exit codes.

`LINKEDIN_ADS_AUTHORIZATION_REQUIRED` and `LINKEDIN_ADS_SCOPE_MISSING` are
**opt-in**: the adapter raises them only when a caller supplies the permissions
this lifecycle recorded. `doctor` and the fixed report workflow pass none, so
they keep the shared `PROVIDER_ACCESS_REQUIRED` code every other provider uses
and their behavior is unchanged by this work.

`LINKEDIN_ADS_AUTHORIZATION_REQUIRED` is the specific diagnosis for the pilot's
wrong-slot 401. Its next action names the three distinguishable causes — an
expired bearer, a refresh token stored where the bearer belongs, and a missing
`r_ads` permission or ad-account role — and tells the operator to check
`auth linkedin-token status` rather than retry the same value.

## Egress

Two destinations, both HTTPS with no redirects followed and no ambient
environment configuration:

- `www.linkedin.com` — the OAuth authorization and token endpoints. The token
  POST uses the fixed-endpoint OAuth transport rather than the shared
  `SafeHttpClient`, because an OAuth token form carries `client_secret` and
  `refresh_token` field names that the shared transport's generic privacy
  boundary correctly rejects. This mirrors the existing Google desktop OAuth
  family, which is separated from the shared transport for the same reason.
- `api.linkedin.com` — the fixed account identity read, through the ordinary
  `SafeHttpClient` allowlist with `Linkedin-Version` and
  `X-Restli-Protocol-Version`.

`docs/egress-map.md` carries a dedicated row for the LinkedIn OAuth endpoints,
separate from the LinkedIn Marketing API row, because the two are different
hosts with different allowed data.

## Known local risks

- **Denial of enrollment on the fixed callback port.** LinkedIn matches a
  registered redirect URI exactly and documents no variable-port loopback rule,
  so this flow must bind one operator-chosen port and one fixed path rather than
  Google's ephemeral port plus per-attempt nonce path. Any other process running
  as the same OS user can therefore hold that port and prevent enrollment from
  starting, which surfaces as `OAUTH_LOOPBACK_UNAVAILABLE`. This is a
  denial-of-enrollment risk, not a credential-exposure one: the listener still
  accepts only `127.0.0.1` peers, only the exact path, only a matching `Host`
  header, and rejects any request carrying an `Origin` header or a body, and the
  authorization code is still bound to a fresh 32-byte `state` that a squatting
  process cannot predict. If LinkedIn is later confirmed to permit RFC 8252
  variable loopback ports, this should move to an ephemeral port and a nonce
  path to match Google.
- **A partially installed enrollment blocks retries** until both removal
  commands above have run.

## Reusable OAuth module

`src/adhelm/oauth_enrollment.py` holds the provider-parameterized parts: the
provider contract, the loopback redirect receiver (nonce path by default, fixed
port and path when a provider requires an exactly registered redirect), the
state nonce, optional S256 PKCE, the confidential-client token exchange, the
refresh grant, the fixed-endpoint token transport, and the hidden prompt.

The Google desktop OAuth modules are **not** rewritten onto it in this change.
Google's flow carries OIDC identity verification, a Google-specific two-scope
contract, and a protected bundle lifecycle that are not generalized here, so
rewriting it would be a behavior change rather than a refactor. Google's
behavior is unchanged and its existing suites still pass.

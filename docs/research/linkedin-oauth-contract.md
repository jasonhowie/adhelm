# LinkedIn Ads OAuth 2.0 contract — 2026-09-08

## Evidence status

Every statement below is read from LinkedIn's official published documentation
on Microsoft Learn. Each source URL carries the date it was fetched for this
document. Nothing here has been exercised against a live LinkedIn application:
this repository's implementation is **fixture-tested only**, has made no live
OAuth request, and holds no LinkedIn application approval. Anything the
documentation does not settle is marked **UNVERIFIED** rather than assumed.

Sources fetched 2026-09-08:

- [3-legged OAuth (authorization code flow)](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [Programmatic refresh tokens](https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens)
- [Token introspection](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection)
- [Increasing access / permissions](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access?view=li-lms-2026-08)
- [Marketing API versioning](https://learn.microsoft.com/en-us/linkedin/marketing/versioning?view=li-lms-2026-07)
- [Advertising API overview](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/ads-overview?view=li-lms-2026-08)

## Endpoints

| Purpose | Method and URL | Source |
| --- | --- | --- |
| Authorization | `GET https://www.linkedin.com/oauth/v2/authorization` | 3-legged OAuth |
| Token exchange and refresh | `POST https://www.linkedin.com/oauth/v2/accessToken` | 3-legged OAuth; programmatic refresh tokens |
| Token introspection | `POST https://www.linkedin.com/oauth/v2/introspectToken` | Token introspection |

Both token endpoints require `Content-Type: application/x-www-form-urlencoded`.
The Marketing resource base is a different host, `https://api.linkedin.com/rest/`.

This implementation calls the authorization and token endpoints only. The
introspection endpoint is **not implemented**: it requires posting the client
secret together with the token being inspected, and the local lifecycle already
tracks expiry and scope from the token response itself.

## Authorization request

Documented parameters: `response_type=code` (required), `client_id` (required),
`redirect_uri` (required), `scope` (required, "URL-encoded, space-delimited"),
`state` (documented as optional). LinkedIn's own guidance is that the
application must compare the returned `state` with the request value and treat a
mismatch as a `401`. This implementation always sends a 32-byte random `state`
and rejects any mismatch.

The authorization code has a documented 30-minute lifespan and must be used
immediately.

Failure redirects carry `error` (`user_cancelled_login` or
`user_cancelled_authorize`), `error_description`, and `state`.

### UNVERIFIED — PKCE

The authorization request parameter table documents **no** `code_challenge` or
`code_challenge_method` parameter, and the token request table documents no
`code_verifier`. One `/oauth/v2/accessToken` error string on the same page does
mention "appid/redirect uri/code verifier does not match authorization code",
which implies a code-verifier concept exists somewhere in LinkedIn's
implementation, but no request parameter is documented for it.

Treatment: `pkce_method="NONE"` for the LinkedIn contract. The reusable flow
supports S256 and sends it only for a provider whose contract declares it, so no
undocumented parameter is sent to LinkedIn. Whether LinkedIn would accept or
require PKCE is UNVERIFIED.

### UNVERIFIED — loopback redirect URI

LinkedIn requires the redirect URI to match a URL registered on the
application's Auth tab, states "Add the redirect (callback) URL via `HTTPS` to
your server", requires absolute URLs, ignores query parameters, and rejects
fragments. The same page also tells integrators to "follow the [standard OAuth
specification](https://datatracker.ietf.org/doc/html/rfc8252)" — RFC 8252 is the
native-app profile that defines loopback redirects — but only in the context of
the extended login options.

Whether `http://127.0.0.1:<port>/oauth/callback` is accepted as a registered
redirect URL, and whether LinkedIn varies the loopback port per RFC 8252
section 7.3, are both **UNVERIFIED**. This is the largest single risk to the
implemented desktop flow.

Treatment: the local flow binds an **operator-chosen fixed port** and the fixed
path `/oauth/callback`, so the operator can register exactly
`http://127.0.0.1:<port>/oauth/callback`. It does not assume a variable port. If
LinkedIn rejects an `http` loopback redirect, enrollment fails explicitly at the
authorization step; nothing falls back to a pasted token.

## Token exchange (authorization code)

Documented request fields, all required: `grant_type=authorization_code`,
`code`, `client_id`, `client_secret`, `redirect_uri`. This is a **confidential
client** flow: LinkedIn requires the client secret in the exchange, so the local
lifecycle keeps the secret in a protected OS credential-store slot.

Documented response fields: `access_token`, `expires_in`, `refresh_token`,
`refresh_token_expires_in`, `scope`. LinkedIn's published success sample is:

```json
{"access_token":"...","expires_in":5184000,"scope":"r_basicprofile"}
```

`token_type` is **not** present in either documented sample. The implementation
therefore treats `token_type` as optional and requires the value `Bearer` only
when it is returned.

Documented token sizes: access and refresh tokens are approximately 500
characters; LinkedIn recommends handling at least 1,000 characters. The local
lifecycle bounds stored secret text at 16,384 characters.

## Token lifetimes

| Value | Documented figure | Source |
| --- | --- | --- |
| Access token | "all access tokens are issued with a 60-day lifespan"; sample `expires_in` 5184000 | 3-legged OAuth |
| Access token after refresh | "the new access token has a new TTL of 60 days" | Programmatic refresh tokens |
| Programmatic refresh token | "valid for a year" / "365 days"; TTL does **not** reset on use | Programmatic refresh tokens |

### UNVERIFIED — `refresh_token_expires_in` unit and sample inconsistency

The field description says "the number of seconds remaining until the refresh
token expires", but the published samples on the programmatic-refresh-tokens
page show `refresh_token_expires_in` values of `525600` and `439200`. 525600 is
exactly 365 days expressed in **minutes**, not seconds; 439200 is 305 days in
minutes. The same page's samples also show `expires_in: 86400` (one day) while
the prose says 60 days.

The documentation is internally inconsistent here and the live unit is
**UNVERIFIED**.

Treatment: the field description is followed literally — when the value is
present it is read as seconds and bounded at 31,536,000 (365 days). If LinkedIn
actually returns minutes, the locally recorded refresh expiry will be far
earlier than the real one and `auth linkedin-token refresh` will fail closed
with `LINKEDIN_TOKEN_REFRESH_EXPIRED`, requiring a browser re-enrollment. That
is a conservative, explicit failure, not a silent fallback. This must be
corrected against a live response before any refresh lifetime claim is made.

When the field is **absent** from a refresh response — including a response that
rotates the refresh token without restating its expiry — the previously recorded
expiry is preserved. LinkedIn documents that "the lifespan or Time To Live (TTL)
of the refresh token remains the same as specified in the initial OAuth flow
(365 days)", so an absent field is missing information about an unchanged
deadline, never a statement that the refresh token has no expiry. Reading
absence as "no expiry" would silently discard known lifecycle state and disable
the `LINKEDIN_TOKEN_REFRESH_EXPIRED` guard permanently.

## Refresh grant

Documented request fields, all required: `grant_type=refresh_token`,
`refresh_token`, `client_id`, `client_secret`. A successful request "returns a
new access token with a new expiration time and the refresh token".

Documented error: HTTP 400 `invalid_request` "The provided authorization grant
or refresh token is invalid, expired or revoked", whose documented resolution is
to reauthenticate the member.

### Refresh-token availability is conditional

"LinkedIn supports programmatic refresh tokens for **all approved Marketing
Developer Platform (MDP) partners**" and "Programmatic refresh tokens are
available for a limited set of partners. If this feature has been enabled for
your application …". An application without that enablement simply receives no
`refresh_token` in the authorization-code response, and LinkedIn's documented
alternative is to send the member through the authorization flow again — which
it notes is usually silent while the member is still signed in and the current
access token has not expired.

Treatment: when the token response contains no `refresh_token`, the local state
records `REFRESH_NOT_GRANTED` and `auth linkedin-token refresh` fails with exit
code 4 and that exact code. There is no silent re-authorization fallback.

LinkedIn also "reserves the right to revoke Refresh Tokens or Access Tokens at
any time due to technical or policy reasons", so a stored refresh token is never
treated as a guarantee.

## Scopes

Exact permission strings for the Advertising API product, quoted from the
permissions table:

| Scope | Documented description |
| --- | --- |
| `r_ads` | "Retrieve your advertising accounts." |
| `r_ads_reporting` | "Retrieve reporting for your advertising accounts." |
| `rw_ads` | "Manage and read an authenticated member's ad accounts. Restricted to ad accounts where the authenticated member has one of the following roles: `ACCOUNT_BILLING_ADMIN`, `ACCOUNT_MANAGER`, `CAMPAIGN_MANAGER`, `CREATIVE_MANAGER`." |

All three are Member Permissions (3-legged OAuth) granted to an application
already approved for the Advertising API. The permissions page adds that
"members must have the appropriate roles as defined in … Ad Account Roles … to
act on or read data from a given … Ad Account", so a granted scope is never by
itself sufficient.

Access-tier limits: the Development tier gives unlimited **read** on
administered ad accounts, one API-created test ad account, and **edit** on up to
five administered ad accounts. Standard tier removes the edit limits and is
requested separately.

Operationally important: "If you request a different scope than the previously
granted scope, all the previous access tokens are invalidated." Changing the
requested scope set therefore invalidates the existing local bearer, which is
why the local scope selection is a closed per-purpose allowlist rather than a
free-form option.

Treatment: the default requested set is the least-privilege read pair
`r_ads r_ads_reporting`. `rw_ads` is admitted only for an `EDIT` profile and
only when requested explicitly; a `READ` profile can never request it.

## `Linkedin-Version` header interaction

The versioning page scopes the version header to the Marketing REST base:
"LinkedIn's Marketing APIs first versioned API release was June 2022 under a new
base path: `https://api.linkedin.com/rest/`", and "Include a request header with
the key 'Linkedin-Version' and set the value to the version (format: YYYYMM)".
Its examples pair `Linkedin-Version` with `X-Restli-Protocol-Version: 2.0.0` on
`api.linkedin.com/rest/...` calls, and it states that an unversioned call to
those endpoints returns an error.

Neither OAuth page shows a `Linkedin-Version` (or `X-Restli-Protocol-Version`)
header on `/oauth/v2/authorization`, `/oauth/v2/accessToken`, or
`/oauth/v2/introspectToken`.

Treatment: the token POST sends only `accept: application/json` and
`content-type: application/x-www-form-urlencoded`. The identity read that
follows enrollment and refresh is an ordinary Marketing REST call and does send
`Linkedin-Version: 202608` plus `X-Restli-Protocol-Version: 2.0.0` through the
existing adapter.

**UNVERIFIED**: that the OAuth endpoints reject or ignore a `Linkedin-Version`
header. The documentation is silent, so absence of a documented requirement is
recorded as absence of documentation, not as proven behavior.

## Other UNVERIFIED items

- **Token revocation endpoint.** No OAuth revocation endpoint was found in the
  fetched documentation. Provider-side removal is documented only as a member
  action in LinkedIn's Settings & Privacy "Permitted Services" screen. Local
  credential removal is never described as provider revocation.
- **Token-endpoint rate limits and 429 behavior.** Not documented on the OAuth
  pages; the Marketing quota documentation covers resource APIs.
- **`scope` delimiter in a live token response.** The authorization-code page
  says the `scope` value is space-delimited; the introspection page documents a
  comma-separated list. Either delimiter is accepted on its own
  (`"r_ads r_ads_reporting"` or `"r_ads,r_ads_reporting"`), but a **mixed** form
  such as `"r_ads, r_ads_reporting"` produces an empty token and fails closed as
  an unsupported token response (exit 6) rather than being silently repaired.
  Which form a live token response uses is UNVERIFIED, and a mixed form would
  need a deliberate parser change with its own evidence.
- **Whether a refreshed response always echoes `scope`.** The samples include
  it, but the field is not marked required. The implementation keeps the
  previously recorded scope set when it is absent and fails closed if a returned
  set is narrower than the enrolled set. A refresh never widens the recorded
  set: a refresh renews an existing consent rather than collecting a new one, so
  a wider echo is reported and discarded, not stored.
- **Whether a token response can echo permissions the request did not ask for.**
  LinkedIn documents that a member consents to the requested set as a whole, and
  that requesting a different scope set invalidates previously issued tokens.
  What a live response returns when the member already consented to a wider set
  through another application flow is UNVERIFIED. The implementation clamps the
  recorded set to exactly what this authorization requested and reports the
  excess separately as `unrequested_granted_scopes`; a READ profile additionally
  refuses to record `rw_ads` under any circumstances.

  That clamp limits only what this product records and enforces locally. It does
  **not** narrow the grant at LinkedIn. The bearer stored in the profile's slot
  still carries whatever the member actually consented to, and any other holder
  of that token could exercise the wider permission. The only thing preventing
  this product from issuing a LinkedIn write today is that no LinkedIn write
  path exists in this codebase. Narrowing the real grant requires re-authorizing
  with a different requested scope set — which LinkedIn documents invalidates
  previously issued access tokens — or removing the application under Permitted
  Services.
- **Behavior when the same member authorizes twice.** The documentation says
  multiple simultaneous access tokens may exist for the same scope, but does not
  say whether a new authorization invalidates a previously issued refresh token.
- **Account timezone.** As already recorded in the read contract, the ad-account
  schema supplies no IANA timezone, so none is inferred here either.

## What this document does not establish

It does not establish that any application is approved for the Advertising API,
that any member holds a qualifying ad-account role, that programmatic refresh
tokens are enabled for a given application, or that the loopback desktop flow
works against LinkedIn. Those are live, authorized checks that have not been
performed.

# Reddit Ads OAuth 2.0 contract — 2026-10-10

## Evidence status

This document records the wire contract the local Reddit enrollment is built
against. The 0.1.0 build runs with no provider network access, and the
implementation is **fixture-tested only**: no live Reddit OAuth request has
been made and no Reddit application exists for this repository. Live
acceptance of every item stays `UNVERIFIED` until the first live enrollment.

The confidential-client items (the application ID and secret, and HTTP Basic
authentication on the token exchange) were checked against the Authenticate a
developer application and Create a Developer Application pages fetched on
2026-10-11. The remaining items restate documentation already recorded for this
repository or Reddit's general OAuth 2 documentation as the author knew it, and
are `UNVERIFIED` against current Reddit text. The source classes below say which
is which. The setup guide must re-check each remaining item against the
provider's primary pages and record the fetch date.

Each claim below is attributed to one of two source classes. They are not
interchangeable, and the second does not inherit the Ads API's authority.

- **Ads API pages.** The
  [Authenticate a developer application](https://ads-api.reddit.com/docs/v3/authenticate-your-developer-application)
  and [System Users](https://ads-api.reddit.com/docs/v3/guides/programs/business/system-users)
  pages, as recorded in
  the 2026-09-04 provider access research record. That record
  supports only: an OAuth authorization-code flow, authorization and token
  endpoints under `reddit.com/api/v1`, the scope names `adsread`, `adsedit` and
  `adsconversions`, a customer-owned developer application, and the System Users
  open beta. The Authenticate page and the Create a Developer Application page
  were re-fetched on 2026-10-11 (dates recorded under Sources in
  [`docs/setup/reddit_ads.md`](../setup/reddit_ads.md)) and add two statements:
  Reddit generates the developer application's ID **and secret**, and the token
  exchange authenticates with `-u '<app ID>:<secret>'`. This repository still
  made no live request.
- **General Reddit OAuth 2 documentation.** Everything else: `duration=permanent`,
  the one-hour access token, the non-expiring permanent refresh token, the
  descriptive `User-Agent` requirement, the revocation endpoint, and the
  `prefs/apps` registration page. That documentation describes Reddit's general
  API; the Ads API page does not repeat it and this repository has not confirmed
  that the Ads API behaves the same way.

## Endpoints

| Purpose | Method and URL |
| --- | --- |
| Authorization | `GET https://www.reddit.com/api/v1/authorize` |
| Token exchange and refresh | `POST https://www.reddit.com/api/v1/access_token` |
| Ads API resource base | `https://ads-api.reddit.com/api/v3` (a different host) |

This implementation calls the authorization and token endpoints only.

## Confidential client

*Source: the Ads API Authenticate and Create a Developer Application pages, as
re-fetched on 2026-10-11.* A developer application created in Reddit Ads has a
client ID and a **secret**. The token endpoint authenticates the client with
HTTP Basic: the client ID is the user name and the secret is the password
(`Authorization: Basic base64("<client_id>:<secret>")`, Reddit's
`-u '<app ID>:<secret>'`). Neither is placed in the form body, and the same
authentication is sent on the refresh grant. A client ID containing a colon
cannot be encoded this way, so the authorization target refuses one before the
browser handoff, and the token transport refuses one again before any token
request.

The local flow prompts for the client ID and the secret at hidden prompts, never
from an argument or an environment variable, and stores the secret in its own
credential slot beside the refresh token and lifecycle state. A refresh reads
the secret back from that slot; it is never asked for again. Local removal
deletes the slot. An empty secret is refused at the prompt and again before any
request is built, so the exchange this build was first written to send (the
client ID with an empty password) cannot be produced. The flow proves nothing
about ad-account membership or role, only that one fixed identity read answered.

### UNVERIFIED — live acceptance

No live Reddit request has been made. That Reddit accepts a loopback-redirect
application's ID and secret in this exchange, and answers the permanent grant
with a refresh token, rests on Reddit's Authenticate and Create a Developer
Application pages only. The fixture tests show only what AdHelm sends (the
client ID and secret as HTTP Basic, and the refusal of an empty secret); they
cannot show that Reddit accepts it, and that stays unestablished until a live
pilot runs. If Reddit rejects the exchange, enrollment fails at the token
exchange with `OAUTH_TOKEN_REJECTED` (HTTP 400/401/403) or
`OAUTH_TOKEN_ERROR_RESPONSE` (an error object such as `invalid_client` returned
in a 200 body), after the browser step has already succeeded, and nothing is
stored. Nothing falls back to a pasted token. A live read pilot (owner action 4)
is optional evidence of this, not part of the fix.

## Authorization request

Parameters sent: `response_type=code`, `client_id`, `redirect_uri`, `scope`
(space delimited), `state` (a fresh 32-byte random value, compared on return),
and **`duration=permanent`**. Reddit returns a refresh token only for a
permanent authorization; the default `temporary` duration yields an access token
and nothing else. No PKCE parameter is documented for this flow, so none is sent
(`pkce_method="NONE"`).

Failure redirects carry `error` and `state`; the callback receiver accepts only
`code`, `state`, `error`, `error_description` and `error_uri`.

### UNVERIFIED — loopback redirect URI

*Source: general Reddit OAuth 2 documentation.* Reddit matches the redirect URI exactly against the one registered on the
application. The flow binds one operator-chosen fixed port and the fixed path
`/oauth/callback`, so the operator registers exactly
`http://127.0.0.1:<port>/oauth/callback` and passes the same port with
`--redirect-port`. Whether Reddit accepts a literal `127.0.0.1` host (as opposed
to `localhost`) in a registered redirect is **UNVERIFIED**; if it does not,
enrollment fails at the authorization step and nothing falls back to a pasted
token.

## Token exchange (authorization code)

`POST` with `Content-Type: application/x-www-form-urlencoded`, HTTP Basic client
authentication as above, and form fields `grant_type=authorization_code`, `code`,
`redirect_uri`. Reddit rejects requests that lack a descriptive `User-Agent`, so
the token POST sends the same agent string as the Ads adapter.

Documented success fields: `access_token`, `token_type`, `expires_in`, `scope`,
and (for a permanent authorization) `refresh_token`. The documented `token_type`
is the **lower-case** `bearer`; RFC 6749 section 7.1 makes the type
case-insensitive, and the shared parser now accepts any casing of `bearer` and
refuses every other value. A field outside that set fails closed rather than
being ignored.

Reddit reports a failed exchange as either an HTTP 4xx or a `200` body that is an
OAuth error object (`{"error": "invalid_grant"}`); both are classified as a
definite authorization failure (exit `4`), never as an ambiguous outcome.

## Token lifetimes

| Value | Documented figure |
| --- | --- |
| Access token | One hour (`expires_in` 3600) |
| Permanent refresh token | No expiry; valid until revoked |

The accepted `expires_in` ceiling is 86,400 seconds, an upper bound on a response
this build will accept, not a promise about any grant. No `refresh_token_expires_in`
is documented. If a response nonetheless carries one, the local state records it
and `auth reddit-token refresh` enforces it, because dropping a stated expiry
would silently disable the guard.

## Refresh grant

`POST` to the same token endpoint with HTTP Basic authentication and
`grant_type=refresh_token`, `refresh_token`. The documented response is a new
access token with a new `expires_in`; **no new refresh token is issued**, so the
stored permanent token stays. A `refresh_token` in a refresh response, were one
ever returned, would replace the stored one.

A refresh that returns a narrower scope set than the enrolled one fails closed
with `REDDIT_TOKEN_SCOPE_REDUCED`. A refresh never widens the recorded set: a
wider echo is reported as `unrequested_granted_scopes` and discarded.

Rate limits and `429` behavior at the token endpoint are **not documented** in
the sources above. The token transport classifies `429`, `5xx` and every
redirect as an uncertain outcome (exit `6`) and does not retry; it never reports
them as a rejected credential.

## Scopes

| Scope | Use here |
| --- | --- |
| `adsread` | The only scope a `READ` profile requests; covers account identity, inventory and reports |
| `adsedit` | Admitted only for an `EDIT` profile that names it explicitly; no 0.1.0 code path uses it because every remote write is refused |
| `adsconversions` | Not admitted: no conversion capability exists in a read-only build |

A `READ` profile can never request or record `adsedit`, whatever the provider
echoes. As with LinkedIn, that clamp limits only what this product records and
enforces locally; it does not narrow the grant at Reddit. To narrow a real grant,
re-authorize with a smaller scope set or revoke the application.

## Revocation

Reddit documents a token-revocation endpoint in its general OAuth 2 documentation.
**This lifecycle does not call it.** Revoking a grant is a provider-side change
the release scope does not include, so the commands offer local removal and
guidance only. The guidance tells the operator to look for the application in the
signed-in Reddit account's authorized-applications settings and to remove any app
registration they created; the CLI strings say that this has not been checked
against Reddit's current pages and do not name a page as fact. Local removal is
never described as provider revocation and `remote_revocation_verified` is always
`false`. Whether the revoke endpoint accepts the application's Basic
authentication, and where and how the Ads API integration's authorized-application
grant can be revoked, are **UNVERIFIED**.

## Other UNVERIFIED items

- **Live acceptance of the ID-and-secret exchange.** See the section above.
- **System users.** The Ads API documents an open-beta system-user alternative to
  tying an integration to a person. It is not implemented here.
- **Whether a second authorization of the same user invalidates an earlier
  refresh token** is not documented in the reviewed sources.
- **Ad-account role.** A granted `adsread` scope does not establish a role on the
  configured ad account. Results report `app_review_verified: false` and
  `account_role_verified: false`.
- **Developer terms.** Reddit's Ads API terms and developer terms apply to a
  user-registered application; nothing here is a compliance claim.

## What this document does not establish

It does not establish that any Reddit application is registered or approved for
the Ads API, that Reddit accepts the exchange AdHelm sends, that any
user holds a qualifying ad-account role, that the loopback
desktop flow works against Reddit, or that the response shapes above match
production. Those are live, authorized checks that have not been performed.

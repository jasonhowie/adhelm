# TikTok Ads app auth-code exchange contract

Reviewed 2026-09-09. This records exactly what the fetchable official material
proves about the TikTok API for Business advertiser authorization lifecycle,
and — just as importantly — what it does not.

TikTok's own reference portal (`business-api.tiktok.com/portal/docs?id=...`,
`ads.tiktok.com/gateway/docs/index?doc_id=...`) renders through JavaScript. A
plain fetch of `https://business-api.tiktok.com/portal/docs?id=1738373164380162`
on 2026-09-09 returned a page title and no documentation body. Everything
marked `PROVEN` below therefore comes from the **TikTok-maintained** SDK
repository, which is first-party and served as plain text, or from a
first-party file in this repository's own reviewed research. Everything else is
marked `UNVERIFIED` and is never relied on by code.

## Primary evidence

| Source | Fetched | What it proves |
| --- | --- | --- |
| [`python_sdk/docs/AuthenticationApi.md`](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AuthenticationApi.md) | 2026-09-09 | `POST /open_api/v1.3/oauth2/access_token/`; request `Content-Type: application/json`, `Accept: application/json`; no authorization header; return type `InlineResponse200`. Also `GET /open_api/v1.3/oauth2/advertiser/get/` taking `app_id`, `secret`, `access_token`. |
| [`js_sdk/docs/AuthenticationApi.md`](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/js_sdk/docs/AuthenticationApi.md) | 2026-09-09 | The same two operations and no third. There is **no** refresh-token operation in the business/advertiser Authentication API. |
| [`python_sdk/docs/Oauth2AccessTokenBody.md`](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/Oauth2AccessTokenBody.md) | 2026-09-09 | The whole request body is exactly three required string fields: `app_id`, `auth_code`, `secret`. There is no `grant_type`, no `redirect_uri`, and no PKCE parameter. |
| [`python_sdk/docs/InlineResponse200.md`](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/InlineResponse200.md) | 2026-09-09 | The response envelope is `{code: int, data: object, message: str, request_id: str}`. `data` is an untyped object: the SDK proves the envelope, not the payload field names. |
| [`python_sdk/business_api_client/configuration.py`](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/business_api_client/configuration.py) | 2026-09-09 | `self.host = "https://business-api.tiktok.com"` — the production base URL. |
| The 2026-09-04 provider access research record | in-repo, 2026-09-04 | Registered developer app; advertiser authorization flow; the authorization code is **single-use and valid for one hour**; the exchange returns the accessible advertiser IDs and a granted permission scope; app ID and secret are application credentials while the advertiser token is an access credential. |
| [`docs/research/tiktok-ads-read-contract.md`](tiktok-ads-read-contract.md) | in-repo, 2026-09-04 | The fixed v1.3 read host, `Access-Token` header, and the `{code,message,request_id,data}` envelope this build already parses. |

## What is PROVEN

1. **Endpoint.** `POST https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/`.
2. **Body.** A JSON object with exactly `app_id`, `secret`, `auth_code`. This is
   not an RFC 6749 form post: there is no `grant_type`, no `redirect_uri`, no
   `client_id`/`client_secret` naming, and no PKCE. The shared
   `adhelm.oauth_enrollment` confidential-client module builds and parses an
   RFC 6749 **form** exchange, so its wire functions cannot be reused here.
   `adhelm.tiktok_token_workflow.FixedTikTokTokenTransport` is the TikTok
   analogue of `FixedOAuthTokenTransport`: one POST, no redirects, no ambient
   environment, and an endpoint pinned inside the transport to the closed
   `TIKTOK_TOKEN_ENVIRONMENT_HOSTS` map, so a host outside that map is a
   `NOT_DISPATCHED` refusal and never a request. The reusable module's
   non-wire seams —
   `prompt_hidden_secret`, `OAuthTransportError`, `OAuthTokenDispatchState` —
   are reused unchanged.
3. **Envelope.** `{code, message, request_id, data}`, the same envelope the
   fixed read adapter already validates.
4. **No refresh route.** The business Authentication API documents exactly two
   operations, and neither refreshes an advertiser access token.
5. **Production host.** `business-api.tiktok.com`.

## What is UNVERIFIED

* **The `data` payload field names.** `advertiser_ids` and `scope` are named by
  this repository's 2026-09-04 provider-access review, not by any page fetched
  on 2026-09-09. The exchange therefore accepts an `advertiser_ids` array of
  decimal-string advertiser IDs **when the response carries one**, and records
  `advertiser_list_status="NOT_RETURNED"` with
  `advertiser_binding="NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"` when it does not.
  It never assumes a shape, and it never treats a missing list as an empty list.
  A returned *empty* list is the opposite case — a definite provider answer that
  the grant reaches no advertiser — and is refused as
  `TIKTOK_TOKEN_NO_ADVERTISERS_GRANTED`.
* **Permission scope strings.** No fetchable official source publishes the
  advertiser permission vocabulary; endpoint permissions are selected per app in
  TikTok's My Apps console. This build therefore records the granted permissions
  as the fixed literal `NOT_DOCUMENTED_LOCALLY` and **never** stores, echoes, or
  interprets a `scope` value. It records only whether a scope field was present.
* **Access-token expiry.** No fetchable official source documents an expiry for
  the advertiser access token. The SDK's `oauth2AccessToken` description
  (*"The creator access token is valid for 24 hours and the refresh token is
  valid for one year"*) is about the **creator/TikTok-account** token obtained
  through `/tt_user/oauth2/...`, not the advertiser token; nothing proves it
  applies here. If the response carries a bounded whole-second lifetime, status
  tracks it and marks the source `PROVIDER_DECLARED`; otherwise expiry is
  reported as `NOT_DOCUMENTED_LOCALLY` and no expiry is invented. The two
  candidate field names, `expires_in` and `token_expires_in`, are both unproven,
  so neither arbitrates the other: a response carrying both with different values
  is refused as `TIKTOK_TOKEN_RESPONSE_UNSUPPORTED` rather than resolved to one
  `PROVIDER_DECLARED` expiry the same response contradicts.
* **Refresh.** Because no advertiser refresh route is documented,
  `auth tiktok-token refresh` is a typed `TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED`
  refusal (exit 4) that points at re-enrollment. It performs no egress.
* **The authorization URL.** TikTok's app detail page in My Apps shows each
  app's own authorization URL, and it is not published as a constructable
  template on any page fetched here. This build therefore never constructs,
  opens, or emits an authorization URL and runs no loopback listener. The
  operator obtains the one-hour auth code themselves and pastes it into a
  hidden prompt.
* **The sandbox base URL.** Third-party write-ups name
  `https://sandbox-ads.tiktok.com/open_api`, but no first-party page fetched on
  2026-09-09 confirms it. The environment is therefore a **declared value**,
  not a profile field: it is the `--environment` flag, recorded in the
  protected state slot. `TIKTOK_TOKEN_ENVIRONMENT_HOSTS` maps only
  `PRODUCTION` to the proven host (`TIKTOK_TOKEN_ENVIRONMENTS` is just the
  closed tuple of valid values, `PRODUCTION` and `SANDBOX`).
  `tiktok_environment_host` resolves the declared value and refuses `SANDBOX`
  with the typed `TIKTOK_TOKEN_ENVIRONMENT_NOT_DOCUMENTED` error, not a guessed
  host; any other value is `TIKTOK_TOKEN_ENVIRONMENT_INVALID`.
* **Rate-limit business codes.** The sandbox tiering recorded on 2026-09-04
  (1 QPS / 30 QPM / 1,000 QPD) is a documented *limit*, not a documented
  *refusal code*. No fetchable source enumerates the `code` values TikTok
  returns when a caller is throttled, so `TIKTOK_ADS_RATE_LIMIT_BUSINESS_CODES`
  is deliberately empty and the read adapter classifies a rate limit from the
  observed HTTP `429` only. A non-zero business code the contract cannot
  classify becomes a fixed diagnostic; no `message` text ever reaches one.
* **Revocation.** TikTok's Authorization FAQs (fetched 2026-10-11) give the
  advertiser's route: the TikTok For Business authorization settings page
  (`ads.tiktok.com/ac/page/authorizations`). TikTok's "Revoke a long-term access
  token" page documents `/oauth2/revoke_token/`, which takes the app ID, app
  secret and the token. AdHelm does not send it. `remove-local` is described
  only as local deletion and carries that guidance; the provider-side step is
  never checked and never claimed.

## Local lifecycle shape

The enrollment mirrors `docs/implementation/linkedin-token-lifecycle.md`:
distinct protected slots under a private credential-store service, a durable
`ENROLL_STARTED` written before any secret, an interrupted install reported as
`UNVERIFIED` rather than usable, and no credential mutation lock held across the
one HTTP call. TikTok's four slots are the profile's ordinary access-token slot
plus a protected **app secret**, a protected **advertiser ID list**, and a
protected non-secret **state** record.

No live TikTok request was made for this work. Everything is fixture-only.

# Google desktop OAuth protocol boundary

`adhelm.google_oauth_protocol` implements the narrow OAuth 2.0 desktop
authorization-code boundary used by the local Google profile lifecycle. It is
not a configuration loader, browser launcher, loopback server, keyring reader,
provider client, durable grant, or authorization mechanism.

The supported targets are closed:

| Provider and purpose | API scope |
| --- | --- |
| Google Ads `READ` | `https://www.googleapis.com/auth/adwords` |
| GA4 Data `READ` | `https://www.googleapis.com/auth/analytics.readonly` |
| GA4 Admin `READ` | `https://www.googleapis.com/auth/analytics.readonly` |
| GA4 Admin `EDIT` | `https://www.googleapis.com/auth/analytics.edit` |
| GTM `READ` | `https://www.googleapis.com/auth/tagmanager.readonly` |

Google Ads exposes no narrower user OAuth scope. The local `READ` purpose
constrains installed product behavior; it does not claim that the Google grant
itself is read-only. The GA4 Admin EDIT grant also has broad provider management
scope; its separate local purpose does not authorize a write. GTM edit/publish, Google Ads mutations,
incremental authorization, device flow, custom redirect URIs, DPoP, and
provider account access proof remain outside this boundary.

The profile layer imports only the dependency-light mapping and grammar
helpers. `GoogleOAuthTarget` accepts one installed client ID in the closed
`*.apps.googleusercontent.com` grammar and one supported API scope. A verified
OIDC issuer and raw `sub` produce a fixed-size operational profile reference:

```text
google_oidc_principal_v1.<SHA-256(
  "adhelm-google-oidc-principal-ref-v1\0" +
  "https://accounts.google.com" + "\0" + verified-sub
)>
```

The raw subject is transient or protected bundle material only. It is not
configuration, CLI output, ledger evidence, or a human-readable identity
claim. The two Google issuer spellings accepted by Google are canonicalized to
the HTTPS spelling before this calculation.

## Authorization flow

The loopback owner binds `127.0.0.1` first, creates its random fixed callback
path, then calls:

```python
attempt = start_google_oauth_authorization(target, redirect_uri)
url = google_oauth_authorization_url(attempt)
```

Only the typed attempt can reach the URL builder. It reconstructs the frozen
attempt and checks expiry using the protocol's private clock before producing a
URL for the operating-system browser handoff. Public callers cannot supply a
clock or randomness source. The request has exactly these meaningful
parameters: `response_type=code`, the configured client ID, the bound IPv4
loopback redirect URI, `openid` plus one API scope, state, nonce, PKCE S256,
`access_type=offline`, and `prompt=consent`. It does not request `email` or
`profile`, set `include_granted_scopes`, or permit another authorization
endpoint.

`consume_google_oauth_callback(attempt, raw_query)` accepts one bounded raw
query only after exact state comparison. It does not expose callback code or
provider error text. The loopback owner remains responsible for accepting only
one local HTTP callback and closing its listener before code exchange.

The frozen dataclasses are parsing and transport values, not authorization
proof. A caller must complete the maintained ID-token verification in the same
flow before the protected lifecycle accepts an initial grant; reconstructing or
copying a dataclass cannot establish an identity or credential authority.

`complete_google_oauth_authorization(...)` sends one exact form POST to
`https://oauth2.googleapis.com/token`; it never retries a code exchange. A
successful initial response must contain an access token, exact integer
`expires_in`, `Bearer`, the allowed scope set, a refresh token, and an ID
token. Optional documented `refresh_token_expires_in` is represented only as a
private expiry timestamp for protected lifecycle handling.

The response has two distinct scope facts. `requested_scopes` is always the
fixed local request `("openid", api_scope)`. `returned_scopes` records only
the bounded Google response set: `(api_scope,)` or `("openid", api_scope)`.
The protocol does not turn an omitted textual `openid` scope into an invented
response claim. A nonce-bound verified ID token is required for the initial
identity proof.

## Refresh and identity

`refresh_google_oauth_grant(target, expected_identity, ...)` accepts a sealed
refresh token and optional sealed client secret from the protected lifecycle.
It returns a fresh in-memory access token plus:

- `returned_scopes`, or `None` when the refresh response supplies no scope;
- `identity_reverified`, which is `False` when no refresh ID token is returned;
- an optional private rotated refresh token and its expiry.

A returned refresh ID token uses a separate no-nonce verification path. It
checks signature, issuer, audience, expiry, authorized party when present, and
exact canonical issuer/raw subject/derived reference equality with the sealed
initial identity. It cannot establish or replace an identity. A response with
no ID token establishes no new identity fact. The protected lifecycle records
`REFRESH_STARTED` before the POST and must require reconnect after a dispatched
response, malformed success, or transport uncertainty rather than retrying.

`google-auth`'s maintained `verify_oauth2_token` verifier checks the initial
and optional refresh ID tokens. The custom request adapter permits only its
current fixed default certificate GET,
`https://www.googleapis.com/oauth2/v1/certs`; a library change to another URL,
method, body, or header shape fails closed in tests and at runtime. The protocol
does not use application-default credentials, metadata credentials, tokeninfo,
userinfo, `AuthorizedSession`, or custom JWT verification.

## Transport boundary

`adhelm.google_oauth_transport` is deliberately not `SafeHttpClient`. The
regular transport screens JSON and parameter fields for secret names, which is
correct for provider APIs but incompatible with OAuth forms. The OAuth
transport has no caller-selected URL or method:

- one URL-encoded POST to the fixed token endpoint;
- one GET to the fixed certificate endpoint;
- `httpx.Client(trust_env=False, follow_redirects=False)` in the production
  constructor;
- bounded JSON responses, fixed timeout, no token retry, no redirect;
- a private 30-second monotonic whole-response budget, checked after headers,
  after every received chunk, and before return, so individually timely slow
  chunks cannot retain the protected refresh lock indefinitely;
- no secret-bearing value in public serialization or diagnostic text.

`GoogleOAuthTransportError.dispatch_state` tells the lifecycle whether a token
request was `NOT_DISPATCHED`, received a `DEFINITE_RESPONSE`, or is
`UNCERTAIN`. The latter two are not safe retry permission. A malformed or
oversized 2xx response is uncertain because Google may already have rotated a
refresh token.

## Verification

The focused protocol and transport tests cover the RFC 7636 S256 vector,
closed client/principal grammars, exact URL fields, private expiry/randomness
seams, callback duplication, original JSON scalar types, explicit nulls,
scope normalization, actual `google-auth` certificate-request verification,
refresh identity continuity, form encoding, MIME/HTML rejection, endpoint
pinning, and sanitized dispatch classification.

The protocol was designed from Google's [desktop OAuth guidance](https://developers.google.com/identity/protocols/oauth2/native-app), [OpenID Connect guidance](https://developers.google.com/identity/openid-connect/openid-connect), and the maintained [`google-auth` ID-token verifier documentation](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html).

It does not prove Google account ownership, provider role, Cloud project
access level, API enablement, resource access, or app-review eligibility. Each
provider workflow must still perform its existing fixed identity/resource read
under the fresh in-memory token before it can claim current account access.

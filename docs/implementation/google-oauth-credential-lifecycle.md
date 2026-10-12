# Google OAuth protected credential lifecycle

This local-only implementation supports an `OS_OAUTH_GOOGLE` profile only for
the fixed targets below. The profile keeps one API scope; the private
authorization request and sealed bundle additionally retain the locally
requested `openid` scope. A missing textual `scope` response does not prove
`openid` was granted, and a verified OIDC subject does not prove resource access
or authority.

| Profile target | Exact API scope |
| --- | --- |
| `google_ads` / `READ` | `https://www.googleapis.com/auth/adwords` |
| `ga4` / `READ` | `https://www.googleapis.com/auth/analytics.readonly` |
| `ga4_admin` / `READ` | `https://www.googleapis.com/auth/analytics.readonly` |
| `ga4_admin` / `EDIT` | `https://www.googleapis.com/auth/analytics.edit` |
| `gtm` / `READ` | `https://www.googleapis.com/auth/tagmanager.readonly` |

The Google Ads scope is broad at Google. `READ` is this application's local
usage restriction and does not make the provider credential read-only. This
credential lifecycle does not execute remote writes; activation, publish,
arbitrary scopes, and other provider OAuth flows remain outside this slice. The
GA4 Admin `EDIT` target is a separate, deliberately
enrolled profile for the documented GA4 management scope. The documented
key-event methods name `analytics.edit`, whose catalog description is “Edit
Google Analytics management entities,” rather than a key-event-only scope. The
local target therefore does not make that provider scope key-event-only,
authorize this application to perform a remote write, or prove property access.
It only allows a later
guarded key-event operation to use a distinct credential after its own plan,
authorization, identity, drift, journal, and verification gates.
The [Google OAuth scope catalog](https://developers.google.com/identity/protocols/oauth2/scopes)
and [GA4 key-event create reference](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents/create)
name `analytics.edit` for this management operation.

## Profile and bundle boundary

`Profile` and `Configuration` require an actual JSON/Python integer
`schema_version: 1`; booleans and floats cannot pass by equality. The OAuth
profile also requires the exact target mapping, client-ID grammar, derived
operational principal grammar, provider-native account shape, IANA timezone,
and declared GA4 currency. Legacy `OS_STORE` and `EXPLICIT_CI` serialization
remains unchanged.

The protected bundle is a private canonical codec, not a Pydantic model or
public schema. It holds the raw OIDC subject, issuer, client binding, requested
and returned scopes, refresh/access tokens and expiries. It is never
configuration, ledger, artifact, command result, exception detail, or a
generic credential value. Bundle decode rechecks canonical JSON and the full
detached Profile binding before token transport.

The bundle binds the profile digest, provider, purpose, account, derived
principal, installed client, and exact API scope. A configured `READ` profile
cannot reconnect with, decode, or return a token from an `EDIT` bundle, and an
`EDIT` profile cannot use a retained `READ` bundle. Reconnect always derives its
target from the selected configured profile; it never adds a scope to an
existing consent or changes that profile's purpose.

Bundles use the fixed OS credential-store service
`adhelm.google-oauth-v1`. Ordinary `OS_STORE` secrets use
`adhelm`, so an unrelated or forged generic profile sharing a
reference cannot receive bundle JSON as a bearer token. Both services use the
same per-reference mutation lock and validation rejects case-folded aliases
across both sources. OAuth-primary status and removal use the dedicated
service; a Google Ads developer-token secondary stays an ordinary secret.

## Local transitions and ambiguity

Installation and reconnect require an opaque slot snapshot. It contains no raw
bundle: a process-local domain-separated HMAC binds the observed slot value,
reference, and configuration/profile binding. New enrollment requires an absent
slot. Reconnect requires the configuration-bound preparation API; it may begin
from an absent slot to recover a configuration-first failed installation, but a
slot changed or removed after preflight fails the final compare-and-write check.
The HMAC snapshot is a same-process local handoff guard, not a cross-process
trust boundary.

The sealed state machine is `READY`, `REFRESH_STARTED`, or
`RECONNECT_REQUIRED`.

1. A cached access token still requires a nonexpired refresh token.
2. Before fixed token egress, the implementation writes and reads back
   `REFRESH_STARTED`.
3. A `NOT_DISPATCHED` request failure restores the original `READY` bundle.
   Rejected, malformed, post-dispatch, certificate, timeout, redirect, or
   other uncertain outcomes persist `RECONNECT_REQUIRED` and forbid automatic
   replay.
4. A refreshed response must preserve target/identity/scope bindings and be
   written and read back before its token is returned. Clock rollback or expiry
   at the final check also becomes `RECONNECT_REQUIRED`.

If a completed `READY` write has no immediate readback proof, that invocation
returns no token and makes a best-effort `RECONNECT_REQUIRED` write. If the
store proves that block, later resolution fails closed. If the store also
cannot prove the defensive write, this code cannot claim an atomic multi-write
quarantine: a later fresh read can only reconcile the exact sealed state then
observable and never repeats the unresolved token POST. The current operation
still reports `CREDENTIAL_LOCAL_CHANGE_UNVERIFIED`.

Local removal is only a readback-verified local deletion; it does not claim
provider revocation. No test invokes a browser, a real OS credential store, an
OAuth endpoint, or a provider resource endpoint.

## Per-operation snapshots and evidence

Google Ads and GA4 report reads acquire the primary credential once before
identity discovery and retain that bearer through all provider requests. Google
Ads also acquires its static secondary developer token once. Google Ads
inventory has the same two-token snapshot. This prevents a protected refresh
from changing an identity/report binding within one sequential observation; it
does not make the observation atomic or prove current provider authority.

Focused fake-only validation covers profile/config raw-type rejection,
source/slot aliasing, detached-profile and bundle binding, opaque slot
snapshots, recovery and removal, service isolation, status routing, refresh
ambiguity, post-write expiry, and GA4's multi-request bearer snapshot. The
independent lifecycle review adds fake-store checks for generic isolation,
lifecycle transitions, HMAC compare-and-write, and bounded local readback.
No live OAuth enrollment, OS-store exercise, Google resource read, or release
credential-lifecycle claim follows from this evidence.

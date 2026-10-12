# Google OAuth protected credential lifecycle: independent review

This review exercised the protected Google OAuth bundle lifecycle with
in-memory, service-aware fake stores only. It did not open a browser, contact
Google, access an OS credential store, or use a real credential.

## Reviewed boundary

The review covered `google_oauth_credentials.py` together with its routing
through `credentials.py` and `credential_setup.py`:

- strict, canonical private-bundle decoding before any refresh dispatch;
- exact profile, client, principal, scope, account, and bundle-digest binding;
- opaque HMAC-bound pre-browser slot snapshots and compare-under-lock install;
- `READY`, `REFRESH_STARTED`, and `RECONNECT_REQUIRED` transitions;
- refresh-token expiry boundaries and refresh-response rotation handling;
- fixed service isolation from ordinary `OS_STORE` credentials; and
- source-aware status and local removal without exposing a bundle as a bearer
  credential.

The independent test module contains 14 fake-store regressions. Together with
the protocol and fixed-transport tests, the focused review group has 41
passing tests.

## Findings and repairs reviewed

The initial implementation stored the sealed OAuth bundle under the generic
`adhelm` credential service. A separate workspace could create
an `OS_STORE` profile with the same reference and receive the entire sealed
bundle from the generic `credential()` path as though it were a bearer token.
That was a credential disclosure and adapter-routing flaw.

The repaired lifecycle uses the fixed
`adhelm.google-oauth-v1` service while retaining the shared
reference-lock namespace. Independent regressions confirm both directions of
isolation: a generic profile cannot read an OAuth bundle, and generic local
removal cannot delete one. The reciprocal case also fails closed: a generic
secret at the same reference is never adopted as an OAuth bundle. OAuth
primary status and local removal now use the sealed service; Google Ads
secondary developer-token handling remains on the ordinary service.

The review also reproduced an immediate completion-readback ambiguity: a fake
store could retain a refreshed `READY` bundle while failing its required
readback. The current call returns no access token. The lifecycle now makes a
best-effort verified transition to `RECONNECT_REQUIRED` without masking the
original local-state ambiguity. If that defensive write cannot itself be
verified, a later process can only validate and reconcile the exact durable
bundle it then reads; a one-slot credential store has no atomic multi-write
proof that can distinguish every historical readback failure from a later
valid durable state. It never repeats the original token POST solely because
of this condition.

## Scope limits

This is local credential lifecycle evidence, not proof of Google account
ownership, provider resource access, developer-token validity, app review, or
authorization to mutate a provider. A structurally valid protocol dataclass is
also not cryptographic proof by itself; the workflow must obtain it from the
maintained ID-token verifier in the same enrollment flow.

The reviewed bundle is intentionally private. Public configuration, CLI
results, ledger records, and review output do not contain raw access tokens,
refresh tokens, client secrets, or OIDC subjects. A fresh provider identity or
resource read remains necessary before a workflow may make a current access
claim.

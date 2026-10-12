# Google OAuth GA4 Admin EDIT execution binding

`google_oauth_execution_binding.py` is an internal library handoff for a
future GA4 Admin key-event CREATE plan. It has no CLI command, configuration
writer, direct provider request, ledger append, or authorization grant. Its
resolver delegates protected-store reads and any fixed refresh transport to the
credential lifecycle; this module does not implement that I/O itself. It does
not prove that Google currently permits a CREATE request.

## Accepted profile

`resolve_ga4_admin_edit_execution_binding(profile)` first revalidates an
actual configured `Profile`. The only accepted target is:

| Field | Required value |
| --- | --- |
| credential source | `OS_OAUTH_GOOGLE` |
| provider | `ga4_admin` |
| purpose | `EDIT` |
| API scope | `https://www.googleapis.com/auth/analytics.edit` |

The mapping comes from
`GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE`; it is not reconstructed from
caller input. Static OS-store, CI, READ, widened, copied-invalid, and malformed
profiles fail before the protected bundle store is read. The lifecycle also
checks the sealed bundle's exact profile digest, provider, purpose, account,
derived principal, client ID, and API scope before it can return a bearer.

## One lifecycle snapshot

The credential lifecycle performs bundle decode, cache validation, and any
necessary refresh under one credential-reference mutation lock. The existing
`resolve_google_oauth_access_token` delegates to that same locked core, so the
M19 token resolver behavior and refresh/readback transitions remain the source
of truth. This binding path does not resolve the token a second time.

The locked core returns a private snapshot containing the current bearer,
nonsecret sealed-bundle association facts, access-token expiry, and its final
trusted local clock sample. The snapshot is a slots-only, non-dataclass object
with no generic serializer, copy, pickle, or useful representation. It omits
the raw OIDC subject, refresh token, client secret, bundle encoding, and bundle
state.

The public handoff is a slots-only
`Ga4AdminEditOAuthExecutionBinding`. It exposes only the profile reference and
digest plus the provider, purpose, source, account, derived principal, client,
and API scope. The bearer remains in a private weak registry keyed by that
specific binding. `bearer_for_ga4_admin_edit(binding, profile)` revalidates the
same profile, compares every exposed fact to the registry record, and consumes
the record before returning the bearer. A copied, constructed, altered,
different-profile, expired, or already consumed binding returns no token.

The issuer objects and private registry are in-process misuse guards. They do
not protect against arbitrary code with access to the same Python process, do
not make a caller-created object authoritative, and do not replace a later
plan's authorization checks.

## Local time boundary

The lifecycle records a final clock sample while it still holds the credential
lock. Binding creation takes a fresh lifecycle-clock sample and refuses a
rollback or expired access token; that later registration sample is retained
with the private bearer. Consumption takes another fresh sample and refuses a
rollback relative to the registration sample or expiry at the consume boundary.
A rejected binding is removed from the registry.

This is a local wall-clock guard around a short-lived in-process handoff. It
does not establish a provider-time guarantee, make the token current after the
method returns, or atomically observe an external process changing a protected
slot. A future egress path must still perform its own configuration, plan,
identity, drift, authorization, journal, and post-write verification gates.

## Focused evidence

`tests/test_google_oauth_execution_binding.py` uses a fake credential store and
fake refresh result. It covers exact sealed association, one refresh path,
static/CI/READ rejection before bundle access, altered and constructed binding
rejection, expiry and clock-rollback rejection, malformed protected-bundle
redaction, and serialization attempts against every bearer-bearing private
handoff. It does not invoke an OAuth endpoint, browser, OS credential store,
or GA4 endpoint.

The scoped reproduction command is:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_google_oauth_execution_binding.py \
  tests/test_google_oauth_credentials.py \
  tests/test_google_oauth_credentials_ultra_review.py \
  tests/test_google_oauth_edit_profile.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  src/adhelm/google_oauth_credentials.py \
  src/adhelm/google_oauth_execution_binding.py \
  tests/test_google_oauth_execution_binding.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check \
  src/adhelm/google_oauth_credentials.py \
  src/adhelm/google_oauth_execution_binding.py \
  tests/test_google_oauth_execution_binding.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy \
  src/adhelm/google_oauth_credentials.py \
  src/adhelm/google_oauth_execution_binding.py
```

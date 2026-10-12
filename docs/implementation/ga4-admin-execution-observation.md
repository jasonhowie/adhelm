# GA4 Admin EDIT execution-bound inventory observation

Status: local-only implementation with fake protected-store and HTTP evidence.
No Google request, browser flow, OS credential-store operation, plan,
authorization, key-event create, or other mutation was performed for this
slice.

`ga4_admin_execution_observation.py` produces one internal typed
`Ga4AdminEditExecutionInventoryObservation` for the later GA4 key-event CREATE
planning path. It is not a CLI result, ledger record, plan, grant, or provider
permission assertion. A future caller must apply its own redaction before any
agent-visible output.

## Closed entry points

`observe_ga4_admin_edit_execution_inventory(directory, profile_id,
stream_id=..., fingerprint_key=..., expected_configuration_digest=...)`
performs the complete local preflight, lifecycle handoff, fixed read, marker
check, and result construction. It calls the M23 OAuth resolver itself and
consumes its one-use bearer exactly once. It never accepts a caller-supplied
bearer, principal, binding, inventory, or identity as authorization.

`validate_ga4_admin_edit_execution_observation(...)` is a local preflight
helper for the factory. It returns configuration objects only inside the local
workflow; it is not a result or serialization surface. The returned execution
observation carries no configuration object: it retains only the exact
configuration digest and closed allowlisted profile/binding facts.

`revalidate_ga4_admin_edit_execution_observation(value, fingerprint_key=...)`
accepts only the exact typed observation, rebuilds its closed JSON surface, and
rechecks the inventory's two current-key markers and the observation-content
marker. Constructed, copied, malformed,
or wrong-key values fail before reuse. The private serialized surfaces reject
malformed constructed/copy-bypassed models. A valid-shape copied observation is
not authenticated merely because it can be serialized: every reuse, retention,
or future agent-visible projection must call this revalidator with the current
fingerprint key first.

## Source and read boundary

Preflight accepts exactly one configured `Profile` with all of these values:

| Field | Required value |
| --- | --- |
| credential source | `OS_OAUTH_GOOGLE` |
| provider | `ga4_admin` |
| purpose | `EDIT` |
| API scope | `https://www.googleapis.com/auth/analytics.edit` |
| target IDs | configured numeric property and selected numeric web stream |

Static OS-store, explicit-CI, READ, widened, malformed, and constructed profile
values fail before fingerprint-key validation, protected-bundle resolution, or
HTTP. The M23 binding lifecycle independently checks the sealed bundle's exact
profile digest, provider, purpose, account, derived principal, client, and API
scope before it releases the short-lived bearer.

The module captures that bearer in a local closure and gives it to the existing
fixed inventory client. The client makes only the projected property GET,
selected web-stream GET, and bounded `v1alpha` key-event collection GET. All
three requests use the same captured bearer. The returned observation contains
neither the bearer, refresh token, raw OIDC subject, bundle bytes, nor raw
provider response.

The observation binds these nonsecret facts:

- the canonical full configuration digest and canonical exact Profile digest;
- the profile reference; GA4 property reference/native ID; derived principal;
  client reference; and exact API scope;
- the exact configured target, complete typed normalized inventory, and
  inventory digest;
- trusted UTC read start and finish times; and
- current local fingerprint-key and keyed-content marker matches; and
- a separate domain-separated observation-content marker over every closed
  observation fact, including the configuration/profile binding, exact target,
  read interval, statuses, and inventory digest.

`SEALED_LOCAL_OAUTH_BUNDLE_ASSOCIATION` means only that the lifecycle paired
the configured profile with its locally protected validated OAuth bundle. It is
not proof of current Google CREATE permission, granted-scope completeness,
reservation, current provider state after the GETs, remote resource ownership,
or execution authority. The result explicitly keeps those limits as
`NOT_PREPROVABLE`, `NOT_EVALUATED`, `NOT_ESTABLISHED`, and `false` controls.
The read remains `SEQUENTIAL_NONATOMIC`.

## Drift, time, and local-key checks

The factory compares the expected configuration digest before OAuth resolution,
after resolution, after binding-fact extraction, after bearer consumption,
after the fixed GET sequence, after current-key revalidation, and immediately
before return. It does not hold a configuration or ledger lock across HTTP. A
mismatch detected at any comparison returns `CONFIGURATION_DRIFT`; an edit that
changes and restores the exact digest between samples is not observable. The
result therefore does not establish interval-atomic configuration currentness.

The read interval uses a private trusted UTC clock seam. Invalid timestamps or a
finish earlier than start fail closed. These local clock samples do not establish
provider time or atomicity with external configuration edits.

The private fingerprint key is required locally and is never retained in the
result. Fresh inventory is revalidated against both the key-continuity marker
and the distinct keyed-content marker before construction and again in the
explicit revalidator. A matching marker proves only local keyed-content
integrity for that supplied key; it does not prove provider origin, remote
completeness beyond the fixed reader's bounded result, or current permission.
The observation-level marker is separate from the inventory marker because it
also detects changed local configuration/profile facts or a rewritten read
interval. It remains local keyed integrity, not an authorization or provider
claim.

## Focused evidence

`tests/test_ga4_admin_execution_observation.py` uses a memory-only protected
store, patched lifecycle clock, and `httpx.MockTransport`. It covers one
protected snapshot and one bearer across all fixed GETs, source/profile and
preflight rejection before key/lifecycle access, configuration drift before and
after bearer/read/revalidation, returned marker and target tampering,
constructed/copy serializer bypasses, wrong-key rejection, UTC clock rollback,
and unexpected lifecycle-error redaction. It does not access an OS credential
store, browser, network, or Google account.

Run the focused checks with:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_ga4_admin_execution_observation.py \
  tests/test_google_oauth_execution_binding.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  src/adhelm/ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check \
  src/adhelm/ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy \
  src/adhelm/ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation.py
```

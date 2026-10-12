# GTM Web measurement read workflow

Status: fixture-tested local composition; not live capability evidence.

`read_gtm()` composes the fixed GTM v2 account/container identity discovery and
bounded inventory reader without adding a CLI command, ledger record, artifact,
workspace operation, staging decision, browser action, or remote mutation.  It
returns only `GtmIdentityDiscovery` and `GtmReadInventory`; callers own any
observation interval and durable measurement receipt.

## Preconditions and identity boundary

`validate_gtm_read(root, profile_id, *, container_id, ci_enabled=False)` is a
pure local preflight.  It requires one configured `gtm` profile with purpose
`READ`, exactly the documented
`https://www.googleapis.com/auth/tagmanager.readonly` scope, and an ASCII
numeric raw account identifier.  The public container identifier is likewise a
bounded ASCII numeric value.  A missing selected GTM profile returns
`NOT_CONFIGURED_OPTIONAL`, leaving unrelated configured providers untouched.

The workflow validates public input, private 32-byte fingerprint key, and the
detached/revalidated profile before reading a credential.  It obtains one
printable-ASCII credential snapshot and closes both discovery and all inventory
pages over that snapshot in one `httpx.Client(trust_env=False)` session.  It
allows only `tagmanager.googleapis.com`.  For a profile enrolled through
`auth google-oauth` the snapshot is the protected Google OAuth bearer, refreshed
before the first request when it is near expiry; see
[GTM auth lifecycle](gtm-auth-lifecycle.md).

The identity client makes the documented fixed GET reads:

1. `accounts/{account}`
2. `accounts/{account}/containers/{container}`

Both responses must bind exact native IDs and paths, and the container must
declare exactly `usageContext: ["web"]`.  `GtmReadClient` then receives the
exact discovery object plus `GtmIdentityClient.is_current_identity`; a declared,
cloned, stale, or failed-refresh identity cannot authorize inventory.  The
reader performs its documented bounded sequence: latest header, live version,
latest version, workspace pages, then latest-header and live-version rereads.
A mismatch or a late page error raises without a partial workflow result.

## Output and limits

Provider display metadata, domains, URLs, names, notes, provider fingerprints,
workspace contents, tag/template/custom HTML source, and credential values are
transient.  The normalized result has only the existing typed discovery and
inventory models: canonical local references with the required numeric
account/container binding fields, declared feature availability, keyed
configuration fingerprints, bounded counts, opaque custom-code markers, and
explicit `SEQUENTIALLY_REREAD_NONATOMIC` / `NOT_EVALUATED` status.  It does not
establish workspace completeness, dependency proof, staging eligibility,
preview/compiler status, browser behavior, consent behavior, GA4 receipt, or
publish readiness.

The route and response constraints derive from the documented GTM API v2
[authorization scopes](https://developers.google.com/tag-platform/tag-manager/api/v2/authorization),
[accounts.get](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts/get),
[containers.get](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers/get),
[live version](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions/live),
[latest version header](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.version_headers/latest),
and [workspace list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces/list).
The executable behavior is mocked; live provider authorization, full inventory
coverage, and the complete GTM measurement workflow remain release gates.

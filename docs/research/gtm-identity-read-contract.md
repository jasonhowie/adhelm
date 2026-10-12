# GTM v2 identity-read contract

This record covers a fixed, read-only identity proof for an already selected
GTM account and Web container. It is fixture-tested only and does not add a CLI
command, provider credential flow, inventory, workspace operation, staging
proof, or live-capability claim.

## Fixed reads and authorization

The adapter exposes exactly these logical `GET` routes, after locally validating
the ASCII numeric account and container identifiers. The shared bounded-read
transport may repeat either route on its documented retryable read failures:

| Read | Fixed path |
| --- | --- |
| account | `accounts/{account}` |
| container | `accounts/{account}/containers/{container}` |

The documented methods are [accounts.get](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts/get)
and [accounts.containers.get](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers/get).
Both use `https://tagmanager.googleapis.com/tagmanager/v2/{path}`, take an
empty request body, and accept the readonly scope
`https://www.googleapis.com/auth/tagmanager.readonly`.

The documented [Account resource](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts)
has `path`, `accountId`, display metadata, `shareData`, a fingerprint, UI URL,
and feature flags `supportUserPermissions` and `supportMultipleContainers`.
The documented [Container resource](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers)
has parent IDs/path, display and URL metadata, feature flags, and
`usageContext`; its documented context enum includes `web`, mobile variants,
`amp`, and `server`.

## Binding and output boundary

Discovery validates public inputs before obtaining a credential or issuing HTTP,
then obtains one printable-ASCII token snapshot and uses it for both reads. It
requires exact response paths and matching native account/container IDs. The
container must declare exactly `["web"]`; missing, unspecified, server,
mobile, AMP, or mixed contexts fail closed.

The normalized result includes only `GtmReadIdentity`, local canonical account
and container references (`gtm.account.{id}`, `gtm.container.{id}`), and
three-state feature availability: `TRUE`, `FALSE`, or `UNKNOWN`. Missing or
unavailable documented features remain `UNKNOWN`; they are never normalized to
`FALSE`. Names, domains, URLs, provider fingerprints, notes, tag IDs, and all
other account/container metadata are only bounded and validated transiently.
They are not returned, logged, fingerprinted, or retained here.

Each discovery call clears prior proof before input validation. A discovery
identity is current only when it is the exact object retained by that successful
discovery and its public model still validates and binds to the configured local
references. This supports the mandatory `GtmReadClient` session verifier while
rejecting cloned, caller-constructed, stale, or failed-refresh identities.

Unknown response fields, malformed metadata, response parent/path mismatch,
invalid authorization tokens, and non-Web containers fail before any discovery
result. This read does not prove resource inventory, feature-gated resource
availability beyond the exposed flags, live/latest state, staging eligibility,
preview/compiler status, browser firing, consent behavior, GA4 receipt, or
publish readiness.

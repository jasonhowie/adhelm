# Paired GA4 Admin resource-access checks

`measurement ga4-admin-access --read-profile <read> --edit-profile <edit>
--stream-id <stream>` performs a bounded diagnostic for a separately configured
GA4 Admin READ and EDIT profile. `schema measurement-ga4-access` exposes its
closed input. The existing local `doctor` and retained `measurement ga4-admin`
commands retain their original read-only behavior.

Both profiles must declare the same native property, currency, and IANA timezone.
READ requires exactly `analytics.readonly`; EDIT requires exactly
`analytics.edit`. Their profile IDs and credential references must differ, and
the check rejects two references resolving to the same token. Principals and
client applications may differ: separate operating roles need not belong to
the same Google identity. CI credentials require explicit `--ci-enabled`.
Configuration changes detected during the diagnostic reject its result.

The command snapshots each credential once and uses a separate HTTP session
for that role. It reads only the fixed v1beta property and explicitly selected
web-stream resource, using fixed partial-response fields. No collection
enumeration, access-binding/user lookup, key-event create, or permission probe
is performed. Environment proxy routing and redirects are disabled. The
project's existing bounded response and read-retry controls apply.

The two observations must agree on property, stream, currency, timezone, and
reported service level. A missing or unspecified service level remains
`NOT_AVAILABLE`; it never implies Standard or a resource capacity. Sequential
GETs are not an atomic provider snapshot. The check reports detected drift,
not a promise that the remote state will remain unchanged after it returns.

## What a passing diagnostic means

The compact result proves only that this invocation completed the fixed
resource reads using each configured role. It deliberately reports:

- `granted_scopes: NOT_EVALUATED`: declared scopes and successful reads do not
  establish the complete OAuth grant, including when textual scope metadata
  was absent from a token response;
- `create_permission: NOT_PREPROVABLE`: these GETs cannot establish permission
  to execute `keyEvents.create`; the reviewed method has no documented dry run;
- key-event inventory and mutation readiness as unevaluated/unevidenced; and
- false execution authority, remote mutation, persistence, and atomicity flags.

No native IDs, profile/principal/client references, credentials, raw provider
responses, property names, measurement IDs, or site URLs enter the command
result. It creates no ledger observation, artifact, plan, grant, or permission
receipt. It uses neither the local sharing key nor an approval key. Default
output is preflighted before credentials or provider reads and fits the smallest
configured 1,024-byte result bound.

A real provider denial is reported for the READ or EDIT resource-read phase;
it is not described as a create denial. A future GA4 write still needs the
dedicated compiled plan, current state and contract checks, exact direct or
covered delegated authorization, durable pre-egress start, one write attempt,
readback, and bounded recovery. Configuration readback does not prove browser
behavior, native receipt, Realtime or processed reporting.

## Evidence and provider references

Tests use synthetic HTTP, credential, and configuration seams. No Google
account, OAuth endpoint, browser, or OS credential store was invoked. Live
partial-response conformance and account access remain unverified.

The official [property GET](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties/get)
and [selected data-stream GET](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams/get)
accept either the readonly or edit scope. The
[property resource](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties)
defines service level. The
[key-event create method](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents/create)
requires `analytics.edit`; this does not turn the access diagnostic into a
create-permission test. Resource-capacity enforcement is a separate later gate;
the [configuration limits](https://support.google.com/analytics/answer/12229528?hl=en)
must not be inferred from this client's local response bounds.

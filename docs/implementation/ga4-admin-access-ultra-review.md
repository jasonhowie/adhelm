# GA4 Admin access ultra review

This independent review covers the paired GA4 Admin resource-access diagnostic:
the fixed-host access client, profile-pair workflow, installed CLI/schema
registration, and bounded result path. It uses only temporary configuration and
fake `httpx` transports. It makes no provider, credential-store, sharing-key,
ledger, native-host, or mutation call.

The fixed request shape is supported by Google's standard partial-response
contract: the [`fields` parameter applies to requests that return response
data](https://developers.google.com/workspace/forms/api/guides/performance).
The current [Analytics Admin v1beta discovery
document](https://analyticsadmin.googleapis.com/$discovery/rest?version=v1beta)
identifies both `properties.get` and `properties.dataStreams.get` as GETs that
return their respective resources under the readonly or edit scope. The
requested property fields (`name,timeZone,currencyCode,serviceLevel,deleteTime`)
and data-stream fields (`name,type`) are documented by the [Property
resource](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties)
and [DataStream
resource](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams).

The review reproduced that a normal full web-stream response can include
`webStreamData.defaultUri`, which is outside the product's raw-response privacy
boundary. The access client now asks only for the fixed projections and rejects
every response key outside them before constructing an identity. This retains
the shared raw-response privacy validation instead of weakening it. A selected
`WEB_DATA_STREAM` is established from its typed `name` and `type`; it does not
collect a measurement ID, default URI, display name, or stream metadata.

The review also reproduced that a caller-supplied `httpx.Client` default query
parameter was merged into each otherwise fixed GET. The access-client
constructor now rejects a nonempty default-query configuration before reading a
token or making HTTP. The installed workflow already builds a blank client; the
new check keeps the exported fixed-read adapter from silently widening its
query shape.

The diagnostic proves only two non-atomic fixed resource reads under distinct
configured profile and resolved-credential snapshots. It does not prove granted
scopes, current create permission, key-event absence, a duplicate check, cap
headroom, authorization, execution readiness, or mutation authority. In
particular, Google documents a 30-key-event limit for Standard properties in
its [configuration limits](https://support.google.com/analytics/answer/12229528?hl=en-EN),
but an observed service level alone is not a current capacity fact and is not
turned into one here.

The independent tests in
`tests/test_ga4_admin_access_ultra_review.py` cover:

- all four exact GET targets, fixed decoded field masks, no body, fixed host,
  no redirects, and a single token snapshot per profile;
- result redaction and explicit `NOT_EVALUATED` / `NOT_PREPROVABLE` /
  `NOT_EVIDENCED` controls;
- missing service level becoming `NOT_AVAILABLE`, never an inferred Standard
  property;
- same resolved credentials stopping before edit-profile egress;
- same-profile and malformed/constructed request rejection before credential
  or provider access;
- nonempty caller-supplied default query parameters rejected before token or
  provider access;
- output-limit preflight stopping before sharing-key access, credential reads,
  or HTTP; and
- configuration drift after the read observation blocking the edit credential
  and egress; and
- provider responses containing an unrequested property or web-stream field
  failing closed without reflecting the private value.

The focused independent suite passed 12 tests. The combined access,
precondition, and GA4 candidate review group passed 76 tests. Ruff check,
Ruff format check, source-only mypy for the access/workflow/CLI modules, and
the diff check passed. This review does not establish live Google conformance,
OAuth-grant proof, current GA4 create permission, current cap or duplicate
facts, cross-process atomicity, remote authorization, or v1 completion.

The existing `Ga4DataClient.discover_identity` still consumes full property and
stream responses in separate legacy GA4 reporting/Admin-read flows. A normal
full stream response may contain `defaultUri`, so Root has recorded a separate
follow-up to migrate those legacy fixed reads to closed partial projections.
That source is intentionally outside this M20 diagnostic slice.

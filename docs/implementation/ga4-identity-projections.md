# GA4 identity projections

`Ga4DataClient.discover_identity()` now obtains only the fields needed to bind
the existing local `Ga4Identity` before GA4 aggregate reporting or the retained
Admin inventory flow continues. It does not change GA4 report queries,
aggregate response parsing, identity references, identity wire shape, or
identity digest inputs.

The property request is the fixed empty-body GET for
`properties/{property_id}` with:

```text
fields=name,timeZone,currencyCode
```

The fixed empty-body data-stream list GET uses `pageSize=200` and:

```text
fields=dataStreams(name,type),nextPageToken
```

Google documents the [property
GET](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties/get)
and [data-stream list
GET](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams/list)
as readonly-or-edit-scope resource reads with the stated paths, empty request
bodies, and bounded `pageSize` maximum. Google's standard [partial-response
guide](https://developers.google.com/workspace/forms/api/guides/performance)
documents `fields` for response-returning requests, including nested collection
subselectors. The current exact [Analytics Admin v1beta discovery
document](https://analyticsadmin.googleapis.com/$discovery/rest?version=v1beta)
is the source for these resource methods and schemas.

The identity parser rejects every outer property/list key and every stream
member key outside those projections. It accepts a web stream solely when its
projected `type` is `WEB_DATA_STREAM`; it never reads `webStreamData`, a
measurement ID, default URI, display name, package identifier, or application
bundle identifier. The shared raw-response privacy validator remains in place,
so a provider response that ignores the projection and includes prohibited
content fails without reflection or persistence.

List traversal is bounded to five pages and 1,000 unique stream identifiers.
Each member must belong to the selected property and use a known GA4 stream
kind. Duplicate resource identifiers, repeated page tokens, an empty page with
a continuation token, malformed pagination values, and a continuation after
the fifth page fail closed. A caller-supplied default HTTP query configuration
is rejected before token access because it could otherwise widen the fixed
request shape.

This is identity discovery only. It neither inventories key events nor proves
their absence, capacity, OAuth grant completeness, create permission, current
state, mutation readiness, authorization, or browser/runtime behavior. It
creates no plan, artifact, approval, ledger record, or remote mutation.

The synthetic focused tests in `tests/test_ga4_identity_projection.py` cover
the exact masks and pages, preserved identity shape, safe rejection of ignored
full response fields, original scalar/type failures, duplicate and cyclic
pagination, page bounds, and default-query rejection before token or HTTP
access. No Google endpoint, credential store, browser, or provider account was
called. The focused projection suite has 13 passing cases; the related GA4
identity, measurement-workflow, report-CLI, and integration group has 47
passing cases. Ruff, mypy for `src/adhelm/providers/ga4.py`, and `git diff
--check` pass for this slice.

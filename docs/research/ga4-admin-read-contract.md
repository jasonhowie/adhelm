# GA4 Admin fixed read contract

Last verified: 2026-09-04. This fixture-tested adapter makes no live capability
claim and is not CLI-integrated. Its caller supplies a session identity verifier; it fails before token
access or HTTP unless the verifier proves the current configured property and
web stream. It uses the Google Analytics Admin API `v1alpha` fixed GET
operations below with `https://www.googleapis.com/auth/analytics.readonly`:

| Resource | Fixed path | Returned local metadata |
| --- | --- | --- |
| Key events | `/v1alpha/{parent=properties/*}/keyEvents` | identity-bound resource reference, counting method, custom/deletable flags, exact bounded Decimal default value and ISO currency, keyed fingerprints |
| Event create rules | `/v1alpha/{parent=properties/*/dataStreams/*}/eventCreateRules` | identity-bound reference, keyed configuration fingerprint, condition/mutation counts, source-copy flag, opaque-content unsupported status |
| Event edit rules | `/v1alpha/{parent=properties/*/dataStreams/*}/eventEditRules` | identity-bound reference, keyed configuration fingerprint, condition/mutation counts, exact signed-int64 processing-order string, opaque-content unsupported status |

Google documents `pageSize` as a maximum of 200 for all three lists and
returns an optional `nextPageToken`; this adapter sends `pageSize=200`, accepts
at most five pages per collection, rejects duplicate resources/repeated cursors,
and returns an inventory only after every selected list ends without a next
token. The official resources are [KeyEvent](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents), [EventCreateRule](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.dataStreams.eventCreateRules), [EventEditRule](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.dataStreams.eventEditRules), [MatchingCondition](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/MatchingCondition), and [ParameterMutation](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/ParameterMutation).

The caller must supply both a current session verifier and a private stable
HMAC key. Before credential access, the adapter binds `property_ref` and
`web_stream_ref` to their numeric native IDs using its configured reference
function, then invokes the verifier. It refuses mismatched labels, a missing
verifier proof, or a short key before any HTTP request. The key is never stored
in the inventory or sent to Google.

Event-create and event-edit resources have different documented shapes.
Create rules carry `destinationEvent` and the optional
`sourceCopyParameters`; edit rules carry required `displayName` and
`processingOrder` and have no source-copy setting. Conditions and mutations
may name undeclared parameters or contain values, regular expressions, URLs, or
user-related text. They are inspected only in memory for the exact documented
shape, then represented by a domain-separated local HMAC-SHA-256 fingerprint.
The inventory never retains a raw value, public SHA-256 digest of raw content,
display name, destination, parameter name/value, URL, page text, custom code,
or user data. Every normalized event rule carries
`OPAQUE_RULE_CONTENT_UNSUPPORTED`, so the inventory does not claim semantic
coverage of its conditions or mutations.

This is a read-only inventory surface. It has no create, patch, delete, reorder,
conversion-event, data-stream, measurement-protocol-secret, custom-dimension,
or generic Admin API query methods. The existing GA4 Data API reporting profile
remains read-only. A future Admin edit operation requires a separately
configured, separately authorized profile and a complete guarded mutation
contract; this module provides no such authority.

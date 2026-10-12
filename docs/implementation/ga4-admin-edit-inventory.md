# GA4 Admin EDIT key-event inventory

`Ga4AdminEditKeyEventInventoryClient` is a fixed, read-only prerequisite
adapter for the configured GA4 Admin EDIT role. It is separate from the
historical `Ga4AdminReadClient` inventory and never represents an EDIT read as
`analytics.readonly` evidence.

The caller supplies a closed `Ga4AdminEditInventoryTarget`: deterministic
property and selected Web-stream references with numeric IDs, expected IANA
timezone and ISO currency, `profile_purpose: EDIT`, `v1alpha`, and exactly
`https://www.googleapis.com/auth/analytics.edit`. The configured workflow must
validate the actual Profile before it builds this target; the adapter itself
does not receive a Profile or credential reference.

## Fixed request sequence

`read_inventory(target)` first reconstructs the target, then captures the EDIT
token exactly once. That one token is used for all fixed reads:

1. v1beta property GET through `Ga4AdminAccessClient`;
2. v1beta selected-Web-stream GET through the same client; and
3. one or more v1alpha `GET /v1alpha/properties/{property}/keyEvents` calls.

The access observation must match the target's property ID, stream ID, currency,
and timezone before any list request. There is no caller-supplied access
identity, so a detached model cannot bind a different credential to the list.
The list sends no request body and uses only:

```text
pageSize=200
fields=keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),nextPageToken
```

The adapter permits at most five pages and 1,000 local members. Each page is
closed, bounded, privacy-screened, and must progress through a unique bounded
page token. Duplicate IDs, unknown fields, malformed parents, or a pagination
cycle fail closed. It has no write method, generic endpoint, redirect handling,
resource-creation probe, delegated grant, or mutation retry.

Google documents this v1alpha GET path, empty body, `pageSize` maximum 200,
pagination token, and OAuth scopes in [properties.keyEvents.list](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents/list).
Although Google permits either readonly or edit for the list, this adapter
requires the distinct configured EDIT role. The exact selected member fields
come from the [KeyEvent resource](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents).
`required_scope` is a declared local target constraint, not proof that the
captured bearer has that scope: successful GETs do not establish granted scope
metadata or `keyEvents.create` permission. The workflow projection reports
those facts separately as unevaluated/not preprovable.

## Observed capacity facts

The closed `Ga4AdminEditKeyEventInventory` contains opaque member fingerprints,
complete observed count, pagination declaration, target, and local markers. It
does not contain event names, tokens, raw provider responses, Profiles,
principals, client references, or request bodies.

For `GOOGLE_ANALYTICS_STANDARD`, nested
`Ga4AdminEditStandardKeyEventCapacity` records the complete observed count,
literal cap `30`, nonnegative remaining slots, and a below-cap or at-or-above-cap
status. The official [GA4 configuration limits](https://support.google.com/analytics/answer/12229528?hl=en)
currently list 30 key events per Standard property. At or above that count does
not assert an eventual create outcome.

For `GOOGLE_ANALYTICS_360` or unavailable service level, the result preserves
the complete count but gives only
`CAP_NOT_EVIDENCED_FOR_OBSERVED_SERVICE_LEVEL`. It deliberately does not invent
a 360 or unknown-property capacity. A later preparation flow requiring a cap
must fail closed for those statuses.

## Keyed integrity and revalidation

Fresh results carry the existing static `inventory-key-marker`, the established
opaque `key-event` and `key-event-name` member HMACs, and a distinct
`edit-key-event-inventory-content-marker`. The last HMAC covers this versioned
EDIT-inventory shape: target, capacity, sorted members, pagination declaration,
and static key marker. Its different purpose prevents conflating this
one-collection EDIT surface with the historical READ inventory surface.

`revalidate_ga4_admin_edit_key_event_inventory(value, fingerprint_key=...)`
is the required public boundary before a constructed, copied, raw, retained, or
otherwise detached inventory is consumed. It accepts only the exact typed model
or a JSON-shaped mapping, checks original scalar types before normalization,
rebuilds the closed model, verifies the fixed key marker and then recomputes the
content marker. Nested default values must retain their normalized decimal
string and exact currency-string representation; a number, Boolean/float alias,
changed member, count, capacity, marker, or custom Python scalar fails closed.
Token-getter failures are reduced to a fixed access diagnostic before any HTTP
request. The fingerprint key stays transient and is never serialized, returned,
or logged.

Matching markers prove only local keyed content integrity and continuity. They
do not prove Google response origin, remote currentness/completeness after the
sequential reads, OAuth grant completeness, current create permission, capacity
reservation, consent, conversion semantics, authorization, or execution
authority. A later write still needs an exact plan and authorization, current
drift read, durable pre-egress journal, one bounded attempt, reconciliation,
readback, and browser/Realtime/processed-report verification.

The small `_parse_ga4_admin_key_event` extraction in `ga4_admin.py` preserves
valid historical READ parser inputs, HMAC purposes, default-value normalization,
and local reference mapping. The EDIT adapter composes it with only its own
EDIT identity check, one-collection pager, capacity model, and distinct content
seal.

`tests/test_ga4_admin_edit_inventory.py` covers one-token sequencing, fixed
partial fields, pagination, Standard/non-Standard capacity, constructed target
bypasses, identity/currency/timezone mismatches, duplicate/cyclic pages,
provider-field rejection, key/transport validation, content-seal separation,
and public revalidation. Tests use only fake `httpx` transports; no account,
OAuth endpoint, browser, OS store, or provider operation was used.

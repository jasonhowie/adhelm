# GA4 Admin key-event CREATE semantic state

`src/adhelm/providers/ga4_admin_create_state.py` is a pure local reconstruction
boundary for a future fixed GA4 Admin key-event `CREATE`. It does not read a
provider, access credentials, create an execution plan, authorize a mutation,
or persist evidence.

The module exposes three related boundaries:

- `project_ga4_admin_edit_key_event_semantic_collection(inventory, *, fingerprint_key)`
  validates one complete keyed EDIT inventory and returns its opaque semantic
  `StateRef`, member count, target-binding fingerprint, and separate full
  inventory digest. It intentionally does not perform duplicate or capacity
  checks, so a later post-create reread can use it after the proposed key event
  exists.
- `build_ga4_admin_key_event_create_semantic_state(fresh, inventory, *,
  fingerprint_key)` strictly rebuilds the fresh create input and EDIT
  inventory, invokes the existing EDIT duplicate/capacity precondition, and
  returns a closed predicted before/after transition.
- The two `revalidate_*` functions reparse a mapping or exact typed value,
  enforce original JSON scalar types and bounded shape, then compare it with a
  keyed reconstruction from the supplied fresh facts. A model copy or
  constructed value is therefore not a trusted state binding.

## State projection

GA4 assigns the created key-event resource ID. The projection deliberately
does not predict that ID or derive a state from a full inventory digest. Each
observed member is HMAC-bound under a separate local domain to exactly:

- the existing event-name HMAC;
- its observed counting method; and
- its exact normalized default-value decimal string and currency, when present.

Member fingerprints are sorted but never deduplicated. Two resources with the
same request-controllable semantics therefore remain distinct from one. The
collection state binds the target's property, stream, API version, required
scope, configured timezone/currency, and the current fingerprint-key marker,
but emits only opaque HMAC fingerprints. No raw event name, native identifier,
or marker is exposed by either public output.

`COUNTING_METHOD_UNSPECIFIED` remains a distinct observed value. A proposed
create can only use `ONCE_PER_EVENT` or `ONCE_PER_SESSION`; it never normalizes
an unknown observed value into either one. Default values are read from exact
JSON decimal strings. The post-create comparison accepts only the normalized
provider representation supplied through the existing fixed reader; it does
not treat a different numeric spelling as equivalent.

The predicted `after` collection is the sorted `before` collection plus one
proposed semantic member. A later fixed reader can project a reread inventory
and compare its `StateRef` to this predicted state even though the provider
selected a new resource ID. Extra matching resources, a changed counting
method, or a changed default value produce a different state.

## Drift and non-claims

The state result also retains the full keyed EDIT-inventory digest separately.
That digest changes for resource IDs, create times, `custom`, `deletable`, and
other normalized inventory facts excluded from the semantic collection. A
future pre-apply path must compare it during its required reread; matching the
semantic `before` alone is not a broad inventory-drift proof.

Matching the current fingerprint-key marker and content marker proves only
local keyed-content integrity for the supplied normalized inventory. It does
not prove provider origin, current remote state, grant scope, CREATE
permission, name reservation, consent enforcement, execution authority, or a
remote mutation outcome. The output carries the existing explicit
`NOT_ESTABLISHED` / `NOT_EVALUATED` / false-authority statuses. A later guarded
adapter must still perform its own current rereads, expiry and authorization
checks, fixed POST handling, independent readback, and reconciliation without
inventing a delete or pause compensation path.

## Validation coverage

`tests/test_ga4_admin_create_state.py` uses only `httpx.MockTransport` to
construct normalized fixture inventories. It covers redaction, sorted and
multiplicity-preserving state, unknown counting semantics, an arbitrary
provider-assigned post-create ID, ID/custom/deletable/create-time exclusions,
duplicate/counting/default drift, exact decimal-string behavior, duplicate and
Standard-cap preconditions, keyed revalidation, constructed-model controls,
and bounded untrusted mappings.

The create-state count is also bound to the complete supplied-inventory total
inside its preconditions: the predicted transition has exactly one additional
semantic member, and its baseline count cannot describe a different inventory.

# GA4 Admin supplied-inventory preconditions

`evaluate_ga4_key_event_create_preconditions()` is a pure, local check for one
fresh `Ga4AdminKeyEventCreateInput` against one supplied
`Ga4AdminInventory`. It recompiles the input through the installed fixed GA4
Admin compiler and returns only a closed
`Ga4AdminKeyEventInventoryPreconditions` result:

- the fresh compiler candidate digest;
- `COMPLETE_SUPPLIED_INVENTORY` as the scope of the supplied-object check;
- `MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY`;
- `MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT`;
- `NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY`; and
- explicit `NOT_ESTABLISHED` / `NOT_EVALUATED` state, capacity, and permission
  facts with false remote-side-effect and execution-authority controls.

The result deliberately contains no candidate, event name, field name, native
property or stream reference, request, inventory, marker, or fingerprint key.
It is a duplicate precondition result, not a plan or authorization object.

## Closed input boundary

The helper accepts only the exact typed create-input and inventory classes. It
serializes each model with Pydantic warnings elevated to errors, parses its
original JSON with duplicate-key and byte bounds, and checks the complete raw
object shape and scalar types before RFC 8785 canonicalization. In particular,
Boolean or floating aliases cannot become fixed integer or Boolean controls.
It reparses both models and requires their complete normalized JSON shape to
match before compiling or comparing anything.

For the supplied inventory, this validation covers every nested key event and
event-rule member: fixed literals, counts, digests, deterministic local
resource references, property and stream parents, rule-kind metadata, and
per-collection resource uniqueness. The create input must use its deterministic
property and stream references and fixed Admin version/edit scope. The two
objects must then name exactly the same native property and Web stream. A
custom resolver label, a rehashed member parent, a constructed nested scalar,
or an abbreviated model fails closed.

## Keyed duplicate comparison

The caller supplies transient private fingerprint key material. It must be an
exact `bytes` value between 32 and 4,096 bytes; the helper never stores,
returns, logs, or derives a public representation of it.

Before it derives an event-name fingerprint, the helper requires both a
syntactically valid key-continuity marker and content marker. It compares the
first in constant time with the fixed current-key marker, then recomputes the
separate `inventory-content-marker` HMAC over the full normalized supplied
surface (identity, all members, pagination declaration, and key marker;
excluding only the content marker itself). A key-marker-only M18 inventory
returns `GA4_FINGERPRINT_INVENTORY_CONTENT_NOT_EVIDENCED`; altered, reordered,
removed, or foreign member fingerprints return
`GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH`. A missing key marker returns
`GA4_FINGERPRINT_KEY_CONTINUITY_NOT_EVIDENCED`, and a different key returns
`GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH`. None of those paths reveals whether
an event-name fingerprint matched.

Only after both markers verify does the helper HMAC the freshly compiled event
name under the existing `key-event-name` domain and reject a match with
`GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY`. The content marker establishes
only local keyed integrity of the supplied normalized surface: any holder of
the private key can make one. It does not authenticate a provider response,
prove reader origin, retention, current remote state, provider completeness,
capacity or permission, bind consent or event semantics, reserve a name, make
the check atomic with a future write, or authorize anything. A later GA4 write
still needs current complete inventory, capability and access checks, an exact
plan and authorization, pre-apply drift read, bounded write recovery, and
independent readback/verification. The helper has no provider, credential,
browser, operating-system, file, ledger, or network surface.

Legacy markerless and M18 key-marker-only inventory serialization are not
changed. The helper returns an evidence-unknown diagnostic rather than
rewriting, adding a null marker to, or recanonicalizing historical bytes.

`tests/test_ga4_admin_preconditions.py` covers matching and mismatched local
keys, markerless and key-marker-only compatibility, content-seal ordering,
foreign/member/reordering bypasses, deterministic property/stream bindings,
nested constructed scalar/reference/member bypasses, bounded fingerprint-key
validation, result redaction, and the explicit non-authority status fields
using fixture-only Admin reads.

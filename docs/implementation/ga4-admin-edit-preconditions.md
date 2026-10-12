# GA4 Admin EDIT supplied-inventory create preconditions

`evaluate_ga4_edit_key_event_create_preconditions()` is a pure local
prerequisite for a later GA4 key-event CREATE plan. It accepts only a fresh
`Ga4AdminKeyEventCreateInput`, one exact typed
`Ga4AdminEditKeyEventInventory`, and transient private fingerprint-key bytes.
It has no configuration, credential, provider, file, ledger, browser, plan,
approval, or execution surface.

The helper first reparses the fresh input through the existing closed
create-input boundary. It recompiles the fixed candidate and requires exact
deterministic property, Web-stream, Admin-version, and `analytics.edit` scope
binding to the supplied EDIT inventory target. It never attempts to recreate
an `EventContract` or customer semantics from a retained candidate: those
source values are intentionally not retained.

The supplied EDIT inventory is rebuilt with its original JSON scalar kinds and
both local HMAC markers are checked before the proposed event-name HMAC is
compared. A matching marker means only that the complete normalized supplied
surface matches the current local fingerprint key. It does not establish a
provider response origin, reader origin, remote freshness, permission,
reservation, consent enforcement, or authorization.

Planning can continue past this helper only when the observed service level is
`GOOGLE_ANALYTICS_STANDARD`, the complete observed total is below the
documented 30-key-event cap, and the proposed HMAC name is not observed in the
complete supplied inventory. A 360 or unavailable service level returns
`GA4_KEY_EVENT_CAPACITY_NOT_EVIDENCED`; an observed Standard total at the cap
returns `GA4_KEY_EVENT_DOCUMENTED_CAP_REACHED`. Neither result deletes,
reserves, or otherwise changes a key event.

On success the closed redacted result contains only candidate and inventory
digests, the exact observed Standard count/cap/remaining slots, marker
integrity labels, and `NOT_ESTABLISHED` / `NOT_EVALUATED` currentness,
reservation, and permission facts with false side-effect and authority
controls. Its serialization boundary rechecks the complete closed result shape
so `model_copy` or `model_construct` cannot emit forged authority/currentness
claims, inconsistent capacity, or arbitrary digest text. It omits event names,
request fields, property and stream IDs, inventory members, HMAC marker values,
and the fingerprint key.

This is not a CREATE plan or an executable capability. A later slice must
still bind a fresh input to the retained conversion contract and candidate,
collect a configured EDIT inventory under the exact profile, reject drift,
compile an exact digest-bound plan, obtain valid authorization, and perform a
new pre-apply read before any durable mutation handoff. A completed list read
is sequential and non-atomic; it cannot reserve a slot or prove that the state
will still hold at a future write.

`tests/test_ga4_admin_edit_preconditions.py` uses fixture-only fixed-host HTTP
responses to construct normalized EDIT inventories. It covers marker ordering,
member/reordering tamper, missing markers, duplicate detection, target
mismatch, unknown and exhausted capacity, constructed and numeric-type bypasses,
and redacted result controls. No customer credential, external provider, or
key store is used.

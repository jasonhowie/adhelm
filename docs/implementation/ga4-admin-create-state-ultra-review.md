# GA4 Admin key-event CREATE semantic-state ultra review

This independent review covers the pure semantic-state boundary in
`src/adhelm/providers/ga4_admin_create_state.py`. It used only in-memory
`httpx.MockTransport` fixtures to build a normalized EDIT inventory. It made
no provider, credential, browser, file, ledger, authorization, plan, or
mutation call.

The builder accepts an exact typed fresh compiler input and an exact typed,
current-keyed EDIT inventory. It reparses both before deriving the transition.
The keyed revalidators rebuild projections and CREATE state from the supplied
trusted inputs instead of accepting a retained HMAC or digest at face value.
The public typed models intentionally are not generic JSON parsers: their
`StateRef` and precondition fields require installed typed values. A retained
JSON-shaped state must pass the explicit `revalidate_*` boundary, which parses
the closed wire shape and compares it with a fresh keyed reconstruction.

The review reproduced an invariant gap in the initial M23 implementation. A
normal constructor and a `model_copy()` value could serialize
`before_member_count=0` and `after_member_count=1` while its nested
preconditions stated that the complete supplied inventory contained one key
event. The transition delta was valid, but its before-count fact contradicted
the precondition fact. The repaired model validator and raw serializer now
require `before_member_count` to equal
`preconditions.complete_observed_key_event_total`; the wire revalidator rejects
the same forged record.

The review also verifies that normal construction rejects equal before/after
fingerprints and that copied or incomplete constructed models cannot serialize
an altered count, true authority/side-effect control, an equal state reference,
or an arbitrary shape. Failures use fixed errors without exposing an injected
value. Exact original JSON types reject boolean and float aliases for schema
versions, counts, revisions, and nested capacity facts.

The semantic collection deliberately omits GA4-assigned key-event IDs,
creation time, `custom`, and `deletable`. Two post-create inventories with
different arbitrary numeric resource IDs and output-only values therefore
produce the predicted `after` state when their event-name HMAC, counting rule,
and exact default value match. Their full inventory digests remain different.
Changing the counting rule changes the projection. Agent-visible results omit
event names, property and stream IDs, key-event IDs, HMAC markers, and key
material. They preserve only opaque digests/state fingerprints and fixed
non-authoritative statuses. The result does not establish a current provider
state, permission, reservation, remote effect, execution authority, or a
successful future CREATE.

`tests/test_ga4_admin_create_state_ultra_review.py` adds independent coverage
for pure operation and redaction, count/precondition binding, equal and
incomplete constructed models, copied authority controls, exact nested scalar
types, keyed reconstruction, direct-wire behavior, resource-ID-independent
post-create projection, semantic drift, and current-keyed inventory input.

Validation completed with:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_admin_create_state.py \
  tests/test_ga4_admin_create_state_ultra_review.py \
  tests/test_ga4_admin_edit_preconditions.py \
  tests/test_ga4_admin_edit_preconditions_ultra_review.py \
  tests/test_ga4_admin_edit_inventory.py \
  tests/test_ga4_admin_edit_inventory_ultra_review.py
.venv/bin/ruff check src/adhelm/providers/ga4_admin_create_state.py \
  tests/test_ga4_admin_create_state.py \
  tests/test_ga4_admin_create_state_ultra_review.py
.venv/bin/ruff format --check src/adhelm/providers/ga4_admin_create_state.py \
  tests/test_ga4_admin_create_state.py \
  tests/test_ga4_admin_create_state_ultra_review.py
.venv/bin/mypy src/adhelm/providers/ga4_admin_create_state.py
git diff --check
```

The semantic state remains a local prediction over one supplied inventory. A
later workflow must obtain a fresh configured EDIT-profile read, bind an exact
plan and authorization, re-read before an egress attempt, verify the resulting
configuration, and record an honest recovery disposition.

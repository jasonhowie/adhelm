# GA4 Admin EDIT preconditions ultra review

This review covers the pure
`evaluate_ga4_edit_key_event_create_preconditions()` gate in
`src/adhelm/providers/ga4_admin_edit_preconditions.py`. It accepts one fresh
compiler input, one exact typed supplied EDIT inventory, and transient local
fingerprint-key bytes. The review used only in-memory fixture responses to
construct normalized inventories; it made no provider, credential, file,
browser, ledger, plan, approval, or mutation call.

The gate reparses the fresh input through the closed compiler boundary and
revalidates the exact EDIT inventory through its raw JSON and keyed-content
boundary. Constructed/mutated booleans, floats, default values, identities,
capacity facts, mappings, subclasses, stale content markers, and fingerprint
key rotations fail before they can create a duplicate or capacity conclusion.
The fresh candidate and supplied target must agree on property, selected Web
stream, Admin API version, `analytics.edit` scope, and EDIT profile purpose.

Only a `GOOGLE_ANALYTICS_STANDARD` inventory with a complete observed total
below 30 can produce success. The result calculates the exact remaining slot
count, compares the proposed event only through the current-key HMAC, and
rejects an observed duplicate. A 360/unknown service level has no inferred
cap; a Standard inventory at 30 returns an explicit cap failure. None of these
facts establishes create permission, a reservation, current remote state,
authorization, or an eventual mutation outcome.

The review reproduced a constructed-result serialization gap: a public
`model_copy` or `model_construct` result could previously emit `CURRENT`,
`RESERVED`, `GRANTED`, true side-effect/authority controls, or arbitrary digest
text. The repaired result serializer now validates direct fields and the full
closed emitted shape before returning agent-visible data. Incomplete constructed
results also produce a fixed invalid-shape failure without leaking field names
or injected values. The successful result contains only fixed status labels,
counts, and digests; it omits event/request text, property and stream identity,
inventory members, HMAC markers, and key material.

`tests/test_ga4_admin_edit_preconditions_ultra_review.py` adds 17 independent
cases for pure/no-I/O operation, redaction, typed-only inventory input, nested
raw-scalar and model-copy bypasses, target/API/scope/profile mismatch, key
rotation, content/order tamper, duplicate and capacity states, and copied or
constructed result serialization attacks.

Validation completed with:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_admin_edit_preconditions.py \
  tests/test_ga4_admin_edit_preconditions_ultra_review.py \
  tests/test_ga4_admin_edit_inventory.py \
  tests/test_ga4_admin_edit_inventory_ultra_review.py
.venv/bin/ruff check src/adhelm/providers/ga4_admin_edit_preconditions.py \
  tests/test_ga4_admin_edit_preconditions.py \
  tests/test_ga4_admin_edit_preconditions_ultra_review.py
.venv/bin/ruff format --check src/adhelm/providers/ga4_admin_edit_preconditions.py \
  tests/test_ga4_admin_edit_preconditions.py \
  tests/test_ga4_admin_edit_preconditions_ultra_review.py
.venv/bin/mypy src/adhelm/providers/ga4_admin_edit_preconditions.py
git diff --check
```

The focused command passed 72 tests. The result remains a static local
precondition over a supplied inventory. A later workflow must obtain the
configured EDIT profile, collect fresh identity and collection evidence,
bind and retain an exact plan, obtain valid authorization, re-read before a
write, and verify/recover afterward.

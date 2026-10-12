# GA4 Admin preconditions ultra review

This independent review covers the supplied-inventory duplicate helper and the
new GA4 inventory content marker. It used only locally constructed Pydantic
models and fake-only tests. It made no provider, credential-store, filesystem,
ledger, browser, or network call.

## Fixed finding

- **P1 — a key-continuity marker did not authenticate inventory membership.**
  A constructed inventory could keep a valid current-key marker while replacing
  an event fingerprint with an unsalted SHA-256 value, a value made under a
  foreign key, or an arbitrary 64-hex string. The old helper could then report
  the candidate absent. A new reader-produced,
  domain-separated `inventory_content_marker` HMAC covers the normalized
  identity, all members, pagination declaration, and key marker, excluding
  only itself. Preconditions verify both markers before deriving the candidate
  event-name HMAC. Missing historical content evidence and every changed,
  reordered, removed, or foreign sealed member fail closed.

The marker is local keyed-content integrity only. A holder of the fingerprint
key can produce it; it does not prove reader origin, retention, current remote
state, completeness, capacity, permission, consent, authority, or a future
write outcome.

## Independent regressions

`tests/test_ga4_admin_preconditions_ultra_review.py` adds fake-only coverage
for marker-before-event-HMAC ordering, foreign-key markers, a duplicate event
name under a different native ID, unsalted and foreign member fingerprints,
removed/replaced members, copied key markers, absent and explicit-null content
markers, preserved legacy JSON bytes, forged parent bindings, constructed
numeric input, and forged output authority/side-effect controls.

The focused command passed **39 tests**:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_admin_preconditions.py \
  tests/test_ga4_admin_preconditions_ultra_review.py
```

Ruff check, Ruff format check, source-only mypy for
`src/adhelm/providers/ga4_admin.py` and
`src/adhelm/providers/ga4_admin_preconditions.py`, and `git diff --check` also
passed.

## Remaining boundary

This is a local duplicate precondition over one supplied, normalized
inventory. It creates no plan, durable record, provider request, credential
read, key-store access, mutation, or authorization. A guarded GA4 Admin write
still needs fresh identity/access proof, current complete inventory and drift
checks, capacity and permission evidence, consent/semantics checks, exact
authorization, recovery handling, and independent readback/runtime evidence.

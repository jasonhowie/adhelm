# GA4 Admin EDIT inventory ultra review

## Scope

This independent review covers the fixed, read-only
`Ga4AdminEditKeyEventInventoryClient`, its public keyed revalidation boundary,
and the small shared `_parse_ga4_admin_key_event` extraction used by the
existing READ inventory. It does not approve a GA4 mutation, an authorization,
or a claim that an OAuth bearer has create permission.

The reviewed request sequence is closed:

1. validate one deterministic EDIT target before token access;
2. capture one locally configured token snapshot;
3. GET the selected property and selected Web stream under that snapshot; and
4. list the target property's key events with the same bearer, fixed v1alpha
   path, empty body, `pageSize=200`, and the fixed partial-response mask.

The identity observations must match property ID, stream ID, timezone, and
currency before the list starts. The list stops on duplicate members, repeated
page tokens, malformed/extra fields, more than five pages, or more than 1,000
members. Its returned collection is a sequential, non-atomic observation;
there is no reservation, currentness assertion after return, or provider write.

Google documents the fixed list path, pagination contract, maximum page size,
and read scopes in the [Key Events list reference](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents/list).
The permitted member fields come from the [KeyEvent resource reference](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents).
The 30-slot figure is used only for observed Standard properties, as documented
in [GA4 configuration limits](https://support.google.com/analytics/answer/12229528?hl=en).
For 360 or unavailable service level, the implementation correctly reports the
complete count with no inferred cap.

## Adversarial results

The review added 21 independent cases in
`tests/test_ga4_admin_edit_inventory_ultra_review.py`. They cover:

- one bearer capture and exactly the two fixed v1beta identity GETs followed by
  the fixed v1alpha list GET;
- constructed target and access-identity bypasses before the list;
- property, stream, timezone, and currency drift;
- duplicate/cyclic paging and unknown provider response fields without
  reflecting private input;
- Standard versus 360/unknown capacity semantics;
- original JSON controls, including Boolean/float aliases, deeply nested raw
  mappings, and forged model-copy capacity or membership state;
- raw nested `default_value.numeric_value` aliases (`1`, `1.0`, and `true`)
  before model normalization or HMAC comparison;
- a raising token getter normalized to `PROVIDER_ACCESS_REQUIRED` before any
  HTTP request; and
- byte-for-byte legacy READ inventory/HMAC/digest preservation through the
  shared parser extraction.

Two review findings were repaired before freeze:

1. A constructed or rehashed inventory could previously bypass structural
   consistency through copied nested capacity/member state. The public
   revalidator now reconstructs a closed JSON-shaped value, validates strict
   scalar types and nested fields, then checks both the current-key marker and
   distinct EDIT content marker. The serializer refuses forged constructed
   state as well.
2. A raw nested decimal value and a token-getter exception could respectively
   normalize an original scalar type or disclose backend exception text. Raw
   default values now require exact keys, decimal-string serialization, and an
   uppercase currency; getter failures produce the bounded access diagnostic
   before egress.

The inventory contains locally derived references and opaque HMACs for internal
binding. It is not an agent-facing review object. The reviewed CLI projection
must continue to redact member/property/stream identifiers and omit content
MACs; this library layer does not itself establish that sharing boundary.

`required_scope: analytics.edit` is a local target requirement only. A
successful list GET can be served to a readonly bearer, so the later workflow
correctly needs `granted_scopes: NOT_EVALUATED` and
`create_permission: NOT_PREPROVABLE`. No result establishes execution
authority, remote mutation, an approved plan, a duplicate-free future write,
or a capacity reservation.

## Validation

The focused combined command completed with **177 passed**:

```text
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_admin.py \
  tests/test_ga4_admin_ultra_review.py \
  tests/test_ga4_admin_fingerprint_continuity.py \
  tests/test_ga4_admin_preconditions.py \
  tests/test_ga4_admin_preconditions_ultra_review.py \
  tests/test_ga4_admin_edit_inventory.py \
  tests/test_ga4_admin_edit_inventory_ultra_review.py \
  tests/test_ga4_admin_access_ultra_review.py \
  tests/test_ga4_admin_access_workflow.py
```

Ruff check and format check passed for the two reviewed provider modules and
the independent test; mypy passed for both modules; `git diff --check` passed.
All tests used `httpx.MockTransport` only. No provider, OAuth, browser,
credential-store, or native-host operation ran.

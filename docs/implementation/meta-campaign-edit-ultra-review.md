# Meta campaign EDIT observation review

This independent review covers the fixed Meta campaign-state reader, the
EDIT-only inspected token session, and the narrow shared READ/EDIT continuity
extraction.  It uses in-memory credential stores and `httpx.MockTransport`
only; it does not prompt, use an operating-system credential store, or contact
Meta.

The review verifies that invalid campaign, identity, composition, and
constructed-object state fails before a guarded subject callback or provider
GET.  It verifies one fixed campaign-node GET, exact native campaign/account
binding, separate configured and effective statuses, `ads_management` scope
for the EDIT facade, and that the inspector token is confined to the fixed
inspection request.  The yielded binding and campaign state expose no grant,
current-live, mutation, or execution claim.

It also verifies that configuration, bundle, and expiry drift after account
proof stops the campaign GET; that the safe local drift diagnostics are
rebuilt without callback text; and that retention holds the shared local state
while callbacks remain owner-only, repeatable in context, and free of network
or lock reentry.

The shared extraction preserved the existing READ behavior after its tests
moved only their private monkeypatch targets to
`_meta_ads_token_session`.  The transient pre-migration fixture-target failure
was not a product behavior failure.

Validation:

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_meta_campaign_edit_ultra_review.py \
  tests/test_meta_campaign_state.py \
  tests/test_meta_ads_token_edit_session.py \
  tests/test_meta_edit_session_integration.py
# 76 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_meta_ads_token_read_session.py \
  tests/test_meta_ads_token_read_session_ultra_review.py
# 23 passed
```

No remaining concrete source finding is recorded by this review.  This remains
development-only read preparation: it creates no plan, grant, ledger record,
provider mutation, current-live assertion, or execution authority.

The final root pass additionally reproduced malformed IANA-zone path inputs
escaping as `ValueError`, and a missing-slot EDIT binding escaping as
`AttributeError`. Both now return fixed typed diagnostics. The affected suite
passed 190 cases after these fixes and the root workflow clock-fixture migration;
the focused observation subset in that run contains 101 cases. No behavioral
assertion was removed to accommodate the shared extraction.

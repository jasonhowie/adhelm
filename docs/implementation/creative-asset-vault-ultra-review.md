# Creative asset vault ultra review

Reviewed: 2026-09-05
Status: **implemented / local image-object boundary only**

## Scope

This independent review covered the private local-image vault APIs
`store_image(root, data, digest)` and `read_image(root, digest)`. It used only
temporary files, local descriptor seams, and injected `os` failures. It made no
provider, credential-store, browser, native-host, or network call.

## Verified behavior

- Input digest and byte validation happen before a missing vault root can be
  created.
- The vault rejects a symlinked root without writing through its target.
- Publication writes an owned temporary file, fsyncs it, and publishes with a
  no-overwrite hard link. A simulated link failure removes the owned temporary
  file and returns a fixed error without the raw OS exception.
- A simulated competing corrupt final object is neither replaced nor accepted:
  the final reread detects the digest mismatch and reports a closed corruption
  status.
- A simulated rewrite after the descriptor has started reading changes its
  metadata before the final descriptor/path comparison, so `read_image` fails
  with `ASSET_CHANGED` rather than returning bytes.

## Limits retained

The read proves one currently selected immutable local object. It does not
promise that the object remains available after the call, establish a provider
serving surface, validate a decoded image, or prove an analyzer result. The
creative registration and ledger closure own those separate facts.

## Validation

```text
PYTHONPATH=src:tests .venv/bin/pytest -q tests/test_creative_asset_vault_ultra_review.py
# 5 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check tests/test_creative_asset_vault_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check tests/test_creative_asset_vault_ultra_review.py
```

All scoped checks passed.

# Creative projection ultra review

Reviewed: 2026-09-05
Status: **implemented / local export boundary only**

## Scope

This independent fake-only review covered the creative portion of the verified
ledger export projection. It used temporary local ledgers and export
directories; it made no provider, credential-store, browser, native-host, or
network call.

## Findings repaired

Default exports previously redacted component copy, `claim`, and `mechanism`,
but emitted `CreativeFamilyRecord.family_label` and
`CreativeMessage.audience_frame` verbatim. The legacy creative-register path
can set the family label equal to the local family identifier, so this leaked a
local ID through a field that was not pseudonymized. The default projection now
returns `null` for both fields. The same fields, along with exact/normalized
component copy, claim, and mechanism, remain available only when
`include_marketing_text` is explicitly true.

`creative-versions.csv` also had a column named `content_digest` while a
`CreativeVersionRecord` has no such field. It therefore emitted an empty cell.
The projection now emits the actual retained `serving_manifest_digest` under a
matching column name. These externally visible export changes advance
`projection_version` from 1 to 2; the canonical ledger schema is unchanged.

## Independent coverage

`tests/test_creative_projection_ultra_review.py` adds two cases that verify:

- all creative free-text fields and the legacy family-ID label stay out of the
  complete default bundle, including JSONL and scalar-node projection data;
- an explicitly opted-in marketing-text bundle retains those local review
  fields; and
- the creative-version CSV header and value use the exact retained semantic
  manifest digest; and
- the resulting export manifest declares projection version 2.

## Limits retained

The export is a deterministic redacted historical projection. It does not
prove current local-media availability, analyzer evidence, a provider serving
surface, an exact performance association, or authority to mutate a provider.
Those source-integrity and deployment invariants remain separate creative
ledger/workflow work.

## Validation

```text
PYTHONPATH=src:tests .venv/bin/pytest -q tests/test_creative_projection_ultra_review.py tests/test_projections.py
# 4 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check src/adhelm/projections.py tests/test_creative_projection_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check src/adhelm/projections.py tests/test_creative_projection_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy src/adhelm/projections.py
```

All scoped static checks passed.

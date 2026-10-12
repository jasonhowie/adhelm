# Measurement history ultra review

Reviewed: 2026-09-04

## Scope

This review covered the closed measurement snapshot models,
`retain_measurement`/catalog/load/projection helpers, the installed measurement
CLI composition, and the `MEASUREMENT_OBSERVATION` ledger binding. It used only
synthetic snapshots and mocked readers. No provider request, credential-store
read, customer data, or remote mutation occurred.

The retained record represents a completed bounded **configuration read**. It
does not prove browser firing, native receipt, processed reporting, a native
optimization association, a change actor/time, or execution authority. Each of
those fields remains explicitly `NOT_EVALUATED` or `false`.

## Findings fixed

| Severity | Finding | Resolution |
| --- | --- | --- |
| P1 | The OpenAI conversion-settings branch acquired the local export-sharing key even though its read receipt and normalization never use it. A missing or newly created unrelated key could block a valid read. | Key acquisition now occurs only in the GA4 branch, where the Admin reader needs it for local HMAC fingerprints. |
| P2 | GA4 profile/stream validation happened after the sharing-key read. Invalid public input could reach the credential-store boundary. | The CLI calls the pure `validate_ga4_admin_read` preflight before obtaining the GA4 fingerprint key. |
| P1 | Every `AdhelmError` from retention was rewritten as an uncertain durable-write result, including deterministic pre-append validation and path failures. That obscured a known failure as possible persistence. | Only `OSError` from the write/fsync boundary becomes `MEASUREMENT_RETENTION_UNVERIFIED`; known ledger errors retain their actual diagnostic. |
| P1 | `Ledger._append_bytes` treated a short successful `write()` as a complete append, then fsynced a truncated JSONL line. | The append loop now requires forward progress until every byte is written before fsync; failure after any partial write remains an uncertain local-write error and the torn tail is detected on the next scan. |
| P2 | `load_measurement` passed non-string digests into `re.fullmatch`, leaking a Python `TypeError` instead of failing closed. `measurement_rows` also trusted a caller-supplied model instance without reparsing it. | Digest selection now type-checks before any checkpoint; row projection reparses the closed observation and rejects forged `model_copy` state. |

## Verified behavior

- Ledger validation binds every retained observation's payload provider and
  canonical account to its envelope, requires `UNKNOWN` source, no run or
  occurred-at claim, and makes `observed_at` exactly equal the normalized read
  finish time.
- Selection is from one verified cross-stream checkpoint. Catalog and show
  paths do not reread providers or silently return an incomplete remote read.
- The CLI retains only after the complete fixed reader returns. A malformed
  later OpenAI page produces no measurement observation.
- `show` passes identity and rows through the existing keyed redaction path;
  native account/property, stream, resource, and setting references are opaque
  in agent-visible output. Snapshot and ledger digests remain provenance
  values, not native identifiers.
- Catalog/show enforce configured row and byte bounds. Existing 1 KiB coverage
  verifies the receipt and catalog. Separate paged GA4 review coverage retains
  the next cursor and exact default-value decimal string under its configured
  review budget; an indivisible review can require more than 1 KiB.

## Remaining evidence limits

The records are local, append-only evidence of fixture-tested configuration
reads. The OpenAI list response envelope is still live-unverified, and GA4
Admin inventory is not evidence of runtime firing, processed data, or a
cross-provider conversion. Local JSONL integrity cannot prove facts that a
provider has not exposed or that no local operator can independently alter a
fresh data directory; it makes retained history tamper-evident and validates
the declared schema/provenance once it exists.

## Validation

```text
.venv/bin/pytest -q \
  tests/test_measurement_history_ultra_review.py \
  tests/test_saved_measurements.py \
  tests/test_measurement_cli_integration.py \
  tests/test_ga4_measurement_workflow.py \
  tests/test_openai_measurement_workflow.py \
  tests/test_ledger.py \
  tests/test_composite_ledger.py
# 56 passed

.venv/bin/ruff format --check src/adhelm/measurement_cli.py \
  src/adhelm/saved_measurements.py src/adhelm/ledger.py \
  tests/test_measurement_history_ultra_review.py
.venv/bin/ruff check src/adhelm/measurement_cli.py \
  src/adhelm/saved_measurements.py src/adhelm/ledger.py \
  tests/test_measurement_history_ultra_review.py
.venv/bin/mypy src/adhelm/measurement_cli.py src/adhelm/saved_measurements.py \
  src/adhelm/ledger.py tests/test_measurement_history_ultra_review.py
# clean
```

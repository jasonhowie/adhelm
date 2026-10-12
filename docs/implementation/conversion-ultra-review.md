# Conversion-definition ultra review

Reviewed: 2026-09-04

## Scope

This independent offline review covered the new customer conversion-definition
boundary and its local-history integration:

- `src/adhelm/conversion_contracts.py`
- `src/adhelm/saved_conversion_contracts.py`
- `src/adhelm/conversion_cli.py`
- conversion-specific changes in `src/adhelm/ledger.py` and
  `src/adhelm/projections.py`
- `tests/test_conversion_contracts.py`, `tests/test_conversion_history.py`, and
  the independent regressions in `tests/test_conversion_ultra_review.py`

No provider, CRM, website, browser, credential, credential-store, or network
operation was used. The reviewed capability remains a local declaration and
history boundary, not a conversion setup or verification workflow.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | The ledger reader limited an entire segment to `MAX_RECORD_BYTES + 1`, although configured segments can validly exceed one record. A valid conversion-adjacent segment over 1 MiB was therefore treated as a torn tail. Its read validation also omitted the physical terminating LF counted by the writer. | The reader now uses the configured segment size plus bounded header/record allowance, revalidates a mutable setting before a read allocation, counts the LF exactly as the writer does, and rejects over-limit segments with `LEDGER_SEGMENT_TOO_LARGE`. The regression covers a large multi-record segment, rotation, verification, checkpointing, export, the smallest segment, an exact line-limit case, mutable invalid settings, and an over-limit file. |
| P1 | A conversion parameter such as `diagnosis_type` with `cancer` or `diabetes` enum values passed validation, allowing a sensitive event schema to be declared. | Closed lexical checks now reject health, financial, protected-trait, identifier, URL, DOM, form, and CRM semantics in parameter names and enum values, including hyphen-separated enum terms. |
| P1 | A `CustomerDeclaredStatement` accepted a serialized JSON object or array, allowing raw event payload syntax to be stored inside prose. | Statements now reject JSON object/array payloads while preserving bounded printable customer wording. |

## Verified boundary

- Definitions have closed, strict schemas; unknown structured fields, arbitrary
  JSON schema, dynamic code, credentials, URL/click/DOM/form parameter
  semantics, and person-level deduplication keys do not validate. Bounded
  customer prose remains separately reviewable after screening.
- The canonical digest covers the fully revalidated definition. Rebuilding at
  compilation prevents `model_copy()` or `model_construct()` from bypassing a
  nested validator, and a forged wrapper digest fails validation.
- A retained definition has local-only `CUSTOMER_DECLARED` provenance and false
  execution authority. It cannot carry provider, account, run, or
  provider-observation provenance.
- The append lock validates each contract's version, predecessor digest, and
  strictly later effective time before a record is written. A verified read
  rechecks the same chain. Existing regressions cover competing next-version
  registrations and a rehashed conflicting predecessor.
- Registration returns `USER_DEFINITION_REQUIRED` for the required outcome,
  source, qualification, and counting declaration when absent. The tested
  retention `OSError` path becomes `CONVERSION_RETENTION_UNVERIFIED`, with no
  success receipt.
- Registration does not require an export key. Catalog, show, and deterministic
  export pseudonymize `*_ref`, `*_id`, and `destination_profiles` with the
  local sharing key. Declaration prose remains intentionally visible after
  validation so the customer meaning can be reviewed; every show result labels
  it as a redacted projection with `semantic_accuracy: NOT_VERIFIED` and no
  execution authority.
- Review pages preserve mandatory provenance and no-authority facts. They page
  whole top-level definition fields, and fail explicitly when a required field
  cannot fit the configured output bound.

## Limits retained deliberately

- A declaration is not source evidence, consent proof, browser proof, native
  receipt, processed reporting, a mapping, a provider configuration, or an
  authorization grant. No provider profile is discovered or contacted.
- Lexical and shape screening cannot prove that arbitrary customer prose is
  semantically nonsensitive. It blocks known sensitive parameter terms and raw
  serialized payloads, but it is not a legal, clinical, or source-semantics
  classifier. A future event mapping must still fail closed until its reviewed
  parameter, consent, egress, and destination proof exists.
- Ledger chains are append-only and tamper-evident within retained history. As
  documented by the ledger contract, they do not claim WORM storage or detect
  an administrator who replaces every retained record and recomputes every
  digest chain.
- These checks are hermetic. They do not prove OS credential-store behavior,
  real provider/CRM behavior, customer source semantics, consent validity,
  native conversion configuration, browser firing, receipt, processed
  reporting, authorization, or live release evidence.

## Validation

```text
.venv/bin/pytest -q tests/test_conversion_ultra_review.py \
  tests/test_conversion_contracts.py tests/test_conversion_history.py tests/test_ledger.py
# 56 passed

.venv/bin/ruff format --check src/adhelm/conversion_contracts.py src/adhelm/ledger.py \
  tests/test_conversion_ultra_review.py
.venv/bin/ruff check src/adhelm/conversion_contracts.py src/adhelm/ledger.py \
  tests/test_conversion_ultra_review.py
.venv/bin/mypy src/adhelm/conversion_contracts.py src/adhelm/saved_conversion_contracts.py \
  src/adhelm/conversion_cli.py src/adhelm/ledger.py tests/test_conversion_ultra_review.py
# clean
```

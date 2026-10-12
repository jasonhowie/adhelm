# Strict JSON input ultra review

Reviewed: 2026-09-05
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope

This review covered the shared strict-JSON boundary and its current command,
workflow, audit, and ledger callers: `strict_json.py`, `cli.py`,
`artifact_cli.py`, `workflows.py`, `audit.py`, `conversion_cli.py`, and
`ledger.py`. It used synthetic JSON, fixture models, temporary local
directories, and mocked key, credential, and HTTP entry points. It made no
provider request, read no customer credential, retained no rejected payload,
and performed no provider mutation.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | Canonicalizing parsed input before strict Pydantic validation rewrote JSON `1.0` and `-0.0` to integer lexemes. A strict integer field could therefore accept an asserted float before local command or workflow preconditions ran. The ledger used the same ordering for public payload mappings. | `validate_json_input` now screens the original bounded JSON for duplicate keys, non-finite values, and canonical-domain representability, then validates the original bytes. `validate_json_value` retains the representability check but validates `json.dumps()` of the original mapping, so Python `1.0` and booleans keep their original types. Canonical persistence still happens only after successful validation. |
| P1 | Syntactically valid exponent input such as `1e9999` overflowed the screening parser to `inf`; a future permissive `float` or `Any` field could retain that non-finite value. Replacing the old canonicalization also temporarily removed its safe-integer domain check. | The screened parse tree now rejects non-finite floats before model validation and is passed to `canonical_bytes` only to retain the established string-key and RFC 8785 safe-integer domain. Its bytes are discarded, so lexical strictness remains intact. |

## Verified behavior

- Raw `1.0`, `-0.0`, booleans, duplicate keys, literal non-finite constants,
  overflowed exponents, and integers beyond the canonical safe range fail
  closed. Failures contain no supplied JSON fragment.
- Invalid audit input is normalized to `INVALID_AUDIT_INPUT`; invalid
  conversion input stops before compilation, credential access, or history.
  An overflowed report bound stops before key export, credential lookup, HTTP,
  or ledger access.
- Public changes, performance, and creative ledger append boundaries reject
  bool/float substitutions in typed integer fields and leave no application
  record in their stream.
- Mapping revalidation preserves supported JSON conversions: a conversion
  definition retains its ISO timestamp and tuple fields, and the discriminated
  candidate record reconstructs JSON arrays as strict tuples. Existing
  normalized record/digest behavior was exercised across audit, artifact,
  workflow, conversion, measurement, reporting, creative, candidate, and
  ledger tests.

## Retained limits

This is an input-validation and local-history boundary. It does not validate a
provider response, grant authority, establish identity, make a network request,
or establish a live provider capability. Existing ledger records are not
migrated or rewritten. This review does not establish v1 or release
completion.

## Validation

```text
.venv/bin/pytest -q tests/test_strict_json_inputs.py tests/test_strict_json_inputs_ultra_review.py
# 27 passed in 0.27s

.venv/bin/pytest -q tests/test_strict_json_inputs.py tests/test_strict_json_inputs_ultra_review.py tests/test_audit.py tests/test_ledger.py tests/test_composite_ledger.py tests/test_reporting.py tests/test_creative.py tests/test_conversion_contracts.py tests/test_conversion_history.py tests/test_artifact_cli.py tests/test_cli_workflows.py tests/test_saved_measurements.py tests/test_saved_candidates.py
# 124 passed in 2.62s

.venv/bin/pytest -q tests/test_artifact_workflows.py tests/test_artifact_detail.py tests/test_artifact_detail_integration.py tests/test_report_cli_providers.py
# 17 passed in 0.34s

.venv/bin/ruff check src/adhelm/strict_json.py src/adhelm/cli.py src/adhelm/artifact_cli.py src/adhelm/workflows.py src/adhelm/audit.py src/adhelm/conversion_cli.py src/adhelm/ledger.py tests/test_strict_json_inputs_ultra_review.py
.venv/bin/ruff format --check src/adhelm/strict_json.py src/adhelm/cli.py src/adhelm/artifact_cli.py src/adhelm/workflows.py src/adhelm/audit.py src/adhelm/conversion_cli.py src/adhelm/ledger.py tests/test_strict_json_inputs_ultra_review.py
.venv/bin/mypy src/adhelm/strict_json.py src/adhelm/cli.py src/adhelm/artifact_cli.py src/adhelm/workflows.py src/adhelm/audit.py src/adhelm/conversion_cli.py src/adhelm/ledger.py
# Success: no issues found in 7 source files

git diff --check
```

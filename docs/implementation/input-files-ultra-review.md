# Input-file ultra review

Reviewed: 2026-09-05
Status: **IMPLEMENTING / local-file boundary only**

## Scope

This review covered `input_files.py`, the migrated JSON and local-image CLI
boundaries, report workflow input handling, and the inherited
`StrictModel.schema_version` guard. It used temporary files, FIFOs, a bounded
local subprocess, and fake local dependencies only. It made no provider,
credential-store, browser, Keychain, or network request.

## Findings fixed during review

| Severity | Finding | Resolution |
| --- | --- | --- |
| P1 | A selected regular file removed after descriptor read reached the final `lstat()` as `FileNotFoundError`, which the outer handler reported as a new invalid input rather than an observed replacement. | Final-path `FileNotFoundError` and `NotADirectoryError` now produce `INPUT_FILE_CHANGED`; an initially absent selection still produces `INPUT_FILE_INVALID`. |
| P1 | `Literal[1]` accepts equality aliases such as `1.0` and `true` in Pydantic despite strict model configuration. Nested output preferences could therefore accept an original float schema version. | The inherited `StrictModel` before-validator requires an exact `int` for every declared `schema_version`; per-model constraints remain in force. |
| P1 | The installed-smoke wrapper compared `SystemExit.code` with equality only, so `SystemExit(2.0)` was accepted for the required exit `2`. | The wrapper now requires `type(code) is int` before exact equality; float and boolean aliases fail validation. |

## Verified behavior

- The final component must be a regular file. Symlinks, FIFOs, directories,
  devices, and unsupported no-follow/nonblocking primitives fail closed before
  caller input reaches schema validation or downstream work.
- A real installed `measurement validate-event` FIFO invocation returns within
  the two-second subprocess bound. The checked image boundary uses
  `read_regular_input_file(..., maximum_bytes=MAX_IMAGE_BYTES)` and rejects a
  FIFO before decoding or persistence.
- Descriptor identity and metadata are checked before and after the bounded
  read. Regular-file replacement, deletion, path conversion to a special file,
  rewrite, and growth cannot return a prefix to the caller. Read failures close
  the descriptor and use fixed diagnostics without a path, input bytes, or OS
  exception text.
- The reader takes one extra byte beyond the selected limit to detect growth;
  the JSON limit remains 1 MiB and binary image reads use their fixed image
  bound.
- JSON bytes pass unchanged to the existing strict JSON boundary after the
  safe descriptor read. Numeric and boolean aliases remain their original JSON
  types, duplicate keys remain rejected, and canonicalized bytes never replace
  the original input before schema validation.
- The report workflow rejects a FIFO before it constructs a ledger, exports a
  sharing key, resolves a credential, or opens an HTTP client. Migrated leaf
  commands reject unsafe files before configuration, key, retention, or
  workflow work.
- The wheel smoke runs from the installed target and keeps its socket,
  DNS, credential-backend, and credential-lock isolation guards active for
  both normal and negative calls. Its FIFO smoke accepts only an exact integer
  exit status of `2`, a fixed closed diagnostic, and a redacted result envelope.
  The full archived suite has a 600-second deadline based on the prior
  286-second M26 run; every other validator check retains the 300-second bound.

## Limits retained

The helper detects changes it observes while reading one descriptor; it is not
a filesystem lock and cannot guarantee a future unchanged path after its final
check. Parent-directory aliases remain caller-selected inputs, and network
filesystem latency is not bounded. The boundary does not validate document
semantics, establish provider identity, grant authority, or make a retained
record valid. Private Google OAuth client-file handling retains its dedicated
credential-file boundary.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q tests/test_input_files.py tests/test_input_files_ultra_review.py tests/test_command_input_files.py tests/test_compiler_input_files.py
# 90 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q tests/test_core.py tests/test_cli_workflows.py tests/test_report_cli_providers.py tests/test_artifact_cli.py tests/test_conversion_contracts.py tests/test_conversion_history.py tests/test_ga4_create_cli.py tests/test_ga4_create_cli_ultra_review.py tests/test_ga4_key_event_candidate_cli.py tests/test_pause_cli_workflow.py tests/test_pause_cli_workflow_ultra_review.py tests/test_strategy_cli.py tests/test_strategy_cli_ultra_review.py
# 147 passed

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q tests/test_validate_development.py tests/test_input_files_ultra_review.py tests/test_input_files.py tests/test_root_input_files.py
# 45 passed
```

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check src/adhelm/input_files.py src/adhelm/models.py src/adhelm/cli.py src/adhelm/workflows.py src/adhelm/artifact_cli.py src/adhelm/ga4_create_cli.py src/adhelm/plan_cli.py src/adhelm/conversion_cli.py src/adhelm/ga4_key_event_candidate_cli.py src/adhelm/compile_cli.py src/adhelm/additional_compile_cli.py src/adhelm/strategy_cli.py tests/test_input_files.py tests/test_input_files_ultra_review.py tests/test_command_input_files.py tests/test_compiler_input_files.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check src/adhelm/input_files.py src/adhelm/models.py src/adhelm/cli.py src/adhelm/workflows.py src/adhelm/artifact_cli.py src/adhelm/ga4_create_cli.py src/adhelm/plan_cli.py src/adhelm/conversion_cli.py src/adhelm/ga4_key_event_candidate_cli.py src/adhelm/compile_cli.py src/adhelm/additional_compile_cli.py src/adhelm/strategy_cli.py tests/test_input_files.py tests/test_input_files_ultra_review.py tests/test_command_input_files.py tests/test_compiler_input_files.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy src/adhelm/input_files.py src/adhelm/models.py src/adhelm/cli.py src/adhelm/workflows.py src/adhelm/artifact_cli.py src/adhelm/ga4_create_cli.py src/adhelm/plan_cli.py src/adhelm/conversion_cli.py src/adhelm/ga4_key_event_candidate_cli.py src/adhelm/compile_cli.py src/adhelm/additional_compile_cli.py src/adhelm/strategy_cli.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check scripts/validate_development.py tests/test_validate_development.py tests/test_input_files_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check scripts/validate_development.py tests/test_validate_development.py tests/test_input_files_ultra_review.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy scripts/validate_development.py
git diff --check
```

All static checks passed.

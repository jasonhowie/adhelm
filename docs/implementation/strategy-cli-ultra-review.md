# Strategy CLI and schema-view ultra review

Reviewed 2026-09-05. This hermetic review covered the strategy preview,
progressive installed schema views, their fixed CLI registration, and the
development-wheel smoke. It used synthetic fixtures only and made no provider
request, credential-store access, history write, retention action, or execution
attempt.

## Findings fixed

| Finding | Disposition |
| --- | --- |
| RFC 8785 canonicalization happened before strict strategy input validation. A JSON money value such as `2500.0` could become `2500`, allowing a float to bypass the closed integer boundary. | The command now performs bounded duplicate/non-finite JSON screening, then validates the original bytes. The same confirmed boundary defect was fixed in all six provider preview commands. Float and boolean money regressions cover every command, including all three LinkedIn money fields. |
| The installed-wheel smoke compared only the `Money` fragment with the full strategy schema, while source tests reassembled every named definition. | One installed Python subprocess now rebuilds the complete schema from its root, catalog, and every fragment, checking the common digest and exact structural equality. The existing installed CLI calls still prove command wiring without launching one process per definition. |

## Reviewed behavior

`compile strategy` validates at most 1 MiB of JSON before compilation, rejects
expired briefs with a structured diagnostic, and builds its agent-visible result
only after every selected native compiler succeeds. A late sixth-child rejection
therefore returns an empty failed-closed envelope rather than a partial list of
earlier candidates.

The successful projection contains only ordered digests, typed currency caps and
maximum spends, each child’s approved redacted review and planning boundary, and
the OpenAI activation boundary and mapping statuses. HMAC redaction covers
account references and local handles before output. The projection omits the
source brief, customer intent text, assumptions, provenance, native request
graphs, creative copy, destinations, and provider account/resource identifiers.
Its exact newline-delimited output bound is checked before the sharing key is
accessed.

The installed schema registry exposes a fixed `strategy-brief` model rather than
a caller-selected runtime class. Full, root, catalog, and named-definition views
share one digest; their fragments reassemble exactly without reading customer
configuration. Conflicting view options and unknown definitions fail closed
without echoing the supplied definition value.

## Validation

```text
.venv/bin/pytest -q tests/test_strategy.py tests/test_strategy_cli.py \
  tests/test_schema_views.py tests/test_strategy_cli_ultra_review.py
# 28 passed

.venv/bin/pytest -q tests/test_compile_cli.py tests/test_additional_compile_cli.py \
  tests/test_compiler_money_contract.py tests/test_compiler_money_and_previews_ultra_review.py
# 63 passed

.venv/bin/ruff check src/adhelm/strategy_cli.py src/adhelm/compile_cli.py \
  src/adhelm/additional_compile_cli.py src/adhelm/schema_views.py \
  scripts/validate_development.py tests/test_strategy_cli_ultra_review.py \
  tests/test_compiler_money_and_previews_ultra_review.py
.venv/bin/ruff format --check [same scoped paths]
.venv/bin/mypy src/adhelm/strategy_cli.py src/adhelm/compile_cli.py \
  src/adhelm/additional_compile_cli.py src/adhelm/schema_views.py
# clean

.venv/bin/python -m py_compile scripts/validate_development.py
# clean
```

The new installed-wheel reassembly assertion has source-level and syntax
coverage here. Exact-archive wheel validation remains the root release
validation step after the shared workspace is frozen. The reviewed surfaces are
still offline previews: provider identity, eligibility, drift, authorization,
native conversion, and execution/readback proof remain explicit future gates.

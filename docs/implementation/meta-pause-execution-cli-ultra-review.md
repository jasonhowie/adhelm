# Meta campaign PAUSE execution CLI independent review

Status: `REVIEWED`.

This review covers the explicit `execute meta-campaign-pause --input REQUEST`
leaf, its static schema and discovery registration, and the shared execution
result boundary. The independent cases use temporary directories, Typer's
in-process runner, fake workflow results, and fake readiness functions. They
do not import a configured artifact, issue a binding, launch a helper, use a
credential store, start XPC, or contact a provider.

## Boundaries verified

The request schema is closed: it has only optional integer schema version `1`
and the required lowercase 64-hex retained-plan-record digest. It exposes no
receipt, candidate, grant, key, credential, transport, adapter, artifact, or
generic execution selector. Schema and discovery inspection remain static and
do not resolve either installed binding. Discovery calls the command
development-only, requires separate Meta V2 setup, and makes no live-provider
or automatic-execution claim.

The command rejects a final-component symlink or FIFO as
`INPUT_FILE_INVALID` before configuration, the V1 gate, the Meta V2 gate, or
the workflow. For valid input, the intentionally local ordering is preserved:
configuration/Operator/output-envelope checks precede V2 readiness. The Meta
leaf never calls the V1 readiness function; a default ordinary source returns
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED` before a ledger
or workflow is reached.

The closed output converter rejects Meta resource references and more than
three evidence references as `EXECUTION_RESULT_INVALID` with ambiguous exit
6. It excludes authorization IDs, adapter recovery text, diagnostics, opaque
resource references, and excess evidence from the emitted diagnostic.

The new shared unexpected-workflow boundary maps a raw `RuntimeError` from the
existing OpenAI leaf to `EXECUTION_RESULT_UNAVAILABLE` with ambiguous exit 6
without exposing its text. It continues to preserve an established fixed
`AdhelmError` code and exit classification; it does not replace it with the
generic unavailable result. This keeps the existing OpenAI/GA4 error contract
while preventing accidental raw text from an unexpected workflow exception.

Owner cases separately cover the full Meta status/exit table, bounded success
projection, exact CLI-to-private-workflow composition through a module-issued
synthetic V2 binding and fake pipe, and success/unknown/lost acknowledgement
outcomes. Those composition cases keep unknown/lost results ambiguous and
block replay through recovery history.

No source finding remains in this bounded review.

## Limits

The CLI command is an explicit local entry point only. It neither authorizes
execution from a plan, artifact, digest, review, or agent prose nor proves an
installed native route, helper availability, native-host origin, configured
wheel, live Graph conformance, or provider-side idempotency. The validation
paths covered here use unconfigured defaults and synthetic boundaries only.

## Validation

The independent file contains eight cases and passed its focused run:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m53-uv-cache uv run pytest -q \
  tests/test_meta_pause_execution_cli_ultra_review.py
```

The combined execution CLI and Meta workflow review group passed 89 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m53-uv-cache uv run pytest -q \
  tests/test_installed_approval_execution_cli.py \
  tests/test_execution_cli_ultra_review.py \
  tests/test_meta_pause_execution_cli.py \
  tests/test_meta_pause_execution_cli_ultra_review.py \
  tests/test_meta_pause_execution_workflow.py \
  tests/test_meta_pause_execution_integration.py \
  tests/test_meta_pause_execution_workflow_ultra_review.py
```

Ruff check and format check passed for the independent test file.

The fake installed-validator harness also passed 25 tests. It checks the
Meta command's FIFO rejection and default V2 refusal only after both reviewed
unconfigured source and wheel-member hash guards; it does not execute a
configured wheel.

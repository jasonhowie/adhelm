# Installed-approval execution CLI ultra review

## Scope

This independent review covers the two new root CLI leaves,
`execute openai-pause --input` and `execute ga4-key-event-create --input`,
their static discovery/schema registration, their bounded result projection,
and the plan/artifact availability status they advertise. All cases use
`Typer`'s in-process runner, temporary files, and fake workflow results. They
do not configure a native binding, start a helper, access a credential store,
perform OS authentication, or make a provider request.

## Findings and repairs

The initial result projection serialised a valid strict `ExecutionResult` to
JSON and then called `ExecutionResult.model_validate(...)`. JSON serialisation
turns the required `results` tuple into a list, so the strict model rejected
every otherwise valid completed or recovery result as
`EXECUTION_RESULT_INVALID`. The repair validates the JSON wire through
`model_validate_json(canonical_bytes(...))` before rebuilding the closed output
surface. The independent success and recovery cases exercise that repaired
boundary.

The availability literal was shortened to `DEVELOPMENT_ONLY_SETUP_REQUIRED`
after the longer form made a compact GA4 plan review exceed the supported
1024-byte minimum. Discovery separately states that native setup is required;
all plan and artifact views remain explicitly non-authorizing and
non-mutating.

The first CLI revision also used exit code 5 for every recovery-required
result. That mislabeled `AMBIGUOUS_REMOTE_STATE` as partial and hid the
distinction between a proven no-effect result and a partial provider effect.
The fixed mapping emits the same safe result first, then maps
`PARTIAL_EFFECT` to 5, `AMBIGUOUS_REMOTE_STATE` to 6, and
`FAILED_NO_EFFECT` or `NOT_STARTED` to 3. Consumption and recovery facts stay
present for every non-success result.

## Boundaries checked

- Symlinks, FIFOs, oversized files, duplicate-key JSON, and raw
  `schema_version: 1.0` are rejected before configuration, bridge readiness,
  or either workflow.
- The leaves expose no receipt, challenge, pin, key, grant, candidate, adapter,
  transport, or credential option.
- The ordinary generated binding refuses with
  `NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` before the workflow can reach a
  helper, credential, or provider.
- `discover` and both installed execution schemas remain static and never
  resolve native readiness.
- The complete worst-case result fits a saved 1024-byte output cap before
  readiness or workflow work. Completed and ambiguous results project only the
  plan digest, consumed/recovery controls, one typed effect status, bounded
  opaque/evidence references, and a fixed next action.
- Authorization IDs, provider diagnostic text, recovery prose, receipt text,
  and transport facts are absent. An unpresentable provider result fails closed
  without exposing those values.
- Process exits distinguish partial effect, ambiguity, proven no effect, and
  no start after the bounded result has been emitted.
- Plan/catalog/artifact availability now says setup is required while retaining
  `execution_authority: false` and `remote_mutations: false`.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_installed_approval_execution_cli.py \
  tests/test_execution_cli_ultra_review.py
```

This focused command passed 41 fake-only cases: 12 owner cases and 29
independent cases. Ruff check/format and `git diff --check` also passed for the
independent file. The broader archive validation remains responsible for the
integrated plan and artifact suites.

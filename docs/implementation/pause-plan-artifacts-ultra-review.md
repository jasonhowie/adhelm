# Retained pause-plan artifact ultra review

This independent review exercised the closed OpenAI pause-plan artifact path:
`artifacts plan`, saved-rule generation during `plan openai-pause`, the typed
renderer, and the shared local writer. It used fixture pause actions, temporary
directories, and in-memory export keys only. No provider, credential store,
approval host, XPC service, or remote operation was used.

## Findings resolved before review freeze

The public artifact-task override accepted truthy non-booleans for replacement
authorization. The resolver now requires an exact boolean before it can reach
the writer.

A forged `ArtifactDecision` could previously carry an unsafe digest/path,
unbounded limit, or private reason through a public result path. The common
writer and pause-plan result serializer now normalize the complete decision
shape, allow only resolver-owned outcome/reason combinations, and reject unsafe
paths before output or filesystem access.

The public pause-plan result serializer also accepted completed metadata whose
reported input/settings digests did not match the versioned writer filename.
It now requires the exact unsuffixed or `.v2`–`.v9999` filename derived from
those digest prefixes, its selected format, and the result's source/checkpoint
bindings. This is structural result validation; it is not a claim that a file
still exists or has not later changed.

The partial-result preflight accepted arbitrary source/checkpoint strings and
could place them in agent-visible preview metadata. It now accepts only
lowercase SHA-256 references and otherwise returns a generic closed source
diagnostic.

## Review coverage

The independent tests cover exact boolean authorization, forged policy and
metadata rejection, source-reference sanitization, full worst-case result
envelope plus trailing-LF sizing before export-key access, partial publication
with every completed artifact reference retained, and JSON/CSV/HTML redaction.
The rendered pause artifact is a static historical review: it uses retained
`recorded_at` as `as_of`, labels current validity as not evaluated, pseudonymizes
account/campaign/profile bindings, and carries no execution command or remote
mutation authority.

Saved automatic generation is confined to the `plan` / `before_approval` /
`plan_review` rule context. Default `ON_REQUEST` remains file-free. A failed
optional artifact preserves the mandatory retained plan and review; bounded
preflight reserves the partial outcome and all possible completed references
before a key is read or a file is created.

## Validation

- `tests/test_pause_plan_artifacts_ultra_review.py`: 17 passed.
- Artifact, pause-plan, retention, and review focused group: 322 passed.
- Scoped Ruff check and mypy passed. Source formatting was rerun after the
  final builder normalization change.

This evidence does not establish fresh provider state, execution authority,
durable replay consumption, live filesystem behavior after publication, or an
installed approval host. Those require their own guarded execution and
verification paths.

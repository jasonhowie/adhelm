# Measurement-comparison artifact ultra review

This independent review covers the M17 retained measurement-comparison artifact
renderer and its explicit CLI path. It examined
`measurement_comparison_artifacts.py`, the new
`artifacts measurement-comparison` command, and the saved-rule integration in
`measurement compare`. The review used only temporary local ledgers and fixture
snapshots; it made no provider, credential-store, native-host, or remote calls.

## Finding repaired during review

The first renderer revision reconstructed the supplied comparison before
artifact preflight, then entered the shared writer without another source
check. An append between those steps could publish a document whose checkpoint
was no longer the current verified local checkpoint. The reproduction injected
two fixture observations immediately after the original preflight function and
observed a successful file publication with a different checkpoint digest.

The renderer now rebuilds the original pair at the shared writer's document
handoff, after preflight and safe directory setup. A changed checkpoint returns
the public `MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID` failure, with zero
artifact references and no file. The output remains a historical review, not a
claim that the remote state is current. This check is not an atomic lock against
a ledger append that occurs after the handoff; the implementation and artifact
both state that limitation rather than inventing future-currentness.

## Independent coverage

`tests/test_measurement_comparison_artifacts_ultra_review.py` covers:

- post-preflight checkpoint drift at the final writer handoff;
- GA4, OpenAI, and GTM document scope, pair provenance, continuity caveats,
  no-authority fields, HMAC references, and raw identifier redaction across
  JSON, CSV, and static HTML;
- no script or link output in HTML;
- versioned publication when an existing artifact has different bytes and
  replacement was not authorized;
- result serialization that rejects a forged filename/digest binding and
  normalizes private failure text;
- strict JSON booleans for replacement authorization; and
- raw float/integer bypass attempts against document schema and fixed
  no-authority controls.

The existing focused suites additionally cover explicit-command bounds before
sharing-key access, artifact receipt sizing, source drift across key access,
reuse, partial writes, and saved-rule selection.

## Validation

The review ran:

```text
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_measurement_comparison.py \
  tests/test_measurement_comparison_cli.py \
  tests/test_measurement_comparison_artifacts.py \
  tests/test_measurement_comparison_artifact_cli.py \
  tests/test_measurement_comparison_artifact_rules_ultra_review.py \
  tests/test_measurement_comparison_artifacts_ultra_review.py
```

The combined focused group passed. Ruff check/format and mypy for the affected
source modules also passed. The review does not establish live provider
behavior, current remote configuration, artifact-opening authority, or v1
completion.

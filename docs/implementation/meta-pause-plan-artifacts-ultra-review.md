# Meta PAUSE plan artifacts independent review

Status: `REVIEWED`.

This review covered the M43 static-history renderer and its explicit
historical CLI route. It used temporary ledgers, synthetic M40 actions, fake
sharing-key seams, local regular directories, symlinks, and FIFOs. It did not
contact Meta, open an operating-system credential store, launch the native
host, request approval, or execute a campaign change.

## Boundaries reviewed

`MetaCampaignPausePlanArtifactRequest` is a closed JSON request. `formats` is
required, nonempty, unique, and limited to the installed three formats. Missing
formats, empty formats, duplicates, boolean aliases, and extra `action` input
all fail at the regular-file/schema boundary before configuration, retained
history, export-key lookup, or output handling. The installed schema now
advertises that same rule with `formats` required and `minItems: 1`.

The historical command loads the selected record through the CLI-owned ledger
path, reconstructs the exact M40 action and provenance, and takes a canonical
snapshot. It runs a complete zero-key document/output preflight before the
real export key or descriptor-relative writer. It reloads the selected source
and requires its action, provenance, and checkpoint snapshot to remain exact
before the key/writer handoff. An independent test appends a distinct local
plan between those two loads; the checkpoint change returns
`META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID`, leaves the destination empty, and
does not read the export key.

The renderer itself deliberately has no ledger dependency. It accepts only the
held action and loader provenance after strict structural/action/plan
revalidation. The renderer therefore does not independently prove a supplied
record or checkpoint digest; the CLI's selected-record reload and final
snapshot comparison provide that history boundary. Coherent replacement of
all internally held facts by arbitrary same-process code remains outside the
documented threat model.

Rendered JSON, CSV, and HTML contain the redacted M40 review and historical
controls only. Independent checks confirmed that credential binding, principal,
configuration/profile digests, scopes, canonical challenge/receipt fields,
active public-key material, observation request IDs, and native evidence names
do not appear. The output continues to say that it has no execution authority,
no remote mutation capability, no automatic retry, and that current kernel
checks are required before any future effect.

Explicit historical output refuses symlink and FIFO destinations before export
key access. The shared descriptor-safe writer remains the only publication
path. A partial write retains only verified completed artifact metadata beside
the mandatory retained plan receipt; it does not retry planning or publication.
Static discovery and both installed schemas remain runtime-free and expose no
action, provenance, credential, challenge, receipt, or native-input field.

## Findings repaired during review

- A constructed `MetaCampaignPausePlanArtifactResult` or nested
  `ArtifactMetadata` could raise a raw missing-slot exception during
  serialization. The result validator now protects every field read and
  normalizes these inputs to `META_PAUSE_PLAN_ARTIFACT_RESULT_INVALID`.
- The request model emitted a default empty `formats` array in its schema while
  runtime validation rejected omission and an empty array. `formats` is now a
  required `Field(min_length=1, max_length=3)`, matching the closed runtime
  rule before any side effect.

No open M43 renderer or CLI finding remains in this review scope.

## Validation

The frozen focused group passed 41 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m43-uv-cache uv run pytest -q \
  tests/test_meta_pause_plan_artifacts.py \
  tests/test_meta_pause_plan_artifacts_ultra_review.py \
  tests/test_meta_pause_plan_artifact_cli.py \
  tests/test_meta_pause_plan_artifact_cli_ultra_review.py \
  tests/test_meta_pause_plan_artifact_policy.py \
  tests/test_meta_pause_plan_artifact_integration.py
```

The two independent review files also pass Ruff and format checks.

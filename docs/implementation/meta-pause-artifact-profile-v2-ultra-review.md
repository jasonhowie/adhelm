# Meta campaign PAUSE artifact-profile V2 ultra review

This review covers the explicit build-only `META_CAMPAIGN_PAUSE_V2` profile in
`scripts/build_native_approval_artifact.py` and the adjacent read-only pair
comparator. It uses fake Git, Swift, Mach-O, wheel, tar, and staged-directory
boundaries only. It does not build, import, install, launch, sign, register, or
authenticate an artifact.

The profile selector accepts only `V1` and `META_CAMPAIGN_PAUSE_V2`, before
contract, source, output, or tool activity. V1 pins both archived Meta
generated sources to their reviewed absent defaults at extraction and each
later source-integrity checkpoint. The explicit profile replaces and hashes
only the Meta Python binding and native route source, checks the fixed Meta
helper as ARM64/macOS, and records its fixed path, hash, target, full metadata
digest, and protocol digest in Meta-only manifest evidence. The result remains
unsigned and non-authorizing.

The wheel is inspected only as bounded ZIP data. The Meta profile requires the
generated Meta binding member, its exact payload hash and `RECORD` row, and the
compiled Meta bridge module. The test harness injects a generated binding whose
body raises `SystemExit`; its successful fake build proves the builder reads it
as wheel bytes and does not import it. No configured wheel executes.

The pair comparator accepts only the closed V1 or Meta file sets, validates
component hashes against staged files, and streams exact tar headers and bytes.
It rebuilds the full 18-field contract, descriptor, metadata digest, protocol
digest, and descriptor digest through the adjacent source contract parser. Its
report describes manifest declarations and matching local bytes; it is not
provenance, installation, runtime, or approval evidence.

Two comparator defects found during review were repaired before this final run:

- It originally accepted syntactically valid digest fields without recomputing
  the complete metadata and descriptor binding. The final comparator rebuilds
  those values through `native_approval_artifact_contract.py` and compares the
  original JSON shape without scalar aliasing.
- It originally used `TarFile.getmembers()`, which could allocate an unbounded
  member list before enforcing the 16-member limit. The final implementation
  uses `TarFile.next()`, admits only the fixed sequence, and rejects a surplus
  header immediately.

An older V1 builder ultra fixture also lacked the newly required reviewed
absent Meta Python module. The fixture now includes those exact bytes and has a
positive admission control before its altered/foreign-payload negatives run.
This was a test correction, not a product behavior defect.

Independent cases in
`tests/test_meta_pause_artifact_profile_v2_ultra_review.py` verify:

- V1 detects a native Meta-route replacement after the V1 client build, before
  wheel construction or output staging.
- Meta rejects a missing generated-binding `RECORD` row and a non-Mach-O helper
  before staging.
- V1 and Meta fake-builder artifacts cannot be compared as one profile.
- A rebuilt tar carrying a changed but well-formed metadata digest is rejected
  through full contract/descriptor recomputation.
- The comparator succeeds with `TarFile.getmembers()` forbidden, then rejects
  17 surplus tar headers.

The combined fake-only review group passed 64 tests:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m50-uv-cache uv run pytest -q \
  tests/test_build_native_approval_artifact.py \
  tests/test_native_approval_artifact_builder_ultra_review.py \
  tests/test_compare_native_approval_artifacts.py \
  tests/test_meta_pause_artifact_pair_integration.py \
  tests/test_meta_pause_artifact_profile_v2_ultra_review.py
```

Ruff, formatting, and `git diff --check` pass for the owned review tests. The
legacy helper test module has pre-existing strict-mypy diagnostics when imported
by a standalone strict check; this review does not represent that inherited
test-harness baseline as a clean mypy result.

No open M50 source finding remains within this review boundary. A configured
profile, signed artifact, installed host, helper execution, wheel import, OS
credential operation, provider operation, and approval authority all remain
outside it.

## Exact M50 follow-up

Commit `0540a8a4d1ca739d422eabbe53c03f9896d585f4` separately passed
3,451 full Python tests and 1,274 ultra regressions, strict typing across
156 source files, installed unconfigured smokes and reproducible Python
packaging. Exact unsigned V1 pairs matched eight files; exact unsigned Meta
V2 pairs matched nine files and thirteen tar members, as the default and Meta
reproducibility validation records show. All
configured products remained unexecuted. The M49 native 292-test result is
carried by an identical native package Git tree, not a new M50 native run.

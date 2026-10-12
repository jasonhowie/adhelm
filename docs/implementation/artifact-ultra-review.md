# Artifact renderer and report-cache ultra review

Reviewed: 2026-09-04. This is implementation-stage validation, not a release
claim. The review used hermetic local files and fixture reports only: no provider
HTTP, credentials, customer data, or remote authority was used.

## Findings and remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| P1 | A task override with an empty output directory silently fell back to the saved directory. An explicit override could therefore publish somewhere the task did not select. | Resolution distinguishes `None` (inherit) from `""` (invalid local directory). `test_blank_task_destination_does_not_fall_back_to_saved_directory` fails before publication. |
| P1 | A retry could mark an artifact reused after a prior directory-fsync failure without retrying the directory fsync. | Every returned artifact, including reuse, receives a directory fsync and post-fsync identity check. `test_retry_after_directory_fsync_failure_reconfirms_reused_publication` covers the recovery path. |
| P1 | Post-publication temporary cleanup and descriptor-close failures could lose the only completed artifact reference, or report success after the held directory descriptor failed to close. | `ArtifactGenerationError` retains completed metadata for a published file and reports `TEMPORARY_CLEANUP_FAILED` or `DIRECTORY_CLOSE_FAILED`. The held-directory and post-fsync verifier-close regressions cover both paths. |
| P1 | Renderer target checks used path resolution and unbounded reads of a reusable target. A FIFO could block the process; a symlink/rename race could redirect publication or return a redirected path. | Publication and reuse now use no-follow directory descriptors, private regular-file checks, bounded reads, exclusive link creation, directory identity checks, and post-fsync target checks. The suite covers FIFO, mode `0644`, destination swap, target swap, concurrent create collision, and transient link-count races. |
| P1 | The derived report-artifact cache used path-based symlink checks followed by `mkstemp` and path replacement. A same-user rename/symlink race could redirect cache writes outside the selected local data root. Cached artifact reads also used a path read after validation. | Cache creation, reads, and replacement now use private descriptor-relative entries. Cache files are bounded private regular files; cached artifact content is re-read through the no-follow destination descriptor and hash-checked. `test_cache_path_swap_cannot_redirect_descriptor_relative_cache_write` and `test_cache_fifo_fails_closed_without_a_blocking_read` cover the boundary. Cache failure remains a partial result and does not alter retained report history. |
| P1 | The report CLI recognized an artifact generation failure but a full output limit could replace it with a generic `OUTPUT_LIMIT_EXCEEDED`, losing retained and completed artifact references. Its first compact fallback could itself exceed the legal 1 KiB minimum with three artifact digests and maximum-length failure fields. | `_report` emits `PARTIAL` with exit code 5, then uses a second minimal bounded envelope if necessary. It retains the record digest, failure code, artifact count, and every completed artifact SHA reference. The worst-case three-reference regression fits in 1 KiB. |
| P1 | `OutputPreferences.detail` affected only the rendering-settings digest; compact, standard, and detailed produced identical full views. | The renderer now applies a non-mutating presentation transform: compact wraps only size-saving repeated object rows with explicit inheritance, standard retains the full redacted document, and detailed adds a static structural field guide without source values. `test_saved_detail_changes_presentation_without_changing_evidence_or_review_facts` covers every format. `test_compact_keeps_boolean_and_integer_fields_distinct_when_factoring` prevents Python's `True == 1` rule from corrupting a lossless compact view. |

## Privacy and authority checks

Artifacts accept only retained normalized reports or audits and render the
existing keyed redaction projection. The regressions cover GA4 event values,
nested reference fields, formula escaping, static HTML without scripts or
remote assets, and no raw local path in the bounded report fallback. Artifact
policy resolution and cache lookup make no provider request and create no
provider authority. Automatic report artifacts are derived only after a report
has been retained; an artifact/cache failure reports a partial local result
without retracting canonical history.

## Remaining limits

- A same-UID process can mutate a returned local file after the final in-method
  check. The artifact digest is supplied for a consumer to verify before acting;
  cache reuse rehashes bytes through the safe descriptor path. This is a normal
  local filesystem trust boundary, not proof of immutable storage.
- POSIX mode `0600` cannot prove that a macOS inherited ACL grants no additional
  reader. Selected local directories remain part of the user's OS trust policy.
- This review covers the retained report/audit renderer. The later separately
  typed [pause-plan artifact](pause-plan-artifacts.md) surface accepts only a
  verified reconstructed OpenAI pause action; neither surface accepts offline
  candidates or makes a plan/file opening an execution authority.
- The report-rule documentation was corrected during this review to describe
  preflight, verified-prior-report cache reuse, and post-retention partial
  failures. The detail transform and its renderer integration passed focused
  local validation; release validation still needs an exact integrated source
  snapshot.

## Validation

```text
uv run pytest -q tests/test_artifacts.py tests/test_artifact_cli.py \
  tests/test_artifact_workflows.py tests/test_artifact_ultra_review.py \
  tests/test_artifact_detail.py tests/test_artifact_detail_integration.py
# 51 passed

uv run ruff check src/adhelm/artifact_detail.py src/adhelm/artifacts.py \
  src/adhelm/artifact_cli.py src/adhelm/artifact_workflows.py src/adhelm/cli.py \
  tests/test_artifact_detail.py tests/test_artifact_detail_integration.py \
  tests/test_artifact_ultra_review.py tests/test_artifact_workflows.py
uv run ruff format --check src/adhelm/artifact_detail.py src/adhelm/artifacts.py \
  src/adhelm/artifact_cli.py src/adhelm/artifact_workflows.py src/adhelm/cli.py \
  tests/test_artifact_detail.py tests/test_artifact_detail_integration.py \
  tests/test_artifact_ultra_review.py tests/test_artifact_workflows.py
uv run mypy src/adhelm/artifact_detail.py src/adhelm/artifacts.py \
  src/adhelm/artifact_cli.py src/adhelm/artifact_workflows.py src/adhelm/cli.py
git diff --check
# clean
```

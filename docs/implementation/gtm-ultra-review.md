# GTM read inventory ultra review

Reviewed: 2026-09-04  
Reviewed source commit: `9b8745ccc32c84f890b4a42b76a84bc48f33c955`

## Scope

This review covered the fixed GTM v2 inventory reader in
`src/adhelm/providers/gtm_read.py`, its baseline and adversarial fixture tests,
and `docs/research/gtm-read-contract.md`. It used fixture HTTP only; no OAuth
credential, customer container, provider request, workspace mutation, or raw
GTM payload persistence was used.

## Findings fixed

| Severity | Reproduced issue | Fix |
| --- | --- | --- |
| P1 | A `model_copy()`-forged account or container native ID could carry path syntax into the fixed request parent before identity verification. | The reader validates decimal native IDs and regenerated local references, then requires its identity-session verifier before token retrieval or HTTP. |
| P1 | Nullable or omitted latest-header counters had no honest representation. Treating them as zero would create a false equality claim; rejecting them unconditionally would conflict with the generated client contract. | Every supplied counter remains bounded and must match the latest version arrays. Omitted or JSON-null fields return `HEADER_COUNTS_UNAVAILABLE` with named `missing_counter_fields`; only eight supplied matching fields return `ALL_HEADER_COUNTS_MATCH`. |
| P1 | Nested version/container data needed parent binding and a closed metadata boundary to prevent a foreign resource or raw custom-code value from becoming normalized output or an error. | Closed outer/container metadata validation, account/container/path binding, HMAC-only opaque code markers, and redacted failure paths now fail closed. Explicit deleted versions fail; absent documented `deleted` defaults to false. |
| P2 | An oversized workspace page could reach item normalization or the reference mapper before its total-item limit was applied. | The 1,000-item cap is checked before workspace-item processing; repeated pagination and duplicate items also fail closed. |

## Verified boundary

- The reader has only four fixed `GET` paths: live version, latest header,
  header-selected latest version, and bounded workspace listing.
- It rereads the latest header and live version, rejects changed observations,
  and labels a successful result `SEQUENTIALLY_REREAD_NONATOMIC` with
  `NOT_EVALUATED` staging eligibility.
- It compares every supplied non-null header counter, including when another
  counter is unavailable. Matching counters do not establish resource
  completeness or a dependency proof.
- Raw tag conditions, template source, custom HTML, names, and descriptions do
  not appear in the normalized inventory or errors.

## Remaining limits

This is library-only: no installed `adhelm` command exposes the GTM reader. It
is fixture-tested rather than live capability evidence. A caller must compose
the verifier with a current, real GTM identity session; a callback that merely
compares strings is not provider identity proof. The sequential view is not
atomic and does not reread latest-version content or workspace pages. It does
not inspect workspace contents, prove dependencies or staging readiness, run
quick preview or browser verification, create a version, synchronize, publish,
or mutate GTM.

## Validation

```text
uv run pytest -q tests/test_gtm_read.py tests/test_gtm_read_ultra_review.py tests/test_gtm_counts.py
# 26 passed
uv run ruff check src/adhelm/providers/gtm_read.py tests/test_gtm_counts.py
uv run ruff format --check src/adhelm/providers/gtm_read.py tests/test_gtm_counts.py
uv run mypy src/adhelm/providers/gtm_read.py
# clean
```

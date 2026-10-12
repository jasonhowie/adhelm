# Installed guidance and development-validator ultra review

Reviewed: 2026-09-04. This is local implementation-stage evidence, not a
release claim. The review used authored guidance, synthetic files, a local Git
archive, and offline build tooling. It made no provider request, did not access
credentials or customer data, and did not invoke an external agent.

## Findings and remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| P1 | Guidance path handling could blur a source checkout with an incomplete installed package, and path-based reads could not hold a verified directory chain while opening a document. | The bundle root permits the source fallback only from a recognized `src` checkout. Installed packages require a real bundled directory. Descriptor-relative, no-follow traversal opens only the static skill/section allowlist and fails closed for missing, symlinked, nonregular, oversized, or non-UTF-8 documents. |
| P1 | Frontmatter extraction could accept duplicate or detached metadata fields, and a `main` request read the document again after parsing metadata. | Metadata now requires one matching name, description, metadata block, and nested semantic version. A main response derives metadata and digest from the same bounded read. Regressions cover duplicate/detached metadata and CRLF UTF-8 digest truth. |
| P2 | A catalog cursor beyond the static skill list produced an empty successful response. | It now returns `INVALID_GUIDANCE_CURSOR`; the normal terminal cursor remains explicit and bounded. |
| P1 | Development validation inherited environment variables that could select a pytest subset, load external pytest/mypy behavior, alter Python import/assert behavior, or redirect `uv` project configuration while still labeling the result as a full suite. | The validator removes validation-affecting `PYTEST_*`, `MYPY_*`, `PYTHON*`, `RUFF_*`, `PIP_*`, `GIT_*`, `HATCH_*`, and all `UV_*` except the caller-provided `UV_CACHE_DIR`. It supplies explicit test paths/configuration, disables pytest plugin autoload, uses explicit non-assert smoke failures, and keeps tool caches outside archived source. |
| P1 | The two builds reused the source extraction after tests and static analysis, so generated cache artifacts could affect the first build or make the second build dependent on the first. | Each build now extracts a fresh copy of the original exact-commit archive before offline build. The regression mutates a first extraction and proves a later extraction retains only archived bytes. |

## Verified guidance boundary

`guidance catalog` and `guidance show` expose only the two statically named
skills and their named local sections. They do not accept a caller-controlled
path, agent command, provider name, transport, or executable. Documents are
bounded to 64 KiB before decoding; JSON paging accounts for canonical JSON
escaping, UTF-8 byte length, metadata, cursors, and the final newline. The
adversarial suite reassembles synthetic escaped Unicode text and every shipped
section at the saved 1 KiB limit without omission or a split code point.

The development validator resolves one commit, archives that commit before any
source check, and runs import, Ruff, format, strict mypy, full tests, targeted
ultra regressions, repository checks, two independent offline builds, and an
installed-wheel smoke read of every named guidance section. Its CycloneDX output
is explicitly a runtime lock graph and deterministically binds the source
commit, lockfile digest, and source-date epoch.

## Focused validation

```text
.venv/bin/pytest -q tests/test_validate_development.py tests/test_guidance.py \
  tests/test_guidance_ultra_review.py
# 18 passed

.venv/bin/ruff check scripts/validate_development.py tests/test_validate_development.py \
  src/adhelm/guidance.py src/adhelm/guidance_cli.py tests/test_guidance_ultra_review.py
.venv/bin/ruff format --check scripts/validate_development.py \
  tests/test_validate_development.py src/adhelm/guidance.py src/adhelm/guidance_cli.py \
  tests/test_guidance_ultra_review.py
.venv/bin/mypy src/adhelm/guidance.py src/adhelm/guidance_cli.py
.venv/bin/python -m py_compile scripts/validate_development.py
```

The hardened validator was also exercised against the pre-review committed
`HEAD` (`5293a60bc65bd8044bbcf0249c6afe61f5e6ce1a`). It passed the isolated
source checks and two clean offline builds, then correctly stopped at the
installed-wheel command smoke because that older archive does not contain the
new uncommitted guidance/candidate/auth command surface. The resulting
`Installed wheel is missing expected development commands` error is expected
for that snapshot and is not passing validation evidence. Run the validator
again after committing the reviewed worktree and record that exact result.

## Remaining limits

The guidance slice remains authored local guidance; named-agent host
compatibility, usability, and provider capability are unvalidated. The
validator establishes matching local offline development builds for one
committed snapshot. It does not sign, publish, tag, perform a release SBOM,
license or vulnerability audit, or establish the broader release and live
evidence required by the definition of done. The reported state remains
`IMPLEMENTING`.

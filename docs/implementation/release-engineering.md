# Release engineering for AdHelm 0.1.0

Implements the release-engineering section of
[`release-0.1.0.md`](../release-0.1.0.md). The build never tags, never publishes and
never creates the public repository; those are owner actions, listed under
[Owner actions this relies on](#owner-actions-this-relies-on) below.

## Files

| File | Purpose |
| --- | --- |
| `LICENSE` | The unmodified Apache License 2.0 text; a test pins its SHA-256 |
| `NOTICE` | Product name, copyright line and the statement that Apache-2.0 grants no right to the AdHelm name or marks |
| `pyproject.toml` | Name `adhelm`, version, `license = "Apache-2.0"` (PEP 639) with `license-files`, classifiers, project URLs, `requires-python = ">=3.12"`, the sdist file list, the PyPI long-description rewrite and the exact build-tool pins |
| `.github/workflows/release.yml` | Dry run on every push and pull request; release on a `v*` tag |
| `.github/workflows/dco.yml` | DCO sign-off check on pull requests |
| `scripts/release_artifacts.py` | Verifies the wheel and sdist, writes and checks `SHA256SUMS`, normalises and verifies the SBOM |
| `scripts/smoke_installed_wheel.py` | Offline smoke of an installed wheel |
| `scripts/check_dco.py` | The DCO check the workflow runs |
| `scripts/check_dependency_licenses.py` | Apache-2.0 clearance of runtime dependencies; see the [review](dependency-license-review.md) |

The copyright holder is "The AdHelm authors", so no personal name enters the
public tree. If the owner wants a named legal holder, change `NOTICE` and the
`authors` entry together.

## Workflow

| Job | Runs on | Does |
| --- | --- | --- |
| `build` | every push and pull request | Clears runtime licenses; scans the locked runtime dependencies for known vulnerabilities with `pip-audit`; builds the sdist and wheel with `SOURCE_DATE_EPOCH` set to the commit time; rebuilds into a second directory and requires byte-identical files; verifies the artifacts (and that a tag equals `v` plus the package version); exports a CycloneDX 1.5 SBOM, binds it to the commit and `uv.lock`, verifies it, exports and binds it a second time and requires identical bytes; writes `SHA256SUMS` |
| `smoke` | every push and pull request, on Ubuntu, macOS and Windows | Checks the downloaded files against `SHA256SUMS`; installs the wheel into a fresh environment; runs `scripts/smoke_installed_wheel.py` from outside the checkout |
| `attest` | `v*` tag in `jasonhowie/adhelm` only | Re-checks the checksums and records GitHub build provenance for the wheel, sdist and SBOM |
| `publish` | same | Re-checks the checksums and publishes exactly the wheel and sdist to PyPI by trusted publishing, in the `pypi` environment |
| `github-release` | same | Re-checks the checksums and creates the GitHub release with the wheel, sdist, SBOM and `SHA256SUMS` |

A branch push or pull request therefore builds and smoke-tests but cannot reach
`attest`, `publish` or `github-release`: those jobs carry
`github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') && github.repository == 'jasonhowie/adhelm'`,
and no `id-token`, `attestations` or `contents: write` permission, environment or
PyPI step exists in any other job. `tests/test_release_workflows.py` pins that
structure. The artifacts are built once; every later job downloads them and checks
them against the build job's checksums instead of rebuilding. No stored PyPI token
exists: publishing uses the OIDC identity of the `release.yml` run in the `pypi`
environment. Actions are pinned to commit hashes. If the public repository is
renamed, change the repository guard in the three jobs and in the test.

### What the smoke checks

`scripts/smoke_installed_wheel.py` runs with the interpreter of an environment that
holds only the wheel and its dependencies:

- the package is imported from that environment's site-packages, not a checkout;
- `adhelm version` reports the expected version; `adhelm discover` lists every
  write command as `UNRELEASED` with nothing enabling it, and labels all eight
  providers; `adhelm doctor` on a fresh data directory asks for attention and
  performs no remote check;
- a write command (`execute openai-pause`) is refused with `WRITES_NOT_RELEASED`
  and exit code 3;
- an offline fixture report: a Google Ads daily report runs through the installed
  CLI against a fake HTTP transport and a fake credential lookup, with socket
  connections blocked, and `history verify` then verifies the ledger it wrote.

It never contacts a provider or the operating-system credential store.

### What the artifact check requires

`scripts/release_artifacts.py verify` requires exactly `adhelm-<version>-py3-none-any.whl`
and `adhelm-<version>.tar.gz`, where the version is the one in `pyproject.toml`; the
wheel's metadata to name `adhelm`, declare `License-Expression: Apache-2.0` and exactly the
`LICENSE` and `NOTICE` files, with those files byte-identical to the repository's;
no top-level wheel entry other than the package and its `dist-info`; `py.typed`, both
bundled skills and the pilot records directory; and an sdist rooted at
`adhelm-<version>/` with regular files only, carrying `pyproject.toml`, `PKG-INFO`,
`README.md`, `LICENSE` and `NOTICE`. Archive members that escape their root are
refused.

It also requires the long description (the `METADATA` body of the wheel and the
`PKG-INFO` body of the sdist) to read correctly on pypi.org: no relative markdown link,
reference definition, `href` or `src`, and no link into the repository at a tag other than
`v<version>`. The long description is `README.md`, rewritten at build time by
`hatch-fancy-pypi-readme` so each relative link such as `docs/setup/ga4.md` becomes
`https://github.com/jasonhowie/adhelm/blob/v<version>/docs/setup/ga4.md`. The version in
that rewrite (`pyproject.toml`, `[[tool.hatch.metadata.hooks.fancy-pypi-readme.substitutions]]`)
is written out, so a version bump must change it too; the artifact check and a test fail
if it is left behind. The links resolve once the tag exists.

### What the SBOM check requires

`uv export` stamps a random serial number and the current time on the SBOM, which would
make the published file differ on every run. `scripts/release_artifacts.py normalise-sbom`
replaces them with values derived from the source, the way
`scripts/validate_development.py` does: the serial number is a UUIDv5 of the commit, the
SHA-256 of `uv.lock` and `SOURCE_DATE_EPOCH`, the timestamp is `SOURCE_DATE_EPOCH`, and the
metadata gains the properties `adhelm:source_commit`, `adhelm:uv_lock_sha256`,
`adhelm:source_date_epoch` and `adhelm:scope` (`RUNTIME_LOCK_GRAPH`). The document is
written with sorted keys and compact separators. `verify-sbom` then requires CycloneDX 1.5,
this project and version as the root component, PyPI package URLs only, and those
properties, the serial number and the timestamp to match the checkout's commit,
`uv.lock` and epoch. The workflow exports and normalises twice and compares the files with
`cmp`. The derivation is also in `validate_development.py`, which is loaded by file path
in its tests, so the two copies are kept separate. The two derivations share their inputs
but are separated by scope: the release serial also hashes the scope
(`RUNTIME_LOCK_GRAPH`), so the development runtime-lock graph, which is not the release
SBOM, never gets the same `serialNumber` as the published document.

### What the vulnerability scan requires

The build job exports the locked runtime graph (`uv export --locked --no-dev
--no-emit-project --format requirements-txt`, with hashes) and runs
`pip-audit --strict --require-hashes --disable-pip` on it before anything is built or
uploaded. Any known vulnerability, or any package pip-audit cannot check, fails the job,
and `attest`, `publish` and `github-release` depend on that job. The scan asks the
advisory database over the network, so it is the one build step whose result can change
without a change to the repository: an advisory published after the lock was written
fails the next run. The fix is to move the lock to a patched version, review it in
[`dependency-license-review.md`](dependency-license-review.md) and rerun; an advisory
that has no fix is a decision for the maintainers, recorded in the change that adds an
explicit `--ignore-vuln`, never a skipped step.

## Verifying a download

```sh
sha256sum --check --strict SHA256SUMS
gh attestation verify adhelm-0.1.0-py3-none-any.whl --repo jasonhowie/adhelm
```

PyPI trusted publishing also uploads its own attestations for the wheel and sdist.

## Limits

- The SBOM is the CycloneDX export of the locked runtime dependency graph
  (`uv export --no-dev`), bound to the commit and `uv.lock`. It lists package versions
  and PyPI URLs, not license data or the wheel's own files. Rebuilding the same tag with
  the same `uv` version reproduces it byte for byte; another `uv` version may export a
  different component order or tool entry.
- Reproducibility is checked by two builds in one run. The build backend and its
  dependencies (`hatchling`, `hatch-fancy-pypi-readme`, `packaging`, `pathspec`,
  `pluggy`, `tomlkit`, `trove-classifiers`) are pinned exactly for `uv build` with
  `[tool.uv] build-constraint-dependencies`, recorded in `uv.lock`, so a third party who
  rebuilds the tag with the same `uv` version installs the same tools. Python itself is
  pinned in the workflow (`UV_PYTHON`) but a different interpreter, or a different `uv`,
  is not covered, and a build tool release does not enter until someone updates the pins
  and the test that pins them. To update them, remove or loosen
  `[tool.uv] build-constraint-dependencies`, run `uv build -v` and read the `Installing
  build requirements` line (with the pins in force it only shows the pinned set), write
  those versions back as exact pins, run `uv lock`, and update the test that compares the
  pins to the lock.
- The vulnerability scan covers the runtime dependencies that are locked, not the build
  tools or the development group, and depends on the advisory database being reachable.
- The install smoke resolves dependencies from PyPI within the declared ranges, as a
  user's installer would; it does not use `uv.lock`. A dependency release that
  breaks the wheel therefore fails the release before anything is published. It runs
  with guards against the network and against every entry point of the `keyring`
  package, and fails if either is touched.
- The DCO check accepts a sign-off whose email is the commit author's or committer's,
  matches the `Signed-off-by` key without regard to case, and skips merge commits. It is
  independent of, and does not replace, the DCO GitHub app named below.

## Owner actions this relies on

The build never performs these; the owner does, as `release-0.1.0.md` says under
[Gate states](../release-0.1.0.md#gate-states):

1. Create the public repository `jasonhowie/adhelm` and push the exported tree. Install the
   DCO GitHub app, and enable private vulnerability reporting and branch protection on `main`.
2. Add the pending PyPI trusted publisher (project `adhelm`, owner `jasonhowie`, repository
   `adhelm`, workflow `release.yml`, environment `pypi`).
3. Create the `pypi` environment with required reviewers, so that a tag push waits for
   approval before `publish` runs.
4. Tag `v0.1.0` on the exported tree. Until then the dry run builds and tests but publishes
   nothing.

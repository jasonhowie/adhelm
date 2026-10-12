# Dependency license review for Apache-2.0 distribution

Status: reviewed 2026-10-11 against `uv.lock` for AdHelm `0.1.0`.

The [license inventory](dependency-license-inventory.md) records evidence and, by
design, decides nothing. The `0.1.0` release definition requires that the
dependency license inventory shows no license incompatible with Apache-2.0
distribution. This document and
[`dependency-license-review.json`](dependency-license-review.json) hold that
decision for the packages AdHelm installs at runtime. It is an engineering review
of declared metadata and license files, not legal advice.

## What is reviewed

AdHelm's wheel and sdist contain only AdHelm's own code, the bundled skills and the
committed pilot records, plus `LICENSE` and `NOTICE`. They bundle no third-party
code. A user's installer fetches the 38 packages reachable from the project's
runtime dependencies and installs them under their own licenses, so the question
for Apache-2.0 distribution is whether any of them forbids that arrangement. The
development group (pytest, ruff, mypy, pip-audit and their dependencies) is neither
distributed nor installed by `uv tool install adhelm` or `pipx install adhelm`, so it
is out of this record's scope.

## Result

All 38 runtime packages declare a permissive license, or the file-level copyleft
MPL-2.0, and none is cleared on the strength of an unreviewed expression:

| License | Packages |
| --- | --- |
| MIT | 17 |
| BSD-3-Clause | 8 |
| Apache-2.0 | 3 |
| BSD-2-Clause | 3 |
| MIT-0, MIT-CMU, ISC, PSF-2.0, Python-2.0, MPL-2.0, `Apache-2.0 OR BSD-3-Clause` | 1 each |

Two entries carry a note in the record:

- `certifi` is MPL-2.0. Its copyleft is file level and applies to changes made to
  the covered files. AdHelm installs it unmodified and never bundles or patches it.
- `pywin32` is installed on Windows only (through `portalocker` and `keyring`). It
  declares the PSF license, which the scanner reads from the classifier as
  `Python-2.0`, but its wheel also ships the license texts of components under
  LGPL-2.1 (`adodbapi`), BSD-3-Clause, MIT and a permissive Scintilla notice.
  AdHelm neither bundles nor imports `pywin32`, and users obtain it from PyPI under
  its own terms.

Binary wheels of other dependencies (for example Pillow, `cryptography` and
`pydantic-core`) embed third-party native libraries or compiled crates under their
own licenses. They are the dependency's distribution, not AdHelm's, and this review
does not enumerate them.

## How the review is enforced

`scripts/check_dependency_licenses.py` compares the runtime inventory with the
record and fails on:

- a runtime package that is not in the record;
- a package locked at a different version than the one reviewed;
- a reviewed license expression that is not made of Apache-2.0-compatible SPDX
  identifiers (`Apache-2.0`, `MIT`, `MIT-0`, `MIT-CMU`, `BSD-2-Clause`,
  `BSD-3-Clause`, `ISC`, `PSF-2.0`, `Python-2.0`, `MPL-2.0`). `OR` and `AND` are
  understood; parentheses, `WITH` exceptions and unknown identifiers fail closed;
- a record that disagrees with the license expression the installed package
  declares, so a dependency that relicenses cannot hide behind an old review;
- a reviewed package that is no longer a runtime dependency;
- an installed package the scanner could not compare with the lock: a different
  version, an unreadable or invalid version, a name mismatch or a metadata read
  failure. Only "not installed" is tolerated, because the lock holds packages that
  apply on other platforms;
- a run that compared no installed package at all (for example because
  `uv sync` was skipped), so an unsynced environment cannot report `CLEARED`. The
  `CLEARED` line states how many installed packages were compared.

`tests/test_dependency_license_review.py` runs it against the committed lock and
the test environment on Linux and macOS (the scanner reads files through POSIX
descriptors, so that one test is a documented skip on Windows), and the release
workflow runs it in its Linux build job. The `basis` field of each entry says where the reviewed
expression came from: PEP 639 `License-Expression` metadata, an exact Trove
classifier mapping, or the license file read from the installed or PyPI wheel
(`httpx`, `pyasn1`, `pyasn1-modules`, `tzdata`, `colorama` and `pywin32-ctypes`
declare no machine-readable expression).

## Changing a dependency

1. Update the dependency and `uv.lock`.
2. Run the inventory and the check:

   ```sh
   uv run python scripts/audit_dependency_licenses.py --scope runtime --output /absolute/new/path.json
   uv run python scripts/check_dependency_licenses.py --inventory /absolute/new/path.json
   ```

3. For each finding, read the package's declared license and license files, then
   update its entry in `dependency-license-review.json`. A license that is not in
   the compatible set needs a decision recorded here before the set changes.

`colorama`, `pywin32` and `pywin32-ctypes` install only on Windows, so a Linux or
macOS inventory reports them as not installed and no run compares the record with
installed metadata for them. Their entries were read from the PyPI wheels at the
locked versions on 2026-10-11 (license files for `colorama` and `pywin32-ctypes`,
the classifier and bundled license texts for `pywin32`).

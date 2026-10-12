# Dependency-license inventory ultra review

This review covers the local-only scanner in
`scripts/audit_dependency_licenses.py`. It does not contact package indexes,
providers, credential stores, browsers, or operating-system authentication.

The scanner now binds its output to a bounded, descriptor-read `uv.lock`
snapshot. Replacing the requested lock name after its descriptor has been read
causes a fixed lock-format failure instead of a scan of the replacement. The
lock graph preserves selected extras on both editable-project roots and
transitive edges, including nested extras, so an activated optional dependency
cannot be silently omitted from the declared development scope.

Installed license evidence is gated on both the normalized installed metadata
name and exact version. A same-version distribution with a different name, a
malformed UTF-8 metadata value, or a metadata accessor failure stops before
license scanning and emits a bounded review-required state. The scanner does
not turn legacy `License` prose into SPDX: it stores only its length and hash,
keeps the raw prose out of the canonical output, and leaves the overall legal
decision explicitly unmade.

License files are opened only through held non-symlink descriptors beneath the
selected distribution's own `.dist-info` directory. A `RECORD` entry naming a
different distribution, a FIFO, a symlink, an unstable file, or an over-bound
file is a review item rather than evidence. The total per-distribution byte
limit is checked before another candidate is opened once the budget is
exhausted.

New output creation holds its checked parent directory, uses exclusive
descriptor-relative creation with mode `0600`, fsyncs, and rechecks the current
directory entry. A parent-directory replacement or a hard link added during
creation raises a fixed `OutputPathError`; the requested output is removed when
its held entry is still the created file. These checks are point-in-time local
filesystem checks. They do not claim to prevent a separate process from
changing the filesystem after the scanner returns.

`tests/test_dependency_license_inventory_ultra_review.py` adds 12 independent
cases for nested root/transitive extras, a post-read lock swap, cross-dist-info
license records, aggregate-byte exhaustion before another open, FIFO handling,
malformed and failing metadata access, same-version name forgery, output
hard-link and parent-swap races, deterministic output, and legacy-text
non-inference.

Validation completed with:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_dependency_license_inventory.py \
  tests/test_dependency_license_inventory_ultra_review.py
.venv/bin/ruff check scripts/audit_dependency_licenses.py \
  tests/test_dependency_license_inventory.py \
  tests/test_dependency_license_inventory_ultra_review.py
.venv/bin/ruff format --check scripts/audit_dependency_licenses.py \
  tests/test_dependency_license_inventory.py \
  tests/test_dependency_license_inventory_ultra_review.py
git diff --check
```

The focused tests passed 34 cases. The scanner produces dependency and license
evidence only. It does not establish installed-package provenance beyond the
observed local metadata/files, legal compatibility, release approval, or
release readiness.

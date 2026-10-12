# Offline dependency-license inventory

The development validator already produces a runtime-lock CycloneDX graph.  Its
own contract says that graph is not a license audit.  This utility adds a
separate local evidence inventory for the exact `uv.lock` graph; it supports
the dependency-and-license-scan work required by the definition of done without
claiming that the release requirement is complete.

Run it with the locked local environment and an unused absolute output path:

```sh
./.venv/bin/python scripts/audit_dependency_licenses.py \
  --lock uv.lock \
  --scope all \
  --output /private/tmp/adhelm-license-inventory.json
```

`--scope runtime` retains packages reachable from the editable project's runtime
dependencies. `--scope development` retains packages reachable from its `dev`
group, including packages shared with runtime. `all` also retains a bounded,
explicit `UNREACHABLE_LOCKED` category rather than silently dropping a package
from the exact lock. Scope traversal covers all `uv.lock` resolution markers;
it does not pretend that the current host is the only release target.
Activated dependency extras are part of reachability: for example,
`cachecontrol[filecache]` includes its declared `filelock` edge. Unknown extras
or missing selected edges fail rather than silently omitting dependencies.

The scanner accepts only a regular, non-symlink format-1 `uv.lock` within a
fixed byte/package/artifact bound. It records the lock SHA-256, package version,
source kind, dependency edges and every supported source-distribution/wheel
SHA-256 plus byte size. The editable project is an anchor, not an external
dependency record. Unsupported multi-version graphs, unsafe numeric values,
unknown roots, malformed metadata and unsupported lock forms fail closed rather
than yielding a partial exact-graph claim.

For each locked external package, the scanner asks the local Python environment
for its installed distribution. It reads license metadata and declared license
files only after the installed name and version exactly match the locked package.
Missing distributions, name/version mismatches, unreadable metadata, unsafe paths,
oversized files and unavailable file records are explicit `REVIEW_REQUIRED`
states. License file contents are never emitted: the output contains a bounded
relative record path, byte count and SHA-256 only. Final output creation requires
a new absolute path in an existing non-symlink directory, uses exclusive
creation relative to a held checked directory descriptor, writes mode `0600`, and fsyncs.
Existing files are never overwritten.
Lock and license reads use bounded regular-file descriptors without following
symlinks or blocking on FIFOs. Declared license paths are restricted to the
selected distribution's metadata directory; unsupported locations remain review
items. Path, inode and file-state rechecks detect observed replacements during
the operation. These are point-in-time checks, not a claim that another local
process cannot change files after the scanner returns.

License evidence has separate provenance fields:

- A bounded PEP 639 `License-Expression` is parsed with the installed
  `license-expression` parser when present. Unknown identifiers, invalid syntax,
  unsupported characters or an unavailable parser remain review-required.
- A small closed mapping can translate only exact, uniquely named Trove
  classifiers such as `License :: OSI Approved :: MIT License`. Broad classifiers
  such as `BSD License`, multiple mappings and unmapped classifiers do not become
  SPDX expressions. A malformed PEP 639 expression does not fall back to a
  classifier mapping.
- Legacy `License` free text is recorded only by byte count and SHA-256. It is
  never interpreted as an SPDX expression. Classifier text and PEP 639 source
  expressions retain their separate source provenance in the result.

Every package and the inventory as a whole state
`REVIEW_REQUIRED_NO_LEGAL_COMPATIBILITY_DECISION`. A syntactically valid SPDX
expression, a matching license-file hash, a lock hash, or a known classifier is
evidence for a human/legal review; none establishes license compatibility,
approval, attribution accuracy, a release artifact, or release readiness. This
tool does not contact package indexes, providers, credential stores, browsers or
operating-system authentication services. It does not replace a release SBOM,
vulnerability scan, provenance attestation, platform scan, signing, or the
definition-of-done release evidence.

The JSON bytes are sorted UTF-8 JSON with no timestamp, output path or host
identifier, so repeated scans over the same exact lock and the same installed
metadata/file bytes reproduce byte-for-byte. Different installed wheels or
platform-specific file sets are intentionally visible as different evidence and
must be reviewed rather than normalized away.

`tests/test_dependency_license_inventory.py` covers stable graph traversal and
runtime/development separation, exact lock hashes and artifacts, missing and
version-mismatched distributions, PEP 639 versus classifier provenance,
ambiguous/free-text non-inference, bounded safe license-file hashing, strict
numeric lock inputs, and safe new-output behavior.
The independent [ultra review](dependency-license-inventory-ultra-review.md)
adds replacement-race, graph-extra, distribution-provenance and malformed-input
regressions. Exact-snapshot development validation runs the scanner twice and
records its deterministic evidence hash and explicit review-required summary.

The Apache-2.0 decision for the `0.1.0` release is made separately, from this
scanner's runtime inventory, in the [dependency license review](dependency-license-review.md).

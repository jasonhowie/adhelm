# Native artifact pair comparison

`scripts/compare_native_approval_artifacts.py` compares two already-staged,
unsigned native approval-host artifact directories. It is a local,
read-only evidence tool. It does not build, install, sign, launch, import, or
unzip an artifact; it does not connect to a host, helper, provider, or approval
service.

The script accepts two absolute directory paths:

```sh
uv run python scripts/compare_native_approval_artifacts.py \
  --first /absolute/path/to/first-artifact \
  --second /absolute/path/to/second-artifact \
  --report /absolute/path/to/new-report.json
```

`--report` is optional. When supplied it must name a new regular file outside
both artifact trees; the tool refuses to overwrite it. A failure emits only a
fixed `NATIVE_ARTIFACT_PAIR_*` diagnostic and exits 2.

The module API is:

```python
compare_native_approval_artifacts(
    first: Path,
    second: Path,
    *,
    report_path: Path | None = None,
) -> dict[str, object]
```

It raises `NativeApprovalArtifactComparisonError`; its `code` is one of the
fixed local input, mutation, mismatch, output, or platform diagnostics.

## Closed layouts

The comparator derives the layout from the exact manifest contract rather than
from caller flags. `signing_key_access_group: null` means no entitlement file;
a declared access group means `AdHelmApprovalHost.entitlements` is required.

| Profile | Files without entitlement | Files with entitlement | Extra executable |
| --- | ---: | ---: | --- |
| `V1` | 7 | 8 | none |
| `META_CAMPAIGN_PAUSE_V2` | 8 | 9 | `adhelm-macos-meta-campaign-pause-approval-client` |

Both profiles require the fixed host, V1 client, wheel, `Info.plist`, one
manifest-derived LaunchAgent plist, `manifest.json`, and the deterministic tar
archive. The Meta profile is admitted only when the manifest contains exactly
one `meta_campaign_pause_approval_bridge_v2` object with its fixed profile,
scope, helper path, hashes, metadata/protocol digests, and binary-target facts.
V1 admits no Meta block or Meta helper.

The comparator rebuilds the complete 18-field contract and descriptor through
the adjacent source-owned `native_approval_artifact_contract.py` parser. It
requires the reconstructed canonical contract, descriptor, metadata digest,
protocol digest, and descriptor digest to match the manifest. This catches a
malformed or stale declared configuration before artifact-member access.

## What is compared

For each staged directory, the comparator holds non-symlink directory/file
descriptors, requires regular files with one link, and reads at most 256 MiB
(268,435,456 bytes) per
fixed member in 64 KiB chunks. It rejects unexpected entries, symlinks,
hardlinks, special files, changed paths, profile substitution, component hash
mismatches, and noncanonical staged file modes.

It compares every allowed staged file byte-for-byte across the two directories:
the archive, manifest, wheel, app files, entitlement when declared, and Meta
helper when selected. It also validates each component hash from the manifest.

The tar archive is read incrementally rather than materializing all headers. It
requires exactly the fixed sorted member list, no additional member, and no
links, special entries, PAX data, or nonempty owner/link fields. Every header
must use UID/GID 0 and the manifest-declared epoch. Directories and fixed
executables must have mode `0755`; other archive members must have mode `0644`.
Each archive regular member is compared byte-for-byte to its held staged file.

The deterministic result contains the inferred profile, manifest-declared
source facts, rebuilt configuration digests, every staged file’s byte count and
SHA-256, archive header facts, and the manifest’s unsigned-build status fields.
The report scope explicitly says that those source/runtime fields are manifest
declarations. The comparator does not independently prove Git provenance,
archive provenance, signing, installation, configuration, host/helper launch,
or any provider effect. Claims about those matters require separate evidence;
this development comparison does not establish them.

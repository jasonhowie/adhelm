# Windows portability diagnosis for R02

This note turns the R01 Windows archive into bounded implementation work.  It
does not alter supported-platform requirements, add a fallback, or treat the
existing fail-closed refusals as Windows support.

## Evidence boundary

The only Windows result reviewed for this diagnosis is the archived merge
validation record of 2026-09-08, a build record that the public tree omits.  Its
latest result is for `6564ee0`, while R01's exact validation target is
`febfbca`.  The archive reports 26 failures, 195 passes and 15 skips with
`--maxfail=20`; because two workers were active, it may include failures that
finished after the threshold.  It does not list the complete test IDs.  The
classification is a triage lead, not a complete failure inventory.  R01 has no
Windows runner and did not run a Windows emulator.

Windows is still a release blocker under the definition of done.  The current
source uses POSIX descriptors, ownership/mode bits and `pwd` for the protected
boundaries below.  Porting must prove equivalent Windows security properties;
it cannot silently substitute path checks, follow junctions, relax ownership or
convert an unsupported operation into a partial success.

## Proposed tickets

### R02.1 — safe selected regular-file input

Implement a narrow Windows backend for `input_files.py`.  Before parsing a
selected JSON/image request, it must reject final-component symlinks and all
reparse points (including junctions and other reparse tags), directories,
devices and special files.  It must open one bounded regular handle, compare a
stable file identity before and after the bounded read, reject replacement,
growth, truncation and metadata change, and close every handle on every path.
The public errors must remain path/content-free.  Define the behavior for SMB or
other remote filesystems explicitly; no latency guarantee may be inferred.

Native tests: valid regular input; symlink/junction/reparse-point rejection;
pipe/device rejection; swap-before-open and swap-during-read; removal, growth
and truncation; maximum-byte boundary; handles released after success/failure;
and failure before JSON parsing, credentials, ledger or provider access.  Keep
the existing POSIX descriptor tests unchanged.

### R02.2 — credential identity, private client file and coordination lock

Replace the POSIX-only credential lock only after defining a Windows-specific
credential-root contract.  It needs per-user identity and ACL checks for every
ancestor/final object, reparse-point rejection, an exclusive cross-process lock,
bounded acquisition/cancellation, and a durable create/open protocol.  It must
not create the root before rejecting an unsafe path.  The Google installed client
reader requires the same owner/ACL/reparse-point boundary and must prove that an
unsafe file was not read.  Preserve configuration-then-credential lock ordering
and never hold the lock across provider HTTP.

Native tests: two processes contend and release; cancellation/timeout leaves no
false ownership; user/foreign or weak ACL rejection; junction and rename races;
stale lock recovery with proof of non-concurrent ownership; client-file
no-read-on-rejection; normal private client read; and no credentials in output.
Run provider-facing tests only with fake stores/transports.

### R02.3 — artifacts, assets and storage observation

Port each descriptor-relative protected filesystem boundary independently:
asset vault, artifact destination/cache, guidance traversal and storage usage.
Each needs explicit reparse-point-safe directory traversal, private ACLs,
regular-file checks, bounded enumeration/read behavior, and atomic publication
or explicit partial-file diagnostics.  Artifact publication must retain its
existing rule that incomplete output cannot be presented as a completed artifact.
Storage observation must continue to avoid file-content reads and return no
names, paths or file contents.

Native tests: private destination success; weak ACL/reparse-point rejection;
replacement during cache/artifact write; partial-write recovery; hard-link and
special-file treatment; storage tree drift, depth/entry/time bounds; handles
closed; no accidental external-target traversal; and unchanged POSIX suites.

### R02.4 — ledger durability and provenance diagnostics

First capture the complete native-Windows failures without `--maxfail` into a
private transcript.  Classify every ledger failure by test ID and assertion;
the archive supplies only the high-level provenance-mismatch label.  Then make a
separate targeted change, if necessary, to preserve the append-only JSONL
contract: append and fsync `APPLY_STARTED` before mutation egress, reconcile
unresolved starts, maintain checkpoint/hash-chain provenance, and derive
projections only from a verified high-water mark.  Do not use a Windows-specific
weaker durability acknowledgment.

Native tests: interrupted/torn append behavior; fsync ordering observable at
the abstraction boundary; reopening/reconciliation; checkpoint and record
digest continuity; SQLite index rebuild from JSONL; forged/replaced/tampered
records rejected; ledger diagnostic privacy; and existing Linux/macOS tests.

## Implementation status (O3.1)

R02.1, R02.2 and R02.3 are implemented with Win32 primitives; R02.4 (ledger
durability and provenance) is not part of this change. The refusal-only
`tests/test_windows_platform_refusals.py` was removed because the boundaries it
asserted as refused now work; the cross-platform suites run on Windows and
`tests/test_windows_filesystem.py` holds the native cases. Evidence is the
`windows-latest` job of the offline conformance workflow, not a local machine.

Shared design (`src/adhelm/windows_filesystem.py`, `windows_acl.py`,
`directory_access.py`):

- **Reparse points.** Every object is opened with `FILE_FLAG_OPEN_REPARSE_POINT`
  and inspected through its own handle. Symbolic links, junctions, mount points
  and cloud placeholders are rejected, never followed. Directories, pipes and
  devices are rejected as input files.
- **Pinned paths.** A checked directory chain stays open without
  `FILE_SHARE_DELETE`, so no component can be renamed, deleted or replaced while
  a lock is held, an asset is read or an artifact is published. This is the
  counterpart of the POSIX descriptor chain and is stronger: the swap is
  prevented instead of detected afterwards. Children are addressed by path, so
  every ancestor must stay pinned for as long as a child is used. A directory
  opened with `PinnedDirectory.open_child_directory` (the derived artifact cache,
  every directory in a storage scan) holds a reference on its parent, and the
  chain is released only when the last reference is closed; closing the parent
  first changes nothing. The design does not rely on NTFS refusing to rename a
  directory that has an open descendant. `tests/test_windows_filesystem.py`
  renames the cache root and its parent while the cache directory is open and
  requires `PermissionError` for both.
- **Identity.** Volume serial plus 128-bit file ID, trusted only when the
  volume reports `FILE_SUPPORTS_OPEN_BY_FILE_ID` (NTFS, ReFS and servers that keep
  IDs stable) and neither value is zero. `windows_filesystem._state` raises
  `UnsafeObjectError` otherwise, so a FAT or exFAT object (which synthesizes a
  positional ID or none) is refused for every boundary that needs identity: the
  selected input, the asset vault, artifacts, the cache, storage observation and
  the credential lock. The refusal lives in `_state`, so it also covers two
  readers that never compare identity: the Google OAuth client file
  (`_open_checked_input` -> `open_regular_file` -> `fd_state`, error
  `GOOGLE_OAUTH_CLIENT_FILE_INVALID`) and the installed guidance bundle
  (`open_directory` and `open_file`, error `GUIDANCE_INVALID`). A client_secret
  JSON on a FAT or exFAT USB stick, or a Python installation on an exFAT volume,
  is therefore refused. The policy is `windows_acl.has_stable_identity`, unit
  tested on every platform; `tests/test_windows_filesystem.py` injects a zero ID
  and an unsupported volume and requires each boundary to fail closed.
- **Checked close.** Closing a pinned directory handle is checked.
  `PinnedDirectory.close` closes every handle it owns, then raises `OSError` if
  any close failed. On an open error path (`open_directory`,
  `open_child_directory`) a failed close is reported in place of the original
  refusal, with that refusal chained as `__cause__`, so a caller classifies the
  failure as a storage failure rather than an unsafe path (for example
  `assets.read_image` reports `ASSET_STORAGE_FAILED` instead of
  `UNSAFE_ASSET_PATH`). As a context manager the failure is raised only when no
  other error is active: `__exit__` keeps the error that ended its block. The
  callers keep their POSIX fail-closed reporting: `DIRECTORY_CLOSE_FAILED` in
  artifacts and `CREDENTIAL_LOCAL_CHANGE_UNVERIFIED` in the credential lock.
  Transient handles that pin nothing (the attribute-only handle in `path_state`
  and `PinnedDirectory.lstat`, the process token in `_token_sid`, and the file
  handle `_to_fd` closes when `msvcrt.open_osfhandle` could not adopt it, while
  that error propagates) are closed on a best-effort basis and their result is
  not checked.
- **Use after close.** A closed `PinnedDirectory` raises `UnsafeObjectError` from
  every operation (`stat`, `lstat`, `open_file`, `create_file`, `open_lock_file`,
  `link`, `replace`, `unlink`, `names`, `open_child_directory`,
  `current_state`, `directory_stat`): once its last reference is gone nothing
  pins the chain, so a name would be resolved by path alone.
- **Privacy.** Private objects are created with a protected DACL naming the
  current user, `SYSTEM` and `Administrators`. An existing object is private only
  when its owner is the current user (or the default owner of an elevated
  administrator token) and every effective allow entry names one of those
  principals. A NULL DACL, a foreign owner or any other allow entry is rejected.
  Ancestors of the credential lock directory must be owned by a trusted principal
  and must not let another principal delete or take over the directory; creating
  a child is tolerated, as for a sticky POSIX directory.
- **Network paths.** Every caller of `windows_filesystem.open_directory` is
  local-volume only: the credential lock root, asset vault, artifact destination
  and cache, storage observation root, and the installed guidance bundle. UNC
  paths, mapped network drives and `\\?\` or `\\.\` namespace roots are refused.
  A Python installation on a network location therefore cannot serve guidance
  (`GUIDANCE_INVALID`). A selected input file does not use `open_directory` and
  may live on a network share whose volume advertises stable file IDs; no latency
  bound is implied.
- **Alternate data streams.** A colon in the final component of an input file is
  refused, because a stream is not the file's regular content.
- **Durability.** Windows has no directory `fsync`. Files are flushed with
  `FlushFileBuffers` before publication and replacement uses
  `MOVEFILE_WRITE_THROUGH`; the directory entry itself relies on NTFS journaling.
  The asset and artifact code therefore skips the directory flush on Windows.

Per ticket:

- **R02.1** `input_files.read_regular_input_file`: reparse point, directory,
  pipe, device and alternate-stream rejection before any read; file ID, size and
  write/change stamps compared before and after a bounded read and against a
  fresh open of the name; handles released on every path.
- **R02.2** `credential_lock`: the lock directory is `%LOCALAPPDATA%\adhelm\credential-locks`
  (per user, independent of the data directory). It and its lock file are created
  private, the lock is a nonblocking exclusive byte-range lock that the operating
  system releases when the holder exits, so a killed holder leaves no stale
  ownership. Configuration-then-credential ordering and the rule against holding
  the lock across provider HTTP are unchanged. `read_google_installed_client`
  applies the same owner and DACL check and is never read when it fails.
- **R02.3** `assets`, `artifacts`, `artifact_workflows`, `storage_usage` and
  `guidance`: the same flows run on pinned directories. Temporary files are
  closed before they are linked or removed (an open handle blocks removal).
  Windows reports synthesized permission bits (`0o600`/`0o700` exactly when the
  DACL check passes) so the shared POSIX-shaped validators stay single-source.
  Storage observation reads only metadata and counts every reparse point as a
  link that was not followed.

Full-suite parity on `windows-latest` (the exit criterion) needed these further
changes outside R02.1-R02.3:

- `.gitattributes` pins `eol=lf`. Windows checkouts rewrote line endings in the
  checked-in vectors and reviewed sources whose bytes the tests hash and compare.
- `Ledger._fsync_file` opens the file read/write on Windows, because
  `FlushFileBuffers` fails on a read-only handle. Append, fsync, checkpoint and
  hash-chain behavior is otherwise unchanged. The remaining R02.4 work (an
  exhaustive native review of torn appends and recovery) is not part of this
  change.
- Tests that created FIFOs or symbolic links use the `make_pipe` and `make_link`
  fixtures (named pipe and symlink-or-junction on Windows). Tests that rewrote
  ledger segments with `write_text` now write bytes so Windows does not translate
  line endings. Parametrized one-megabyte inputs have short explicit ids: a
  one-megabyte test id stalled the Windows runner console and hung the job.
- POSIX-only instrumentation (patching `dir_fd` calls, renaming a directory that
  Windows keeps pinned) keeps a marked skip that names its Windows counterpart in
  `tests/test_windows_filesystem.py`.
- Eight test modules for release and native-host tooling that has no Windows
  implementation are skipped on Windows from one table in `tests/conftest.py`
  (`_WINDOWS_UNSUPPORTED_TOOLING`): the dependency license inventory
  (`scripts/audit_dependency_licenses.py`, POSIX descriptor reads on the release
  build host) and the macOS approval-host artifact builder and comparators (ADR
  0003). These are not shipped commands; porting them is a separate decision.

Known limits: the Windows privacy check trusts `SYSTEM` and `Administrators`, who
can read any file; it protects against other standard users, not administrators.
Ledger, configuration and data-directory privacy (`ensure_private_directory`) are
outside R02.1-R02.3.

## Native acceptance sequence

1. Provision a documented, authorized Windows runner with CPython 3.12.13 and
   frozen dependencies.  Record OS build, filesystem type and `uv` version.
2. Capture the current exact-source full CI-style output without `--maxfail` in
   a private location.  Publish only redacted counts, classifications and hashes.
3. Land and test one ticket at a time; rerun its native adversarial tests plus
   POSIX regressions.
4. Run the full Windows CI-style suite and build on the integrated exact commit.
   Record source/tree, commands, results, skip reasons and artifact hashes.
5. Retain failures honestly.  A full native pass is platform evidence, but it
   does not replace installed approval, signing, provider or release gates.

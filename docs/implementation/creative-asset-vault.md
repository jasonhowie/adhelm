# Local creative asset storage

This development implementation stores bounded content-addressed local bytes for
installed image analysis and registration. The caller owns image decoding;
storage itself does not claim that arbitrary bytes are a decoded image or a
provider-served rendition. It makes no network, model, or credential-store call.

`store_image(root, data, expected_digest)` requires exact immutable bytes, a
lowercase SHA-256 digest matching those bytes, and the existing 20 MiB image
bound. It opens each directory component relative to an already opened parent
with no-follow directory descriptors. The two asset directories must belong to
the current user and have mode 0700. A new private mode-0600 temporary object is
fully written and fsynced before an exclusive hardlink publication. An existing
object is never overwritten. Only the temporary name still naming the created
inode is removed; a replaced temporary name fails closed. The published object
is re-read and digest-verified, then its directory is fsynced before success.

`read_image(root, expected_digest)` does not create missing storage. It rejects
symlinks, directories, devices, sockets, pipes, nonprivate files, multiple hard
links, and oversized files before reading them. Opens are also nonblocking and
no-follow to handle replacement between selection and open. Bounded descriptor
reads verify the digest and compare inode, size, ownership, mode, link count,
mtime, and ctime before and after reading. Both reads and writes recheck the
selected directory-to-inode chain before returning. Read-side atime changes do
not invalidate an otherwise unchanged object.

Missing local bytes are `ASSET_NOT_AVAILABLE`; a digest mismatch is
`ASSET_CORRUPT`; observed replacement is `ASSET_CHANGED`. Neither a failed read
nor a retention policy rewrites an earlier asset record. Historical
`availability_status: AVAILABLE` records the ingestion observation; registration
must freshly read and decode the current local bytes. No uninterrupted future
availability, provider currentness, or protection against a privileged same-user
process is asserted. Network-filesystem latency is not bounded by this API.

A write interrupted before publication can leave no final object. A directory
fsync failure after publication can leave the valid final object despite a
failed operation result. A repeat store verifies and fsyncs the existing object;
it never deletes or replaces it to hide a failure. Unexpected I/O diagnostics
are static and do not expose paths, media, or raw operating-system exceptions.
Descriptors are closed on ordinary success and failure paths.

The implementation requires POSIX directory-relative, no-follow, nonblocking,
and hardlink primitives. Unsupported platforms fail explicitly with
`ASSET_STORAGE_UNSUPPORTED` before storage creation. This is development
coverage on macOS, not the v1 cross-platform release gate.

`tests/test_assets.py` exercises bounded no-overwrite storage, real pipe swaps,
opened descriptor closure, directory and file replacement, missing/corrupt
objects, private modes, hardlinks, and fsync failure/retry. Independent tests are
in `tests/test_creative_asset_vault_ultra_review.py`. These use only temporary
local files and injected failures.

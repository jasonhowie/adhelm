# Local storage usage observation

`storage usage` observes the existing selected data directory without reading
file contents, creating files, contacting providers or opening the credential
store. It reports regular-file lengths and entry counts for the three ledger
streams, assets, exports, caches and other local files. Known canonical paths
take precedence over overlapping output preferences. The actual installed
`artifact-cache` and ledger SQLite index paths are included in cache totals.
Classification by location does not prove ownership or safe deletion.

The implementation walks directories through no-follow descriptors and compares
two complete metadata traversals. Symbolic links and special files are counted
without opening their targets. A changed, unreadable, over-limit or replaced
tree fails explicitly without returning partial totals. The installed bounds
are 25,000 entries, 64 descendant levels and a checked 30-second observation
budget. Filesystem calls can still block on an underlying filesystem; the
budget is checked between calls and late results are refused. Platforms lacking
the required descriptor primitives fail explicitly; Windows support remains
required v1 work.

Results contain no local names, paths, inode IDs or file contents. The saved
artifact destination is classified only when lexically within the selected
root. The observed metadata separately identifies a missing destination, a
non-directory destination or any unfollowed symlink in its relative path. An
external destination is not opened or included in the total. A saved
destination equal to the data root is reported as overlapping rather than
misclassifying configuration and all other root files as generated exports.

These are apparent lengths per directory entry, including repeated hard links.
They do not measure unique physical allocation, sparse-file blocks or bytes
that cleanup would reclaim. Two matching observations are not an atomic
filesystem snapshot. The command does not verify JSONL integrity, infer growth
from a single observation, activate retention settings or authorize cleanup.
Observed growth, scoped retention policy previews, dependency-proven pruning,
durable intent/receipts and retained-boundary verification remain separate
required work under the local ledger contract.

The initial 20 filesystem tests and three installed CLI tests use temporary synthetic data and exercise
category totals, absent/empty roots, no content reads, external destinations,
overlapping preferences, hard links, symlink/FIFO handling, metadata drift,
directory replacement, entry/depth/deadline limits, clock rollback and explicit
unsupported-platform behavior. CLI output uses the saved byte bound and never
drops categories to fit it. These are development checks, not release evidence.

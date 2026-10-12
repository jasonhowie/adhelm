# Local storage usage ultra review

This review exercises `storage usage` with temporary synthetic filesystem
trees only. It does not open customer data, invoke providers, access
credentials, or create retention or cleanup work.

The review covers five adversarial cases:

1. A configured export path whose final component is a symbolic link reports
   `SYMLINK_NOT_FOLLOWED` and does not open the link target.
2. A configured export path below a symbolic-link ancestor has the same
   fail-closed status; lexical containment does not mean the target was
   observed.
3. Missing, regular-file, and directory destinations report
   `NOT_PRESENT_IN_SCOPE`, `NOT_A_DIRECTORY`, and `WITHIN_SCOPE` respectively,
   without serializing local names.
4. Replacing the selected root pathname with an external symbolic link after
   both descriptor-backed scans fails with `STORAGE_OBSERVATION_CHANGED`.
5. The installed 1,024-byte minimum output limit emits only the bounded
   `OUTPUT_LIMIT_EXCEEDED` diagnostic, never a partial category projection or
   local filenames.

The initial implementation classified a configured in-root symbolic-link
destination as `WITHIN_SCOPE`. That overclaimed what the no-follow scan had
observed. The repaired status derives from the completed metadata map and
reports `SYMLINK_NOT_FOLLOWED`; missing and non-directory paths are also
explicitly distinguished. The command still reports apparent lengths per
directory entry only. It does not establish physical allocation, retention
eligibility, reclaimability, growth, ledger integrity, or cleanup authority.

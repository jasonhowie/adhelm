# ADR 0002: bounded machine results and edition discovery

> Names superseded by [ADR 0013](0013-open-source-release.md): the product, package and command were renamed to AdHelm. The text below is the historical record.

Accepted 2026-09-04 for implementation, with commercial decisions unvalidated.

`adcp` emits a versioned JSON envelope by default. It includes command/tool/schema
version, outcome, sanitized data and diagnostics, warnings, pagination metadata,
and artifact references. Exit codes are 0 success, 2 invalid input, 3 failed
precondition, 4 missing authorization, 5 partial, 6 ambiguous, 7 pending, and
10 internal failure. Ctrl-C uses 130. Missing inputs never trigger a terminal
prompt. Unknown arguments must fail without echoing potentially sensitive text.
Output overflow fails explicitly rather than silently dropping rows.

The development build exposes edition and provider evidence separately. An
auditor may inspect reports and review full canonical plan previews, but its
execution boundary prohibits mutations and browser measurement traffic.
The operator requires provider permissions and exact direct/delegated authority.
Edition selection does not supply either. Development config is not a secure
paid-distribution entitlement; signed edition manifests and download controls
remain distribution work. No renewal check or runtime meter is introduced.

Compact output and artifacts on-request are defaults. A task override is an
in-memory value and does not change saved preferences. Supported formats are
discoverable; unavailable renderers fail explicitly. User-selected external
agents can produce other document formats from safe canonical data. An output
rule never creates a schedule or grants access. Inline canonical review remains
required when optional artifacts are OFF.

Proposed maintained-edition policy: free compatibility updates for the latest
supported auditor minor; operator downloads/updates/support through the paid
distribution channel; already obtained builds continue to run. Agency auditors
may inspect explicitly authorized client accounts without account quotas.
Support initially covers reproducible software defects and published onboarding
guidance, with no response-time or personalized consulting promise. Supported
version windows and final sale terms require product-owner/commercial review
before sale, not before local implementation. These defaults add no license
heartbeat and make no validated conversion or willingness-to-pay claim.

# ADR index

This directory holds two ADR series that both start at `0001`. Numbers `0001`
through `0004` are therefore used twice. The collision is historical: the
Phase-0 design ADRs were written first as a design record, and the
implementation ADRs were later added under their own numbering while the build
was in progress. Neither series was renumbered, because the files are cited by
path from `docs/implementation/traceability.json` and from other documents.
Do not rename, move, or delete these files to resolve the collision.

**Conflict rule: where the two series disagree, the implementation series
wins.** It records what the shipped code in `src/adhelm` actually does. The
Phase-0 design series remains the record of the original intent.

**The next ADR number is `0014`.** Continue in a single sequence from there; do
not start a third series.

## Phase-0 design series

Bullet header style (`- Status:` / `- Date:`), dated 2026-09-04. ADRs `0005`
through `0011` continue this series and have no counterpart in the
implementation series.

| Number | File | Title | Status as written |
| --- | --- | --- | --- |
| 0001 | [`0001-stack-and-package-layout.md`](0001-stack-and-package-layout.md) | Stack and package layout | Superseded in part by 0001-local-runtime.md; the shipped package is `adhelm` with flat modules under src/adhelm. |
| 0002 | [`0002-cli-result-and-exit-codes.md`](0002-cli-result-and-exit-codes.md) | CLI, result, and exit-code contract | Superseded for exit codes by 0002-machine-interface-and-editions.md, which matches src/adhelm/errors.py and `adhelm discover`. |
| 0003 | [`0003-schema-versioning-and-canonicalization.md`](0003-schema-versioning-and-canonicalization.md) | Schema versioning and canonicalization | Accepted |
| 0004 | [`0004-provider-interface-and-conformance.md`](0004-provider-interface-and-conformance.md) | Provider interface and conformance | Accepted |
| 0005 | [`0005-threat-model-and-egress.md`](0005-threat-model-and-egress.md) | Threat model and egress control | Accepted |
| 0006 | [`0006-credentials-auth-and-authorization.md`](0006-credentials-auth-and-authorization.md) | Credentials, auth coordination, and authorization | Accepted |
| 0007 | [`0007-jsonl-sqlite-and-projections.md`](0007-jsonl-sqlite-and-projections.md) | JSONL ledger, SQLite index, and projections | Accepted |
| 0008 | [`0008-evidence-and-capability-promotion.md`](0008-evidence-and-capability-promotion.md) | Evidence layout and capability promotion | Accepted |
| 0009 | `0009-editions.md` (not published) | User-selected editions | Superseded by 0013 |
| 0010 | [`0010-live-test-and-fault-harness.md`](0010-live-test-and-fault-harness.md) | Live-test and integrated-fault harness | Accepted |
| 0011 | [`0011-release-signing-and-reproducibility.md`](0011-release-signing-and-reproducibility.md) | Release signing and reproducibility | Pending; narrowed for `0.1.0` by 0013 |

## Implementation series

Prose style. `0001` through `0004` and `0012` exist. Three of the first four
state their acceptance in a prose sentence rather than an explicit status line;
that is recorded below as "no status line". `0012` continues the single
sequence announced above rather than the Phase-0 numbering.

| Number | File | Title | Status as written |
| --- | --- | --- | --- |
| 0001 | [`0001-local-runtime.md`](0001-local-runtime.md) | deterministic Python core and explicit adapter boundaries | no status line (body opens "Accepted 2026-09-04 for implementation; release remains IMPLEMENTING.") |
| 0002 | [`0002-machine-interface-and-editions.md`](0002-machine-interface-and-editions.md) | bounded machine results and edition discovery | no status line (body opens "Accepted 2026-09-04 for implementation, with commercial decisions unvalidated.") |
| 0003 | [`0003-native-approval-host-artifacts.md`](0003-native-approval-host-artifacts.md) | fixed native host metadata and unsigned development artifacts | no status line (body opens "Accepted 2026-09-05 for the macOS implementation. Release remains IMPLEMENTING.") |
| 0004 | [`0004-installed-approval-bridge.md`](0004-installed-approval-bridge.md) | macOS installed approval bridge | Accepted for bounded offline protocol implementation on 2026-09-05. Review is complete. This does not authorize a runtime bridge, host installation, client signing, key enrollment, or an execution command. Its accepted kind subset is amended by 0012. |
| 0012 | [`0012-composite-execution-plan-approval.md`](0012-composite-execution-plan-approval.md) | provider-neutral composite-execution approval | Accepted for the offline Python protocol implementation on 2026-09-08. Amends 0004's accepted kind subset to admit `COMPOSITE_EXECUTION_PLAN` and `ATTEMPT_RECOVERY`. This does not authorize a runtime bridge for those kinds, a host change, client signing, key enrollment, or a CLI command. |

### Single sequence from 0013

| Number | File | Title | Status as written |
| --- | --- | --- | --- |
| 0013 | [`0013-open-source-release.md`](0013-open-source-release.md) | Free open-source release as AdHelm | Accepted 2026-10-10. Supersedes 0009; narrows 0011 for `0.1.0` |

## Resolved conflicts

- **Exit codes.** `0002-cli-result-and-exit-codes.md` lists a nine-code table
  (`0/2/3/4/5/6/7/8/9`). The shipped mapping is the one in
  `0002-machine-interface-and-editions.md`: `0` success, `2` invalid input,
  `3` failed precondition, `4` authorization required, `5` partial,
  `6` ambiguous, `7` pending, `10` internal failure, and `130` for Ctrl-C.
  Verified against `src/adhelm/errors.py` (`ExitCode`), the `exit_codes` block of
  `adhelm discover` in `src/adhelm/cli.py`, and `sys.exit(130)` in `src/adhelm/cli.py`.
- **Package name and layout.** `0001-stack-and-package-layout.md` specifies a
  `lacp` distribution with `lacp.cli`, `lacp.schemas`, `lacp.kernel`,
  `lacp.ledger`, `lacp.providers`, `lacp.measurement`, `lacp.creative`, and
  `lacp.evidence` sub-packages. The shipped package is `adhelm` with flat modules
  under `src/adhelm` plus one `src/adhelm/providers` sub-package; `lacp` appears
  nowhere outside that ADR. `0001-local-runtime.md` records the shipped layout.

## Known unresolved conflicts

- **Result envelope fields.** `docs/adr/0002-cli-result-and-exit-codes.md`
  specifies an envelope carrying `schema_version, command, tool_version,
  outcome, data, warnings, diagnostics, plan_ref, run_ref, status_ref,
  artifact_refs, next_actions`. The shipped `ResultEnvelope` in
  `src/adhelm/models.py` is a closed model (`extra="forbid"`) whose fields are
  `schema_version, tool_version, command, outcome, data, diagnostics, warnings,
  next_cursor, total_count, artifact_refs`. It has no `plan_ref`, `run_ref`,
  `status_ref`, or `next_actions`; `run_ref`, `status_ref`, and `next_actions`
  do not appear anywhere in `src/`. `docs/adr/0002-machine-interface-and-editions.md`
  instead describes pagination metadata, which matches the shipped model. Only
  the exit-code half of the older ADR has been marked superseded, so the
  envelope difference is still open. Neither file was edited for this index.

No other ADR pair was found to state something that shipped code in `src/`
contradicts. `docs/adr/0004-provider-interface-and-conformance.md` describes a
common typed port interface and a single conformance suite that are not present
in `src/adhelm/providers` or `tests/`; that is unbuilt work, not a contradiction with
shipped behavior.

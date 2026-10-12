# ADR 0008: Evidence layout and capability promotion

- Status: Accepted
- Date: 2026-09-04

## Decision

Evidence is local, deterministic, redacted, content-addressed, and indexed by a
machine-readable manifest. The exact layout, shared with
[`evidence-model.md`](../evidence-model.md), is:

```text
evidence/<release-id>/manifest.json
evidence/<release-id>/<requirement-id>/<evidence-id>.json
evidence/<release-id>/<requirement-id>/artifacts/<sha256>.<safe-extension>
```

`manifest.json` is the deterministic index over the individual evidence-entry
JSON files. The directory requirement ID is the entry's primary requirement;
additional requirement references are indexed without copying the entry. Test
class and profile pseudonym distinguish fixture, live, UI-reconciliation,
review, and other evidence; those classes do not create alternate layouts.

Evidence entries identify requirement, capability, canonical test class,
provider/API/connector version, profile pseudonym, commit, timestamps, source ledger checkpoints,
artifact digests, result, reviewer, and blocker or recovery state. Raw payloads,
credentials, customer identifiers, custom-code source, and prohibited data are
excluded.

Promotion is monotonic only with qualifying evidence:

```text
UNRESEARCHED -> DOCUMENTED -> FIXTURE_VERIFIED -> LIVE_VERIFIED
```

`BLOCKED` can occur at any unproved stage and never counts as parity. Research
documents cannot produce fixture or live promotion. Superseded behavior retains
old evidence and adds a new versioned entry; stale evidence cannot support a
current claim.

## Consequences

- The traceability matrix points to evidence types before evidence exists.
- Release claims cannot exceed the lowest required operation status.
- Export and support bundles require preview and user-selected destinations;
  nothing uploads automatically.

# Evidence model

Last updated: 2026-09-04

## Purpose

Evidence proves a requirement against a specific commit, capability, API
version, test class, and pseudonymous profile. Documentation and research define
what must be proved; they are not fixture or live evidence.

## Exact local layout

```text
evidence/<release-id>/manifest.json
evidence/<release-id>/<requirement-id>/<evidence-id>.json
evidence/<release-id>/<requirement-id>/artifacts/<sha256>.<safe-extension>
```

This is the only evidence directory contract. Test classes and profile
pseudonyms are fields, not alternate subdirectories. `manifest.json` is a
deterministic projection over the validated evidence-entry JSON files. The path
`requirement-id` is the entry's `primary_requirement_id`; any additional
`requirement_ids[]` are indexed by the manifest without copying the entry:

```text
schema_version, evidence_id, primary_requirement_id, requirement_ids[],
capability_ids[], test_class, result, commit, tool_version,
connector_version, provider_api_version, profile_pseudonym, observed_at,
ledger_checkpoints[], artifact_digests[], reviewer_ref,
data_classification, redaction_summary, blocker, recovery_state
```

Validation requires `primary_requirement_id` to contain exactly one known,
stable requirement ID and equal the `<requirement-id>` segment of the canonical
entry path. `requirement_ids[]` contains only zero or more additional covered
stable IDs as references; it excludes the primary ID and duplicates. Unknown
IDs, path mismatches, or a copied entry under another requirement directory
fail validation.

## Test-class taxonomy

The traceability matrix uses concise lowercase spellings. They map to the
canonical classes below. `+` requires separate evidence for every listed class.
`/` requires one combined run to satisfy both named contexts; it does not create
a new class. Adjectives such as `controlled`, `clean`, or `live` narrow the
class and must be recorded in the entry. Evidence entries store the uppercase
canonical class values, never the matrix shorthand.

| Canonical class | Traceability spellings | Proves; limits |
| --- | --- | --- |
| `RESEARCH` | `research` | Official behavior was reviewed on an access date; not implementation, fixture behavior, account access, or live behavior. |
| `DOC_REVIEW` | `architecture`, `contract`, `doc`, `docs`, `capability review`, `commercial review`, `output review`, `docs/output contract`, `docs/output scan` | A named contract or output was checked against documented requirements; not executable safety or provider support. |
| `UNIT_PROPERTY` | `unit`, `property`, `unit/property`, `schema/property`, `golden` | A deterministic local invariant. `golden` means immutable expected bytes/values; it does not imply a live provider. |
| `SANITIZED_FIXTURE` | `fixture`, `graph fixtures` | Schema/adapter/graph behavior for sanitized recorded cases; not current provider behavior. |
| `CONFORMANCE` | `conformance` | Common interface behavior against injected transports; not live access or UI parity. |
| `INTEGRATED_ACCEPTANCE` | `integration`, `integrated`, `integrated conformance`, `integrated live`, `integrated live fault`, `integrated live/UI` | Required subsystems compose under one scenario. Any `live`/`UI`/`fault` qualifier also requires those classes. |
| `FAULT_INJECTION` | `fault`, `process fault`, `live fault`, `controlled live fault` | Behavior at a named failure boundary. Live faults must use the safe injection rules in `live-test-design.md`. |
| `NEGATIVE_SECURITY` | `negative`, `live negative`, `adversarial`, `canary`, `leak scan`, `secret/history scan`, `network/output scan`, `privacy` | Prohibited input/effect is rejected and canaries do not leak. It proves only the enumerated surfaces. |
| `NETWORK_CAPTURE` | `network capture`, `network/output capture`, `network/output`, `install/network`, `offline/network` | Observed destinations and sanitized output match allowlists. `offline` additionally means execution with network disabled. |
| `LIVE` | `live`, `live read-back` | Bounded behavior on one named pseudonymous profile. Read-back proves only observed remote state for that operation/version. |
| `UI_RECONCILIATION` | `UI`, `UI reconciliation`, `live/UI` | API state/report matches the provider UI under identical dimensions and account context; not causality or future compatibility. |
| `BROWSER_MEASUREMENT` | `browser`, `browser measurement`, `browser/live`, `browser network capture` | Approved runtime, consent, and endpoint behavior. Processed reporting requires separate live evidence. |
| `INSTALL_DISTRIBUTION` | `install`, `clean install`, `clean-machine matrix`, `install/offline/network`, `package`, `offline` | Install, package, upgrade, uninstall, or network-independent behavior on each declared target. |
| `MIGRATION_PROJECTION` | `migration`, `projection`, `ledger`, `ledger/projection verification`, `cross-platform` | Version/read-model/export continuity and deterministic output. `cross-platform` repeats the same vectors on every supported OS. |
| `AGENT_CONTRACT` | `deterministic caller` | A model-free caller can discover and exercise the machine contract; not named-agent usability. |
| `USABILITY` | `usability` | Consented users complete the stated workflow in the recorded host/version; not general market demand. |
| `MANIFEST_VALIDATION` | `manifest validation` | Required entries, references, digests, and schemas form a closed manifest; not the truth of referenced evidence by itself. |
| `REPRODUCIBILITY_SIGNING` | `reproducibility`, `signing`, `reproducibility + signing`, `supply-chain/reproducibility`, `signature/tag verification` | Build inputs reproduce the unsigned payload and signatures bind selected identities; not provider behavior. |
| `SOAK` | `soak` | Scheduled reliability for one qualifying calendar day or the declared consecutive run; not release-commit smoke/signing. |
| `RELEASE_SMOKE` | `release smoke` | Reversible behavior from the exact release commit; not reproducibility/signature unless linked separately. |
| `INDEPENDENT_REVIEW` | `independent review`, `independent evidence review` | A named independent reviewer assessed the stated evidence scope; not criteria outside that scope. |

## Promotion and freshness

Capability status follows [`capability-matrix.md`](capability-matrix.md).
Promotion to `FIXTURE_VERIFIED` requires sanitized fixture plus conformance
evidence. Promotion to `LIVE_VERIFIED` additionally requires current live
evidence, UI reconciliation where applicable, API/capability version, account
pseudonym, and evidence-manifest reference. `BLOCKED` never promotes a claim.

Evidence becomes stale when its operation contract, API version, connector,
schema, safety kernel, profile identity/scope, or relevant provider behavior
changes. Supersession appends a new entry and preserves the old one.

## Redaction and integrity

Entries may include normalized allowlisted facts, keyed opaque references,
endpoint domains, field names, statuses, digests, and source ledger
high-watermarks. They exclude credentials, customer identifiers, raw requests
or responses, cookies/headers, person-level data, sensitive URL values,
custom-code source, and unapproved exact creative copy/media.

Every retained artifact has SHA-256, media type, byte count, producer version,
and classification. Optional exports are previewed and user-initiated. Evidence
is tamper-evident and local, not WORM storage.

## Completion

The release manifest must resolve every stable ID in
[`traceability-matrix.md`](traceability-matrix.md). `COMPLETE` remains
impossible until required live validation, the 30-day soak, final release-commit
smokes, reproducible signed artifacts, evidence review, and the signed
`v1.0.0` tag all pass.

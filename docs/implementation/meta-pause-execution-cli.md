# Meta campaign PAUSE execution CLI

The development CLI exposes `execute meta-campaign-pause --input REQUEST.json`.
Its closed `meta-campaign-pause-installed-approval-execution-request` schema
contains only integer schema version 1 and one retained plan-record digest.
The command does not accept a receipt, trust key, grant, token, provider
request, adapter, transport, artifact option or generic execution selector.

Static discovery and schema inspection do not resolve either native binding.
Discovery marks execution `UNRELEASED`, declares the
separate Meta V2 prerequisite and reports no default-runtime provider mutations
or verified live execution. A ready V1 OpenAI/GA4 binding cannot enable Meta.

The command is refused first by the [release-policy gate](release-policy-gate.md)
(`WRITES_NOT_RELEASED`, before any input is read). A build that lifted the gate
would then validate its bounded regular JSON input, read local output
preferences and reserve the complete redacted result envelope. Those local
reads intentionally precede readiness, matching the existing execution-command
policy. It then requires the distinct Meta V2 binding before invoking the
private workflow. Past the gate, an unconfigured build returns
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED` before any
protected credential, native helper or provider work. The private workflow
independently requires V2 readiness before its own Ledger/configuration reads.
Neither layer starts setup or supplies a fallback approval path.

The private workflow owns retained-source, configuration, expiry and recovery
checks around native approval; exact durable authorization precedes the
credential session and guarded kernel. CLI output contains only one Meta
subplan status, exact digests, consumed/recovery flags, at most three evidence
references and fixed next-step prose. Meta pause results cannot contain resource
references. Authorization IDs, credentials, raw provider payloads, reusable
approval material and arbitrary adapter diagnostics are excluded. Unknown or
unsafe result shapes return `EXECUTION_RESULT_INVALID` with exit 6. An unexpected
workflow exception becomes fixed `EXECUTION_RESULT_UNAVAILABLE` with exit 6
without its original diagnostic text; established `AdhelmError` results retain
their existing bounded classification.

The status-to-exit mapping remains verified=0, partial=5, ambiguous=6 and
no-effect/not-started=3. Non-success results still emit the bounded result
before exiting; no write is retried. No artifact is automatically produced and
no plan, review, digest, artifact or agent prose invokes execution implicitly.
The versioned ad-program guidance explains these boundaries progressively.

This registration proves neither installed native authority nor live Graph-v26
conformance. The fixed local acknowledgement subset still has the limits in
[the research note](../research/meta-pause-local-acknowledgement-subset.md).

## Validation

Focused tests use synthetic workflow returns and fake readiness functions.
Three additional cases compose the actual private workflow, module-issued
synthetic binding, fake framed pipe, kernel and fixed Meta HTTP adapter from a
retained plan; success, unfamiliar acknowledgement and lost reply preserve the
expected statuses with one POST and no replay.
They exercise closed input, static discovery/schema/guidance, distinct default
V2 refusal, Operator/output preflight, redacted bounded outcomes, exit codes and
unpresentable post-workflow results. No configured artifact, real credential
store, native helper or provider is invoked. The [independent review](meta-pause-execution-cli-ultra-review.md) has no open
finding in its eight-case scope. The final combined CLI/workflow/privacy/validator
group passed 158 tests, and product source typing passed for 159 modules. Full
archived M53 validation passed 3,579 tests and 1,306 ultra regressions, installed
unconfigured CLI checks and reproducible Python builds. Signed installation and
live provider conformance remain separate requirements.

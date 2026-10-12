# Implementation threat model and data flow

Status: initial engineering model; independent security review remains required.

The chosen agent supplies untrusted typed inputs to the local CLI. Provider
responses, OCR, exact copy, files, and model prose are untrusted data. They may
not grant authority or bypass schema, consent, identity, or egress checks.
Unknown fields and obvious sensitive values fail before persistence/display.
Heuristic text screening cannot establish legal meaning or detect every form of
sensitive text; closed operation schemas and declared data contracts are the
primary boundary. No compliance claim follows from local execution.

Credentials flow from the OS credential store into the selected adapter's
in-memory authentication headers, then to fixed approved HTTPS hosts. Explicit
CI profiles may use designated environment variables. There is no plaintext
credential-store fallback, automatic agent/tool invocation, or credential-cache
inspection. Default discovery and doctor inspect local readiness only.

Fixed adapter requests go through a bounded injectable HTTP client. Redirects
are denied; reads have bounded retry attempts and responses, while mutations
are attempted at most once and interrupted writes require reconciliation.
Adapter schemas, not the transport alone, define permissible fields, endpoint
paths, operation permissions, and aggregate-query semantics. Transport is
internal and never a generic CLI command. Provider origins are public fixed
origins; browser validation needs an additional consent/route/DNS egress design.

Validated normalized records append to locked, synced JSONL. An index or export
derives from verified checkpoints. Same-user hostile processes can replace
ordinary files and history, so hash chains are tamper-evident continuity checks,
not immutable storage or a security boundary against the local administrator.
Coordinator locks and durable start claims prevent accidental concurrent replay
on one host; copying a ledger does not distribute spending allowances.

Shared output uses keyed opaque references and omits raw marketing text by
default. Local operational identity remains available for correct authorization.
Agent-visible output can reach the customer's selected model. CSV escaping,
HTML escaping, redaction, deterministic digests, and artifact provenance must
apply to every output path and hidden data. Files and GET links cannot mutate.

Remaining review subjects include host-origin authority provenance, credential
refresh and revocation, crash windows, policy cumulative accounting, all-provider
API race semantics, runtime consent/browser egress, asset decoders, retained
history gaps, supply chain, platform signatures, and user-controlled recovery.

# Retained compilation reviews

`compile openai-ads --input <brief> --retain`, `compile meta-ads --input <brief>
--retain`, and `compile google-ads --input <brief> --retain` recompile the closed
local brief and append one `CANDIDATE_COMPILED` record to the canonical `changes`
JSONL stream. Without `--retain`, the command remains a preview. Retention does not
invoke a provider, create a native asset, grant authorization, or store an
executable plan. A successful receipt fits the minimum 1 KiB output preference.

The schema-versioned record contains the input/specification digests, a digest
of the retained review, compiler/capability versions, declared account and
creative references, hierarchy counts, exact USD maximum spend, initial paused
state, and unresolved prerequisites. It contains no native request, exact copy,
destination, file ID, approval, or subplan. In particular, declared creative
references are explicitly not verified against local creative history. Both
append and verified reads check the record's provider/account/source binding.

The candidate digest identifies the normalized review; the specification digest
identifies the compiler's full native candidate. The retained projection cannot
recreate that specification. Later planning requires a fresh compilation and
the missing identity, creative, and provider-file evidence, followed by the
ordinary exact-plan authorization and drift checks. The file-proof gap is
documented in [OpenAI file readback research](../research/openai-file-read-contract.md).

The record selects a closed provider variant using `compilation.provider`.
Existing OpenAI version-one records remain valid without rewriting history.
Meta and Google variants retain their entire typed review and planning boundary,
one declared creative reference, and the exact native lifetime-budget units.
Repeated account, creative, specification and spend facts must agree; altered
required checks fail validation even if someone recomputes the wrapper hash.
Meta retains placement/transformation uncertainty. Google retains its dynamic
RSA combination and unavailable component-attribution status. Their native
requests, Page/image references, keywords, geographic IDs and serving copy are
absent from retained records. A retained local label is a reference, not proof
that a native resource exists.

`candidate catalog` returns newest-first observations with bounded pagination.
Repeated explicit compilations append new observations; they can share a
candidate/specification digest but have different record digests. Older
observations remain readable with `candidate show <record-digest>`. Both commands
select a verified cross-stream checkpoint. Show applies the private export HMAC
to identifiers and caller-selected role handles, and pages creative references without dropping total counts or
critical spend/prerequisite fields. Catalog and retention receipts contain
only digests, bounded amounts, version/status metadata, and observation times.
Ordinary history projections include the new record using the same redaction
and deterministic JSONL/CSV reconstruction rules.

For unchanged history, catalog pagination adapts to the saved byte bound without
skipping records. Each call reports its checkpoint digest; callers must restart
a catalog walk if concurrent appends change that digest. A cursor does not pin
a multi-call snapshot.
Show preflights the redacted size before sharing-key access; a fixed in-memory
probe key supplies the same opaque-reference lengths and is never emitted.
When mandatory review plus a single creative row exceeds the bound, the caller
receives an explicit instruction to increase it. No required facts are silently
discarded, and a cursor beyond the available catalog or creative rows is invalid.

Receipt output is sized before append. The ledger uses its existing serialized,
hash-linked append and fsync path. If a local append or durability operation
fails, the command returns `CANDIDATE_RETENTION_UNVERIFIED` with a nonzero exit;
it makes no persistence claim and directs the caller to history verification
and catalog inspection before another retention attempt. The error does not
imply a remote side effect.

This is local compilation history, not `PLAN_CREATED`, approval evidence, a
native payload store, or a candidate-import/promotion interface. Same-user
arbitrary filesystem modifications are outside authenticity guarantees;
structural validity of a hash chain alone never establishes user authorization.

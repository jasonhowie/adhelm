# Retained measurement-comparison artifacts

`artifacts measurement-comparison <before-record-digest> <after-record-digest>
--input <measurement-comparison-artifact-render.json>` creates an optional
static local view of exactly one retained measurement comparison. It is a
fixture-verified historical review only. It does not read a provider, access a
credential until all local preflight checks pass, append history, authorize an
action, or create a remote mutation.

The closed `measurement-comparison-artifact-render` request has schema version
1 and permits only JSON, CSV, and static HTML formats, an existing absolute
local output directory, and one explicit replacement authorization. It accepts
no comparison body, snapshot, raw provider response, custom code, user text,
credential, provider URL, action link, or generic document payload. The
comparison itself comes only from the two retained digest selectors.

Before export-key access or file publication, the artifact layer takes a fresh
verified checkpoint through `compare_measurements`, rebuilds the supplied typed
`MeasurementComparison` from its exact before/after record pair, and requires
canonical equality with the supplied comparison, including its checkpoint
digest. It first validates the supplied model's raw JSON serialization, then
canonicalizes the validated model; this rejects a constructed or copied
`schema_version: 1.0` rather than normalizing it to `1`. A changed checkpoint,
source pair, digest, scope, fact, or raw control type fails with
`MEASUREMENT_COMPARISON_ARTIFACT_SOURCE_INVALID` before output.

`preflight_measurement_comparison_artifact_output` is a key-free boundary used
by the command before private export-key access. It builds the same closed
redacted document with a fixed synthetic 32-byte HMAC key and checks the full
row and JSON/CSV/HTML byte bounds. HMAC references have a fixed hexadecimal
width, so this reserves the real document's rendered size without treating the
synthetic key as an export key. The renderer repeats source reconstruction
immediately before it invokes the shared writer, closing drift between CLI
preflight and file creation. A row or byte failure creates no file and needs no
credential-store access.

The only document schema is `measurement_comparison_review`. It retains both
record, snapshot, and observation digests; the shared checkpoint and comparison
digests; the outer retained observation interval; the after-recorded historical
`as_of`; source completeness records; typed normalized-difference facts; and
the counts. `ArtifactMetadata.source_record_digest` remains the existing
single-source compatibility field and is anchored to the before record. The
document and agent-visible result separately retain both record digests and the
comparison digest, so that compatibility field never becomes a synthetic pair
identifier.

Account, profile, property, stream, container, workspace, version-slot, and
fact resource references are HMAC pseudonyms before the shared writer sees a
document. Fact rows contain only an allowlisted resource type, pseudonymized
reference, status, changed-field names, source fact digests, and opaque-marker
counts. The artifact never includes a retained snapshot payload, native ID,
provider response, event name, custom HTML/template source, custom code, or
user-entered text. Detailed presentation can add a static schema field guide;
it does not add source values.

The document always keeps the comparison limitations even when its fact list is
empty: `observed_actor=UNKNOWN`, null exact change time,
`SEQUENTIAL_NONATOMIC` consistency,
`FIXTURE_VERIFIED_LIVE_UNVERIFIED` implementation evidence,
`NORMALIZED_OBSERVATION_DIFFERENCE` fact basis, and all runtime/receipt/native
association/change-attribution statuses as `NOT_EVALUATED`. GA4 carries
`MATCHING_RECORDED_KEY_MARKERS` only when both retained inventories include the
same marker; different markers reject comparison before key access or files.
Legacy GA4 and GTM retain `NOT_EVIDENCED`; OpenAI retains `NOT_APPLICABLE`.
The marker itself stays out of the comparison document. Historical matching
markers do not prove current remote state or grant authority. OpenAI list-response and GTM partial-coverage,
dependency, header-count, staging, and non-atomic caveats remain visible from
the two source completeness records. `OBSERVED_ABSENT_FROM_LIST` never means a
resource was deleted.

Opening or editing the artifact authorizes nothing. It sets
`execution_authority=false`, `remote_mutations=false`,
`opening_or_editing_authorizes_execution=false`, and
`execution_command=NOT_INSTALLED`. Its current remote state is
`NOT_EVALUATED_HISTORICAL_ARTIFACT`.

The shared typed writer is descriptor-relative, no-follow, private-mode,
fsynced, versioned, and deterministic. It reuses exact rendered bytes where
saved policy permits and creates a new version for a changed collision unless
the explicit request authorized replacement. It never truncates a comparison:
over-limit facts or bytes fail before any publication. A later per-format local
write failure returns a bounded partial result containing every completed
artifact SHA-256 reference and a fixed public failure code. It neither changes
the retained observations nor retries a provider operation.

## Saved completion rules

`measurement compare` remains inline-only by default. It creates an automatic
artifact only when the saved output policy is `RULES` and an enabled rule
matches the exact `diagnostics` / `completion` / `comparison` context, provider,
and source profile. `ON_REQUEST`, disabled comparison purpose, and a missing or
nonmatching rule create no file. A matching but incomplete local rule fails
with `ARTIFACT_CONFIGURATION_REQUIRED` before export-key access.

For a matching rule, the command reconstructs the complete comparison and
preflights both the redacted JSON result envelope and the worst-case artifact
receipt using the synthetic HMAC key before it reads the real sharing key or
publishes a file. One real key is then used for both inline pseudonyms and the
artifact renderer. Source drift during the final renderer reconstruction fails
closed before a writer is reached. If a later per-format local write fails, the
command returns the complete historical comparison and every completed artifact
reference with `outcome=PARTIAL` and exit status 5; it does not omit facts or
retry publication.

## Validation

Hermetic tests cover full pair reconstruction, checkpoint/fact/raw-float
forgery rejection, all three formats, HTML without scripts or links, HMAC
redaction, zero-fact GTM caveats, row and byte preflight before a real key or
writer (including exact fixed-width synthetic-HMAC size parity), deterministic
reuse, partial second-format failure, failure-preview validation, result
metadata binding, strict request version types, and no mapping-based comparison
input. Independent saved-rule tests cover default/nonmatching suppression,
exact rule matching, malformed rule rejection, full-envelope preflight before a
key or file, checkpoint drift, one-key reuse, and partial second-format output.
Tests use temporary local ledgers and fixture snapshots only; they do not
contact providers, read credentials, invoke native approval code, or mutate
remote state.

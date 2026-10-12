# Retained GA4 key-event CREATE plan artifacts

Status: `IMPLEMENTING`. This library creates deterministic local static views of
one verified retained GA4 key-event CREATE plan. It is not an OAuth, provider,
approval-host, execution, capacity, permission, browser, or processed-data
verification surface.

`adhelm.ga4_create_plan_artifacts` accepts only the closed
`Ga4KeyEventCreatePlannedAction` returned by
`load_ga4_key_event_create_plan`, plus its narrow retained provenance. It
rebuilds the action before use and binds the record digest, action digest, plan
digest, checkpoint digest, historical record time, fixture capability status,
historical-evidence status, and fixed no-authority controls. The established
saved-plan loader has no expiry or capability field: the artifact provenance
derives both from the revalidated action. A complete artifact-provenance mapping
is also accepted only when it exactly matches those derived values. Other,
partial, or extended mappings fail closed.

The sole document is `ga4_key_event_create_plan_review`. Its review comes only
from `preview_ga4_key_event_create_plan` over a reconstructed closed plan
record, with the private export key and `reviewed_at` fixed to retained
`recorded_at`. This creates a deterministic historical `as_of` view. It records
whether the plan was expired at that historical instant and always says current
validity is `NOT_EVALUATED_HISTORICAL_ARTIFACT`. The typed document requires the
same retained time in provenance, historical review, and redacted review; it
also fixes the supported GA4 semantics to `ONCE_PER_EVENT`, zero incremental
spend, one semantic-member addition, empty recovery actions,
`RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION`, and no blind retry.
Its JSON serializer reparses emitted nested content and source evidence, so a
constructed or copied typed document cannot change static, recovery, or
no-authority controls without failing before output.

The review retains all redacted critical planning facts: plan/source digests,
prospective semantic before/after states and counts, fixed target/API boundary,
contract and inventory provenance digests, declared destination relationship,
configuration and profile digest facts, observation interval, expiry, recovery
limits, and every unresolved current-state, permission, configuration-readback,
browser, realtime, processed-report, and native-conversion gate. It does not
serialize raw event or field names, property or stream IDs, profile/principal or
client references, OAuth material, native request paths or bodies, local
fingerprint/content markers, input prose, raw provider responses, or key bytes.
References use the existing HMAC opaque-reference projection. JSON, RFC 4180
CSV, and static HTML render the same lossless presentation; compact detail only
factors repeated fields and does not truncate critical review data.

The library resolves saved policy in the exact context
`plan` / `before_approval` / `plan_review` / `ga4_admin` / selected profile.
`ON_REQUEST` remains file-free. An explicit request can override `OFF` only for
that invocation. A matching incomplete rule returns
`ARTIFACT_CONFIGURATION_REQUIRED`. Generation uses the existing
component-descriptor, no-follow, private-mode, fsynced, versioned writer.
Matching bytes are reused; a changed or tampered collision receives a new
version unless replacement is explicitly authorized.

`ga4_create_plan_artifact_failure_preview` reserves the complete possible
partial result with version-9999 artifact references and the longest public
failure code before a sharing-key lookup or file write. The preflight uses an
actual allowlisted code, not padding that normal runtime output can never emit.
After a plan has been durably retained and reloaded from verified history,
`preflight_ga4_key_event_create_plan_artifact_output` renders all selected
formats using a fixed-width synthetic HMAC key before the real sharing key or
writer is opened. It checks row and byte bounds without touching the output
directory. The full artifact cannot be rendered before plan preparation's EDIT
reads because a verified retained plan is the only permitted artifact source;
the earlier planning boundary can preflight only its bounded result envelope.

`render_ga4_key_event_create_plan_artifacts` repeats closed source validation
at the document-writer handoff. It returns completed metadata for any formats
written before a local failure and normalizes public failure codes. The helper
`ga4_create_plan_artifact_failure_result` lets a caller report a safe
post-retention source/preflight failure without inventing source data. A failed
artifact never retracts a retained plan, opens a grant, extends expiry, causes a
remote call, creates an execution command, or justifies a retry.

## Validation

The focused hermetic tests cover all formats, source/action reconstruction,
opaque redaction, static HTML, historical time and expiry, lossless critical
gates, writer reuse, strict source/result controls, saved-rule and default
`ON_REQUEST` policy behavior, zero-key preflight, realistic maximum partial
failure reservation, raw numeric boolean aliases, constructed nested-document
authority bypasses, impossible counting/as-of document mutations, and no
artifact publication on preflight failure. Tests use temporary local ledger and
output directories plus fake GA4 workflow fixtures. They make no provider,
OAuth, credential-store, browser, approval-host, or mutation call.

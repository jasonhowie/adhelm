# Retained OpenAI pause-plan artifacts

Status: `IMPLEMENTING`. This document describes deterministic local artifact
work over fixture-only retained pause plans. It is not provider, approval-host,
or execution evidence.

`artifacts plan <record-digest> --input <pause-plan-artifact-render.json>`
accepts only one verified `EXECUTION_PLAN` record through
`load_openai_pause_plan`. Its closed request schema is exposed as
`pause-plan-artifact-render`; it permits only JSON, CSV, and static HTML
formats, an existing absolute local output directory, and a one-request
replacement authorization. The command does not accept a plan body, native
request, raw response, shell command, action link, arbitrary document payload,
or remote destination.

The command first validates the typed artifact request, loads and revalidates
the selected retained action from a verified checkpoint, resolves the explicit
user request against saved output settings, and sizes the longest partial result
envelope before reading the private export key or creating a file. An explicit
request can override saved `OFF` for that invocation only. Missing formats or a
safe local directory return a configuration need. The writer then uses the
existing descriptor-relative, no-follow, private-mode, fsynced, versioned
artifact path. It reuses matching rendered bytes and creates a new version for
a changed or tampered collision unless prior replacement authorization permits
atomic replacement.

The only plan document is a schema-versioned static historical review. It
revalidates the closed action again, derives `OpenAIPauseReview` from that action
with the local export key, and retains the source record/checkpoint, action and
plan digests, exact subplan digest, fixture capability status, native budget,
bid mode, schedule, active-to-paused state, expiry, zero incremental spend, and
recovery limits. Account, campaign, credential-profile, and workspace-profile
references are opaque HMAC values. Native account/campaign IDs, the private
credential-binding HMAC, raw credential, and export-key bytes are never
serialized.

The artifact's `as_of` is the retained record's local `recorded_at`, not the
wall clock when a user opens the file. This makes identical verified retained
input and rendering settings reusable. It records whether the plan was expired
at that historical instant and always labels current validity
`NOT_EVALUATED_HISTORICAL_ARTIFACT`; a static file never calls an expired plan
currently valid. `execution_authority`, `remote_mutations`, and approval from
opening or editing are all false. It contains no scripts, remote assets, or
action links.

During `plan openai-pause`, a matching saved `RULES` policy for
`plan` / `before_approval` / `plan_review` is resolved before pause-planning
credentials or provider reads. `ON_REQUEST` remains file-free by default. Once
the mandatory plan is retained, automatic artifact generation reloads that
record through `load_openai_pause_plan`; it cannot publish from an in-memory
action. Artifact failure returns `PARTIAL`, preserves the retained plan review
and its result digest, and includes every completed artifact SHA-256 reference.
The full possible partial envelope is measured before files are created, so an
output bound cannot erase a completed reference. A failed artifact does not
retract, mutate, authorize, or extend the plan.

## Validation

The hermetic regression suite covers explicit `OFF` override, all three formats,
static HTML, source/action reconstruction, opaque redaction, deterministic
reuse, tamper versioning, expiration, matching saved rules, default
`ON_REQUEST`, strict request types, unsafe destination rejection,
output-envelope preflight, partial local writes, unverified post-retention
source reload, constructed-action rejection, provenance mismatch, sanitized
internal failures, forged policy/overwrite attempts, direct-result
serialization, filename/digest-bound metadata, complete partial-result shapes,
and discovery/schema wiring. Tests use only in-memory fixture actions and
temporary local directories; they do not contact a provider, keychain, approval
host, or external CLI.

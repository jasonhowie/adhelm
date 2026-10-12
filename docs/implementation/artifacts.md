# Optional local artifacts

The artifacts module resolves optional output without changing configuration,
scheduling a job, uploading data, or creating provider authority. Its report
surface supports only NormalizedReport and AuditResult (the implemented
audit-report type). The separately typed `artifacts plan` surface supports only
a verified retained OpenAI pause action reconstructed by `load_openai_pause_plan`.
Callers supply source-record and verified-checkpoint digests that bind a file to
retained local evidence. No surface accepts arbitrary payloads, shell commands,
action links, remote destinations, or external rendering dependencies.
Generation also requires the existing private local export key. It applies the
same keyed redaction projection as evidence export, including GA4 event
dimension values and nested identifier/reference fields.

Resolution is side-effect free. A direct user task override has first priority
and can request a file even when saved mode is OFF; it is not saved. Otherwise
OFF suppresses files, ON_REQUEST requires a direct request, and RULES requires a
matching provider/profile/workflow/stage rule and enabled purpose. Missing
formats or a local output directory return a structured configuration decision.
An explicit task request without formats inherits configured saved formats when
they exist. Resolution validates the destination before a provider workflow can
continue: it must already exist, be absolute and local, and have no symlink
component.

The renderer creates only JSON, RFC 4180 CSV, and static HTML. It builds
redacted report/audit views that retain keyed opaque references and crosslinks,
source/checkpoint digests, statuses, partial/pending/unavailable facts, and
audit evidence digests. CSV text cells use the canonical reversible formula
guard; HTML escapes all text and contains no scripts, remote assets, or links.

Presentation detail has an explicit lossless contract. `standard` retains the
full redacted document. `compact` factors exactly equal fields shared by repeated
object rows into a marked `shared_fields` / `per_row_values` representation only
when this reduces canonical JSON size. Each row inherits shared fields; its own
keys take precedence. Row order, JSON types, nulls, metric grains, spend and
quality/evidence fields are preserved, with no aggregation or silent truncation.
`detailed` includes the full standard document plus a deterministic structural
field guide from its installed schema; descriptions, defaults and example values
are excluded from that guide. Every format uses the same selected presentation.
The source input digest remains independent of presentation detail, while the
renderer version and settings digest distinguish the resulting files.

Names derive from canonical redacted input and rendering-setting digests.
Matching bytes are reused. A changed/tampered collision creates a versioned
file by default. REGENERATE creates a new version even when bytes match;
replacement uses the saved authorized-replace policy unless an explicit task
override enables or disables it. An explicit task request takes precedence;
permission changes do not alter the rendering digest or defeat reuse.
Destinations must be existing absolute local directories and may not traverse
symlinks. All selected formats are rendered and checked against row and size
bounds before the first file is created. Files are created with exclusive
no-follow creation, mode 0600, file fsync, and directory fsync. Authorized
replacement writes and fsyncs a same-directory temporary file before atomic
replacement, preserving the original when rendering or temporary writing fails.

`artifacts configure` saves output preferences, `artifacts policy` previews a
saved rule decision, `artifacts report` renders a verified retained report, and
`artifacts plan` renders a verified retained OpenAI pause-plan review. The
report and plan commands validate their bounded result before generating files.
Saved report rules run at completion or meaningful-change stages. A saved
pause-plan rule runs only for `plan` / `before_approval` /
`plan_review`, is preflighted before pause-planning credentials or provider
reads, and creates a file only after mandatory plan retention has succeeded.
Default `ON_REQUEST` leaves planning file-free. An explicit `artifacts plan`
request can override saved `OFF` for that one request without persisting a
preference change.

Meaningful-change comparison excludes observation IDs/timestamps and retains
normalized query, data, quality and freshness semantics. Reuse is bound to the
actual prior retained report, its checkpoint, rendering settings and verified
file bytes. The local derived cache is authenticated with the private export key;
it does not become canonical evidence or authorization. Failures after report
or plan retention preserve the current retained record/checkpoint and any known
published artifact references. Pause-plan files are static historical reviews:
their deterministic `as_of` is the retained record's local `recorded_at`,
current validity is explicitly not evaluated, and opening or editing a file
never supplies approval or execution authority. The plan projection contains
only redacted review facts; native IDs, credential bindings, and key bytes never
enter the file or result envelope. See [pause-plan artifacts](pause-plan-artifacts.md)
for that closed projection. [Independent ultra review](artifact-ultra-review.md)
covers filesystem races, bounded partial output and lossless detail. Later
workflow stages, additional formats and cross-platform implementation remain
required.

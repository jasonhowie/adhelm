# Measurement workflow

Read this reference before reviewing GA4/GTM configuration, preparing an offline
plan for a GA4 or native conversion change, or writing a missing-signal contract.
This release applies no change and runs no browser measurement; whatever a plan
proposes is carried out by the marketer. Discover exact commands and schemas from
the installed CLI; this document is not an executable interface.

## Inspect retained configuration first

If discovery exposes `measurement catalog` and `measurement show`, inspect the
relevant retained observation before requesting another provider read. Keep its
record/snapshot digests, account identity, read interval and capability version
with your finding. Use paged rows and saved output limits; an old observation
does not establish current state or authorize an edit.

If installed, `measurement compare <before-record-digest> <after-record-digest>`
compares two retained observations from the same profile, provider and resource
scope at one verified checkpoint. Discover `measurement-comparison-request` and
`measurement-comparison` schemas. Keep both source digests, comparison digest,
outer observation interval and every source coverage limitation with the result.
`OBSERVED_ABSENT_FROM_LIST` does not prove deletion, and a zero-fact comparison
does not prove uninspected configuration remained unchanged. Actor stays UNKNOWN
and exact change time is unavailable. Output is one complete review; if saved
row/byte bounds are too small, follow the diagnostic to increase them instead of
discarding facts. Comparing history makes no provider request or execution grant.
New GA4 pairs may report `MATCHING_RECORDED_KEY_MARKERS`. Different recorded
keys fail comparison before output; collect a new pair under one configured key
or review each historical observation separately. Legacy GA4 and GTM retain
`NOT_EVIDENCED`: a changed fingerprint alone cannot establish a remote change
because the key may have rotated. Matching historical markers prove no current
access, duplicate absence, or execution authority.

Where installed, optional `artifacts measurement-comparison` uses the two source
record digests and a typed `measurement-comparison-artifact-render` request.
Saved diagnostics/completion/comparison rules also apply to `measurement compare`;
default settings create no files. Both paths rebuild the pair at its exact
checkpoint and preserve every fact and uncertainty in JSON/CSV/HTML. Respect
configured formats, directory, reuse, replacement and output bounds. A partial
artifact result retains completed references and does not alter history or
authorize execution. Opening or editing a file does not supply authorization.

Where installed, `measurement ga4-admin` uses an explicit GA4 Admin read profile
and expected web stream; `measurement openai-conversions` uses an explicit
OpenAI Ads read profile. Discover the command interface and profile schema
before invocation. Both return retained configuration evidence, not event
payloads. Names may be represented by fingerprints; do not guess their original
values or use a fingerprint to infer business semantics.

For the separately installed `measurement ga4-admin-edit` group, use `catalog`
and `show <record-digest>` to inspect retained EDIT-profile observations. An
explicit `read --profile <edit-profile> --stream-id <stream-id>` validates local
configuration before private key or credential access, then reads property,
selected web stream and bounded key-event pages under one credential snapshot.
CI use requires explicit enablement. This history has its own observation kind;
do not mix it with READ-role inventory or assume the generic compare command
accepts it. Keep the interval, non-atomic consistency and fixture/live boundary.
The complete observed count is compared with the documented Standard cap;
360 or unavailable service level has no inferred cap. Capacity can change after
the read and is never a reservation. Public history review does not authenticate
content markers, establish granted scopes, prove create permission or authorize
writes. Missing name fingerprints are not license to infer event semantics.

Where installed, `measurement gtm <profile> <container-id>` uses a GTM READ
profile and explicit expected web container; `schema measurement-gtm-read`
describes its versioned input. Local validation precedes OS sharing-key and
credential access. Explicit CI credentials require `--ci-enabled`. Its retained
configuration evidence covers account/container identity, live/latest version
counts, opaque code fingerprints, and accessible workspace metadata. It does
not inspect workspace contents or establish complete dependencies, staging,
compiler, browser or live conformance. Preserve UNKNOWN feature flags and
missing header-counter fields; never convert their absence to false or zero.
Use catalog/show pagination and saved output bounds. If mandatory review plus
one row exceeds the bound, follow the diagnostic to increase it; do not discard
uncertainty metadata. An unconfigured optional GTM profile returns
`NOT_CONFIGURED_OPTIONAL`; continue unrelated ad/GA4 work.

Preserve `NOT_EVALUATED` for runtime firing, processed receipt, native
optimization association and change attribution. A setting's existence, a
campaign association count or successful pagination cannot fill those gaps.
Respect explicit fixture/live-conformance limits. On
`MEASUREMENT_RETENTION_UNVERIFIED`, inspect history and catalog before repeating
the read. Do not turn an uncertain local receipt into a success claim.

## Plan offline

If installed, `conversion register --input <definition.json>` records explicit
customer conversion meaning under `schema conversion-definition`. Use retained
`conversion catalog` and `conversion show` versions before proposing another
definition. Do not invent qualification, source, exclusions, counting/value,
consent or destinations from labels or clicks. Missing meaning requires a
customer definition; the CLI reports `USER_DEFINITION_REQUIRED` when required
meaning is absent. A later version must extend the latest content digest with
the next version and a later effective time. Registration records declarations
only: it does not prove source semantics, configure events, validate legal policy,
or grant authority. Keep the version/content digest with proposed mappings;
mapping and provider execution still require independently installed capabilities.
On `CONVERSION_RETENTION_UNVERIFIED`, inspect history/catalog before retrying.
Whole definition fields page under saved output limits; preserve all fields
and required uncertainty when adjusting the review bound.

If installed, `measurement key-event-candidate compile` uses the closed
`measurement-ga4-key-event-candidate-input` schema and an exact retained
conversion definition. Resolve source digests through the catalog and customer
declaration; do not ask the marketer to copy digests. This narrow offline
binding accepts Boolean fields, each qualifying occurrence and no monetary
value. It rejects unsupported counting, value and field mappings. It never
proves that a website emits the declared event or that the declaration has taken
effect. Optional retention requires the latest preceding definition; historical
catalog/show remain available after later versions. Full show/preview preserves
all required live planning checks without native request bodies or source text.
The candidate cannot be used as a plan, grant, native conversion or receipt proof.

Where installed, `plan ga4-key-event create --input <request.json>` accepts
`schema ga4-key-event-create-plan-request`: the configured GA4 Admin OAuth EDIT
profile, retained candidate digest, full fresh candidate input and a 60–900 second
lifetime. It recompiles the source against the latest effective retained
conversion definition, reads a new complete EDIT inventory under one protected
OAuth snapshot, and binds configuration, identity, source records, checkpoint,
before/after semantic state and expiry into the plan. Static or CI credentials
do not support this planning path. It performs fixed GETs and local retention;
it sends no CREATE request and proves no Google write permission.

Use `plan ga4-key-event catalog` to select retained metadata, and follow the
create receipt's exact `plan ga4-key-event show <record-digest>` reference for
the full redacted review. The receipt is not the review. Preserve zero ad spend,
immediate live GA4 configuration with no draft or historical backfill, absent
native conversion linkage, unevaluated browser/processed receipt, and the
reconcile/export/stop recovery disposition without remote compensation. The
declared destination label does not prove its mapping to the execution profile.
Historical review remains available after expiry, without current-state or
authorization claims. This CLI reports `execution_command: UNRELEASED`:
`execute ga4-key-event-create` is registered and refused with
`WRITES_NOT_RELEASED`, and the provider must not be invoked separately.

Plan output is checked against saved byte limits before provider reads. A full
show fails if required review facts cannot fit; increase the bound instead of
discarding evidence. When installed, `artifacts ga4-key-event-plan <record-digest>
--input <request.json>` uses `schema ga4-key-event-create-plan-artifact-render`
to create JSON, CSV or static HTML from one verified retained plan. Resolve the
digest through catalog/history for the marketer. Explicit requests can override
saved artifact `OFF` for that task. Matching saved `RULES` for `plan` /
`before_approval` / `plan_review` / `ga4_admin` create the same artifacts during
planning; default `ON_REQUEST` creates no files.

The artifact preserves the historical record time, exact source/plan/checkpoint
digests, redacted changes and every review gate. Its static historical status
never proves present validity, current provider state, authorization or
execution. It contains no native account/event identifiers, private credential
bindings, raw source declarations or executable approval links. Opening or
editing it cannot refresh or apply the plan. Use the trusted local approval
review for the exact account identity and declared change when that approval
path is installed.

Before planning, the CLI reserves bounded output for the retained-plan receipt
and possible artifact references. After verified retention it checks the full
artifact content before file writing. If artifact generation returns `PARTIAL`,
keep the retained plan and completed artifact references; request the missing
files from that plan instead of repeating planning. On
`GA4_CREATE_RETENTION_UNVERIFIED` or `GA4_CREATE_PLAN_RECEIPT_UNVERIFIED`, inspect
catalog/history before retrying; a durable plan may exist despite the failed
local receipt.

A plan review has the identity, exact before/after changes, side effects, spend
where relevant, expiry, and recovery that the verified canonical rendering
carries; resolve digests through the CLI without asking the marketer to type
one. Compact review and enabled artifacts differ only in presentation; neither
changes what a plan authorizes, which in this release is nothing. The design
contract for a later release is a one-use authorization bound to the exact plan
digest, a drift check and durable `APPLY_STARTED` history before any egress. This
build refuses every step from execution onward.

To carry a plan out, the marketer makes the change by hand. Turn the plan review
into the manual step described in the skill: object, setting, current and
intended value, the declared conversion meaning, evidence, and what to check
afterward. Then verify by re-reading, not by assuming.

GA4 Data access is read-only. A GA4 Admin change the marketer makes by hand is
prospective and cannot backfill history. Key-event names are immutable where the
provider says so, so a rename means a new key event and an explicit decision about
the old one. Native provider conversion linkage is a separate provider-side
setting; a GA4 key event is not one.

## GTM in this release

GTM is optional per customer. If unconfigured, retain
`NOT_CONFIGURED_OPTIONAL` and continue independent ad/GA4 work.

This release reads GTM: the fixed inventory (`measurement gtm`, with
`--full-inventory` for the complete one), and from one retained inventory the
dependency graph and read-only audit (`measurement gtm-graph`). It creates no
workspace, version or publication and runs no browser test. There is no command
for staging or publishing; do not improvise one with the GTM API or a browser. If
the marketer wants a GTM change, recommend it from the graph and let them make
it in GTM themselves, and say the CLI has not tested it. The graph reports the
workspace stage gate as not evaluated unless a workspace allowance is declared;
do not guess one.

Recommend deleting a resource only when the dependency engine proved it
`PROVEN_UNREFERENCED` across all applicable resources and pending workspaces.
Age, naming, paused status and apparent inactivity are not proof, and
unknown/custom-code dependencies do not establish it.

Static checks and `quick_preview` do not prove browser firing, and nothing in
this release does. Do not describe an event as firing, consented or received
from configuration alone.

## Verify each layer honestly

Distinguish configuration verification, browser/network behavior, GA4 Realtime,
and processed-report verification. This release observes configuration only.
Realtime success, if the marketer reports it, does not establish processed
receipt or correct counting. Report missing, duplicate, pending, thresholded,
timed-out, partial, and ambiguous results without substituting zero or success.

A change made by hand, whether to GA4, GTM or a provider, can be a non-atomic
set of steps. Ask which were done, re-read each surface, and report each layer
separately rather than as one success. Restoring configuration cannot unsend
collected data, and rolling a change back is likewise the marketer's step.

## Missing site signals

When no safe site signal exists, label the gap `NEEDS_SITE_INSTRUMENTATION` and
write out the required typed event-contract data using user-declared meaning,
counting, allowlisted nonsensitive fields, consent, and destinations. Do not
simulate a lead from a button name, scrape values, synthesize arbitrary
JavaScript, or claim instrumentation was installed. This release's CLI does not
emit that status: it is your finding label for a proposed contract, never an
observed CLI diagnostic, and the marketer's site owner makes the change.

Render/export a human handoff file only when the request or configured output
rule calls for it. Required canonical event-contract data remains available
without generating an optional document, spreadsheet, or HTML presentation.

## CRM and native conversion evidence

CRM attribution is not available in this release (discovery reports HubSpot and
Salesforce as not implemented), so qualified outcomes, pipeline, and revenue are
absent unless the marketer supplies approved aggregates, with their own privacy
minimums, campaign mappings, and attribution/counting windows; never request raw
CRM records. Native conversion resources, campaign optimization associations, and
event/tag setup are manual marketer steps that a later read verifies; each needs
its own declared signal, consent, and data contract in the plan. Configuration,
browser behavior, native receipt, processed reports, and optimization readiness
are separate evidence states. GA4 success cannot fill a missing native proof. Unsafe identifier
requirements and unavailable provider capabilities fail explicitly.

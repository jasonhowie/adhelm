# GA4 Admin EDIT inventory workflow

Status: IMPLEMENTING; synthetic HTTP, configuration and local-history evidence
only. No live Google request, credential-store enrollment or mutation was run.

The installed `measurement ga4-admin-edit` group offers an explicit `read
--profile <profile> --stream-id <stream>` command, plus `catalog` and `show
<record-digest>`. Discover the closed `measurement-ga4-edit-read` and
`measurement-ga4-edit-observation` schemas before invoking it. Explicit CI
credential use requires `--ci-enabled`. The marketer's selected agent resolves
record digests from the catalog; the marketer need not type them.

The workflow requires exactly one configured GA4 Admin EDIT profile with
`analytics.edit`, a native numeric property, expected currency/timezone and a
selected numeric web stream. It validates inputs and configuration before key
or credential access, captures one credential, and issues only fixed projected
property, selected-stream and bounded key-event collection GETs through the
Admin host. No provider response or event name is retained. Config changes
before authentication, after reads and before retention reject the operation.
Cooperating configuration writers are locked only around local key access and
retention; no network call holds those locks.

The reader returns sorted normalized key-event facts and capacity observations.
For observed Standard properties it compares the complete count with the
documented cap of 30. All observed events count conservatively. Other service
levels retain an explicit cap-not-evidenced result. This is not a reservation;
provider state can change after any read. Successful GETs cannot prove that the
token has a particular granted scope or that the principal can create a resource.

A dedicated `GA4_ADMIN_EDIT_INVENTORY_OBSERVATION` record binds the exact
inventory digest, configuration/profile digests and read interval. Its envelope
requires the matching GA4 property, UNKNOWN source, no run ID and no inferred
external change time. The checks apply both at append and while scanning physical
history. The old READ observation shape and comparison API remain separate.
The workflow and retention helper authenticate the inventory's content marker
under the current local fingerprint key before retaining it. This is local
keyed integrity, not provider-origin authentication or execution authority.

Catalog and show read verified local history without provider access. Show
states that content authentication was not checked; the ordinary ledger
checkpoint does not become a current-key authenticity claim. Public rows omit
name/configuration/content/key markers and native IDs; references are redacted
using the local sharing key. Full review preserves capacity, read interval,
non-atomic consistency, runtime/processed-data gaps and fixture/live limitations.
Rows paginate under saved bounds. If mandatory context and one row cannot fit,
the command fails before sharing-key access and explains which saved limit to
increase. The compact read receipt and catalog fit the supported 1024-byte minimum.

An OS error after the completed read yields `MEASUREMENT_RETENTION_UNVERIFIED`;
inspect history verification and the dedicated catalog before repeating because
an observation may already be durable. An incomplete provider read is never
retained as a complete inventory. No command creates a plan, grant, key event,
browser test, processed receipt or native optimization association.

Validation includes end-to-end mocked HTTP -> complete inventory -> sealed local
record -> catalog/show, empty inventory, separate READ history, small output
bounds, wrong-key rejection and preflight config mismatch. See the independent
[ultra workflow review](ga4-admin-edit-workflow-ultra-review.md) and
[inventory review](ga4-admin-edit-inventory-ultra-review.md) for adversarial cases.

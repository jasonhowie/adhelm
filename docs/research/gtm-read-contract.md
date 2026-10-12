# GTM v2 fixed read inventory contract

This research record governs the bounded GTM read adapter. It is fixture-tested only and is not live capability evidence. The adapter is library-only: no installed `adhelm` CLI command exposes this GTM inventory yet.

## Primary API contract

Google's [authorization reference](https://developers.google.com/tag-platform/tag-manager/api/v2/authorization) requires OAuth 2.0 and documents `https://www.googleapis.com/auth/tagmanager.readonly` for viewing containers. The adapter requests and accepts only that declared read scope; it exposes no mutation route.

The implemented fixed routes are all `GET` requests to `https://tagmanager.googleapis.com/tagmanager/v2`:

| Read | Fixed route | Use |
| --- | --- | --- |
| live version | `accounts/{account}/containers/{container}/versions:live` | Published `ContainerVersion` response |
| latest header | `accounts/{account}/containers/{container}/version_headers:latest` | Current latest version identity |
| latest version | `accounts/{account}/containers/{container}/versions/{version}` | `ContainerVersion` response for the header identity |
| workspaces | `accounts/{account}/containers/{container}/workspaces` | Every accessible workspace, with bounded pagination |

The [live version method](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions/live), [latest header method](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.version_headers/latest), [container version resource](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions), and [workspace list method](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces/list) define these response shapes. GTM has no atomic multi-read snapshot. The adapter rereads the latest header and full live-version response before returning, rejects a changed view, and labels a successful result `SEQUENTIALLY_REREAD_NONATOMIC`. It does not reread the latest-version body or the workspace pages, so a successful read is not a transactionally complete point-in-time inventory.

## Output and safety boundary

`GtmReadClient.inventory(identity=...)` requires both deterministic local-reference binding and an injected session verifier before it calls the token getter or HTTP transport. A caller declaration alone is not identity proof.

The result contains local normalized account/container/version/workspace references, HMAC-SHA-256 configuration fingerprints, resource counts, and opaque markers for custom HTML tags and custom templates. The HMAC key is caller-owned and never appears in the output. Provider fingerprints, names, descriptions, UI URLs, domains, template source, custom HTML, parameter values, and customer code remain transient in memory and never enter normalized output.

Every non-null count supplied by the latest-version header must be a bounded
decimal integer and equal the corresponding latest-version resource array. A
mismatch or malformed supplied count fails `INVENTORY_INCONSISTENT` or
`PROVIDER_RESPONSE_INVALID`, respectively, even when repeated header reads are
stable. Google's [generated Java client](https://developers.google.com/resources/api-libraries/documentation/tagmanager/v2/java/latest/com/google/api/services/tagmanager/model/ContainerVersionHeader.html)
models these header counters as nullable, so an omitted or JSON-null counter is
never normalized to zero. The result instead sets
`latest_header_count_status.status` to `HEADER_COUNTS_UNAVAILABLE` and names
each unavailable field in `missing_counter_fields`. Only all eight present,
matching counts receive `ALL_HEADER_COUNTS_MATCH`. Neither status establishes
independent completeness, atomicity, or dependency proof.

Workspaces are enumerated with a 20-page/1,000-item bound applied before workspace-item normalization. Their resources are intentionally `NOT_INSPECTED`; dependency status is `UNKNOWN_NOT_INSPECTED`. A live/latest mismatch remains visible as distinct local version references and still returns `NOT_EVALUATED`; it is not staging proof. The reader therefore does not prove staging eligibility, a dependency graph, resource-deletion safety, synchronization, compiler status, preview, browser firing, consent, GA4 receipt, or publishing readiness.

The documented `ContainerVersion` outer schema and nested `Container` metadata schema are closed. Raw configuration beneath documented tag, trigger, template, and other version-resource envelopes remains opaque: it is bounded, HMACed where needed, never output, and does not become a dependency graph. A nested resource that declares an account/container pair or a path must bind to the configured parent. Missing `deleted` is normalized as the documented false default; explicit `true` fails closed.

Malformed JSON, unknown outer or container-metadata fields, parent/path mismatches, deleted or inconsistent version identities, duplicate workspaces, incomplete/repeated pagination, oversized collections, access failures, and a changing sequential view fail closed without returning a partial inventory. Pagination is capped at 20 pages and 1,000 workspaces. Each returned version's declared resource arrays are capped at 4,000 total resources.

No workspace creation, workspace synchronization, conflict resolution, quick preview, version creation, `set_latest`, publish, restore, generic request API, browser work, provider OAuth flow, credential lookup, or external CLI is part of this adapter.

## Complete fixed inventory routes (added 2026-09-09)

The M01.1 complete inventory in `src/adhelm/gtm_inventory.py` adds the documented
per-workspace and container collection reads below. Every row was fetched from
the official Tag Manager API v2 REST reference on **2026-09-09**; each linked
page is the exact source for that row's path, response array field, and page
token surface.

| Collection | Documented list method | Response array | `nextPageToken` | `pageToken` request parameter |
| --- | --- | --- | --- | --- |
| tags | [accounts.containers.workspaces.tags.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.tags/list) | `tag` | yes | yes |
| triggers | [accounts.containers.workspaces.triggers.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.triggers/list) | `trigger` | yes | yes |
| variables | [accounts.containers.workspaces.variables.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.variables/list) | `variable` | yes | yes |
| folders | [accounts.containers.workspaces.folders.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.folders/list) | `folder` | yes | yes |
| built_in_variables | [accounts.containers.workspaces.built_in_variables.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.built_in_variables/list) | `builtInVariable` | yes | yes |
| templates | [accounts.containers.workspaces.templates.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.templates/list) | `template` | yes | yes |
| zones | [accounts.containers.workspaces.zones.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.zones/list) | `zone` | yes | yes |
| clients | [accounts.containers.workspaces.clients.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.clients/list) | `client` | yes | yes |
| transformations | [accounts.containers.workspaces.transformations.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.transformations/list) | `transformation` | yes | yes |
| gtag_configs | [accounts.containers.workspaces.gtag_config.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.gtag_config/list) | `gtagConfig` | yes | yes |
| destinations | [accounts.containers.destinations.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.destinations/list) | `destination` | yes | **no** |
| environments | [accounts.containers.environments.list](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.environments/list) | `environment` | yes | yes |

Two documented asymmetries drive behavior. `destinations` is parented by
`accounts/{account}/containers/{container}`, not by a workspace, and its
documented request surface has no `pageToken` query parameter even though its
response documents `nextPageToken`. The reader therefore issues exactly one
destinations request per container and records
`INCOMPLETE_UNDOCUMENTED_PAGE_TOKEN` when a continuation token appears, instead
of claiming completeness or inventing an undocumented parameter. Workspace and
container-version scopes record `destinations` as `NOT_DOCUMENTED` and never
call it; the documented
[ContainerVersion](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions)
resource embeds its resources under `tag`, `trigger`, `variable`, `folder`,
`builtInVariable`, `customTemplate`, `zone`, `client`, `transformation`, and
`gtagConfig`, and documents no destination array.

The path segments `built_in_variables` and `gtag_config` use underscores while
their response fields are `builtInVariable` and `gtagConfig`; `templates` uses
the response field `template` while the container version embeds the same
resources under `customTemplate`.

### Environments (added 2026-09-09)

`accounts.containers.environments.list` was fetched from the official v2
reference on **2026-09-09** at
<https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.environments/list>.
It is parented by `accounts/{account}/containers/{container}`, returns the
array `environment[]`, and — unlike `destinations` — documents **both** a
`pageToken` request parameter and a `nextPageToken` response field, so the
reader consumes its pages under the same 20-page/4,000-item bounds as the
workspace collections. `docs/measurement-control.md` lists environments in the
GTM Web inventory contract, so the collection is read rather than statused
`NOT_READ_BY_DESIGN`.

The [`Environment`](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.environments)
resource (same fetch date) documents `path`, `accountId`, `containerId`,
`environmentId`, `name`, `description`, `type` (`user`, `live`, `latest`,
`workspace`), `fingerprint`, `enableDebug`, `url`, `authorizationCode`,
`authorizationTimestamp`, `tagManagerUrl`, and the union `containerVersionId` /
`workspaceId`. The inventory keys an environment by `environmentId` and keeps
only the fields every other resource keeps: scope, collection, references,
`type`, bounded `name`, keyed digests, and the keyed configuration fingerprint.
`authorizationCode`, `url`, `tagManagerUrl` and `description` are never
normalized, so an environment's preview URL and authorization code exist only
transiently in memory.

### Workspace pending changes (added 2026-09-09)

`accounts.containers.workspaces.getStatus` was fetched on **2026-09-09** at
<https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces/getStatus>.
It is `GET {workspace}/status`, takes no documented request parameter, and
returns a closed `GetWorkspaceStatusResponse` with `workspaceChange[]` (an
array of [`Entity`](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/Entity),
each a `changeStatus` of `changeStatusUnspecified` / `none` / `added` /
`deleted` / `updated` plus one embedded resource body) and `mergeConflict[]`.
The shape is documented and stable, so `docs/measurement-control.md`'s "every
accessible workspace and its pending changes" is satisfied by reading it rather
than by an explicit `NOT_READ` status.

Only the two array lengths are retained, as `counts.workspace_pending_changes`
and `counts.workspace_merge_conflicts` plus a `workspace_changes` coverage
entry. A `workspaceChange` entry embeds a whole changed tag, trigger, or
variable, including custom HTML, so none of that body is normalized, digested,
or persisted: doing so would duplicate customer configuration into the
observation through an undocumented second path. An unsupported response field
fails `PROVIDER_RESPONSE_INVALID`.

## Resource fields the inventory relies on (fetched 2026-09-09)

The reference-resolution and classification rules use only these documented
fields, from the resource pages linked above and the shared
[Parameter](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/Parameter)
type:

- `Tag`: `tagId`, `name`, `type`, `parameter`, `fingerprint`, `firingTriggerId`,
  `blockingTriggerId`, `setupTag`, `teardownTag`, `parentFolderId`, `paused`,
  `consentSettings`, `path`.
- `Trigger`: `triggerId`, `name`, `type`, `filter`, `customEventFilter`,
  `autoEventFilter`, `parameter`, `fingerprint`, `parentFolderId`, `path`.
  `filter`, `customEventFilter` and `autoEventFilter` are arrays of the shared
  `Condition` type, each carrying its own `parameter` array. Those values are
  as free-form as a tag's own parameters, so custom-code classification scans
  them too rather than only the top-level `parameter` tree.
- `Variable`: `variableId`, `name`, `type`, `parameter`, `fingerprint`,
  `parentFolderId`, `enablingTriggerId`, `disablingTriggerId`, `path`.
- `Folder`: `folderId`, `name`, `fingerprint`, `path`.
- `BuiltInVariable`: `type`, `name`, `path`. The built-in variable list resource
  page states it has no persistent data of its own; the schema is documented on
  the container version page.
- `CustomTemplate`: `templateId`, `name`, `fingerprint`, `templateData`,
  `galleryReference`.
- `Zone`: `zoneId`, `name`, `fingerprint`, `childContainer` (array of
  `publicId`/`nickname`), `boundary` (`condition`,
  `customEvaluationTriggerId`), `typeRestriction` (`enable`,
  `whitelistedTypeId`).
- `Client`: `clientId`, `name`, `type`, `parameter`, `fingerprint`, `priority`
  (an integer here, unlike `Tag.priority`, which is a `Parameter`).
- `Transformation`: `transformationId`, `name`, `type`, `parameter`,
  `fingerprint`.
- `Destination`: `destinationLinkId`, `destinationId`, `name`, `fingerprint`.
  The inventory keys a destination by `destinationLinkId`.
- `GtagConfig`: `gtagConfigId`, `type`, `parameter`, `fingerprint`. It documents
  no `name`.
- `Parameter`: `type`, `key`, `value`, `list`, `map`, `isWeakReference`. The
  documented `type` enum is `typeUnspecified`, `template`, `integer`, `boolean`,
  `list`, `map`, `triggerReference`, and `tagReference`. `triggerReference`
  carries a trigger **id**; `tagReference` carries a tag **name**. Both become
  raw edges.
- `SetupTag` (`tagName`, `stopOnSetupFailure`) and `TeardownTag` (`tagName`,
  `stopTeardownOnFailure`) reference their sequenced tag by name.
- `ConsentSetting`: `consentStatus` with documented values `notSet`,
  `notNeeded`, `needed`, plus `consentType` as a `Parameter`. An undocumented
  status value is recorded as `UNDOCUMENTED_VALUE` rather than normalized.

### UNVERIFIED

- The `cvt_<containerId>_<templateId>` form of a custom-template instance's
  provider `type` is observed behavior, not documented in the v2 reference. An
  unparsable type stays `UNRESOLVED_MALFORMED` and no template edge is invented.
- The internal fields of the shared `Condition` and `FormatValue` sub-objects
  were not fetched. The reader treats them as opaque bounded structures and
  only scans their strings for `{{ }}` references.
- The full `BuiltInVariableType`, `EventType`, and `TagFiringOption` enum member
  lists were not fetched. No enum membership is asserted for those fields.
- The GTM UI's own pagination and display behavior is not documented here, so
  none of these routes constitutes UI reconciliation evidence.

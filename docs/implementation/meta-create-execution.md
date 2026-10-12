# One paused Meta Ads single-image hierarchy CREATE, as five journaled requests

Last updated: 2026-10-10

This is P04-MA. It registers the second `ChildEgressAdapter` in the repository on
the two provider-neutral registries S3 and S4 created, for the exact pair
`("meta_ads", CREATE)`. It adds **no command, no approval kind, no credential, no
scope, and no live evidence**. `plan execution create --provider meta_ads
--operation CREATE` compiles and retains a plan; `execute plan <digest>` refuses
at the uninstalled approval bridge in every ordinary build, before the adapter
factory opens and before any credential is read. ADR 0013's release-policy gate
(ticket O3.2) will refuse it earlier still in a `0.1.0` build; it is not part of
this ticket.

The compiled candidate is the offline Meta compiler's
([`meta-compiler-contract.md`](../research/meta-compiler-contract.md)): one
`OUTCOME_TRAFFIC` / `AUCTION` campaign with an exact lifetime budget, one
`LINK_CLICKS` ad set targeting US Facebook Feed, one standalone single-image link
creative and one ad, all paused at every level that can be paused.

## Why this operation is five requests

Meta documents no atomic hierarchy mutation, and an ad creative cannot be created
without an image `hash` that only an upload returns. One approved outcome is
therefore five dependent requests, in this order, all under
`https://graph.facebook.com/v26.0/act_{id}`:

```text
1  upload-image     POST /adimages      -> {"images": {"creative.png": {"hash": ...}}}
2  create-campaign  POST /campaigns     -> {"id": "..."}
3  create-ad-set    POST /adsets        -> {"id": "..."}   (needs campaign_id from 2)
4  create-creative  POST /adcreatives   -> {"id": "..."}   (needs the hash from 1)
5  create-ad        POST /ads           -> {"id": "..."}   (needs adset_id from 3, creative from 4)
```

`SubPlan.expected_child_egress_count` is `5`, well inside the kernel's
`MAX_CHILD_EGRESS_COUNT` (32). Each child appends and fsyncs
`CHILD_APPLY_STARTED`, receives a one-use permit bound to
`(plan_digest, subplan_digest, attempt_ref, child_index)`, consumes that permit on
the statement immediately before its POST, parses one closed acknowledgement
subset, and reports `CONFIRMED` with evidence or nothing at all. Labels are step
names, never resource names. The compiler fixes the hierarchy at one of each
entity, so the child plan is a constant rather than a computation.

## Per-claim wire-fact provenance

This build made no provider or documentation request. The only primary-source
check available offline is a **local copy of the official generated SDK, v25.0.2**
(`facebook_business`), which is one major version behind the v26.0.0 the compiler
contract pins. Claims it supports are marked `SDK-CORROBORATED`: the field names
and enums exist in a generated schema, which is evidence of a documented surface
and no evidence of behavior. The v26.0.0 pin, the host and every response shape
come from the original author's 2026-09-09 research and were **not** re-verified
in this session; they are marked `RECORDED 2026-09-09`. Rows marked `UNVERIFIED`
are local narrowings or assumptions, not documented facts. Before any live pilot
each `RECORDED` and `UNVERIFIED` row needs a fresh primary-source check.

| Claim | Status |
| --- | --- |
| Host `graph.facebook.com`, version prefix `v26.0`, `Authorization: Bearer` for the subject token | `RECORDED 2026-09-09` (same host, version and header the shipped read and pause paths use) |
| `POST /act_{id}/campaigns` takes `name`, `objective`, `buying_type`, `special_ad_categories`, `lifetime_budget`, `start_time`, `stop_time`, `status` | `SDK-CORROBORATED` (v25.0.2 `AdAccount.create_campaign`) |
| `POST /act_{id}/adsets` takes `campaign_id`, `name`, `status`, `billing_event`, `optimization_goal`, `bid_strategy`, `targeting` | `SDK-CORROBORATED` (v25.0.2 `AdAccount.create_ad_set`) |
| `POST /act_{id}/adcreatives` takes `name`, `object_story_spec`; link data carries `link`, `message`, `name`, `image_hash`, `call_to_action` | `SDK-CORROBORATED` (v25.0.2 `AdAccount.create_ad_creative`, `AdCreativeLinkData`) |
| `POST /act_{id}/ads` takes `name`, `adset_id`, `creative`, `status` | `SDK-CORROBORATED` (v25.0.2 `AdAccount.create_ad`) |
| `POST /act_{id}/adimages` accepts a binary file upload, and `GET /act_{id}/adimages` accepts a `hashes` list; `AdImage` carries `hash`, `name`, `width`, `height`, `status` with `ACTIVE`/`DELETED`/`INTERNAL` | `SDK-CORROBORATED` (v25.0.2 `AdAccount.create_ad_image` with `allow_file_upload`, `get_ad_images`, `AdImage`) |
| The multipart field name for the binary image is `filename` | `UNVERIFIED`. The SDK types `AdImage.filename` as `file`; the long-documented curl form is `-F filename=@...`. The Marketing API reference documents a base64 `bytes` field as well. This build uses `filename` because the shared `SafeHttpClient.upload_image` seam can express exactly one fixed-grammar part |
| The upload response is `{"images": {"<uploaded file name>": {"hash": ...}}}` and may add `url`, `url_128`, `url_256`, dimensions, `name`, `id` | `RECORDED 2026-09-09`. The acknowledgement parser requires the single neutral file name this route sent and keeps only the `hash` |
| The image `hash` is exactly 32 lowercase hexadecimal characters | `UNVERIFIED`, a local narrowing. Meta documents `hash` as an opaque string with no grammar; every documented example is 32 lowercase hexadecimal characters. The closed grammar is what lets planning reserve the creative form's exact length (see "The creative form is sized before the upload"). A response carrying any other hash fails the acknowledgement as ambiguous, which is the safe failure: nothing after it is sent |
| A create response is `{"id": "<decimal>"}` | `RECORDED 2026-09-09`. The Marketing API return-type struct also names `success`; this build refuses a `success`-bearing response as ambiguous rather than treating it as equivalent |
| `campaign`, `adset`, `ad` nodes expose `configured_status` and `effective_status`; the creative node exposes `image_hash` | `SDK-CORROBORATED` for the field names. **Whether a creative made with `object_story_spec` returns a top-level `image_hash` is `UNVERIFIED`**; the alternative, reading `object_story_spec`, would pull ad copy and the Page ID into this process, which the readback deliberately avoids. If a live read returns no `image_hash`, the image association stays `NOT_PROVEN` and the attempt stays ambiguous, which is the safe failure |
| **No** idempotency key, request identifier or settlement resource is documented for a Graph write | `RECORDED 2026-09-09` (an evidence gap, not proof none exists) |
| A campaign with a lifetime budget needs no ad-set budget, and the ad-set `bid_strategy` is accepted beside a campaign-level budget | `UNVERIFIED`. This is the offline compiler's shape. Graph may require the bid strategy at the campaign level when the campaign owns the budget; a live validation must settle it before any pilot |
| Graph compares campaign names case-insensitively, or not | `UNVERIFIED`, undocumented; a near match is refused rather than assumed distinct |
| `GET /act_{id}/campaigns` with no `effective_status` filter includes archived and deleted campaigns in its default listing | `UNVERIFIED`. The collision check and the post-create probe read this edge exactly as requested and compare names locally. If the default listing omits archived or deleted campaigns, a name held only by one is invisible, so "no campaign with this name" means *no campaign this edge lists* with it. Live-pilot check |
| A campaign created a moment ago is visible on the immediate `GET /act_{id}/campaigns` list read | `UNVERIFIED`. This is **load-bearing**: the happy-path readback refuses unless the probe lists exactly the campaign this attempt created, so a list that lags the create leaves a correct hierarchy unverified (ambiguous, never retried). It also decides whether a lost campaign response is later seen as an unattributable name. When the list lags an acknowledged create, the record is `ACKNOWLEDGED_CAMPAIGN_NOT_LISTED`, which states only that the probe did not list the campaign. Live-pilot check |
| The creative node can be read for `id`, `name` and `image_hash` alone, and a creative made with `object_story_spec` reports a top-level `image_hash` | `UNVERIFIED` for the second half (see the node-field row above). Nothing copy-bearing is read, so the call to action, format and presence flags are *not* part of what a readback proves |
| The PNG/JPEG allowlist and the 5 MiB upload bound | `UNVERIFIED` local narrowings; Meta documents no media allowlist or size bound for the upload that this build relies on |

**Because no create response schema beyond the above is verified**, the
acknowledgement parsers accept one closed subset per kind and keep only the
identifier (or the image hash). A response carrying any other field fails the
acknowledgement. After a consumed permit that is ambiguous, never a retry. The
readback, not the acknowledgement, is what proves what was created.

## The semantic before/after

Meta assigns every native identifier at creation time, so `before` and `after`
are **semantic hierarchy fingerprints** from `providers/_semantic_state.py`:
locally keyed HMACs over request-controllable names and configuration only.

* `before` is the empty member set under one `(ad account, campaign name)`
  binding: the local statement "no campaign with this name in this ad account".
* `after` is the exact predicted member set: one `CAMPAIGN` (objective, buying
  type, lifetime budget in minor units, schedule as epoch seconds, `PAUSED`), one
  `AD_SET` (billing event, optimization goal, bid strategy, countries, platforms,
  positions, `PAUSED`), one `CREATIVE` (its name, the creative asset content
  digest, and the explicit not-read sentinel for every copy-bearing field) and
  one `AD` (`PAUSED`, the creative association).

The schedule is compared as an instant, because Graph echoes a time in the
account's offset. The creative is not a pausable Meta entity, and the projection
says so (`NOT_A_PAUSABLE_META_ENTITY`) instead of inventing a paused claim for it.

**A creative match proves less than the plan declared, and says so.** The creative
node read is `id`, `name` and `image_hash`. The call to action, creative format,
headline, primary text, destination and Page live in `object_story_spec`, which
the readback never requests because it would pull ad copy and a Page identifier
into this process. Predicting a literal for them and "observing" the same literal
would assert configuration nobody read. The `CREATIVE` member therefore carries
`NOT_READ_BACK_COPY_BEARING_FIELD` for `call_to_action`, `creative_format`,
`destination_declared`, `headline_declared`, `page_declared` and
`primary_text_declared`, identically on the prediction and on the observation
(`creative_member_configuration` builds both). A creative match proves its name,
its image association and its ad association, and nothing about its copy or call
to action; those stay bound by the plan's candidate digest, not by the readback.

**The image association is observed, not assumed, and still not byte equality.**
Unlike OpenAI's file, Meta documents an account read of ad images by hash. The
readback therefore proves two linked facts: the account holds an `ACTIVE` image
with the exact `hash` the upload acknowledged, and the created creative references
that same `hash`. Only then does the observed creative member carry the content
digest **this attempt uploaded**. No documented route returns the stored bytes or
a digest of them, so `PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ` is
retained separately and never collapsed into the association claim. Any other
image gets the `IMAGE_ASSOCIATION_NOT_PROVEN` sentinel, which cannot equal a
digest, so the hierarchy cannot match its prediction. The ad's creative
association is proven the same way: the ad must reference the creative this
attempt created.

## The creative form is sized before the upload

The creative's `object_story_spec` carries the ad copy as `ensure_ascii` JSON, so
non-ASCII text expands to six characters per code unit, and it also carries the
image hash, which exists only after the upload. Planning must therefore reserve
the hash's length without having it. Reserving a placeholder is sound only if no
acknowledgement can be longer than the placeholder, so the hash grammar is closed
at exactly 32 lowercase hexadecimal characters (`checked_image_hash`, an
`UNVERIFIED` local narrowing; escape-free, so it cannot grow under JSON either).
Planning sizes the form with a hash from that grammar
(`META_CREATE_PLANNING_IMAGE_HASH`) and refuses
`META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE` when a form value exceeds 4,096
characters, so a candidate that passes planning has a creative form of exactly the
length planning measured.

The adapter does not rely on that argument alone. Before each child's durable
`CHILD_APPLY_STARTED` it checks every value of the form it just built
(`form_value_expressible`). A residual failure is then a refusal *between*
children, handled by the ordinary partial-readback path, and leaves no unresolved
child journal for a request that was never sent. The capability's own pre-permit
bound stays as defense in depth for any other caller.

## The compiled candidate's own `image_hash` is never sent

The compiler requires a customer-supplied opaque `image_hash` and binds it into
the local creative content digest. This route does not send it. The creative must
reference the image *this attempt's own upload* returned, because that is the only
association a readback can prove. The plan records `compiled_image_reference_use:
LOCAL_EVIDENCE_NOT_SENT`; the golden request fixture shows the difference
(`customer-local-evidence-hash` is compiled evidence, `b5f5...` is on the wire).

## The creative comes only from the private vault

`docs/implementation/creative-asset-vault.md` owns the customer's image bytes. The
vault is content-addressed, so its object path is `<workspace>/assets/sha256/<content
digest>`. The selection names `creative_vault_ref: vault:sha256:<digest>` and the
planner derives the path; accepting a filesystem path would let a request read a
file the vault does not hold. The planner requires that digest to be the one the
compiled ad declares, reads the bytes through `assets.read_image` (which rehashes
them), re-decodes them and requires the resulting `asset:sha256:{...}` identity to
equal the compiled candidate's, and requires PNG or JPEG. The adapter repeats the
read, hash and decode against the retained binding immediately before the first
child. The bytes are never retained; the readback keeps the content digest, byte
length and media type only.

## Preconditions, all before egress

| Precondition | Refusal |
| --- | --- |
| Request carries no `meta_selection`, or `account_ref` / `resource_ref` disagree with it | `INVALID_META_ADS_CREATE_EXECUTION_SELECTION` |
| A special ad category, a non-US country, a non-USD account, or any compiled option outside traffic / link-clicks / US Facebook Feed | `META_ADS_CREATE_OPTION_NOT_ENABLED` (`NOT_ENABLED`), before any configuration, credential, vault or provider work |
| A build's ledger does not register the two additive record kinds | `META_ADS_CREATE_RECORD_KIND_NOT_INSTALLED` (planning, either kind) and `META_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED` (adapter admission, the readback kind) |
| This build's transport has no bounded multipart upload | `META_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED` (planning and adapter admission) |
| Local configuration changed since the command read it | `CONFIGURATION_CHANGED` (planning), `CONFIGURATION_DRIFT` (factory) |
| No OS-store EDIT profile for this exact ad account | `META_TOKEN_EDIT_PROFILE_REQUIRED` |
| Unresolved mutation history | `RECOVERY_REQUIRED` |
| Supplied brief does not recompile to the retained candidate | `META_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH` |
| A compiled name or request value is not printable ASCII, or a form value exceeds 4,096 characters once its copy is escaped and the reserved image hash is added | `META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE` (planning; the adapter repeats the check on each built form before the child's durable start) |
| Vault image is not the one the compiled ad declares | `META_ADS_CREATE_CREATIVE_NOT_DECLARED` |
| Vault object missing / corrupt | `ASSET_NOT_AVAILABLE` / `ASSET_CORRUPT` (the vault's own codes) |
| Vault object is not the compiled asset identity, or is not PNG/JPEG, or exceeds 5 MiB | `META_ADS_CREATE_CREATIVE_DRIFT` / `META_ADS_CREATE_CREATIVE_MEDIA_TYPE_UNSUPPORTED` / `META_ADS_CREATE_CREATIVE_TOO_LARGE` |
| Ad account `account_status` is not `1` | `META_ADS_ACCOUNT_NOT_ACTIVE` |
| Account currency or timezone differs from the candidate or the profile | `META_ADS_CREATE_ACCOUNT_MISMATCH` (or `IDENTITY_MISMATCH` from the EDIT session, one boundary earlier) |
| A campaign with this name exists, byte-exact or as a near variant | `META_ADS_CREATE_NAME_COLLISION` / `META_ADS_CREATE_NAME_COLLISION_NEAR_MATCH` |
| Inspected token window ends before the plan's own lifetime | `META_TOKEN_WINDOW_TOO_SHORT` |
| EDIT token or account facts no longer reproduce the plan's binding | `META_TOKEN_EDIT_BINDING_DRIFT` / `IDENTITY_MISMATCH` |
| Plan expired before the adapter opened, or between children | `PLAN_EXPIRED` |
| Local fingerprint key changed | `META_ADS_CREATE_FINGERPRINT_KEY_CHANGED` |
| Retained semantic states no longer reproducible | `META_ADS_CREATE_STATE_UNREPRODUCIBLE` |
| Observed baseline is not "no campaign with this name" | `REMOTE_DRIFT` |
| An adapter instance is asked to run a second time | `META_ADS_CREATE_ADAPTER_USED` (inside the apply; see below) |

**Where each refusal surfaces.** A refusal raised before the kernel's durable
`APPLY_STARTED` reaches the caller under its own code, with no credential-bearing
request made and no unresolved start. That covers every planning refusal, the
route's own `PLAN_EXPIRED` check, and everything adapter admission checks before
it opens the credential session: `PLAN_EXPIRED`, `CONFIGURATION_DRIFT`, the
record-kind and upload-seam checks, and the purely local re-checks
`META_ADS_CREATE_FINGERPRINT_KEY_CHANGED`, `META_ADS_CREATE_STATE_UNREPRODUCIBLE`,
the vault's `ASSET_NOT_AVAILABLE` / `ASSET_CORRUPT`, and
`META_ADS_CREATE_CREATIVE_DRIFT`. The kernel's own preflight (identity and state
reads) also runs before `APPLY_STARTED`, so account drift surfaces as
`REMOTE_DRIFT`, `IDENTITY_MISMATCH` or `META_TOKEN_EDIT_BINDING_DRIFT`.

A refusal raised **inside** `apply_children` is different. The kernel converts
every adapter exception raised there into an `AMBIGUOUS` outcome and keeps no
exception code, so the `ExecutionResult` for that subplan is
`AMBIGUOUS_REMOTE_STATE` with `diagnostic_code=None` and `recovery_required=True`
even when nothing was sent. The in-apply re-checks (the vault, key and state
re-checks repeated as defense in depth, the plan-expiry check, token continuity,
the per-form bound, and `META_ADS_CREATE_ADAPTER_USED`) are of this kind. They are
all re-checks of something admission or preflight has just proven, so they fire
only on a state that changed in between.

The near-match set is the cross product of two seeds (the name and its stripped
form), the four Unicode normalization forms, five case foldings and four
single-space paddings, compared locally against the campaign names the account's
`campaigns` edge lists (Meta returns the names, so no per-variant probe is
needed). The read is `GET /act_{id}/campaigns` with no `effective_status` filter,
paged to a bound, so "complete" means only *every page of what that edge lists*;
whether the default listing includes archived or deleted campaigns is
`UNVERIFIED` (see the wire-fact table). It catches whitespace, normalization and
*uniform* case differences. It does **not** catch an arbitrary mixed-case name or
padding of more than one space. An account holding more than 5,000 listed
campaigns, or a listing that exceeds the page bound, fails closed rather than
comparing a partial list.

The credential boundary is the Meta EDIT session, unchanged: the inspected
subject and inspector tokens, the configuration-then-credential transaction, and
the continuity marker are those of
[`meta-ads-token-edit-session.md`](meta-ads-token-edit-session.md). The adapter
holds no token, URL, HTTP client or transport, only a factory-issued capability
whose account is fixed, whose five edges are the ones above, and whose form-field
set is a closed allowlist per edge.

## A partial hierarchy is recorded, never repaired

A child that fails after an earlier child succeeded stops the sequence. The route
then reads back whatever now exists, retains that observation, and returns
ambiguous. This holds whether the failure was after the permit (a lost or
unparseable response) or before a later child's permit (an expired plan, broken
token continuity, an inexpressible form): the earlier children already created
resources, so they are read back and retained either way. A refusal before the
**first** child created nothing and retains no readback record. If it is raised
inside `apply_children` its result is the ambiguous, code-less outcome described
under "Where each refusal surfaces"; the checks that need no provider read run at
adapter admission instead, so they reach the caller under their own codes.

The kernel makes the subplan ambiguous regardless of what the adapter returns,
because a subplan whose approved children did not all prove they applied has no
attributable acknowledgement; the unresolved child blocks every later guarded
write until reconciliation.

Nothing is deleted. Everything this route creates is `PAUSED`, rule 11 forbids
deleting a resource that is not proven `PROVEN_UNREFERENCED`, and no documented
idempotency contract exists that could make a retry converge rather than
duplicate. The declared disposition is `RETAIN_REFS_LEAVE_PAUSED_NO_DELETE`. The
adapter has no delete, remove or archive path at all, and `reconcile` returns
`AMBIGUOUS_REMOTE_STATE` with `settled=False`: a later matching read never settles
an attempt this kernel started. `SubPlan` reserves its explicit
`recovery_disposition` for GA4 CREATE and requires a provider-request recovery
action, so the compiled subplan names `PAUSE`, the only non-destructive one and a
no-op against a hierarchy this route created paused.

The readback is by the identifiers this attempt's own children acknowledged, and
it first runs the planned-name probe. What the probe lists and what this attempt
acknowledged are compared, and each combination is a different statement:

- **The campaign's response was lost.** There is no campaign identifier to read.
  If the campaign was in fact created, the probe lists a campaign carrying the
  planned name that this attempt cannot attribute to itself, and the record says
  exactly that (`UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED`). It is most likely this
  attempt's own campaign, but nothing in the record proves that.
- **The campaign identifier was acknowledged and the probe lists exactly that
  campaign.** The acknowledged nodes are then read by identifier.
- **The campaign identifier was acknowledged and the probe lists a different
  campaign carrying the planned name or a near variant, alone or beside the
  acknowledged one.** This is a second campaign with the planned name or a near
  variant, possibly a rival writer. The record is again
  `UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED`, because the probe listed a name it
  cannot attribute. It says nothing about the acknowledged campaign's state.
- **The campaign identifier was acknowledged and the probe lists no campaign with
  the planned name or a near variant.** A list that lags the create looks
  exactly like this (see the wire-fact table). The record says only that the
  acknowledged campaign was not listed (`ACKNOWLEDGED_CAMPAIGN_NOT_LISTED`). It
  does not claim any campaign carrying the name was observed, and it does not
  claim the acknowledged campaign is absent.

Each of those records names no resource and shows what was acknowledged and
nothing more. When the upload's response was lost there is no identifier at all,
so no read is made (`READBACK_NOT_ATTEMPTED`). Reconciliation by name is the
recovery path, not this route.

## Two records, one join key, plus the readback

The planner retains `META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN` (the closed
action: recompiled candidate, observed account, EDIT token binding, vault
creative, promotion checks, postcondition) and then the provider-neutral
`EXECUTION_PLAN_RETAINED` record, both describing the byte-identical
`CompositePlan`. `plan_digest` is the join key: the adapter factory resolves the
retained action from a verified checkpoint by that digest and refuses unless the
two plans compare byte for byte.

`META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_READBACK_OBSERVATION` is the third record.
It retains the bounded observation interval, the ordered child labels this
attempt acknowledged, the observed member count, keyless SHA-256 pseudonyms for
the campaign, ad set, creative, ad and image, the creative's content digest, byte
length and media type, the observed campaign effective status, and the fixed
statements above. It never retains a request body, a response body, a token, ad
copy, a destination URL, a Page ID, an image hash or image bytes.

What the readback did and found is one of eight relations, and the record cannot
say more than it observed. The model enforces each relation against the record's
own child journal:

| `observed_hierarchy_relation` | Meaning | Model requires |
| --- | --- | --- |
| `MATCHES_PREDICTED_SEMANTICS` | All four members observed, every pausable entity `PAUSED`, both associations proven, every child acknowledged. The creative member's copy-bearing fields are the not-read sentinel on both sides | complete attempt, both associations proven |
| `DIVERGENT_HIERARCHY_OBSERVED` | All four members observed, but configuration, a status or an association differs from the prediction. `every_pausable_entity_configured_status` is `NOT_ALL_PAUSED` when the provider returned any campaign, ad set or ad that is not `PAUSED` | complete attempt, four members |
| `PARTIAL_HIERARCHY_OBSERVED` | The acknowledged nodes were read and fewer than four members exist, as an interrupted ordered create leaves behind | fewer than four members |
| `NO_HIERARCHY_OBSERVED` | The planned-name probe completed and listed no campaign with the planned name or a near variant. No campaign identifier had been acknowledged, so no node was read | at least one acknowledged child; `create-campaign` not among them; no resource |
| `UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED` | The probe listed a campaign carrying the planned name (or a near variant) that this attempt cannot attribute to itself. Without an acknowledged campaign identifier it is most likely this attempt's own campaign whose response was lost; with one it is a second campaign carrying the name, possibly a rival writer. Nothing here proves which | at least one acknowledged child; no resource |
| `ACKNOWLEDGED_CAMPAIGN_NOT_LISTED` | This attempt acknowledged the campaign identifier, and the probe listed no campaign with the planned name or a near variant. A list that lags the create looks exactly like this. It records no observation of any campaign and no claim that the acknowledged one is absent | `create-campaign` among the acknowledged children; no resource |
| `READBACK_NOT_ATTEMPTED` | No child had acknowledged an identifier, so there was nothing to read by identifier and no read was made. It is not "nothing exists": an upload whose response was lost may have stored an image | no acknowledged child; no resource |
| `READBACK_UNAVAILABLE` | A child acknowledged a resource and the readback could not be completed or parsed (an allowlist miss, a transport failure). It is deliberately not "nothing exists" | at least one acknowledged child; no resource |

`every_pausable_entity_configured_status` is derived from the observed
`configured_status` of the campaign, ad set and ad; it is never defaulted. A
matching record requires `PAUSED`.

## The ledger registration and transport seam

`ledger.py` is coordinator-owned; this ticket's registration is five additive
edits with no change to any existing kind, stream, projection or digest chain:
the two record models' imports, two `_KINDS["changes"]` entries, one `append`-path
call to `validate_meta_create_readback_history`, one `_validate_payload` branch
calling `revalidate_meta_ads_create_plan_record`, and one call in `_scan_stream`
so the history validator runs on every verified read as well as at append
(`tests/test_ledger_history_validators_on_scan.py` pins that correspondence). The
two build checks stay: a build whose ledger lacks either kind refuses at planning
and at factory admission, before any credential.

The upload uses `SafeHttpClient.upload_image`, which P04-OA added for OpenAI and
this route reuses unchanged: exactly one part, a fixed field-name grammar, a
neutral generated filename (`creative.png` / `creative.jpg`, never a customer name
or the content digest), a closed media-type set, a 5 MiB in-memory bound, no
caller-set content headers, no redirects and single-shot mutation semantics. The
EDIT session adds two private fixed handlers, one multipart upload and one
urlencoded create, each re-checking its route grammar and form allowlist rather
than trusting the issuing capability.

## Validation

`tests/test_meta_create_execution.py` drives the whole path with fakes only: the
real contract, vault, `SafetyKernel` with its journaled child egress, local
ledger, EDIT token session and a signed receipt from an ephemeral test key, over a
fake credential store, `httpx.MockTransport` and a fake installed bridge. It
covers:

* registration on both registries and the declared `child_egress` capability, the
  ordered child plan, the golden five-request sequence in
  `tests/fixtures/meta_ads_hierarchy_create_requests.json`, and the
  `APPLIED_VERIFIED` happy path with one durable child start before each POST;
* the retained readback's pseudonyms and proven associations, the redaction
  boundary of every shared surface, and the creative member's not-read sentinel on
  both the prediction and the observation;
* non-ASCII copy escaped on the wire; the closed image-hash grammar; a creative
  form planned exactly at the 4,096-character bound that still executes; a hash
  longer than the reserved one that is refused before the creative child; and a
  widened grammar whose overflowing form is refused before the creative child's
  durable start, leaving no unresolved child journal;
* a lost child that leaves the earlier children retained; the relation each lost
  acknowledgement (children 1 to 5) records; a transport loss before the campaign
  exists; a refusal between children that still retains the partial readback; a
  plan that expires between children on the injected route clock; a
  `CHILD_APPLY_RESULT` that never lands and the global write barrier;
* an acknowledgement with an undocumented field at each of the five children;
* an adversarial provider that reads back an `ACTIVE` campaign, ad set or ad,
  drifted budget, schedule, objective, optimization goal, billing event or bid
  strategy, an image the account does not hold, a creative or ad that references
  something else, an out-of-allowlist node, and a rival campaign holding the
  planned name;
* one negative test per precondition code in the table above, each asserting the
  exact code, zero provider POSTs and, for the checks that run before the
  credential session, no further token inspections: `META_ADS_CREATE_*` option,
  not-expressible, candidate-mismatch, creative-not-declared, creative-drift,
  media-type, too-large, account-mismatch, name-collision (including a paged
  collision, a looping or foreign cursor and an account above the bounded read),
  fingerprint-key, state-unreproducible, adapter-used and the three build
  fail-closed codes; `CONFIGURATION_CHANGED`, `CONFIGURATION_DRIFT`, the
  vault's `ASSET_NOT_AVAILABLE` and `ASSET_CORRUPT` at planning and at admission;
  `META_TOKEN_WINDOW_TOO_SHORT`, `META_TOKEN_EDIT_PROFILE_REQUIRED`,
  `META_TOKEN_EDIT_BINDING_DRIFT`, `IDENTITY_MISMATCH`, `REMOTE_DRIFT`,
  `RECOVERY_REQUIRED`; and `PLAN_EXPIRED` driven through the real clock seams at
  the route, at adapter admission and between children;
* the fixed capability's closed route and form grammar and its pre-permit value
  bound, the unconfigured-bridge refusal with no credential read, and the readback
  history validator and the per-relation model invariants.

`tests/test_meta_create_execution_review.py` adds the direct per-child
permit-consumption test.

`scripts/validate_development.py` adds `meta_ads_create_execution_refusal_smoke`:
in the installed unconfigured wheel, `discover` reports the registered pairs and
`plan execution create --provider meta_ads --operation CREATE` refuses a request
with no selection, exit 2, before any credential, echoing back no ad-account
reference.

## Evidence and limits

- Every check is hermetic. No provider transport, credential store, native
  helper, approval host, operating-system authentication or network call was
  involved, and none of this is evidence that Meta accepts anything.
- **No live request, no account eligibility proof, no provider-UI
  reconciliation, and no activation.** The capability status stays
  `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.
- Page eligibility, placement eligibility and creative-transformation
  eligibility are unreadable before the entities exist; the plan records them as
  explicit `NOT_READABLE` / `REQUIRES_POST_CREATE_READBACK` statuses.
- An uploaded image's bytes can never be read back; the association is an
  account-level hash match, not byte equality.
- No idempotency, conditional-write or settlement contract exists, so no
  ambiguous child is ever retried and `AttributableSettlementProof` stays
  unimplemented for this provider.
- The write barrier remains global across providers and accounts, per
  `kernel-generalization.md` §5.
- The wire-fact table above lists what a live validation must still settle. The
  riskiest `UNVERIFIED` claims, each a live-pilot check before anything is
  trusted: the multipart field name `filename`; the top-level creative
  `image_hash`; the ad-set `bid_strategy` beside a campaign budget; the
  32-lowercase-hexadecimal image-hash grammar; whether the `campaigns` edge's
  default listing includes archived and deleted campaigns; and whether a
  just-created campaign is visible on the immediate list read.

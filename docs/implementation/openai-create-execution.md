# One paused OpenAI Ads chat-card hierarchy CREATE, as several journaled requests

Last updated: 2026-09-09

This is P04-OA. It registers the first `ChildEgressAdapter` in the repository on
the two provider-neutral registries S3 and S4 created, for the exact pair
`("openai_ads", CREATE)`. It adds **no command, no approval kind, no credential,
no scope, and no live evidence**. `plan execution create --provider openai_ads
--operation CREATE` compiles and retains a plan; `execute plan <digest>` refuses
at the uninstalled approval bridge in every ordinary build, before the adapter
factory opens and before any credential is read.

## Why this operation is several requests

Google Ads creates a whole Search hierarchy in one atomic `googleAds:mutate`
with temporary resource IDs, so P04-GA is a single-request guarded adapter.
OpenAI Ads documents no atomic hierarchy mutation and no way to create an ad
without a `file_id` that only an upload returns. One approved outcome is
therefore four or more dependent requests:

```text
1  upload-creative   POST /v1/upload      -> {"file_id": "file_..."}
2  create-campaign   POST /v1/campaigns   -> the campaign resource object
3..2+N create-ad-group  POST /v1/ad_groups   -> the ad-group resource object
3+N..2+N+M create-ad    POST /v1/ads         -> the ad resource object
```

`SubPlan.expected_child_egress_count` is `1 + 1 + N + M`, bounded by the kernel's
`MAX_CHILD_EGRESS_COUNT` (32). A compiled hierarchy that needs more is refused at
planning with `OPENAI_ADS_CREATE_HIERARCHY_TOO_LARGE`, before any read.
`kernel-generalization.md` §3 records why this stays one subplan rather than
several: nobody authorized "create the campaign but not the ads".

Each child appends and fsyncs `CHILD_APPLY_STARTED`, receives a one-use permit
bound to `(plan_digest, subplan_digest, attempt_ref, child_index)`, consumes that
permit on the statement immediately before its POST, parses one closed
acknowledgement subset, and reports `CONFIRMED` with evidence or nothing at all.
Labels are step names, not resource names: `upload-creative`, `create-campaign`,
`create-ad-group`, `create-ad` all satisfy the kernel's label regex, which
refuses anything identifier-shaped.

## Per-claim wire-fact provenance

Every row was re-verified by fetching the cited page on **2026-09-09**. The
citation is the page; the fetch date is the same for all of them. Rows marked
`UNVERIFIED` are local narrowings this build chose, not documented facts.

| Claim | Source | Status |
| --- | --- | --- |
| Base host and version prefix are `https://api.ads.openai.com/v1` | [Files reference](https://developers.openai.com/ads/api-reference/files) (the upload curl uses the full path) | VERIFIED 2026-09-09 |
| Authorization is `Authorization: Bearer $OPENAI_ADS_API_KEY` | [API overview](https://developers.openai.com/ads/api-overview) | VERIFIED 2026-09-09 |
| Rate limits are 600 requests/minute per endpoint and 1,200 overall, by ad account and IP | [API overview](https://developers.openai.com/ads/api-overview) | VERIFIED 2026-09-09 |
| **No** idempotency key, request identifier, or settlement resource is documented for a write | [API overview](https://developers.openai.com/ads/api-overview) | VERIFIED 2026-09-09 (an evidence gap, not proof none exists) |
| `POST /v1/upload` accepts a binary file as multipart form data with the single field `file` | [Files reference](https://developers.openai.com/ads/api-reference/files), "Upload a binary file" | VERIFIED 2026-09-09 |
| The upload response is exactly `{"file_id": "file_901"}` | [Files reference](https://developers.openai.com/ads/api-reference/files), "Upload from an image URL" | VERIFIED 2026-09-09 |
| The binary-upload section itself prints **no** response example | [Files reference](https://developers.openai.com/ads/api-reference/files) | VERIFIED 2026-09-09 — this build accepts the `{"file_id": ...}` shape from the sibling section |
| No `GET` by file ID, no file listing, no metadata, no content route, no provider hash | [Files reference](https://developers.openai.com/ads/api-reference/files), and [openai-file-read-contract.md](../research/openai-file-read-contract.md) | VERIFIED 2026-09-09 |
| No documented media-type allowlist or maximum byte size for a creative upload | [Files reference](https://developers.openai.com/ads/api-reference/files) | VERIFIED 2026-09-09 (absence). The PNG/JPEG allowlist and the 5 MiB bound below are **UNVERIFIED** local narrowings |
| The `file_` identifier prefix | one response example only | **UNVERIFIED** as a grammar; a local narrowing |
| `POST /v1/campaigns` takes `name`, `start_time`, `end_time`, `status`, `budget.lifetime_spend_limit_micros`, `bidding_type`, `targeting.locations.include` | [Campaigns reference](https://developers.openai.com/ads/api-reference/campaigns) | VERIFIED 2026-09-09 |
| `status` accepts `"active"` or `"paused"`; the lifetime budget minimum is 1,000,000 micros | [Campaigns reference](https://developers.openai.com/ads/api-reference/campaigns) | VERIFIED 2026-09-09 |
| The campaign **create** section prints a single-object response with `id`, `created_at`, `updated_at`, `name`, `description`, `status`, `start_time`, `end_time`, `budget`, `bidding_type`, `targeting` | [Campaigns reference](https://developers.openai.com/ads/api-reference/campaigns), "Create a campaign" | VERIFIED 2026-09-09 |
| `POST /v1/ad_groups` takes `campaign_id`, `name`, `status`, `bidding_config.billing_event_type`, `bidding_config.max_bid_micros` | [Ad groups reference](https://developers.openai.com/ads/api-reference/ad-groups) | VERIFIED 2026-09-09 |
| The ad-group **create** section prints **no** response example | [Ad groups reference](https://developers.openai.com/ads/api-reference/ad-groups) | VERIFIED 2026-09-09 (absence) |
| `POST /v1/ads` takes `ad_group_id`, `name`, `status`, `creative.type`, `creative.title`, `creative.body`, `creative.target_url`, `creative.file_id` | [Ads reference](https://developers.openai.com/ads/api-reference/ads) | VERIFIED 2026-09-09 |
| The ad **create** section prints **no** response example; the list/update sections show `id`, `name`, `created_at`, `updated_at`, `creative`, `status`, `review_status` | [Ads reference](https://developers.openai.com/ads/api-reference/ads) | VERIFIED 2026-09-09 |
| `GET /v1/campaigns`, `GET /v1/ad_groups?campaign_id=`, `GET /v1/ads?ad_group_id=` list under an `{"object":"list","data":[...],"first_id","last_id","has_more"}` envelope | [Campaigns](https://developers.openai.com/ads/api-reference/campaigns) / [Ad groups](https://developers.openai.com/ads/api-reference/ad-groups) / [Ads](https://developers.openai.com/ads/api-reference/ads) references, and the shipped [hierarchy read contract](../research/openai-hierarchy-read-contract.md) | VERIFIED 2026-09-09 |
| `GET /v1/campaigns/{id}` retrieves one campaign | [Campaigns reference](https://developers.openai.com/ads/api-reference/campaigns), and [openai-campaign-current-contract.md](../research/openai-campaign-current-contract.md) | VERIFIED 2026-09-09 |
| Conversion bidding requires an enabled account and exactly one active standard conversion event | [Conversion-optimized campaigns](https://developers.openai.com/ads/conversion-optimized-campaigns), restated in [openai-write-contract.md](../research/openai-write-contract.md) | VERIFIED 2026-09-04, not re-fetched 2026-09-09 — this build refuses the option outright, so nothing depends on the detail |

**Because no create response schema is documented for ad groups or ads**, the
acknowledgement parsers accept the documented *resource object* allowlist for
each kind and keep exactly two facts from it: the `id` and that `status` is
`paused`. A response carrying any field outside that allowlist fails the
acknowledgement. After a consumed permit that is ambiguous, never a retry. The
readback, not the acknowledgement, is what proves what was created.

## The semantic before/after

OpenAI assigns every native identifier at creation time, so `before` and `after`
are **semantic hierarchy fingerprints** from `providers/_semantic_state.py`:
locally keyed HMACs over request-controllable names and configuration only.

* `before` is the empty member set under one `(ad account, campaign name)`
  binding — the local statement "no campaign with this name in this ad account".
* `after` is the exact predicted member set: one `CAMPAIGN` (bidding type,
  schedule, lifetime micros, a digest of the declared location targeting,
  `status: paused`), one `AD_GROUP` per compiled group (billing event, max bid,
  `status: paused`), and one `AD` per compiled ad (creative kind, the creative
  asset content digest, title/body/target-URL presence, `status: paused`).

No native identifier enters either side, which is what makes the post-create
readback comparable with the prediction at all. `member_fingerprint` additionally
refuses identifier-shaped keys and provider resource paths, so the declared
location IDs are folded into one `location_targeting_digest` value rather than
hashed under an `_ids` key.

**The ad's creative digest is a proven association, not a provider byte fact.**
There is no documented Ads file read of any kind, so nothing this build sends can
later be compared against provider storage. The observed ad member carries the
content digest **this attempt uploaded**, and only when the readback ad
references the exact `file_id` the upload child acknowledged in the same attempt.
That is `PROVEN_UPLOAD_ASSOCIATION`, exactly the narrower claim
[`openai-file-read-contract.md`](../research/openai-file-read-contract.md)
identified as supportable. `PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ` is
retained separately and never collapsed into it. An ad referencing any other file
gets the `FILE_ASSOCIATION_NOT_PROVEN` sentinel, which cannot equal a digest, so
the hierarchy cannot match its prediction.

## The compiled candidate's own `file_id` is never sent

`providers/openai_compiler.py` requires a customer-supplied opaque
`file_reference.file_id` and binds it into the local creative content digest.
This route does not send it. An ad must reference the file *this attempt's own
upload* minted, because that is the only association a readback can prove. The
plan records `compiled_file_reference_use: LOCAL_EVIDENCE_NOT_SENT`, and the
golden request fixture shows the difference directly: the compiled evidence is
`file_customer_local_evidence`, and the wire body carries `file_901`.

## The creative comes only from the private vault

`docs/implementation/creative-asset-vault.md` owns the customer's image bytes.
The vault is content-addressed, so its object path is exactly
`<workspace>/assets/sha256/<content digest>`. The selection therefore names
`creative_vault_ref: vault:sha256:<digest>` and the planner derives the path;
accepting a filesystem path would let a request read a file the vault does not
hold. The planner:

1. requires that digest to be the one **every** compiled ad declares;
2. reads the bytes through `assets.read_image`, which rehashes them;
3. decodes them again and requires the resulting
   `asset:sha256:{content, mime, width, height}` identity to equal the compiled
   candidate's own `asset_version_id`;
4. requires the decoded media type to be PNG or JPEG.

The adapter repeats (2)–(4) against the retained binding immediately before the
first child. The bytes are never retained anywhere; the readback record keeps
only the content digest, the byte length and the media type.

## Preconditions, all before egress

| Precondition | Refusal |
| --- | --- |
| Conversion bidding requested | `OPENAI_ADS_CREATE_OPTION_NOT_ENABLED` (`NOT_ENABLED`), before any configuration, credential, vault or provider work |
| A build's ledger does not register the two additive record kinds | `OPENAI_ADS_CREATE_RECORD_KIND_NOT_INSTALLED` |
| This build's transport has no bounded multipart upload | `OPENAI_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED` |
| Compiled hierarchy needs more than 32 child requests | `OPENAI_ADS_CREATE_HIERARCHY_TOO_LARGE` |
| Local configuration changed since the command read it | `CONFIGURATION_CHANGED` |
| No OS-store account-key EDIT profile for this exact ad account | `OPENAI_ACCOUNT_KEY_EDIT_PROFILE_REQUIRED` |
| Unresolved mutation history | `RECOVERY_REQUIRED` |
| Supplied brief does not recompile to the retained candidate | `OPENAI_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH` |
| Vault image is not the one every compiled ad declares | `OPENAI_ADS_CREATE_CREATIVE_NOT_DECLARED` |
| Vault object missing / corrupt | `ASSET_NOT_AVAILABLE` / `ASSET_CORRUPT` (the vault's own codes) |
| Vault object is not the compiled asset identity | `OPENAI_ADS_CREATE_CREATIVE_DRIFT` |
| Decoded media type outside PNG/JPEG | `OPENAI_ADS_CREATE_CREATIVE_MEDIA_TYPE_UNSUPPORTED` |
| Ad account not `active` | `OPENAI_ADS_ACCOUNT_NOT_ACTIVE` |
| Account currency or timezone differs from the candidate or the profile | `OPENAI_ADS_CREATE_ACCOUNT_MISMATCH` |
| A campaign with this name already exists | `OPENAI_ADS_CREATE_NAME_COLLISION` |
| Account-key binding no longer reproduces the plan's binding | `CREDENTIAL_BINDING_MISMATCH` (factory) / `CREDENTIAL_BINDING_DRIFT` (adapter) |
| Local fingerprint key changed | `OPENAI_ADS_CREATE_FINGERPRINT_KEY_CHANGED` |
| Retained semantic states no longer reproducible | `OPENAI_ADS_CREATE_STATE_UNREPRODUCIBLE` |
| Observed baseline is not "no campaign with this name" | `REMOTE_DRIFT` |

The account-key continuity is the pause family's, unchanged: the configuration
transaction opens first, the private credential slots are read inside it, the
transaction closes before any provider request, and `_credential_binding` is
derived, never accepted. See
[`openai-pause-installed-execution.md`](openai-pause-installed-execution.md) and
[`openai-account-key-lifecycle.md`](openai-account-key-lifecycle.md).

## A partial hierarchy is recorded, never repaired

A child that fails after an earlier child succeeded stops the sequence. The route
then reads back whatever now exists, retains that partial observation with
`observed_hierarchy_relation: PARTIAL_HIERARCHY_OBSERVED`, and returns ambiguous.
The kernel makes it ambiguous anyway — a subplan whose approved children did not
all prove they applied has no attributable acknowledgement — and the unresolved
child blocks every later guarded write until reconciliation.

Nothing is deleted. Everything this route creates is `paused`; rule 11 forbids
deleting a resource that is not proven `PROVEN_UNREFERENCED`; and no documented
idempotency contract exists that could make a retry converge rather than
duplicate. The declared disposition is `RETAIN_REFS_LEAVE_PAUSED_NO_DELETE`. The
adapter has no delete, remove, or archive path at all, and `reconcile` returns
`AMBIGUOUS_REMOTE_STATE` with `settled=False` — a later matching read never
settles an attempt this kernel started.

`SubPlan` reserves its explicit `recovery_disposition` for GA4 CREATE and
requires at least one provider-request recovery action, so the compiled subplan
names `PAUSE` — the only non-destructive one, and a no-op against a hierarchy
this route already created paused.

## Two records, one join key

The planner retains `OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN` (the closed
action: recompiled candidate, observed account, account-key binding, vault
creative, promotion checks, postcondition) and then the provider-neutral
`EXECUTION_PLAN_RETAINED` record. Both describe the byte-identical
`CompositePlan`, so `plan_digest` is the join key: the adapter factory resolves
the retained action from a verified checkpoint by that digest and refuses unless
the two plans compare byte for byte.

`OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION` is the third record.
It retains the bounded observation interval, the ordered child labels this
attempt acknowledged, the observed counts, keyless SHA-256 pseudonyms for the
campaign, each ad group, each ad and the uploaded file, the creative content
digest, byte length and media type, and the fixed statements above. It never
retains a request body, a response body, a bearer, an account key, ad copy, a
destination URL, an image URL, or file bytes.

## The one additive transport seam

`SafeHttpClient` supported JSON, query and urlencoded form bodies. The documented
binary upload is `-F "file=@..."`, which none of those can express, and the
alternative documented form (`{"image_url": ...}`) would make OpenAI fetch a
customer asset from another host instead of sending the private-vault bytes.
This ticket therefore adds `SafeHttpClient.upload_image`: exactly one part, a
fixed field-name grammar, a neutral generated filename (`creative.png` /
`creative.jpg` — never a customer file name and never the content digest), a
declared media type from a closed set, a 5 MiB in-memory bound, no caller-set
content headers, no redirects, and single-shot mutation semantics with no retry.
It is isolated as a patch at `/tmp/adhelm-finishing/w3-p04-oa-transport.patch` so a
coordinator can review or relocate it; the branch also carries it applied,
because rule 20 forbids shipping a capability that cannot run.

## The additive ledger registration

`ledger.py` is normally coordinator-owned; the coordinator granted this ticket
the registration directly. It is five additive edits and changes no existing
record kind, stream, projection, or digest chain:

* two imports — the readback model and its history validator, and the plan
  record model and its revalidator;
* two `_KINDS["changes"]` entries,
  `OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN` and
  `OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION`;
* one `append`-path branch calling `validate_openai_create_readback_history`
  with the record about to be written, beside the Google CREATE branch;
* one `_validate_payload` branch calling
  `revalidate_openai_ads_create_plan_record`, so a retained plan is
  reconstructed rather than merely schema-checked; and
* one call in `_scan_stream`'s `changes` block, so the same history validator
  runs on **every** verified read and not only at append.
  `tests/test_ledger_history_validators_on_scan.py` pins exactly that
  correspondence for every history validator the ledger imports.

The two build checks stay: a build whose ledger lacks either kind refuses at
planning and at factory admission, before any credential, exactly as P03 did.
An isolated patch is kept at `/tmp/adhelm-finishing/w3-p04-oa-ledger.patch` for
review.

## Validation

`tests/test_openai_create_execution.py` drives the whole path with fakes only: a
real contract, a real vault, a real `SafetyKernel` and its journaled child
egress, a real local ledger, a real signed receipt from an ephemeral test key,
and a fake credential store, `httpx.MockTransport`, and installed bridge. It
covers the registered pair on both registries and the declared `child_egress`
capability; the ordered child plan; the golden per-child request sequence in
`tests/fixtures/openai_ads_chat_card_hierarchy_create_requests.json`; the
`APPLIED_VERIFIED` happy path with one durable child start before each POST; the
retained readback's pseudonyms and proven association; the redaction boundary of
every shared surface; the review lines that state the file-readback limit; a lost
child that leaves the earlier children retained and the subplan ambiguous with a
partial readback; a `CHILD_APPLY_RESULT` that never lands, its unresolved child
start, the recovery catalog row, and the global write barrier; an ambiguous
upload that issues no further child; an acknowledgement with an undocumented
field at four different children; every precondition above; the two build
fail-closed checks; the unconfigured-bridge refusal with no credential read; the
upload seam's own bounds; and the readback history validator.

`scripts/validate_development.py` adds
`openai_ads_create_execution_refusal_smoke`: in the installed unconfigured wheel,
`discover` reports both registered pairs and `plan execution create --provider
openai_ads --operation CREATE` refuses a request with no selection, exit 2,
before any credential, echoing back no ad-account reference.

## Evidence and limits

- Every check is hermetic. No provider transport, credential store, native
  helper, approval host, operating-system authentication, or network call was
  involved, and none of this is evidence that OpenAI Ads accepts anything.
- **No live request, no account eligibility proof, no provider-UI
  reconciliation, and no activation.** The capability status stays
  `DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.
- The create response schemas for ad groups and ads are **not documented**. The
  acknowledgement parsers are built from the documented resource objects, which
  is a reviewed assumption, not a provider contract.
- An uploaded file can never be read back. `PROVEN_UPLOAD_ASSOCIATION` is a
  same-attempt association claim only; delivered-rendition or provider-storage
  byte equality remains `PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ`.
- No idempotency, conditional-write, or settlement contract exists, so no
  ambiguous child is ever retried and `AttributableSettlementProof` stays
  unimplemented for this provider.
- The write barrier remains global across providers and accounts, per
  `kernel-generalization.md` §5.

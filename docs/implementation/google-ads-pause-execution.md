# Google Ads campaign PAUSE execution, and the first configured adapter

Last updated: 2026-09-09

This is P03. It completes the Google Ads campaign PAUSE half that F01
([planning](google-ads-pause-workflow.md)) and F02
([artifacts](google-ads-pause-plan-artifacts.md)) deliberately left open: a
guarded adapter that sends the one v25 `campaigns:mutate` request, the readback
observation that records what it did, and the registration that makes
`("google_ads", PAUSE)` the first executable pair on the two provider-neutral
registries S3 and S4 introduced.

It adds **no command**, no approval kind, no credential, no scope, no installed
host, and no live evidence. `CONFIGURED_EXECUTION_ADAPTERS` stops being empty,
which is the whole change to the route's behaviour; every ordinary build still
refuses `execute plan` at the uninstalled approval bridge, two gates before the
adapter, and therefore before any credential is opened or any provider is
reached.

## The shape

```text
plan execution create --provider google_ads --operation PAUSE --input REQUEST.json
adhelm execute plan <record-digest>
```

Both are the existing S3/S4 leaves
([`execution-plan-records.md`](execution-plan-records.md),
[`generic-execution-route.md`](generic-execution-route.md)). P03 adds no leaf of
its own, and the three fixed provider leaves are untouched.

`plan google-ads-campaign-pause create` is unchanged, byte for byte. Its
request model, protected session, reads, expiry rules, retention recheck,
receipt, and artifact behaviour are exactly what F01 and F02 shipped.

## Two records, one join key

`plan execution create` runs the F01 workflow and then retains a second,
provider-neutral record:

| Record kind | Written by | Read by |
| --- | --- | --- |
| `GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN` | `plan google-ads-campaign-pause create` and `plan execution create` | `plan google-ads-campaign-pause catalog/show`, `artifacts google-ads-campaign-pause`, and the P03 adapter factory |
| `EXECUTION_PLAN_RETAINED` | `plan execution create` only | `plan execution catalog/show`, `execute plan`, `recover attempt`, `recover catalog` |

Both describe the byte-identical `CompositePlan` the M55 contract built, so
**`plan_digest` is the join key**. `open_configured_adapter` resolves the closed
M55 action from a verified checkpoint by that digest and refuses unless the
retained action's own plan compares equal to the approved one, so a record that
merely reuses a digest cannot substitute a different customer, campaign, or
credential association. A provider-neutral record with no matching action is
refused with `GOOGLE_ADS_PAUSE_PLAN_NOT_RETAINED`, before any credential.

Keeping both is not duplication for its own sake. `ExecutionPlanRecord`
deliberately models only the provider-neutral plan; the customer baseline, the
declared OAuth association, the bounded observation interval,
and the configuration postcondition live in the M55 action, and the adapter
needs all four to reach exactly one campaign under exactly one credential
binding. The alternative — teaching the generic record to carry a provider
payload — would put provider schema back inside the provider-neutral family that
S3 exists to remove.

**Why the F01 command was not changed.** The ticket allowed either editing the
F01 workflow to retain the provider-neutral record too, or registering a planner
that produces one from the same flow. The second is the smaller change: it is
zero lines inside the retention path F01/F02 already evidence, so the existing
Google planning, history, view, and artifact suites are unchanged, and one
compiled plan still produces exactly one `GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN`
record. The consequence is explicit: a plan created with
`plan google-ads-campaign-pause create` alone is historical evidence only and is
**not** executable, because no `EXECUTION_PLAN_RETAINED` record names it.
`plan execution create --provider google_ads --operation PAUSE` is the
executable route.

The generic `ExecutionPlanRequest` carries no lifetime, so the planner uses the
F01 request's own documented default of 300 seconds rather than inventing a
second bound. `resource_ref` must be
`google_ads.campaign.<ten-digit-customer>.<campaign-id>` and `account_ref` must
be the matching `google_ads.customer.<id>`; a mismatch, a missing
`resource_ref`, or a profile that does not own that customer is refused before
the protected session opens, with nothing read, compiled, or retained.

## Registration

```python
register_execution_planner("google_ads", OperationKind.PAUSE, GOOGLE_ADS_PAUSE_EXECUTION_PLANNER)
register_execution_adapter(
    "google_ads", OperationKind.PAUSE, GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
)
```

Both calls live in `register_google_ads_pause_execution()` in
`src/adhelm/google_ads_pause_execution.py`, invoked at the **end of
`src/adhelm/google_ads_pause_workflow.py`** — the module `cli.py` already imports
through `google_ads_pause_cli`. `discover` therefore renders the pair from the
two registries themselves rather than from a restated list, and
`plan execution create --provider google_ads --operation CREATE` still refuses
with `EXECUTION_PLAN_NOT_COMPILABLE` because only PAUSE is registered.

The import sits at the end of the workflow module so the registration module can
reach the workflow's request model lazily, inside the planner method, without an
import cycle. Registration of these exact singletons is idempotent; a *different*
factory for the same pair is still refused by the registries themselves
(`EXECUTION_ADAPTER_ALREADY_REGISTERED`).

One consequence is worth stating plainly: a process that never imports
`adhelm.google_ads_pause_workflow` sees empty registries. Every command path
imports it through `adhelm.cli`; the two suites that assert registry contents
import it explicitly rather than relying on import order.

`declared_capabilities()` returns `ExecutionAdapterCapabilities(child_egress=False)`
— the route is one request, so there are no journaled children — and is answered
from this module's compiled contract without reading configuration, opening a
credential store, or contacting a provider. `open_configured_adapter` receives
the route's own checked clock and refuses an already-expired plan with
`PLAN_EXPIRED` before the credential store is touched.

## The adapter

`GoogleAdsCampaignPauseGuardedAdapter` in
`src/adhelm/providers/google_ads_pause_adapter.py` implements `GuardedAdapter`.
It has no public bearer, URL, HTTP-client, raw-acknowledgement,
or clock input, refuses serialization, and revalidates its own members and the
approved subplan on every call.

- **`capabilities`** — v25, `google-ads-v25-campaign-pause-contract-2026-09-06`,
  `(PAUSE,)`, writes enabled. The kernel refuses any mismatch with the approved
  subplan.
- **`read_identity`** — one fixed `googleAds:search` customer read through the
  sealed session, including the manager `login-customer-id` route when the
  profile declares one. The observed customer id, currency, timezone, status,
  and API version must equal the approved account baseline; anything else is
  `IDENTITY_MISMATCH`. `CLOSED` remains an observed provider status, not an
  eligibility gate, exactly as in M54/M55.
- **`read_state`** — one fixed campaign read through
  `providers/google_ads_campaign_state.py`, after the identity check.
  `ENABLED` maps to the approved `before` fingerprint and `PAUSED` to the
  approved `after`; `REMOVED`, a different campaign, or a different customer is
  `REMOTE_DRIFT` or `IDENTITY_MISMATCH`. The kernel's `_preflight_one` compares
  that against the plan, so status, currency, and customer drift all refuse
  **before** the durable `APPLY_STARTED` and before any egress.
- **`apply_guarded`** — refuses a second use, requires the kernel's durable
  `APPLY_STARTED` in local history, takes a final customer+campaign baseline and
  rechecks drift, rechecks plan expiry, preflights the transport, and then sends
  exactly one request:

  ```text
  POST https://googleads.googleapis.com/v25/customers/{customer}/campaigns:mutate
  {"operations":[{"updateMask":"status",
                  "update":{"resourceName":"customers/{customer}/campaigns/{campaign}",
                            "status":"PAUSED"}}],
   "partialFailure":false,"validateOnly":false,"responseContentType":"RESOURCE_NAME_ONLY"}
  ```

  The field mask is exactly `status`, so no other campaign field is writable by
  this route. There is no retry: `SafeHttpClient` forces one attempt for a
  mutation, and the adapter never issues a second request under any outcome.
  Which parts of that request and response shape are sourced, and which are
  not, is recorded fact by fact in [wire-fact provenance](#wire-fact-provenance);
  several rows are `UNVERIFIED` because the v25 reference pages were unreachable
  on 2026-09-09.

- **`verify`** — true only when `apply_guarded` produced its two evidence
  references in this process; otherwise false with no evidence.
- **`reconcile`** — always `AMBIGUOUS_REMOTE_STATE`, `settled=False`, with no
  evidence, even when a later read shows `PAUSED`. There is no reviewed
  settlement, operation resource, or idempotency proof for this route, so no
  read this caller takes can attribute the observed state to this attempt.
  `ATTRIBUTABLE_SETTLEMENT_PROVIDERS` stays empty and this adapter implements no
  settlement proof.

### Permit and ambiguity

The kernel's one-use `EgressPermit` is claimed **after** the header callback has
completed its configuration and three-slot credential checks and after the
transport is revalidated, and **immediately before** the single POST. That
ordering keeps a credential-drift, expiry, or clock failure a clean pre-egress
refusal instead of an ambiguous one.

Everything from the claim onward is potentially dispatched. A transport failure
raises a private dispatch-unverified marker, and every later failure — a refused
acknowledgement, a readback failure, an unexpected post-state, a failed record
append — is caught by one handler. All of them produce
`AMBIGUOUS_REMOTE_STATE`, which is what the kernel already requires of any
non-`CONFIRMED` acknowledgement. A confirmed acknowledgement whose permit was
never claimed is downgraded by the kernel itself with
`GUARDED_EGRESS_NOT_CLAIMED`, and a confirmed acknowledgement whose readback
never left the `ENABLED` baseline is ambiguous because it contradicts itself.

An ambiguous attempt leaves its durable `APPLY_STARTED` unresolved, which blocks
every later guarded write on every provider until `recover attempt` records an
observation. Releasing that barrier settles nothing and authorizes no retry; the
plan stays one-use and a further mutation needs a new plan compiled from a fresh
read.

### The acknowledgement subset

The parser is closed. It accepts an exact HTTP 200 whose bounded JSON body is an
object with the single member `results`, holding exactly one object with the
single member `resourceName`, equal to `customers/{customer}/campaigns/{campaign}`
for the approved campaign. Everything else is refused: a non-200 status, a
`partialFailureError` member, an extra member on the result, zero or more than
one result, a different resource name, or a non-JSON body. Because the permit is
already consumed, a refusal is `AMBIGUOUS_REMOTE_STATE`, never a failure claim.

It is not a claim that Google Ads has a live acknowledgement, partial-failure,
conditional-write, or idempotency contract for this repository. The M54 read
research ([`google-ads-edit-observation-contract.md`](../research/google-ads-edit-observation-contract.md))
explicitly deferred those questions, and they remain deferred: the plan and the
readback record both carry `conditional_write_proof: false` and
`idempotency_proof: false`. Where each individual wire fact comes from, and
which ones nothing in this repository verifies, is recorded fact by fact below.

## Wire-fact provenance

Every wire fact this route depends on is listed here with its own status. A
blanket disclaimer is not enough: a reader needs to know which specific claim is
sourced and which is not.

**Reachability, checked 2026-09-09.** The Google Ads v25 REST *reference* pages
for this endpoint could not be retrieved. `.../reference/rest/v25/customers.campaigns/mutate`
and `.../reference/rest/v25/MutateCampaignsResponse` both returned **HTTP 404**;
`.../reference/rpc/v25/MutateCampaignsRequest`, `.../reference/rpc/v25/CampaignService`,
`.../reference/rpc/v25/Campaign`, `.../fields/v25/campaign` and
`.../docs/mutating/overview` returned only the documentation site's navigation
shell with no field definitions; `.../docs/mutating/field-masks` returned **HTTP
404**. Three *design* pages were reachable and are cited below. No customer API,
OAuth endpoint, credential store, or provider request was used to write this
note.

| # | Wire fact this route relies on | Status | Source |
| --- | --- | --- | --- |
| 1 | Resource name format `customers/{customer_id}/campaigns/{campaign_id}` | **CITED** | [REST design: resource names](https://developers.google.com/google-ads/api/rest/design/resource-names), fetched 2026-09-09 ("Campaign: `customers/1234567890/campaigns/8765432109`") |
| 2 | The JSON member carrying a resource name is `resourceName`, not `name` | **CITED** | [REST design: resource names](https://developers.google.com/google-ads/api/rest/design/resource-names), fetched 2026-09-09 ("referred to by the `resourceName` field, not the `name` field") |
| 3 | Proto `snake_case` maps to JSON `lowerCamelCase`, so `update_mask`/`partial_failure`/`validate_only`/`response_content_type` are `updateMask`/`partialFailure`/`validateOnly`/`responseContentType` in a JSON body | **CITED** | [REST design: JSON mappings](https://developers.google.com/google-ads/api/rest/design/json-mappings), fetched 2026-09-09 ("Identifiers are transformed from _snake_case_ ... to _lowerCamelCase_ in JSON") |
| 4 | With partial failure **not** enabled, the whole request fails if any operation errors | **CITED** | [Best practices: partial failures](https://developers.google.com/google-ads/api/docs/best-practices/partial-failures), fetched 2026-09-09 |
| 5 | A partial-failure response carries `partial_failure_error` (JSON `partialFailureError`) | **DERIVED FROM CITED** | field name from [partial failures](https://developers.google.com/google-ads/api/docs/best-practices/partial-failures); JSON spelling by rule 3. Not read from a v25 response schema |
| 6 | `partial_failure` **defaults to false** | `UNVERIFIED (reference page unreachable on 2026-09-09)` | the partial-failures page describes the disabled behaviour but does not state the default. The adapter never relies on the default: it always sends `"partialFailure": false` explicitly |
| 7 | Host `googleads.googleapis.com` and the `/v25/` path segment | **CITED (read route only)** | [`google-ads-edit-observation-contract.md`](../research/google-ads-edit-observation-contract.md), which cites the v25 `GoogleAdsService.Search` REST reference for `POST https://googleads.googleapis.com/v25/customers/{customerId}/googleAds:search` (checked 2026-09-06). The host and version segment are shared; the write route below is not covered by it |
| 8 | The write route is `POST https://googleads.googleapis.com/v25/customers/{customer_id}/campaigns:mutate` | `UNVERIFIED (reference page unreachable on 2026-09-09)` | the `customers.campaigns/mutate` REST reference returned HTTP 404 |
| 9 | An update operation is `{"updateMask": ..., "update": {...}}` inside a top-level `operations` array | `UNVERIFIED (reference page unreachable on 2026-09-09)` | no reachable page defines `CampaignOperation` or `MutateCampaignsRequest.operations` |
| 10 | The field-mask path for `campaign.status` is the bare `"status"` | `UNVERIFIED (reference page unreachable on 2026-09-09)` | `.../docs/mutating/field-masks` returned HTTP 404 and `.../fields/v25/campaign` returned a navigation shell |
| 11 | `CampaignStatus` includes the value `PAUSED` | `UNVERIFIED (reference page unreachable on 2026-09-09)` | not re-verified here. It is however the same closed value the M54 read parser already accepts and this workspace already observes on the read route |
| 12 | `validate_only` exists on the request and defaults to false | `UNVERIFIED (reference page unreachable on 2026-09-09)` | the adapter always sends `"validateOnly": false` explicitly, so no default is relied on |
| 13 | `response_content_type` exists on the request | `UNVERIFIED (reference page unreachable on 2026-09-09)` | no reachable page defines it for v25 |
| 14 | `RESOURCE_NAME_ONLY` is the **v25 default** for `response_content_type` | `UNVERIFIED (reference page unreachable on 2026-09-09)` | the adapter's inline comment asserts this. **The route does not depend on it**: it sends the value explicitly, precisely so the acknowledgement shape is fixed by the request rather than by a server default. The "v25 default" half of that comment should be read as unverified |
| 15 | A successful response is `{"results": [{"resourceName": ...}]}` | `UNVERIFIED (reference page unreachable on 2026-09-09)` | `MutateCampaignsResponse` returned HTTP 404. Facts 1 and 2 support the *shape* of the `resourceName` value, not the envelope around it |

**Why an incorrect guess here is contained rather than dangerous.** The parser
is closed and refuses everything outside the one subset (§ *The acknowledgement
subset*), so a wrong assumption about the response envelope produces
`AMBIGUOUS_REMOTE_STATE` — never a silent accept and never a retry. A wrong
assumption about the *request* body produces a provider error, which is also
ambiguous because the permit is already consumed. The failure mode of every
unverified row above is therefore an unresolved attempt that blocks later
guarded writes until `recover attempt` records an observation, which is the
correct behaviour for an uncertain write.

### Software-development-kit provenance

**No Google Ads client library is used, vendored, or installed by this
repository.** `pyproject.toml` declares no `google-ads` dependency and `uv.lock`
contains no `google-ads` entry; the only Google package is `google-auth`, which
is OAuth-only and has no Ads service surface. There is therefore **no SDK
version to cite**, and no wire fact in the table above derives from reading SDK
source.

Rows 8 through 15 are consequently *model-derived offline engineering* over the
v25 REST surface — an assistant's prior knowledge of the shape, constrained by
the three reachable design pages and by the M54 read contract — and not
transcribed from either a reference page or a client library. A later reviewer
should treat them as unsourced until the reference pages are reachable again or
a live feasibility test is authorized.

Two consequences worth stating for that reviewer:

- The adapter module docstring in
  `src/adhelm/providers/google_ads_pause_adapter.py` opens with "This is
  SDK-derived offline engineering over the reviewed v25 shape." That wording was
  mirrored from the Meta adapter and is **inaccurate for Google**: there is no
  SDK here. It should read "model-derived"; this note is the authoritative
  provenance record until that one-line correction lands.
- The right closure for this gap is a reviewed research note under
  `docs/research/` once the v25 `customers.campaigns/mutate` and
  `MutateCampaignsResponse` references are reachable, followed by the live
  feasibility evidence the capability matrix row still withholds.

### Credentials

The OAuth bundle and the fixed PAUSE binding key are resolved
only through the existing two-slot protected continuity described in
[`google-ads-pause-credentials.md`](google-ads-pause-credentials.md). P03 adds
no credential API and no new slot. The adapter reuses the sealed F01 planning
session as its credential and read boundary — the same configuration
transaction, the same casefold-ordered slot locking, the same no-refresh header
callback, and the same retention recheck — through that module's own validated
private accessors, and composes only the single mutation itself. At factory
admission the adapter requires the current association to equal the approved
one (profile, installed client, derived principal, `adwords` scope, manager
route, credential-binding HMAC, both configuration digests, and the finite
bearer expiry) and the plan to expire inside that credential window;
otherwise `GOOGLE_ADS_PAUSE_EXECUTION_BINDING_DRIFT`. No secret enters a plan,
record, evidence reference, diagnostic, or command output.

## The readback observation

`src/adhelm/google_ads_pause_readback_records.py` defines
`GOOGLE_ADS_CAMPAIGN_PAUSE_READBACK_OBSERVATION`, the Google sibling of
`META_CAMPAIGN_PAUSE_READBACK_OBSERVATION`. One record is appended after the
acknowledgement, under the session's retention recheck, and holds:

- the action, plan, subplan and attempt references;
- the account and campaign references;
- the bounded observation interval around the post-acknowledgement reads;
- the factual `before_configured_status: ENABLED` and
  `after_configured_status: PAUSED`;
- the observed customer status;
- `acknowledged_resource_name` — the **only** acknowledgement content retained;
- fixed `NOT_PREDICTED` delivery and false CAS/idempotency controls.

No request body, response body, header, bearer, or provider
diagnostic is retained. The record's digest is the adapter's second evidence
reference; the first is a digest of the acknowledgement subset's own facts
(action, plan digest, HTTP 200, the `status` field mask, one result).

`validate_google_ads_pause_readback_history` requires every retained readback to
name one retained M55 action, restate that action's own plan and subplan
digests, sit in the right ledger envelope, and follow exactly one durable
`APPLY_STARTED` for the same attempt reference. A second readback for one
attempt is refused.

The record differs from Meta's in one deliberate way: it carries `occurred_at`
equal to its own `observation_interval_end`, because the event it describes *is*
the local observation and its instant is known exactly. Meta leaves that field
null and needs a ledger exemption for it; this record does not.

**Ledger registration.** The kind is registered in `adhelm.ledger`
(`_KINDS["changes"]`), the history validator runs in `Ledger.append`, and the
review repair also runs it in the stream verification block so a retained
readback is revalidated on every scan rather than only at append time. The
adapter still checks the registration at factory admission and refuses closed
with `GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND_NOT_INSTALLED` — before any
credential, header, or provider request — so a build that ever dropped the entry
would refuse the whole execution rather than pause a campaign and then fail to
retain the observation that proves it.

## Discovery and status

`GOOGLE_ADS_PAUSE_EXECUTION_COMMAND_STATUS` moves from
`NOT_IMPLEMENTED_P03_REQUIRED` to the shared
`INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS`
(then `DEVELOPMENT_ONLY_SETUP_REQUIRED`, now `UNRELEASED` under the 0.1.0
release policy). Both `discover` and the Google artifact renderer read that one
constant, so they cannot drift apart. The value is a statement about the
command, not about this capability: the route exists and is registered, and it
refuses.

`discover` now reports `google_ads`/`PAUSE` in both
`execution_plan.compilable_operations` and
`execution_plan.executable_operations`, and the `CONFIGURED_EXECUTION_ADAPTER`
refusal gate as satisfied. The adapter gate is still never reached, because the
release-policy gate (`WRITES_NOT_RELEASED`, since O3.2) and both bridge gates
precede it.

## Validation

`tests/test_google_ads_pause_execution.py` drives the real M55 contract, the
real F01 workflow, the real protected-credential lifecycle, the real S3 record,
the real S4 route, a real signed receipt from an ephemeral test key, a real
`SafetyKernel`, and a real local ledger, against a fake operating-system
credential store, `httpx.MockTransport`, and a fake *installed* bridge. It
covers: the exact registered pair on both registries and the refusal of a
duplicate; the two retained records and the `plan_digest` join; malformed and
cross-customer planner selections refused before any read; the full happy path
to `APPLIED_VERIFIED` with the exact mutate body; status, currency, and customer
drift refused before the mutation and before the durable start; an ambiguous
transport and the write barrier it leaves; a wrong resource name, a
partial-failure envelope, an extra member, a non-200 status and a non-JSON body
all ambiguous; a readback that never left the baseline; the permit claimed
exactly once immediately before the POST; an unclaimed permit downgraded;
`verify` false without the adapter's own proof; `reconcile` never settling even
after a matching `PAUSED` read; determinism of the readback record; the
uninstalled bridge refusing with a recording credential store proving no store
was touched; an unretained record digest; a missing M55 action; and the
readback-kind refusal before credentials.

`tests/test_generic_execution_route.py`, `tests/test_generic_execution_cli.py`,
`tests/test_execution_plan_records.py`,
`tests/test_execution_plan_records_review.py` and
`tests/test_execution_plan_cli.py` had pinned the registries as empty; each pin
now names the exact registered set instead, and the unregistered-pair refusals
use `ARCHIVE`, which no adapter executes. The Google planning, history, view,
and artifact suites are otherwise unchanged apart from the one status string.

`scripts/validate_development.py` adds
`google_ads_pause_execution_refusal_smoke`: in the installed unconfigured wheel,
`discover` reports the pair and the `UNRELEASED` execution
status, `plan execution show <64 zeros>` refuses with
`EXECUTION_PLAN_NOT_RETAINED`, and `execute plan <64 zeros>` refuses with
`WRITES_NOT_RELEASED` at exit 3, before any credential.

## Evidence and limits

- Every check is hermetic and fixture-only. **No live Google Ads request** was
  made, no operating-system credential store was opened, no native helper or
  approval host ran, and no network socket was used.
- Nothing here proves that Google Ads accepts this mutation, that it
  acknowledges it in this shape, that the acknowledgement is idempotent or
  conditional, that the campaign stops delivering, or that spend stops.
- The write route, the operation shape, the `status` field-mask path, the
  request's `validateOnly`/`responseContentType` members, and the success
  envelope are **unsourced**: the v25 REST reference pages returned HTTP 404 or a
  navigation shell on 2026-09-09, and no Google Ads client library is a
  dependency of this repository, so no SDK version backs them either. See
  [wire-fact provenance](#wire-fact-provenance) for the per-fact status. Every
  unsourced assumption fails to `AMBIGUOUS_REMOTE_STATE` rather than to a silent
  accept.
- There is **no installed approval host**. The bridge refuses
  `COMPOSITE_EXECUTION_PLAN`, so in an ordinary build `execute plan` for a
  Google PAUSE record refuses before the plan is even loaded. Making it usable
  is A01's signed-host work, not P03's.
- Registering the pair makes `execute plan` *able* to reach Google Ads in a
  build that also installs a reviewed signed host. It is not a promotion of the
  Google Ads `PLAN_APPLY_SAFETY`, `MAINTAIN`, or `RECOVER` operation group; the
  [capability matrix](../capability-matrix.md) records it as one
  `FIXTURE_VERIFIED` subset row.
- The write barrier stays global across providers and accounts, and releasing it
  through `recover attempt` never settles the attempt or authorizes a retry.
- The readback record kind is registered and its history validator runs at both
  append and scan time; the adapter's admission check remains as a fail-closed
  guard, not as a pending dependency.

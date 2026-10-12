# Meta campaign PAUSE plan artifacts

Status: `IMPLEMENTING`.

`adhelm.meta_pause_plan_artifacts` creates deterministic local JSON, CSV, or static HTML views of one already retained Meta campaign PAUSE plan. It has no ledger, configuration, credential-store, provider, native-approval, or execution dependency. It does not create an authorization, refresh a plan, or claim that the provider accepted a pause.

The CLI owns durable source selection. Immediately before rendering it must use `load_meta_campaign_pause_plan` to select the exact retained record, compare the requested record/action/plan references and current checkpoint, and then pass the held action plus that loader provenance to the renderer. The renderer reconstructs the M40 action and verifies the complete loader-provenance shape, its action and plan digests, derived expiry/capability facts, false execution authority, and required current-kernel checks. Record and checkpoint digests are caller-supplied history facts at this internal handoff: this module does not open a ledger or independently prove them. A malformed or inconsistent mapping fails before review redaction or file publication.

The only document is `meta_campaign_pause_plan_review`:

```json
{
  "schema_version": 1,
  "artifact_kind": "meta_campaign_pause_plan_review",
  "source_evidence": {
    "source_record_digest": "…",
    "checkpoint_digest": "…"
  },
  "content": {
    "provenance": "closed retained-history facts",
    "historical_review": "static recorded-at view",
    "review": "existing MetaCampaignPausePlanReview",
    "recovery_limits": "reconcile then new exact authorization",
    "authority": "all execution and mutation controls false"
  }
}
```

The document embeds the existing closed `MetaCampaignPausePlanReview`; it does not copy its account, campaign, configured/effective status, transition, safety, or provider-caveat fields into a second provider model. The embedded review is rebuilt from the exact action record with `reviewed_at` fixed to retained `recorded_at`. It is a deterministic historical `as_of` view and states `NOT_EVALUATED_HISTORICAL_ARTIFACT`, whether the plan was expired at that historical instant, `CONFIGURATION_ONLY`/`PAUSED`, and `NOT_PREDICTED` effective delivery. Its opaque references exclude workspace/account/campaign/profile/app raw values, credential binding, principal, configuration/profile digests, required scope, token material, raw provider payloads, challenges, receipts, and keys.

The library resolves saved policy in the fixed context `plan` / `before_approval` / `plan_review` / `meta_ads` / selected profile. It also accepts an optional closed `ArtifactTaskOverride`, or an explicit decision helper, for one invocation. `ON_REQUEST` and suppressed policy remain file-free. An incomplete matching rule fails with `ARTIFACT_CONFIGURATION_REQUIRED` before source reload, export-key lookup, or rendering.

Before a plan is retained, the CLI uses `meta_campaign_pause_plan_artifact_failure_preview` to reserve the largest possible partial result, including version-9999 opaque artifact references. Once the exact retained source is reloaded, it calls `preflight_meta_campaign_pause_plan_artifact_output`. That helper creates the complete selected document with a fixed-width zero HMAC key and renders every selected format before the real export key or destination writer is accessed. The preflight enforces row and byte limits without opening an output directory.

`render_meta_campaign_pause_plan_artifacts` repeats closed action/provenance validation and zero-key preflight, then uses only the shared descriptor-relative, no-follow, fsynced, versioned writer. Identical bytes reuse an existing artifact; a changed or tampered collision gets a new version unless replacement was explicitly authorized. The renderer returns completed metadata and a fixed public failure code after a local partial write. A failed artifact never changes retained plan history, authorizes execution, creates a remote mutation, grants a retry, or turns `PAUSE` recovery evidence into an automatic rollback.

The focused tests use temporary ledgers and output directories with synthetic M40 actions only. They cover redaction, static as-of semantics, all formats, reuse, source/action reconstruction, bounded zero-key preflight, saved/task policy behavior, closed partial-failure output, and constructed-object result or document rejection. They make no provider, credential-store, native host, or execution call.

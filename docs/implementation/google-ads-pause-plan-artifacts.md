# Google Ads campaign PAUSE plan artifacts

Status: `IMPLEMENTING`.

`adhelm.google_ads_pause_plan_artifacts` creates deterministic local JSON, CSV, or
static HTML views of one already retained Google Ads campaign PAUSE plan. It has
no ledger, configuration, credential-store, provider, native-approval, or
execution dependency. It does not create an authorization, refresh a plan, or
claim that Google Ads accepted a pause.

The CLI owns durable source selection. Immediately before rendering it must use
`load_google_ads_campaign_pause_plan` to select the exact retained record,
compare the requested record/action/plan references and current checkpoint, and
then pass the held action plus that loader provenance to the renderer. The
renderer reconstructs the closed planned action and verifies the complete
loader-provenance shape, its action and plan digests, derived expiry and
capability facts, false execution authority, and required current-kernel checks.
Record and checkpoint digests are caller-supplied history facts at this internal
handoff: this module does not open a ledger or independently prove them. A
malformed or inconsistent mapping fails before review redaction or file
publication.

The only document is `google_ads_campaign_pause_plan_review`:

```json
{
  "schema_version": 1,
  "artifact_kind": "google_ads_campaign_pause_plan_review",
  "source_evidence": {
    "source_record_digest": "…",
    "checkpoint_digest": "…"
  },
  "content": {
    "provenance": "closed retained-history facts",
    "historical_review": "static recorded-at view",
    "review": "existing GoogleAdsCampaignPausePlanReview",
    "recovery_limits": "reconcile then new exact authorization",
    "authority": "all execution and mutation controls false"
  }
}
```

The document embeds the existing closed `GoogleAdsCampaignPausePlanReview`; it
does not copy its customer, campaign, configured-status, postcondition, safety,
or provider-caveat fields into a second provider model. The embedded review is
rebuilt from the exact action record with `reviewed_at` fixed to retained
`recorded_at`. It is a deterministic historical `as_of` view and states
`NOT_EVALUATED_HISTORICAL_ARTIFACT`, whether the plan was expired at that
historical instant, `CONFIGURATION_ONLY`/`PAUSED`, `NOT_PREDICTED` effective
delivery, `NOT_DOCUMENTED` provider conditional-write and idempotency semantics,
and that a configured pause does not prove effective delivery or zero residual
spend. Its opaque references exclude workspace, customer, login-customer,
campaign, profile, and client-application raw values, credential binding,
principal, configuration/profile digests, required scope, OAuth
material, raw provider payloads, and keys.

Because Google Ads campaign PAUSE has no installed approval host, the artifact
`authority` block records the same fixed `UNRELEASED`
status the installed `discover` contract reports, from the one shared
`GOOGLE_ADS_PAUSE_EXECUTION_COMMAND_STATUS` constant. P03 registered the
execution route ([execution contract](google-ads-pause-execution.md)); it still
refuses at the uninstalled approval bridge, and the artifact never states a
pending, staged, or available execution path.

The library resolves saved policy in the fixed context `plan` /
`before_approval` / `plan_review` / `google_ads` / selected profile. It also
accepts an optional closed `ArtifactTaskOverride`, or an explicit decision
helper, for one invocation. `OFF`, `ON_REQUEST`, a nonmatching provider, profile
or stage, and a disabled purpose all remain file-free on the saved-rule path. A
disabled purpose does not currently block an *explicit* one-task request: the
shared `resolve_artifact_policy` task-override branch never compares the context
purpose against the saved purposes, so this is a shared artifact-policy boundary
inherited by Meta, GA4 and OpenAI rather than a Google-specific behavior, it is
tracked by the U01 artifact-policy ticket, and
`tests/test_google_ads_pause_plan_artifacts_review.py` keeps it visible as a
strict `xfail`. An incomplete matching rule fails with
`ARTIFACT_CONFIGURATION_REQUIRED` before source reload, export-key lookup, or
rendering.

Before publication the CLI uses
`google_ads_campaign_pause_plan_artifact_failure_preview` to reserve the largest
possible partial result, including version-9999 opaque artifact references. Once
the exact retained source is reloaded, it calls
`preflight_google_ads_campaign_pause_plan_artifact_output`. That helper creates
the complete selected document with a fixed-width zero HMAC key and renders every
selected format before the real export key or destination writer is accessed. The
preflight enforces row and byte limits without opening an output directory.

`render_google_ads_campaign_pause_plan_artifacts` repeats closed
action/provenance validation and zero-key preflight, then uses only the shared
descriptor-relative, no-follow, fsynced, versioned writer. That shared writer
also supplies the POSIX-only refusal: a platform without `O_NOFOLLOW`, directory
descriptors, or descriptor-relative link/rename support fails with
`UNSAFE_ARTIFACT_DESTINATION` when the destination is opened. The synthetic
zero-key preflight has already built and rendered the complete document in
memory by then; the refusal precedes the real sharing key, any writer, and any
byte reaching the filesystem, not every document construction. Identical inputs
reuse
an existing artifact byte-for-byte; a changed or tampered collision gets a new
version unless replacement was explicitly authorized. The renderer returns
completed metadata and a fixed public failure code after a local partial write. A
failed artifact never changes retained plan history, authorizes execution,
creates a remote mutation, grants a retry, or turns `PAUSE` recovery evidence
into an automatic rollback.

## Command

```text
artifacts google-ads-campaign-pause RECORD_DIGEST --input REQUEST.json
```

`REQUEST.json` uses the closed
`google-ads-campaign-pause-plan-artifact-render` schema. It contains only schema
version 1, a nonempty unique set of `json`, `csv`, and/or `html` formats, an
optional local output directory (otherwise the saved destination is used), and
an optional explicit replacement choice. It cannot contain an action, a
source/provenance mapping, a provider response, a credential, an approval, or a
generic document body. The installed document schema is
`google-ads-campaign-pause-plan-artifact-document`.

The artifacts group rejects the request file before it reads configuration,
local history, a sharing key, or a destination. It then loads exactly the
selected retained plan, revalidates its closed action and loader provenance, and
requires the selected record digest to match. The command resolves an explicit
one-task artifact decision and renders every complete document with a fixed
synthetic HMAC key before it reads the real sharing key or opens the output
directory. It also fits the full result with the largest allowed partial artifact
reference set under the saved output bound.

Immediately before the real export-key and writer handoff the command reloads the
same record and requires a private canonical snapshot of the action, loader
provenance, and derived artifact provenance — including the checkpoint digest —
to match. A changed source or checkpoint does not reach the writer. This is an
exact local retained-history check; it does not claim a cross-process ledger
transaction or current provider state.

A file write can partially complete. The result then remains `PARTIAL`, keeps
every verified completed artifact reference, and uses a fixed public failure
code. It does not retry planning or publication, open a provider path, grant
execution authority, or treat the artifact as an approval.

`plan google-ads-campaign-pause create` also consumes this renderer, exactly as
the Meta create route does. It resolves the same closed policy before the
protected planner, reserves a full receipt plus the longest allowed partial
artifact result, and — when the policy resolves to `GENERATE` — reloads the
retained record after the durable append, binds it to the receipt digests, runs
the synthetic-key preflight, re-reads the source immediately before the real key
and writer, and renders. `ON_REQUEST`, `OFF`, a nonmatching rule and
`--no-artifacts` leave the receipt file-free. A matching rule that names no
complete local destination fails with `ARTIFACT_CONFIGURATION_REQUIRED` before
the planner, and that diagnostic names this command as the alternative. An
inline artifact failure downgrades the result to `PARTIAL` and keeps the durable
plan receipt and every completed file reference; it never discards the retained
plan. The command above remains the way to render, re-render, or reuse output
from an already retained plan.

`GOOGLE_ADS_PAUSE_PLAN_ARTIFACTS_UNSUPPORTED` no longer exists. The F01 refusal
it named was accurate only while no renderer was installed.

## Evidence limits

- Fixture-only. Every test uses temporary ledgers, temporary output directories,
  fake protected-store and HTTP boundaries, and synthetic actions.
- No live Google Ads request, real OAuth grant, or real
  operating-system credential store is exercised.
- No execution, approval, native host, or provider mutation path exists for
  Google Ads campaign PAUSE; that remains P03 work.
- A rendered artifact is historical local evidence. It does not prove current
  remote state, provider acceptance, effective delivery, or zero residual spend.
- POSIX-only publication is evidenced on Linux. Windows behavior is the shared
  writer's documented fail-closed refusal, not a validated platform path.

## Validation

```sh
uv run pytest -q \
  tests/test_google_ads_pause_plan_artifacts.py \
  tests/test_google_ads_pause_plan_artifacts_ultra_review.py \
  tests/test_google_ads_pause_plan_artifact_cli.py \
  tests/test_google_ads_pause_plan_artifact_cli_ultra_review.py \
  tests/test_google_ads_pause_plan_artifact_policy.py \
  tests/test_google_ads_pause_plan_artifact_integration.py \
  tests/test_google_ads_pause_plan_artifacts_review.py \
  tests/test_google_ads_pause_cli.py
```

These tests do not contact Google, invoke a real operating-system credential
store, launch a native host, request approval, or execute a campaign change.
`scripts/validate_development.py` additionally runs an installed-wheel smoke that
proves the closed request/document schemas and the no-retained-source refusal
without credentials or network access.

References: [planning CLI](google-ads-pause-cli.md),
[retained history](google-ads-pause-plan-history.md),
[redacted review](google-ads-pause-plan-views.md),
[shared artifact contract](artifacts.md),
[the Meta profile this mirrors](meta-pause-plan-artifacts.md).

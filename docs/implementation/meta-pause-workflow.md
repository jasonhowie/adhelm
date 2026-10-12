# Meta campaign PAUSE planning workflow

`adhelm.meta_pause_workflow` prepares and retains one closed M40 Meta campaign
PAUSE plan. It is an implementing, synthetic-tested planning path. It does
not authorize a pause, invoke native approval, call a Graph mutation endpoint,
or establish live provider write eligibility, idempotency, conditional-write,
or delivery evidence.

The only public workflow input is
`MetaCampaignPausePlanRequest(schema_version=1, profile_id, campaign_id,
expires_in_seconds)`. `campaign_id` is an ASCII nonzero native decimal ID and
the requested lifetime is an exact integer from 60 through 900 seconds. The
entry point is:

```python
plan_configured_meta_campaign_pause(
    directory,
    request,
    expected_configuration_digest=None,
) -> RetainedMetaCampaignPausePlan
```

`expected_configuration_digest` is composition data for a caller that already
read local output policy; it is not part of the request schema or an agent
input. The retained result is internal and contains an M40 action plus its
retained record digest. A caller must reload the record and use the closed M40
historical view before producing agent-visible output.

Before a protected bundle or provider read, the workflow validates the request,
rejects explicit CI (`CI=1` or `CI=true`), captures one configuration digest,
selects exactly one Meta token EDIT profile with `ads_management`, and rejects
unresolved mutation history. The M39 fixed EDIT session then performs its
existing inspected-bundle and account proof before the one fixed campaign-state
GET. The workflow uses the binding's `observed_at` as the M40 observation start
and samples the local clock after the campaign GET as its finish. The resulting
interval therefore covers completed token inspection, account proof, and the
fixed campaign read; it is not a provider occurrence timestamp.

The action binds the M39 configuration/profile/credential continuity facts,
the configured account identity, exact campaign baseline, both finite token
expiry boundaries, the mandatory ledger workspace, and the requested plan
lifetime. `ACTIVE` is the only accepted configured campaign status. The plan
will fail rather than shorten a requested lifetime that exceeds either
inspected expiry. Its postcondition remains configuration-only `PAUSED` with
effective delivery `NOT_PREDICTED`.

After all provider reads complete, the workflow enters M39's owner-thread
retention recheck and calls `retain_meta_campaign_pause_plan` once. Its
ledger callback only rechecks already-held configuration/credential state and
the local clock. It does not read the ledger, perform network I/O, acquire a
new credential or configuration lock, or use an approval/authorization path.
A final continuity or clock failure leaves no Meta PAUSE plan record. A local
retention `OSError` is reported as `META_PAUSE_PLAN_RETENTION_UNVERIFIED`; the
completed read must be inspected through retained history before a deliberate
new plan attempt.

Artifact policy and output limits belong to the CLI composition layer. It must
resolve the existing `ArtifactTaskOverride` and saved output preferences, and
preflight the entire bounded receipt plus worst-case partial artifact result
before this workflow opens the M39 session. A resolved request can create only
the static redacted historical view after durable retention and exact retained
source reload; it does not change the workflow's provider-read, retention, or
non-authorizing plan semantics. Suppressed or unrequested policy leaves the
retained plan file-free.

The base suite is synthetic only:

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_meta_pause_workflow.py
# 12 passed
```

It covers strict input and CI refusal before configuration/session work, exact
EDIT profile selection, unresolved-history refusal, M39 account/campaign facts,
active-only planning, non-shortened lifetime bounds, final retention continuity,
and ambiguous local retention writes. No test invokes a credential store,
provider, OS helper, native host, or mutation endpoint.

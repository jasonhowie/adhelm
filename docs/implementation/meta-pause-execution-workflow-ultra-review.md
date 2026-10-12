# Meta PAUSE installed execution workflow independent review

Status: `REVIEWED`.

> Historical for edition checks. The configured edition was removed in O3.2:
> where this review lists an operator-edition check, the
> [release-policy gate](release-policy-gate.md) now refuses at the workflow
> entry before any of the other steps.

This review covers M52's private retained-plan execution workflow. It uses
temporary ledgers, a fake Meta credential/session boundary, fake HTTPX
responses, synthetic direct grants, and the root's fake compiled-binding/pipe
integration. It does not import a configured wheel, start a helper or XPC
service, access a real credential store, call Graph, or claim a live provider
or native-host result.

## Boundaries verified

`MetaCampaignPauseExecutionRequest` accepts only schema version `1` and a
lowercase 64-hex retained-record digest. The workflow reparses an exact typed
request before any runtime activity. A low-level allocated request with missing
model state returns `INVALID_META_CAMPAIGN_PAUSE_EXECUTION_INPUT` before the
compiled readiness gate, ledger, configuration, credential session, helper, or
HTTP boundary.

After that strict parse,
`_require_compiled_meta_campaign_pause_approval_bridge_v2_ready()` is the first
runtime gate. The ordinary generated resolver returns no binding, so it returns
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED` before retained
source, configuration, protected-store, helper, or provider activity.

When a reviewed static binding is injected only by a fake integration test, the
workflow reloads and revalidates the exact retained M40 action. It requires its
workspace/profile, one Meta PAUSE subplan, account and EDIT binding facts,
unexpired plan/token windows, no unresolved history, the configured operator
edition, and the exact current Meta EDIT profile/configuration digests. The
same source, expiry, recovery, edition, and configuration checks run again
after approval and before the adapter can open its protected session.

The accepted value must be exactly an `AcceptedNativeApproval` for
`META_CAMPAIGN_PAUSE_PLAN`, with a `DirectGrant`, no policy, and exact plan and
subplan digests. The workflow calls
`SafetyKernel._ensure_durable_authorization()` after the post-approval recheck
and before `_configured_meta_campaign_pause_adapter()` opens the M51 EDIT
session. A shape-valid copied grant with a changed durable identity, and a
shape-valid but unrecorded grant, both stop at that boundary with
`USER_APPROVAL_HOST_REQUIRED` and zero provider work.

The workflow holds no configuration or credential lock across native approval
or HTTP and adds no callback to ledger append paths. `SafetyKernel` owns the
durable `APPLY_STARTED` append and the issued permit. The M51 adapter checks
that durable start, consumes that permit only at its fixed POST boundary, owns
the final local continuity checks and provider/readback work, and owns its
retention callback; that callback rechecks held local facts without ledger or
network reentry.

## Independent cases

The ten independent fake-only cases cover:

- a low-level missing-slot request before readiness;
- expiry and an invalid clock after a durable synthetic approval;
- post-approval operator-edition and recovery-barrier drift;
- copied same-plan durable-identity drift and an unrecorded valid direct grant;
- policy and non-Meta-kind result substitution;
- a non-exact acknowledgement becoming an ambiguous one-POST result, followed
  by `RECOVERY_REQUIRED` before a second approval or POST.

The owner base cases cover default binding refusal, pre-approval profile and
expiry gates, configuration drift after approval, and a wrong plan-bound grant.
The root's three composition cases use only a module-issued full-18 static
binding and synthetic V2 framed pipe. They verify that the M51 EDIT session
opens only after the durable approval record and one synthetic native reply;
exact ACK succeeds while unknown/lost acknowledgement paths remain ambiguous
and replay is blocked.

No open source finding remains in this bounded workflow review.

## Limits

This is private offline composition. The M51 acknowledgement subset remains a
conservative local rule, not documented Graph-v26 confirmation semantics. The
workflow does not prove an installed artifact, native host origin, helper
availability, provider-side idempotency, a delivery/spend outcome, or live
provider support. It accepts no receipt, key, candidate, adapter, transport,
clock, credential, configuration digest, or grant from a caller.

## Validation

The immediate workflow group passed 20 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m52-uv-cache uv run pytest -q \
  tests/test_meta_pause_execution_workflow.py \
  tests/test_meta_pause_execution_integration.py \
  tests/test_meta_pause_execution_workflow_ultra_review.py
```

The independent test file passed Ruff and format checks. The owner additionally
reported the expanded M46--M52 dependency group at 102 passing tests, with
strict mypy for the affected source and scoped Ruff/format checks passing.

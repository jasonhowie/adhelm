# Meta campaign pause contract

Status: local, fixture-tested plan reconstruction only. This contract creates no
HTTP request, reads no configuration or credential, writes no ledger record,
and grants no execution authority.

`src/adhelm/providers/meta_pause_contract.py` defines one closed Meta Graph
`v26.0` campaign configured-status transition:

```text
ACTIVE configured status -> PAUSED configured status
```

It does not model an activation, budget change, account eligibility decision,
or effective-delivery prediction. A later guarded adapter needs a separate
reviewed promotion for its fixed POST, durable history, exact authorization,
fresh readback, and ambiguous-result handling.

## Public pure contract

The module exposes these immutable serializable types:

| Type | Retained facts |
| --- | --- |
| `MetaCampaignPauseWorkspaceBinding` | Mandatory local workspace profile reference only. |
| `MetaCampaignPauseAccountBinding` | Allowlisted Meta account reference, native `act_` reference, timezone, currency, canonical unsigned account-status text, and API version. |
| `MetaCampaignPauseExecutionBinding` | EDIT profile/application/derived-principal association, exact `ads_management` scope, credential/configuration digests, and inspected/token/data-access expiry times. |
| `MetaCampaignPauseCampaignBaseline` | Campaign and native account association, fixed `ACTIVE` configured status, and one allowlisted effective status. |
| `MetaCampaignPauseObservationBinding` | The closed campaign baseline plus a bounded sequential read interval. |
| `MetaCampaignPauseConfigurationPostcondition` | The unchanged account/campaign association with `configured_status=PAUSED` and `effective_delivery=NOT_PREDICTED`. |
| `MetaCampaignPausePlannedAction` | All bindings, the postcondition, static limitations, and one reconstructed `CompositePlan`. |

`build_meta_campaign_pause_plan(...)` accepts a mandatory workspace binding,
closed account/execution/observation bindings, and a requested expiry. It
returns `MetaCampaignPausePlannedAction`. The companion
`revalidate_meta_campaign_pause_planned_action(value)` strictly reparses a
persisted action and reconstructs every bound value. `action.to_data()` returns
only the validated closed JSON data.

The execution binding retains no access token, inspector token, unknown granted
scope, provider payload, or raw user identifier. The principal is a private
derived reference and the credential material is represented only by its fixed
binding digest.

## Exact plan binding

The builder derives a full 64-hex SHA-256 `action_id` from every outer fact:
workspace binding, account baseline, EDIT binding, campaign observation and
read interval, configuration-only postcondition, exact plan expiry, and fixed
capability semantics. The full ID appears untruncated in the plan and subplan
IDs, so the `CompositePlan` digest commits all of those facts without adding a
generic Meta account-identity variant.

The nested generic `AccountIdentity` is reconstructed only from the fixed Meta
facts: account reference/native `act_` ID, exact EDIT profile, derived
principal, client application, and the singleton `ads_management` scope.

The single subplan has:

- provider `meta_ads`, operation `PAUSE`, API version `v26.0`, and fixed
  `graph.facebook.com` egress;
- zero incremental spend in the observed account currency;
- one field change at `campaign/configured-status`;
- `recovery_actions=(PAUSE,)`; and
- a recovery meaning of reconciliation followed by a new exact authorization.

That recovery candidate never permits an automatic rollback or retry. A
timeout, malformed acknowledgement, or uncertain post-write state must remain
ambiguous until a later reconciliation; a matching later `PAUSED` observation
cannot attribute the state to the earlier attempt.

## State and freshness rules

The before `StateRef` fingerprint covers only normalized remote facts: the
complete allowlisted account baseline and campaign baseline, including both
configured and effective statuses. It deliberately excludes workspace,
credential/configuration facts, inspection/token times, and the read interval,
so a fresh local read is not false drift solely because its provenance time
changed. `StateRef.revision` is `null`: this contract has no provider revision
or compare-and-swap token.

The after fingerprint covers only the closed configuration postcondition. It
requires the same complete account metadata and exact campaign/account
association, `PAUSED` configured status, and
`effective_delivery=NOT_PREDICTED`. It does not assert unchanged effective
status, immediate delivery stop, zero residual billing, or instant zero spend.
A future adapter must retain the actual post-write effective status separately
and preserve an ambiguous acknowledgement rather than manufacturing a
confirmed result.

The read interval must be ordered and no longer than five minutes. The EDIT
inspection must precede the read. Both inspected expiries must remain strictly
after the completed read. The plan expiry must be strictly after the completed
read and no later than either inspected expiry or fifteen minutes after the
completed read. These are local freshness limits, not a statement about Meta
provider expiration, delivery, or update semantics.

Only an `ACTIVE` configured campaign baseline is accepted. Other configured
states, mismatched campaign-account identities, unsupported effective-status
tags, malformed native IDs, and invalid currencies/timezones fail before a plan
exists. Account status is retained as canonical unsigned numeric text; the
contract never maps it to update eligibility.

## Explicit limitations

The action fixes these values to avoid overstating authority:

```text
execution_authority = false
live_validated = false
conditional_write_proof = false
idempotency_proof = false
provider_write_permission = NOT_PREPROVABLE
```

The false proof fields mean this local contract has no evidence of those
provider properties. They do not claim Meta lacks a conditional-write or
idempotency feature. The plan also does not establish current account access,
app review, mutation permission, native approval support, transport behavior,
or live provider capability.

## Input discipline and tests

Revalidation preserves original JSON scalar types before strict parsing, then
canonicalizes only valid data for comparison. It rejects boolean/float aliases
for schema versions and nested money, noncanonical UTC wire text, extra fields,
constructed/copy-mutated bindings, and an altered plan, postcondition, action
ID, or fixed capability control.

`tests/test_meta_pause_contract.py` is fake-only and exercises exact plan
reconstruction, full action-ID binding, remote-only before/after fingerprints,
ACTIVE-only eligibility, bounded read/expiry rules, strict raw scalar parsing,
and recovery/authority limitations. It does not call Meta, a credential store,
ledger, CLI, native approval host, or mutation adapter.

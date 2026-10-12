# Private Meta campaign PAUSE execution workflow

`MetaCampaignPauseExecutionRequest` selects one retained
`META_CAMPAIGN_PAUSE_PLAN` by its lowercase SHA-256 record digest. Its only
entry point, `execute_retained_meta_campaign_pause_with_installed_approval`,
does not accept a receipt, public key, native candidate, helper path, adapter,
transport, clock, configuration digest, credential, or grant.

After strict request reconstruction, the workflow calls
`_require_compiled_meta_campaign_pause_approval_bridge_v2_ready()` before it
opens a ledger, reads configuration, opens protected credentials, constructs a
helper adapter, or makes HTTP activity. The checked-in generated route has no
binding, so ordinary source returns
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED` at that boundary.

When a separately reviewed compiled binding is available, the workflow loads the
exact retained action in its local workspace and rejects expired plans or
unresolved starts and composite runs. It then verifies the exact
customer-declared `MetaAccountTokenProfile`: the same EDIT profile ID, OS
Meta-token source, EDIT purpose with `ads_management` scope, account, client
application, currency, timezone, whole configuration digest, and profile digest.
This is configuration-only preflight; it does not read a credential.

The private V2 handoff records an approval and must return only an
`AcceptedNativeApproval` for `META_CAMPAIGN_PAUSE_PLAN` with an exact
`DirectGrant`, matching complete plan and subplan digests, and no delegation
policy. The workflow reloads the retained source and local configuration after
that handoff, then calls `SafetyKernel._ensure_durable_authorization` before it
opens the M51 fixed Meta adapter. The existing kernel performs its locked final
preflight, appends `APPLY_STARTED`, and supplies the only usable egress permit.

The adapter remains responsible for fresh protected-token/account/campaign
checks, the one fixed `status=PAUSED` POST, local acknowledgement subset, and
dedicated factual readback record. This workflow makes no claim that the
offline SDK-derived request shape proves live provider acceptance, delivery,
or residual-spend behavior.

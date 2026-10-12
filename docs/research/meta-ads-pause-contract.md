# Meta Ads campaign-pause contract research — 2026-09-06

This note narrows the contract that a future guarded Meta campaign-pause
adapter must implement. It records source observations only. No Meta request,
credential read, SDK dependency, plan, native-approval kind, or mutation
adapter was added while preparing it.

## Sources and retrieval status

| Source | Retrieval on 2026-09-06 | What it establishes |
| --- | --- | --- |
| Meta's tagged [Business SDK 26.0.1 release](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.1) and tagged [API configuration](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/apiconfig.py) | Available | The reviewed generated SDK tag identifies API `v26.0` and SDK `v26.0.1`. It is version evidence for a proposed fixed endpoint, not a live capability claim. |
| Tagged [Campaign generated object](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.1/facebook_business/adobjects/campaign.py) | Available | The generated Campaign field set includes `id`, `account_id`, `configured_status`, `effective_status`, and `status`. Its node `api_update` operation is a `POST` and accepts `status` from the Campaign status enum, which includes `PAUSED`. |
| Meta's [manage-campaigns guide](https://developers.facebook.com/docs/marketing-api/get-started/manage-campaigns) | Direct retrieval was unavailable in this research environment. The local 2026-09-05 Context7 record of this official page reports a campaign-node POST with `status=PAUSED` and a boolean success acknowledgement. | This is supporting documentation, not a re-fetched primary response. The future implementation must retain the fixed node POST only after its own source review. |
| Meta's [Marketing API authorization guide](https://developers.facebook.com/docs/marketing-api/get-started/authorization) and the official [Business SDK README](https://github.com/facebook/facebook-python-business-sdk/blob/main/README.md) | The direct developer-doc retrieval was unavailable; the official SDK README was available through the repository index. | The README says a Marketing API user token needs `ads_management`. It does not prove that a particular customer app, user, account, or campaign is usable. |
| Meta's [debug-token reference](https://developers.facebook.com/docs/graph-api/reference/debug_token) | Direct retrieval returned a rate-limit response in this environment. M37 recorded the previously indexed official contract and implements the bounded inspector separately. | It does not change the M37 rule: inspection proves app/user/scope/finite expiry facts only, never account access or mutation authority. |

The inaccessible developer pages are not evidence that the corresponding API
operation is unsupported. They are a reason to retain the uncertainty below.

## Observed contract facts

The tagged generated Campaign source supports this limited data model:

- a campaign node exposes its own ID, account ID, configured status, effective
  status, and mutable status;
- `PAUSED` is a member of the configured-status and status enums; and
- the generated node-update operation uses `POST` and admits `status`.

The local Context7 record of Meta's official guide additionally describes
posting `status=PAUSED` to an existing campaign node and receiving a boolean
success acknowledgement. The exact response body, transport encoding, and
provider error shape were not independently fetched on 2026-09-06, so they
must be parsed as a closed future contract rather than accepted generically.

The official SDK README associates Marketing API management with a user token
and `ads_management`. This is the least-privilege target for the existing
customer-declared `EDIT` Meta account-token profile. It remains necessary to
inspect the configured token and prove its app, USER subject, granted scope,
and finite expiry immediately before a future fixed account/campaign read.

## Required future adapter boundary

The existing `MetaAdsClient` remains read-only and cannot be extended by a
generic `POST`. It currently reads only `effective_status`, while a pause
requires the distinction between configured intent and effective delivery.
The smallest complete follow-up is a separate private EDIT-only session and a
closed `MetaCampaignPause` contract, with these phases:

1. Reuse the M37 one-bundle lifecycle only through a dedicated EDIT boundary.
   It must inspect the subject with `ads_management`, prove the declared app
   and USER identity, retain no inspector token, and recheck the full
   configuration/profile/bundle/finite-expiry continuity before every fixed
   provider request.
2. Read the declared account first, then read exactly one customer-selected
   campaign node with the allowlist `id,account_id,configured_status,
   effective_status`. Require its account ID to equal the proved account. A
   closed plan binds the normalized account identity, profile and credential
   binding references, campaign ID, and the complete allowlisted baseline.
3. Permit only a deliberate `ACTIVE` configured-status to `PAUSED`
   configured-status transition. `effective_status` remains an observed
   delivery fact; the plan and review must not claim that a pause instantly
   eliminates spend or that any inherited/effective state proves the intended
   configuration change.
4. Build `before` and `after` state fingerprints from the entire normalized
   baseline. The provider does not currently supply a reviewed revision or
   conditional-write token, so a baseline digest may be used for local drift
   detection but must not be represented as a provider compare-and-swap
   guarantee.
5. Require `SafetyKernel` durable `APPLY_STARTED` and an exact one-use guarded
   egress permit before a fixed campaign-node POST. Parse only the reviewed
   success shape, then issue a fresh allowlisted campaign GET. A confirmed
   result needs the expected configured `PAUSED` state and redacted evidence
   references.
6. Treat a timeout, malformed acknowledgement, transport failure after egress,
   or any uncertain POST result as `AMBIGUOUS_REMOTE_STATE`. A later matching
   `PAUSED` observation cannot attribute that state to this attempt, so it
   cannot authorize a blind retry or upgrade ambiguity to a confirmed write.

No configuration or credential lock should span HTTP. A future durable-history
recheck must take configuration then the fixed credential lock before ledger
locking, just as the M38 read session does.

## Remaining primary-source gaps

The tagged SDK is useful generated-contract evidence, but it does not settle
the following provider semantics:

- the exact Graph v26 campaign-node read and update response schemas;
- the exact authorization/app-review requirements for a customer-owned app and
  account beyond the observed `ads_management` label;
- whether a documented conditional update, ETag/revision, or idempotency
  parameter exists for this operation; and
- the precise timing and meaning of `effective_status` after a configured
  pause.

The observed `api_update` parameter list does not show a conditional or
idempotency field. That omission is **not proof that Meta offers none**. Until
an official Graph reference establishes one, the implementation must behave as
though no provider-side idempotency or conditional-write proof is available and
preserve ambiguous-write recovery.

The current native approval wire admits only the reviewed OpenAI pause and GA4
CREATE challenge kinds. A Meta pause record, direct challenge/review surface,
and installed bridge binding therefore require a separate reviewed promotion
after the fixed provider contract; this note does not add them.

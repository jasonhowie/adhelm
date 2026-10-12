# Meta campaign PAUSE adapter independent review

Status: `REVIEWED`.

This review covers the M51 private, fixed Meta campaign `ACTIVE` to `PAUSED`
adapter, its bounded form transport, and its factual readback history. It uses
temporary ledgers, a fake HTTPX handler, synthetic direct grants, and a fake
Meta credential/session boundary. It does not call Graph, access a real
credential store, start a native helper or XPC service, or claim a configured
or live provider capability.

## Boundaries verified

`_configured_meta_campaign_pause_adapter(directory, action, ledger)` is a
private factory with no caller-selected token, client, URL, body,
acknowledgement parser, retry setting, or clock. It rejects a ledger rooted at
another workspace or carrying another profile before it opens the EDIT session
or credential boundary. Its issued POST capability is bound again to the same
session core, native campaign ID, plan digest, and subplan digest before it can
be used.

The only mutation handoff is an EDIT-only fixed `POST` to the native campaign
node with the bounded form `{"status": "PAUSED"}`. The core takes a fresh
protected-token snapshot and repeats the strict one-attempt transport check
immediately before that POST. A final baseline response that sets a cookie
therefore cannot add ambient state to the mutation request: the permit has
already been consumed, no POST occurs, and the outcome remains ambiguous.

Before permit consumption, a missing, inactive, malformed, or non-`EgressPermit`
argument is the fixed local `KERNEL_EGRESS_PERMIT_REQUIRED` refusal and makes
zero POSTs. Once the issued capability consumes an active permit, dispatch,
transport, acknowledgement, readback, continuity, or append faults are
`AMBIGUOUS_REMOTE_STATE`; the adapter does not retry. A later matching
configured `PAUSED` read cannot attribute the prior write and reconciliation
remains unsettled.

The local acknowledgement subset accepts only exact HTTP 200 and bounded,
duplicate-key-safe JSON consisting solely of a real Boolean
`{"success": true}`. It then requires a fresh account-bound campaign readback
whose configured status is `PAUSED`. The observed effective status is retained
as a separate factual value. It is neither predicted to be `PAUSED` nor used
as a spend or delivery claim.

After the local acknowledgement/readback path, the adapter appends the
dedicated `META_CAMPAIGN_PAUSE_READBACK_OBSERVATION` record through the session
retention callback. It binds the retained action, plan, subplan, account,
campaign, envelope, and the matching durable `APPLY_STARTED` attempt. This
avoids polluting the inventory `RESOURCE_SNAPSHOT` baseline with a partial
configured-status observation. The record preserves the actual effective
status while excluding raw acknowledgement data and credentials. Exported
references are bounded/redacted; the adapter's local evidence reference is a
SHA-256 digest binding, not an HMAC or a provider acknowledgement proof.

## Repairs verified during review

- The early fixed POST capability needed an exact EDIT-policy check. The final
  core rejects a READ context before credential inspection or route selection.
- An independently issued, valid capability for another campaign could have
  crossed the adapter boundary. The adapter now binds the capability's core,
  campaign, plan, and subplan to its revalidated action before provider work.
- The factory initially accepted an independently constructed ledger from a
  different root or profile. It now rejects that mismatch before the session
  factory runs.
- Permit classification initially risked treating local permit errors as
  ambiguous. The fixed handoff keeps non-`EgressPermit` and inactive-permit
  failures outside the post-consumption ambiguity boundary; only a failure
  after consumption is uncertain egress.
- A final campaign GET could mutate the shared HTTPX client's cookie state
  before the mutation. The immediate strict transport revalidation now blocks
  the POST and retains ambiguity after the spent permit.
- A proposed generic resource snapshot would have shared the existing
  inventory baseline key and manufactured false inventory drift. The dedicated
  readback record instead binds the exact earlier action/start and retains the
  factual effective status separately.

No open source finding remains within this bounded M51 review.

## Limits

The exact-200 / sole-real-Boolean acknowledgement rule is a conservative
offline product subset, not documented Graph-v26 acknowledgement semantics.
It does not prove provider-side idempotency, conditional update behavior,
campaign eligibility, delivery cessation, spend cessation, or a live support
claim. Every post-egress form outside the subset remains ambiguous and
non-retryable. The versioned-provider evidence and promotion limits are
recorded in [the local acknowledgement subset](../research/meta-pause-local-acknowledgement-subset.md).

## Validation

The independent file contains 14 fake-only cases covering malformed success
aliases, duplicate and extra JSON members, wrong ledger root/profile before
session entry, substituted issued capability, inactive permit refusal,
post-ack identity drift, append interruption, and the final-GET cookie
mutation boundary.

The final focused group passed 74 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m51-uv-cache uv run pytest -q \
  tests/test_meta_campaign_pause_adapter.py \
  tests/test_meta_campaign_pause_adapter_ultra_review.py \
  tests/test_meta_pause_adapter_pipe_integration.py \
  tests/test_meta_pause_readback_records.py \
  tests/test_transport_form.py
```

Ruff and format checks passed for the independent test file. Root separately
reported strict mypy passing for the affected product source; no claim is made
that legacy test-import diagnostics are newly clean.

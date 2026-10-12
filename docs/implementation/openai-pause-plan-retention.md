# OpenAI pause-plan retention

## Scope

This library-only slice retains one exact
`OpenAIPausePlannedAction` as local `EXECUTION_PLAN` history. It introduces no
CLI command, credential lookup, provider request, authorization decision, or
execution path. The action remains fixed at
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.

The extracted `providers/openai_pause_contract.py` contains only the closed
pause baseline, binding, state fingerprint, plan reconstruction, and action
model. `openai_pause.py` re-exports the existing public action and binding
imports, so the planner, adapter, and pause review retain their prior action,
plan, and subplan digests.

## Stored record and envelope binding

`OpenAIPauseExecutionPlanRecord` is a closed payload with all of these fields:

- the complete reconstructed action, action digest, plan digest, expiry, and
  maximum incremental spend;
- the fixed fixture-only capability status;
- `HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE`;
- `execution_authority = false`; and
- `requires_current_kernel_checks = true`.

It rejects extra fields, missing nested baseline or plan fields, malformed or
incomplete constructed/copy-derived nested values, mismatched action/plan
digests, altered spend or expiry, and alternate JSON numeric/boolean forms for
fixed action and plan controls. A complete valid copy round-trips through the
same closed reconstruction.
Python mappings must already contain JSON-shaped values; bytes, `Decimal`, and
custom objects cannot be silently serializer-coerced into an action. The action
binding also requires an IANA account timezone. This is a closed record for the
fixed pause action, not a generic native-payload store.

The ledger permits this record only in `changes` with provider `openai_ads`,
source `LOCAL_CLI`, no run ID, and no occurrence or observation timestamp. Its
envelope account must equal the action binding account. The ledger profile is
compared to the plan's workspace profile, while the native account key's
authentication profile is independently compared to the action binding profile.
These checks run before directory creation on append and again during verified
stream scans, so rehashing a modified envelope does not make it readable.

`recorded_at` is the local time that the history line was retained. It does not
claim a remote event time, observation time, or actor. Old JSONL bytes are not
rewritten; normal segment rotation and checkpoint verification remain in use.
The append path writes its already validated detached payload, preventing a
caller mutation after validation from changing the retained action.

## Retrieval and sharing boundary

`retain_openai_pause_plan` appends through `Ledger`. `load_openai_pause_plan`
and `openai_pause_plan_catalog` start with `Ledger.checkpoint()`, so they read
only a verified checkpoint. Loading returns the complete typed action for local
library review plus digest/expiry/status provenance. Catalog pages are bounded
to 1–500 records and include only record/action/plan digests, local record time,
expiry, capability/evidence status, zero spend, and the false-authority/current-
kernel flags. Provenance and catalog timestamps are JSON UTC strings. They omit
the action, account/profile/campaign identifiers, and credential-binding
reference.

The generic verified export redacts reference-shaped action fields. The retained
action contains no credential secret, raw provider request/response, or general
payload field. The HMAC credential-binding reference remains only in the closed
local action and is pseudonymized for export.

## Authority and current-state boundary

Retention neither proves current provider identity/state nor grants execution.
An expired plan stays readable with its exact expiry so history remains truthful.
Any future operation must still obtain current kernel authorization, re-read
identity and baseline state, reject drift, record its own durable start, consume
its one-use permit, and verify or reconcile the provider result.

Newly built actions carry an explicit local workspace binding, and their plan
profile must equal that workspace ledger profile. Their selected credential
profile remains separately bound to the native account key. Legacy actions
without the binding retain their historical credential-profile plan routing and
their original bytes. A pause workflow must explicitly pass the existing
workspace ledger profile for new actions; it must not silently create a
different ledger or weaken either profile binding.

## Offline validation

`tests/test_saved_openai_pause_plans.py` covers digest compatibility, strict
reconstruction, constructed-model and nested scalar bypasses, envelope and
rehash tampering, profile/account/source/time binding, mutable input replacement
during append, rotation, checkpoint-only loading, catalog bounds, old-segment
byte preservation, and redacted generic export. The tests use fixture transports
only and make no provider request or live effect.

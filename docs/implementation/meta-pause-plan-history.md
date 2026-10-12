# Retained Meta campaign PAUSE plans

The `META_CAMPAIGN_PAUSE_PLAN` changes-stream record stores one complete,
reconstructed Meta campaign pause action. It is local historical planning
evidence. It supplies no execution grant, provider-origin attestation, current
remote-state proof, native approval kind or mutation command.

The record binds its action and plan digests, expiry, provider, exact zero
incremental spend in the account currency, and false authority controls.
Retention and ledger scans both revalidate the complete action. Missing fields,
coercible control values, a changed operation or egress target, and a rehashed
but incoherent plan are rejected. The envelope must identify the action's local
workspace and exact Meta account, use `LOCAL_CLI`, and carry no provider
occurrence time, envelope observation time or execution run ID. The action's
own bounded observation interval remains part of its retained provenance.

`retain_meta_campaign_pause_plan` appends through the canonical ledger only.
A configured workflow can pass its session retention callback after acquiring
configuration then credential locks. The ledger invokes that callback under
its own lock immediately before the plan payload append. The callback must use
held local handles and do no ledger or network I/O. A refusal does not append
the proposed plan; prior valid history remains available. Automatic segment
metadata may already exist after a failed first append, so no filesystem-wide
transaction is claimed.

`load_meta_campaign_pause_plan` selects exactly one record by its digest from
a verified cross-stream checkpoint. It returns the canonical action plus closed
historical provenance for a later private coordinator. It does not refresh the
action, read a credential, or authorize replay. `meta_campaign_pause_plan_catalog`
returns bounded newest-first pages with exact digests, expiry, capability status,
currency and incremental spend. Expired historical plans remain inspectable.

No separate observation-record predecessor is required for this record: the
closed action contains its complete normalized account/campaign baseline and
observation interval. That self-contained historical representation does not
replace the future post-authorization credential/account/campaign rereads,
drift checks, guarded one-use egress permit or durable `APPLY_STARTED`.

Tests cover exact round trips, original integer and Boolean types, envelope
mismatches before filesystem creation, rehashed envelope corruption during
scans, complete action reconstruction, final callback ordering/failure and
deterministic catalog pagination. They use local temporary ledgers only.

The related redacted review/export surface uses an explicit allowlist rather
than exposing the stored action. Credential/configuration bindings and raw
native identifiers stay within the existing local sharing boundary.

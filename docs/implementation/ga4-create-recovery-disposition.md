# GA4 key-event create recovery disposition

This M22 development contract addresses an execution-plan modeling gap. A GA4
key event has no native pause operation, and deletion cannot be promised as
compensation without proof that the resource is unreferenced. Listing `PAUSE`
or `DELETE` merely to satisfy a nonempty recovery-action field would invent an
unsupported or unauthorized recovery operation.

`SubPlan` now permits one explicit `recovery_disposition`:
`RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION`, only for `ga4_admin` / `CREATE`
with an empty `recovery_actions` array. The disposition is part of the canonical
plan digest. Unknown dispositions, explicit null, another provider/operation,
or any accompanying remote recovery action fail validation. Installed JSON
schema states the same conditional contract.

Historical plans omit the new field and retain their original canonical bytes
and digests. They still require nonempty recovery actions. This does not widen
the existing OpenAI pause action or the native approval protocol. Neither a
GA4 approval challenge nor an apply command is installed by this change.

The disposition requires later guarded execution to reconcile the attempted
operation, export local evidence and stop without automatic remote compensation.
It grants no delete, pause, restore or retry authority. Any proposed later edit
needs a new plan and valid authorization; deletion additionally requires the
existing dependency proof. Read-only reconciliation must preserve pending and
ambiguous outcomes and cannot interpret an eventually consistent absent list
member as proof that a write had no effect.

Tests verify historical canonical bytes, schema/model agreement, digest binding
and rejection of unsupported or contradictory recovery claims. This is local
model evidence only; it does not establish live GA4 creation or recovery behavior.

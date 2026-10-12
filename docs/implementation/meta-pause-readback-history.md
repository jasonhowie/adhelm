# Meta pause readback history

`META_CAMPAIGN_PAUSE_READBACK_OBSERVATION` is a closed, schema-versioned
`changes` record. The private adapter appends it after accepting its narrow
local acknowledgement subset and reading the configured account and campaign
again. It retains configured `PAUSED`, the actual allowlisted effective status,
a bounded UTC observation interval, API/capability facts, and exact action,
plan, subplan, attempt and resource references. It contains no raw response,
request, credential or provider error text.

Append and ledger reread require an earlier identical retained action and one
matching durable `APPLY_STARTED`. The kernel's start resource reference is the
subplan ID; the observation's campaign reference is separately checked against
the action. The envelope binds the workspace, account, run and observation end.
No provider occurrence time is invented, and a duplicate observation for the
same attempt is refused. Validation uses the already-scanned record list and
performs no ledger or network access inside the retention callback.

The record remains separate from `RESOURCE_SNAPSHOT`. A partial configured-status
readback must not replace a richer inventory baseline or turn omitted budget and
objective fields into apparent external changes. It also cannot settle an
uncertain write: the kernel still requires its exact apply and verification
sequence. If observation retention fails after egress, the adapter reports
ambiguity and never retries the write.

Existing verified JSONL and `record-nodes.csv` exports preserve these complete
facts. References are pseudonymized by the existing sharing boundary; statuses
remain readable. No additional CSV write path is introduced. The 19 focused
model/history cases cover malformed records, retained-action/start binding,
readback survival, export redaction, duplicate refusal, unresolved history and
unchanged inventory comparison. These are synthetic local checks; live Meta
behavior and installed approval authority remain unevidenced.

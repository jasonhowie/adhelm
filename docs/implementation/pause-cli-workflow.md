# OpenAI pause planning and retained review

The development CLI exposes `plan openai-pause --input <request>`,
`plan catalog`, and `plan show <record-digest>`. Discover its closed input with
`schema openai-pause-request`. This is implementation work with fixture evidence;
it is not a live-validated lifecycle or an installed execution path.

The request names an explicit OpenAI account-key EDIT profile and native
campaign ID. It bounds complete campaign inventory to at most 5,000 records and
plan lifetime to 60–900 seconds, defaulting to 500 records and 300 seconds.
Original JSON types, duplicate keys, profile shape, expected currency/timezone,
verified local history and unresolved recovery checks precede credential reads.
Missing GA4 configuration remains a required customer setup gap; it does not
turn this read-only plan preparation into a mutation capability.

One captured account key supplies the fixed account, complete campaign inventory
and current campaign GETs. The account's native identity, expected currency and
timezone must match before reading campaign details. The key is bound by HMAC
using a separate private OS-store key; the public profile's purpose does not
prove provider-enforced permission scopes. No configured CI binding-key channel
exists yet, so CI pause planning fails before secret access.

New actions explicitly bind the existing workspace ledger, independently of
the credential profile. Retention appends the reconstructed action as a closed
`EXECUTION_PLAN`; it never creates an independent credential-role ledger.
The redacted review preserves exact composite/subplan digests, native unchanged
budget, bid and schedule, active-to-paused state, expiry, zero incremental spend,
and the possibility of residual delivery or billing. The full output must fit
before the sharing key is accessed or the plan appended. Storage failure reports
unverified retention and directs inspection of local history before repetition.

Catalogs read verified checkpoints without accessing keys. Showing a plan
requires the sharing key for pseudonyms, with no provider or credential-binding
key access. Expired plans remain reviewable as historical evidence. Neither
retention, review, a file path nor a plan digest grants execution authority.
The separately registered development execution leaf still verifies
host-mediated direct approval or a valid bounded delegation, then re-reads
identity/state and enforces kernel expiry, drift, durable-start and recovery
checks. Its ordinary binding fails closed until native setup is separately
evidenced; see [installed-approval execution](installed-approval-execution-cli.md).

`artifacts plan` can render an explicitly requested static JSON, CSV, or HTML
view from a verified retained pause plan. A saved `RULES` preference may render
the same view during `plan` / `before_approval` / `plan_review`; default
`ON_REQUEST` creates no file. The rule is resolved before pause credentials or
provider reads, while file generation happens only after retention and a fresh
verified history load. The artifact binds its deterministic historical `as_of`
to local `recorded_at`, never reports current validity as evaluated, and preserves
the exact redacted review, expiry, false authority and recovery limits. Its
document is not an approval surface; opening or editing it cannot authorize,
refresh, or execute the plan. See [pause-plan artifacts](pause-plan-artifacts.md).

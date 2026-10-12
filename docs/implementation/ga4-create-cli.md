# GA4 key-event CREATE plan CLI

`plan ga4-key-event create --input REQUEST.json` is a local plan-preparation
command. It validates a bounded 1 MiB JSON request, reads the configured
GA4 Admin EDIT inventory through the lifecycle-owned workflow, retains the
exact observation and CREATE plan, and returns a compact receipt. It does not
send a CREATE request, create an authorization grant, or establish Google
permission to create a key event.

The request is `Ga4KeyEventCreatePlanRequest`: an explicit configured profile,
a retained candidate record digest, a complete fresh candidate input, and a
60–900 second plan lifetime. Duplicate JSON keys, noncanonical numeric forms,
and input over 1 MiB fail before configuration-dependent workflow work. The
workflow itself owns OAuth binding resolution, the one bearer use, fixed GETs,
fresh input recompilation, candidate/contract selection, local HMAC checks,
configuration drift checks, and ledger retention. The CLI never accepts an
identity, bearer, inventory, OAuth binding, or provider state from input.

Before the workflow can read a local key or contact the configured provider,
the command resolves the saved artifact policy and sizes a fixed receipt using
maximum-width closed placeholders. If that receipt cannot fit the saved
`max_output_bytes`, it returns `OUTPUT_LIMIT_EXCEEDED` before provider reads,
key access, or retention. The receipt contains only plan/record/observation
digests, expiry, the fixture-only status, zero incremental spend, fixed
no-authority controls, and an exact `plan ga4-key-event show RECORD_DIGEST`
command. It intentionally does not replace the full review.

The CLI passes the digest of that same preflight configuration to the workflow.
If configuration changes between artifact/output preflight and the workflow's
initial validation, it returns `CONFIGURATION_DRIFT` before protected
credential, sharing-key, or provider work. The command does not silently use
new output rules with a plan prepared under older local policy.

Before emitting success, the CLI reloads the receipt's plan record from the
verified local ledger and requires its action, action digest, plan digest, and
observation binding to exactly match the workflow result. A forged or stale
returned digest therefore fails closed instead of producing a show command for
unretained history. If that post-retention reload cannot be verified because
local ledger I/O fails, the command returns the fixed ambiguous
`GA4_CREATE_PLAN_RECEIPT_UNVERIFIED` diagnostic. The plan may already be
retained; inspect history verification and the catalog before another
preparation, and do not blindly repeat it.

The CLI has no GA4 CREATE plan artifact renderer. A matching saved
`plan_review` rule that requires generation fails with
`GA4_CREATE_PLAN_ARTIFACT_NOT_SUPPORTED`; an incomplete matching rule fails
with `ARTIFACT_CONFIGURATION_REQUIRED`. It never writes an artifact as a
fallback and does not create files outside the existing local configuration and
ledger writes performed by the workflow.

`plan ga4-key-event catalog` lists bounded retained metadata only. It does not
read a credential, sharing key, or provider. `plan ga4-key-event show
RECORD_DIGEST` loads a verified retained plan, makes a zero-key redacted size
probe, then obtains the sharing key only if every full planning and evidence
fact fits. It shows the complete redacted historical plan and provenance; it
does not paginate or silently omit planning gates. An expired plan remains
reviewable as history and remains non-authorizing.

Every command reports `execution_authority: false`, `remote_mutations: false`,
and `execution_command: UNRELEASED`. Opening a review, copying a digest, or
configuring an artifact rule does not authorize a GA4 mutation. The retained
plan still requires future current-state checks and a separately implemented,
exact authorization path before any egress.

Focused fake-only coverage verifies receipt preflight, 1 MiB/duplicate JSON
rejection, no sharing-key use for receipt/catalog, fixed GET-only preparation,
artifact-rule refusal without a file, historical redaction, and full-review
size preflight before sharing-key access. No real OAuth, credential store,
provider call, browser, or mutation is used by these tests.

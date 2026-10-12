# OpenAI pause-plan review facts

`preview_openai_pause` derives a closed, redacted review from a completely
revalidated `OpenAIPausePlannedAction`. It preserves the exact composite and
ordered subplan digests, native lifetime-budget micros, bid mode, schedule,
account currency/timezone, active-to-paused transition and plan expiry. Native
account/campaign identifiers and local profile references are pseudonymized
with the existing private sharing key; the credential binding and raw key are
omitted.

The view shows zero incremental authorized spend alongside an explicit warning
that residual delivery or billing can continue. It retains the missing provider
conditional-write/idempotency guarantees and unresolved/no-blind-retry outcome
for ambiguous attempts. Viewing an expired plan remains possible; the view
marks expiry using the explicitly supplied review time.

This function reads no provider, credential store, customer files or ledger and
changes no persistent state. Its review digest describes only the exact presentation.
Opening, changing or forwarding that presentation never creates authority,
fresh provider evidence, a new plan or a successful execution result. A trusted
approval host must independently verify the underlying action and exact plan
before any future authorization integration.

The root tests cover preserved facts and digests, deterministic output, expiry,
reference redaction, key changes, malformed input and nested-plan tampering.
This is a library review surface, with no installed pause-plan command, artifact
action link, native approval host or live capability claim.

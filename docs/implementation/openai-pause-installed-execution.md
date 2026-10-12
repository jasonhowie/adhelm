# OpenAI pause with installed native approval — M36 development integration

`execute_retained_openai_pause_with_installed_approval(directory, request)` is
an internal typed workflow for a previously retained pause plan. Its closed
request contains only schema version 1 and the source record digest. It accepts
no receipt, challenge, trust key, grant, adapter, transport or credential.
The explicit `execute openai-pause --input` CLI leaf is documented separately;
it accepts this same closed request and has no approval, credential, or provider
override flags. The default generated binding still refuses with
`NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` before helper/provider access.

Before requesting native review, the workflow verifies the selected ledger
source, its explicit workspace binding, expiry and unresolved history. It
requires the current OpenAI account-key EDIT profile to use the OS store and
match the plan's credential profile, account, currency and timezone. It derives
the credential binding from the current provider secret and separate private
binding key. The existing account-key role declaration does not establish
provider-side scopes. The configuration digest freezes this execution attempt's
local configuration; the older pause contract does not carry a historical
configuration digest, and this workflow does not claim otherwise.

The private installed bridge alone constructs the challenge and validates the
native candidate, then records the receipt claim and exact direct grant. The
fixed grantor reference is a logical protocol label, never a human identity
claim. The workflow rechecks the selected source, expiry, unresolved history,
configuration and credential binding after approval. Native consumption or
claiming followed by a failed local check produces no provider mutation and is
never automatically retried.

The existing OpenAI pause adapter and SafetyKernel own provider identity/state
reads, the durable `APPLY_STARTED`, bounded permit, fixed pause POST, independent
verification and recovery evidence. A fresh credential supplier rechecks local
configuration and binding on every use, including the adapter's final check
before POST. Short configuration/credential transactions never cross native
review or HTTP. They coordinate cooperating CLI writers; arbitrary same-user
code and simultaneous changes in an external provider are not made atomic.
Ambiguous writes are not retried, and unresolved execution blocks another
approval attempt.

Tests use synthetic credential stores, an ephemeral signed native candidate,
the actual private receipt-acceptance and SafetyKernel paths, and fixed mock
HTTP. They check default refusal, pre/post approval drift, exact grant selection,
claim/start ordering, a single successful POST, and no retry after ambiguous
responses. No configured helper, host, actual OS authentication, Keychain or
provider was used. OpenAI pause remains
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.

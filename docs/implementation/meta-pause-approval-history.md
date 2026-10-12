# Meta PAUSE receipt history

The local receipt-consumption history now recognizes the dedicated
`META_CAMPAIGN_PAUSE_PLAN` subject. The acceptance library requires a verified
retained Meta plan in the same workspace and compares its complete canonical
action with the signed subject before recording a claim. The resulting direct
grant binds only that plan, its one subplan and the receipt expiry. A plan
record, embedded public key or signature-consistency record does not configure
trusted native installation.

The changes-stream validator separately proves that a Meta claim references an
earlier exact Meta plan. This check runs under the existing append lock and
during history verification. It uses the already-validated records and performs
no separate ledger read, credential access or provider request. Receipt digest,
nonce digest and receipt identifier consumption remain shared across all
approval kinds. A claim cannot be repointed to another valid retained action.

The existing export allowlist carries only redacted consumption metadata. It
omits the challenge, signed receipt, host configuration and verification key;
it does not export a reusable approval object. The stored local claim remains
signature-consistency evidence, distinct from installed trust.

Synthetic P-256 and temporary-ledger regressions cover exact acceptance, missing
and different retained sources, three replay cases, direct-append source checks,
redacted export and continued rejection by the installed bridge v1 source
selector and request builder. Acceptance records `NO_REMOTE_WRITE` and no
`APPLY_STARTED`. These tests supply configured trust as test data; they do not
establish an installed host, actual user review, protected receipt redemption,
Meta write permission or provider execution. Bridge promotion and the guarded
Meta execution path remain separate unfinished work.

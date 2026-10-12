# Durable local receipt acceptance

The Python library verifies a canonical native receipt against separately
supplied fixed host configuration, P-256 public-key pin, and coordinating
workspace. No CLI flag or receipt chooses these trust values. The installation
and current key-revocation resolver are not implemented by this increment.

For a direct pause receipt, the exact full action must equal an earlier retained
execution plan in that workspace. Policy install and revoke receipts use
distinct proof methods; neither can prove the other operation. Verification
reconstructs the expected challenge from canonical bytes and binds every unsigned
field, review digest, time and signature domain.

Acceptance appends and fsyncs `APPROVAL_RECEIPT_CLAIMED` before recording the
grant or policy operation. The changes-stream lock rejects previously consumed
receipt identifiers, signed-receipt digests and challenge nonces. A failed or
ambiguous handoff after consumption never replays the receipt: the caller must
inspect canonical history and obtain a fresh challenge/review as appropriate.
There is no provider invocation in this library.

A policy transition lock covers current-state validation, receipt consumption,
and the authorization event. A revocation must name the exact latest installed
policy digest and version. Orphan, stale, mismatched, and already-revoked
requests fail before consuming a receipt; rejected revocations cannot advance
the installed-version counter. Concurrent cooperating install/revoke callers
share this lock. This is local serialization, not a remote policy service.

The claim retains bounded canonical local challenge/receipt material and the
public verification point so later history verification can prove signature
consistency. That embedded point is expressly not an enrollment/trust root.
Exports use a separate allowlisted projection containing only digests,
fingerprint, kind, local acceptance time and pseudonymized references. Raw
challenge, receipt, signature, nonce, public point and host configuration do
not enter JSONL/CSV artifact projections.

Current receipt and subject eligibility are checked before consumption and at
recording handoffs. The ledger invokes a private bounded callback immediately
before its record append while holding the stream lock. Authorization proof
callbacks cannot acquire or read that ledger. Expired/revoked policies cannot
be newly installed; future effective policies may be configured but have a
separate current-effective execution gate.

Revocations are local immediate operations, not a scheduler. Acceptance refuses
a signed requested revocation instant that is still in the future. The signed
requested time remains in the claim; the actual local revocation takes effect
when its durable authorization event is recorded. It does not claim a past
effect or a provider-side change. Historical verification does not apply today's
clock to previously valid claims.

`tests/test_approval_acceptance.py` and the independent acceptance review tests
exercise real cryptographic verification with synthetic software keys, exact
plan binding, expiry handoffs, concurrent consumption, uncertainty and export
redaction. They do not establish installed host identity, current enrollment,
Keychain protection, actual OS authentication, live provider access or release
readiness.

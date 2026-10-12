# Approval acceptance follow-up ultra review

This follow-up reviewed the durable policy-receipt transition handoff after
M15. It used only the local JSONL ledger, synthetic canonical challenges, and
ephemeral in-memory P-256 keys. It made no Keychain, native-host, provider, or
network call.

The initial review reproduced a material transition defect: an otherwise valid
revocation for a policy that had never been installed could consume its receipt
and append a local revocation event. That event then made a later exact
installation of the same policy version fail as a replay. The repair treats a
policy transition as one serialized local operation: it holds the per-policy
lock across preflight, claim, and the durable authorization event; installation
history counts only durable grants; and a revocation must name the exact latest
installed, still-active policy version and digest.

`tests/test_approval_acceptance_followup_ultra_review.py` covers four paths:

- an orphan revocation is rejected before a receipt claim;
- stale install and stale revoke receipts are rejected without consuming a new
  nonce;
- a same-version but digest-mismatched revoke and an already-revoked policy
  both leave their new receipts unclaimed; and
- concurrent valid install and revoke requests serialize to one claim and one
  durable authorization event each, in install-then-revoke order.

The focused acceptance, protocol, safety, and ledger suite passed 86 tests.
Ruff and source-only mypy pass for the reviewed Python modules. These checks
establish local transition and redaction behavior only. They do not establish
installed host identity, enrollment, current key-revocation resolution, OS
authentication, Keychain controls, provider execution, or v1 completion.

The subsequent locked-preflight clock-failure regression is contained before
receipt consumption: an unexpected policy-state read failure is translated to
`APPROVAL_POLICY_STATE_UNVERIFIED`, preserves explicit `AdhelmError` diagnostics,
and appends no claim. The catch surrounds only the policy transition preflight
under the policy lock, so the later durable-record uncertainty path remains
`APPROVAL_RECORDING_UNVERIFIED` and still forbids replay.

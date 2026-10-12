# Python approval-protocol bridge

`adhelm.approval_protocol` is a closed Python implementation of the canonical
v1 wire contract used by the local macOS approval host. It creates, decodes,
reviews, and verifies only these kinds:

- `OPENAI_PAUSE_PLAN`
- `GA4_KEY_EVENT_CREATE_PLAN`
- `META_CAMPAIGN_PAUSE_PLAN`
- `COMPOSITE_EXECUTION_PLAN`
- `ATTEMPT_RECOVERY`
- `DELEGATION_POLICY_INSTALL`
- `DELEGATION_POLICY_REVOKE`

The rest of this document describes the three kinds that existed when it was
written. The two GA4 and Meta provider-specific kinds are documented in
`ga4-key-event-create-native-approval.md` and `meta-pause-approval-protocol.md`.
The two provider-neutral kinds added by
[ADR 0012](../adr/0012-composite-execution-plan-approval.md) -- their subject
schema, `review_lines` boundary, derived lines, acceptance, and the Swift work
they still require -- are documented in
[`composite-execution-plan-approval.md`](composite-execution-plan-approval.md).
Those two kinds also replaced the hard-coded `_SUPPORTED_PAUSE_TIMEZONES` pair
mentioned below with validation of the IANA name the plan carries.

It has no CLI command, provider request, credential-store access, OS
authentication, private-key handling, kernel grant, policy installation,
replay claim, or durable-write path. A verified receipt is cryptographic
evidence only. A later integration must independently select its trusted
configuration and public key, atomically claim the nonce and receipt in the
canonical ledger, recheck current policy and plan requirements, and then use
the existing safety kernel.

## Closed challenge data

`ApprovalHostConfiguration` is a frozen dataclass containing only the host
reference, signing-key reference, and finite integer protocol limits. It has
`to_data()`, `canonical_bytes`, and `configuration_digest` so a later retained
record can bind exact configuration data separately from the public-key
fingerprint. A retained configuration is evidence, not a trust source; the
accepting integration must supply its reviewed configuration and public key
again.

Challenge construction is deliberately typed:

- `build_openai_pause_challenge()` revalidates the complete installed
  `OpenAIPausePlannedAction` through both
  `revalidate_openai_pause_action()` and `plan_views._validated_action()`.
  The bridge accepts only the native-reviewed source time zones `UTC` and
  `America/Chicago`.
- `build_delegation_policy_install_challenge()` and
  `build_delegation_policy_revoke_challenge()` revalidate the complete
  `DelegationPolicy`, including the configured host reference, exact safe
  integer money limits, and all policy fields.

The builder uses a secure random 32-byte nonce and UTC clock by default. Tests
can inject exact nonce and timestamp values. All accepted challenge timestamps
use UTC whole seconds or exactly six fractional digits. Decoding rejects
duplicate keys, noncanonical JSON, decimals and exponents, alternate Base64URL
pad bits, unsafe integers, unexpected fields, and malformed nested action or
policy shapes. `decode_approval_challenge()` enforces the original issuance
window and current expiry. `parse_approval_challenge_wire()` is explicitly for
structural historical parsing and intentionally does not establish freshness or
authority.

`ApprovalChallenge` exposes canonical bytes, digest, kind, nonce, issue and
expiry times, and the validated action or policy subject. Its public fields are
not trusted by the verifier: receipt verification rebuilds the challenge from
the canonical bytes.

## Review and receipt binding

`approval_review_facts()` emits the exact canonical facts shape rendered by the
native host. The pause review binds the complete closed baseline, transition,
plan, spend cap, egress, workspace binding, and fixture-only status. It reports
that a credential binding exists without including the binding reference. The
policy review binds all account, operation, spend, host, timing, and policy
identity fields. The review digest is SHA-256 over those canonical facts.

`receipt_signing_payload()` exposes a typed, non-authorizing test/integration
seam for the exact unsigned receipt and its signing input:

```text
adhelm.macos-approval-host.receipt.v1\0
<receipt kind>\0
<canonical unsigned receipt bytes>
```

This allows Python and Swift fixtures to compare every bound field without a
Python signer. The function does not possess a private key or create any
authority.

`verify_approval_receipt()` accepts only canonical bounded receipt JSON. It
requires an independently supplied uncompressed 65-byte P-256 X9.63 public
key; the receipt never chooses its pin. It requires a canonical Base64URL,
strict DER ECDSA signature and verifies it with `SECP256R1` and SHA-256 using
[`cryptography`'s EC API](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/)
and [X9.63 serialization API](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/).
It reconstitutes every expected unsigned field before signature verification,
including kind, nonce, challenge/review digest, host/key reference, exact
subject bindings, plan or policy digests, and revocation timestamp.

Receipt issue timestamps are exactly UTC milliseconds. Verification checks the
current challenge expiry and receipt issue window, but it intentionally does
not repeat the initial 60-second challenge age check after a longer local
review. `VerifiedApprovalReceipt` exposes canonical bytes/digest, receipt and
host/key identities, issue/expiry, exact action or policy bindings,
configuration digest, and the independently supplied key fingerprint. It still
is not a grant or replay receipt consumption.

`parse_approval_receipt_wire()` is available only for closed, canonical
structural parsing into a nested mapping suitable for the existing redacted
ledger boundary. It verifies neither the signature nor trusted-key provenance.

## Validation

`tests/test_approval_protocol.py` uses only synthetic fixtures and ephemeral
in-memory P-256 keys. It covers checked-in Python/Swift vector parity, all
three subject kinds, exact review facts, hostile JSON and Base64URL variants,
constructed-model bypasses, configuration binding, receipt domain/binding and
pin failures, strict timestamp forms, long-review behavior, expiry, and raw
input error sanitization. No private key is persisted.

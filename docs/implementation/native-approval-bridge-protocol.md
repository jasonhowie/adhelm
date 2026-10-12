# Native approval bridge protocol

This increment defines private Python and package-internal Swift validators
for one canonical wire contract
for the proposed installed approval bridge in [ADR 0004](../adr/0004-installed-approval-bridge.md).
It does not start a child process, open a pipe or XPC connection, load build
configuration, validate an installed host, access a protected key or store,
review a challenge, authenticate a user, consume a nonce, append JSONL, create
a grant, or execute a provider operation. Its candidate status is structured
evidence syntax only; it is not authority.

`NativeApprovalBridgeRequestV1` accepts only the direct retained-plan kinds
`OPENAI_PAUSE_PLAN` and `GA4_KEY_EVENT_CREATE_PLAN`. Delegation-policy
challenges remain outside this slice because their source binding does not use
the retained-plan record and checkpoint required here.

## Request

The request is one RFC 8785 canonical JSON object, at most 98,304 bytes, with
exactly these seven scalar fields:

```text
schema_version: 1
message_type: "NATIVE_APPROVAL_BRIDGE_REQUEST_V1"
request_id: Base64URL of exactly 32 random bytes
source_context_digest: lowercase SHA-256 hex
metadata_digest: lowercase SHA-256 hex
protocol_configuration_digest: lowercase SHA-256 hex
canonical_challenge_b64url: Base64URL of exact canonical challenge bytes
```

`request_digest` is SHA-256 of the whole canonical request. It is deliberately
not a self-referential request field. The challenge bytes must be canonical,
bounded by the configured challenge limit, and identify one of the two direct
plan kinds; complete challenge, plan, metadata, and freshness checks occur when
the request is bound to static metadata and a trusted exchange-start time.

Python creates the opaque source-context digest from this local-only canonical
preimage and sends only its digest to native code:

```text
schema_version: 1
scope: "ADHELM_NATIVE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V1"
workspace_profile_ref
source_stream: "changes"
source_plan_record_digest
checkpoint_digest
```

The native contract validates the digest shape and echoes it. It does not parse
this preimage, read a workspace, inspect a ledger, or infer that a plan is
current.

## Candidate

The successful private response is at most 131,072 bytes and has exactly these
19 scalar fields:

```text
schema_version: 1
message_type: "NATIVE_APPROVAL_BRIDGE_CANDIDATE_V1"
native_observation_status: "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED"
request_id_digest
request_digest
source_context_digest
metadata_digest
protocol_configuration_digest
canonical_challenge_b64url
challenge_digest
challenge_nonce_digest
receipt_kind
expires_at
canonical_receipt_b64url
receipt_digest
receipt_id
active_public_key_x963_b64url
active_public_key_fingerprint
native_observed_at
```

Every digest is lowercase SHA-256 hex. `request_id_digest` is SHA-256 of the
decoded request ID. The public key is exactly one uncompressed 65-byte P-256
X9.63 value, and its fingerprint is SHA-256 of those bytes. Request IDs and
the source-context preimage never appear in the candidate. Challenge and
receipt JSON are carried as normalized Base64URL so the outer wire stays a
closed scalar object while retaining their exact UTF-8 bytes.

Candidate revalidation rebuilds the compiled metadata descriptor, requiring
both the full 18-field metadata digest and the existing seven-field protocol
projection digest. It reparses the candidate challenge and receipt, verifies
the receipt with the returned key, recomputes every digest/fingerprint, and
requires exact challenge/receipt nonce, kind, expiry, and receipt-ID bindings.
The returned key is checked only for cryptographic consistency in this slice;
the protocol does not expose a key resolver or turn it into a reusable pin.
Swift rebuilds the full descriptor from typed metadata. Python validates against
the separately retained full-metadata digest and typed protocol configuration;
this slice does not supply an installed metadata resolver in either language.

`native_observed_at` uses exactly six UTC fractional digits. Revalidation
requires:

```text
receipt_issued_at <= native_observed_at < challenge_expiry
trusted_exchange_started_at <= native_observed_at <= trusted_current_now < challenge_expiry
trusted_current_now >= trusted_exchange_started_at
```

Those checks bind a point-in-time observation and reject local rollback during
the exchange. They do not attest installation, a current native host, an
actual protected-store transition, user review, authentication, or future
currentness.

## Future boundary ownership

The future fixed client owns one anonymous-pipe request on stdin and one
candidate response on stdout, with no arguments and explicit rejection of
non-pipe endpoints. The M32 Core contract only handles already supplied bytes.
It emits no generic error document; a later runner maps an empty, malformed, or
failed exchange to a compact private failure.

That future configured macOS boundary must derive the descriptor from generated
build metadata, perform deterministic review and fresh authentication, obtain
the current key under the fixed protected-state lease, verify and consume the
receipt through the M31 coordinator, and only then construct a candidate. The
existing raw-receipt XPC method is not an acceptance path.

Python remains the plan, ledger, credential, and provider-execution owner. A
future Python bridge must create the request ID/source digest from a verified
retained source, validate the private candidate against that exact request,
independently verify its receipt, and then use the existing JSONL
claim/grant and `APPLY_STARTED` ordering. It must not accept caller-supplied
receipt bytes, a generic public key, `PinnedApprovalTrust`, or a candidate as a
public authorization input.

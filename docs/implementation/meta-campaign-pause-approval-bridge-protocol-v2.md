# Meta campaign PAUSE approval bridge protocol V2

This is a pure, private cross-language codec for one existing
`META_CAMPAIGN_PAUSE_PLAN` approval challenge. It validates canonical request
and candidate bytes only. It does not launch a helper, contact a host, resolve
an installed identity or key, obtain user interaction, consume a nonce, record
a receipt claim or grant, read configuration or credentials, or execute a
provider operation.

The existing V1 bridge remains unchanged. Its request and candidate byte
formats, helper, generated binding, raw receipt XPC route, and direct-kind
allowlist continue to cover only OpenAI PAUSE and GA4 key-event CREATE. V1
therefore rejects Meta bytes. This V2 codec accepts only the existing closed
M42 `META_CAMPAIGN_PAUSE_PLAN` subject; it does not create a generic plan
bridge.

## Python codec surface

The private module
`adhelm._meta_campaign_pause_approval_bridge_protocol_v2` exposes only these
module-issued opaque values and closed operations:

- `NativeMetaCampaignPauseApprovalBridgeRequestV2` and
  `NativeMetaCampaignPauseApprovalBridgeCandidateV2`;
- `derive_meta_campaign_pause_approval_bridge_source_context_digest_v2()`;
- `build_meta_campaign_pause_approval_bridge_request_v2()` and
  `decode_meta_campaign_pause_approval_bridge_request_v2()`;
- `revalidate_meta_campaign_pause_approval_bridge_request_v2_at_exchange()`;
  and
- `validate_meta_campaign_pause_approval_bridge_candidate_v2()` and
  `revalidate_meta_campaign_pause_approval_bridge_candidate_v2()`.

Request decoding reconstructs the complete canonical M42 subject before it
returns a typed value. Candidate validation reparses the request, validates
the exact Meta kind, request correlation, full metadata and protocol digests,
receipt/key/fingerprint consistency, nonce and expiry binding, and the trusted
exchange/current-time window. Neither value exposes a raw receipt, key,
permit, pin, host resolver, or acceptance operation.

The Swift codec and its package-internal types are described separately in
[the native V2 protocol document](meta-campaign-pause-native-bridge-protocol-v2.md).

## Closed V2 records

`NativeMetaCampaignPauseApprovalBridgeRequestV2` accepts exactly these seven
canonical JSON fields, bounded to 98,304 bytes:

```text
schema_version = 2
message_type = NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_REQUEST_V2
request_id
source_context_digest
metadata_digest
protocol_configuration_digest
canonical_challenge_b64url
```

Request construction and decoding reconstruct the complete canonical M42 Meta
subject, action, plan, and expiry relationships before producing a typed V2
request. They intentionally defer only current issuance and expiry eligibility
to the later trusted exchange-start binding.

`NativeMetaCampaignPauseApprovalBridgeCandidateV2` accepts exactly these
nineteen canonical JSON fields, bounded to 131,072 bytes:

```text
schema_version = 2
message_type = NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_CANDIDATE_V2
native_observation_status = NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED
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

The candidate verifies only self-consistency between its exact receipt and its
returned P-256 X9.63 public key. It is non-authorizing evidence, not a trust
pin, direct grant, provider credential, execution permit, current-host
attestation, or proof that a protected-store operation occurred.

The request and candidate bind the full 18-field host metadata digest and the
existing seven-field protocol-configuration digest. In this pure codec, both
are structurally checked caller-supplied facts. The codec does not prove that
they came from a generated installed binding, a host binary, or a ledger.

## Source context

The source-context digest uses the exact V1 field shape with V2 domain
separation:

```json
{
  "schema_version": 2,
  "scope": "ADHELM_NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V2",
  "workspace_profile_ref": "…",
  "source_stream": "changes",
  "source_plan_record_digest": "…",
  "checkpoint_digest": "…"
}
```

The canonical JSON SHA-256 digest is the only source-context value on the wire.
The challenge and signed receipt already bind the exact M42 action, plan, and
subplan digests, so this record intentionally adds no duplicate action fields.

## Synthetic vectors

The deterministic vectors are in
[`Fixtures/NativeApprovalBridgeV2MetaPause`](../../native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridgeV2MetaPause).
They rebuild and revalidate the checked-in M42 whole-second and `.123456Z`
fractional action fixtures before producing a V2 request and candidate. The
generator contains fixed synthetic public-key/signature data only; it contains
no private key. It rejects a changed source action, challenge, review, unsigned
receipt, or digest before writing output.

`tests/test_meta_campaign_pause_approval_bridge_protocol_v2.py` reruns that
generator and requires byte-for-byte equality with the checked-in JSON before
it accepts either whole-second or fractional evidence. The native fixture
consumer uses the same nested JSON; neither side treats it as host,
installation, or authorization evidence.

The vectors exercise canonical JSON, exact request correlation, complete
receipt/key/fingerprint binding, nonce and expiry binding, six-fraction native
observation time, V1/V2 non-interoperability, and Meta-only kind admission.
They provide no installed host, XPC, pipe, Keychain, LocalAuthentication,
provider, credential, ledger, grant, or egress evidence.

## Deferred route work

A later separately reviewed slice may add a dedicated fixed V2 helper, XPC
selector, post-signing consumer, and Python source-reload/acceptance path. It
must keep V1 bytes and routing intact, retain raw receipt-XPC Meta refusal, and
preserve the existing provider kernel ordering. This codec does not authorize
that work or any Meta Graph POST. The current acknowledgement evidence remains
insufficient for a confirmed live mutation claim.

## Validation

The focused Core fixture consumer passed five tests with SwiftPM sandboxing
enabled:

```text
env SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m45-module-cache \
  CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m45-clang-module-cache \
  swift test --package-path native/macos-approval-host --enable-xctest \
  --disable-swift-testing --scratch-path /private/tmp/adhelm-m45-swift-build \
  --filter NativeMetaCampaignPauseApprovalBridgeProtocolV2Tests
```

The full native package suite then passed 255 tests with the same sandboxed
configuration. Transcripts are retained at
`/private/tmp/adhelm-m45-v2-focused-tests.txt` and
`/private/tmp/adhelm-m45-full-swift-tests.txt`. `swift-format lint --strict`
passed for the V2 Core source and fixture consumer.

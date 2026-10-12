# Native receipt verification

`PinnedReceiptVerifier` verifies closed macOS approval receipts against a trusted
P-256 public-key pin and the exact previously validated approval challenge. It
uses Apple's CryptoKit implementation; it implements no cryptographic primitive.
The pin and trusted host configuration come from the integration, never from
receipt or challenge fields.

The verifier parses bounded canonical JSON, reconstructs the complete unsigned
receipt from the expected challenge, regenerated deterministic review, fixed
host/key configuration and validated receipt timestamp/identifier, and compares
all unsigned bytes. Extra or changed fields fail, including nonce, operation
kind, action/plan/policy digests, ordered subplans, authentication mode and expiry.
It accepts canonical DER ECDSA signatures only and verifies the exact
domain-separated payload with P-256/SHA-256. Install, revoke and pause kinds have
distinct signature domains.

Issue times must match the host factory's UTC millisecond encoding and lie within
the configured initial clock-skew boundary and the challenge expiry. The current
time must precede the challenge's exact expiry. A previously validated challenge
is not subjected to the initial request-age limit again after a long review;
the final challenge deadline still applies.

Verification is pure. It does not consume a nonce, enroll a key, show a review,
authenticate the OS user, append a grant, or authorize provider egress. The future
installed integration must obtain the pin through protected enrollment and check
durable replay/consumption and policy-revocation state before recording authority.
An injectable public key is a development integration seam, not proof that an
installed host or protected key was used.

Offline tests generate ephemeral software P-256 keys in process memory. They
exercise real signature verification for the three approval kinds and reject
wrong keys, signed field substitutions, cross-kind/nonce rebinding, exact expiry,
invalid/future timestamps, duplicate/noncanonical/oversized JSON and invalid keys.
They do not access Keychain, invoke OS authentication, or start an approval service.
The native vector parity checks independently regenerate the Swift fixtures from
the current Python safety models, preserving whole seconds and microseconds.

The primary API declarations were checked in the local Xcode macOS 26.2
`CryptoKit.swiftinterface`: `P256.Signing.PublicKey(x963Representation:)`,
`P256.Signing.ECDSASignature(derRepresentation:)`, `derRepresentation`, and
`isValidSignature(_:for:)`. Exact-commit build and test evidence is recorded
separately after validation; none of this is installed approval or release proof.

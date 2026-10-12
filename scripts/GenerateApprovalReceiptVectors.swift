// Development-only interoperability vector generator. Compile against the
// checked-in ApprovalHostCore module. The software key exists only in memory;
// stdout contains synthetic challenges, public key, facts and signed receipts.
// This tool never imports Security or LocalAuthentication or invokes a host.
import ApprovalHostCore
import CryptoKit
import Foundation

guard CommandLine.arguments.count == 2 else {
  fatalError("Expected the synthetic challenge fixture directory")
}
let fixtures = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let now = Date(timeIntervalSince1970: 1_893_456_030)
let configuration = try TrustedHostConfiguration(
  hostReference: "host:adhelm-macos-approval-v1",
  signingKeyReference: "key:approval-test-v1",
  machServiceName: "invalid.fixture.service",
  expectedClientCodeSigningRequirement: "never",
  expectedHostCodeSigningRequirement: "never",
  signingKeyApplicationTag: Data("synthetic-interoperability-only".utf8),
  keyStorage: .keychainP256
)
let key = P256.Signing.PrivateKey()
let pin = key.publicKey.x963Representation
let verifier = try PinnedReceiptVerifier(configuration: configuration, trustedPublicKeyX963: pin)
var vectors: [CanonicalJSONValue] = []
for (index, name) in [
  "openai-pause-challenge-v1", "openai-pause-whole-seconds-challenge-v1",
  "delegation-policy-install-challenge-v1", "delegation-policy-revoke-challenge-v1",
].enumerated() {
  let fixture = try Data(contentsOf: fixtures.appendingPathComponent(name + ".json"))
  guard case .object(var fields) = try CanonicalJSON.parseCanonical(fixture) else {
    fatalError("Expected a synthetic challenge object")
  }
  // The legacy parser fixtures reuse a nonce across kinds. These receipt
  // vectors must also support a sequential install/revoke acceptance test,
  // so give each distinct challenge its own deterministic synthetic nonce.
  let nonce = Data(repeating: UInt8(144 + index), count: 32).base64EncodedString()
    .replacingOccurrences(of: "+", with: "-")
    .replacingOccurrences(of: "/", with: "_")
    .replacingOccurrences(of: "=", with: "")
  fields["nonce"] = .string(nonce)
  let raw = try CanonicalJSON.encode(.object(fields))
  let challenge = try ApprovalChallenge.decode(raw, now: now)
  let review = try DeterministicApprovalReview.make(for: challenge)
  let unsigned = try ApprovalReceiptFactory.makeUnsigned(
    challenge: challenge, review: review, configuration: configuration, now: now,
    receiptIdentifier: "approval-receipt:" + String(repeating: String(index), count: 43)
  )
  let signature = try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
  let signed = try ApprovalReceiptFactory.finalize(unsigned, signature: signature.derRepresentation)
  _ = try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: now)
  vectors.append(
    .object([
      "fixture_name": .string(name),
      "challenge": try CanonicalJSON.parseCanonical(raw),
      "review_facts": try CanonicalJSON.parseCanonical(review.facts),
      "review_digest": .string(review.digest),
      "receipt": try CanonicalJSON.parseCanonical(signed.canonicalReceipt),
      "receipt_digest": .string(signed.digest),
    ]))
}
let result = try CanonicalJSON.encode(
  .object([
    "schema_version": .integer(1),
    "producer": .string("SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"),
    "scope": .string("SYNTHETIC_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION"),
    "verified_at": .string("2030-01-01T00:00:30Z"),
    "public_key_x963_hex": .string(pin.map { String(format: "%02x", $0) }.joined()),
    "vectors": .array(vectors),
  ]))
FileHandle.standardOutput.write(result)

// Development-only GA4 CREATE interoperability vector generator. Compile
// against the checked-in ApprovalHostCore module and pass the synthetic
// fixture directory as its only argument. The P-256 software key exists only
// in memory; stdout contains its public X9.63 key and a signed synthetic
// receipt. This tool never imports Security or LocalAuthentication.
import ApprovalHostCore
import CryptoKit
import Foundation

guard CommandLine.arguments.count == 2 else {
  fatalError("Expected the synthetic challenge fixture directory")
}

let fixtures = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let fixtureName = "ga4-key-event-create-challenge-v1"
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

let rawChallenge = try Data(contentsOf: fixtures.appendingPathComponent(fixtureName + ".json"))
let challenge = try ApprovalChallenge.decode(rawChallenge, now: now)
guard challenge.kind == .ga4KeyEventCreatePlan else {
  fatalError("Expected the fixed GA4 CREATE approval subject")
}

let review = try DeterministicApprovalReview.make(for: challenge)
let key = P256.Signing.PrivateKey()
let unsigned = try ApprovalReceiptFactory.makeUnsigned(
  challenge: challenge,
  review: review,
  configuration: configuration,
  now: now,
  receiptIdentifier: "approval-receipt:" + String(repeating: "G", count: 43)
)
let signature = try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
let signed = try ApprovalReceiptFactory.finalize(unsigned, signature: signature.derRepresentation)
let pin = key.publicKey.x963Representation
let verifier = try PinnedReceiptVerifier(configuration: configuration, trustedPublicKeyX963: pin)
_ = try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: now)

let result = try CanonicalJSON.encode(
  .object([
    "schema_version": .integer(1),
    "producer": .string("SWIFT_CRYPTOKIT_EPHEMERAL_SOFTWARE_KEY"),
    "scope": .string("SYNTHETIC_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION"),
    "fixture_name": .string(fixtureName),
    "verified_at": .string("2030-01-01T00:00:30Z"),
    "public_key_x963_hex": .string(pin.map { String(format: "%02x", $0) }.joined()),
    "challenge": try CanonicalJSON.parseCanonical(rawChallenge),
    "review_facts": try CanonicalJSON.parseCanonical(review.facts),
    "review_digest": .string(review.digest),
    "receipt": try CanonicalJSON.parseCanonical(signed.canonicalReceipt),
    "receipt_digest": .string(signed.digest),
  ])
)
FileHandle.standardOutput.write(result)

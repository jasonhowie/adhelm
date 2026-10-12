import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore

/// Independent fake-only adversarial coverage for the offline M32 bridge wire
/// contract. These tests use ephemeral P-256 keys and bundled synthetic
/// challenges only. They do not open pipes, create XPC connections, access
/// protected storage, invoke Local Authentication, or record a ledger claim.
final class NativeApprovalBridgeProtocolUltraReviewTests: XCTestCase {
  func testRequestRejectsPrimitiveAndPolicyChallengesBeforeTypedRequest() throws {
    let fixture = try makeFixture()
    let metadata = fixture.metadata
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)

    for invalidChallenge in [
      Data("\"not a challenge\"".utf8),
      try fixtureData("delegation-policy-install-challenge-v1"),
    ] {
      XCTAssertThrowsError(
        try NativeApprovalBridgeRequestV1(
          requestID: Data(repeating: 0x21, count: 32),
          sourceContextDigest: String(repeating: "a", count: 64),
          metadataDigest: descriptor.metadataDigest,
          protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
          canonicalChallenge: invalidChallenge
        )
      )
    }

    let malformedDirect = try NativeApprovalBridgeRequestV1(
      requestID: Data(repeating: 0x21, count: 32),
      sourceContextDigest: String(repeating: "a", count: 64),
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: Data("{\"kind\":\"OPENAI_PAUSE_PLAN\",\"schema_version\":1}".utf8)
    )
    XCTAssertThrowsError(
      try malformedDirect.boundChallenge(
        metadata: metadata, exchangeStartedAt: fixture.exchangeStartedAt)
    )
  }

  func testCandidateRejectsCorrelatedRequestSourceChallengeAndStaticDigestChanges() throws {
    let fixture = try makeFixture()

    try mutateCandidate(fixture) { fields in
      fields["request_id_digest"] = .string(String(repeating: "0", count: 64))
    }
    try mutateCandidate(fixture) { fields in
      fields["source_context_digest"] = .string(String(repeating: "1", count: 64))
    }
    try mutateCandidate(fixture) { fields in
      fields["metadata_digest"] = .string(String(repeating: "2", count: 64))
    }
    try mutateCandidate(fixture) { fields in
      fields["protocol_configuration_digest"] = .string(String(repeating: "3", count: 64))
    }

    let otherChallenge = try ApprovalChallenge.decode(
      try fixtureData("ga4-key-event-create-challenge-v1"),
      now: fixture.exchangeStartedAt,
      limits: fixture.configuration.limits
    )
    try mutateCandidate(fixture) { fields in
      fields["canonical_challenge_b64url"] = .string(
        base64URLEncoded(otherChallenge.canonicalBytes))
    }
  }

  func testCandidateRejectsReplacementKeyWithMatchingReplacementFingerprintAndWrongSignature()
    throws
  {
    let fixture = try makeFixture()
    let replacementKey = P256.Signing.PrivateKey().publicKey.x963Representation

    try mutateCandidate(fixture) { fields in
      fields["active_public_key_x963_b64url"] = .string(base64URLEncoded(replacementKey))
      fields["active_public_key_fingerprint"] = .string(CanonicalJSON.sha256Hex(replacementKey))
    }

    var nonUncompressedKey = fixture.publicKey
    nonUncompressedKey[nonUncompressedKey.startIndex] = 0x02
    try mutateCandidate(fixture) { fields in
      fields["active_public_key_x963_b64url"] = .string(base64URLEncoded(nonUncompressedKey))
      fields["active_public_key_fingerprint"] = .string(
        CanonicalJSON.sha256Hex(nonUncompressedKey))
    }
  }

  func testCandidateRejectsExchangeRollbackAndObservationOutsideClosedWindow() throws {
    let fixture = try makeFixture()
    let oneMicrosecond = 0.000_001

    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        fixture.candidate.canonicalBytes,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.currentNow,
        currentNow: fixture.exchangeStartedAt
      )
    )

    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        fixture.candidate.canonicalBytes,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.challenge.expiresAt
      )
    )

    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        fixture.candidate.canonicalBytes,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: date("2029-12-31T23:58:00Z"),
        currentNow: fixture.currentNow
      )
    )

    try mutateCandidate(fixture) { fields in
      fields["native_observed_at"] = .string("2030-01-01T00:00:09.999999Z")
    }
    try mutateCandidate(fixture) { fields in
      fields["native_observed_at"] = .string("2030-01-01T00:00:13.000001Z")
    }
    try mutateCandidate(fixture) { fields in
      fields["native_observed_at"] = .string(fixture.challenge.expiresAtText)
    }

    let slightlyBeforeObservation = fixture.currentNow.addingTimeInterval(-1 - oneMicrosecond)
    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        fixture.candidate.canonicalBytes,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: slightlyBeforeObservation
      )
    )
  }

  func testCandidateRejectsNoncanonicalFramesAliasesAndReceiptSignatureChange() throws {
    let fixture = try makeFixture()
    let canonical = fixture.candidate.canonicalBytes
    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        canonical + Data("\n".utf8),
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.currentNow
      )
    )

    let raw = String(decoding: canonical, as: UTF8.self)
    let duplicateSuffix = ",\"schema_version\":1}"
    let duplicateString = String(raw.dropLast()) + duplicateSuffix
    XCTAssertTrue(duplicateString.hasSuffix(duplicateSuffix))
    XCTAssertEqual(duplicateString.components(separatedBy: "\"schema_version\"").count, 3)
    let duplicate = Data(duplicateString.utf8)
    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        duplicate,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.currentNow
      )
    )

    try mutateCandidate(fixture) { fields in
      fields["schema_version"] = .bool(true)
    }
    try mutateCandidate(fixture) { fields in
      fields["native_observation_status"] = .integer(1)
    }
    try mutateCandidate(fixture) { fields in
      fields["canonical_receipt_b64url"] = .string(
        base64URLEncoded(fixture.receipt.canonicalReceipt) + "=")
    }

    guard
      case .object(var receiptFields) = try CanonicalJSON.parseCanonical(
        fixture.receipt.canonicalReceipt
      )
    else { return XCTFail("expected receipt object") }
    receiptFields["signature"] = .string(String(repeating: "A", count: 86))
    let alteredReceipt = try CanonicalJSON.encode(.object(receiptFields))
    try mutateCandidate(fixture) { fields in
      fields["canonical_receipt_b64url"] = .string(base64URLEncoded(alteredReceipt))
    }
  }

  func testWireCapsRejectBeforeAnyCandidateOrChallengeBinding() throws {
    let fixture = try makeFixture()
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1.decode(
        Data(repeating: 0x20, count: NativeApprovalBridgeRequestV1.maximumWireBytes + 1)
      )
    )
    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        Data(repeating: 0x20, count: NativeApprovalBridgeCandidateV1.maximumWireBytes + 1),
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.currentNow
      )
    )
  }

  func testOfflineCandidateHasNoCodableOrRawPayloadDescriptionSurface() throws {
    let fixture = try makeFixture()
    let requestDescription = String(describing: fixture.request)
    let candidateDescription = String(describing: fixture.candidate)

    XCTAssertFalse(fixture.request is any Encodable)
    XCTAssertFalse(fixture.candidate is any Encodable)
    XCTAssertFalse(
      requestDescription.contains(base64URLEncoded(fixture.request.canonicalChallenge)))
    XCTAssertFalse(
      candidateDescription.contains(base64URLEncoded(fixture.candidate.canonicalReceipt)))
    XCTAssertFalse(
      candidateDescription.contains(base64URLEncoded(fixture.candidate.activePublicKeyX963)))
  }

  private func mutateCandidate(
    _ fixture: Fixture,
    mutate: (inout [String: CanonicalJSONValue]) -> Void,
    file: StaticString = #filePath,
    line: UInt = #line
  ) throws {
    guard
      case .object(var fields) = try CanonicalJSON.parseCanonical(fixture.candidate.canonicalBytes)
    else { return XCTFail("expected candidate object", file: file, line: line) }
    mutate(&fields)
    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        CanonicalJSON.encode(.object(fields)),
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.currentNow
      ),
      file: file,
      line: line
    )
  }

  private func makeFixture() throws -> Fixture {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    let exchangeStartedAt = date("2030-01-01T00:00:10Z")
    let currentNow = date("2030-01-01T00:00:13Z")
    let challenge = try ApprovalChallenge.decode(
      try fixtureData("openai-pause-challenge-v1"),
      now: exchangeStartedAt,
      limits: configuration.limits
    )
    let request = try NativeApprovalBridgeRequestV1(
      requestID: Data(0..<32),
      sourceContextDigest: String(repeating: "a", count: 64),
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: challenge.canonicalBytes
    )
    let privateKey = P256.Signing.PrivateKey()
    let publicKey = privateKey.publicKey.x963Representation
    let receipt = try signedReceipt(
      challenge: challenge,
      configuration: configuration,
      privateKey: privateKey,
      issuedAt: date("2030-01-01T00:00:11Z")
    )
    let candidate = try makeNativeApprovalBridgeFixtureCandidate(
      request: request,
      canonicalReceipt: receipt.canonicalReceipt,
      activePublicKeyX963: publicKey,
      nativeObservedAtText: "2030-01-01T00:00:12.000000Z",
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt,
      currentNow: currentNow
    )
    return Fixture(
      metadata: metadata,
      configuration: configuration,
      challenge: challenge,
      request: request,
      publicKey: publicKey,
      receipt: receipt,
      candidate: candidate,
      exchangeStartedAt: exchangeStartedAt,
      currentNow: currentNow
    )
  }

  private func signedReceipt(
    challenge: ApprovalChallenge,
    configuration: TrustedHostConfiguration,
    privateKey: P256.Signing.PrivateKey,
    issuedAt: Date
  ) throws -> SignedApprovalReceipt {
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: issuedAt,
      receiptIdentifier: "approval-receipt:" + base64URLEncoded(Data(repeating: 0xAB, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: try privateKey.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        .derRepresentation
    )
  }

  private func fixtureMetadata() throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:fixture",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try selector(.client, "com.aptsonic.adhelm.client"),
      hostCodeIdentity: try selector(.host, "com.aptsonic.adhelm.host"),
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1"
    )
  }

  private func selector(
    _ role: ApprovalHostCodeIdentityRoleV1,
    _ bundleIdentifier: String
  ) throws -> ApprovalHostCodeIdentitySelectorV1 {
    try ApprovalHostCodeIdentitySelectorV1(
      role: role,
      bundleIdentifier: bundleIdentifier,
      teamIdentifier: "ABCDE12345"
    )
  }

  private func fixtureData(_ name: String) throws -> Data {
    let url = try XCTUnwrap(
      Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
    )
    return try Data(contentsOf: url)
  }

  private func date(_ raw: String) -> Date {
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let value = formatter.date(from: raw) { return value }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: raw)!
  }
}

private struct Fixture {
  let metadata: ApprovalHostBuildMetadataV1
  let configuration: TrustedHostConfiguration
  let challenge: ApprovalChallenge
  let request: NativeApprovalBridgeRequestV1
  let publicKey: Data
  let receipt: SignedApprovalReceipt
  let candidate: NativeApprovalBridgeCandidateV1
  let exchangeStartedAt: Date
  let currentNow: Date
}

import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore

final class NativeApprovalBridgeProtocolTests: XCTestCase {
  func testCanonicalRequestAndCandidateRoundTripBindEveryClosedFact() throws {
    let fixture = try makeFixture()

    XCTAssertEqual(
      fixture.request.metadataDigest,
      try NativeApprovalHostArtifactDescriptorV1(metadata: fixture.metadata).metadataDigest
    )
    XCTAssertEqual(
      fixture.request.protocolConfigurationDigest,
      try NativeApprovalHostArtifactDescriptorV1(metadata: fixture.metadata)
        .protocolConfigurationDigest
    )
    XCTAssertEqual(
      try NativeApprovalBridgeRequestV1.decode(fixture.request.canonicalBytes), fixture.request
    )
    XCTAssertEqual(
      try NativeApprovalBridgeCandidateV1.decode(
        fixture.candidate.canonicalBytes,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.currentNow
      ),
      fixture.candidate
    )
    XCTAssertEqual(
      fixture.candidate.requestIDDigest, CanonicalJSON.sha256Hex(fixture.request.requestID))
    XCTAssertEqual(fixture.candidate.requestDigest, fixture.request.digest)
    XCTAssertEqual(fixture.candidate.sourceContextDigest, fixture.request.sourceContextDigest)
    XCTAssertEqual(fixture.candidate.canonicalChallenge, fixture.request.canonicalChallenge)
    XCTAssertEqual(fixture.candidate.challengeDigest, fixture.challenge.digest)
    XCTAssertEqual(
      fixture.candidate.challengeNonceDigest,
      CanonicalJSON.sha256Hex(fixture.challenge.nonce)
    )
    XCTAssertEqual(fixture.candidate.receiptDigest, fixture.receipt.digest)
    XCTAssertEqual(fixture.candidate.receiptIdentifier, fixture.receipt.receiptIdentifier)
    XCTAssertEqual(fixture.candidate.activePublicKeyX963, fixture.publicKey)
    XCTAssertEqual(
      fixture.candidate.activePublicKeyFingerprint,
      CanonicalJSON.sha256Hex(fixture.publicKey)
    )
  }

  func testRequestRejectsAliasesUnknownFieldsAndNoncanonicalBase64() throws {
    let fixture = try makeFixture()
    guard
      case .object(var fields) = try CanonicalJSON.parseCanonical(fixture.request.canonicalBytes)
    else { return XCTFail("expected request object") }

    fields["schema_version"] = .bool(true)
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1.decode(CanonicalJSON.encode(.object(fields))))

    fields["schema_version"] = .integer(1)
    fields["extra"] = .string("not allowed")
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1.decode(CanonicalJSON.encode(.object(fields))))

    fields.removeValue(forKey: "extra")
    fields["request_id"] = .string(base64URLEncoded(fixture.request.requestID) + "=")
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1.decode(CanonicalJSON.encode(.object(fields))))

    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1.decode(fixture.request.canonicalBytes + Data("\n".utf8))
    )

    let oversized = Data(
      repeating: 0x7B,
      count: NativeApprovalBridgeRequestV1.maximumWireBytes + 1
    )
    XCTAssertThrowsError(try NativeApprovalBridgeRequestV1.decode(oversized)) { error in
      XCTAssertEqual(error as? NativeApprovalBridgeProtocolError, .malformedRequest)
    }
  }

  func testWireDescriptionsAreRedacted() throws {
    let fixture = try makeFixture()

    XCTAssertEqual(
      String(describing: fixture.request),
      "NativeApprovalBridgeRequestV1(redacted)"
    )
    XCTAssertEqual(
      String(reflecting: fixture.request),
      "NativeApprovalBridgeRequestV1(redacted)"
    )
    XCTAssertEqual(
      String(describing: fixture.candidate),
      "NativeApprovalBridgeCandidateV1(redacted)"
    )
    XCTAssertEqual(
      String(reflecting: fixture.candidate),
      "NativeApprovalBridgeCandidateV1(redacted)"
    )
  }

  func testCandidateRejectsRequestAndCompiledMetadataCrossBindingChanges() throws {
    let fixture = try makeFixture()
    guard
      case .object(var fields) = try CanonicalJSON.parseCanonical(fixture.candidate.canonicalBytes)
    else { return XCTFail("expected candidate object") }

    fields["request_digest"] = .string(String(repeating: "0", count: 64))
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    fields["request_digest"] = .string(fixture.request.digest)
    fields["source_context_digest"] = .string(String(repeating: "1", count: 64))
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    fields["source_context_digest"] = .string(fixture.request.sourceContextDigest)
    fields["metadata_digest"] = .string(String(repeating: "2", count: 64))
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    fields["metadata_digest"] = .string(fixture.request.metadataDigest)
    fields["protocol_configuration_digest"] = .string(String(repeating: "3", count: 64))
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)
  }

  func testCandidateRejectsRawAliasesTimeFailuresAndKeyFingerprintChanges() throws {
    let fixture = try makeFixture()
    guard
      case .object(var fields) = try CanonicalJSON.parseCanonical(fixture.candidate.canonicalBytes)
    else { return XCTFail("expected candidate object") }

    fields["schema_version"] = .integer(1)
    fields["native_observation_status"] = .bool(true)
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    fields["native_observation_status"] = .string(NativeApprovalBridgeCandidateV1.observationStatus)
    fields["native_observed_at"] = .string("2030-01-01T00:00:12Z")
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    fields["native_observed_at"] = .string("2030-01-01T00:04:00.000000Z")
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    fields["native_observed_at"] = .string("2030-01-01T00:00:12.000000Z")
    fields["active_public_key_fingerprint"] = .string(String(repeating: "f", count: 64))
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)

    var nonX963Key = fixture.publicKey
    nonX963Key[nonX963Key.startIndex] = 0x02
    fields["active_public_key_x963_b64url"] = .string(base64URLEncoded(nonX963Key))
    fields["active_public_key_fingerprint"] = .string(CanonicalJSON.sha256Hex(nonX963Key))
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)
  }

  func testCandidateRejectsReceiptIssuedAfterNativeObservation() throws {
    let fixture = try makeFixture()
    let laterReceipt = try signedReceipt(
      challenge: fixture.challenge,
      configuration: fixture.configuration,
      privateKey: fixture.privateKey,
      issuedAt: date("2030-01-01T00:00:13.000000Z")
    )
    guard
      case .object(var fields) = try CanonicalJSON.parseCanonical(fixture.candidate.canonicalBytes)
    else { return XCTFail("expected candidate object") }
    fields["canonical_receipt_b64url"] = .string(base64URLEncoded(laterReceipt.canonicalReceipt))
    fields["receipt_digest"] = .string(laterReceipt.digest)
    fields["receipt_id"] = .string(laterReceipt.receiptIdentifier)
    try assertCandidateRejects(CanonicalJSON.encode(.object(fields)), fixture: fixture)
  }

  func testCandidateRejectsTrustedCurrentTimeAtExpiry() throws {
    let fixture = try makeFixture()

    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        fixture.candidate.canonicalBytes,
        expectedRequest: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.exchangeStartedAt,
        currentNow: fixture.challenge.expiresAt
      ))
  }

  func testRequestBindsOnlyDirectRetainedPlanKinds() throws {
    let metadata = try fixtureMetadata()
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1(
        requestID: Data(repeating: 0x55, count: 32),
        sourceContextDigest: String(repeating: "a", count: 64),
        metadataDigest: descriptor.metadataDigest,
        protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
        canonicalChallenge: try fixtureData("delegation-policy-install-challenge-v1")
      )
    )
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1(
        requestID: Data(repeating: 0x55, count: 32),
        sourceContextDigest: String(repeating: "a", count: 64),
        metadataDigest: descriptor.metadataDigest,
        protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
        canonicalChallenge: Data("\"not-a-challenge\"".utf8)
      )
    )
  }

  func testGeneratedCrossLanguageVectorsAreCanonicalAndCrossBound() throws {
    let document = try bridgeVectorDocument()
    XCTAssertEqual(document.sourceCases.count, 2)
    XCTAssertEqual(
      Set(document.sourceCases.map(\.caseID)),
      ["swift_ga4_key_event_create", "swift_openai_pause"]
    )

    for source in document.sourceCases {
      XCTAssertEqual(
        try NativeApprovalBridgeRequestV1.decode(source.request.canonicalBytes),
        source.request
      )
      XCTAssertEqual(source.request.digest, source.requestDigest)
      XCTAssertEqual(
        CanonicalJSON.sha256Hex(source.sourceContextCanonicalBytes),
        source.request.sourceContextDigest
      )
      XCTAssertEqual(
        try NativeApprovalHostArtifactDescriptorV1(metadata: source.metadata).metadataDigest,
        source.request.metadataDigest
      )
      XCTAssertEqual(
        try NativeApprovalHostArtifactDescriptorV1(metadata: source.metadata)
          .protocolConfigurationDigest,
        source.request.protocolConfigurationDigest
      )

      let candidate = try NativeApprovalBridgeCandidateV1.decode(
        source.candidateCanonicalBytes,
        expectedRequest: source.request,
        metadata: source.metadata,
        exchangeStartedAt: source.exchangeStartedAt,
        currentNow: source.currentNow
      )
      XCTAssertEqual(candidate.canonicalBytes, source.candidateCanonicalBytes)
      XCTAssertEqual(candidate.digest, source.candidateDigest)
    }

    let primary = try XCTUnwrap(document.sourceCases.first)
    for invalid in document.invalidInputs {
      switch invalid.target {
      case "REQUEST":
        if invalid.caseID == "request_metadata_digest_mismatch" {
          let parsed = try NativeApprovalBridgeRequestV1.decode(invalid.rawJSON)
          XCTAssertThrowsError(
            try parsed.boundChallenge(
              metadata: primary.metadata,
              exchangeStartedAt: primary.exchangeStartedAt
            ))
        } else {
          XCTAssertThrowsError(try NativeApprovalBridgeRequestV1.decode(invalid.rawJSON))
        }
      case "CANDIDATE":
        XCTAssertThrowsError(
          try NativeApprovalBridgeCandidateV1.decode(
            invalid.rawJSON,
            expectedRequest: primary.request,
            metadata: primary.metadata,
            exchangeStartedAt: primary.exchangeStartedAt,
            currentNow: primary.currentNow
          ))
      default:
        XCTFail("Unexpected fixed fixture target")
      }
    }
  }

  private func assertCandidateRejects(
    _ data: Data,
    fixture: Fixture,
    file: StaticString = #filePath,
    line: UInt = #line
  ) throws {
    XCTAssertThrowsError(
      try NativeApprovalBridgeCandidateV1.decode(
        data,
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
      requestID: Data((0..<32).map(UInt8.init)),
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
      privateKey: privateKey,
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
      receiptIdentifier: "approval-receipt:" + base64URLEncoded(Data(repeating: 0xC3, count: 32))
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

  private func bridgeVectorDocument() throws -> BridgeVectorDocument {
    let bytes = try bridgeFixtureData()
    guard bytes.count <= 1_048_576 else { throw BridgeFixtureError.invalid }
    let root = try CanonicalJSON.parseCanonical(
      bytes,
      limits: CanonicalJSONLimits(
        maximumBytes: 1_048_576,
        maximumDepth: 32,
        maximumContainerItems: 512,
        maximumStringBytes: NativeApprovalBridgeCandidateV1.maximumWireBytes
      )
    )
    let fields = try bridgeObject(
      root,
      exactly: ["schema_version", "scope", "source_cases", "invalid_inputs"]
    )
    guard fields["schema_version"] == .integer(1),
      try bridgeString(fields["scope"])
        == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INTEROPERABILITY_ONLY_NOT_ACCEPTANCE_OR_AUTHORIZATION"
    else { throw BridgeFixtureError.invalid }
    let sourceCases = try bridgeArray(fields["source_cases"])
    let invalidInputs = try bridgeArray(fields["invalid_inputs"])
    guard sourceCases.count == 2, invalidInputs.count == 7 else {
      throw BridgeFixtureError.invalid
    }
    return try BridgeVectorDocument(
      sourceCases: sourceCases.map(bridgeSourceCase),
      invalidInputs: invalidInputs.map(bridgeInvalidInput)
    )
  }

  private func bridgeSourceCase(_ value: CanonicalJSONValue) throws -> BridgeVectorSourceCase {
    let fields = try bridgeObject(
      value,
      exactly: [
        "case_id", "source_fixture", "metadata_fixture", "source_context",
        "source_context_canonical_json", "source_context_digest", "request",
        "request_canonical_json", "request_digest", "candidate", "candidate_canonical_json",
        "candidate_digest", "exchange_started_at", "current_now",
      ]
    )
    let caseID = try bridgeString(fields["case_id"])
    guard !caseID.isEmpty,
      !(try bridgeString(fields["source_fixture"])).isEmpty
    else { throw BridgeFixtureError.invalid }
    let metadata = try bridgeMetadata(fields["metadata_fixture"])
    let sourceContextCanonicalBytes = Data(
      try bridgeString(fields["source_context_canonical_json"]).utf8)
    let sourceContext = try bridgeCanonicalObject(
      sourceContextCanonicalBytes,
      maximumBytes: 4_096,
      maximumItems: 6
    )
    let sourceContextFields = try bridgeObject(
      fields["source_context"],
      exactly: [
        "schema_version", "scope", "workspace_profile_ref", "source_stream",
        "source_plan_record_digest", "checkpoint_digest",
      ]
    )
    guard try CanonicalJSON.encode(.object(sourceContextFields)) == sourceContextCanonicalBytes,
      try CanonicalJSON.encode(.object(sourceContext)) == sourceContextCanonicalBytes,
      try bridgeString(sourceContext["scope"])
        == "ADHELM_NATIVE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V1",
      try bridgeString(sourceContext["source_stream"]) == "changes"
    else { throw BridgeFixtureError.invalid }

    let requestCanonicalBytes = Data(try bridgeString(fields["request_canonical_json"]).utf8)
    let requestFields = try bridgeCanonicalObject(
      requestCanonicalBytes,
      maximumBytes: NativeApprovalBridgeRequestV1.maximumWireBytes,
      maximumItems: 7
    )
    let requestFixtureFields = try bridgeObject(
      fields["request"],
      exactly: Set(requestFields.keys)
    )
    guard try CanonicalJSON.encode(.object(requestFixtureFields)) == requestCanonicalBytes else {
      throw BridgeFixtureError.invalid
    }
    let request = try NativeApprovalBridgeRequestV1.decode(requestCanonicalBytes)
    let requestDigest = try bridgeString(fields["request_digest"])
    guard request.digest == requestDigest,
      request.sourceContextDigest == (try bridgeString(fields["source_context_digest"])),
      request.sourceContextDigest == CanonicalJSON.sha256Hex(sourceContextCanonicalBytes)
    else { throw BridgeFixtureError.invalid }

    let candidateCanonicalBytes = Data(try bridgeString(fields["candidate_canonical_json"]).utf8)
    let candidateFields = try bridgeCanonicalObject(
      candidateCanonicalBytes,
      maximumBytes: NativeApprovalBridgeCandidateV1.maximumWireBytes,
      maximumItems: 19
    )
    let candidateFixtureFields = try bridgeObject(
      fields["candidate"],
      exactly: Set(candidateFields.keys)
    )
    guard try CanonicalJSON.encode(.object(candidateFixtureFields)) == candidateCanonicalBytes
    else {
      throw BridgeFixtureError.invalid
    }
    let candidateDigest = try bridgeString(fields["candidate_digest"])
    guard CanonicalJSON.sha256Hex(candidateCanonicalBytes) == candidateDigest else {
      throw BridgeFixtureError.invalid
    }
    return BridgeVectorSourceCase(
      caseID: caseID,
      metadata: metadata,
      sourceContextCanonicalBytes: sourceContextCanonicalBytes,
      request: request,
      requestDigest: requestDigest,
      candidateCanonicalBytes: candidateCanonicalBytes,
      candidateDigest: candidateDigest,
      exchangeStartedAt: try bridgeDate(fields["exchange_started_at"]),
      currentNow: try bridgeDate(fields["current_now"])
    )
  }

  private func bridgeInvalidInput(_ value: CanonicalJSONValue) throws -> BridgeInvalidInput {
    let fields = try bridgeObject(value, exactly: ["case_id", "target", "raw_json"])
    let caseID = try bridgeString(fields["case_id"])
    guard !caseID.isEmpty else {
      throw BridgeFixtureError.invalid
    }
    let target = try bridgeString(fields["target"])
    guard target == "REQUEST" || target == "CANDIDATE" else { throw BridgeFixtureError.invalid }
    return BridgeInvalidInput(
      caseID: caseID,
      target: target,
      rawJSON: Data(try bridgeString(fields["raw_json"]).utf8)
    )
  }

  private func bridgeMetadata(_ value: CanonicalJSONValue?) throws -> ApprovalHostBuildMetadataV1 {
    let fields = try bridgeObject(
      value,
      exactly: [
        "schema_version", "host_ref", "signing_key_ref", "mach_service_name",
        "client_bundle_identifier", "client_team_identifier", "host_bundle_identifier",
        "host_team_identifier", "signing_key_application_tag_hex", "signing_key_access_group",
        "key_storage", "provenance_service", "provenance_account", "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds", "maximum_issued_at_skew_seconds",
        "maximum_subplans", "maximum_policy_items",
      ]
    )
    guard fields["schema_version"] == .integer(1),
      let keyStorage = ProtectedKeyStorage(rawValue: try bridgeString(fields["key_storage"]))
    else { throw BridgeFixtureError.invalid }
    let accessGroup: String?
    if case .null? = fields["signing_key_access_group"] {
      accessGroup = nil
    } else {
      accessGroup = try bridgeString(fields["signing_key_access_group"])
    }
    return try ApprovalHostBuildMetadataV1(
      hostReference: try bridgeString(fields["host_ref"]),
      signingKeyReference: try bridgeString(fields["signing_key_ref"]),
      machServiceName: try bridgeString(fields["mach_service_name"]),
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: try bridgeString(fields["client_bundle_identifier"]),
        teamIdentifier: try bridgeString(fields["client_team_identifier"])
      ),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: try bridgeString(fields["host_bundle_identifier"]),
        teamIdentifier: try bridgeString(fields["host_team_identifier"])
      ),
      signingKeyApplicationTag: try bridgeHexData(fields["signing_key_application_tag_hex"]),
      signingKeyAccessGroup: accessGroup,
      keyStorage: keyStorage,
      provenanceService: try bridgeString(fields["provenance_service"]),
      provenanceAccount: try bridgeString(fields["provenance_account"]),
      limits: ApprovalHostLimits(
        maximumChallengeBytes: try bridgeInteger(fields["maximum_challenge_bytes"]),
        maximumChallengeLifetime: TimeInterval(
          try bridgeInteger(fields["maximum_challenge_lifetime_seconds"])
        ),
        maximumIssuedAtSkew: TimeInterval(
          try bridgeInteger(fields["maximum_issued_at_skew_seconds"])
        ),
        maximumSubplans: try bridgeInteger(fields["maximum_subplans"]),
        maximumPolicyItems: try bridgeInteger(fields["maximum_policy_items"])
      )
    )
  }

  private func bridgeFixtureData() throws -> Data {
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: "native-approval-bridge-protocol-v1",
        withExtension: "json",
        subdirectory: "Fixtures/NativeApprovalBridge"
      )
    )
    return try Data(contentsOf: url)
  }

  private func bridgeCanonicalObject(
    _ data: Data,
    maximumBytes: Int,
    maximumItems: Int
  ) throws -> [String: CanonicalJSONValue] {
    let root = try CanonicalJSON.parseCanonical(
      data,
      limits: CanonicalJSONLimits(
        maximumBytes: maximumBytes,
        maximumDepth: 1,
        maximumContainerItems: maximumItems,
        maximumStringBytes: 90_000
      )
    )
    guard case .object(let fields) = root else { throw BridgeFixtureError.invalid }
    return fields
  }

  private func bridgeObject(
    _ value: CanonicalJSONValue?,
    exactly expected: Set<String>
  ) throws -> [String: CanonicalJSONValue] {
    guard case .object(let fields)? = value, Set(fields.keys) == expected else {
      throw BridgeFixtureError.invalid
    }
    return fields
  }

  private func bridgeArray(_ value: CanonicalJSONValue?) throws -> [CanonicalJSONValue] {
    guard case .array(let values)? = value else { throw BridgeFixtureError.invalid }
    return values
  }

  private func bridgeString(_ value: CanonicalJSONValue?) throws -> String {
    guard case .string(let result)? = value else { throw BridgeFixtureError.invalid }
    return result
  }

  private func bridgeInteger(_ value: CanonicalJSONValue?) throws -> Int {
    guard case .integer(let result)? = value,
      result >= 0,
      result <= Int64(Int.max)
    else { throw BridgeFixtureError.invalid }
    return Int(result)
  }

  private func bridgeDate(_ value: CanonicalJSONValue?) throws -> Date {
    let raw = try bridgeString(value)
    guard raw.utf8.count == 27 else { throw BridgeFixtureError.invalid }
    return try ClosedTimestamp.parse(raw)
  }

  private func bridgeHexData(_ value: CanonicalJSONValue?) throws -> Data {
    let bytes = Array(try bridgeString(value).utf8)
    guard !bytes.isEmpty, bytes.count.isMultiple(of: 2) else {
      throw BridgeFixtureError.invalid
    }
    var result: [UInt8] = []
    result.reserveCapacity(bytes.count / 2)
    for index in stride(from: 0, to: bytes.count, by: 2) {
      guard let high = bridgeHexNibble(bytes[index]),
        let low = bridgeHexNibble(bytes[index + 1])
      else { throw BridgeFixtureError.invalid }
      result.append((high << 4) | low)
    }
    return Data(result)
  }

  private func bridgeHexNibble(_ byte: UInt8) -> UInt8? {
    switch byte {
    case 48...57: return byte - 48
    case 97...102: return byte - 87
    default: return nil
    }
  }
}

private struct Fixture {
  let metadata: ApprovalHostBuildMetadataV1
  let configuration: TrustedHostConfiguration
  let challenge: ApprovalChallenge
  let request: NativeApprovalBridgeRequestV1
  let privateKey: P256.Signing.PrivateKey
  let publicKey: Data
  let receipt: SignedApprovalReceipt
  let candidate: NativeApprovalBridgeCandidateV1
  let exchangeStartedAt: Date
  let currentNow: Date
}

private struct BridgeVectorDocument {
  let sourceCases: [BridgeVectorSourceCase]
  let invalidInputs: [BridgeInvalidInput]
}

private struct BridgeVectorSourceCase {
  let caseID: String
  let metadata: ApprovalHostBuildMetadataV1
  let sourceContextCanonicalBytes: Data
  let request: NativeApprovalBridgeRequestV1
  let requestDigest: String
  let candidateCanonicalBytes: Data
  let candidateDigest: String
  let exchangeStartedAt: Date
  let currentNow: Date
}

private struct BridgeInvalidInput {
  let caseID: String
  let target: String
  let rawJSON: Data
}

private enum BridgeFixtureError: Error {
  case invalid
}

/// Test-target-only construction for synthetic wire bytes. Production Core
/// deliberately exposes candidate decoding and validation only; no protected
/// post-consumption candidate producer exists in this increment.
func makeNativeApprovalBridgeFixtureCandidate(
  request: NativeApprovalBridgeRequestV1,
  canonicalReceipt: Data,
  activePublicKeyX963: Data,
  nativeObservedAtText: String,
  metadata: ApprovalHostBuildMetadataV1,
  exchangeStartedAt: Date,
  currentNow: Date
) throws -> NativeApprovalBridgeCandidateV1 {
  let checkedRequest = try NativeApprovalBridgeRequestV1.decode(request.canonicalBytes)
  guard checkedRequest == request else { throw NativeApprovalBridgeProtocolError.bindingMismatch }
  let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
  let challenge = try checkedRequest.boundChallenge(
    metadata: metadata,
    exchangeStartedAt: exchangeStartedAt
  )
  let receiptRoot = try CanonicalJSON.parseCanonical(
    canonicalReceipt,
    limits: CanonicalJSONLimits(
      maximumBytes: 16_384,
      maximumDepth: 4,
      maximumContainerItems: 32,
      maximumStringBytes: 1_024
    )
  )
  guard case .object(let receiptFields) = receiptRoot,
    case .string(let receiptIdentifier)? = receiptFields["receipt_id"]
  else { throw NativeApprovalBridgeProtocolError.malformedCandidate }

  let encoded = try CanonicalJSON.encode(
    .object([
      "schema_version": .integer(Int64(NativeApprovalBridgeCandidateV1.schemaVersion)),
      "message_type": .string(NativeApprovalBridgeCandidateV1.messageType),
      "native_observation_status": .string(NativeApprovalBridgeCandidateV1.observationStatus),
      "request_id_digest": .string(CanonicalJSON.sha256Hex(checkedRequest.requestID)),
      "request_digest": .string(checkedRequest.digest),
      "source_context_digest": .string(checkedRequest.sourceContextDigest),
      "metadata_digest": .string(descriptor.metadataDigest),
      "protocol_configuration_digest": .string(descriptor.protocolConfigurationDigest),
      "canonical_challenge_b64url": .string(base64URLEncoded(challenge.canonicalBytes)),
      "challenge_digest": .string(challenge.digest),
      "challenge_nonce_digest": .string(CanonicalJSON.sha256Hex(challenge.nonce)),
      "receipt_kind": .string(challenge.kind.rawValue),
      "expires_at": .string(challenge.expiresAtText),
      "canonical_receipt_b64url": .string(base64URLEncoded(canonicalReceipt)),
      "receipt_digest": .string(CanonicalJSON.sha256Hex(canonicalReceipt)),
      "receipt_id": .string(receiptIdentifier),
      "active_public_key_x963_b64url": .string(base64URLEncoded(activePublicKeyX963)),
      "active_public_key_fingerprint": .string(CanonicalJSON.sha256Hex(activePublicKeyX963)),
      "native_observed_at": .string(nativeObservedAtText),
    ]))
  return try NativeApprovalBridgeCandidateV1.decode(
    encoded,
    expectedRequest: checkedRequest,
    metadata: metadata,
    exchangeStartedAt: exchangeStartedAt,
    currentNow: currentNow
  )
}

import Foundation
import XCTest

@testable import ApprovalHostCore

final class NativeMetaCampaignPauseApprovalBridgeProtocolV2Tests: XCTestCase {
  func testGeneratedVectorsRoundTripAndBindClosedFacts() throws {
    let document = try vectorDocument()
    XCTAssertEqual(
      Set(document.sourceCases.map(\.caseID)),
      ["meta_campaign_pause_fractional", "meta_campaign_pause_whole_seconds"]
    )

    for source in document.sourceCases {
      let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: source.metadata)
      XCTAssertEqual(source.request.metadataDigest, descriptor.metadataDigest)
      XCTAssertEqual(
        source.request.protocolConfigurationDigest,
        descriptor.protocolConfigurationDigest
      )
      XCTAssertEqual(
        CanonicalJSON.sha256Hex(source.sourceContextCanonicalBytes),
        source.request.sourceContextDigest
      )
      XCTAssertEqual(
        try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(source.request.canonicalBytes),
        source.request
      )
      let challenge = try source.request.boundChallenge(
        metadata: source.metadata,
        exchangeStartedAt: source.exchangeStartedAt
      )
      XCTAssertEqual(challenge.kind, .metaCampaignPausePlan)
      XCTAssertEqual(challenge.canonicalBytes, source.request.canonicalChallenge)

      let candidate = try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
        source.candidateCanonicalBytes,
        expectedRequest: source.request,
        metadata: source.metadata,
        exchangeStartedAt: source.exchangeStartedAt,
        currentNow: source.currentNow
      )
      XCTAssertEqual(candidate.canonicalBytes, source.candidateCanonicalBytes)
      XCTAssertEqual(candidate.digest, source.candidateDigest)
      XCTAssertEqual(candidate.requestIDDigest, CanonicalJSON.sha256Hex(source.request.requestID))
      XCTAssertEqual(candidate.requestDigest, source.request.digest)
      XCTAssertEqual(candidate.sourceContextDigest, source.request.sourceContextDigest)
      XCTAssertEqual(candidate.metadataDigest, source.request.metadataDigest)
      XCTAssertEqual(
        candidate.protocolConfigurationDigest,
        source.request.protocolConfigurationDigest
      )
      XCTAssertEqual(candidate.canonicalChallenge, source.request.canonicalChallenge)
      XCTAssertEqual(candidate.challengeDigest, challenge.digest)
      XCTAssertEqual(candidate.challengeNonceDigest, CanonicalJSON.sha256Hex(challenge.nonce))
      XCTAssertEqual(candidate.receiptKind, .metaCampaignPausePlan)
      XCTAssertEqual(candidate.expiresAtText, challenge.expiresAtText)
      XCTAssertEqual(
        candidate.activePublicKeyFingerprint, CanonicalJSON.sha256Hex(candidate.activePublicKeyX963)
      )
    }
  }

  func testGeneratedInvalidInputsAndV1CrossVersionBytesAreRejected() throws {
    let document = try vectorDocument()
    let source = try XCTUnwrap(document.sourceCases.first)

    for invalid in document.invalidInputs {
      switch invalid.target {
      case .request:
        XCTAssertThrowsError(
          try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(invalid.rawJSON),
          "expected V2 request invalid input \(invalid.caseID) to fail"
        )
      case .candidate:
        XCTAssertThrowsError(
          try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
            invalid.rawJSON,
            expectedRequest: source.request,
            metadata: source.metadata,
            exchangeStartedAt: source.exchangeStartedAt,
            currentNow: source.currentNow
          ),
          "expected V2 candidate invalid input \(invalid.caseID) to fail"
        )
      }
    }

    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: source.metadata)
    let v1Request = try NativeApprovalBridgeRequestV1(
      requestID: Data(repeating: 0x55, count: 32),
      sourceContextDigest: String(repeating: "a", count: 64),
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: try fixtureData("ga4-key-event-create-challenge-v1")
    )
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(v1Request.canonicalBytes)
    )
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1.decode(source.request.canonicalBytes)
    )
    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1(
        requestID: Data(repeating: 0x55, count: 32),
        sourceContextDigest: String(repeating: "a", count: 64),
        metadataDigest: descriptor.metadataDigest,
        protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
        canonicalChallenge: source.request.canonicalChallenge
      )
    )
  }

  func testRequestRequiresCompleteCanonicalMetaSubjectBeforeTypedConstruction() throws {
    let source = try XCTUnwrap(try vectorDocument().sourceCases.first)
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: source.metadata)

    let topLevelOnly = try CanonicalJSON.encode(
      .object([
        "schema_version": .integer(1),
        "kind": .string(ApprovalKind.metaCampaignPausePlan.rawValue),
      ])
    )
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalBridgeRequestV2(
        requestID: Data(repeating: 0xA5, count: 32),
        sourceContextDigest: String(repeating: "a", count: 64),
        metadataDigest: descriptor.metadataDigest,
        protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
        canonicalChallenge: topLevelOnly
      )
    )
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(
        requestBytes(source.request, replacingChallenge: topLevelOnly)
      )
    )

    guard
      case .object(var challenge) = try CanonicalJSON.parseCanonical(
        source.request.canonicalChallenge),
      case .object(var action)? = challenge["action"]
    else { return XCTFail("expected M42 challenge action") }
    action.removeValue(forKey: "plan")
    challenge["action"] = .object(action)
    let nestedMutation = try CanonicalJSON.encode(.object(challenge))
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalBridgeRequestV2(
        requestID: Data(repeating: 0xA5, count: 32),
        sourceContextDigest: String(repeating: "a", count: 64),
        metadataDigest: descriptor.metadataDigest,
        protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
        canonicalChallenge: nestedMutation
      )
    )

    let malformedRequest = try requestBytes(
      source.request,
      replacingChallenge: nestedMutation
    )
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(malformedRequest))
  }

  func testCandidateRejectsCorrelationKeyReceiptAndTimeChanges() throws {
    let source = try XCTUnwrap(try vectorDocument().sourceCases.first)
    let challenge = try source.request.boundChallenge(
      metadata: source.metadata,
      exchangeStartedAt: source.exchangeStartedAt
    )

    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["request_id_digest"] = .string(String(repeating: "0", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["request_digest"] = .string(String(repeating: "1", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["source_context_digest"] = .string(String(repeating: "2", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["metadata_digest"] = .string(String(repeating: "3", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["protocol_configuration_digest"] = .string(String(repeating: "4", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["challenge_digest"] = .string(String(repeating: "5", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["challenge_nonce_digest"] = .string(String(repeating: "6", count: 64))
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["receipt_kind"] = .string(ApprovalKind.ga4KeyEventCreatePlan.rawValue)
      },
      source: source
    )
    try assertCandidateRejects(
      try mutatingCandidate(source) { $0["native_observed_at"] = .string("2030-01-01T00:00:30Z") },
      source: source
    )
    try assertCandidateRejects(
      source.candidateCanonicalBytes,
      source: source,
      currentNow: challenge.expiresAt
    )

    let candidateFields = try canonicalObject(
      source.candidateCanonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes,
      maximumItems: 19
    )
    guard case .string(let publicKeyText)? = candidateFields["active_public_key_x963_b64url"],
      var alteredPublicKey = base64URLDecoded(publicKeyText)
    else { return XCTFail("expected candidate public key") }
    alteredPublicKey[alteredPublicKey.startIndex] = 0x02
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["active_public_key_x963_b64url"] = .string(base64URLEncoded(alteredPublicKey))
        $0["active_public_key_fingerprint"] = .string(CanonicalJSON.sha256Hex(alteredPublicKey))
      },
      source: source
    )

    guard case .string(let receiptText)? = candidateFields["canonical_receipt_b64url"],
      var alteredReceipt = base64URLDecoded(receiptText)
    else { return XCTFail("expected candidate receipt") }
    alteredReceipt[alteredReceipt.startIndex] ^= 0x01
    try assertCandidateRejects(
      try mutatingCandidate(source) {
        $0["canonical_receipt_b64url"] = .string(base64URLEncoded(alteredReceipt))
        $0["receipt_digest"] = .string(CanonicalJSON.sha256Hex(alteredReceipt))
      },
      source: source
    )
  }

  func testDescriptionsAreRedacted() throws {
    let source = try XCTUnwrap(try vectorDocument().sourceCases.first)
    let candidate = try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
      source.candidateCanonicalBytes,
      expectedRequest: source.request,
      metadata: source.metadata,
      exchangeStartedAt: source.exchangeStartedAt,
      currentNow: source.currentNow
    )

    XCTAssertEqual(
      String(describing: source.request),
      "NativeMetaCampaignPauseApprovalBridgeRequestV2(redacted)"
    )
    XCTAssertEqual(
      String(reflecting: source.request),
      "NativeMetaCampaignPauseApprovalBridgeRequestV2(redacted)"
    )
    XCTAssertEqual(
      String(describing: candidate),
      "NativeMetaCampaignPauseApprovalBridgeCandidateV2(redacted)"
    )
    XCTAssertEqual(
      String(reflecting: candidate),
      "NativeMetaCampaignPauseApprovalBridgeCandidateV2(redacted)"
    )
  }

  private func assertCandidateRejects(
    _ bytes: Data,
    source: V2BridgeSourceCase,
    currentNow: Date? = nil,
    file: StaticString = #filePath,
    line: UInt = #line
  ) throws {
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
        bytes,
        expectedRequest: source.request,
        metadata: source.metadata,
        exchangeStartedAt: source.exchangeStartedAt,
        currentNow: currentNow ?? source.currentNow
      ),
      file: file,
      line: line
    )
  }

  private func mutatingCandidate(
    _ source: V2BridgeSourceCase,
    _ mutate: (inout [String: CanonicalJSONValue]) -> Void
  ) throws -> Data {
    var fields = try canonicalObject(
      source.candidateCanonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes,
      maximumItems: 19
    )
    mutate(&fields)
    return try CanonicalJSON.encode(.object(fields))
  }

  private func requestBytes(
    _ request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    replacingChallenge: Data
  ) throws -> Data {
    var fields = try canonicalObject(
      request.canonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeRequestV2.maximumWireBytes,
      maximumItems: 7
    )
    fields["canonical_challenge_b64url"] = .string(base64URLEncoded(replacingChallenge))
    return try CanonicalJSON.encode(.object(fields))
  }

  private func vectorDocument() throws -> V2BridgeVectorDocument {
    let bytes = try fixtureData(
      "meta-campaign-pause-approval-bridge-v2",
      subdirectory: "Fixtures/NativeApprovalBridgeV2MetaPause"
    )
    guard bytes.count <= 1_048_576 else { throw V2BridgeFixtureError.invalid }
    let root = try CanonicalJSON.parseCanonical(
      bytes,
      limits: CanonicalJSONLimits(
        maximumBytes: 1_048_576,
        maximumDepth: 32,
        maximumContainerItems: 512,
        maximumStringBytes: NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes
      )
    )
    let fields = try object(
      root,
      exactly: ["schema_version", "fixture_kind", "scope", "source_cases", "invalid_inputs"]
    )
    guard fields["schema_version"] == .integer(1),
      try string(fields["fixture_kind"]) == "META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2",
      try string(fields["scope"])
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_INTEROPERABILITY_ONLY_NOT_ACCEPTANCE_OR_AUTHORIZATION"
    else { throw V2BridgeFixtureError.invalid }
    let sourceCases = try array(fields["source_cases"])
    let invalidInputs = try array(fields["invalid_inputs"])
    guard sourceCases.count == 2, invalidInputs.count == 6 else {
      throw V2BridgeFixtureError.invalid
    }
    return try V2BridgeVectorDocument(
      sourceCases: sourceCases.map(sourceCase),
      invalidInputs: invalidInputs.map(invalidInput)
    )
  }

  private func sourceCase(_ value: CanonicalJSONValue) throws -> V2BridgeSourceCase {
    let fields = try object(
      value,
      exactly: [
        "case_id", "source_fixture", "metadata_fixture", "source_context",
        "source_context_canonical_json", "source_context_digest", "request",
        "request_canonical_json", "request_digest", "candidate", "candidate_canonical_json",
        "candidate_digest", "exchange_started_at", "current_now",
      ]
    )
    let caseID = try string(fields["case_id"])
    let sourceFixture = try string(fields["source_fixture"])
    guard
      (caseID == "meta_campaign_pause_whole_seconds"
        && sourceFixture == "Fixtures/MetaPause/meta-campaign-pause-approval-v1.json")
        || (caseID == "meta_campaign_pause_fractional"
          && sourceFixture == "Fixtures/MetaPause/meta-campaign-pause-approval-fractional-v1.json")
    else { throw V2BridgeFixtureError.invalid }

    let sourceContextCanonicalBytes = Data(try string(fields["source_context_canonical_json"]).utf8)
    let sourceContext = try canonicalObject(
      sourceContextCanonicalBytes,
      maximumBytes: 4_096,
      maximumItems: 6
    )
    let sourceContextFixture = try object(
      fields["source_context"],
      exactly: [
        "schema_version", "scope", "workspace_profile_ref", "source_stream",
        "source_plan_record_digest", "checkpoint_digest",
      ]
    )
    guard try CanonicalJSON.encode(.object(sourceContext)) == sourceContextCanonicalBytes,
      try CanonicalJSON.encode(.object(sourceContextFixture)) == sourceContextCanonicalBytes,
      sourceContext["schema_version"] == .integer(2),
      try string(sourceContext["scope"])
        == "ADHELM_NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V2",
      try string(sourceContext["source_stream"]) == "changes"
    else { throw V2BridgeFixtureError.invalid }

    let requestCanonicalBytes = Data(try string(fields["request_canonical_json"]).utf8)
    let requestFixture = try object(fields["request"], exactly: requestWireFields)
    let requestCanonical = try canonicalObject(
      requestCanonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeRequestV2.maximumWireBytes,
      maximumItems: 7
    )
    guard try CanonicalJSON.encode(.object(requestFixture)) == requestCanonicalBytes,
      requestCanonical.keys == requestFixture.keys
    else { throw V2BridgeFixtureError.invalid }
    let request = try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(requestCanonicalBytes)
    let requestDigest = try string(fields["request_digest"])
    let sourceContextDigest = try string(fields["source_context_digest"])
    guard request.digest == requestDigest,
      request.sourceContextDigest == sourceContextDigest,
      request.sourceContextDigest == CanonicalJSON.sha256Hex(sourceContextCanonicalBytes)
    else { throw V2BridgeFixtureError.invalid }

    let candidateCanonicalBytes = Data(try string(fields["candidate_canonical_json"]).utf8)
    let candidateFixture = try object(fields["candidate"], exactly: candidateWireFields)
    let candidateCanonical = try canonicalObject(
      candidateCanonicalBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeCandidateV2.maximumWireBytes,
      maximumItems: 19
    )
    let candidateDigest = try string(fields["candidate_digest"])
    guard try CanonicalJSON.encode(.object(candidateFixture)) == candidateCanonicalBytes,
      candidateCanonical.keys == candidateFixture.keys,
      CanonicalJSON.sha256Hex(candidateCanonicalBytes) == candidateDigest
    else { throw V2BridgeFixtureError.invalid }

    return V2BridgeSourceCase(
      caseID: caseID,
      metadata: try metadata(fields["metadata_fixture"]),
      sourceContextCanonicalBytes: sourceContextCanonicalBytes,
      request: request,
      requestDigest: requestDigest,
      candidateCanonicalBytes: candidateCanonicalBytes,
      candidateDigest: candidateDigest,
      exchangeStartedAt: try sixFractionDate(fields["exchange_started_at"]),
      currentNow: try sixFractionDate(fields["current_now"])
    )
  }

  private func invalidInput(_ value: CanonicalJSONValue) throws -> V2BridgeInvalidInput {
    let fields = try object(value, exactly: ["case_id", "target", "raw_json"])
    let caseID = try string(fields["case_id"])
    guard !caseID.isEmpty else { throw V2BridgeFixtureError.invalid }
    guard let target = V2BridgeInvalidTarget(rawValue: try string(fields["target"])) else {
      throw V2BridgeFixtureError.invalid
    }
    return V2BridgeInvalidInput(
      caseID: caseID,
      target: target,
      rawJSON: Data(try string(fields["raw_json"]).utf8)
    )
  }

  private func metadata(_ value: CanonicalJSONValue?) throws -> ApprovalHostBuildMetadataV1 {
    let fields = try object(
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
      let keyStorage = ProtectedKeyStorage(rawValue: try string(fields["key_storage"]))
    else { throw V2BridgeFixtureError.invalid }
    let accessGroup: String?
    if case .null? = fields["signing_key_access_group"] {
      accessGroup = nil
    } else {
      accessGroup = try string(fields["signing_key_access_group"])
    }
    return try ApprovalHostBuildMetadataV1(
      hostReference: try string(fields["host_ref"]),
      signingKeyReference: try string(fields["signing_key_ref"]),
      machServiceName: try string(fields["mach_service_name"]),
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: try string(fields["client_bundle_identifier"]),
        teamIdentifier: try string(fields["client_team_identifier"])
      ),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: try string(fields["host_bundle_identifier"]),
        teamIdentifier: try string(fields["host_team_identifier"])
      ),
      signingKeyApplicationTag: try hexData(fields["signing_key_application_tag_hex"]),
      signingKeyAccessGroup: accessGroup,
      keyStorage: keyStorage,
      provenanceService: try string(fields["provenance_service"]),
      provenanceAccount: try string(fields["provenance_account"]),
      limits: ApprovalHostLimits(
        maximumChallengeBytes: try integer(fields["maximum_challenge_bytes"]),
        maximumChallengeLifetime: TimeInterval(
          try integer(fields["maximum_challenge_lifetime_seconds"])
        ),
        maximumIssuedAtSkew: TimeInterval(
          try integer(fields["maximum_issued_at_skew_seconds"])
        ),
        maximumSubplans: try integer(fields["maximum_subplans"]),
        maximumPolicyItems: try integer(fields["maximum_policy_items"])
      )
    )
  }

  private func fixtureData(_ name: String, subdirectory: String = "Fixtures") throws -> Data {
    let url = try XCTUnwrap(
      Bundle.module.url(forResource: name, withExtension: "json", subdirectory: subdirectory)
    )
    return try Data(contentsOf: url)
  }

  private func canonicalObject(
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
    guard case .object(let fields) = root else { throw V2BridgeFixtureError.invalid }
    return fields
  }

  private func object(
    _ value: CanonicalJSONValue?,
    exactly expected: Set<String>
  ) throws -> [String: CanonicalJSONValue] {
    guard case .object(let fields)? = value, Set(fields.keys) == expected else {
      throw V2BridgeFixtureError.invalid
    }
    return fields
  }

  private func array(_ value: CanonicalJSONValue?) throws -> [CanonicalJSONValue] {
    guard case .array(let values)? = value else { throw V2BridgeFixtureError.invalid }
    return values
  }

  private func string(_ value: CanonicalJSONValue?) throws -> String {
    guard case .string(let result)? = value else { throw V2BridgeFixtureError.invalid }
    return result
  }

  private func integer(_ value: CanonicalJSONValue?) throws -> Int {
    guard case .integer(let result)? = value,
      result >= 0,
      result <= Int64(Int.max)
    else { throw V2BridgeFixtureError.invalid }
    return Int(result)
  }

  private func sixFractionDate(_ value: CanonicalJSONValue?) throws -> Date {
    let raw = try string(value)
    guard raw.utf8.count == 27 else { throw V2BridgeFixtureError.invalid }
    return try ClosedTimestamp.parse(raw)
  }

  private func hexData(_ value: CanonicalJSONValue?) throws -> Data {
    let bytes = Array(try string(value).utf8)
    guard !bytes.isEmpty, bytes.count.isMultiple(of: 2) else {
      throw V2BridgeFixtureError.invalid
    }
    var result: [UInt8] = []
    result.reserveCapacity(bytes.count / 2)
    for index in stride(from: 0, to: bytes.count, by: 2) {
      guard let high = hexNibble(bytes[index]), let low = hexNibble(bytes[index + 1]) else {
        throw V2BridgeFixtureError.invalid
      }
      result.append((high << 4) | low)
    }
    return Data(result)
  }

  private func hexNibble(_ byte: UInt8) -> UInt8? {
    switch byte {
    case 48...57: return byte - 48
    case 97...102: return byte - 87
    default: return nil
    }
  }

  private func base64URLDecoded(_ raw: String) -> Data? {
    guard
      raw.utf8.allSatisfy({
        (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0)
          || $0 == 45 || $0 == 95
      })
    else { return nil }
    let base64 = raw.replacingOccurrences(of: "-", with: "+")
      .replacingOccurrences(of: "_", with: "/")
    let padding = String(repeating: "=", count: (4 - base64.utf8.count % 4) % 4)
    guard let decoded = Data(base64Encoded: base64 + padding), base64URLEncoded(decoded) == raw
    else {
      return nil
    }
    return decoded
  }

  private let requestWireFields: Set<String> = [
    "schema_version", "message_type", "request_id", "source_context_digest", "metadata_digest",
    "protocol_configuration_digest", "canonical_challenge_b64url",
  ]

  private let candidateWireFields: Set<String> = [
    "schema_version", "message_type", "native_observation_status", "request_id_digest",
    "request_digest", "source_context_digest", "metadata_digest", "protocol_configuration_digest",
    "canonical_challenge_b64url", "challenge_digest", "challenge_nonce_digest", "receipt_kind",
    "expires_at", "canonical_receipt_b64url", "receipt_digest", "receipt_id",
    "active_public_key_x963_b64url", "active_public_key_fingerprint", "native_observed_at",
  ]
}

private struct V2BridgeVectorDocument {
  let sourceCases: [V2BridgeSourceCase]
  let invalidInputs: [V2BridgeInvalidInput]
}

private struct V2BridgeSourceCase {
  let caseID: String
  let metadata: ApprovalHostBuildMetadataV1
  let sourceContextCanonicalBytes: Data
  let request: NativeMetaCampaignPauseApprovalBridgeRequestV2
  let requestDigest: String
  let candidateCanonicalBytes: Data
  let candidateDigest: String
  let exchangeStartedAt: Date
  let currentNow: Date
}

private struct V2BridgeInvalidInput {
  let caseID: String
  let target: V2BridgeInvalidTarget
  let rawJSON: Data
}

private enum V2BridgeInvalidTarget: String {
  case request = "REQUEST"
  case candidate = "CANDIDATE"
}

private enum V2BridgeFixtureError: Error {
  case invalid
}

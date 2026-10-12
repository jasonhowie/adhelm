import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore

final class ReceiptRedemptionStateTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_030)

  func testCanonicalPendingVerifiedAndGlobalStateRoundTrip() throws {
    let binding = try sourceBinding()
    let pending = try ReceiptRedemptionPendingV1(
      binding: binding,
      reservedAtText: "2030-01-01T00:00:30.000000Z"
    )
    let verified = try ReceiptRedemptionVerifiedV1(
      pending: pending,
      verifiedAtText: "2030-01-01T00:00:31.000000Z"
    )
    let pendingState = ReceiptRedemptionStateV1.pending(pending)
    let verifiedState = ReceiptRedemptionStateV1.verified(verified)

    XCTAssertEqual(try ReceiptRedemptionStateV1.decode(pendingState.canonicalBytes()), pendingState)
    XCTAssertEqual(
      try ReceiptRedemptionStateV1.decode(verifiedState.canonicalBytes()), verifiedState)

    let initial = ReceiptRedemptionGlobalStateV1.ready(
      try ReceiptRedemptionGlobalReadyV1(
        generation: 1,
        observedAtText: "2030-01-01T00:00:29.000000Z"
      )
    )
    let intent = try initial.nextIntent(
      binding: binding,
      observedAtText: "2030-01-01T00:00:30.000000Z"
    )
    let final = try intent.nextReady(
      expectedBinding: binding,
      observedAtText: "2030-01-01T00:00:31.000000Z"
    )
    XCTAssertEqual(try ReceiptRedemptionGlobalStateV1.decode(initial.canonicalBytes()), initial)
    XCTAssertEqual(try ReceiptRedemptionGlobalStateV1.decode(intent.canonicalBytes()), intent)
    XCTAssertEqual(try ReceiptRedemptionGlobalStateV1.decode(final.canonicalBytes()), final)
  }

  func testStateDecoderRejectsNoncanonicalAndCrossStateTimestampChanges() throws {
    let binding = try sourceBinding()
    let pending = try ReceiptRedemptionPendingV1(
      binding: binding,
      reservedAtText: "2030-01-01T00:00:30.000000Z"
    )
    let verified = try ReceiptRedemptionStateV1.verified(
      ReceiptRedemptionVerifiedV1(
        pending: pending,
        verifiedAtText: "2030-01-01T00:00:31.000000Z"
      )
    )
    let bytes = try verified.canonicalBytes()
    XCTAssertThrowsError(try ReceiptRedemptionStateV1.decode(bytes + Data("\n".utf8)))

    guard case .object(var fields) = try CanonicalJSON.parseCanonical(bytes) else {
      return XCTFail("expected state object")
    }
    fields["verified_at"] = .string("2030-01-01T00:00:29.000000Z")
    XCTAssertThrowsError(try ReceiptRedemptionStateV1.decode(CanonicalJSON.encode(.object(fields))))
    fields["schema_version"] = .bool(true)
    XCTAssertThrowsError(try ReceiptRedemptionStateV1.decode(CanonicalJSON.encode(.object(fields))))
  }

  func testSourceBindingRejectsCanonicalReceiptWithWrongChallengeOrDigest() throws {
    let challenge = try loadChallenge()
    let config = try configuration()
    let receipt = try makeReceipt(challenge: challenge, configuration: config)
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(receipt.canonicalReceipt)
    else {
      return XCTFail("expected receipt object")
    }
    fields["challenge_digest"] = .string(String(repeating: "0", count: 64))
    let wrongDigest = try CanonicalJSON.encode(.object(fields))
    XCTAssertThrowsError(
      try ReceiptRedemptionSourceBindingV1(
        configuration: config,
        expectedChallenge: challenge,
        canonicalReceipt: wrongDigest
      )
    )

    fields["challenge_digest"] = .string(challenge.digest)
    fields["challenge_nonce"] = .string(base64URLEncoded(Data(repeating: 0xAA, count: 32)))
    let wrongNonce = try CanonicalJSON.encode(.object(fields))
    XCTAssertThrowsError(
      try ReceiptRedemptionSourceBindingV1(
        configuration: config,
        expectedChallenge: challenge,
        canonicalReceipt: wrongNonce
      )
    )
  }

  func testSourceBindingRejectsExtraOrAlteredUnsignedReceiptFields() throws {
    let challenge = try loadChallenge()
    let config = try configuration()
    let receipt = try makeReceipt(challenge: challenge, configuration: config)
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(receipt.canonicalReceipt)
    else {
      return XCTFail("expected receipt object")
    }
    fields["unbound"] = .string("unexpected")
    XCTAssertThrowsError(
      try ReceiptRedemptionSourceBindingV1(
        configuration: config,
        expectedChallenge: challenge,
        canonicalReceipt: CanonicalJSON.encode(.object(fields))
      )
    )

    fields.removeValue(forKey: "unbound")
    fields["review_digest"] = .string(String(repeating: "0", count: 64))
    XCTAssertThrowsError(
      try ReceiptRedemptionSourceBindingV1(
        configuration: config,
        expectedChallenge: challenge,
        canonicalReceipt: CanonicalJSON.encode(.object(fields))
      )
    )
  }

  func testSourceBindingRejectsConstructedReceiptMismatch() throws {
    let challenge = try loadChallenge()
    let config = try configuration()
    let receipt = try makeReceipt(challenge: challenge, configuration: config)
    let binding = try ReceiptRedemptionSourceBindingV1(
      configuration: config,
      expectedChallenge: challenge,
      canonicalReceipt: receipt.canonicalReceipt
    )
    let otherChallenge = try challengeWithNonce(Data(repeating: 0xA5, count: 32))
    let otherReceipt = try makeReceipt(challenge: otherChallenge, configuration: config)
    XCTAssertFalse(
      binding.matchesVerifiedReceipt(
        otherReceipt,
        configuration: config,
        expectedChallenge: challenge
      )
    )
  }

  func testTimestampBoundsRejectReservationOrVerificationAtExpiry() throws {
    let binding = try sourceBinding()
    XCTAssertThrowsError(
      try ReceiptRedemptionPendingV1(binding: binding, reservedAtText: binding.expiresAtText)
    )
    let pending = try ReceiptRedemptionPendingV1(
      binding: binding,
      reservedAtText: "2030-01-01T00:00:30.000000Z"
    )
    XCTAssertThrowsError(
      try ReceiptRedemptionVerifiedV1(pending: pending, verifiedAtText: binding.expiresAtText)
    )
  }

  func testGlobalTransitionsAreOrderedBindingSpecificAndLeaveTwoGenerationSlots() throws {
    let binding = try sourceBinding()
    let ready = ReceiptRedemptionGlobalStateV1.ready(
      try ReceiptRedemptionGlobalReadyV1(
        generation: 1,
        observedAtText: "2030-01-01T00:00:29.000000Z"
      )
    )
    XCTAssertThrowsError(
      try ready.nextReady(
        expectedBinding: binding,
        observedAtText: "2030-01-01T00:00:30.000000Z"
      )
    )
    let intent = try ready.nextIntent(
      binding: binding,
      observedAtText: "2030-01-01T00:00:30.000000Z"
    )
    XCTAssertThrowsError(
      try intent.nextIntent(
        binding: binding,
        observedAtText: "2030-01-01T00:00:31.000000Z"
      )
    )
    XCTAssertThrowsError(
      try intent.nextReady(
        expectedBinding: binding,
        observedAtText: "2030-01-01T00:00:29.000000Z"
      )
    )
    let nearlyExhausted = ReceiptRedemptionGlobalStateV1.ready(
      try ReceiptRedemptionGlobalReadyV1(
        generation: CanonicalJSON.maximumSafeInteger - 1,
        observedAtText: "2030-01-01T00:00:29.000000Z"
      )
    )
    XCTAssertThrowsError(
      try nearlyExhausted.nextIntent(
        binding: binding,
        observedAtText: "2030-01-01T00:00:30.000000Z"
      )
    )
  }

  func testGeneratedCrossLanguageStateVectorsAreCanonicalAndNonAuthorizing() throws {
    let fixture = try loadStateFixture()
    XCTAssertEqual(fixture.schemaVersion, 1)
    XCTAssertEqual(
      fixture.scope,
      "RECEIPT_REDEMPTION_STATE_INTEROPERABILITY_ONLY_NOT_RECEIPT_ACCEPTANCE_OR_AUTHORIZATION"
    )
    XCTAssertEqual(
      Set(fixture.sourceCases.map(\.caseID)),
      ["swift_ga4_key_event_create", "swift_openai_pause"]
    )

    for sourceCase in fixture.sourceCases {
      let pendingBytes = Data(sourceCase.pendingStateCanonicalJSON.utf8)
      let verifiedBytes = Data(sourceCase.verifiedStateCanonicalJSON.utf8)
      let initialGlobalBytes = Data(sourceCase.initialGlobalStateCanonicalJSON.utf8)
      let intentGlobalBytes = Data(sourceCase.intentPendingGlobalStateCanonicalJSON.utf8)
      let finalGlobalBytes = Data(sourceCase.finalGlobalStateCanonicalJSON.utf8)
      let pending = try ReceiptRedemptionStateV1.decode(pendingBytes)
      let verified = try ReceiptRedemptionStateV1.decode(verifiedBytes)
      XCTAssertEqual(try pending.canonicalBytes(), pendingBytes)
      XCTAssertEqual(try verified.canonicalBytes(), verifiedBytes)
      XCTAssertEqual(
        try ReceiptRedemptionGlobalStateV1.decode(initialGlobalBytes).canonicalBytes(),
        initialGlobalBytes)
      XCTAssertEqual(
        try ReceiptRedemptionGlobalStateV1.decode(intentGlobalBytes).canonicalBytes(),
        intentGlobalBytes)
      XCTAssertEqual(
        try ReceiptRedemptionGlobalStateV1.decode(finalGlobalBytes).canonicalBytes(),
        finalGlobalBytes)

      guard case .object(let fields) = try CanonicalJSON.parseCanonical(pendingBytes),
        case .object(let binding) = fields["binding"]
      else { return XCTFail("expected pending binding") }
      XCTAssertEqual(
        try CanonicalJSON.encode(.object(binding)),
        Data(sourceCase.bindingCanonicalJSON.utf8)
      )
    }

    for invalidInput in fixture.invalidInputs {
      let raw = Data(invalidInput.rawJSON.utf8)
      switch invalidInput.target {
      case "NONCE_STATE":
        XCTAssertThrowsError(try ReceiptRedemptionStateV1.decode(raw), invalidInput.caseID)
      case "GLOBAL_STATE":
        XCTAssertThrowsError(
          try ReceiptRedemptionGlobalStateV1.decode(raw), invalidInput.caseID)
      default:
        XCTFail("unexpected fixture target")
      }
    }

    for recovery in fixture.recoveryInputs {
      switch recovery.caseID {
      case "missing_or_null_global_state":
        XCTAssertEqual(recovery.missingGlobalState, true)
        XCTAssertThrowsError(
          try ReceiptRedemptionGlobalStateV1.decode(
            Data((recovery.nullGlobalRawJSON ?? "").utf8)
          )
        )
      case "pending_with_ready_global_state", "pending_with_different_intent_binding":
        let globalBytes = Data((recovery.globalStateCanonicalJSON ?? "").utf8)
        let nonceBytes = Data((recovery.nonceStateCanonicalJSON ?? "").utf8)
        XCTAssertEqual(
          try ReceiptRedemptionGlobalStateV1.decode(globalBytes).canonicalBytes(), globalBytes)
        XCTAssertEqual(try ReceiptRedemptionStateV1.decode(nonceBytes).canonicalBytes(), nonceBytes)
      default:
        XCTFail("unexpected recovery case \(recovery.caseID)")
      }
    }
  }

  private func sourceBinding() throws -> ReceiptRedemptionSourceBindingV1 {
    let challenge = try loadChallenge()
    let config = try configuration()
    let receipt = try makeReceipt(challenge: challenge, configuration: config)
    return try ReceiptRedemptionSourceBindingV1(
      configuration: config,
      expectedChallenge: challenge,
      canonicalReceipt: receipt.canonicalReceipt
    )
  }

  private func loadChallenge() throws -> ApprovalChallenge {
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: "openai-pause-challenge-v1",
        withExtension: "json",
        subdirectory: "Fixtures"
      )
    )
    return try ApprovalChallenge.decode(Data(contentsOf: url), now: now)
  }

  private func challengeWithNonce(_ nonce: Data) throws -> ApprovalChallenge {
    let original = try loadChallenge()
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(original.canonicalBytes)
    else {
      throw ApprovalHostError.malformedChallenge
    }
    fields["nonce"] = .string(base64URLEncoded(nonce))
    return try ApprovalChallenge.decode(try CanonicalJSON.encode(.object(fields)), now: now)
  }

  private func configuration() throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:fixture",
      machServiceName: "invalid.fixture.service",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256
    )
  }

  private func makeReceipt(
    challenge: ApprovalChallenge,
    configuration: TrustedHostConfiguration
  ) throws -> SignedApprovalReceipt {
    let key = P256.Signing.PrivateKey()
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + base64URLEncoded(Data(repeating: 0xD2, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        .derRepresentation
    )
  }

  private func loadStateFixture() throws -> ReceiptRedemptionFixture {
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: "receipt-redemption-state-v1",
        withExtension: "json",
        subdirectory: "Fixtures/ReceiptRedemption"
      )
    )
    let data = try Data(contentsOf: url)
    _ = try CanonicalJSON.parseCanonical(
      data,
      limits: CanonicalJSONLimits(
        maximumBytes: 32_768,
        maximumDepth: 12,
        maximumContainerItems: 64,
        maximumStringBytes: 8_192
      )
    )
    return try JSONDecoder().decode(ReceiptRedemptionFixture.self, from: data)
  }
}

private struct ReceiptRedemptionFixture: Decodable {
  let schemaVersion: Int
  let scope: String
  let sourceCases: [ReceiptRedemptionFixtureSourceCase]
  let invalidInputs: [ReceiptRedemptionFixtureInvalidInput]
  let recoveryInputs: [ReceiptRedemptionFixtureRecoveryInput]

  enum CodingKeys: String, CodingKey {
    case schemaVersion = "schema_version"
    case scope
    case sourceCases = "source_cases"
    case invalidInputs = "invalid_inputs"
    case recoveryInputs = "recovery_inputs"
  }
}

private struct ReceiptRedemptionFixtureSourceCase: Decodable {
  let caseID: String
  let bindingCanonicalJSON: String
  let pendingStateCanonicalJSON: String
  let verifiedStateCanonicalJSON: String
  let initialGlobalStateCanonicalJSON: String
  let intentPendingGlobalStateCanonicalJSON: String
  let finalGlobalStateCanonicalJSON: String

  enum CodingKeys: String, CodingKey {
    case caseID = "case_id"
    case bindingCanonicalJSON = "binding_canonical_json"
    case pendingStateCanonicalJSON = "pending_state_canonical_json"
    case verifiedStateCanonicalJSON = "verified_state_canonical_json"
    case initialGlobalStateCanonicalJSON = "initial_global_state_canonical_json"
    case intentPendingGlobalStateCanonicalJSON = "intent_pending_global_state_canonical_json"
    case finalGlobalStateCanonicalJSON = "final_global_state_canonical_json"
  }
}

private struct ReceiptRedemptionFixtureInvalidInput: Decodable {
  let caseID: String
  let rawJSON: String
  let target: String

  enum CodingKeys: String, CodingKey {
    case caseID = "case_id"
    case rawJSON = "raw_json"
    case target
  }
}

private struct ReceiptRedemptionFixtureRecoveryInput: Decodable {
  let caseID: String
  let missingGlobalState: Bool?
  let nullGlobalRawJSON: String?
  let globalStateCanonicalJSON: String?
  let nonceStateCanonicalJSON: String?

  enum CodingKeys: String, CodingKey {
    case caseID = "case_id"
    case missingGlobalState = "missing_global_state"
    case nullGlobalRawJSON = "null_global_raw_json"
    case globalStateCanonicalJSON = "global_state_canonical_json"
    case nonceStateCanonicalJSON = "nonce_state_canonical_json"
  }
}

import Foundation
import XCTest

@testable import ApprovalHostCore

final class InitialEnrollmentRedemptionGlobalSetupTests: XCTestCase {
  private let identifier = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  private let fingerprint = String(repeating: "a", count: 64)
  private let otherFingerprint = String(repeating: "b", count: 64)
  private let initialObservedAt = "2030-01-01T00:00:00.123456Z"

  func testV1ActiveBytesRemainExactAndV2NotStartedDoesNotUpcastThem() throws {
    let active = try identity()
    let legacy = InitialEnrollmentProvenance.active(active)
    let legacyBytes = try legacy.canonicalBytes()

    XCTAssertEqual(
      legacyBytes,
      Data(
        #"{"created_at":"2030-01-01T00:00:00.123456Z","enrollment_id":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","host_ref":"host:fixture","key_ref":"key:fixture","key_storage":"KEYCHAIN_P256","public_key_x963_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1,"state":"ACTIVE"}"#
          .utf8
      )
    )
    XCTAssertEqual(try InitialEnrollmentProvenance.decode(legacyBytes), legacy)

    let v2 = try notStarted()
    let v2Bytes = try InitialEnrollmentProvenance.activeV2(v2).canonicalBytes()
    XCTAssertTrue(v2Bytes.contains(Data(#""schema_version":2"#.utf8)))
    XCTAssertTrue(
      v2Bytes.contains(
        Data(#""redemption_global_setup":{"schema_version":1,"state":"NOT_STARTED"}"#.utf8)))
    XCTAssertEqual(try InitialEnrollmentProvenance.decode(v2Bytes), .activeV2(v2))
    XCTAssertEqual(try legacy.canonicalBytes(), legacyBytes)
  }

  func testV2SetupStatesRequireExactClosedNestedForms() throws {
    let complete = try complete()
    let bytes = try InitialEnrollmentProvenance.activeV2(complete).canonicalBytes()
    XCTAssertEqual(try InitialEnrollmentProvenance.decode(bytes), .activeV2(complete))

    let value = try CanonicalJSON.parseCanonical(bytes)
    guard case .object(var fields) = value,
      case .object(var setup) = fields["redemption_global_setup"]
    else { return XCTFail("expected V2 setup object") }

    setup["schema_version"] = .bool(true)
    fields["redemption_global_setup"] = .object(setup)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(fields)))
    )

    guard case .object(var notStartedFields) = value else {
      return XCTFail("expected V2 object")
    }
    notStartedFields["redemption_global_setup"] = .object([
      "schema_version": .integer(1),
      "state": .string("NOT_STARTED"),
      "slot_digest": .string(fingerprint),
    ])
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(notStartedFields)))
    )

    guard case .object(var wrongGenerationFields) = value,
      case .object(var wrongGenerationSetup) = wrongGenerationFields["redemption_global_setup"]
    else { return XCTFail("expected V2 setup object") }
    wrongGenerationSetup["initial_global_generation"] = .integer(2)
    wrongGenerationFields["redemption_global_setup"] = .object(wrongGenerationSetup)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(
        try CanonicalJSON.encode(.object(wrongGenerationFields)))
    )

    guard case .object(var wholeSecondFields) = value,
      case .object(var wholeSecondSetup) = wholeSecondFields["redemption_global_setup"]
    else { return XCTFail("expected V2 setup object") }
    wholeSecondSetup["initial_global_observed_at"] = .string("2030-01-01T00:00:00Z")
    wholeSecondFields["redemption_global_setup"] = .object(wholeSecondSetup)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(wholeSecondFields)))
    )
  }

  func testV2TransitionsAreOneWayAndBindActiveKeyAndSlot() throws {
    let notStarted = try notStarted()
    let binding = try setupBinding()
    let intent = try InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
      active: notStarted,
      binding: binding
    )
    let complete = try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
      active: intent,
      binding: binding
    )
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
        active: intent,
        binding: binding
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .recoveryRequired)
    }
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
        active: notStarted,
        binding: binding
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .recoveryRequired)
    }
    XCTAssertEqual(
      try InitialEnrollmentTransitions.requireCompletedRedemptionGlobalSetup(
        active: complete,
        expectedSlotDigest: binding.slotDigest,
        observedPublicKeyX963SHA256: fingerprint
      ),
      binding
    )
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.requireCompletedRedemptionGlobalSetup(
        active: complete,
        expectedSlotDigest: binding.slotDigest,
        observedPublicKeyX963SHA256: otherFingerprint
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .recoveryRequired)
    }
  }

  private func intent() throws -> InitialEnrollmentIntent {
    try InitialEnrollmentIntent(
      hostReference: "host:fixture",
      signingKeyReference: "key:fixture",
      keyStorage: .keychainP256
    )
  }

  private func identity() throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: identifier,
      intent: intent(),
      publicKeyX963SHA256: fingerprint,
      createdAtText: initialObservedAt
    )
  }

  private func notStarted() throws -> InitialEnrollmentActiveV2 {
    let active = try identity()
    return try InitialEnrollmentActiveV2(
      enrollmentID: active.enrollmentID,
      intent: active.intent,
      publicKeyX963SHA256: active.publicKeyX963SHA256,
      createdAtText: active.createdAtText,
      redemptionGlobalSetup: .notStarted
    )
  }

  private func setupBinding() throws -> InitialEnrollmentRedemptionGlobalSetupBinding {
    try InitialEnrollmentRedemptionGlobalSetupBinding(
      slotDigest: otherFingerprint,
      initialGlobalDigest: String(repeating: "c", count: 64),
      initialGeneration: 1,
      initialObservedAtText: initialObservedAt,
      activeKeyX963SHA256: fingerprint
    )
  }

  private func complete() throws -> InitialEnrollmentActiveV2 {
    let binding = try setupBinding()
    return try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
      active: InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
        active: try notStarted(),
        binding: binding
      ),
      binding: binding
    )
  }
}

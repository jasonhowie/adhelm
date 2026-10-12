import Foundation
import XCTest

@testable import ApprovalHostCore

/// Independent M36 closed-wire and transition checks. These are pure Core
/// tests: they do not construct a protected-store adapter, key, helper, XPC
/// connection, or authentication session.
final class InitialEnrollmentRedemptionGlobalSetupUltraReviewTests: XCTestCase {
  private let identifier = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  private let fingerprint = String(repeating: "a", count: 64)
  private let otherFingerprint = String(repeating: "b", count: 64)
  private let observedAt = "2030-01-01T00:00:00.123456Z"

  func testLegacyActiveCannotAcquireAMarkerThroughSchemaOrFieldRewrite() throws {
    let legacy = InitialEnrollmentProvenance.active(try identity())
    let bytes = try legacy.canonicalBytes()
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(bytes) else {
      return XCTFail("expected ACTIVE object")
    }

    fields["redemption_global_setup"] = .object([
      "schema_version": .integer(1),
      "state": .string("NOT_STARTED"),
    ])
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(fields))))

    fields.removeValue(forKey: "redemption_global_setup")
    fields["schema_version"] = .integer(2)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(fields))))
    XCTAssertEqual(try legacy.canonicalBytes(), bytes)
  }

  func testNestedBindingRejectsScalarAliasesAndControlSuffixes() throws {
    guard
      case .object(var fields) = try CanonicalJSON.parseCanonical(
        try InitialEnrollmentProvenance.activeV2(try complete()).canonicalBytes()
      ), case .object(var setup) = fields["redemption_global_setup"]
    else {
      return XCTFail("expected V2 setup object")
    }

    setup["initial_global_generation"] = .bool(true)
    fields["redemption_global_setup"] = .object(setup)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(fields))))

    guard
      case .object(var suffixFields) = try CanonicalJSON.parseCanonical(
        try InitialEnrollmentProvenance.activeV2(try complete()).canonicalBytes()
      ), case .object(var suffixSetup) = suffixFields["redemption_global_setup"]
    else {
      return XCTFail("expected V2 setup object")
    }
    suffixSetup["initial_global_observed_at"] = .string(observedAt + "\n")
    suffixFields["redemption_global_setup"] = .object(suffixSetup)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(suffixFields)))
    )
  }

  func testSetupTransitionsRequireTheExactIntentBindingAndObservedKey() throws {
    let binding = try setupBinding(initialDigest: String(repeating: "c", count: 64))
    let alternate = try setupBinding(initialDigest: String(repeating: "d", count: 64))
    let intent = try InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
      active: try notStarted(),
      binding: binding
    )

    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
        active: intent,
        binding: alternate
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .recoveryRequired)
    }

    let complete = try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
      active: intent,
      binding: binding
    )
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.requireCompletedRedemptionGlobalSetup(
        active: complete,
        expectedSlotDigest: fingerprint,
        observedPublicKeyX963SHA256: fingerprint
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .recoveryRequired)
    }
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
      hostReference: "host:m36-ultra",
      signingKeyReference: "key:m36-ultra",
      keyStorage: .keychainP256
    )
  }

  private func identity() throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: identifier,
      intent: intent(),
      publicKeyX963SHA256: fingerprint,
      createdAtText: observedAt
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

  private func setupBinding(initialDigest: String) throws
    -> InitialEnrollmentRedemptionGlobalSetupBinding
  {
    try InitialEnrollmentRedemptionGlobalSetupBinding(
      slotDigest: otherFingerprint,
      initialGlobalDigest: initialDigest,
      initialGeneration: 1,
      initialObservedAtText: observedAt,
      activeKeyX963SHA256: fingerprint
    )
  }

  private func complete() throws -> InitialEnrollmentActiveV2 {
    let binding = try setupBinding(initialDigest: String(repeating: "c", count: 64))
    return try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
      active: InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
        active: try notStarted(),
        binding: binding
      ),
      binding: binding
    )
  }
}

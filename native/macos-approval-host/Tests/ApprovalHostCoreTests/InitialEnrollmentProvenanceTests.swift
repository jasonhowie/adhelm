import Foundation
import XCTest

@testable import ApprovalHostCore

final class InitialEnrollmentProvenanceTests: XCTestCase {
  private let identifier = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  private let fingerprint = String(repeating: "a", count: 64)

  func testStateSpecificCanonicalFormsRoundTripWithoutImplicitPromotion() throws {
    let pending = try InitialEnrollmentPending(enrollmentID: identifier, intent: intent())
    let pendingBytes = try InitialEnrollmentProvenance.pending(pending).canonicalBytes()
    XCTAssertTrue(pendingBytes.contains(Data(#""state":"PENDING""#.utf8)))
    XCTAssertFalse(pendingBytes.contains(Data(#""created_at""#.utf8)))
    XCTAssertFalse(pendingBytes.contains(Data(#""public_key_x963_sha256""#.utf8)))
    XCTAssertEqual(try InitialEnrollmentProvenance.decode(pendingBytes), .pending(pending))

    let active = try InitialEnrollmentTransitions.activate(
      pending: pending,
      observedPublicKeyX963SHA256: fingerprint,
      createdAtText: "2030-01-01T00:00:00.123456Z"
    )
    let activeBytes = try InitialEnrollmentProvenance.active(active).canonicalBytes()
    XCTAssertEqual(try InitialEnrollmentProvenance.decode(activeBytes), .active(active))

    let revoked = try InitialEnrollmentTransitions.revoke(
      active: active,
      revokedAtText: "2030-01-01T00:00:01.123456Z"
    )
    XCTAssertEqual(
      try InitialEnrollmentProvenance.decode(
        try InitialEnrollmentProvenance.revoked(revoked).canonicalBytes()
      ),
      .revoked(revoked)
    )
  }

  func testDecoderRejectsStateInapplicableFieldsAndLiteralTypeBypasses() throws {
    let pending = try InitialEnrollmentPending(enrollmentID: identifier, intent: intent())
    let pendingValue = try canonicalValue(.pending(pending))
    guard case .object(var fields) = pendingValue else { return XCTFail("expected object") }
    fields["public_key_x963_sha256"] = .string(fingerprint)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(fields))))

    let active = try InitialEnrollmentTransitions.activate(
      pending: pending,
      observedPublicKeyX963SHA256: fingerprint,
      createdAtText: "2030-01-01T00:00:00Z"
    )
    guard case .object(var activeFields) = try canonicalValue(.active(active)) else {
      return XCTFail("expected active object")
    }
    activeFields.removeValue(forKey: "created_at")
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(activeFields))))

    guard case .object(var versionFields) = pendingValue else { return XCTFail("expected object") }
    versionFields["schema_version"] = .bool(true)
    XCTAssertThrowsError(
      try InitialEnrollmentProvenance.decode(try CanonicalJSON.encode(.object(versionFields))))
  }

  func testRejectsAlternateBase64PadBitsAndInvalidOrOutOfOrderTimestamps() throws {
    let alternatePadBits = String(repeating: "A", count: 42) + "B"
    XCTAssertThrowsError(
      try InitialEnrollmentPending(enrollmentID: alternatePadBits, intent: intent()))

    XCTAssertThrowsError(
      try InitialEnrollmentActive(
        enrollmentID: identifier,
        intent: intent(),
        publicKeyX963SHA256: fingerprint,
        createdAtText: "2030-02-30T00:00:00Z"
      ))
    XCTAssertThrowsError(
      try InitialEnrollmentActive(
        enrollmentID: identifier,
        intent: intent(),
        publicKeyX963SHA256: fingerprint,
        createdAtText: "2030-01-01T00:00:00.1Z"
      ))
    XCTAssertThrowsError(
      try InitialEnrollmentActive(
        enrollmentID: identifier,
        intent: intent(),
        publicKeyX963SHA256: fingerprint,
        createdAtText: "2030-01-01T00:00:00Z\n"
      ))
    XCTAssertThrowsError(
      try InitialEnrollmentRevoked(
        enrollmentID: identifier,
        intent: intent(),
        publicKeyX963SHA256: fingerprint,
        createdAtText: "2030-01-01T00:00:01Z",
        revokedAtText: "2030-01-01T00:00:00Z"
      ))
  }

  func testActiveGateRejectsPendingRevokedConfigAndFingerprintMismatch() throws {
    let pending = try InitialEnrollmentPending(enrollmentID: identifier, intent: intent())
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.requireUsableActive(
        .pending(pending),
        expectedIntent: intent(),
        observedPublicKeyX963SHA256: fingerprint
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .recoveryRequired)
    }
    let active = try InitialEnrollmentTransitions.activate(
      pending: pending,
      observedPublicKeyX963SHA256: fingerprint,
      createdAtText: "2030-01-01T00:00:00Z"
    )
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.requireUsableActive(
        .active(active),
        expectedIntent: intent(),
        observedPublicKeyX963SHA256: String(repeating: "b", count: 64)
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .replacementUnsupported)
    }
    let revoked = try InitialEnrollmentTransitions.revoke(
      active: active,
      revokedAtText: "2030-01-01T00:00:01Z"
    )
    XCTAssertThrowsError(
      try InitialEnrollmentTransitions.requireUsableActive(
        .revoked(revoked),
        expectedIntent: intent(),
        observedPublicKeyX963SHA256: fingerprint
      )
    ) { error in
      XCTAssertEqual(error as? InitialEnrollmentProvenanceError, .revoked)
    }
  }

  private func intent() throws -> InitialEnrollmentIntent {
    try InitialEnrollmentIntent(
      hostReference: "host:fixture",
      signingKeyReference: "key:fixture",
      keyStorage: .keychainP256
    )
  }

  private func canonicalValue(_ provenance: InitialEnrollmentProvenance) throws
    -> CanonicalJSONValue
  {
    try CanonicalJSON.parseCanonical(try provenance.canonicalBytes())
  }
}

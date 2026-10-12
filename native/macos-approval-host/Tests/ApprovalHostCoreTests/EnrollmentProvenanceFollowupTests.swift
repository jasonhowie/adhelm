import XCTest
@testable import ApprovalHostCore

final class EnrollmentProvenanceFollowupTests: XCTestCase {
  private let identifier = String(repeating: "A", count: 43)
  private let fingerprint = String(repeating: "a", count: 64)

  private func intent() throws -> InitialEnrollmentIntent {
    try InitialEnrollmentIntent(
      hostReference: "host:synthetic", signingKeyReference: "key:synthetic", keyStorage: .keychainP256
    )
  }

  func testFingerprintRequiresExactlyLowercaseASCIIWithoutTerminator() throws {
    for suffix in ["\n", "\r\n", "\u{2028}", "\u{2029}"] {
      XCTAssertThrowsError(try InitialEnrollmentActive(
        enrollmentID: identifier, intent: intent(), publicKeyX963SHA256: fingerprint + suffix,
        createdAtText: "2030-01-01T00:00:00Z"
      ))
    }
  }

  func testMicrosecondRevocationOrderDoesNotDependOnFoundationDatePrecision() throws {
    XCTAssertThrowsError(try InitialEnrollmentRevoked(
      enrollmentID: identifier, intent: intent(), publicKeyX963SHA256: fingerprint,
      createdAtText: "9999-01-01T00:00:00.000001Z",
      revokedAtText: "9999-01-01T00:00:00.000000Z"
    ))
    XCTAssertNoThrow(try InitialEnrollmentRevoked(
      enrollmentID: identifier, intent: intent(), publicKeyX963SHA256: fingerprint,
      createdAtText: "9999-01-01T00:00:00Z",
      revokedAtText: "9999-01-01T00:00:00.000001Z"
    ))
  }

  func testValidFractionCannotBeRejectedByRoundingIntoNextSecond() throws {
    XCTAssertNoThrow(try InitialEnrollmentActive(
      enrollmentID: identifier, intent: intent(), publicKeyX963SHA256: fingerprint,
      createdAtText: "9999-12-31T23:59:59.999999Z"
    ))
  }
}

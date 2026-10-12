import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Independent fake-only regression coverage for the fixed-slot enrollment
/// boundary. These tests do not instantiate LocalAuthentication, AppKit,
/// Security, Keychain, XPC, or a real signing key.
final class InitialEnrollmentIndependentReviewTests: XCTestCase {
  private let identifier = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  private let fingerprint = String(repeating: "a", count: 64)
  private let createdAt = "2030-01-01T00:00:00.123456Z"

  func testCancellationAfterIdentifierGenerationCannotPersistPending() throws {
    let cancellation = ReviewCancellationState()
    let provenance = ReviewProvenanceStore()
    let keys = ReviewKeyOperations(fingerprint: fingerprint)
    let coordinator = try makeCoordinator(
      provenance: provenance,
      keys: keys,
      identifierGenerator: ReviewIdentifierGenerator(identifier: identifier) {
        cancellation.cancel()
      }
    )

    XCTAssertThrowsError(
      try coordinator.enroll(
        session: ReviewEnrollmentSession(),
        shouldContinue: {
          !cancellation.isCancelled
        })
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertNil(provenance.record)
    XCTAssertEqual(provenance.addCalls, 0)
    XCTAssertEqual(keys.createCalls, 0)
  }

  func testCancellationAfterRevocationTimestampCannotPersistRevokedRecord() throws {
    let cancellation = ReviewCancellationState()
    let provenance = ReviewProvenanceStore()
    provenance.record = .active(try activeRecord())
    let keys = ReviewKeyOperations(fingerprint: fingerprint)
    let coordinator = try makeCoordinator(
      provenance: provenance,
      keys: keys,
      timestampGenerator: {
        cancellation.cancel()
        return "2030-01-01T00:00:01.123456Z"
      }
    )

    XCTAssertThrowsError(
      try coordinator.revoke(
        session: ReviewEnrollmentSession(),
        shouldContinue: {
          !cancellation.isCancelled
        })
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    guard case .active? = provenance.record else {
      return XCTFail("cancelled revocation must retain the exact prior ACTIVE record")
    }
    XCTAssertEqual(provenance.replaceCalls, 0)
    XCTAssertEqual(keys.createCalls, 0)
  }

  func testProvenanceRejectsTrailingLineTerminatorsInCanonicalTimestamp() throws {
    for timestamp in [
      "2030-01-01T00:00:00Z\n",
      "2030-01-01T00:00:00.123456Z\r\n",
    ] {
      XCTAssertThrowsError(
        try InitialEnrollmentActive(
          enrollmentID: identifier,
          intent: try intent(),
          publicKeyX963SHA256: fingerprint,
          createdAtText: timestamp
        ),
        "trailing control must not be accepted as a canonical timestamp"
      )
    }
  }

  func testRegistryRejectsLogicalAliasesAndEitherOverlappingProtectedSelector() throws {
    let original = try configuration()
    let variants = [
      try configuration(host: "host:other"),
      try configuration(key: "key:other"),
      // Same provenance with a different key selector still overlaps.
      try configuration(tag: "different-key"),
      // Same key with a different provenance selector still overlaps.
      try configuration(service: "different-provenance"),
      // An omitted group can overlap the explicit group on either item.
      try configuration(group: "TEAM.review"),
    ]
    for variant in variants {
      let registry = InitialEnrollmentCoordinatorRegistry()
      let fake = try makeCoordinator(
        provenance: ReviewProvenanceStore(), keys: ReviewKeyOperations(fingerprint: fingerprint)
      )
      var builds = 0
      let make = {
        builds += 1
        return fake
      }
      XCTAssertNotNil(registry.coordinator(for: original, make: make))
      XCTAssertNil(registry.coordinator(for: variant, make: make))
      XCTAssertEqual(builds, 1)
    }
  }

  private func makeCoordinator(
    provenance: ReviewProvenanceStore,
    keys: ReviewKeyOperations,
    identifierGenerator: ReviewIdentifierGenerator = ReviewIdentifierGenerator(
      identifier: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    timestampGenerator: @escaping () -> String = { "2030-01-01T00:00:00.123456Z" }
  ) throws -> InitialEnrollmentCoordinator {
    InitialEnrollmentCoordinator(
      configuration: try configuration(),
      codeIdentityValidator: ReviewCodeIdentityValidator(),
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: identifierGenerator,
      timestampGenerator: timestampGenerator
    )
  }

  private func configuration(
    host: String = "host:independent-review",
    key: String = "key:independent-review",
    tag: String = "independent-review",
    service: String = "com.aptsonic.adhelm.independent-review.enrollment",
    group: String? = nil
  ) throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: host,
      signingKeyReference: key,
      machServiceName: "com.aptsonic.adhelm.independent-review",
      expectedClientCodeSigningRequirement: "identifier \"com.example.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.example.host\"",
      signingKeyApplicationTag: Data(tag.utf8),
      signingKeyAccessGroup: group,
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: service,
        provenanceAccount: "fixed-slot"
      )
    )
  }

  private func intent() throws -> InitialEnrollmentIntent {
    try InitialEnrollmentIntent(
      hostReference: "host:independent-review",
      signingKeyReference: "key:independent-review",
      keyStorage: .keychainP256
    )
  }

  private func activeRecord() throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: identifier,
      intent: try intent(),
      publicKeyX963SHA256: fingerprint,
      createdAtText: createdAt
    )
  }
}

private final class ReviewCancellationState: @unchecked Sendable {
  private let lock = NSLock()
  private var cancelled = false

  var isCancelled: Bool {
    lock.lock()
    defer { lock.unlock() }
    return cancelled
  }

  func cancel() {
    lock.lock()
    cancelled = true
    lock.unlock()
  }
}

private final class ReviewEnrollmentSession: EnrollmentProtectedSession, @unchecked Sendable {}

private final class ReviewCodeIdentityValidator: HostCodeIdentityValidator, @unchecked Sendable {
  func validateCurrentHost(requirement _: String) throws {}
}

private final class ReviewProvenanceStore: EnrollmentProvenanceStore, @unchecked Sendable {
  var record: InitialEnrollmentProvenance?
  private(set) var addCalls = 0
  private(set) var replaceCalls = 0

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    record
  }

  func addPending(
    _ pending: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    addCalls += 1
    record = pending
  }

  func replace(
    _ provenance: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    replaceCalls += 1
    record = provenance
  }
}

private final class ReviewKeyOperations: FixedSlotKeyOperations, @unchecked Sendable {
  let fingerprint: String
  private(set) var createCalls = 0

  init(fingerprint: String) { self.fingerprint = fingerprint }

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    nil
  }

  func createAndObservePublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    createCalls += 1
    return fingerprint
  }

  func observeExistingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    fingerprint
  }

  func signExistingKey(
    session _: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint _: String,
    payload _: Data
  ) throws -> Data {
    Data()
  }
}

private final class ReviewIdentifierGenerator: EnrollmentIdentifierGenerator, @unchecked Sendable {
  let identifier: String
  private let onNext: () -> Void

  init(identifier: String, onNext: @escaping () -> Void = {}) {
    self.identifier = identifier
    self.onNext = onNext
  }

  func nextEnrollmentIdentifier() throws -> String {
    onNext()
    return identifier
  }
}

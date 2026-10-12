import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// All adapters below are in-memory fakes.  These tests do not create an
/// LAContext, access Keychain, display AppKit, connect XPC, or sign with a key.
final class InitialEnrollmentCoordinatorTests: XCTestCase {
  private let identifier = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  private let fingerprint = String(repeating: "a", count: 64)
  private let nowText = "2030-01-01T00:00:00.123456Z"

  func testInitialEnrollmentPersistsPendingThenActiveWithOneSessionAndFixedFacts() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)
    let session = FakeEnrollmentSession()

    let active = try coordinator.enroll(session: session)

    XCTAssertEqual(active.enrollmentID, identifier)
    XCTAssertEqual(active.publicKeyX963SHA256, fingerprint)
    XCTAssertEqual(active.createdAtText, nowText)
    XCTAssertEqual(
      events.values,
      [
        "code", "provenance.load", "key.existing", "provenance.add_pending", "key.create",
        "provenance.replace",
      ]
    )
    XCTAssertEqual(provenance.sessions.count, 3)
    XCTAssertTrue(provenance.sessions.allSatisfy { $0 === session })
    XCTAssertEqual(keys.sessions.count, 2)
    XCTAssertTrue(keys.sessions.allSatisfy { $0 === session })
    guard case .activeV2(let record)? = provenance.record else {
      return XCTFail("expected durable schema-v2 ACTIVE record")
    }
    XCTAssertEqual(record.identity.intent, try intent())
    XCTAssertEqual(record.redemptionGlobalSetup, .notStarted)
  }

  func testCreationFailureLeavesPendingAndNeverRetriesDeletesOrPromotes() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    keys.createFailure = true
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)

    XCTAssertThrowsError(try coordinator.enroll(session: FakeEnrollmentSession())) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .recoveryRequired)
    }
    guard case .pending? = provenance.record else {
      return XCTFail("creation failure must retain PENDING")
    }
    XCTAssertEqual(keys.createCalls, 1)
    XCTAssertEqual(provenance.replaceCalls, 0)
    XCTAssertFalse(events.values.contains("key.delete"))
  }

  func testActiveWriteFailureLeavesPendingAndNeverAdoptsCreatedKey() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    provenance.replaceFailure = true
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)

    XCTAssertThrowsError(try coordinator.enroll(session: FakeEnrollmentSession())) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .recoveryRequired)
    }
    guard case .pending? = provenance.record else {
      return XCTFail("failed ACTIVE update must retain PENDING")
    }
    XCTAssertEqual(keys.createCalls, 1)
    XCTAssertEqual(provenance.replaceCalls, 1)
    XCTAssertEqual(keys.signCalls, 0)
  }

  func testCancellationAfterPendingLeavesRecoveryStateWithoutCreatingKey() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)
    var shouldContinue = true
    provenance.onAdd = { shouldContinue = false }

    XCTAssertThrowsError(
      try coordinator.enroll(
        session: FakeEnrollmentSession(),
        shouldContinue: { shouldContinue }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .recoveryRequired)
    }
    guard case .pending? = provenance.record else {
      return XCTFail("cancellation after PENDING must not promote or delete it")
    }
    XCTAssertEqual(keys.createCalls, 0)
    XCTAssertEqual(provenance.replaceCalls, 0)
  }

  func testCancellationDuringIdentifierGenerationNeverPersistsPending() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let identifierGenerator = FakeIdentifierGenerator(identifier: identifier)
    var shouldContinue = true
    identifierGenerator.onNext = { shouldContinue = false }
    let coordinator = makeCoordinator(
      events: events,
      provenance: provenance,
      keys: keys,
      identifierGenerator: identifierGenerator
    )

    XCTAssertThrowsError(
      try coordinator.enroll(
        session: FakeEnrollmentSession(),
        shouldContinue: { shouldContinue }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertNil(provenance.record)
    XCTAssertEqual(provenance.addCalls, 0)
    XCTAssertEqual(keys.createCalls, 0)
  }

  func testExistingUnprovenKeyAndAllPriorStatesRejectBeforeCreation() throws {
    let cases: [(String, InitialEnrollmentProvenance?, String?, ProtectedKeyStoreError)] = [
      ("orphan", nil, fingerprint, .recoveryRequired),
      ("pending", .pending(try pending()), nil, .recoveryRequired),
      ("active", .active(try active()), nil, .alreadyEnrolled),
      ("revoked", .revoked(try revoked()), nil, .revoked),
    ]
    for (_, record, existingFingerprint, expectedError) in cases {
      let events = EventLog()
      let provenance = FakeProvenanceStore(events: events)
      provenance.record = record
      let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
      keys.existingFingerprint = existingFingerprint
      let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)

      XCTAssertThrowsError(try coordinator.enroll(session: FakeEnrollmentSession())) { error in
        XCTAssertEqual(error as? ProtectedKeyStoreError, expectedError)
      }
      XCTAssertEqual(keys.createCalls, 0)
      XCTAssertEqual(provenance.addCalls, 0)
    }
  }

  func testCodeIdentityFailureOccursBeforeProtectedStoreOrKeyOperation() throws {
    let events = EventLog()
    let code = FakeCodeIdentityValidator(events: events)
    code.shouldFail = true
    let provenance = FakeProvenanceStore(events: events)
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = makeCoordinator(
      events: events,
      provenance: provenance,
      keys: keys,
      codeIdentity: code
    )

    XCTAssertThrowsError(try coordinator.enroll(session: FakeEnrollmentSession())) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .codeIdentityInvalid)
    }
    XCTAssertEqual(events.values, ["code"])
  }

  func testSigningClaimsPermitBeforeFirstProtectedReadAndRechecksActivePin() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    provenance.record = .active(try active())
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    keys.observedFingerprint = fingerprint
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)
    var claims = 0

    let signature = try coordinator.sign(
      payload: try payload(),
      session: FakeEnrollmentSession(),
      claimPermit: {
        events.append("claim")
        claims += 1
        return true
      }
    )

    XCTAssertEqual(signature, Data("fake-signature".utf8))
    XCTAssertEqual(claims, 1)
    XCTAssertEqual(
      events.values,
      ["code", "claim", "provenance.load", "key.observe", "key.sign"]
    )
    XCTAssertEqual(keys.lastExpectedFingerprint, fingerprint)
  }

  func testSigningPinMismatchConsumesPermitAndDoesNotSign() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    provenance.record = .active(try active())
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    keys.observedFingerprint = String(repeating: "b", count: 64)
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)
    var claims = 0

    XCTAssertThrowsError(
      try coordinator.sign(
        payload: try payload(),
        session: FakeEnrollmentSession(),
        claimPermit: {
          events.append("claim")
          claims += 1
          return true
        }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .replacementUnsupported)
    }
    XCTAssertEqual(claims, 1)
    XCTAssertEqual(keys.signCalls, 0)
    XCTAssertEqual(events.values, ["code", "claim", "provenance.load", "key.observe"])
  }

  func testRevocationWritesRecordOnlyThenMakesSigningFailClosed() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    provenance.record = .active(try active())
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)
    let session = FakeEnrollmentSession()

    let revoked = try coordinator.revoke(session: session)
    XCTAssertEqual(revoked.publicKeyX963SHA256, fingerprint)
    guard case .revoked? = provenance.record else { return XCTFail("expected REVOKED record") }
    XCTAssertEqual(keys.sessions.count, 0, "revocation must not delete or touch the key")

    var claims = 0
    XCTAssertThrowsError(
      try coordinator.sign(
        payload: try payload(),
        session: session,
        claimPermit: {
          claims += 1
          return true
        }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .revoked)
    }
    XCTAssertEqual(claims, 1, "the permit is claimed before the protected record read")
    XCTAssertEqual(keys.signCalls, 0)
    XCTAssertEqual(keys.observedCalls, 0)
  }

  func testAmbiguousRevocationWriteDoesNotDeleteOrUseKey() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    provenance.record = .active(try active())
    provenance.replaceFailure = true
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = makeCoordinator(events: events, provenance: provenance, keys: keys)

    XCTAssertThrowsError(try coordinator.revoke(session: FakeEnrollmentSession())) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    guard case .active? = provenance.record else {
      return XCTFail("an uncertain revoke update must not be normalized")
    }
    XCTAssertEqual(keys.sessions.count, 0)
    XCTAssertEqual(keys.signCalls, 0)
  }

  func testCancellationDuringRevocationTimestampNeverPersistsRevoked() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    provenance.record = .active(try active())
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    var shouldContinue = true
    let coordinator = makeCoordinator(
      events: events,
      provenance: provenance,
      keys: keys,
      timestampGenerator: {
        shouldContinue = false
        return self.nowText
      }
    )

    XCTAssertThrowsError(
      try coordinator.revoke(
        session: FakeEnrollmentSession(),
        shouldContinue: { shouldContinue }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    guard case .active? = provenance.record else {
      return XCTFail("cancellation before the REVOKED write must retain ACTIVE")
    }
    XCTAssertEqual(provenance.replaceCalls, 0)
  }

  func testUnconfiguredHostDoesNotClaimPermitOrAccessProtectedFakes() throws {
    let events = EventLog()
    let provenance = FakeProvenanceStore(events: events)
    let keys = FakeKeyOperations(events: events, createdFingerprint: fingerprint)
    let coordinator = InitialEnrollmentCoordinator(
      configuration: try configuration(initialEnrollment: nil),
      codeIdentityValidator: FakeCodeIdentityValidator(events: events),
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: FakeIdentifierGenerator(identifier: identifier),
      timestampGenerator: { self.nowText }
    )
    var claims = 0

    XCTAssertThrowsError(
      try coordinator.sign(
        payload: try payload(),
        session: FakeEnrollmentSession(),
        claimPermit: {
          claims += 1
          return true
        }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertEqual(claims, 0)
    XCTAssertEqual(events.values, ["code"])
  }

  func testCoordinatorRegistrySharesOnlyEqualCompleteConfigurations() throws {
    let events = EventLog()
    let registry = InitialEnrollmentCoordinatorRegistry()
    let configuration = try configuration(
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "com.aptsonic.adhelm.enrollment",
        provenanceAccount: "initial-slot"
      ))
    var builds = 0
    func make() -> InitialEnrollmentCoordinator {
      builds += 1
      return makeCoordinator(
        events: events,
        provenance: FakeProvenanceStore(events: events),
        keys: FakeKeyOperations(events: events, createdFingerprint: fingerprint)
      )
    }

    let first = registry.coordinator(for: configuration, make: make)
    let second = registry.coordinator(for: configuration, make: make)
    XCTAssertTrue(first === second)
    XCTAssertEqual(builds, 1)

    let incompatible = try TrustedHostConfiguration(
      hostReference: "host:fixture",
      signingKeyReference: "key:fixture",
      machServiceName: "com.example.fixture",
      expectedClientCodeSigningRequirement: "identifier \"com.example.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.example.other-host\"",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "com.aptsonic.adhelm.enrollment",
        provenanceAccount: "initial-slot"
      )
    )
    let third = registry.coordinator(for: incompatible, make: make)
    XCTAssertNil(third)
    XCTAssertEqual(builds, 1)
  }

  private func makeCoordinator(
    events: EventLog,
    provenance: FakeProvenanceStore,
    keys: FakeKeyOperations,
    codeIdentity: FakeCodeIdentityValidator? = nil,
    identifierGenerator: FakeIdentifierGenerator? = nil,
    timestampGenerator: (() -> String)? = nil
  ) -> InitialEnrollmentCoordinator {
    InitialEnrollmentCoordinator(
      configuration: try! configuration(
        initialEnrollment: try! InitialEnrollmentConfiguration(
          provenanceService: "com.aptsonic.adhelm.enrollment",
          provenanceAccount: "initial-slot"
        )),
      codeIdentityValidator: codeIdentity ?? FakeCodeIdentityValidator(events: events),
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: identifierGenerator ?? FakeIdentifierGenerator(identifier: identifier),
      timestampGenerator: timestampGenerator ?? { self.nowText }
    )
  }

  private func configuration(
    initialEnrollment: InitialEnrollmentConfiguration?
  ) throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: "host:fixture",
      signingKeyReference: "key:fixture",
      machServiceName: "com.example.fixture",
      expectedClientCodeSigningRequirement: "identifier \"com.example.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.example.host\"",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: initialEnrollment
    )
  }

  private func intent() throws -> InitialEnrollmentIntent {
    try InitialEnrollmentIntent(
      hostReference: "host:fixture",
      signingKeyReference: "key:fixture",
      keyStorage: .keychainP256
    )
  }

  private func pending() throws -> InitialEnrollmentPending {
    try InitialEnrollmentPending(enrollmentID: identifier, intent: intent())
  }

  private func active() throws -> InitialEnrollmentActive {
    try InitialEnrollmentTransitions.activate(
      pending: pending(),
      observedPublicKeyX963SHA256: fingerprint,
      createdAtText: nowText
    )
  }

  private func revoked() throws -> InitialEnrollmentRevoked {
    try InitialEnrollmentTransitions.revoke(
      active: active(),
      revokedAtText: "2030-01-01T00:00:01.123456Z"
    )
  }

  private func payload() throws -> ReceiptSigningPayload {
    let now = Date(timeIntervalSince1970: 1_893_456_060)
    let challengeURL = Bundle.module.url(
      forResource: "openai-pause-challenge-v1",
      withExtension: "json",
      subdirectory: "Fixtures"
    )!
    let challenge = try ApprovalChallenge.decode(try Data(contentsOf: challengeURL), now: now)
    let review = try DeterministicApprovalReview.make(for: challenge)
    return try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: review,
      configuration: try configuration(initialEnrollment: nil),
      now: now,
      receiptIdentifier: "approval-receipt:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ).signingPayload
  }
}

private final class EventLog: @unchecked Sendable {
  private(set) var values: [String] = []
  func append(_ value: String) { values.append(value) }
}

private final class FakeEnrollmentSession: EnrollmentProtectedSession, @unchecked Sendable {}

private final class FakeCodeIdentityValidator: HostCodeIdentityValidator, @unchecked Sendable {
  let events: EventLog
  var shouldFail = false

  init(events: EventLog) { self.events = events }

  func validateCurrentHost(requirement _: String) throws {
    events.append("code")
    if shouldFail { throw ProtectedKeyStoreError.codeIdentityInvalid }
  }
}

private final class FakeProvenanceStore: EnrollmentProvenanceStore, @unchecked Sendable {
  let events: EventLog
  var record: InitialEnrollmentProvenance?
  private(set) var sessions: [any EnrollmentProtectedSession] = []
  private(set) var addCalls = 0
  private(set) var replaceCalls = 0
  var replaceFailure = false
  var onAdd: (() -> Void)?

  init(events: EventLog) { self.events = events }

  func load(session: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    events.append("provenance.load")
    sessions.append(session)
    return record
  }

  func addPending(_ pending: InitialEnrollmentProvenance, session: any EnrollmentProtectedSession)
    throws
  {
    events.append("provenance.add_pending")
    sessions.append(session)
    addCalls += 1
    record = pending
    onAdd?()
  }

  func replace(_ provenance: InitialEnrollmentProvenance, session: any EnrollmentProtectedSession)
    throws
  {
    events.append("provenance.replace")
    sessions.append(session)
    replaceCalls += 1
    if replaceFailure { throw ProtectedKeyStoreError.unavailable }
    record = provenance
  }
}

private final class FakeKeyOperations: FixedSlotKeyOperations, @unchecked Sendable {
  let events: EventLog
  let createdFingerprint: String
  var existingFingerprint: String?
  var observedFingerprint: String?
  var createFailure = false
  private(set) var createCalls = 0
  private(set) var observedCalls = 0
  private(set) var signCalls = 0
  private(set) var lastExpectedFingerprint: String?
  private(set) var sessions: [any EnrollmentProtectedSession] = []

  init(events: EventLog, createdFingerprint: String) {
    self.events = events
    self.createdFingerprint = createdFingerprint
  }

  func existingPublicKeyFingerprint(session: any EnrollmentProtectedSession) throws -> String? {
    events.append("key.existing")
    sessions.append(session)
    return existingFingerprint
  }

  func createAndObservePublicKeyFingerprint(session: any EnrollmentProtectedSession) throws
    -> String
  {
    events.append("key.create")
    sessions.append(session)
    createCalls += 1
    if createFailure { throw ProtectedKeyStoreError.unavailable }
    return createdFingerprint
  }

  func observeExistingPublicKeyFingerprint(session: any EnrollmentProtectedSession) throws -> String
  {
    events.append("key.observe")
    sessions.append(session)
    observedCalls += 1
    return observedFingerprint ?? createdFingerprint
  }

  func signExistingKey(
    session: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint: String,
    payload _: Data
  ) throws -> Data {
    events.append("key.sign")
    sessions.append(session)
    signCalls += 1
    lastExpectedFingerprint = expectedPublicKeyFingerprint
    return Data("fake-signature".utf8)
  }
}

private final class FakeIdentifierGenerator: EnrollmentIdentifierGenerator, @unchecked Sendable {
  let identifier: String
  var onNext: (() -> Void)?
  init(identifier: String) { self.identifier = identifier }
  func nextEnrollmentIdentifier() throws -> String {
    onNext?()
    return identifier
  }
}

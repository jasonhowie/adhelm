import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only M36 coverage. No test here constructs LocalAuthentication,
/// invokes Security/Keychain, starts an XPC listener, or runs a native helper.
final class ReceiptRedemptionGlobalInitializationTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_000.123456)

  func testExplicitSetupPersistsIntentGenesisAndCompleteWithExactReadbacks() throws {
    let fixture = try makeFixture()

    XCTAssertEqual(
      try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      ),
      .initialized
    )
    let completed = try fixture.completedActive()
    let binding = try XCTUnwrap(completedBinding(completed))
    let global = try XCTUnwrap(fixture.store.global)
    guard case .ready(let ready) = try ReceiptRedemptionGlobalStateV1.decode(global) else {
      return XCTFail("expected genesis READY state")
    }
    XCTAssertEqual(ready.generation, 1)
    XCTAssertEqual(ready.observedAtText, binding.initialObservedAtText)
    XCTAssertEqual(CanonicalJSON.sha256Hex(global), binding.initialGlobalDigest)
    XCTAssertEqual(fixture.store.initialAdds, 1)
    XCTAssertEqual(fixture.provenance.replaceCalls, 2)

    XCTAssertEqual(
      try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      ),
      .alreadyInitialized
    )
    XCTAssertEqual(fixture.store.initialAdds, 1)

    fixture.store.global = try ReceiptRedemptionGlobalStateV1.ready(
      ReceiptRedemptionGlobalReadyV1(generation: 3, observedAt: now.addingTimeInterval(1))
    ).canonicalBytes()
    XCTAssertEqual(
      try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      ),
      .alreadyInitialized
    )
    XCTAssertEqual(fixture.store.initialAdds, 1)
  }

  func testLegacyActiveNeverUpcastsOrTouchesFixedGlobalStore() throws {
    let fixture = try makeFixture()
    let identity = fixture.notStarted.identity
    fixture.provenance.record = .active(identity)

    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    XCTAssertEqual(fixture.store.globalLoads, 0)
    XCTAssertEqual(fixture.store.initialAdds, 0)
    XCTAssertEqual(fixture.provenance.replaceCalls, 0)
    XCTAssertEqual(fixture.keys.observeCalls, 0)
    XCTAssertEqual(fixture.provenance.record, .active(identity))
  }

  func testNotStartedWithExistingGlobalDoesNotAdoptOrOverwriteIt() throws {
    let fixture = try makeFixture()
    let existing = try ReceiptRedemptionGlobalStateV1.ready(
      ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: now)
    ).canonicalBytes()
    fixture.store.global = existing

    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    XCTAssertEqual(fixture.store.global, existing)
    XCTAssertEqual(fixture.store.initialAdds, 0)
    XCTAssertEqual(fixture.provenance.replaceCalls, 0)
  }

  func testIntentInterruptionAndPostAddAmbiguityBlockAnyRetry() throws {
    let fixture = try makeFixture()
    fixture.store.throwAfterInitialAdd = true

    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    guard case .activeV2(let afterFirst)? = fixture.provenance.record,
      case .intentPending = afterFirst.redemptionGlobalSetup
    else { return XCTFail("post-intent ambiguity must retain INTENT_PENDING") }
    XCTAssertEqual(fixture.store.initialAdds, 1)
    XCTAssertNotNil(fixture.store.global)

    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    XCTAssertEqual(fixture.store.initialAdds, 1)
    XCTAssertEqual(fixture.provenance.replaceCalls, 1)

    let lostReadbackFixture = try makeFixture()
    lostReadbackFixture.store.eraseAfterInitialAdd = true
    assertRecovery {
      _ = try lostReadbackFixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: lostReadbackFixture.store,
        session: lostReadbackFixture.session
      )
    }
    guard case .activeV2(let afterLostReadback)? = lostReadbackFixture.provenance.record,
      case .intentPending = afterLostReadback.redemptionGlobalSetup
    else { return XCTFail("lost add readback must retain INTENT_PENDING") }
    XCTAssertNil(lostReadbackFixture.store.global)
    assertRecovery {
      _ = try lostReadbackFixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: lostReadbackFixture.store,
        session: lostReadbackFixture.session
      )
    }
    XCTAssertEqual(lostReadbackFixture.store.initialAdds, 1)
  }

  func testCompleteWithMissingOrChangedGlobalIsRecoveryOnly() throws {
    let fixture = try makeFixture()
    _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
      store: fixture.store,
      session: fixture.session
    )
    fixture.store.global = nil
    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    XCTAssertEqual(fixture.store.initialAdds, 1)

    let changedFixture = try makeFixture()
    _ = try changedFixture.coordinator.initializeReceiptRedemptionGlobalState(
      store: changedFixture.store,
      session: changedFixture.session
    )
    changedFixture.store.global = try ReceiptRedemptionGlobalStateV1.ready(
      ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: now.addingTimeInterval(10))
    ).canonicalBytes()
    assertRecovery {
      _ = try changedFixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: changedFixture.store,
        session: changedFixture.session
      )
    }
    XCTAssertEqual(changedFixture.store.initialAdds, 1)
  }

  func testCurrentKeyMismatchStopsBeforeAnyV2MarkerOrGlobalMutation() throws {
    let fixture = try makeFixture()
    fixture.keys.observedFingerprint = String(repeating: "b", count: 64)

    XCTAssertThrowsError(
      try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .replacementUnsupported)
    }
    XCTAssertEqual(fixture.store.initialAdds, 0)
    XCTAssertEqual(fixture.provenance.replaceCalls, 0)
    guard case .activeV2(let active)? = fixture.provenance.record else {
      return XCTFail("key mismatch must preserve NOT_STARTED")
    }
    XCTAssertEqual(active.redemptionGlobalSetup, .notStarted)
  }

  func testSetupRejectsRedemptionLeaseReentryBeforeTheCoordinatorLock() throws {
    let fixture = try makeFixture()
    var attempted = false
    fixture.store.onInitialAdd = {
      attempted = true
      do {
        try fixture.coordinator.withReceiptRedemptionLease(session: fixture.session) { _, _ in () }
        XCTFail("nested redemption lease must fail before coordinator lock acquisition")
      } catch {
        XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
      }
    }

    XCTAssertEqual(
      try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      ),
      .initialized
    )
    XCTAssertTrue(attempted)
  }

  private func makeFixture() throws -> M36Fixture {
    let configuration = try TrustedHostConfiguration(
      hostReference: "host:m36-fixture",
      signingKeyReference: "key:m36-fixture",
      machServiceName: "com.example.m36.fixture",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("m36-fixture".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "com.example.m36.provenance",
        provenanceAccount: "approval-host"
      )
    )
    let intent = try InitialEnrollmentIntent(
      hostReference: configuration.hostReference,
      signingKeyReference: configuration.signingKeyReference,
      keyStorage: configuration.keyStorage
    )
    let fingerprint = String(repeating: "a", count: 64)
    let notStarted = try InitialEnrollmentActiveV2(
      enrollmentID: String(repeating: "A", count: 43),
      intent: intent,
      publicKeyX963SHA256: fingerprint,
      createdAtText: "2030-01-01T00:00:00.000000Z",
      redemptionGlobalSetup: .notStarted
    )
    let provenance = M36Provenance(record: .activeV2(notStarted))
    let keys = M36Keys(fingerprint: fingerprint)
    let coordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: M36CodeIdentity(),
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: M36Identifier(),
      timestampGenerator: { "2030-01-01T00:00:00.000000Z" },
      clock: { self.now }
    )
    return M36Fixture(
      coordinator: coordinator,
      notStarted: notStarted,
      provenance: provenance,
      keys: keys,
      store: M36GlobalStore(),
      session: M36Session()
    )
  }

  private func completedBinding(
    _ active: InitialEnrollmentActiveV2
  ) -> InitialEnrollmentRedemptionGlobalSetupBinding? {
    guard case .complete(let binding) = active.redemptionGlobalSetup else { return nil }
    return binding
  }

  private func assertRecovery(_ operation: () throws -> Void) {
    XCTAssertThrowsError(try operation()) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .recoveryRequired)
    }
  }
}

private struct M36Fixture {
  let coordinator: InitialEnrollmentCoordinator
  let notStarted: InitialEnrollmentActiveV2
  let provenance: M36Provenance
  let keys: M36Keys
  let store: M36GlobalStore
  let session: M36Session

  func completedActive() throws -> InitialEnrollmentActiveV2 {
    guard case .activeV2(let active)? = provenance.record else {
      throw ProtectedKeyStoreError.recoveryRequired
    }
    return active
  }
}

private final class M36Session: EnrollmentProtectedSession, @unchecked Sendable {}

private final class M36CodeIdentity: HostCodeIdentityValidator, @unchecked Sendable {
  func validateCurrentHost(requirement _: String) throws {}
}

private final class M36Identifier: EnrollmentIdentifierGenerator, @unchecked Sendable {
  func nextEnrollmentIdentifier() throws -> String {
    String(repeating: "A", count: 43)
  }
}

private final class M36Provenance: EnrollmentProvenanceStore, @unchecked Sendable {
  var record: InitialEnrollmentProvenance?
  private(set) var replaceCalls = 0

  init(record: InitialEnrollmentProvenance) {
    self.record = record
  }

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    record
  }

  func addPending(
    _ pending: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
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

private final class M36Keys: FixedSlotKeyOperations, @unchecked Sendable {
  var observedFingerprint: String
  private(set) var observeCalls = 0

  init(fingerprint: String) {
    observedFingerprint = fingerprint
  }

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    observedFingerprint
  }

  func createAndObservePublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    observedFingerprint
  }

  func observeExistingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    observeCalls += 1
    return observedFingerprint
  }

  func signExistingKey(
    session _: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint _: String,
    payload _: Data
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }
}

private enum M36StoreError: Error {
  case unavailable
}

private final class M36GlobalStore: ReceiptRedemptionFixedSlotStore, @unchecked Sendable {
  var global: Data?
  private(set) var globalLoads = 0
  private(set) var initialAdds = 0
  var throwAfterInitialAdd = false
  var eraseAfterInitialAdd = false
  var onInitialAdd: (() -> Void)?

  func loadGlobalState(session _: any EnrollmentProtectedSession) throws -> Data? {
    globalLoads += 1
    return global
  }

  func addInitialGlobalState(
    _ canonicalState: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    guard global == nil else { throw M36StoreError.unavailable }
    initialAdds += 1
    global = canonicalState
    onInitialAdd?()
    if eraseAfterInitialAdd {
      global = nil
    }
    if throwAfterInitialAdd {
      throwAfterInitialAdd = false
      throw M36StoreError.unavailable
    }
  }

  func loadNonceState(
    nonceDigest _: String,
    session _: any EnrollmentProtectedSession
  ) throws -> Data? {
    nil
  }

  func addNonceState(
    nonceDigest _: String,
    canonicalState _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    throw M36StoreError.unavailable
  }

  func replaceNonceState(
    nonceDigest _: String,
    expected _: Data,
    replacement _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    throw M36StoreError.unavailable
  }

  func replaceGlobalState(
    expected _: Data,
    replacement _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    throw M36StoreError.unavailable
  }
}

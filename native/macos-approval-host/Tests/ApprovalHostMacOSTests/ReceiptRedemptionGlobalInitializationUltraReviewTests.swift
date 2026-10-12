import ApprovalHostCore
import Foundation
import XCTest

@testable import ApprovalHostMacOS

/// Independent M36 checks through the concrete fixed-slot adapter with only
/// in-memory collaborators. They never construct LocalAuthentication,
/// Security/Keychain, an XPC connection, a helper, or a native host.
final class ReceiptRedemptionGlobalInitializationUltraReviewTests: XCTestCase {
  func testLegacyAndIntentMarkersCannotAcquireConcreteRedemptionLease() throws {
    let legacy = try Fixture()
    legacy.provenance.record = .active(legacy.notStarted.identity)
    let legacyAdapter = try legacy.adapter()

    assertAdapterUnavailable {
      try legacyAdapter.withRedemptionLease { _ in
        XCTFail("legacy ACTIVE must not receive a redemption lease")
      }
    }
    XCTAssertEqual(legacy.store.globalLoads, 0)
    XCTAssertEqual(legacy.keys.observedCalls, 0)

    let interrupted = try Fixture()
    let genesis = try ReceiptRedemptionGlobalInitialization.makeGenesis(
      configuration: interrupted.configuration,
      active: interrupted.notStarted,
      observedAt: interrupted.now
    )
    interrupted.provenance.record = .activeV2(
      try InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
        active: interrupted.notStarted,
        binding: genesis.binding
      )
    )
    let interruptedAdapter = try interrupted.adapter()

    assertAdapterUnavailable {
      try interruptedAdapter.withRedemptionLease { _ in
        XCTFail("INTENT_PENDING must not receive a redemption lease")
      }
    }
    XCTAssertEqual(interrupted.store.globalLoads, 0)
    XCTAssertEqual(interrupted.store.initialAdds, 0)
  }

  func testPostIntentFailureStaysDurableAndCannotStartAnotherSetupAttempt() throws {
    let fixture = try Fixture()
    fixture.store.throwAfterInitialAdd = true

    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    guard case .activeV2(let retained)? = fixture.provenance.record,
      case .intentPending = retained.redemptionGlobalSetup
    else { return XCTFail("post-intent failure must retain INTENT_PENDING") }
    XCTAssertEqual(fixture.store.initialAdds, 1)

    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    XCTAssertEqual(fixture.store.initialAdds, 1)
    assertAdapterUnavailable {
      try fixture.adapter().withRedemptionLease { _ in
        XCTFail("recovery marker must not acquire a redemption lease")
      }
    }
  }

  func testCompleteMarkerRejectsChangedGenesisAndNeverReinitializesMissingGlobal() throws {
    let fixture = try Fixture()
    XCTAssertEqual(
      try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      ),
      .initialized
    )
    XCTAssertEqual(fixture.store.initialAdds, 1)

    fixture.store.global = try ReceiptRedemptionGlobalStateV1.ready(
      ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: fixture.now.addingTimeInterval(1))
    ).canonicalBytes()
    let changedAdapter = try fixture.adapter()
    assertAdapterUnavailable {
      _ = try changedAdapter.withRedemptionLease { lease in
        try lease.loadGlobalRedemptionState()
      }
    }
    XCTAssertEqual(fixture.store.initialAdds, 1)

    fixture.store.global = nil
    assertRecovery {
      _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
        store: fixture.store,
        session: fixture.session
      )
    }
    XCTAssertEqual(fixture.store.initialAdds, 1)
  }

  func testCurrentKeySwapBlocksLeaseBeforeGlobalRead() throws {
    let fixture = try Fixture()
    _ = try fixture.coordinator.initializeReceiptRedemptionGlobalState(
      store: fixture.store,
      session: fixture.session
    )
    fixture.keys.observedFingerprint = String(repeating: "b", count: 64)
    let globalLoadsBeforeLease = fixture.store.globalLoads
    let adapter = try fixture.adapter()

    assertAdapterUnavailable {
      try adapter.withRedemptionLease { _ in
        XCTFail("a swapped current key must not receive a redemption lease")
      }
    }
    XCTAssertEqual(fixture.store.globalLoads, globalLoadsBeforeLease)
  }

  private func assertRecovery(_ body: () throws -> Void) {
    XCTAssertThrowsError(try body()) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .recoveryRequired)
    }
  }

  private func assertAdapterUnavailable(_ body: () throws -> Void) {
    XCTAssertThrowsError(try body()) { error in
      XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
    }
  }
}

private final class M36UltraSession: EnrollmentProtectedSession, ReceiptRedemptionSession,
  @unchecked Sendable
{
  func invalidate() {}
}

private final class M36UltraCodeIdentity: HostCodeIdentityValidator, @unchecked Sendable {
  func validateCurrentHost(requirement _: String) throws {}
}

private final class M36UltraIdentifier: EnrollmentIdentifierGenerator, @unchecked Sendable {
  func nextEnrollmentIdentifier() throws -> String {
    String(repeating: "A", count: 43)
  }
}

private final class M36UltraProvenance: EnrollmentProvenanceStore, @unchecked Sendable {
  var record: InitialEnrollmentProvenance?

  init(_ record: InitialEnrollmentProvenance) {
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
    record = provenance
  }
}

private final class M36UltraKeys: FixedSlotKeyOperations, @unchecked Sendable {
  var observedFingerprint: String
  private(set) var observedCalls = 0

  init(fingerprint: String) {
    observedFingerprint = fingerprint
  }

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    nil
  }

  func createAndObservePublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    observedFingerprint
  }

  func observeExistingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    observedCalls += 1
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

private enum M36UltraStoreError: Error {
  case unavailable
}

private final class M36UltraStore: ReceiptRedemptionFixedSlotStore, @unchecked Sendable {
  var global: Data?
  private(set) var globalLoads = 0
  private(set) var initialAdds = 0
  var throwAfterInitialAdd = false

  func loadGlobalState(session _: any EnrollmentProtectedSession) throws -> Data? {
    globalLoads += 1
    return global
  }

  func addInitialGlobalState(
    _ canonicalState: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    guard global == nil else { throw M36UltraStoreError.unavailable }
    initialAdds += 1
    global = canonicalState
    if throwAfterInitialAdd {
      throwAfterInitialAdd = false
      throw M36UltraStoreError.unavailable
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
    throw M36UltraStoreError.unavailable
  }

  func replaceNonceState(
    nonceDigest _: String,
    expected _: Data,
    replacement _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    throw M36UltraStoreError.unavailable
  }

  func replaceGlobalState(
    expected _: Data,
    replacement _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    throw M36UltraStoreError.unavailable
  }
}

private final class Fixture {
  let now = Date(timeIntervalSince1970: 1_893_456_000.123456)
  let configuration: TrustedHostConfiguration
  let notStarted: InitialEnrollmentActiveV2
  let provenance: M36UltraProvenance
  let keys: M36UltraKeys
  let store = M36UltraStore()
  let session = M36UltraSession()
  let coordinator: InitialEnrollmentCoordinator

  init() throws {
    configuration = try TrustedHostConfiguration(
      hostReference: "host:m36-ultra",
      signingKeyReference: "key:m36-ultra",
      machServiceName: "com.example.m36.ultra",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("m36-ultra".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "com.example.m36.ultra.provenance",
        provenanceAccount: "approval-host"
      )
    )
    let fingerprint = String(repeating: "a", count: 64)
    let intent = try InitialEnrollmentIntent(
      hostReference: configuration.hostReference,
      signingKeyReference: configuration.signingKeyReference,
      keyStorage: configuration.keyStorage
    )
    notStarted = try InitialEnrollmentActiveV2(
      enrollmentID: String(repeating: "A", count: 43),
      intent: intent,
      publicKeyX963SHA256: fingerprint,
      createdAtText: "2030-01-01T00:00:00.000000Z",
      redemptionGlobalSetup: .notStarted
    )
    provenance = M36UltraProvenance(.activeV2(notStarted))
    keys = M36UltraKeys(fingerprint: fingerprint)
    coordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: M36UltraCodeIdentity(),
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: M36UltraIdentifier(),
      timestampGenerator: { "2030-01-01T00:00:00.000000Z" },
      clock: { Date(timeIntervalSince1970: 1_893_456_000.123456) }
    )
  }

  func adapter() throws -> ReceiptRedemptionFixedSlotAdapter {
    try ReceiptRedemptionFixedSlotAdapter(
      configuration: configuration,
      enrollmentCoordinator: coordinator,
      store: store,
      enrollmentSession: session,
      redemptionSession: session
    )
  }
}

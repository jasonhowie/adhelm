import ApprovalHostCore
import CryptoKit
@preconcurrency import Dispatch
import Foundation
import XCTest

@testable import ApprovalHostMacOS

/// Independent fake-only coverage for the concrete M33 adapter.  These cases
/// exercise the adapter through the real M31 coordinator, never Keychain,
/// Local Authentication, XPC, or an installed host.
final class ReceiptRedemptionFixedSlotAdapterUltraReviewTests: XCTestCase {
  func testEscapedConcreteLeaseCannotReadStateAfterCallbackReturns() throws {
    let fixture = try AdapterReviewFixture()
    var escaped: (any ReceiptRedemptionLease)?

    try fixture.adapter.withRedemptionLease { lease in
      escaped = lease
    }

    let lease = try XCTUnwrap(escaped)
    XCTAssertThrowsError(try lease.loadGlobalRedemptionState()) { error in
      XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
    }
    XCTAssertEqual(fixture.store.globalLoads, 0)
  }

  func testConcreteLeaseRejectsOtherThreadBeforeStoreRead() throws {
    let fixture = try AdapterReviewFixture()
    let result = AdapterReviewThreadResult()
    let finished = DispatchSemaphore(value: 0)

    try fixture.adapter.withRedemptionLease { lease in
      Thread.detachNewThread {
        defer { finished.signal() }
        do {
          _ = try lease.loadGlobalRedemptionState()
          result.error = nil
        } catch {
          result.error = error
        }
      }
      XCTAssertEqual(finished.wait(timeout: .now() + 1), .success)
    }

    XCTAssertEqual(result.error as? ReceiptRedemptionFixedSlotError, .unavailable)
    XCTAssertEqual(fixture.store.globalLoads, 0)
  }

  func testVerifierObservationAheadOfOuterClockStopsBeforeIntentWrite() throws {
    let initial = AdapterReviewFixture.initialTime
    let fixture = try AdapterReviewFixture(
      coordinatorTimes: [
        initial,
        initial.addingTimeInterval(1),
        initial.addingTimeInterval(5),
      ],
      verifierTimes: [
        initial.addingTimeInterval(2),
        initial.addingTimeInterval(3),
        initial.addingTimeInterval(4),
        initial.addingTimeInterval(5),
        initial.addingTimeInterval(10),
      ]
    )

    XCTAssertThrowsError(
      try fixture.redeemer.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: fixture.session
      )
    ) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .clockRollback)
    }

    XCTAssertEqual(fixture.store.globalReplaces, 0)
    XCTAssertEqual(fixture.store.nonceAdds, 0)
    XCTAssertFalse(try fixture.globalState().isIntentPending)
  }

  func testActualAdapterRetainsPostReservationAmbiguityFromCoordinator() throws {
    let initial = AdapterReviewFixture.initialTime
    let fixture = try AdapterReviewFixture(
      coordinatorTimes: [
        initial,
        initial.addingTimeInterval(1),
        initial.addingTimeInterval(7),
        initial.addingTimeInterval(8),
        initial.addingTimeInterval(9),
        initial.addingTimeInterval(15),
        initial.addingTimeInterval(16),
      ],
      verifierTimes: [
        initial.addingTimeInterval(2),
        initial.addingTimeInterval(3),
        initial.addingTimeInterval(4),
        initial.addingTimeInterval(5),
        initial.addingTimeInterval(6),
        initial.addingTimeInterval(10),
        initial.addingTimeInterval(11),
        initial.addingTimeInterval(12),
        initial.addingTimeInterval(13),
        initial.addingTimeInterval(14),
      ]
    )
    fixture.store.failPendingAdd = true

    XCTAssertThrowsError(
      try fixture.redeemer.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: fixture.session
      )
    ) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .postReservationUnverified)
    }

    XCTAssertEqual(fixture.store.globalReplaces, 1)
    XCTAssertEqual(fixture.store.nonceAdds, 1)
    XCTAssertNil(fixture.store.nonce)
    XCTAssertTrue(try fixture.globalState().isIntentPending)
    XCTAssertEqual(fixture.session.invalidations, 1)
  }
}

private final class AdapterReviewFixture {
  static let initialTime = Date(timeIntervalSince1970: 1_893_456_010)

  let configuration: TrustedHostConfiguration
  let challenge: ApprovalChallenge
  let receipt: SignedApprovalReceipt
  let session = AdapterReviewSession()
  let store: AdapterReviewStore
  let adapter: ReceiptRedemptionFixedSlotAdapter
  let redeemer: ReceiptRedemptionCoordinator

  init(
    coordinatorTimes: [Date]? = nil,
    verifierTimes: [Date]? = nil
  ) throws {
    configuration = try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:adapter-ultra-review",
      machServiceName: "com.aptsonic.adhelm.adapter-ultra-review",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("adapter-ultra-review".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "com.aptsonic.adhelm.adapter-ultra-review.provenance",
        provenanceAccount: "approval-host"
      )
    )
    let key = P256.Signing.PrivateKey()
    let initial = Self.initialTime
    let challengeURL = try XCTUnwrap(
      Bundle.module.url(
        forResource: "openai-pause-challenge-v1",
        withExtension: "json",
        subdirectory: "Fixtures"
      )
    )
    challenge = try ApprovalChallenge.decode(Data(contentsOf: challengeURL), now: initial)
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: initial,
      receiptIdentifier: "approval-receipt:" + String(repeating: "A", count: 43)
    )
    receipt = try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        .derRepresentation
    )

    let intent = try InitialEnrollmentIntent(
      hostReference: configuration.hostReference,
      signingKeyReference: configuration.signingKeyReference,
      keyStorage: configuration.keyStorage
    )
    let active = try InitialEnrollmentActive(
      enrollmentID: String(repeating: "A", count: 43),
      intent: intent,
      publicKeyX963SHA256: CanonicalJSON.sha256Hex(key.publicKey.x963Representation),
      createdAtText: "2030-01-01T00:00:00.000000Z"
    )
    let notStarted = try InitialEnrollmentActiveV2(
      enrollmentID: active.enrollmentID,
      intent: active.intent,
      publicKeyX963SHA256: active.publicKeyX963SHA256,
      createdAtText: active.createdAtText,
      redemptionGlobalSetup: .notStarted
    )
    let genesis = try ReceiptRedemptionGlobalInitialization.makeGenesis(
      configuration: configuration,
      active: notStarted,
      observedAt: initial
    )
    let provenance = AdapterReviewProvenance(
      record: .activeV2(
        try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
          active: InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
            active: notStarted,
            binding: genesis.binding
          ),
          binding: genesis.binding
        )
      )
    )
    let verifierClock = AdapterReviewClock(
      verifierTimes
        ?? [
          initial.addingTimeInterval(2),
          initial.addingTimeInterval(3),
          initial.addingTimeInterval(4),
          initial.addingTimeInterval(5),
          initial.addingTimeInterval(6),
          initial.addingTimeInterval(10),
          initial.addingTimeInterval(11),
          initial.addingTimeInterval(12),
          initial.addingTimeInterval(13),
          initial.addingTimeInterval(14),
        ]
    )
    let enrollmentCoordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: AdapterReviewCodeIdentity(),
      provenanceStore: provenance,
      keyOperations: AdapterReviewKeys(x963: key.publicKey.x963Representation),
      identifierGenerator: AdapterReviewIdentifier(),
      timestampGenerator: { "2030-01-01T00:00:00.000000Z" },
      clock: { verifierClock.next() }
    )
    store = try AdapterReviewStore(
      global: try ReceiptRedemptionGlobalStateV1.decode(genesis.canonicalBytes))
    adapter = try ReceiptRedemptionFixedSlotAdapter(
      configuration: configuration,
      enrollmentCoordinator: enrollmentCoordinator,
      store: store,
      enrollmentSession: session,
      redemptionSession: session
    )
    let redemptionClock = AdapterReviewClock(
      coordinatorTimes
        ?? [
          initial,
          initial.addingTimeInterval(1),
          initial.addingTimeInterval(7),
          initial.addingTimeInterval(8),
          initial.addingTimeInterval(9),
          initial.addingTimeInterval(15),
          initial.addingTimeInterval(16),
        ]
    )
    redeemer = ReceiptRedemptionCoordinator(
      configuration: configuration,
      fixedSlot: adapter,
      clock: { redemptionClock.next() }
    )
  }

  func globalState() throws -> ReceiptRedemptionGlobalStateV1 {
    try ReceiptRedemptionGlobalStateV1.decode(try XCTUnwrap(store.global))
  }
}

private final class AdapterReviewSession: EnrollmentProtectedSession, ReceiptRedemptionSession,
  @unchecked Sendable
{
  var invalidations = 0

  func invalidate() {
    invalidations += 1
  }
}

private final class AdapterReviewClock: @unchecked Sendable {
  private var values: [Date]

  init(_ values: [Date]) {
    self.values = values
  }

  func next() -> Date {
    guard !values.isEmpty else { return .distantFuture }
    return values.removeFirst()
  }
}

private final class AdapterReviewThreadResult: @unchecked Sendable {
  private let lock = NSLock()
  private var stored: Error?

  var error: Error? {
    get {
      lock.lock()
      defer { lock.unlock() }
      return stored
    }
    set {
      lock.lock()
      stored = newValue
      lock.unlock()
    }
  }
}

private final class AdapterReviewCodeIdentity: HostCodeIdentityValidator, @unchecked Sendable {
  func validateCurrentHost(requirement _: String) throws {}
}

private final class AdapterReviewProvenance: EnrollmentProvenanceStore, @unchecked Sendable {
  private var stored: InitialEnrollmentProvenance?

  init(record: InitialEnrollmentProvenance) {
    stored = record
  }

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    stored
  }

  func addPending(
    _ pending: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    stored = pending
  }

  func replace(
    _ provenance: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    stored = provenance
  }
}

private final class AdapterReviewKeys: CurrentReceiptVerificationKeyOperations, @unchecked Sendable
{
  private let x963: Data

  init(x963: Data) {
    self.x963 = x963
  }

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    nil
  }

  func createAndObservePublicKeyFingerprint(
    session _: any EnrollmentProtectedSession
  ) throws -> String {
    CanonicalJSON.sha256Hex(x963)
  }

  func observeExistingPublicKeyFingerprint(
    session _: any EnrollmentProtectedSession
  ) throws -> String {
    CanonicalJSON.sha256Hex(x963)
  }

  func observeExistingPublicKeyX963(session _: any EnrollmentProtectedSession) throws -> Data {
    x963
  }

  func signExistingKey(
    session _: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint _: String,
    payload _: Data
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class AdapterReviewIdentifier: EnrollmentIdentifierGenerator, @unchecked Sendable {
  func nextEnrollmentIdentifier() throws -> String {
    String(repeating: "A", count: 43)
  }
}

private enum AdapterReviewStoreError: Error {
  case failed
}

private final class AdapterReviewStore: ReceiptRedemptionFixedSlotStore, @unchecked Sendable {
  var global: Data?
  var nonce: Data?
  var globalLoads = 0
  var globalReplaces = 0
  var nonceAdds = 0
  var failPendingAdd = false

  init(global: ReceiptRedemptionGlobalStateV1) throws {
    self.global = try global.canonicalBytes()
  }

  func loadGlobalState(session _: any EnrollmentProtectedSession) throws -> Data? {
    globalLoads += 1
    return global
  }

  func loadNonceState(
    nonceDigest _: String,
    session _: any EnrollmentProtectedSession
  ) throws -> Data? {
    nonce
  }

  func addNonceState(
    nonceDigest _: String,
    canonicalState: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    nonceAdds += 1
    guard !failPendingAdd, nonce == nil else { throw AdapterReviewStoreError.failed }
    nonce = canonicalState
  }

  func replaceNonceState(
    nonceDigest _: String,
    expected: Data,
    replacement: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    guard nonce == expected else { throw AdapterReviewStoreError.failed }
    nonce = replacement
  }

  func replaceGlobalState(
    expected: Data,
    replacement: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    guard global == expected else { throw AdapterReviewStoreError.failed }
    global = replacement
    globalReplaces += 1
  }
}

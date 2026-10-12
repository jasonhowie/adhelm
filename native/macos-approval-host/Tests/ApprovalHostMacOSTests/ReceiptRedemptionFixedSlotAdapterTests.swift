import ApprovalHostCore
import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostMacOS

private func adapterBase64URL(_ value: Data) -> String {
  value.base64EncodedString()
    .replacingOccurrences(of: "+", with: "-")
    .replacingOccurrences(of: "/", with: "_")
    .replacingOccurrences(of: "=", with: "")
}

/// Fake-only integration coverage for the M33 adapter. These tests use
/// software P-256 keys and in-memory fixed records; they never instantiate
/// LocalAuthentication, call Security/Keychain, create XPC objects, or start
/// an installed host.
final class ReceiptRedemptionFixedSlotAdapterTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_030)

  func testActualAdapterCompletesOneBoundedRedemptionAndClearsIntent() throws {
    let fixture = try makeFixture()
    let adapter = try fixture.adapter(session: fixture.session)

    let permit = try fixture.redemptionCoordinator(adapter: adapter).redeem(
      receiptData: fixture.receipt.canonicalReceipt,
      expectedChallenge: fixture.challenge,
      session: fixture.session
    )

    guard
      let nonce = fixture.store.nonceState(for: fixture.challenge.replayKey),
      case .verified(let verified) = try ReceiptRedemptionStateV1.decode(nonce),
      let global = fixture.store.globalState,
      case .ready(let ready) = try ReceiptRedemptionGlobalStateV1.decode(global)
    else { return XCTFail("expected verified nonce and cleared fixed global intent") }
    XCTAssertEqual(ready.generation, 3)
    XCTAssertEqual(verified.pending.binding.challengeNonceDigest, fixture.challenge.replayKey)
    XCTAssertEqual(fixture.store.globalReplaceCount, 2)
    XCTAssertEqual(fixture.store.pendingAddCount, 1)
    XCTAssertEqual(fixture.store.pendingReplaceCount, 1)
    XCTAssertEqual(fixture.keyOperations.x963ReadCount, 2)
    XCTAssertTrue(permit.claimPendingProtectedExecution(binding: verified.pending.binding))
    XCTAssertFalse(permit.claimPendingProtectedExecution(binding: verified.pending.binding))
    XCTAssertTrue(fixture.session.invalidated)
  }

  func testPostIntentWriteFailurePropagatesAndRestartBlocksWithoutRetry() throws {
    let fixture = try makeFixture()
    fixture.store.throwAfterNextNonceAdd = true
    let adapter = try fixture.adapter(session: fixture.session)

    assertRedemptionError(.postReservationUnverified) {
      _ = try fixture.redemptionCoordinator(adapter: adapter).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: fixture.session
      )
    }

    guard
      let global = fixture.store.globalState,
      case .intentPending(let intent) = try ReceiptRedemptionGlobalStateV1.decode(global),
      let nonce = fixture.store.nonceState(for: fixture.challenge.replayKey),
      case .pending = try ReceiptRedemptionStateV1.decode(nonce)
    else { return XCTFail("post-write ambiguity must retain both exact reservations") }
    XCTAssertEqual(intent.binding.challengeNonceDigest, fixture.challenge.replayKey)
    let readsBeforeRestart = fixture.keyOperations.x963ReadCount

    let restartedSession = AdapterSession()
    let restartedAdapter = try fixture.adapter(session: restartedSession)
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try fixture.redemptionCoordinator(adapter: restartedAdapter).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: restartedSession
      )
    }
    XCTAssertEqual(fixture.keyOperations.x963ReadCount, readsBeforeRestart)
    XCTAssertEqual(fixture.store.pendingAddCount, 1)
    XCTAssertEqual(fixture.store.globalReplaceCount, 1)
  }

  func testVerifiedReplayAndInvalidCurrentSignatureRetainTheirExactOutcomes() throws {
    let replayFixture = try makeFixture()
    let firstAdapter = try replayFixture.adapter(session: replayFixture.session)
    _ = try replayFixture.redemptionCoordinator(adapter: firstAdapter).redeem(
      receiptData: replayFixture.receipt.canonicalReceipt,
      expectedChallenge: replayFixture.challenge,
      session: replayFixture.session
    )
    let readsBeforeReplay = replayFixture.keyOperations.x963ReadCount
    let replaySession = AdapterSession()
    let replayAdapter = try replayFixture.adapter(session: replaySession)
    assertRedemptionError(.replayed) {
      _ = try replayFixture.redemptionCoordinator(adapter: replayAdapter).redeem(
        receiptData: replayFixture.receipt.canonicalReceipt,
        expectedChallenge: replayFixture.challenge,
        session: replaySession
      )
    }
    XCTAssertEqual(replayFixture.keyOperations.x963ReadCount, readsBeforeReplay)

    let invalidFixture = try makeFixture()
    let initialGlobal = try XCTUnwrap(invalidFixture.store.globalState)
    let forged = try makeReceipt(
      challenge: invalidFixture.challenge,
      key: P256.Signing.PrivateKey(),
      configuration: invalidFixture.configuration,
      identifierByte: 0xD5
    )
    let invalidAdapter = try invalidFixture.adapter(session: invalidFixture.session)
    assertRedemptionError(.currentStateUnavailable) {
      _ = try invalidFixture.redemptionCoordinator(adapter: invalidAdapter).redeem(
        receiptData: forged.canonicalReceipt,
        expectedChallenge: invalidFixture.challenge,
        session: invalidFixture.session
      )
    }
    XCTAssertEqual(invalidFixture.store.globalState, initialGlobal)
    XCTAssertEqual(invalidFixture.store.pendingAddCount, 0)
    XCTAssertEqual(invalidFixture.store.globalReplaceCount, 0)
  }

  func testAdapterRequiresTheExactSessionAndInvalidatesCallbackCapabilities() throws {
    let fixture = try makeFixture()
    XCTAssertThrowsError(
      try ReceiptRedemptionFixedSlotAdapter(
        configuration: fixture.configuration,
        enrollmentCoordinator: fixture.enrollmentCoordinator,
        store: fixture.store,
        enrollmentSession: fixture.session,
        redemptionSession: AdapterSession()
      )
    ) { error in
      XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
    }

    let adapter = try fixture.adapter(session: fixture.session)
    var retained: (any ReceiptRedemptionLease)?
    try adapter.withRedemptionLease { lease in
      retained = lease
      XCTAssertThrowsError(try adapter.withRedemptionLease { _ in () }) { error in
        XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
      }
      _ = try lease.loadGlobalRedemptionState()
    }
    guard let retained else { return XCTFail("expected callback lease") }
    XCTAssertThrowsError(try retained.loadGlobalRedemptionState()) { error in
      XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
    }
  }

  func testSelectorsAreDeterministicAndBoundToReviewedConfiguration() throws {
    let fixture = try makeFixture()
    let first = try ReceiptRedemptionFixedSlotSelectorDerivation(
      configuration: fixture.configuration)
    let again = try ReceiptRedemptionFixedSlotSelectorDerivation(
      configuration: fixture.configuration)
    XCTAssertEqual(first, again)
    XCTAssertEqual(
      try first.nonceSelector(nonceDigest: fixture.challenge.replayKey),
      try again.nonceSelector(nonceDigest: fixture.challenge.replayKey)
    )

    let changed = try makeConfiguration(hostReference: "host:adhelm-macos-approval-v1-changed")
    let changedDerivation = try ReceiptRedemptionFixedSlotSelectorDerivation(configuration: changed)
    XCTAssertNotEqual(first.slotDigest, changedDerivation.slotDigest)
    XCTAssertNotEqual(
      try first.nonceSelector(nonceDigest: fixture.challenge.replayKey),
      try changedDerivation.nonceSelector(nonceDigest: fixture.challenge.replayKey)
    )
    XCTAssertThrowsError(try first.nonceSelector(nonceDigest: "not-a-digest")) { error in
      XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
    }
  }

  private func makeFixture() throws -> AdapterFixture {
    let configuration = try makeConfiguration()
    let challenge = try loadChallenge()
    let key = P256.Signing.PrivateKey()
    let receipt = try makeReceipt(challenge: challenge, key: key, configuration: configuration)
    let provenance = AdapterProvenanceStore()
    let setup = try completedSetup(for: key, configuration: configuration, observedAt: now)
    provenance.record = .activeV2(setup.active)
    let keyOperations = AdapterKeyOperations(x963: key.publicKey.x963Representation)
    let enrollmentCoordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: AdapterCodeIdentity(),
      provenanceStore: provenance,
      keyOperations: keyOperations,
      identifierGenerator: AdapterIdentifierGenerator(),
      timestampGenerator: { "2030-01-01T00:00:00.000000Z" },
      clock: { self.now }
    )
    let store = AdapterStore()
    store.globalState = setup.globalBytes
    return AdapterFixture(
      configuration: configuration,
      challenge: challenge,
      receipt: receipt,
      enrollmentCoordinator: enrollmentCoordinator,
      keyOperations: keyOperations,
      store: store,
      session: AdapterSession(),
      now: now
    )
  }

  private func makeConfiguration(
    hostReference: String = "host:adhelm-macos-approval-v1"
  ) throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: hostReference,
      signingKeyReference: "key:fixture",
      machServiceName: "invalid.fixture.service",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "fixture.provenance",
        provenanceAccount: "fixture.account"
      )
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

  private func makeReceipt(
    challenge: ApprovalChallenge,
    key: P256.Signing.PrivateKey,
    configuration: TrustedHostConfiguration,
    identifierByte: UInt8 = 0xD4
  ) throws -> SignedApprovalReceipt {
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:"
        + adapterBase64URL(Data(repeating: identifierByte, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        .derRepresentation
    )
  }

  private func active(
    for key: P256.Signing.PrivateKey,
    configuration: TrustedHostConfiguration
  ) throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      intent: try InitialEnrollmentIntent(
        hostReference: configuration.hostReference,
        signingKeyReference: configuration.signingKeyReference,
        keyStorage: configuration.keyStorage
      ),
      publicKeyX963SHA256: CanonicalJSON.sha256Hex(key.publicKey.x963Representation),
      createdAtText: "2030-01-01T00:00:00.000000Z"
    )
  }

  private func completedSetup(
    for key: P256.Signing.PrivateKey,
    configuration: TrustedHostConfiguration,
    observedAt: Date
  ) throws -> (active: InitialEnrollmentActiveV2, globalBytes: Data) {
    let identity = try active(for: key, configuration: configuration)
    let notStarted = try InitialEnrollmentActiveV2(
      enrollmentID: identity.enrollmentID,
      intent: identity.intent,
      publicKeyX963SHA256: identity.publicKeyX963SHA256,
      createdAtText: identity.createdAtText,
      redemptionGlobalSetup: .notStarted
    )
    let genesis = try ReceiptRedemptionGlobalInitialization.makeGenesis(
      configuration: configuration,
      active: notStarted,
      observedAt: observedAt
    )
    let intent = try InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
      active: notStarted,
      binding: genesis.binding
    )
    let complete = try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
      active: intent,
      binding: genesis.binding
    )
    return (complete, genesis.canonicalBytes)
  }

  private func assertRedemptionError(
    _ expected: ReceiptRedemptionError,
    _ operation: () throws -> Void
  ) {
    XCTAssertThrowsError(try operation()) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, expected)
    }
  }
}

private struct AdapterFixture {
  let configuration: TrustedHostConfiguration
  let challenge: ApprovalChallenge
  let receipt: SignedApprovalReceipt
  let enrollmentCoordinator: InitialEnrollmentCoordinator
  let keyOperations: AdapterKeyOperations
  let store: AdapterStore
  let session: AdapterSession
  let now: Date

  func adapter(session: AdapterSession) throws -> ReceiptRedemptionFixedSlotAdapter {
    try ReceiptRedemptionFixedSlotAdapter(
      configuration: configuration,
      enrollmentCoordinator: enrollmentCoordinator,
      store: store,
      enrollmentSession: session,
      redemptionSession: session
    )
  }

  func redemptionCoordinator(adapter: ReceiptRedemptionFixedSlotAdapter)
    -> ReceiptRedemptionCoordinator
  {
    ReceiptRedemptionCoordinator(configuration: configuration, fixedSlot: adapter, clock: { now })
  }
}

private enum AdapterStoreError: Error {
  case failure
}

private final class AdapterSession: EnrollmentProtectedSession, ReceiptRedemptionSession,
  @unchecked Sendable
{
  private let lock = NSLock()
  private(set) var invalidated = false

  func invalidate() {
    lock.lock()
    invalidated = true
    lock.unlock()
  }
}

private final class AdapterCodeIdentity: HostCodeIdentityValidator, @unchecked Sendable {
  func validateCurrentHost(requirement _: String) throws {}
}

private final class AdapterProvenanceStore: EnrollmentProvenanceStore, @unchecked Sendable {
  private let lock = NSLock()
  private var storage: InitialEnrollmentProvenance?

  var record: InitialEnrollmentProvenance? {
    get {
      lock.lock()
      defer { lock.unlock() }
      return storage
    }
    set {
      lock.lock()
      storage = newValue
      lock.unlock()
    }
  }

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    lock.lock()
    defer { lock.unlock() }
    return storage
  }

  func addPending(
    _ pending: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    lock.lock()
    storage = pending
    lock.unlock()
  }

  func replace(
    _ provenance: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    lock.lock()
    storage = provenance
    lock.unlock()
  }
}

private final class AdapterKeyOperations: CurrentReceiptVerificationKeyOperations,
  @unchecked Sendable
{
  private let lock = NSLock()
  private let x963: Data
  private(set) var x963ReadCount = 0

  init(x963: Data) { self.x963 = x963 }

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    nil
  }

  func createAndObservePublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    CanonicalJSON.sha256Hex(x963)
  }

  func observeExistingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    CanonicalJSON.sha256Hex(x963)
  }

  func observeExistingPublicKeyX963(session _: any EnrollmentProtectedSession) throws -> Data {
    lock.lock()
    x963ReadCount += 1
    lock.unlock()
    return x963
  }

  func signExistingKey(
    session _: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint _: String,
    payload _: Data
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class AdapterIdentifierGenerator: EnrollmentIdentifierGenerator, @unchecked Sendable {
  func nextEnrollmentIdentifier() throws -> String {
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }
}

private final class AdapterStore: ReceiptRedemptionFixedSlotStore, @unchecked Sendable {
  private let lock = NSLock()
  private var globalStorage: Data?
  private var nonceStorage: [String: Data] = [:]
  private var globalReplacements = 0
  private var nonceAdds = 0
  private var nonceReplacements = 0
  var throwAfterNextNonceAdd = false

  var globalState: Data? {
    get {
      lock.lock()
      defer { lock.unlock() }
      return globalStorage
    }
    set {
      lock.lock()
      globalStorage = newValue
      lock.unlock()
    }
  }

  var globalReplaceCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return globalReplacements
  }

  var pendingAddCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return nonceAdds
  }

  var pendingReplaceCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return nonceReplacements
  }

  func nonceState(for nonceDigest: String) -> Data? {
    lock.lock()
    defer { lock.unlock() }
    return nonceStorage[nonceDigest]
  }

  func loadGlobalState(session _: any EnrollmentProtectedSession) throws -> Data? {
    globalState
  }

  func loadNonceState(
    nonceDigest: String,
    session _: any EnrollmentProtectedSession
  ) throws -> Data? {
    nonceState(for: nonceDigest)
  }

  func addNonceState(
    nonceDigest: String,
    canonicalState: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    lock.lock()
    defer { lock.unlock() }
    guard nonceStorage[nonceDigest] == nil else { throw AdapterStoreError.failure }
    nonceStorage[nonceDigest] = canonicalState
    nonceAdds += 1
    if throwAfterNextNonceAdd {
      throwAfterNextNonceAdd = false
      throw AdapterStoreError.failure
    }
  }

  func replaceNonceState(
    nonceDigest: String,
    expected: Data,
    replacement: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    lock.lock()
    defer { lock.unlock() }
    guard nonceStorage[nonceDigest] == expected else { throw AdapterStoreError.failure }
    nonceStorage[nonceDigest] = replacement
    nonceReplacements += 1
  }

  func replaceGlobalState(
    expected: Data,
    replacement: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    lock.lock()
    defer { lock.unlock() }
    guard globalStorage == expected else { throw AdapterStoreError.failure }
    globalStorage = replacement
    globalReplacements += 1
  }
}

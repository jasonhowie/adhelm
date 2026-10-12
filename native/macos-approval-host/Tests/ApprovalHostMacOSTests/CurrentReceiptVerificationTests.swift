import CryptoKit
@preconcurrency import Dispatch
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only coverage for the internal fixed-slot current-receipt boundary.
/// These tests use ephemeral software P-256 keys and in-memory adapters. They
/// do not instantiate LocalAuthentication, access Keychain/Security, display
/// AppKit, connect XPC, or contact a provider.
final class CurrentReceiptVerificationTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_030)

  func testActiveCurrentKeyVerifiesActualEphemeralP256Receipt() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)

    let checked = try fixture.coordinator.verifyCurrentReceipt(
      receiptData: signed.canonicalReceipt,
      expectedChallenge: challenge,
      session: ReceiptVerificationSession()
    )

    XCTAssertEqual(checked, signed)
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testBoundedMalformedAndExpiredCandidatesFailBeforeProtectedReads() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    let malformedSignature = try replacingSignature(in: signed.canonicalReceipt, with: "!")

    for candidate in [
      Data("{\"schema_version\":1}".utf8),
      malformedSignature,
      Data(repeating: 0x7B, count: 16_385),
    ] {
      XCTAssertThrowsError(
        try fixture.coordinator.verifyCurrentReceipt(
          receiptData: candidate,
          expectedChallenge: challenge,
          session: ReceiptVerificationSession()
        )
      ) { error in
        XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
      }
      XCTAssertEqual(fixture.events.values, [])
    }
    fixture.clock.setSequence([challenge.expiresAt])
    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(fixture.events.values, [])
  }

  func testExpectedChallengeRebindingFailsBeforeIdentityOrProtectedReads() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    let rebound = try ApprovalChallenge.decode(
      replacingNonce(in: challenge.canonicalBytes),
      now: now
    )

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: rebound,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(fixture.events.values, [])
  }

  func testTrustedClockRejectsExpiryAndBackwardStepsAtProtectedHandoffs() throws {
    let key = P256.Signing.PrivateKey()
    let challenge = try loadChallenge()

    let expiryBeforeProvenance = try makeFixture(key: key)
    let signed = try makeReceipt(
      challenge: challenge,
      key: key,
      configuration: expiryBeforeProvenance.configuration
    )
    var continuationCalls = 0
    assertProtectedError(.unavailable) {
      _ = try expiryBeforeProvenance.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession(),
        shouldContinue: {
          continuationCalls += 1
          if continuationCalls == 3 {
            expiryBeforeProvenance.clock.setSequence([challenge.expiresAt])
          }
          return true
        }
      )
    }
    XCTAssertEqual(expiryBeforeProvenance.events.values, ["code"])

    let backwardBeforeProvenance = try makeFixture(key: key)
    backwardBeforeProvenance.clock.setSequence([
      now,
      now.addingTimeInterval(1),
      now,
    ])
    assertProtectedError(.unavailable) {
      _ = try backwardBeforeProvenance.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(backwardBeforeProvenance.events.values, ["code"])

    let expiryBeforeCrypto = try makeFixture(key: key)
    expiryBeforeCrypto.keys.afterX963Read = {
      expiryBeforeCrypto.clock.setSequence([challenge.expiresAt])
    }
    assertProtectedError(.unavailable) {
      _ = try expiryBeforeCrypto.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(expiryBeforeCrypto.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testPendingRevokedAndReplacementStatesFailClosed() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)

    fixture.provenance.record = .pending(try pending(for: fixture.configuration))
    assertProtectedError(.recoveryRequired) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load"])

    fixture.events.clear()
    fixture.provenance.record = .revoked(
      try revoked(for: key, configuration: fixture.configuration))
    assertProtectedError(.revoked) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load"])

    fixture.events.clear()
    fixture.provenance.record = .active(try active(for: key, configuration: fixture.configuration))
    fixture.keys.x963 = P256.Signing.PrivateKey().publicKey.x963Representation
    assertProtectedError(.replacementUnsupported) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testCodeIdentityAndSessionReadFailuresAreNormalizedAndFailClosed() throws {
    let key = P256.Signing.PrivateKey()
    let failedIdentity = try makeFixture(key: key, codeIdentityFails: true)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge,
      key: key,
      configuration: failedIdentity.configuration
    )
    assertProtectedError(.codeIdentityInvalid) {
      _ = try failedIdentity.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(failedIdentity.events.values, ["code"])

    let failedSessionRead = try makeFixture(key: key, x963ReadFails: true)
    assertProtectedError(.unavailable) {
      _ = try failedSessionRead.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(failedSessionRead.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testCancellationFailsClosedBeforeAndDuringProtectedVerification() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    var shouldContinue = false

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession(),
        shouldContinue: { shouldContinue }
      )
    }
    XCTAssertEqual(fixture.events.values, [])

    shouldContinue = true
    fixture.keys.afterX963Read = { shouldContinue = false }
    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signed.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession(),
        shouldContinue: { shouldContinue }
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testWrongSignatureFailsAfterCurrentActiveBindingWithoutRawVerifierError() throws {
    let activeKey = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: activeKey)
    let challenge = try loadChallenge()
    let signedByOtherKey = try makeReceipt(
      challenge: challenge,
      key: P256.Signing.PrivateKey(),
      configuration: fixture.configuration
    )

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: signedByOtherKey.canonicalReceipt,
        expectedChallenge: challenge,
        session: ReceiptVerificationSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testVerificationHoldsTheFixedSlotLeaseUntilReceiptCheckFinishes() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let signed = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    let keyReadStarted = DispatchSemaphore(value: 0)
    let releaseKeyRead = DispatchSemaphore(value: 0)
    fixture.keys.blockRead(started: keyReadStarted, release: releaseKeyRead)

    let verificationDone = DispatchSemaphore(value: 0)
    let verificationResult = ReceiptVerificationResultBox()
    DispatchQueue.global(qos: .userInitiated).async {
      do {
        _ = try fixture.coordinator.verifyCurrentReceipt(
          receiptData: signed.canonicalReceipt,
          expectedChallenge: challenge,
          session: ReceiptVerificationSession()
        )
        verificationResult.succeed()
      } catch {
        verificationResult.fail(error)
      }
      verificationDone.signal()
    }
    XCTAssertEqual(keyReadStarted.wait(timeout: .now() + 2), .success)

    let revocationAttempted = DispatchSemaphore(value: 0)
    let revocationDone = DispatchSemaphore(value: 0)
    let revocationResult = ReceiptVerificationResultBox()
    DispatchQueue.global(qos: .userInitiated).async {
      revocationAttempted.signal()
      do {
        _ = try fixture.coordinator.revoke(session: ReceiptVerificationSession())
        revocationResult.succeed()
      } catch {
        revocationResult.fail(error)
      }
      revocationDone.signal()
    }
    XCTAssertEqual(revocationAttempted.wait(timeout: .now() + 2), .success)
    XCTAssertEqual(fixture.codeIdentity.callCount, 1)
    XCTAssertEqual(fixture.provenance.replaceCalls, 0)

    releaseKeyRead.signal()
    XCTAssertEqual(verificationDone.wait(timeout: .now() + 2), .success)
    XCTAssertEqual(revocationDone.wait(timeout: .now() + 2), .success)
    guard let verification = verificationResult.value, case .success = verification else {
      return XCTFail("verification must finish before revocation acquires the lease")
    }
    guard let revocation = revocationResult.value, case .success = revocation else {
      return XCTFail("revocation must run after the verification lease releases")
    }
    XCTAssertEqual(fixture.codeIdentity.callCount, 2)
    guard case .revoked? = fixture.provenance.record else {
      return XCTFail("revocation must persist only after verification completes")
    }
  }

  private func makeFixture(
    key: P256.Signing.PrivateKey,
    codeIdentityFails: Bool = false,
    x963ReadFails: Bool = false
  ) throws -> ReceiptVerificationFixture {
    let configuration = try makeConfiguration()
    let events = ReceiptVerificationEvents()
    let clock = ReceiptVerificationClock(initial: now)
    let provenance = ReceiptVerificationProvenanceStore(events: events)
    provenance.record = .active(try active(for: key, configuration: configuration))
    let keys = ReceiptVerificationKeyOperations(
      events: events,
      x963: key.publicKey.x963Representation,
      failX963Read: x963ReadFails
    )
    let codeIdentity = ReceiptVerificationCodeIdentity(
      events: events,
      shouldFail: codeIdentityFails
    )
    let coordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: codeIdentity,
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: ReceiptVerificationIdentifier(),
      timestampGenerator: { "2030-01-01T00:00:01.000000Z" },
      clock: { clock.now() }
    )
    return ReceiptVerificationFixture(
      configuration: configuration,
      coordinator: coordinator,
      events: events,
      provenance: provenance,
      keys: keys,
      codeIdentity: codeIdentity,
      clock: clock
    )
  }

  private func makeConfiguration() throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
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
    configuration: TrustedHostConfiguration
  ) throws -> SignedApprovalReceipt {
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + base64URLEncoded(Data(repeating: 0xA5, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: key.signature(for: unsigned.signingPayload.domainSeparatedBytes).derRepresentation
    )
  }

  private func pending(for configuration: TrustedHostConfiguration) throws
    -> InitialEnrollmentPending
  {
    try InitialEnrollmentPending(enrollmentID: enrollmentID, intent: try intent(for: configuration))
  }

  private func active(
    for key: P256.Signing.PrivateKey,
    configuration: TrustedHostConfiguration
  ) throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: enrollmentID,
      intent: try intent(for: configuration),
      publicKeyX963SHA256: CanonicalJSON.sha256Hex(key.publicKey.x963Representation),
      createdAtText: "2030-01-01T00:00:00.000000Z"
    )
  }

  private func revoked(
    for key: P256.Signing.PrivateKey,
    configuration: TrustedHostConfiguration
  ) throws -> InitialEnrollmentRevoked {
    try InitialEnrollmentTransitions.revoke(
      active: try active(for: key, configuration: configuration),
      revokedAtText: "2030-01-01T00:00:01.000000Z"
    )
  }

  private func intent(for configuration: TrustedHostConfiguration) throws -> InitialEnrollmentIntent
  {
    try InitialEnrollmentIntent(
      hostReference: configuration.hostReference,
      signingKeyReference: configuration.signingKeyReference,
      keyStorage: configuration.keyStorage
    )
  }

  private func replacingSignature(in receipt: Data, with signature: String) throws -> Data {
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(receipt) else {
      throw ReceiptVerificationError.malformedReceipt
    }
    fields["signature"] = .string(signature)
    return try CanonicalJSON.encode(.object(fields))
  }

  private func replacingNonce(in challenge: Data) throws -> Data {
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(challenge) else {
      throw ApprovalHostError.malformedChallenge
    }
    fields["nonce"] = .string(base64URLEncoded(Data(repeating: 0xFA, count: 32)))
    return try CanonicalJSON.encode(.object(fields))
  }

  private func assertProtectedError(
    _ expected: ProtectedKeyStoreError,
    _ operation: () throws -> Void
  ) {
    XCTAssertThrowsError(try operation()) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, expected)
    }
  }

  private var enrollmentID: String {
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }
}

private struct ReceiptVerificationFixture {
  let configuration: TrustedHostConfiguration
  let coordinator: InitialEnrollmentCoordinator
  let events: ReceiptVerificationEvents
  let provenance: ReceiptVerificationProvenanceStore
  let keys: ReceiptVerificationKeyOperations
  let codeIdentity: ReceiptVerificationCodeIdentity
  let clock: ReceiptVerificationClock
}

private final class ReceiptVerificationSession: EnrollmentProtectedSession, @unchecked Sendable {}

private final class ReceiptVerificationEvents: @unchecked Sendable {
  private let lock = NSLock()
  private var storage: [String] = []

  var values: [String] {
    lock.lock()
    defer { lock.unlock() }
    return storage
  }

  func append(_ value: String) {
    lock.lock()
    storage.append(value)
    lock.unlock()
  }

  func clear() {
    lock.lock()
    storage.removeAll()
    lock.unlock()
  }
}

private final class ReceiptVerificationClock: @unchecked Sendable {
  private let lock = NSLock()
  private var queued: [Date] = []
  private var fallback: Date

  init(initial: Date) { fallback = initial }

  func setSequence(_ samples: [Date]) {
    lock.lock()
    queued = samples
    if let final = samples.last { fallback = final }
    lock.unlock()
  }

  func now() -> Date {
    lock.lock()
    defer { lock.unlock() }
    guard !queued.isEmpty else { return fallback }
    let sampled = queued.removeFirst()
    fallback = sampled
    return sampled
  }
}

private final class ReceiptVerificationCodeIdentity: HostCodeIdentityValidator, @unchecked Sendable
{
  private let lock = NSLock()
  private let events: ReceiptVerificationEvents
  private let shouldFail: Bool
  private var calls = 0

  init(events: ReceiptVerificationEvents, shouldFail: Bool) {
    self.events = events
    self.shouldFail = shouldFail
  }

  var callCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return calls
  }

  func validateCurrentHost(requirement _: String) throws {
    events.append("code")
    lock.lock()
    calls += 1
    lock.unlock()
    if shouldFail { throw ProtectedKeyStoreError.codeIdentityInvalid }
  }
}

private final class ReceiptVerificationProvenanceStore: EnrollmentProvenanceStore,
  @unchecked Sendable
{
  private let lock = NSLock()
  private let events: ReceiptVerificationEvents
  private var storage: InitialEnrollmentProvenance?
  private var replacements = 0

  init(events: ReceiptVerificationEvents) { self.events = events }

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

  var replaceCalls: Int {
    lock.lock()
    defer { lock.unlock() }
    return replacements
  }

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    events.append("provenance.load")
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
    events.append("provenance.replace")
    lock.lock()
    replacements += 1
    storage = provenance
    lock.unlock()
  }
}

private final class ReceiptVerificationKeyOperations: CurrentReceiptVerificationKeyOperations,
  @unchecked Sendable
{
  private let lock = NSLock()
  private let events: ReceiptVerificationEvents
  private let failX963Read: Bool
  private var started: DispatchSemaphore?
  private var release: DispatchSemaphore?
  var afterX963Read: (() -> Void)?
  var x963: Data

  init(events: ReceiptVerificationEvents, x963: Data, failX963Read: Bool) {
    self.events = events
    self.x963 = x963
    self.failX963Read = failX963Read
  }

  func blockRead(started: DispatchSemaphore, release: DispatchSemaphore) {
    lock.lock()
    self.started = started
    self.release = release
    lock.unlock()
  }

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
    events.append("key.x963")
    lock.lock()
    let started = self.started
    let release = self.release
    let afterX963Read = self.afterX963Read
    let x963 = self.x963
    lock.unlock()
    started?.signal()
    if let release, release.wait(timeout: .now() + 2) != .success {
      throw ProtectedKeyStoreError.unavailable
    }
    if failX963Read { throw ProtectedKeyStoreError.unavailable }
    afterX963Read?()
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

private final class ReceiptVerificationIdentifier: EnrollmentIdentifierGenerator,
  @unchecked Sendable
{
  func nextEnrollmentIdentifier() throws -> String {
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }
}

private final class ReceiptVerificationResultBox: @unchecked Sendable {
  private let lock = NSLock()
  private var result: Result<Void, Error>?

  var value: Result<Void, Error>? {
    lock.lock()
    defer { lock.unlock() }
    return result
  }

  func succeed() {
    lock.lock()
    result = .success(())
    lock.unlock()
  }

  func fail(_ error: Error) {
    lock.lock()
    result = .failure(error)
    lock.unlock()
  }
}

import CryptoKit
@preconcurrency import Dispatch
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Independent fake-only adversarial coverage for current-receipt verification.
/// These tests use only in-memory adapters and ephemeral software P-256 keys.
/// They do not instantiate LocalAuthentication, access Keychain/Security,
/// display AppKit, connect XPC, or contact a provider.
final class CurrentReceiptVerificationUltraReviewTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_030)

  func testExactTypedChallengeRebindingFailsBeforeCodeOrProtectedReads() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let original = try loadChallenge()
    let receipt = try makeReceipt(
      challenge: original, key: key, configuration: fixture.configuration)
    let rebound = try challengeWithDifferentNonce(from: original)

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: receipt.canonicalReceipt,
        expectedChallenge: rebound,
        session: UltraReceiptSession()
      )
    }

    XCTAssertEqual(fixture.events.values, [])
    XCTAssertEqual(fixture.codeIdentity.callCount, 0)
    XCTAssertEqual(fixture.provenance.loadCalls, 0)
    XCTAssertEqual(fixture.keys.x963Calls, 0)
  }

  func testNonfiniteTrustedClockStopsBeforeIdentityOrProtectedReads() throws {
    for interval in [Double.infinity, -Double.infinity, Double.nan] {
      let key = P256.Signing.PrivateKey()
      let clock = UltraReceiptClock(initial: Date(timeIntervalSince1970: interval))
      let fixture = try makeFixture(key: key, clock: clock)
      let challenge = try loadChallenge()
      let receipt = try makeReceipt(
        challenge: challenge, key: key, configuration: fixture.configuration)

      assertProtectedError(.unavailable) {
        _ = try fixture.coordinator.verifyCurrentReceipt(
          receiptData: receipt.canonicalReceipt,
          expectedChallenge: challenge,
          session: UltraReceiptSession()
        )
      }
      XCTAssertEqual(fixture.events.values, [])
      XCTAssertEqual(fixture.provenance.loadCalls, 0)
      XCTAssertEqual(fixture.keys.x963Calls, 0)
    }
  }

  func testCancellationAfterCodeIdentityCannotReachAProtectedRead() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let receipt = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    var checks = 0

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: receipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession(),
        shouldContinue: {
          checks += 1
          return checks <= 2
        }
      )
    }

    XCTAssertEqual(checks, 3)
    XCTAssertEqual(fixture.events.values, ["code"])
    XCTAssertEqual(fixture.provenance.loadCalls, 0)
    XCTAssertEqual(fixture.keys.x963Calls, 0)
  }

  func testClockAdvanceFromContinuationStopsBeforeEachProtectedRead() throws {
    let challenge = try loadChallenge()

    let provenanceClock = UltraReceiptClock(initial: now)
    let provenanceKey = P256.Signing.PrivateKey()
    let provenanceFixture = try makeFixture(key: provenanceKey, clock: provenanceClock)
    let provenanceReceipt = try makeReceipt(
      challenge: challenge,
      key: provenanceKey,
      configuration: provenanceFixture.configuration
    )
    var expiredAfterIdentity = false
    assertProtectedError(.unavailable) {
      _ = try provenanceFixture.coordinator.verifyCurrentReceipt(
        receiptData: provenanceReceipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession(),
        shouldContinue: {
          if !expiredAfterIdentity, provenanceFixture.codeIdentity.callCount > 0 {
            expiredAfterIdentity = true
            provenanceClock.set(challenge.expiresAt)
          }
          return true
        }
      )
    }
    XCTAssertTrue(expiredAfterIdentity)
    XCTAssertEqual(provenanceFixture.events.values, ["code"])
    XCTAssertEqual(provenanceFixture.provenance.loadCalls, 0)
    XCTAssertEqual(provenanceFixture.keys.x963Calls, 0)

    let keyClock = UltraReceiptClock(initial: now)
    let key = P256.Signing.PrivateKey()
    let keyFixture = try makeFixture(key: key, clock: keyClock)
    let keyReceipt = try makeReceipt(
      challenge: challenge,
      key: key,
      configuration: keyFixture.configuration
    )
    var expiredAfterProvenance = false
    assertProtectedError(.unavailable) {
      _ = try keyFixture.coordinator.verifyCurrentReceipt(
        receiptData: keyReceipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession(),
        shouldContinue: {
          if !expiredAfterProvenance, keyFixture.provenance.loadCalls > 0 {
            expiredAfterProvenance = true
            keyClock.set(challenge.expiresAt)
          }
          return true
        }
      )
    }
    XCTAssertTrue(expiredAfterProvenance)
    XCTAssertEqual(keyFixture.events.values, ["code", "provenance.load"])
    XCTAssertEqual(keyFixture.keys.x963Calls, 0)
  }

  func testRegressingClockAfterKeyReadCannotProduceAReceipt() throws {
    let key = P256.Signing.PrivateKey()
    let clock = UltraReceiptClock(initial: now)
    let fixture = try makeFixture(key: key, clock: clock)
    let challenge = try loadChallenge()
    let receipt = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    var advancedForKeyRead = false
    fixture.keys.afterX963Read = { clock.set(self.now) }

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: receipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession(),
        shouldContinue: {
          if !advancedForKeyRead, fixture.provenance.loadCalls > 0 {
            advancedForKeyRead = true
            clock.set(self.now.addingTimeInterval(1))
          }
          return true
        }
      )
    }

    XCTAssertTrue(advancedForKeyRead)
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testMalformedCurrentKeyAndOpaqueStoreFailureNormalizeAndReleaseTheLease() throws {
    let key = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: key)
    let challenge = try loadChallenge()
    let receipt = try makeReceipt(
      challenge: challenge, key: key, configuration: fixture.configuration)
    let malformedX963 = Data([0x04]) + Data(repeating: 0, count: 64)
    fixture.keys.setX963(malformedX963)
    fixture.provenance.record = .active(
      try active(for: malformedX963, configuration: fixture.configuration)
    )

    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: receipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])

    fixture.events.clear()
    fixture.provenance.loadFailure = UltraPrivateStoreError.privateDetail
    assertProtectedError(.unavailable) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: receipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load"])
    XCTAssertEqual(fixture.keys.x963Calls, 1)

    fixture.provenance.loadFailure = nil
    fixture.keys.setX963(key.publicKey.x963Representation)
    fixture.provenance.record = .active(
      try active(for: key.publicKey.x963Representation, configuration: fixture.configuration)
    )
    fixture.events.clear()
    let checked = try fixture.coordinator.verifyCurrentReceipt(
      receiptData: receipt.canonicalReceipt,
      expectedChallenge: challenge,
      session: UltraReceiptSession()
    )

    XCTAssertEqual(checked, receipt)
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load", "key.x963"])
  }

  func testFailedVerificationSerializesWithRevokeAndLaterReadsTheRevokedState() throws {
    let activeKey = P256.Signing.PrivateKey()
    let fixture = try makeFixture(key: activeKey)
    let challenge = try loadChallenge()
    let wrongReceipt = try makeReceipt(
      challenge: challenge,
      key: P256.Signing.PrivateKey(),
      configuration: fixture.configuration
    )
    let validReceipt = try makeReceipt(
      challenge: challenge,
      key: activeKey,
      configuration: fixture.configuration
    )
    let keyReadStarted = DispatchSemaphore(value: 0)
    let releaseKeyRead = DispatchSemaphore(value: 0)
    fixture.keys.blockX963Read(started: keyReadStarted, release: releaseKeyRead)

    let verificationFinished = DispatchSemaphore(value: 0)
    let verificationResult = UltraReceiptResult()
    DispatchQueue.global(qos: .userInitiated).async {
      do {
        _ = try fixture.coordinator.verifyCurrentReceipt(
          receiptData: wrongReceipt.canonicalReceipt,
          expectedChallenge: challenge,
          session: UltraReceiptSession()
        )
        verificationResult.succeed()
      } catch {
        verificationResult.fail(error)
      }
      verificationFinished.signal()
    }
    XCTAssertEqual(keyReadStarted.wait(timeout: .now() + 2), .success)

    let revokeAttempted = DispatchSemaphore(value: 0)
    let revokeFinished = DispatchSemaphore(value: 0)
    let revokeResult = UltraReceiptResult()
    DispatchQueue.global(qos: .userInitiated).async {
      revokeAttempted.signal()
      do {
        _ = try fixture.coordinator.revoke(session: UltraReceiptSession())
        revokeResult.succeed()
      } catch {
        revokeResult.fail(error)
      }
      revokeFinished.signal()
    }
    XCTAssertEqual(revokeAttempted.wait(timeout: .now() + 2), .success)
    XCTAssertEqual(fixture.provenance.replaceCalls, 0)

    releaseKeyRead.signal()
    XCTAssertEqual(verificationFinished.wait(timeout: .now() + 2), .success)
    XCTAssertEqual(revokeFinished.wait(timeout: .now() + 2), .success)
    XCTAssertEqual(verificationResult.error as? ProtectedKeyStoreError, .unavailable)
    XCTAssertNil(revokeResult.error)
    guard case .revoked? = fixture.provenance.record else {
      return XCTFail("revocation must persist after the failed verification releases the lease")
    }

    fixture.events.clear()
    assertProtectedError(.revoked) {
      _ = try fixture.coordinator.verifyCurrentReceipt(
        receiptData: validReceipt.canonicalReceipt,
        expectedChallenge: challenge,
        session: UltraReceiptSession()
      )
    }
    XCTAssertEqual(fixture.events.values, ["code", "provenance.load"])
  }

  private func makeFixture(
    key: P256.Signing.PrivateKey,
    clock: UltraReceiptClock? = nil
  ) throws -> UltraReceiptFixture {
    let reviewClock = clock ?? UltraReceiptClock(initial: now)
    let configuration = try makeConfiguration()
    let events = UltraReceiptEvents()
    let provenance = UltraReceiptProvenance(events: events)
    provenance.record = .active(
      try active(for: key.publicKey.x963Representation, configuration: configuration)
    )
    let keys = UltraReceiptKeys(events: events, x963: key.publicKey.x963Representation)
    let codeIdentity = UltraReceiptCodeIdentity(events: events)
    let coordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: codeIdentity,
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: UltraReceiptIdentifier(),
      timestampGenerator: { "2030-01-01T00:00:01.000000Z" },
      clock: { reviewClock.read() }
    )
    return UltraReceiptFixture(
      configuration: configuration,
      coordinator: coordinator,
      events: events,
      provenance: provenance,
      keys: keys,
      codeIdentity: codeIdentity
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
        provenanceService: "fixture.ultra.current-receipt",
        provenanceAccount: "fixed-slot"
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

  private func challengeWithDifferentNonce(from challenge: ApprovalChallenge) throws
    -> ApprovalChallenge
  {
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(challenge.canonicalBytes)
    else {
      throw ReceiptVerificationError.malformedReceipt
    }
    let replacement = base64URLEncoded(Data(repeating: 0xB6, count: 32))
    XCTAssertNotEqual(fields["nonce"], .string(replacement))
    fields["nonce"] = .string(replacement)
    return try ApprovalChallenge.decode(try CanonicalJSON.encode(.object(fields)), now: now)
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
      receiptIdentifier: "approval-receipt:" + base64URLEncoded(Data(repeating: 0xC8, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: key.signature(for: unsigned.signingPayload.domainSeparatedBytes).derRepresentation
    )
  }

  private func active(
    for x963: Data,
    configuration: TrustedHostConfiguration
  ) throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      intent: try InitialEnrollmentIntent(
        hostReference: configuration.hostReference,
        signingKeyReference: configuration.signingKeyReference,
        keyStorage: configuration.keyStorage
      ),
      publicKeyX963SHA256: CanonicalJSON.sha256Hex(x963),
      createdAtText: "2030-01-01T00:00:00.000000Z"
    )
  }

  private func assertProtectedError(
    _ expected: ProtectedKeyStoreError,
    _ operation: () throws -> Void
  ) {
    XCTAssertThrowsError(try operation()) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, expected)
    }
  }
}

private struct UltraReceiptFixture {
  let configuration: TrustedHostConfiguration
  let coordinator: InitialEnrollmentCoordinator
  let events: UltraReceiptEvents
  let provenance: UltraReceiptProvenance
  let keys: UltraReceiptKeys
  let codeIdentity: UltraReceiptCodeIdentity
}

private enum UltraPrivateStoreError: Error {
  case privateDetail
}

private final class UltraReceiptSession: EnrollmentProtectedSession, @unchecked Sendable {}

private final class UltraReceiptClock: @unchecked Sendable {
  private let lock = NSLock()
  private var current: Date

  init(initial: Date) { current = initial }

  func read() -> Date {
    lock.lock()
    defer { lock.unlock() }
    return current
  }

  func set(_ value: Date) {
    lock.lock()
    current = value
    lock.unlock()
  }
}

private final class UltraReceiptEvents: @unchecked Sendable {
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

private final class UltraReceiptCodeIdentity: HostCodeIdentityValidator, @unchecked Sendable {
  private let lock = NSLock()
  private let events: UltraReceiptEvents
  private var calls = 0

  init(events: UltraReceiptEvents) { self.events = events }

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
  }
}

private final class UltraReceiptProvenance: EnrollmentProvenanceStore, @unchecked Sendable {
  private let lock = NSLock()
  private let events: UltraReceiptEvents
  private var storage: InitialEnrollmentProvenance?
  private var readFailure: Error?
  private var reads = 0
  private var replacements = 0

  init(events: UltraReceiptEvents) { self.events = events }

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

  var loadFailure: Error? {
    get {
      lock.lock()
      defer { lock.unlock() }
      return readFailure
    }
    set {
      lock.lock()
      readFailure = newValue
      lock.unlock()
    }
  }

  var loadCalls: Int {
    lock.lock()
    defer { lock.unlock() }
    return reads
  }

  var replaceCalls: Int {
    lock.lock()
    defer { lock.unlock() }
    return replacements
  }

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    events.append("provenance.load")
    lock.lock()
    reads += 1
    let failure = readFailure
    let result = storage
    lock.unlock()
    if let failure { throw failure }
    return result
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

private final class UltraReceiptKeys: CurrentReceiptVerificationKeyOperations, @unchecked Sendable {
  private let lock = NSLock()
  private let events: UltraReceiptEvents
  private var currentX963: Data
  private var starts: DispatchSemaphore?
  private var release: DispatchSemaphore?
  private var x963ReadCallback: (() -> Void)?
  private var reads = 0

  init(events: UltraReceiptEvents, x963: Data) {
    self.events = events
    currentX963 = x963
  }

  var x963Calls: Int {
    lock.lock()
    defer { lock.unlock() }
    return reads
  }

  func setX963(_ x963: Data) {
    lock.lock()
    currentX963 = x963
    lock.unlock()
  }

  func blockX963Read(started: DispatchSemaphore, release: DispatchSemaphore) {
    lock.lock()
    starts = started
    self.release = release
    lock.unlock()
  }

  var afterX963Read: (() -> Void)? {
    get {
      lock.lock()
      defer { lock.unlock() }
      return x963ReadCallback
    }
    set {
      lock.lock()
      x963ReadCallback = newValue
      lock.unlock()
    }
  }

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    nil
  }

  func createAndObservePublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    CanonicalJSON.sha256Hex(current())
  }

  func observeExistingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws
    -> String
  {
    CanonicalJSON.sha256Hex(current())
  }

  func observeExistingPublicKeyX963(session _: any EnrollmentProtectedSession) throws -> Data {
    events.append("key.x963")
    lock.lock()
    reads += 1
    let started = starts
    let release = release
    let callback = x963ReadCallback
    let x963 = currentX963
    lock.unlock()
    started?.signal()
    if let release, release.wait(timeout: .now() + 2) != .success {
      throw UltraPrivateStoreError.privateDetail
    }
    callback?()
    return x963
  }

  func signExistingKey(
    session _: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint _: String,
    payload _: Data
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }

  private func current() -> Data {
    lock.lock()
    defer { lock.unlock() }
    return currentX963
  }
}

private final class UltraReceiptIdentifier: EnrollmentIdentifierGenerator, @unchecked Sendable {
  func nextEnrollmentIdentifier() throws -> String {
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }
}

private final class UltraReceiptResult: @unchecked Sendable {
  private let lock = NSLock()
  private var outcome: Error?

  var error: Error? {
    lock.lock()
    defer { lock.unlock() }
    return outcome
  }

  func succeed() {
    lock.lock()
    outcome = nil
    lock.unlock()
  }

  func fail(_ error: Error) {
    lock.lock()
    outcome = error
    lock.unlock()
  }
}

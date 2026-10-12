import ApprovalHostCore
import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostMacOS

private func fixtureBase64URL(_ data: Data) -> String {
  data.base64EncodedString()
    .replacingOccurrences(of: "+", with: "-")
    .replacingOccurrences(of: "/", with: "_")
    .replacingOccurrences(of: "=", with: "")
}

final class ReceiptRedemptionCoordinatorTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_030)

  func testRedeemUsesOneLeaseAndReturnsSourceBoundOneUsePermit() throws {
    let fixture = try makeFixture()
    let permit = try makeCoordinator(fixture).redeem(
      receiptData: fixture.receipt.canonicalReceipt,
      expectedChallenge: fixture.challenge,
      session: FixtureSession()
    )

    XCTAssertEqual(fixture.slot.leaseInvocations, 1)
    XCTAssertEqual(fixture.lease.verifyCalls, 2)
    XCTAssertEqual(fixture.lease.pendingWriteCount, 1)
    XCTAssertEqual(fixture.lease.pendingReplaceCount, 1)
    XCTAssertEqual(fixture.lease.globalReplaceCount, 2)
    guard let state = fixture.lease.states[fixture.challenge.replayKey],
      case .verified(let verified) = try ReceiptRedemptionStateV1.decode(state),
      let globalBytes = fixture.lease.globalState,
      case .ready(let ready) = try ReceiptRedemptionGlobalStateV1.decode(globalBytes)
    else { return XCTFail("expected verified nonce and cleared global intent") }
    XCTAssertEqual(ready.generation, 3)
    XCTAssertTrue(permit.claimPendingProtectedExecution(binding: verified.pending.binding))
    XCTAssertFalse(permit.claimPendingProtectedExecution(binding: verified.pending.binding))
    XCTAssertFalse(permit is any Encodable)
  }

  func testInvalidOrRevokedCurrentReceiptLeavesGlobalReadyAndNonceStateUnchanged() throws {
    let invalidFixture = try makeFixture()
    let initialGlobal = try XCTUnwrap(invalidFixture.lease.globalState)
    let forged = try makeReceipt(
      challenge: invalidFixture.challenge,
      key: P256.Signing.PrivateKey(),
      configuration: invalidFixture.configuration,
      identifierByte: 0xD4
    )
    assertRedemptionError(.currentStateUnavailable) {
      _ = try makeCoordinator(invalidFixture).redeem(
        receiptData: forged.canonicalReceipt,
        expectedChallenge: invalidFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(invalidFixture.lease.globalState, initialGlobal)
    XCTAssertTrue(invalidFixture.lease.states.isEmpty)
    XCTAssertEqual(invalidFixture.lease.globalReplaceCount, 0)
    XCTAssertEqual(invalidFixture.lease.pendingWriteCount, 0)

    let revokedFixture = try makeFixture()
    let revokedGlobal = try XCTUnwrap(revokedFixture.lease.globalState)
    revokedFixture.lease.verificationFailures = [.revoked]
    assertRedemptionError(.currentStateRevoked) {
      _ = try makeCoordinator(revokedFixture).redeem(
        receiptData: revokedFixture.receipt.canonicalReceipt,
        expectedChallenge: revokedFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(revokedFixture.lease.globalState, revokedGlobal)
    XCTAssertTrue(revokedFixture.lease.states.isEmpty)
    XCTAssertEqual(revokedFixture.lease.globalReplaceCount, 0)
  }

  func testMalformedOrReboundReceiptNeverAcquiresProtectedSlot() throws {
    let fixture = try makeFixture()
    assertRedemptionError(.currentStateUnavailable) {
      _ = try makeCoordinator(fixture).redeem(
        receiptData: Data("{}".utf8),
        expectedChallenge: fixture.challenge,
        session: FixtureSession()
      )
    }

    guard
      case .object(var challengeFields) = try CanonicalJSON.parseCanonical(
        fixture.challenge.canonicalBytes)
    else { return XCTFail("expected challenge object") }
    challengeFields["nonce"] = .string(fixtureBase64URL(Data(repeating: 0xD3, count: 32)))
    let rebound = try ApprovalChallenge.decode(
      CanonicalJSON.encode(.object(challengeFields)), now: now)
    assertRedemptionError(.currentStateUnavailable) {
      _ = try makeCoordinator(fixture).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: rebound,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(fixture.slot.leaseInvocations, 0)
    XCTAssertTrue(fixture.lease.events.isEmpty)
    XCTAssertTrue(fixture.lease.states.isEmpty)
  }

  func testMissingMalformedOrIntentGlobalStateBlocksBeforeCurrentKeyVerification() throws {
    let missing = try makeFixture()
    missing.lease.globalState = nil
    assertRedemptionError(.globalStateRequired) {
      _ = try makeCoordinator(missing).redeem(
        receiptData: missing.receipt.canonicalReceipt,
        expectedChallenge: missing.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(missing.lease.verifyCalls, 0)
    XCTAssertEqual(missing.lease.pendingWriteCount, 0)

    let malformed = try makeFixture()
    malformed.lease.globalState = Data("{}".utf8)
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try makeCoordinator(malformed).redeem(
        receiptData: malformed.receipt.canonicalReceipt,
        expectedChallenge: malformed.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(malformed.lease.verifyCalls, 0)
    XCTAssertEqual(malformed.lease.pendingWriteCount, 0)

    let intent = try makeFixture()
    let binding = try binding(for: intent)
    let ready = try XCTUnwrap(intent.lease.globalState)
    let intentState = try ReceiptRedemptionGlobalStateV1.decode(ready).nextIntent(
      binding: binding,
      observedAtText: "2030-01-01T00:00:31.000000Z"
    )
    intent.lease.globalState = try intentState.canonicalBytes()
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try makeCoordinator(intent).redeem(
        receiptData: intent.receipt.canonicalReceipt,
        expectedChallenge: intent.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(intent.lease.verifyCalls, 0)
    XCTAssertEqual(intent.lease.pendingWriteCount, 0)
  }

  func testPendingNonceWithReadyOrDifferentIntentBlocksBeforeVerification() throws {
    let readyFixture = try makeFixture()
    let binding = try binding(for: readyFixture)
    let pending = try ReceiptRedemptionStateV1.pending(
      ReceiptRedemptionPendingV1(binding: binding, reservedAtText: "2030-01-01T00:00:30.000000Z")
    )
    readyFixture.lease.states[readyFixture.challenge.replayKey] = try pending.canonicalBytes()
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try makeCoordinator(readyFixture).redeem(
        receiptData: readyFixture.receipt.canonicalReceipt,
        expectedChallenge: readyFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(readyFixture.lease.verifyCalls, 0)

    let mismatchFixture = try makeFixture()
    let alternate = try makeReceipt(
      challenge: mismatchFixture.challenge,
      key: mismatchFixture.key,
      configuration: mismatchFixture.configuration,
      identifierByte: 0xD5
    )
    let alternateBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: mismatchFixture.configuration,
      expectedChallenge: mismatchFixture.challenge,
      canonicalReceipt: alternate.canonicalReceipt
    )
    let global = try XCTUnwrap(mismatchFixture.lease.globalState)
    mismatchFixture.lease.globalState = try ReceiptRedemptionGlobalStateV1.decode(global)
      .nextIntent(
        binding: alternateBinding,
        observedAtText: "2030-01-01T00:00:31.000000Z"
      ).canonicalBytes()
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try makeCoordinator(mismatchFixture).redeem(
        receiptData: mismatchFixture.receipt.canonicalReceipt,
        expectedChallenge: mismatchFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(mismatchFixture.lease.verifyCalls, 0)
  }

  func testIntentReadbackAndPendingReadbackAmbiguitiesQuarantineAcrossRestart() throws {
    let intentFixture = try makeFixture()
    intentFixture.lease.eraseGlobalOnNextReadback = true
    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(intentFixture).redeem(
        receiptData: intentFixture.receipt.canonicalReceipt,
        expectedChallenge: intentFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertTrue(intentFixture.lease.states.isEmpty)
    assertRedemptionError(.globalStateRequired) {
      _ = try ReceiptRedemptionCoordinator(
        configuration: intentFixture.configuration,
        fixedSlot: intentFixture.slot,
        clock: SequenceClock([now, now]).next
      ).redeem(
        receiptData: intentFixture.receipt.canonicalReceipt,
        expectedChallenge: intentFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(intentFixture.lease.verifyCalls, 1)

    let pendingFixture = try makeFixture()
    pendingFixture.lease.erasePendingOnNextReadback = true
    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(pendingFixture).redeem(
        receiptData: pendingFixture.receipt.canonicalReceipt,
        expectedChallenge: pendingFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertNil(pendingFixture.lease.states[pendingFixture.challenge.replayKey])
    guard let global = pendingFixture.lease.globalState,
      case .intentPending = try ReceiptRedemptionGlobalStateV1.decode(global)
    else { return XCTFail("intent must remain after nonce readback ambiguity") }
    let verifyCalls = pendingFixture.lease.verifyCalls
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try ReceiptRedemptionCoordinator(
        configuration: pendingFixture.configuration,
        fixedSlot: pendingFixture.slot,
        clock: SequenceClock([now, now]).next
      ).redeem(
        receiptData: pendingFixture.receipt.canonicalReceipt,
        expectedChallenge: pendingFixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(pendingFixture.lease.verifyCalls, verifyCalls)
    XCTAssertEqual(pendingFixture.lease.pendingWriteCount, 1)
  }

  func testRollbackBeforeIntentWritesNothingAndCancellationAfterVerifyTwoDoesNotPromote() throws {
    let rollback = try makeFixture()
    rollback.lease.afterVerify = { [clock = rollback.clock, now] call in
      if call == 1 { clock.prepend(now.addingTimeInterval(-1)) }
    }
    let initialGlobal = try XCTUnwrap(rollback.lease.globalState)
    assertRedemptionError(.clockRollback) {
      _ = try makeCoordinator(rollback).redeem(
        receiptData: rollback.receipt.canonicalReceipt,
        expectedChallenge: rollback.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(rollback.lease.globalState, initialGlobal)
    XCTAssertTrue(rollback.lease.states.isEmpty)
    XCTAssertEqual(rollback.lease.globalReplaceCount, 0)

    let cancelled = try makeFixture()
    var continued = true
    cancelled.lease.afterVerify = { call in if call == 2 { continued = false } }
    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(cancelled).redeem(
        receiptData: cancelled.receipt.canonicalReceipt,
        expectedChallenge: cancelled.challenge,
        session: FixtureSession(),
        shouldContinue: { continued }
      )
    }
    XCTAssertEqual(cancelled.lease.pendingWriteCount, 1)
    XCTAssertEqual(cancelled.lease.pendingReplaceCount, 0)
    XCTAssertEqual(cancelled.lease.globalReplaceCount, 1)
    guard let global = cancelled.lease.globalState,
      case .intentPending = try ReceiptRedemptionGlobalStateV1.decode(global)
    else { return XCTFail("cancellation must retain global intent") }
  }

  func testVerifiedNonceReplaysAcrossCoordinatorReconstructionWithoutPermitOrVerification() throws {
    let fixture = try makeFixture()
    _ = try makeCoordinator(fixture).redeem(
      receiptData: fixture.receipt.canonicalReceipt,
      expectedChallenge: fixture.challenge,
      session: FixtureSession()
    )
    let calls = fixture.lease.verifyCalls
    assertRedemptionError(.replayed) {
      _ = try ReceiptRedemptionCoordinator(
        configuration: fixture.configuration,
        fixedSlot: fixture.slot,
        clock: SequenceClock([now.addingTimeInterval(7), now.addingTimeInterval(8)]).next
      ).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: FixtureSession()
      )
    }
    XCTAssertEqual(fixture.lease.verifyCalls, calls)
  }

  func testFinalGlobalReadbackCancellationOrExpiryNeverMintsPermit() throws {
    let cancelled = try makeFixture()
    var continued = true
    cancelled.lease.afterGlobalReplace = { count in if count == 2 { continued = false } }
    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(cancelled).redeem(
        receiptData: cancelled.receipt.canonicalReceipt,
        expectedChallenge: cancelled.challenge,
        session: FixtureSession(),
        shouldContinue: { continued }
      )
    }
    guard let cancelledState = cancelled.lease.states[cancelled.challenge.replayKey],
      case .verified = try ReceiptRedemptionStateV1.decode(cancelledState)
    else { return XCTFail("final cancellation must retain verified nonce") }
    XCTAssertEqual(cancelled.lease.globalReplaceCount, 2)

    let expired = try makeFixture()
    expired.lease.afterGlobalReplace = {
      [clock = expired.clock, challenge = expired.challenge] count in
      if count == 2 { clock.prepend(challenge.expiresAt) }
    }
    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(expired).redeem(
        receiptData: expired.receipt.canonicalReceipt,
        expectedChallenge: expired.challenge,
        session: FixtureSession()
      )
    }
    guard let expiredState = expired.lease.states[expired.challenge.replayKey],
      case .verified = try ReceiptRedemptionStateV1.decode(expiredState)
    else { return XCTFail("final expiry must retain verified nonce") }
  }

  private func makeCoordinator(_ fixture: Fixture) -> ReceiptRedemptionCoordinator {
    ReceiptRedemptionCoordinator(
      configuration: fixture.configuration,
      fixedSlot: fixture.slot,
      clock: fixture.clock.next
    )
  }

  private func binding(for fixture: Fixture) throws -> ReceiptRedemptionSourceBindingV1 {
    try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: fixture.receipt.canonicalReceipt
    )
  }

  private func makeFixture(clockValues: [Date]? = nil) throws -> Fixture {
    let configuration = try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:fixture",
      machServiceName: "invalid.fixture.service",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256
    )
    let challenge = try loadChallenge()
    let key = P256.Signing.PrivateKey()
    let receipt = try makeReceipt(challenge: challenge, key: key, configuration: configuration)
    let lease = try FixtureLease(
      publicKey: key.publicKey.x963Representation,
      verifier: PinnedReceiptVerifier(
        configuration: configuration,
        trustedPublicKeyX963: key.publicKey.x963Representation
      ),
      initialGlobalState: .ready(
        try ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: now)
      )
    )
    let slot = FixtureFixedSlot(lease: lease)
    return Fixture(
      configuration: configuration,
      challenge: challenge,
      receipt: receipt,
      key: key,
      lease: lease,
      slot: slot,
      clock: SequenceClock(
        clockValues
          ?? [
            now,
            now.addingTimeInterval(1),
            now.addingTimeInterval(2),
            now.addingTimeInterval(3),
            now.addingTimeInterval(4),
            now.addingTimeInterval(5),
            now.addingTimeInterval(6),
          ]
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
    identifierByte: UInt8 = 0xD2
  ) throws -> SignedApprovalReceipt {
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:"
        + fixtureBase64URL(Data(repeating: identifierByte, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        .derRepresentation
    )
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

private struct Fixture {
  let configuration: TrustedHostConfiguration
  let challenge: ApprovalChallenge
  let receipt: SignedApprovalReceipt
  let key: P256.Signing.PrivateKey
  let lease: FixtureLease
  let slot: FixtureFixedSlot
  let clock: SequenceClock
}

private final class FixtureSession: ReceiptRedemptionSession, @unchecked Sendable {
  func invalidate() {}
}

private enum FixtureStoreError: Error {
  case failure
}

private final class FixtureLease: ReceiptRedemptionLease, @unchecked Sendable {
  let verifier: PinnedReceiptVerifier
  let publicKey: Data
  var events: [String] = []
  var states: [String: Data] = [:]
  var globalState: Data?
  var verificationFailures: [ReceiptRedemptionFixedSlotError?] = []
  var verifyCalls = 0
  var pendingWriteCount = 0
  var pendingReplaceCount = 0
  var globalReplaceCount = 0
  var erasePendingOnNextReadback = false
  var eraseGlobalOnNextReadback = false
  var afterVerify: ((Int) -> Void)?
  var afterGlobalReplace: ((Int) -> Void)?

  init(
    publicKey: Data,
    verifier: PinnedReceiptVerifier,
    initialGlobalState: ReceiptRedemptionGlobalStateV1
  ) throws {
    self.verifier = verifier
    self.publicKey = publicKey
    globalState = try initialGlobalState.canonicalBytes()
  }

  func verifyCurrentReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session _: any ReceiptRedemptionSession,
    now: Date
  ) throws -> ReceiptRedemptionCurrentReceipt {
    events.append("verify")
    verifyCalls += 1
    defer { afterVerify?(verifyCalls) }
    if verifyCalls <= verificationFailures.count,
      let failure = verificationFailures[verifyCalls - 1]
    {
      throw failure
    }
    return try ReceiptRedemptionCurrentReceipt(
      receipt: verifier.verify(receiptData, expectedChallenge: expectedChallenge, now: now),
      activePublicKeyX963: publicKey, currentVerificationObservedAt: now)
  }

  func loadRedemptionState(nonceDigest: String) throws -> Data? {
    events.append("state.load")
    if erasePendingOnNextReadback, pendingWriteCount > 0 {
      erasePendingOnNextReadback = false
      states[nonceDigest] = nil
      return nil
    }
    return states[nonceDigest]
  }

  func loadGlobalRedemptionState() throws -> Data? {
    events.append("global.load")
    if eraseGlobalOnNextReadback, globalReplaceCount > 0 {
      eraseGlobalOnNextReadback = false
      globalState = nil
      return nil
    }
    return globalState
  }

  func writePendingRedemption(_ canonicalState: Data) throws {
    events.append("pending.write")
    let state = try ReceiptRedemptionStateV1.decode(canonicalState)
    guard state.isPending, states[state.binding.challengeNonceDigest] == nil else {
      throw FixtureStoreError.failure
    }
    states[state.binding.challengeNonceDigest] = canonicalState
    pendingWriteCount += 1
  }

  func replacePendingRedemption(_ expectedPending: Data, with canonicalVerified: Data) throws {
    events.append("pending.replace")
    let pending = try ReceiptRedemptionStateV1.decode(expectedPending)
    let verified = try ReceiptRedemptionStateV1.decode(canonicalVerified)
    guard pending.isPending,
      !verified.isPending,
      pending.binding == verified.binding,
      states[pending.binding.challengeNonceDigest] == expectedPending
    else { throw FixtureStoreError.failure }
    states[pending.binding.challengeNonceDigest] = canonicalVerified
    pendingReplaceCount += 1
  }

  func replaceGlobalRedemptionState(_ expected: Data, with canonicalState: Data) throws {
    events.append("global.replace")
    guard globalState == expected else { throw FixtureStoreError.failure }
    _ = try ReceiptRedemptionGlobalStateV1.decode(canonicalState)
    globalState = canonicalState
    globalReplaceCount += 1
    afterGlobalReplace?(globalReplaceCount)
  }
}

private final class FixtureFixedSlot: ReceiptRedemptionFixedSlotCoordinator, @unchecked Sendable {
  let lease: FixtureLease
  private var entered = false
  private(set) var leaseInvocations = 0

  init(lease: FixtureLease) {
    self.lease = lease
  }

  func withRedemptionLease<T>(
    _ operation: (any ReceiptRedemptionLease) throws -> T
  ) throws -> T {
    guard !entered else { throw FixtureStoreError.failure }
    entered = true
    leaseInvocations += 1
    defer { entered = false }
    return try operation(lease)
  }
}

private final class SequenceClock: @unchecked Sendable {
  private var values: [Date]

  init(_ values: [Date]) {
    self.values = values
  }

  func next() -> Date {
    guard !values.isEmpty else { return .distantFuture }
    return values.removeFirst()
  }

  func prepend(_ value: Date) {
    values.insert(value, at: 0)
  }
}

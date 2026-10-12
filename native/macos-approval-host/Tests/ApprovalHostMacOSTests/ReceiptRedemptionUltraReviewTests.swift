import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Independent fake-only adversarial coverage for receipt redemption. These
/// tests use ephemeral P-256 keys and in-memory lease/state fakes only. They
/// do not create Keychain, Security, Local Authentication, XPC, provider, or
/// filesystem-backed protected-store objects.
final class ReceiptRedemptionUltraReviewTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_030)

  func testWrongCurrentKeySignatureCannotBurnNonceOrWriteEitherRecord() throws {
    let fixture = try makeFixture()
    let forged = try makeReceipt(
      challenge: fixture.challenge,
      key: P256.Signing.PrivateKey(),
      configuration: fixture.configuration,
      identifierByte: 0xD4
    )
    let session = ReviewSession()
    let coordinator = makeCoordinator(fixture)
    let initialGlobal = fixture.lease.global

    assertRedemptionError(.currentStateUnavailable) {
      _ = try coordinator.redeem(
        receiptData: forged.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: session
      )
    }

    XCTAssertEqual(session.invalidations, 1)
    XCTAssertEqual(fixture.slot.entries, 1)
    XCTAssertEqual(fixture.lease.verifyCalls, 1)
    XCTAssertEqual(fixture.lease.pendingWrites, 0)
    XCTAssertEqual(fixture.lease.globalReplaceWrites, 0)
    XCTAssertTrue(fixture.lease.states.isEmpty)
    XCTAssertEqual(fixture.lease.global, initialGlobal)
  }

  func testDifferentSignedReceiptAtSameNonceNeedsRecoveryBeforeCurrentKeyRead() throws {
    let fixture = try makeFixture()
    let alternate = try makeReceipt(
      challenge: fixture.challenge,
      key: fixture.key,
      configuration: fixture.configuration,
      identifierByte: 0xD5
    )
    let alternateBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: alternate.canonicalReceipt
    )
    let retained = try ReceiptRedemptionStateV1.pending(
      ReceiptRedemptionPendingV1(
        binding: alternateBinding,
        reservedAtText: "2030-01-01T00:00:30.000000Z"
      )
    )
    fixture.lease.states[fixture.challenge.replayKey] = try retained.canonicalBytes()
    let coordinator = makeCoordinator(fixture)

    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try coordinator.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }

    XCTAssertEqual(fixture.slot.entries, 1)
    XCTAssertEqual(fixture.lease.verifyCalls, 0)
    XCTAssertEqual(fixture.lease.pendingWrites, 0)
    XCTAssertEqual(fixture.lease.globalReplaceWrites, 0)
    XCTAssertEqual(fixture.lease.events, ["global.load", "state.load"])
  }

  func testExpiryDuringSecondVerificationLeavesPendingAndBlocksFreshRetry() throws {
    let fixture = try makeFixture()
    fixture.lease.afterVerification = {
      [
        clock = fixture.clock,
        challenge = fixture.challenge,
      ] call in
      if call == 2 { clock.prepend(challenge.expiresAt) }
    }
    let coordinator = makeCoordinator(fixture)

    assertRedemptionError(.postReservationUnverified) {
      _ = try coordinator.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }

    let state = try XCTUnwrap(fixture.lease.states[fixture.challenge.replayKey])
    guard case .pending = try ReceiptRedemptionStateV1.decode(state) else {
      return XCTFail("expiry during second verification must retain PENDING")
    }
    XCTAssertEqual(fixture.lease.pendingWrites, 1)
    XCTAssertEqual(fixture.lease.globalReplaceWrites, 1)
    XCTAssertEqual(fixture.lease.replaceWrites, 0)

    let restarted = ReceiptRedemptionCoordinator(
      configuration: fixture.configuration,
      fixedSlot: fixture.slot,
      clock: ReviewClock([now, now]).next
    )
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try restarted.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }
    XCTAssertEqual(fixture.lease.pendingWrites, 1)
  }

  func testGlobalIntentReadbackAmbiguityNeverStartsNonceOrRetriesAfterRestart() throws {
    let fixture = try makeFixture()
    fixture.lease.hideGlobalReadbackAfterIntent = true
    let coordinator = makeCoordinator(fixture)

    assertRedemptionError(.postReservationUnverified) {
      _ = try coordinator.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }

    XCTAssertNil(fixture.lease.states[fixture.challenge.replayKey])
    guard let global = fixture.lease.global,
      case .intentPending = try ReceiptRedemptionGlobalStateV1.decode(global)
    else { return XCTFail("unverified global intent must remain quarantined") }
    XCTAssertEqual(fixture.lease.pendingWrites, 0)
    XCTAssertEqual(fixture.lease.globalReplaceWrites, 1)
    XCTAssertEqual(fixture.lease.replaceWrites, 0)

    fixture.lease.hideGlobalReadbackAfterIntent = false
    let restarted = ReceiptRedemptionCoordinator(
      configuration: fixture.configuration,
      fixedSlot: fixture.slot,
      clock: ReviewClock([now, now]).next
    )
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try restarted.redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }
    XCTAssertEqual(fixture.lease.verifyCalls, 1)
    XCTAssertEqual(fixture.lease.pendingWrites, 0)
    XCTAssertEqual(fixture.lease.replaceWrites, 0)
  }

  func testErasedNonceAfterIntentReservationRemainsQuarantinedAcrossRestart() throws {
    let fixture = try makeFixture()
    fixture.lease.erasePendingAfterWrite = true

    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(fixture).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }

    XCTAssertNil(fixture.lease.states[fixture.challenge.replayKey])
    guard let global = fixture.lease.global,
      case .intentPending(let intent) = try ReceiptRedemptionGlobalStateV1.decode(global)
    else { return XCTFail("lost nonce state must retain the exact global intent") }
    let expectedBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: fixture.receipt.canonicalReceipt
    )
    XCTAssertEqual(intent.binding, expectedBinding)
    XCTAssertEqual(fixture.lease.verifyCalls, 1)
    XCTAssertEqual(fixture.lease.pendingWrites, 1)

    fixture.lease.erasePendingAfterWrite = false
    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try ReceiptRedemptionCoordinator(
        configuration: fixture.configuration,
        fixedSlot: fixture.slot,
        clock: ReviewClock([now, now]).next
      ).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession()
      )
    }
    XCTAssertEqual(fixture.lease.verifyCalls, 1)
    XCTAssertEqual(fixture.lease.pendingWrites, 1)
  }

  func testCancellationAfterSecondVerificationDoesNotPromoteOrClearIntent() throws {
    let fixture = try makeFixture()
    let shouldContinue = { fixture.lease.verifyCalls < 2 }

    assertRedemptionError(.postReservationUnverified) {
      _ = try makeCoordinator(fixture).redeem(
        receiptData: fixture.receipt.canonicalReceipt,
        expectedChallenge: fixture.challenge,
        session: ReviewSession(),
        shouldContinue: shouldContinue
      )
    }

    let nonceState = try XCTUnwrap(fixture.lease.states[fixture.challenge.replayKey])
    guard case .pending = try ReceiptRedemptionStateV1.decode(nonceState) else {
      return XCTFail("cancellation after verify #2 must retain nonce PENDING")
    }
    guard let global = fixture.lease.global,
      case .intentPending = try ReceiptRedemptionGlobalStateV1.decode(global)
    else { return XCTFail("cancellation after verify #2 must retain global intent") }
    XCTAssertEqual(fixture.lease.verifyCalls, 2)
    XCTAssertEqual(fixture.lease.replaceWrites, 0)
    XCTAssertEqual(fixture.lease.globalReplaceWrites, 1)
  }

  func testGlobalTransitionsCannotOverwriteQuarantineOrClearReadyDirectly() throws {
    let fixture = try makeFixture()
    let binding = try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: fixture.receipt.canonicalReceipt
    )
    let ready = try ReceiptRedemptionGlobalStateV1.ready(
      ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: now)
    )
    let intent = try ready.nextIntent(
      binding: binding,
      observedAtText: "2030-01-01T00:00:31.000000Z"
    )
    let otherReceipt = try makeReceipt(
      challenge: fixture.challenge,
      key: fixture.key,
      configuration: fixture.configuration,
      identifierByte: 0xD6
    )
    let otherBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: otherReceipt.canonicalReceipt
    )

    XCTAssertThrowsError(
      try ready.nextReady(
        expectedBinding: binding,
        observedAtText: "2030-01-01T00:00:31.000000Z"
      )
    )
    XCTAssertThrowsError(
      try intent.nextIntent(
        binding: otherBinding,
        observedAtText: "2030-01-01T00:00:32.000000Z"
      )
    )
    XCTAssertThrowsError(
      try intent.nextReady(
        expectedBinding: otherBinding,
        observedAtText: "2030-01-01T00:00:32.000000Z"
      )
    )
  }

  func testReadyWithNoncePendingOrMismatchedGlobalIntentRequiresRecoveryBeforeVerify() throws {
    let pendingFixture = try makeFixture()
    let pendingBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: pendingFixture.configuration,
      expectedChallenge: pendingFixture.challenge,
      canonicalReceipt: pendingFixture.receipt.canonicalReceipt
    )
    pendingFixture.lease.states[pendingFixture.challenge.replayKey] =
      try ReceiptRedemptionStateV1
      .pending(
        ReceiptRedemptionPendingV1(
          binding: pendingBinding,
          reservedAtText: "2030-01-01T00:00:30.000000Z"
        )
      )
      .canonicalBytes()

    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try makeCoordinator(pendingFixture).redeem(
        receiptData: pendingFixture.receipt.canonicalReceipt,
        expectedChallenge: pendingFixture.challenge,
        session: ReviewSession()
      )
    }
    XCTAssertEqual(pendingFixture.lease.verifyCalls, 0)
    XCTAssertEqual(pendingFixture.lease.pendingWrites, 0)
    XCTAssertEqual(pendingFixture.lease.globalReplaceWrites, 0)

    let intentFixture = try makeFixture()
    let otherReceipt = try makeReceipt(
      challenge: intentFixture.challenge,
      key: intentFixture.key,
      configuration: intentFixture.configuration,
      identifierByte: 0xDA
    )
    let otherBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: intentFixture.configuration,
      expectedChallenge: intentFixture.challenge,
      canonicalReceipt: otherReceipt.canonicalReceipt
    )
    let ready = try XCTUnwrap(intentFixture.lease.global)
    intentFixture.lease.global = try ReceiptRedemptionGlobalStateV1.decode(ready)
      .nextIntent(
        binding: otherBinding,
        observedAtText: "2030-01-01T00:00:31.000000Z"
      )
      .canonicalBytes()

    assertRedemptionError(.pendingRecoveryRequired) {
      _ = try makeCoordinator(intentFixture).redeem(
        receiptData: intentFixture.receipt.canonicalReceipt,
        expectedChallenge: intentFixture.challenge,
        session: ReviewSession()
      )
    }
    XCTAssertEqual(intentFixture.lease.verifyCalls, 0)
    XCTAssertEqual(intentFixture.lease.pendingWrites, 0)
    XCTAssertEqual(intentFixture.lease.globalReplaceWrites, 0)
  }

  func testPermitRejectsWrongBindingWithoutConsumingItsOneExactClaim() throws {
    let fixture = try makeFixture()
    let permit = try makeCoordinator(fixture).redeem(
      receiptData: fixture.receipt.canonicalReceipt,
      expectedChallenge: fixture.challenge,
      session: ReviewSession()
    )
    let verifiedState = try XCTUnwrap(fixture.lease.states[fixture.challenge.replayKey])
    guard case .verified(let verified) = try ReceiptRedemptionStateV1.decode(verifiedState) else {
      return XCTFail("expected a local VERIFIED record")
    }
    let otherReceipt = try makeReceipt(
      challenge: fixture.challenge,
      key: fixture.key,
      configuration: fixture.configuration,
      identifierByte: 0xD6
    )
    let otherBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: otherReceipt.canonicalReceipt
    )

    XCTAssertFalse(permit.claimPendingProtectedExecution(binding: otherBinding))
    XCTAssertTrue(permit.claimPendingProtectedExecution(binding: verified.pending.binding))
    XCTAssertFalse(permit.claimPendingProtectedExecution(binding: verified.pending.binding))
    XCTAssertFalse(permit is any Encodable)
  }

  func testDeletedGlobalStateCannotResetRollbackProtectionForAnotherNonce() throws {
    let fixture = try makeFixture()
    _ = try makeCoordinator(fixture).redeem(
      receiptData: fixture.receipt.canonicalReceipt,
      expectedChallenge: fixture.challenge,
      session: ReviewSession()
    )
    let callsBeforeLoss = fixture.lease.verifyCalls
    let pendingWritesBeforeLoss = fixture.lease.pendingWrites
    let globalReplacesBeforeLoss = fixture.lease.globalReplaceWrites
    let otherChallenge = try challengeWithNonce(
      fixture.challenge,
      nonce: Data(repeating: 0xA7, count: 32)
    )
    let otherReceipt = try makeReceipt(
      challenge: otherChallenge,
      key: fixture.key,
      configuration: fixture.configuration,
      identifierByte: 0xD7
    )

    // A nil global record is not a safe first-use signal: it can result from
    // loss after another nonce has already been redeemed.
    fixture.lease.global = nil
    let restarted = ReceiptRedemptionCoordinator(
      configuration: fixture.configuration,
      fixedSlot: fixture.slot,
      clock: ReviewClock([now, now]).next
    )
    assertRedemptionError(.globalStateRequired) {
      _ = try restarted.redeem(
        receiptData: otherReceipt.canonicalReceipt,
        expectedChallenge: otherChallenge,
        session: ReviewSession()
      )
    }

    XCTAssertEqual(fixture.lease.verifyCalls, callsBeforeLoss)
    XCTAssertEqual(fixture.lease.pendingWrites, pendingWritesBeforeLoss)
    XCTAssertEqual(fixture.lease.globalReplaceWrites, globalReplacesBeforeLoss)
    XCTAssertNil(fixture.lease.states[otherChallenge.replayKey])
  }

  func testReceiptIdentifierRequiresExactAsciiBytesAtReceiptAndStateBoundaries() throws {
    let fixture = try makeFixture()
    let newlineSuffixedIdentifier =
      "approval-receipt:" + base64URL(Data(repeating: 0xD8, count: 32)) + "\n"

    // The exact byte boundary keeps the native identifier contract aligned
    // with Python's full-match rule, including terminal-line-suffix rejection.
    XCTAssertThrowsError(
      try ApprovalReceiptFactory.makeUnsigned(
        challenge: fixture.challenge,
        review: DeterministicApprovalReview.make(for: fixture.challenge),
        configuration: fixture.configuration,
        now: now,
        receiptIdentifier: newlineSuffixedIdentifier
      )
    )

    let binding = try ReceiptRedemptionSourceBindingV1(
      configuration: fixture.configuration,
      expectedChallenge: fixture.challenge,
      canonicalReceipt: fixture.receipt.canonicalReceipt
    )
    let pending = try ReceiptRedemptionStateV1.pending(
      ReceiptRedemptionPendingV1(
        binding: binding,
        reservedAtText: "2030-01-01T00:00:30.000000Z"
      )
    )
    guard
      case .object(var stateFields) = try CanonicalJSON.parseCanonical(pending.canonicalBytes()),
      case .object(var bindingFields) = stateFields["binding"]
    else {
      return XCTFail("expected canonical pending state")
    }
    bindingFields["receipt_id"] = .string(newlineSuffixedIdentifier)
    stateFields["binding"] = .object(bindingFields)
    XCTAssertThrowsError(
      try ReceiptRedemptionStateV1.decode(CanonicalJSON.encode(.object(stateFields)))
    )
  }

  func testAnchoredCoreParsersRejectTerminalLineSuffixes() throws {
    let receiptIdentifier = "approval-receipt:" + base64URL(Data(repeating: 0xD9, count: 32))
    for suffix in ["\n", "\r\n", "\u{2028}"] {
      XCTAssertFalse(isExactApprovalReceiptIdentifier(receiptIdentifier + suffix))
      XCTAssertThrowsError(
        try ClosedTimestamp.parse("2030-01-01T00:00:30.000000Z" + suffix)
      )
      XCTAssertThrowsError(
        try ExactMoney.decode(
          .object(["minor_units": .integer(1), "currency": .string("USD" + suffix)])
        )
      )
      XCTAssertThrowsError(
        try ExactEgressTarget.decode(
          .object(["host": .string("api.openai.com" + suffix), "purpose": .string("fixed")])
        )
      )
    }
  }

  private func makeFixture() throws -> ReviewFixture {
    let configuration = try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:fixture",
      machServiceName: "invalid.fixture.service",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("redemption-ultra".utf8),
      keyStorage: .keychainP256
    )
    let challenge = try loadChallenge()
    let key = P256.Signing.PrivateKey()
    let receipt = try makeReceipt(
      challenge: challenge,
      key: key,
      configuration: configuration,
      identifierByte: 0xD3
    )
    let verifier = try PinnedReceiptVerifier(
      configuration: configuration,
      trustedPublicKeyX963: key.publicKey.x963Representation
    )
    let lease = ReviewLease(verifier: verifier, publicKey: key.publicKey.x963Representation)
    lease.global = try ReceiptRedemptionGlobalStateV1.ready(
      ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: now)
    ).canonicalBytes()
    return ReviewFixture(
      configuration: configuration,
      challenge: challenge,
      key: key,
      receipt: receipt,
      lease: lease,
      slot: ReviewSlot(lease: lease),
      clock: ReviewClock([
        now,
        now.addingTimeInterval(1),
        now.addingTimeInterval(2),
        now.addingTimeInterval(3),
        now.addingTimeInterval(4),
        now.addingTimeInterval(5),
        now.addingTimeInterval(6),
      ])
    )
  }

  private func makeCoordinator(_ fixture: ReviewFixture) -> ReceiptRedemptionCoordinator {
    ReceiptRedemptionCoordinator(
      configuration: fixture.configuration,
      fixedSlot: fixture.slot,
      clock: fixture.clock.next
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
    identifierByte: UInt8
  ) throws -> SignedApprovalReceipt {
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + base64URL(Data(repeating: identifierByte, count: 32))
    )
    return try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: key.signature(for: unsigned.signingPayload.domainSeparatedBytes).derRepresentation
    )
  }

  private func challengeWithNonce(
    _ original: ApprovalChallenge,
    nonce: Data
  ) throws -> ApprovalChallenge {
    guard case .object(var fields) = try CanonicalJSON.parseCanonical(original.canonicalBytes)
    else {
      throw ApprovalHostError.malformedChallenge
    }
    fields["nonce"] = .string(base64URL(nonce))
    return try ApprovalChallenge.decode(try CanonicalJSON.encode(.object(fields)), now: now)
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

private struct ReviewFixture {
  let configuration: TrustedHostConfiguration
  let challenge: ApprovalChallenge
  let key: P256.Signing.PrivateKey
  let receipt: SignedApprovalReceipt
  let lease: ReviewLease
  let slot: ReviewSlot
  let clock: ReviewClock
}

private final class ReviewSession: ReceiptRedemptionSession, @unchecked Sendable {
  private(set) var invalidations = 0

  func invalidate() { invalidations += 1 }
}

private enum ReviewStoreError: Error {
  case rejected
}

private final class ReviewLease: ReceiptRedemptionLease, @unchecked Sendable {
  let verifier: PinnedReceiptVerifier
  let publicKey: Data
  var states: [String: Data] = [:]
  var global: Data?
  var events: [String] = []
  var verifyCalls = 0
  var pendingWrites = 0
  var replaceWrites = 0
  var globalReplaceWrites = 0
  var hideGlobalReadbackAfterIntent = false
  var erasePendingAfterWrite = false
  var afterVerification: ((Int) -> Void)?

  init(verifier: PinnedReceiptVerifier, publicKey: Data) {
    self.verifier = verifier
    self.publicKey = publicKey
  }

  func verifyCurrentReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session _: any ReceiptRedemptionSession,
    now: Date
  ) throws -> ReceiptRedemptionCurrentReceipt {
    events.append("verify")
    verifyCalls += 1
    defer { afterVerification?(verifyCalls) }
    return try ReceiptRedemptionCurrentReceipt(
      receipt: verifier.verify(receiptData, expectedChallenge: expectedChallenge, now: now),
      activePublicKeyX963: publicKey, currentVerificationObservedAt: now)
  }

  func loadRedemptionState(nonceDigest: String) throws -> Data? {
    events.append("state.load")
    return states[nonceDigest]
  }

  func loadGlobalRedemptionState() throws -> Data? {
    events.append("global.load")
    if hideGlobalReadbackAfterIntent, globalReplaceWrites > 0 { return nil }
    return global
  }

  func writePendingRedemption(_ canonicalState: Data) throws {
    events.append("pending.write")
    let state = try ReceiptRedemptionStateV1.decode(canonicalState)
    guard state.isPending, states[state.binding.challengeNonceDigest] == nil else {
      throw ReviewStoreError.rejected
    }
    states[state.binding.challengeNonceDigest] = canonicalState
    pendingWrites += 1
    if erasePendingAfterWrite { states[state.binding.challengeNonceDigest] = nil }
  }

  func replacePendingRedemption(_ expectedPending: Data, with canonicalVerified: Data) throws {
    events.append("pending.replace")
    let pending = try ReceiptRedemptionStateV1.decode(expectedPending)
    let verified = try ReceiptRedemptionStateV1.decode(canonicalVerified)
    guard
      pending.isPending,
      !verified.isPending,
      pending.binding == verified.binding,
      states[pending.binding.challengeNonceDigest] == expectedPending
    else { throw ReviewStoreError.rejected }
    states[pending.binding.challengeNonceDigest] = canonicalVerified
    replaceWrites += 1
  }

  func replaceGlobalRedemptionState(_ expected: Data, with canonicalState: Data) throws {
    events.append("global.replace")
    _ = try ReceiptRedemptionGlobalStateV1.decode(canonicalState)
    guard global == expected else { throw ReviewStoreError.rejected }
    global = canonicalState
    globalReplaceWrites += 1
  }
}

private final class ReviewSlot: ReceiptRedemptionFixedSlotCoordinator, @unchecked Sendable {
  let lease: ReviewLease
  private var active = false
  private(set) var entries = 0

  init(lease: ReviewLease) {
    self.lease = lease
  }

  func withRedemptionLease<T>(
    _ operation: (any ReceiptRedemptionLease) throws -> T
  ) throws -> T {
    guard !active else { throw ReviewStoreError.rejected }
    active = true
    entries += 1
    defer { active = false }
    return try operation(lease)
  }
}

private final class ReviewClock: @unchecked Sendable {
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

private func base64URL(_ data: Data) -> String {
  data.base64EncodedString()
    .replacingOccurrences(of: "+", with: "-")
    .replacingOccurrences(of: "/", with: "_")
    .replacingOccurrences(of: "=", with: "")
}

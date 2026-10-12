import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore

/// Independent fake-only review coverage for the post-signing bridge handoff.
/// These tests never construct a protected store, XPC peer, helper process, or
/// authentication UI.
final class InstalledNativeApprovalBridgeUltraReviewTests: XCTestCase {
  func testCancellationDuringBlockingConsumerSuppressesDeferredCandidate() throws {
    let now = Date(timeIntervalSince1970: 1_893_456_010)
    let challengeData = try challengeFixture()
    let metadata = try metadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let candidate = try candidate(
      challengeData: challengeData, metadata: metadata, now: now)
    let clock = ReviewClock(now)
    let reviewer = ReviewReviewer()
    let authenticator = ReviewAuthenticator()
    let signer = ReviewSigner()
    let session = ReviewSession()
    let consumer = BlockingCandidateConsumer(candidate: candidate)
    let result = ReviewResultBox<NativeApprovalBridgeCandidateV1>()
    let interactionFinished = XCTestExpectation(description: "interaction finished")
    let engine = ApprovalHostEngine(
      configuration: configuration,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: ReviewIdentifierGenerator(),
      scheduler: ReviewScheduler(),
      clock: { clock.now() }
    )

    let handle = engine.requestNativeApprovalBridge(
      challengeData: challengeData,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: { interactionFinished.fulfill() }
    )
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    XCTAssertTrue(signer.hasPendingSignature)

    DispatchQueue.global().async { signer.completeWithValidShapeSignature() }
    XCTAssertEqual(consumer.entered.wait(timeout: .now() + 1), .success)

    handle.cancel()
    XCTAssertEqual(session.invalidationCount, 0)
    XCTAssertNil(result.value)

    consumer.release.signal()
    XCTAssertEqual(consumer.returned.wait(timeout: .now() + 1), .success)
    wait(for: [interactionFinished], timeout: 1)

    XCTAssertEqual(result.failure, .authenticationCancelled)
    XCTAssertNil(result.success)
    XCTAssertEqual(session.invalidationCount, 1)
  }

  func testFinalPostConsumerExpirySuppressesCandidate() throws {
    let now = Date(timeIntervalSince1970: 1_893_456_010)
    let challengeData = try challengeFixture()
    let metadata = try metadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let candidate = try candidate(
      challengeData: challengeData, metadata: metadata, now: now)
    let challenge = try ApprovalChallenge.decode(
      challengeData, now: now, limits: configuration.limits)
    let clock = ReviewClock(now)
    let reviewer = ReviewReviewer()
    let authenticator = ReviewAuthenticator()
    let signer = ReviewSigner()
    let session = ReviewSession()
    let consumer = ExpiringCandidateConsumer(candidate: candidate) {
      clock.set(challenge.expiresAt)
    }
    let result = ReviewResultBox<NativeApprovalBridgeCandidateV1>()
    let interactionFinished = XCTestExpectation(description: "interaction finished")
    let engine = ApprovalHostEngine(
      configuration: configuration,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: ReviewIdentifierGenerator(),
      scheduler: ReviewScheduler(),
      clock: { clock.now() }
    )

    _ = engine.requestNativeApprovalBridge(
      challengeData: challengeData,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: { interactionFinished.fulfill() }
    )
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    signer.completeWithValidShapeSignature()
    wait(for: [interactionFinished], timeout: 1)

    XCTAssertEqual(result.failure, .challengeExpired)
    XCTAssertNil(result.success)
    XCTAssertEqual(session.invalidationCount, 1)
  }

  func testFramingFailurePoisonsDecoderBeforeAnySecondFrameCanBeConsumed() throws {
    let decoder = NativeApprovalBridgeFrameDecoder(role: .candidate)
    try decoder.append(Data([0, 0, 0, 1, 0x61]))
    XCTAssertThrowsError(try decoder.append(Data([0x62])))
    XCTAssertThrowsError(try decoder.append(Data()))
    XCTAssertThrowsError(try decoder.finishAtEOF())
    XCTAssertThrowsError(try decoder.consume())
    XCTAssertEqual(String(reflecting: decoder), "NativeApprovalBridgeFrameDecoder(redacted)")
  }

  func testTerminalLeaseCloseDoesNotWaitOnRequestContinuationCallback() {
    let leaseReady = DispatchSemaphore(value: 0)
    let continuationEntered = DispatchSemaphore(value: 0)
    let releaseContinuation = DispatchSemaphore(value: 0)
    let ownerReturned = DispatchSemaphore(value: 0)
    let closeReturned = DispatchSemaphore(value: 0)
    let box = ReviewLeaseBox()
    let session = ReviewSession()

    DispatchQueue.global(qos: .userInitiated).async {
      let lease = ApprovalPostSigningSessionLease(
        session: session,
        requestMayContinue: {
          continuationEntered.signal()
          _ = releaseContinuation.wait(timeout: .now() + 2)
          return true
        }
      )
      box.store(lease)
      leaseReady.signal()
      _ = try? lease.withAuthenticatedSession { _ in () }
      ownerReturned.signal()
    }

    XCTAssertEqual(leaseReady.wait(timeout: .now() + 1), .success)
    let lease = box.load()
    XCTAssertEqual(continuationEntered.wait(timeout: .now() + 1), .success)

    DispatchQueue.global(qos: .userInitiated).async {
      lease.close()
      closeReturned.signal()
    }
    let closeResult = closeReturned.wait(timeout: .now() + 1)
    releaseContinuation.signal()
    XCTAssertEqual(ownerReturned.wait(timeout: .now() + 1), .success)
    XCTAssertEqual(closeResult, .success)
    XCTAssertFalse(lease.shouldContinue())
  }

  private func challengeFixture() throws -> Data {
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: "openai-pause-challenge-v1", withExtension: "json", subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
  }

  private func metadata() throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "key:fixture",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client, bundleIdentifier: "com.aptsonic.adhelm.client", teamIdentifier: "ABCDE12345"),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host, bundleIdentifier: "com.aptsonic.adhelm.host", teamIdentifier: "ABCDE12345"),
      signingKeyApplicationTag: Data("m34-ultra".utf8),
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1"
    )
  }

  private func candidate(
    challengeData: Data,
    metadata: ApprovalHostBuildMetadataV1,
    now: Date
  ) throws -> NativeApprovalBridgeCandidateV1 {
    let configuration = try metadata.makeTrustedHostConfiguration()
    let challenge = try ApprovalChallenge.decode(
      challengeData, now: now, limits: configuration.limits)
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    let request = try NativeApprovalBridgeRequestV1(
      requestID: Data(repeating: 0x41, count: 32),
      sourceContextDigest: String(repeating: "a", count: 64),
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: challenge.canonicalBytes
    )
    let privateKey = P256.Signing.PrivateKey()
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + String(repeating: "C", count: 43)
    )
    let signature = try privateKey.signature(
      for: unsigned.signingPayload.domainSeparatedBytes).derRepresentation
    let receipt = try ApprovalReceiptFactory.finalize(unsigned, signature: signature)
    return try makeNativeApprovalBridgeFixtureCandidate(
      request: request,
      canonicalReceipt: receipt.canonicalReceipt,
      activePublicKeyX963: privateKey.publicKey.x963Representation,
      nativeObservedAtText: "2030-01-01T00:00:10.000000Z",
      metadata: metadata,
      exchangeStartedAt: now,
      currentNow: now
    )
  }
}

private final class ReviewClock: @unchecked Sendable {
  private let lock = NSLock()
  private var value: Date

  init(_ value: Date) { self.value = value }

  func now() -> Date {
    lock.lock()
    defer { lock.unlock() }
    return value
  }

  func set(_ next: Date) {
    lock.lock()
    value = next
    lock.unlock()
  }
}

private final class ReviewResultBox<Value>: @unchecked Sendable {
  private let lock = NSLock()
  private var stored: Result<Value, ApprovalHostError>?

  func store(_ result: Result<Value, ApprovalHostError>) {
    lock.lock()
    stored = result
    lock.unlock()
  }

  var value: Result<Value, ApprovalHostError>? {
    lock.lock()
    defer { lock.unlock() }
    return stored
  }

  var success: Value? {
    guard case let .success(value)? = self.value else { return nil }
    return value
  }

  var failure: ApprovalHostError? {
    guard case let .failure(error)? = self.value else { return nil }
    return error
  }
}

private final class ReviewLeaseBox: @unchecked Sendable {
  private let lock = NSLock()
  private var lease: ApprovalPostSigningSessionLease?

  func store(_ value: ApprovalPostSigningSessionLease) {
    lock.lock()
    lease = value
    lock.unlock()
  }

  func load() -> ApprovalPostSigningSessionLease {
    lock.lock()
    defer { lock.unlock() }
    return lease!
  }
}

private final class ReviewReviewer: ApprovalReviewer {
  private var completion: ((ApprovalReviewDecision) -> Void)?

  func present(
    review _: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond(_ decision: ApprovalReviewDecision) { completion?(decision) }
}

private final class ReviewAuthenticator: FreshAuthenticator {
  private var completion: ((ApprovalAuthenticationResult) -> Void)?

  func authenticate(
    reason _: String,
    completion: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond(_ result: ApprovalAuthenticationResult) { completion?(result) }
}

private final class ReviewSigner: ReceiptSigner, @unchecked Sendable {
  private let lock = NSLock()
  private var payload: ReceiptSigningPayload?
  private var permit: (any ReceiptSigningPermit)?
  private var completion: ((Result<Data, ApprovalHostError>) -> Void)?

  func sign(
    _ payload: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    lock.lock()
    self.payload = payload
    self.permit = permit
    self.completion = completion
    lock.unlock()
    return NoopApprovalCancellation()
  }

  var hasPendingSignature: Bool {
    lock.lock()
    defer { lock.unlock() }
    return payload != nil && permit != nil && completion != nil
  }

  func completeWithValidShapeSignature() {
    let localPayload: ReceiptSigningPayload?
    let localPermit: (any ReceiptSigningPermit)?
    let localCompletion: ((Result<Data, ApprovalHostError>) -> Void)?
    lock.lock()
    localPayload = payload
    localPermit = permit
    localCompletion = completion
    lock.unlock()
    guard let localPayload, let localPermit, let localCompletion, localPermit.claimSigning() else {
      return
    }
    _ = localPayload
    localCompletion(.success(Data(repeating: 0x30, count: 64)))
  }
}

private final class ReviewSession: AuthenticatedApprovalSession, @unchecked Sendable {
  private let lock = NSLock()
  private var count = 0

  func invalidate() {
    lock.lock()
    count += 1
    lock.unlock()
  }

  var invalidationCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return count
  }
}

private final class ReviewScheduler: ApprovalDeadlineScheduler {
  func schedule(
    after _: TimeInterval,
    action _: @escaping () -> Void
  ) -> any ApprovalCancellation { NoopApprovalCancellation() }
}

private final class ReviewIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:" + String(repeating: "D", count: 43)
  }
}

private final class BlockingCandidateConsumer: ApprovalPostSigningConsumer, @unchecked Sendable {
  let entered = DispatchSemaphore(value: 0)
  let release = DispatchSemaphore(value: 0)
  let returned = DispatchSemaphore(value: 0)
  private let candidate: NativeApprovalBridgeCandidateV1

  init(candidate: NativeApprovalBridgeCandidateV1) { self.candidate = candidate }

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeApprovalBridgeCandidateV1, ApprovalHostError> {
    do {
      try sessionLease.withAuthenticatedSession { _ in () }
    } catch {
      return .failure(.authenticationCancelled)
    }
    entered.signal()
    _ = release.wait(timeout: .now() + 1)
    returned.signal()
    return .success(candidate)
  }
}

private final class ExpiringCandidateConsumer: ApprovalPostSigningConsumer {
  private let candidate: NativeApprovalBridgeCandidateV1
  private let advanceClock: () -> Void

  init(candidate: NativeApprovalBridgeCandidateV1, advanceClock: @escaping () -> Void) {
    self.candidate = candidate
    self.advanceClock = advanceClock
  }

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeApprovalBridgeCandidateV1, ApprovalHostError> {
    do {
      try sessionLease.withAuthenticatedSession { _ in () }
    } catch {
      return .failure(.authenticationCancelled)
    }
    advanceClock()
    return .success(candidate)
  }
}

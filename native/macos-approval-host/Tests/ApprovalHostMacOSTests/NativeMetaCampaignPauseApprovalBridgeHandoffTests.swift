import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only engine and consumer coverage for the package-only Meta V2 route.
/// Injected consumers model the package boundary only; they do not establish
/// OS provenance, metadata attestation, or any XPC/provider authority.
final class NativeMetaCampaignPauseApprovalBridgeHandoffTests: XCTestCase {
  func testSessionBoundV2ConsumerProducesCandidateAfterDurableRedemption() throws {
    let fixture = try MetaV2ProducerFixture()
    let redemptionFactory = MetaV2HandoffRedemptionFactory(fixture: fixture)
    let consumerFactory = try NativeMetaCampaignPauseApprovalBridgePostSigningConsumerFactory(
      metadata: fixture.metadata,
      redemptionFactory: redemptionFactory
    )
    let consumer = try consumerFactory.makeConsumer(
      request: fixture.request,
      exchangeStartedAt: fixture.now
    )
    let reviewer = MetaV2HandoffReviewer()
    let authenticator = MetaV2HandoffAuthenticator()
    let signer = MetaV2HandoffKeySigner(key: fixture.key)
    let session = MetaV2HandoffSession()
    let result = MetaV2HandoffResultBox<NativeMetaCampaignPauseApprovalBridgeCandidateV2>()
    var interactionFinished = 0
    let engine = makeEngine(
      fixture: fixture,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer
    )

    _ = engine.requestNativeMetaCampaignPauseApprovalBridge(
      request: fixture.request,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: { interactionFinished += 1 }
    )
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    signer.respond()

    let candidate = try XCTUnwrap(result.success)
    XCTAssertEqual(candidate.receiptKind, .metaCampaignPausePlan)
    XCTAssertEqual(candidate.requestDigest, fixture.request.digest)
    XCTAssertEqual(redemptionFactory.makeCalls, 1)
    XCTAssertEqual(fixture.lease.events.filter { $0 == "verify" }.count, 2)
    XCTAssertFalse(try XCTUnwrap(fixture.lease.nonce).isPending)
    XCTAssertFalse(try XCTUnwrap(fixture.lease.global).isIntentPending)
    XCTAssertEqual(session.invalidations, 1)
    XCTAssertEqual(interactionFinished, 1)
  }

  func testEngineBindsV2ProtocolDigestBeforeReviewOrAuthentication() throws {
    let fixture = try MetaV2ProducerFixture()
    let mismatchedRequest = try NativeMetaCampaignPauseApprovalBridgeRequestV2(
      requestID: fixture.request.requestID,
      sourceContextDigest: fixture.request.sourceContextDigest,
      metadataDigest: fixture.request.metadataDigest,
      protocolConfigurationDigest: String(repeating: "0", count: 64),
      canonicalChallenge: fixture.request.canonicalChallenge
    )
    let reviewer = MetaV2HandoffReviewer()
    let authenticator = MetaV2HandoffAuthenticator()
    let signer = MetaV2HandoffKeySigner(key: fixture.key)
    let consumer = MetaV2NeverConsumer()
    let result = MetaV2HandoffResultBox<NativeMetaCampaignPauseApprovalBridgeCandidateV2>()
    var interactionFinished = 0
    let engine = makeEngine(
      fixture: fixture,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer
    )

    _ = engine.requestNativeMetaCampaignPauseApprovalBridge(
      request: mismatchedRequest,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: { interactionFinished += 1 }
    )

    XCTAssertEqual(result.failure, .malformedChallenge)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertEqual(signer.calls, 0)
    XCTAssertEqual(consumer.calls, 0)
    XCTAssertEqual(interactionFinished, 1)
  }

  func testV1PostSigningRouteStillRefusesMetaBeforeReview() throws {
    let fixture = try MetaV2ProducerFixture()
    let reviewer = MetaV2HandoffReviewer()
    let authenticator = MetaV2HandoffAuthenticator()
    let signer = MetaV2HandoffKeySigner(key: fixture.key)
    let consumer = MetaV1NeverConsumer()
    let result = MetaV2HandoffResultBox<NativeApprovalBridgeCandidateV1>()
    let engine = makeEngine(
      fixture: fixture,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer
    )

    _ = engine.requestNativeApprovalBridge(
      challengeData: fixture.challenge.canonicalBytes,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: {}
    )

    XCTAssertEqual(result.failure, .malformedChallenge)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertEqual(signer.calls, 0)
    XCTAssertEqual(consumer.calls, 0)
  }

  func testCancellationDuringV2ConsumerSuppressesCandidateUntilSessionHandoffReturns() throws {
    let candidateFixture = try MetaV2ProducerFixture()
    let candidate = try candidateFixture.produce(session: BridgeProducerTestSession())
    let fixture = try MetaV2ProducerFixture()
    let reviewer = MetaV2HandoffReviewer()
    let authenticator = MetaV2HandoffAuthenticator()
    let signer = MetaV2HandoffPendingSigner()
    let session = MetaV2HandoffSession()
    let consumer = MetaV2BlockingConsumer(candidate: candidate)
    let result = MetaV2HandoffResultBox<NativeMetaCampaignPauseApprovalBridgeCandidateV2>()
    let finished = expectation(description: "interaction finished")
    let engine = makeEngine(
      fixture: fixture,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer
    )

    let handle = engine.requestNativeMetaCampaignPauseApprovalBridge(
      request: fixture.request,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: { finished.fulfill() }
    )
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    DispatchQueue.global().async { signer.respondWithValidShapeSignature() }
    XCTAssertEqual(consumer.entered.wait(timeout: .now() + 1), .success)

    handle.cancel()
    XCTAssertNil(result.value)
    XCTAssertEqual(session.invalidations, 0)

    consumer.release.signal()
    XCTAssertEqual(consumer.returned.wait(timeout: .now() + 1), .success)
    wait(for: [finished], timeout: 1)

    XCTAssertNil(result.success)
    XCTAssertEqual(result.failure, .authenticationCancelled)
    XCTAssertEqual(session.invalidations, 1)
  }

  func testFinalPostConsumerExpirySuppressesV2Candidate() throws {
    let candidateFixture = try MetaV2ProducerFixture()
    let candidate = try candidateFixture.produce(session: BridgeProducerTestSession())
    let fixture = try MetaV2ProducerFixture()
    let clock = MetaV2HandoffClock(fixture.now)
    let reviewer = MetaV2HandoffReviewer()
    let authenticator = MetaV2HandoffAuthenticator()
    let signer = MetaV2HandoffPendingSigner()
    let session = MetaV2HandoffSession()
    let consumer = MetaV2ExpiringConsumer(candidate: candidate) {
      clock.set(fixture.challenge.expiresAt)
    }
    let result = MetaV2HandoffResultBox<NativeMetaCampaignPauseApprovalBridgeCandidateV2>()
    var interactionFinished = 0
    let engine = makeEngine(
      fixture: fixture,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      clock: { clock.now() }
    )

    _ = engine.requestNativeMetaCampaignPauseApprovalBridge(
      request: fixture.request,
      postSigningConsumer: consumer,
      completion: { result.store($0) },
      interactionFinished: { interactionFinished += 1 }
    )
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    signer.respondWithValidShapeSignature()

    XCTAssertNil(result.success)
    XCTAssertEqual(result.failure, .challengeExpired)
    XCTAssertEqual(session.invalidations, 1)
    XCTAssertEqual(interactionFinished, 1)
  }

  private func makeEngine(
    fixture: MetaV2ProducerFixture,
    reviewer: MetaV2HandoffReviewer,
    authenticator: MetaV2HandoffAuthenticator,
    signer: any ReceiptSigner,
    clock: (() -> Date)? = nil
  ) -> ApprovalHostEngine {
    ApprovalHostEngine(
      configuration: fixture.configuration,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: MetaV2HandoffIdentifierGenerator(),
      scheduler: MetaV2HandoffScheduler(),
      clock: clock ?? { fixture.now }
    )
  }
}

private final class MetaV2HandoffRedemptionFactory:
  NativeMetaCampaignPauseApprovalBridgeRedemptionFactory
{
  private let fixture: MetaV2ProducerFixture
  private(set) var makeCalls = 0

  init(fixture: MetaV2ProducerFixture) {
    self.fixture = fixture
  }

  func makeCoordinator(
    configuration: TrustedHostConfiguration,
    session _: any AuthenticatedApprovalSession,
    shouldContinue _: @escaping () -> Bool
  ) throws -> ReceiptRedemptionCoordinator {
    makeCalls += 1
    return ReceiptRedemptionCoordinator(
      configuration: configuration,
      fixedSlot: BridgeProducerTestSlot(lease: fixture.lease),
      clock: { self.fixture.now }
    )
  }
}

private final class MetaV2HandoffReviewer: ApprovalReviewer {
  private var completion: ((ApprovalReviewDecision) -> Void)?
  private(set) var calls = 0

  func present(
    review _: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond(_ value: ApprovalReviewDecision) {
    completion?(value)
  }
}

private final class MetaV2HandoffAuthenticator: FreshAuthenticator {
  private var completion: ((ApprovalAuthenticationResult) -> Void)?
  private(set) var calls = 0

  func authenticate(
    reason _: String,
    completion: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond(_ value: ApprovalAuthenticationResult) {
    completion?(value)
  }
}

private final class MetaV2HandoffKeySigner: ReceiptSigner {
  private let key: P256.Signing.PrivateKey
  private var payload: ReceiptSigningPayload?
  private var permit: (any ReceiptSigningPermit)?
  private var completion: ((Result<Data, ApprovalHostError>) -> Void)?
  private(set) var calls = 0

  init(key: P256.Signing.PrivateKey) {
    self.key = key
  }

  func sign(
    _ payload: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    self.payload = payload
    self.permit = permit
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond() {
    guard let payload, let permit, let completion, permit.claimSigning() else { return }
    do {
      completion(
        .success(
          try key.signature(for: payload.domainSeparatedBytes).derRepresentation
        ))
    } catch {
      completion(.failure(.signingFailed))
    }
  }
}

private final class MetaV2HandoffPendingSigner: ReceiptSigner, @unchecked Sendable {
  private let lock = NSLock()
  private var permit: (any ReceiptSigningPermit)?
  private var completion: ((Result<Data, ApprovalHostError>) -> Void)?

  func sign(
    _ _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    lock.lock()
    self.permit = permit
    self.completion = completion
    lock.unlock()
    return NoopApprovalCancellation()
  }

  func respondWithValidShapeSignature() {
    let localPermit: (any ReceiptSigningPermit)?
    let localCompletion: ((Result<Data, ApprovalHostError>) -> Void)?
    lock.lock()
    localPermit = permit
    localCompletion = completion
    lock.unlock()
    guard let localPermit, let localCompletion, localPermit.claimSigning() else { return }
    localCompletion(.success(Data(repeating: 0x30, count: 64)))
  }
}

private final class MetaV2HandoffSession:
  AuthenticatedApprovalSession,
  ReceiptRedemptionSession,
  @unchecked Sendable
{
  private let lock = NSLock()
  private var count = 0

  func invalidate() {
    lock.lock()
    count += 1
    lock.unlock()
  }

  var invalidations: Int {
    lock.lock()
    defer { lock.unlock() }
    return count
  }
}

private final class MetaV2HandoffScheduler: ApprovalDeadlineScheduler {
  func schedule(
    after _: TimeInterval,
    action _: @escaping () -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class MetaV2HandoffIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:" + String(repeating: "H", count: 43)
  }
}

private final class MetaV2HandoffResultBox<Value>: @unchecked Sendable {
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
    guard case .success(let value)? = self.value else { return nil }
    return value
  }

  var failure: ApprovalHostError? {
    guard case .failure(let error)? = self.value else { return nil }
    return error
  }
}

private final class MetaV2NeverConsumer:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumer
{
  private(set) var calls = 0

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease _: ApprovalPostSigningSessionLease
  ) -> Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError> {
    calls += 1
    return .failure(.signingFailed)
  }
}

private final class MetaV1NeverConsumer: ApprovalPostSigningConsumer {
  private(set) var calls = 0

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease _: ApprovalPostSigningSessionLease
  ) -> Result<NativeApprovalBridgeCandidateV1, ApprovalHostError> {
    calls += 1
    return .failure(.signingFailed)
  }
}

private final class MetaV2BlockingConsumer:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumer,
  @unchecked Sendable
{
  let entered = DispatchSemaphore(value: 0)
  let release = DispatchSemaphore(value: 0)
  let returned = DispatchSemaphore(value: 0)
  private let candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2

  init(candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2) {
    self.candidate = candidate
  }

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError> {
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

private final class MetaV2ExpiringConsumer:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumer
{
  private let candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2
  private let advanceClock: () -> Void

  init(
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    advanceClock: @escaping () -> Void
  ) {
    self.candidate = candidate
    self.advanceClock = advanceClock
  }

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError> {
    do {
      try sessionLease.withAuthenticatedSession { _ in () }
    } catch {
      return .failure(.authenticationCancelled)
    }
    advanceClock()
    return .success(candidate)
  }
}

private final class MetaV2HandoffClock: @unchecked Sendable {
  private let lock = NSLock()
  private var value: Date

  init(_ value: Date) {
    self.value = value
  }

  func now() -> Date {
    lock.lock()
    defer { lock.unlock() }
    return value
  }

  func set(_ value: Date) {
    lock.lock()
    self.value = value
    lock.unlock()
  }
}

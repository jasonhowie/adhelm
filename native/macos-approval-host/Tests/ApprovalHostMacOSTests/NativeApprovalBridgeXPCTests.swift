import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// These tests use only in-memory approval, M33 fixture, and transport fakes.
/// They never create XPC, Keychain, LocalAuthentication, or a helper process.
final class NativeApprovalBridgeXPCTests: XCTestCase {
  func testClientAcceptsOnlyExactRequestBoundCandidate() throws {
    let fixture = try BridgeProducerTestFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let transport = BridgeTransport()
    let client = AuthenticatedNativeApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { transport },
      scheduler: BridgeScheduler(),
      clock: { fixture.now }
    )
    var response: NativeApprovalBridgeXPCResponse?

    client.request(fixture.request) { response = $0 }
    XCTAssertEqual(transport.sendCount, 1)
    XCTAssertEqual(transport.requestBytes, fixture.request.canonicalBytes)
    transport.reply(.response(candidate: candidate.canonicalBytes, code: nil, message: nil))
    XCTAssertEqual(response, .candidate(candidate))
    XCTAssertTrue(transport.invalidated)

    let otherFixture = try BridgeProducerTestFixture(
      challengeName: "ga4-key-event-create-challenge-v1"
    )
    let otherCandidate = try otherFixture.produce(session: BridgeProducerTestSession())
    let rejectedTransport = BridgeTransport()
    let rejectedClient = AuthenticatedNativeApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { rejectedTransport },
      scheduler: BridgeScheduler(),
      clock: { fixture.now }
    )
    var rejected: NativeApprovalBridgeXPCResponse?
    rejectedClient.request(fixture.request) { rejected = $0 }
    rejectedTransport.reply(
      .response(candidate: otherCandidate.canonicalBytes, code: nil, message: nil))
    XCTAssertEqual(rejected, .failure(ApprovalPublicFailure(.transportUnavailable)))
  }

  func testBridgeRequestPreflightRejectsMalformedWireBeforeFactoryOrInteraction() throws {
    let fixture = try BridgeProducerTestFixture()
    let factory = BridgeConsumerFactory(metadata: fixture.metadata, consumer: nil)
    let reviewer = BridgeReviewer()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      factory: factory
    )
    var response: (Data?, String?, String?)?

    service.requestNativeApprovalBridge(Data("{}".utf8)) { response = ($0, $1, $2) }

    XCTAssertEqual(factory.makeCalls, 0)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(response?.0, nil)
    XCTAssertEqual(response?.1, ApprovalHostError.malformedChallenge.code)
    XCTAssertEqual(response?.2, ApprovalHostError.malformedChallenge.publicMessage)
  }

  func testDedicatedRouteRequiresStaticConsumerFactoryBeforeInteraction() throws {
    let fixture = try BridgeProducerTestFixture()
    let reviewer = BridgeReviewer()
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    let service = ApprovalHostXPCService(
      engine: ApprovalHostEngine(
        configuration: try fixture.metadata.makeTrustedHostConfiguration(),
        reviewer: reviewer,
        authenticator: BridgeAuthenticator(),
        signer: BridgeSigner(),
        identifierGenerator: BridgeIdentifierGenerator(),
        scheduler: BridgeScheduler(),
        clock: { fixture.now }
      ),
      interactionGate: gate,
      clock: { fixture.now }
    )
    var response: (Data?, String?, String?)?

    service.requestNativeApprovalBridge(fixture.request.canonicalBytes) { response = ($0, $1, $2) }

    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(response?.0, nil)
    XCTAssertEqual(response?.1, ApprovalHostError.hostNotInstalled.code)
    XCTAssertEqual(response?.2, ApprovalHostError.hostNotInstalled.publicMessage)
  }

  func testRawReceiptRouteCannotReachNativeBridgeConsumer() throws {
    let fixture = try BridgeProducerTestFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let consumer = BridgeCandidateConsumer(candidate: candidate)
    let factory = BridgeConsumerFactory(metadata: fixture.metadata, consumer: consumer)
    let reviewer = BridgeReviewer()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      factory: factory
    )

    service.requestApproval(fixture.request.canonicalChallenge) { _, _, _ in }

    XCTAssertEqual(reviewer.calls, 1)
    XCTAssertEqual(factory.makeCalls, 0)
    XCTAssertEqual(consumer.calls, 0)
  }

  func testDedicatedRouteReturnsCandidateOnlyAfterPostSigningConsumer() throws {
    let fixture = try BridgeProducerTestFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let consumer = BridgeCandidateConsumer(candidate: candidate)
    let factory = BridgeConsumerFactory(metadata: fixture.metadata, consumer: consumer)
    let reviewer = BridgeReviewer()
    let authenticator = BridgeAuthenticator()
    let signer = BridgeSigner()
    let session = BridgeSession()
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    let engine = ApprovalHostEngine(
      configuration: try fixture.metadata.makeTrustedHostConfiguration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: BridgeIdentifierGenerator(),
      scheduler: BridgeScheduler(),
      clock: { fixture.now }
    )
    let service = ApprovalHostXPCService(
      engine: engine,
      interactionGate: gate,
      nativeBridgeConsumerFactory: factory,
      clock: { fixture.now }
    )
    var response: (Data?, String?, String?)?

    service.requestNativeApprovalBridge(fixture.request.canonicalBytes) { response = ($0, $1, $2) }
    XCTAssertEqual(factory.makeCalls, 1)
    XCTAssertEqual(reviewer.calls, 1)
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    signer.respond()

    XCTAssertEqual(consumer.calls, 1)
    XCTAssertEqual(response?.0, candidate.canonicalBytes)
    XCTAssertNil(response?.1)
    XCTAssertNil(response?.2)
    XCTAssertEqual(session.invalidations, 1)
    XCTAssertTrue(gate.isAvailable)
  }

  func testConfiguredPostSigningConsumerUsesOneEngineSessionForM33Consumption() throws {
    let fixture = try BridgeProducerTestFixture()
    let redemptionFactory = BridgeRedemptionFactory(fixture: fixture)
    let consumerFactory = try NativeApprovalBridgePostSigningConsumerFactory(
      metadata: fixture.metadata,
      redemptionFactory: redemptionFactory
    )
    let reviewer = BridgeReviewer()
    let authenticator = BridgeAuthenticator()
    let signer = BridgeCurrentKeySigner(key: fixture.key)
    let session = BridgeSession()
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    let engine = ApprovalHostEngine(
      configuration: try fixture.metadata.makeTrustedHostConfiguration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: BridgeIdentifierGenerator(),
      scheduler: BridgeScheduler(),
      clock: { fixture.now }
    )
    let service = ApprovalHostXPCService(
      engine: engine,
      interactionGate: gate,
      nativeBridgeConsumerFactory: consumerFactory,
      clock: { fixture.now }
    )
    var response: (Data?, String?, String?)?

    service.requestNativeApprovalBridge(fixture.request.canonicalBytes) { response = ($0, $1, $2) }
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    signer.respond()

    let bytes = try XCTUnwrap(response?.0)
    let candidate = try NativeApprovalBridgeCandidateV1.decode(
      bytes,
      expectedRequest: fixture.request,
      metadata: fixture.metadata,
      exchangeStartedAt: fixture.now,
      currentNow: fixture.now
    )
    XCTAssertEqual(candidate.requestDigest, fixture.request.digest)
    XCTAssertEqual(candidate.activePublicKeyX963, fixture.key.publicKey.x963Representation)
    XCTAssertEqual(
      candidate.activePublicKeyFingerprint,
      CanonicalJSON.sha256Hex(fixture.key.publicKey.x963Representation)
    )
    XCTAssertEqual(redemptionFactory.makeCalls, 1)
    XCTAssertEqual(fixture.lease.events.filter { $0 == "verify" }.count, 2)
    XCTAssertEqual(session.invalidations, 1)
  }

  func testSynchronousSigningCallbackCompletesSessionBoundHandoffSafely() throws {
    let fixture = try BridgeProducerTestFixture()
    let redemptionFactory = BridgeRedemptionFactory(fixture: fixture)
    let consumerFactory = try NativeApprovalBridgePostSigningConsumerFactory(
      metadata: fixture.metadata,
      redemptionFactory: redemptionFactory
    )
    let reviewer = BridgeReviewer()
    let authenticator = BridgeAuthenticator()
    let session = BridgeSession()
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    let engine = ApprovalHostEngine(
      configuration: try fixture.metadata.makeTrustedHostConfiguration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: SynchronousBridgeCurrentKeySigner(key: fixture.key),
      identifierGenerator: BridgeIdentifierGenerator(),
      scheduler: BridgeScheduler(),
      clock: { fixture.now }
    )
    let service = ApprovalHostXPCService(
      engine: engine,
      interactionGate: gate,
      nativeBridgeConsumerFactory: consumerFactory,
      clock: { fixture.now }
    )
    var response: (Data?, String?, String?)?

    service.requestNativeApprovalBridge(fixture.request.canonicalBytes) { response = ($0, $1, $2) }
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))

    let bytes = try XCTUnwrap(response?.0)
    let candidate = try NativeApprovalBridgeCandidateV1.decode(
      bytes,
      expectedRequest: fixture.request,
      metadata: fixture.metadata,
      exchangeStartedAt: fixture.now,
      currentNow: fixture.now
    )
    XCTAssertEqual(candidate.activePublicKeyX963, fixture.key.publicKey.x963Representation)
    XCTAssertEqual(redemptionFactory.makeCalls, 1)
    XCTAssertEqual(fixture.lease.events.filter { $0 == "verify" }.count, 2)
    XCTAssertEqual(session.invalidations, 1)
  }

  private func makeService(
    fixture: BridgeProducerTestFixture,
    reviewer: BridgeReviewer,
    factory: BridgeConsumerFactory
  ) -> ApprovalHostXPCService {
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    return ApprovalHostXPCService(
      engine: ApprovalHostEngine(
        configuration: try! fixture.metadata.makeTrustedHostConfiguration(),
        reviewer: reviewer,
        authenticator: BridgeAuthenticator(),
        signer: BridgeSigner(),
        identifierGenerator: BridgeIdentifierGenerator(),
        scheduler: BridgeScheduler(),
        clock: { fixture.now }
      ),
      interactionGate: gate,
      nativeBridgeConsumerFactory: factory,
      clock: { fixture.now }
    )
  }
}

private final class BridgeConsumerFactory: NativeApprovalBridgePostSigningConsumerProviding {
  let metadata: ApprovalHostBuildMetadataV1
  let consumer: (any ApprovalPostSigningConsumer)?
  private(set) var makeCalls = 0

  init(metadata: ApprovalHostBuildMetadataV1, consumer: (any ApprovalPostSigningConsumer)?) {
    self.metadata = metadata
    self.consumer = consumer
  }

  func makeConsumer(
    request _: NativeApprovalBridgeRequestV1,
    exchangeStartedAt _: Date
  ) throws -> any ApprovalPostSigningConsumer {
    makeCalls += 1
    guard let consumer else { throw ApprovalHostError.hostNotInstalled }
    return consumer
  }
}

private final class BridgeCandidateConsumer: ApprovalPostSigningConsumer {
  let candidate: NativeApprovalBridgeCandidateV1
  private(set) var calls = 0

  init(candidate: NativeApprovalBridgeCandidateV1) { self.candidate = candidate }

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeApprovalBridgeCandidateV1, ApprovalHostError> {
    calls += 1
    do {
      try sessionLease.withAuthenticatedSession { _ in () }
      return .success(candidate)
    } catch {
      return .failure(.enrollmentStateUnverified)
    }
  }
}

private final class BridgeRedemptionFactory: NativeApprovalBridgeRedemptionFactory {
  private let fixture: BridgeProducerTestFixture
  private(set) var makeCalls = 0

  init(fixture: BridgeProducerTestFixture) { self.fixture = fixture }

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

private final class BridgeTransport: NativeApprovalBridgeXPCTransport {
  private(set) var sendCount = 0
  private(set) var requestBytes: Data?
  private(set) var invalidated = false
  private var completion: ((NativeApprovalBridgeXPCWireResponse) -> Void)?

  func send(
    canonicalRequest: Data,
    completion: @escaping (NativeApprovalBridgeXPCWireResponse) -> Void
  ) {
    sendCount += 1
    requestBytes = canonicalRequest
    self.completion = completion
  }

  func invalidate() { invalidated = true }
  func reply(_ response: NativeApprovalBridgeXPCWireResponse) { completion?(response) }
}

private final class BridgeScheduler: ApprovalDeadlineScheduler {
  func schedule(after _: TimeInterval, action _: @escaping () -> Void) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class BridgeReviewer: ApprovalReviewer {
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

  func respond(_ value: ApprovalReviewDecision) { completion?(value) }
}

private final class BridgeAuthenticator: FreshAuthenticator {
  private var completion: ((ApprovalAuthenticationResult) -> Void)?

  func authenticate(
    reason _: String,
    completion: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond(_ value: ApprovalAuthenticationResult) { completion?(value) }
}

private final class BridgeSigner: ReceiptSigner {
  private var completion: ((Result<Data, ApprovalHostError>) -> Void)?
  private var permit: (any ReceiptSigningPermit)?

  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    self.permit = permit
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func respond() {
    guard permit?.claimSigning() == true else {
      completion?(.failure(.signingFailed))
      return
    }
    completion?(.success(Data(repeating: 0x5A, count: 64)))
  }
}

private final class BridgeSession: AuthenticatedApprovalSession, ReceiptRedemptionSession,
  @unchecked Sendable
{
  private(set) var invalidations = 0
  func invalidate() { invalidations += 1 }
}

private final class BridgeIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:" + String(repeating: "B", count: 43)
  }
}

private final class BridgeCurrentKeySigner: ReceiptSigner {
  private let key: P256.Signing.PrivateKey
  private var permit: (any ReceiptSigningPermit)?
  private var completion: ((Result<Data, ApprovalHostError>) -> Void)?

  init(key: P256.Signing.PrivateKey) { self.key = key }

  func sign(
    _ payload: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    self.permit = permit
    self.completion = completion
    self.payload = payload
    return NoopApprovalCancellation()
  }

  func respond() {
    guard let payload, permit?.claimSigning() == true else {
      completion?(.failure(.signingFailed))
      return
    }
    do {
      let signature = try key.signature(for: payload.domainSeparatedBytes).derRepresentation
      completion?(.success(signature))
    } catch {
      completion?(.failure(.signingFailed))
    }
  }

  private var payload: ReceiptSigningPayload?
}

private final class SynchronousBridgeCurrentKeySigner: ReceiptSigner {
  private let key: P256.Signing.PrivateKey

  init(key: P256.Signing.PrivateKey) { self.key = key }

  func sign(
    _ payload: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    guard permit.claimSigning() else {
      completion(.failure(.signingFailed))
      return NoopApprovalCancellation()
    }
    do {
      completion(.success(try key.signature(for: payload.domainSeparatedBytes).derRepresentation))
    } catch {
      completion(.failure(.signingFailed))
    }
    return NoopApprovalCancellation()
  }
}
